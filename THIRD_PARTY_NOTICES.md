# Third-party components

This project is licensed under GPL-3.0 (see `LICENSE`). It uses the following components,
which keep their own licenses:

| Component | Use | License |
|---|---|---|
| [DINOv2-small](https://huggingface.co/onnx-community/dinov2-small) (Meta AI) | AI similarity model, downloaded by `get_model.ps1` | Apache-2.0 |
| [PySide6 / Qt](https://www.qt.io/qt-for-python) | User interface | LGPL-3.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV) and its bundled FFmpeg libraries | Video decoding | BSD-3-Clause (PyAV), LGPL (FFmpeg) |
| [ONNX Runtime (DirectML)](https://github.com/microsoft/onnxruntime) | Running the AI model on GPU or CPU | MIT |
| [Pillow](https://python-pillow.org/) | Image decoding | MIT-CMU (HPND) |
| [pillow-heif](https://github.com/bigcat88/pillow_heif) | HEIC/HEIF photos | BSD-3-Clause (libheif: LGPL-3.0) |
| [NumPy](https://numpy.org/) | Hashing and vector maths | BSD-3-Clause |
| [Send2Trash](https://github.com/arsenetar/send2trash) | Sending files to the Recycle Bin | BSD-3-Clause |
