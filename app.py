"""Image & Video Duplicate Finder 2.0 - entry point.

GUI:       .venv\\Scripts\\pythonw.exe app.py
Headless:  app.py --scan "D:\\Photos" "E:\\Videos" --report dupes.csv
                  [--level exact|strict|normal|loose|ai|ai-loose]
                  [--media both|images|videos] [--photo-video] [--no-rotations]
"""

import multiprocessing
import sys


def headless(argv: list[str]) -> int:
    import argparse
    import csv

    from dupefinder.engine import CLI_LEVELS, scan

    ap = argparse.ArgumentParser(prog="ImageDupeFinder")
    ap.add_argument("--scan", nargs="+", required=True, metavar="FOLDER")
    ap.add_argument("--report", required=True, metavar="CSV")
    ap.add_argument("--level", choices=list(CLI_LEVELS), default="normal")
    ap.add_argument("--media", choices=["both", "images", "videos"], default="both")
    ap.add_argument("--photo-video", action="store_true",
                    help="AI levels: also report photos that show a frame of a video")
    ap.add_argument("--no-subfolders", action="store_true")
    ap.add_argument("--no-rotations", action="store_true", help="AI levels: skip rotated-copy detection")
    a = ap.parse_args(argv)
    res = scan(a.scan, not a.no_subfolders, CLI_LEVELS[a.level], ai_rotations=not a.no_rotations,
               media=a.media, photo_video=a.photo_video)
    with open(a.report, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["group", "group_type", "path", "media", "size_bytes", "width", "height",
                    "duration_s", "match"])
        for gi, g in enumerate(res.groups, start=1):
            for r, m in zip(g.files, g.match):
                w.writerow([gi, g.kind, r.path, r.media, r.size, r.width, r.height,
                            round(r.duration, 2), m])
    return 0


def main() -> None:
    multiprocessing.freeze_support()  # required for worker processes in a packaged .exe
    if "--scan" in sys.argv:
        sys.exit(headless(sys.argv[1:]))
    from dupefinder.gui import run

    sys.exit(run())


if __name__ == "__main__":
    main()
