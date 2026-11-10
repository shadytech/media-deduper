"""Per-file analysis: exact content hash and perceptual (visual) fingerprints.

Everything here runs locally with Pillow + NumPy. No network access.

Two perceptual fingerprints are computed for each image:

* pHash - 64-bit hash from the low-frequency DCT coefficients of a 32x32
  grayscale version of the image. Robust to resizing, re-compression,
  small colour/brightness changes and format conversion.
* dHash - 64-bit gradient hash (is each pixel brighter than its right
  neighbour on a 9x8 grid). Used as a second opinion to cut false matches.

Two images whose hashes differ in only a few bits look the same to a person.
"""

from __future__ import annotations

import hashlib
import warnings

import numpy as np
from PIL import Image, ImageOps

# Largest image the app will decode. Pillow refuses a file (DecompressionBombError)
# above twice this value, i.e. above one gigapixel. A crafted file can claim
# enormous dimensions in a few bytes and exhaust memory when decoded, so there
# has to be a ceiling; this one is far above any camera or scanner output.
# Refused files are listed as unreadable and never touched.
Image.MAX_IMAGE_PIXELS = 500_000_000
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

try:  # Optional HEIC/HEIF (iPhone photos) support
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - optional dependency
    HEIF_SUPPORTED = False

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".dib", ".gif",
    ".tif", ".tiff", ".webp", ".ico",
}
if HEIF_SUPPORTED:
    IMAGE_EXTS |= {".heic", ".heif"}


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0] /= np.sqrt(2.0)
    return m


_DCT32 = _dct_matrix(32)


def _bits_to_int(bits: np.ndarray) -> int:
    return int.from_bytes(np.packbits(bits.astype(np.uint8).ravel()).tobytes(), "big")


def transient(e: BaseException) -> bool:
    """True when a failure may not happen next time: the file is locked by another
    program, was moved, sits on a drive or cloud folder that is offline, or memory
    ran out. False when the file itself can't be decoded. Transient failures are
    not saved in the cache, so the file is tried again on the next scan."""
    if isinstance(e, (PermissionError, FileNotFoundError, TimeoutError, ConnectionError, MemoryError)):
        return True
    # Plain OSError with an error number comes from the operating system (open/read);
    # Pillow's own "cannot identify image" / "truncated" errors carry no number.
    return type(e) is OSError and e.errno is not None


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _flatten_alpha(im: Image.Image) -> Image.Image:
    """Composite transparent images onto white so hashes reflect what is seen."""
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    return im


def open_oriented(path: str, target: int) -> tuple[Image.Image, int, int]:
    """Open an image, fast-downscale where possible, apply EXIF rotation.

    Returns (image, original_width, original_height) with width/height
    reported as displayed (after EXIF rotation).
    """
    im = Image.open(path)
    w, h = im.size
    try:
        orient = im.getexif().get(0x0112, 1)
    except Exception:
        orient = 1
    if orient in (5, 6, 7, 8):
        w, h = h, w
    try:
        # JPEG only: decode at 1/2, 1/4 or 1/8 scale - a large speed-up.
        im.draft("RGB", (target, target))
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    return im, w, h


FLAT_STD = 3.0  # grey-level std-dev below which an image has no usable structure


def _hashes(gray: Image.Image) -> tuple[int | None, int | None]:
    """(phash, dhash) of a grayscale image.

    Blank / single-colour images get (None, None): their hashes are noise and
    would falsely match each other, so they are only compared as exact copies.
    """
    a = np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    if a.std() < FLAT_STD:
        return None, None
    dct = _DCT32 @ a @ _DCT32.T
    low = dct[:8, :8].ravel()
    phash = _bits_to_int(low > np.median(low[1:]))
    g = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    dhash = _bits_to_int(g[:, 1:] > g[:, :-1])
    return phash, dhash


def perceptual_info(path: str) -> tuple[int, int, int | None, int | None]:
    """Return (width, height, phash, dhash) for an image file."""
    im, w, h = open_oriented(path, 128)
    try:
        gray = _flatten_alpha(im).convert("L")
    finally:
        im.close()
    return (w, h, *_hashes(gray))


def analyze(task: tuple) -> dict:
    """Worker entry point (runs in a separate process).

    task = (path, want_perceptual, want_sha, want_ai). With want_ai the
    224x224 RGB model input is returned as 'ai_pixels'; the model itself runs
    in the main process (on the GPU when available).
    """
    path, want_perc, want_sha = task[:3]
    want_ai = len(task) > 3 and task[3]
    out: dict = {"path": path}
    if want_sha:
        try:
            out["sha256"] = sha256_file(path)
        except OSError as e:
            out["error"] = f"Read failed: {e}"
            out["retry"] = True
    if want_perc or want_ai:
        if want_perc:
            out["perc_done"] = True
        if want_ai:
            out["ai_done"] = True
        try:
            im, w, h = open_oriented(path, 256 if want_ai else 128)
            try:
                flat = _flatten_alpha(im)
                gray = flat.convert("L")
                rgb = flat.convert("RGB") if want_ai else None
            finally:
                im.close()
            ph, dh = _hashes(gray)
            out["width"], out["height"] = w, h
            if want_perc:
                out["phash"], out["dhash"] = ph, dh
            if want_ai and ph is not None:  # skip blank images
                from .ai import prepare
                out["ai_pixels"] = prepare(rgb)
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            if transient(e):
                out["retry"] = True
    return out
