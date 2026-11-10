"""Local AI image embeddings (DINOv2-small via ONNX Runtime).

The model turns each image into a 768-number vector describing its visual
content. Two copies of the same photo produce nearly the same vector even
after cropping, rotation, filters or heavy re-compression, which plain
perceptual hashes cannot handle.

Runs fully offline: the model file ships with the app (models/), inference
uses ONNX Runtime on the GPU (DirectML) when available, otherwise the CPU.
"""

from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image

MODEL_NAME = "dinov2-small"
MODEL_FILE = "dinov2-small.onnx"
INPUT_SIZE = 224
EMBED_DIM = 768  # 384 CLS + 384 mean-of-patches
EMBED_VERSION = f"{MODEL_NAME}/cls+mean/v1"  # bump to invalidate cached embeddings
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def model_path() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates += [os.path.join(getattr(sys, "_MEIPASS", ""), "models", MODEL_FILE),
                       os.path.join(os.path.dirname(sys.executable), "models", MODEL_FILE)]
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "models", MODEL_FILE))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return candidates[-1]


def ai_available() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:
        return False, f"ONNX Runtime not installed ({e})"
    if not os.path.isfile(model_path()):
        return False, f"Model file not found: {model_path()}"
    return True, ""


def prepare(im: Image.Image) -> np.ndarray:
    """RGB PIL image -> 224x224x3 uint8 array (whole image, no centre crop,
    so crops of a photo still overlap the original's content)."""
    return np.asarray(im.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BICUBIC),
                      dtype=np.uint8)


class Embedder:
    def __init__(self, use_gpu: bool = True):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        path = model_path()
        providers = ["CPUExecutionProvider"]
        if use_gpu and "DmlExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "DmlExecutionProvider")
        try:
            self.session = ort.InferenceSession(path, opts, providers=providers)
        except Exception:
            self.session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self.device = "GPU" if "DmlExecutionProvider" in self.session.get_providers() else "CPU"
        self.input = self.session.get_inputs()[0].name

    def embed(self, pixels: list[np.ndarray] | np.ndarray, batch: int = 32) -> np.ndarray:
        """uint8 (N,224,224,3) -> L2-normalised float32 (N,768) embeddings.

        The vector is the CLS token concatenated with the mean of the patch
        tokens: CLS is best for crops/overlays, the patch mean for blur,
        downscaling and rotation (calibrated on real photos)."""
        pixels = np.asarray(pixels)
        out = []
        for s in range(0, len(pixels), batch):
            x = pixels[s : s + batch].astype(np.float32) / 255.0
            x = ((x - _MEAN) / _STD).transpose(0, 3, 1, 2).copy()
            h = self.session.run(None, {self.input: x})[0]    # (n, 1+patches, 384)
            cls, mean = _l2(h[:, 0, :]), _l2(h[:, 1:, :].mean(axis=1))
            out.append(_l2(np.concatenate([cls, mean], axis=1)))
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, EMBED_DIM), np.float32)

    def embed_views(self, pixels: list[np.ndarray], rotations: bool) -> np.ndarray:
        """Embeddings per image: (N, V, 768). V=4 (0/90/180/270 deg) with rotations, else 1."""
        if not rotations:
            return self.embed(pixels)[:, None, :]
        views = [np.rot90(p, k) for p in pixels for k in range(4)]
        return self.embed(views).reshape(len(pixels), 4, EMBED_DIM)


def _l2(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)
