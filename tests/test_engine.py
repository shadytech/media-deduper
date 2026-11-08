"""End-to-end test of the engine on generated images.

Run:  .venv\\Scripts\\python.exe -m tests.test_engine
"""

import os
import shutil
import tempfile

import numpy as np
from PIL import Image, ImageEnhance

from dupefinder.engine import scan


def make_photo(seed: int, size=(800, 600)) -> Image.Image:
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w] / max(w, h)
    img = np.zeros((h, w, 3))
    for c in range(3):
        for _ in range(6):
            fx, fy, ph = rng.uniform(1, 8, 2).tolist() + [rng.uniform(0, 6)]
            img[..., c] += np.sin(fx * x * 6 + fy * y * 6 + ph)
    img = (img - img.min()) / (img.max() - img.min()) * 255
    return Image.fromarray(img.astype(np.uint8))


def build_fixture(root: str) -> dict[str, list[str]]:
    a, b = os.path.join(root, "A"), os.path.join(root, "A", "sub")
    os.makedirs(b)
    expected = {}

    p1 = make_photo(1)
    p1.save(os.path.join(a, "beach.jpg"), quality=95)
    shutil.copy(os.path.join(a, "beach.jpg"), os.path.join(b, "beach copy.jpg"))       # exact copy
    p1.resize((400, 300)).save(os.path.join(b, "beach_small.jpg"), quality=80)          # resized
    p1.save(os.path.join(b, "beach.png"))                                               # format change
    ImageEnhance.Brightness(p1).enhance(1.08).save(os.path.join(b, "beach_bright.jpg"))  # light edit
    expected["beach"] = ["beach.jpg", "beach copy.jpg", "beach_small.jpg", "beach.png", "beach_bright.jpg"]

    p2 = make_photo(2, (600, 900))
    p2.save(os.path.join(a, "portrait.webp"), quality=90)
    p2.save(os.path.join(b, "portrait.jpg"), quality=70)
    expected["portrait"] = ["portrait.webp", "portrait.jpg"]

    for s in range(3, 9):                                                                # unique images
        make_photo(s).save(os.path.join(a, f"unique_{s}.jpg"))
    with open(os.path.join(a, "broken.jpg"), "wb") as f:                                 # corrupt file
        f.write(b"not really a jpeg" * 10)
    return expected


def main():
    root = tempfile.mkdtemp(prefix="dupetest_")
    try:
        expected = build_fixture(root)
        cache = os.path.join(root, "cache.sqlite3")
        for run in (1, 2):  # second run exercises the cache
            res = scan([os.path.join(root, "A")], True, "Normal (resized / re-saved)",
                       cache_path=cache, workers=2)
            got = sorted(sorted(f.name for f in g.files) for g in res.groups)
            want = sorted(sorted(v) for v in expected.values())
            print(f"run {run}: {res.total_images} images, {len(res.groups)} groups, "
                  f"{len(res.unreadable)} unreadable, {res.elapsed:.2f}s")
            for g in res.groups:
                for f, m in zip(g.files, g.match):
                    print(f"   {f.name:20} {f.width}x{f.height:<5} {f.size:>8}  {m}")
            assert got == want, f"groups mismatch:\n got  {got}\n want {want}"
            assert [r.name for r in res.unreadable] == ["broken.jpg"]

        res = scan([os.path.join(root, "A")], True, "Exact copies only", cache_path=None, workers=2)
        got = [sorted(f.name for f in g.files) for g in res.groups]
        print("exact-only:", got)
        assert got == [["beach copy.jpg", "beach.jpg"]]
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
