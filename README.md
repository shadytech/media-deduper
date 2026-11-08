# Image & Video Duplicate Finder 2.1

A Windows desktop app that scans folders of images **and videos** and finds duplicates
using **file size + content hashing** (exact copies) and **local image recognition**
(perceptual hashing and an optional AI vision model). Nothing is uploaded anywhere;
all processing happens on this PC.

Settings and the scan cache are kept in `%LOCALAPPDATA%\ImageDupeFinder2\`.

## Setup (Windows 10/11)

1. Install [Python 3.11 or newer](https://www.python.org/downloads/) and tick "Add python.exe to PATH".
2. Clone or download this repository.
3. Download the AI model (about 84 MB; it is too large to keep in the repository):
   `powershell -ExecutionPolicy Bypass -File get_model.ps1`
   Without it the app still works, but the AI match levels are unavailable.

## Run

* **From source:** double-click `run.bat` (creates `.venv` and installs the requirements on first run).
* **Build a standalone exe:** `powershell -ExecutionPolicy Bypass -File build.ps1`, then run
  `dist\ImageDupeFinder\ImageDupeFinder.exe` (copy the whole folder anywhere).
* **Headless / scripted:**
  `ImageDupeFinder.exe --scan "D:\Photos" "E:\Videos" --report dupes.csv --level ai --media both --photo-video`

## How duplicates are detected

| Step | What it does |
|---|---|
| File size | Only files with the same byte size can be exact copies, so SHA-256 is computed just for those. Videos over 64 MB get a quick partial hash first, and the full hash runs only if those collide. |
| Exact match | Same size + same SHA-256 means a byte-for-byte copy. |
| Image recognition | Each image is reduced to a 64-bit **pHash** (DCT of a 32×32 grayscale image) and a 64-bit **dHash** (gradient). Pictures whose hashes differ by only a few bits are treated as the same picture. Aspect ratios must also match. |
| Grouping | Matches are merged into groups. The best copy is listed first as the reference: highest resolution, then (for videos) highest bitrate and longest duration, then largest file. |

Match levels: **Exact only**, **Strict** (≤4 bits), **Normal** (≤8), **Loose** (≤12).
Blank or single-colour images are only matched as exact copies.

### AI modes (local vision model)

| Level | Finds |
|---|---|
| **AI: edits, crops & rotations** | Everything Normal finds, plus crops, 90°/180°/270° rotations, mirroring, grayscale / colour grading, watermarks and banners. Cosine similarity ≥ 0.90. |
| **AI loose: similar-looking (review!)** | ≥ 0.87. Also groups different shots that look alike. Review before deleting. |

* Model: **DINOv2-small** (Meta, Apache-2.0), stored at `models\dinov2-small.onnx` by
  `get_model.ps1` (from huggingface.co/onnx-community/dinov2-small). It runs through ONNX Runtime
  on the GPU (DirectML) or the CPU. No network access is needed or used.
* Each image becomes a 768-number vector: the CLS token plus the averaged patch tokens.
  With **Detect rotated copies** on, 4 rotations are embedded per image (about 4× AI work).
* Thresholds were calibrated on real photos. Unrelated photos scored below about 0.83, and edited
  copies scored about 0.90 or higher.
* Speed (RTX 5080): about 320 images/s on the GPU, about 50 images/s on the CPU (Ryzen 7 9850X3D) only.

## Videos

Formats: MP4, M4V, MOV, MKV, AVI, WMV, WebM, MTS/M2TS/TS, 3GP, MPG/MPEG, FLV, VOB, OGV.
Decoding uses **PyAV** (bundled FFmpeg libraries, LGPL). The rotation flag that phones write
is applied, so a portrait phone video matches its upright re-export.

| Duplicate type | How it's detected | Level |
|---|---|---|
| Byte-identical copy | Size + SHA-256 | All |
| Re-encoded, resized, different codec or container | 16 frames at fixed points (3% … 97% of the duration) are fingerprinted. Durations must agree within 2%, and ≥ 75% of the aligned frames must match. | Normal and up |
| Colour-graded, watermarked or cropped version | AI vectors of frames sampled every 2 s, compared at the same timestamps | AI |
| Trimmed clip, or a version with an added intro or outro | The shorter video's frames must match the longer one at a single consistent time offset for ≥ 60% of its frames. The label shows where: "Clip 0:04–0:12 of movie.mp4", "Contains movie.mp4 at 0:02". | AI |

Protection against false matches:
* Black or blank frames (fades, black intros) are ignored.
* Frames shared with several otherwise unrelated videos, such as a channel intro or logo
  sequence, are ignored. This keeps different videos with the same intro apart.
* A frame that matches many moments of the other video (a static shot) gets only a partial vote.

**Match photos to video frames** (AI modes, off by default) lists photos that show a frame of a
video, such as stills taken from a clip or the photo half of a Live Photo. These appear as
separate "Photos from a video" groups and are **never auto-marked**.

The preview shows a 2×2 grid of frames from across each video. Double-click opens it in your
default player. Video speed on the test machine is about 0.1–0.3 s per short clip. Frames are
decoded in parallel worker processes, one video per CPU core. Long videos are sampled at most
240 times.

## Removing duplicates

Tick files manually or use **Auto-mark duplicates**, which keeps one file per
group by your chosen rule: highest resolution, largest file, longest duration, oldest, newest
or shortest path. Then **Send to Recycle Bin** (recoverable) or **Move to folder**. The app
warns you if every copy in a group is marked.

Image formats: JPEG, PNG, BMP, GIF, TIFF, WebP, ICO, HEIC/HEIF.

## Tests

```
.venv\Scripts\python.exe -m tests.test_engine
.venv\Scripts\python.exe -m tests.test_gui
.venv\Scripts\python.exe -m tests.test_trash_progress   # fake Recycle Bin; nothing is deleted
.venv\Scripts\python.exe -m tests.test_eta     # time-remaining estimate
.venv\Scripts\python.exe -m tests.test_ai      # uses Windows' built-in wallpapers
.venv\Scripts\python.exe -m tests.test_video   # generates test videos from the wallpapers
```

## License

GPL-3.0 (see `LICENSE`). Third-party components and their licenses are listed in
`THIRD_PARTY_NOTICES.md`.
