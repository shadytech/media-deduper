"""Video decoding and per-video fingerprints (PyAV / bundled FFmpeg, all local).

A video is treated as a timed sequence of images:

* Proportional frames - 16 frames at fixed fractions of the duration.
  Re-encoded / resized copies line up frame-for-frame, so comparing their
  perceptual hashes detects "same video, different file".
* Timed frames (AI modes) - one frame every 2 s (capped), fed to the
  DINOv2 model. Timestamps let us detect trimmed clips by finding a
  consistent time offset between two videos.

Blank frames (fades, black intros) are flagged and ignored for matching.
"""

from __future__ import annotations

import hashlib
import os

import numpy as np
from PIL import Image

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".mts", ".m2ts",
    ".ts", ".3gp", ".3g2", ".mpg", ".mpeg", ".flv", ".vob", ".ogv",
}

PROP_FRACTIONS = np.linspace(0.03, 0.97, 16)
TIMED_INTERVAL = 2.0     # seconds between AI frames
TIMED_MAX = 240          # cap per video; longer videos get a wider interval
PARTIAL_HASH_MIN = 64 << 20  # files above 64 MB get a quick partial hash first


def timed_interval(duration: float) -> float:
    return max(TIMED_INTERVAL, duration / TIMED_MAX) if duration > 0 else TIMED_INTERVAL


def partial_hash(path: str, size: int, chunk: int = 1 << 20) -> str:
    """Hash of 8 evenly spaced 1 MB chunks - a cheap pre-filter for huge files."""
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        for k in range(8):
            f.seek(max(0, (size - chunk) * k // 7))
            h.update(f.read(chunk))
    return "p:" + h.hexdigest()


def _open(path: str):
    import av

    av.logging.set_level(av.logging.PANIC)  # keep FFmpeg quiet about damaged files
    container = av.open(path)
    if not container.streams.video:
        container.close()
        raise ValueError("no video stream")
    stream = container.streams.video[0]
    # Single-threaded decode: FFmpeg frame-threading can deadlock when a
    # container is closed mid-stream, and we already parallelise across
    # videos with worker processes.
    stream.codec_context.thread_count = 1
    return container, stream


def _duration(container, stream) -> float:
    if stream.duration and stream.time_base:
        return float(stream.duration * stream.time_base)
    if container.duration:
        return container.duration / 1_000_000
    return 0.0


def _to_image(frame) -> Image.Image:
    im = frame.to_image()  # RGB
    rot = getattr(frame, "rotation", 0) or 0
    if rot % 360:
        im = im.rotate(rot, expand=True)  # display-matrix angle is counter-clockwise
    return im


def sample_frames(container, stream, times: list[float], max_side: int = 320):
    """Yield (t, PIL image) for the first frame at/after each requested time.

    Seeks only when the next target is far ahead; otherwise decodes forward,
    skipping non-reference frames for speed."""
    tb = float(stream.time_base)
    start = float(stream.start_time * stream.time_base) if stream.start_time else 0.0
    stream.codec_context.skip_frame = "NONREF"
    pos = None
    it = None
    for t in times:
        target = start + t
        if pos is None or target - pos > 4.0 or target < pos:
            container.seek(int(max(target - 0.05, 0) / tb), stream=stream, backward=True)
            it = container.decode(stream)
        frame = None
        for f in it:
            if f.pts is None:
                continue
            pos = f.pts * tb
            if pos >= target - 0.02:
                frame = f
                break
        if frame is None:
            break  # past the end
        im = _to_image(frame)
        im.thumbnail((max_side, max_side))
        yield t, im


def probe(path: str) -> dict:
    container, stream = _open(path)
    try:
        cc = stream.codec_context
        w, h = cc.width, cc.height
        dur = _duration(container, stream)
        rot = 0
        try:  # rotation comes from the first decoded frame's display matrix
            for f in container.decode(stream):
                rot = getattr(f, "rotation", 0) or 0
                break
        except Exception:
            pass
        if rot % 180:
            w, h = h, w
        rate = stream.average_rate or stream.guessed_rate
        return {
            "width": w, "height": h, "duration": dur,
            "fps": float(rate) if rate else 0.0,
            "codec": cc.name or "",
            "bitrate": int(container.bit_rate or 0),
        }
    finally:
        container.close()


def _is_flat(gray: Image.Image) -> bool:
    from .hashing import FLAT_STD
    return np.asarray(gray.resize((32, 32)), dtype=np.float32).std() < FLAT_STD


def analyze_video(task: tuple) -> dict:
    """Worker entry point. task = (path, size, sha_mode, want_prop, want_timed).

    sha_mode: None, "full" or "partial"."""
    from .hashing import _hashes, sha256_file

    path, size, sha_mode, want_prop, want_timed = task
    out: dict = {"path": path, "media": "video"}
    try:
        if sha_mode == "full":
            out["sha256"] = sha256_file(path)
        elif sha_mode == "partial":
            out["sha256"] = partial_hash(path, size)
    except OSError as e:
        out["error"] = f"Read failed: {e}"
    if not (want_prop or want_timed):
        return out
    try:
        meta = probe(path)
        out.update(meta)
        dur = meta["duration"]
        container, stream = _open(path)
        try:
            if want_prop:
                times = [float(f * dur) for f in PROP_FRACTIONS] if dur > 0 else [0.0]
                ph, dh = [], []
                for _, im in sample_frames(container, stream, times, 160):
                    p, d = _hashes(im.convert("L"))  # (None, None) for blank frames
                    ph.append(p)
                    dh.append(d)
                out["prop_phash"] = ph
                out["prop_dhash"] = dh
                out["prop_done"] = True
            if want_timed:
                from .ai import prepare
                step = timed_interval(dur)
                times = list(np.arange(0.0, max(dur - 0.05, 0.0) + 1e-6, step)) or [0.0]
                ts, pix = [], []
                for t, im in sample_frames(container, stream, times, 256):
                    if _is_flat(im.convert("L")):
                        continue
                    ts.append(float(t))
                    pix.append(prepare(im))
                out["timed_times"] = ts
                out["timed_pixels"] = np.stack(pix) if pix else np.zeros((0, 224, 224, 3), np.uint8)
                out["timed_done"] = True
        finally:
            container.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["prop_done"] = bool(want_prop)
        out["timed_done"] = bool(want_timed)
    return out


def thumbnail_strip(path: str, size: int, n: int = 4) -> Image.Image:
    """2x2 grid of frames from across the video, fitting a size x size box (preview pane)."""
    container, stream = _open(path)
    try:
        dur = _duration(container, stream)
        times = [dur * (k + 0.5) / n for k in range(n)] if dur > 0 else [0.0]
        frames = [im for _, im in sample_frames(container, stream, times, size)]
    finally:
        container.close()
    if not frames:
        raise ValueError("no frames")
    cols = 1 if len(frames) == 1 else 2
    rows = (len(frames) + cols - 1) // cols
    gap = 3
    cw = (size - gap * (cols - 1)) // cols
    ar = frames[0].width / max(frames[0].height, 1)
    ch = max(1, min(int(cw / ar), (size - gap * (rows - 1)) // rows))
    cw = int(ch * ar) if ch * ar < cw else cw
    grid = Image.new("RGB", (cols * cw + gap * (cols - 1), rows * ch + gap * (rows - 1)), (20, 20, 20))
    for k, im in enumerate(frames):
        tile = im.resize((cw, ch))
        grid.paste(tile, ((k % cols) * (cw + gap), (k // cols) * (ch + gap)))
    return grid


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS
