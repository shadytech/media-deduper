"""AI-mode test on real photos (uses the wallpapers that ship with Windows 11).

Run:  .venv\\Scripts\\python.exe -m tests.test_ai
"""

import os
import shutil
import sys
import tempfile

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from dupefinder.engine import scan

WEB = r"C:\Windows\Web"
SOURCES = {  # distinct, unrelated photos
    "lake": r"Wallpaper\ThemeB\img24.jpg",
    "hills": r"Wallpaper\ThemeC\img28.jpg",
    "flower": r"Wallpaper\ThemeD\img32.jpg",
    "u1": r"Screen\img100.jpg",
    "u2": r"Screen\img103.jpg",
    "u3": r"Screen\img104.jpg",
}


def edits(im: Image.Image) -> dict[str, Image.Image]:
    w, h = im.size
    banner = im.copy()
    ImageDraw.Draw(banner).rectangle((0, int(h * .85), w, h), fill=(0, 0, 0))
    return {
        "crop": im.crop((int(w * .1), int(h * .1), int(w * .9), int(h * .9))),
        "rot90": im.rotate(90, expand=True),
        "gray": im.convert("L"),
        "mirror": ImageOps.mirror(im),
        "punchy": ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(1.3)).enhance(1.3),
        "banner": banner,
        "thumb": im.resize((w // 5, h // 5)),
    }


def main():
    if not all(os.path.exists(os.path.join(WEB, p)) for p in SOURCES.values()):
        print("SKIPPED: Windows wallpapers not found")
        return
    root = tempfile.mkdtemp(prefix="dupeai_")
    try:
        expected = []
        for key, rel in SOURCES.items():
            im = Image.open(os.path.join(WEB, rel)).convert("RGB")
            im.save(os.path.join(root, f"{key}.jpg"), quality=92)
            if key.startswith("u"):
                continue
            names = [f"{key}.jpg"]
            for ename, e in edits(im).items():
                n = f"{key}_{ename}.jpg"
                e.save(os.path.join(root, n), quality=85)
                names.append(n)
            expected.append(sorted(names))
        expected.sort()

        for rotations in (True, False):
            res = scan([root], True, "AI: edits, crops & rotations", cache_path=None,
                       workers=4, ai_rotations=rotations)
            got = sorted(sorted(f.name for f in g.files) for g in res.groups)
            print(f"rotations={rotations}: {len(res.groups)} groups in {res.elapsed:.1f}s "
                  f"(AI on {res.ai_device})")
            if rotations:
                for g in res.groups:
                    for f, m in zip(g.files, g.match):
                        print(f"   {f.name:22} {m}")
                assert got == expected, f"\n got  {got}\n want {expected}"
            else:  # without rotation views, the 90-degree copies are expected to drop out
                rot = {n for grp in got for n in grp if "rot90" in n}
                assert not rot, rot

        res = scan([root], True, "Normal (resized / re-saved)", cache_path=None, workers=4)
        found = sum(len(g.files) for g in res.groups)
        print(f"for comparison, Normal (non-AI) mode groups {found} of "
              f"{sum(map(len, expected))} files")
        assert found < sum(map(len, expected))
        print("AI TESTS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
