"""Generate test videos from Windows' built-in wallpapers (slow pan/zoom shots).

Each "shot" is a Ken-Burns style move over one photo, so frames change over
time like real footage.
"""

from __future__ import annotations

import os

import av
import numpy as np
from PIL import Image, ImageDraw

WEB = r"C:\Windows\Web"
PHOTOS = {
    "lake": r"Wallpaper\ThemeB\img24.jpg",
    "hills": r"Wallpaper\ThemeC\img28.jpg",
    "flower": r"Wallpaper\ThemeD\img32.jpg",
    "s100": r"Screen\img100.jpg",
    "s103": r"Screen\img103.jpg",
    "s104": r"Screen\img104.jpg",
    "s105": r"Wallpaper\ThemeB\img25.jpg",  # (Screen\img105 is a flat colour)
    "bloom": r"Wallpaper\Windows\img0.jpg",
}
FPS = 24


def available() -> bool:
    return all(os.path.exists(os.path.join(WEB, p)) for p in PHOTOS.values())


def shot(key: str, seconds: float, size=(640, 360), zoom=(1.0, 1.35), pan=(0.0, 0.25)):
    """Yield RGB frames: zoom + horizontal pan across a photo."""
    src = Image.open(os.path.join(WEB, PHOTOS[key])).convert("RGB")
    src = src.resize((1280, int(1280 * src.height / src.width)))
    W, H = size
    n = int(seconds * FPS)
    for i in range(n):
        a = i / max(n - 1, 1)
        z = zoom[0] + (zoom[1] - zoom[0]) * a
        cw, ch = src.width / z, src.width / z * H / W
        ch = min(ch, src.height)
        cx = (src.width - cw) * (0.5 + (pan[0] + (pan[1] - pan[0]) * a) * 0.8)
        cx = min(max(cx, 0), src.width - cw)
        cy = (src.height - ch) / 2
        yield src.crop((int(cx), int(cy), int(cx + cw), int(cy + ch))).resize(size)


def card(text: str, seconds: float, size=(640, 360), color=(30, 60, 160)):
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    for k in range(6):
        d.ellipse((40 + k * 90, 120, 120 + k * 90, 240), outline="white", width=6)
    d.text((40, 300), text, fill="white")
    for _ in range(int(seconds * FPS)):
        yield im


def black(seconds: float, size=(640, 360)):
    im = Image.new("RGB", size, (0, 0, 0))
    for _ in range(int(seconds * FPS)):
        yield im


def write(path: str, frames, codec="libx264", size=None, bitrate=None, rotation=0,
          transform=None, fps=FPS):
    frames = list(frames)
    if transform:
        frames = [transform(f) for f in frames]
    if size:
        frames = [f.resize(size) for f in frames]
    w, h = frames[0].size
    with av.open(path, "w") as out:
        s = out.add_stream(codec, rate=fps)
        s.width, s.height = w - w % 2, h - h % 2
        s.pix_fmt = "yuv420p"
        if codec == "libx265":
            s.options = {"x265-params": "log-level=none"}
        if bitrate:
            s.bit_rate = bitrate
        if rotation:
            s.set_display_rotation(rotation)
        for f in frames:
            if f.size != (s.width, s.height):
                f = f.crop((0, 0, s.width, s.height))
            for p in s.encode(av.VideoFrame.from_ndarray(np.asarray(f), format="rgb24")):
                out.mux(p)
        for p in s.encode():
            out.mux(p)


def base_movie():
    return [*shot("lake", 6), *shot("hills", 6, pan=(0.3, 0.0)), *shot("flower", 6, zoom=(1.3, 1.0))]


def build(root: str) -> dict[str, list[str]]:
    """Create the fixture; returns expected groups (by file name) for AI mode."""
    os.makedirs(root, exist_ok=True)
    movie = base_movie()
    p = lambda n: os.path.join(root, n)  # noqa: E731

    write(p("movie.mp4"), movie)                                              # original
    write(p("movie_small.mp4"), movie, size=(320, 180), bitrate=300_000)      # re-encoded smaller
    write(p("movie.avi"), movie, codec="mpeg4", bitrate=1_500_000)            # other codec/container
    write(p("movie_hevc.mkv"), movie, codec="libx265" if "libx265" in av.codecs_available else "mpeg4")
    write(p("movie_rotated.mp4"), movie, rotation=90,                         # phone-style rotation tag
          transform=lambda f: f.rotate(-90, expand=True))
    write(p("movie_clip.mp4"), movie[int(5 * FPS):int(13 * FPS)])             # trimmed clip 0:05-0:13
    write(p("movie_intro.mp4"), [*black(2), *movie])                          # black intro added
    marked = []
    for f in movie:
        f = f.copy()
        ImageDraw.Draw(f).rectangle((0, 300, 640, 360), fill=(0, 0, 0))
        marked.append(f)
    write(p("movie_banner.mp4"), marked)                                      # banner overlay

    # Unrelated videos that share an identical 4 s intro card - must NOT match.
    write(p("vlog1.mp4"), [*card("MY CHANNEL", 4), *shot("s100", 8), *shot("s103", 6)])
    write(p("vlog2.mp4"), [*card("MY CHANNEL", 4), *shot("s104", 8), *shot("s105", 6)])
    write(p("vlog3.mp4"), [*card("MY CHANNEL", 4), *shot("bloom", 10)])
    write(p("other.mp4"), [*shot("s105", 7, zoom=(1.4, 1.0)), *shot("s100", 7, pan=(0.25, 0.0))])

    # A still photo taken from the movie (photo <-> video matching).
    movie[int(9 * FPS)].save(p("frame_from_movie.jpg"), quality=90)

    return {
        "movie": sorted(["movie.mp4", "movie_small.mp4", "movie.avi", "movie_hevc.mkv",
                         "movie_rotated.mp4", "movie_clip.mp4", "movie_intro.mp4",
                         "movie_banner.mp4"]),
    }


if __name__ == "__main__":
    import sys
    import time
    t = time.perf_counter()
    build(sys.argv[1])
    print(f"built in {time.perf_counter() - t:.1f}s")
