"""Video matching tests on generated clips. Run:  .venv\\Scripts\\python.exe -m tests.test_video"""

import os
import shutil
import sys
import tempfile

from dupefinder.engine import scan
from tests import make_videos


def show(res):
    for g in res.groups:
        print(f"  [{g.kind}]")
        for f, m in zip(g.files, g.match):
            print(f"     {f.name:22} {f.width}x{f.height:<4} {f.duration:5.1f}s {f.codec:6} {m}")


def names(res, kind="duplicate"):
    return sorted(sorted(f.name for f in g.files) for g in res.groups if g.kind == kind)


def main():
    if not make_videos.available():
        print("SKIPPED: Windows wallpapers not found")
        return
    root = tempfile.mkdtemp(prefix="dupevid_")
    try:
        expected = make_videos.build(root)
        shutil.copy(os.path.join(root, "movie.mp4"), os.path.join(root, "movie - Copy.mp4"))
        movie_all = sorted(expected["movie"] + ["movie - Copy.mp4"])
        cache = os.path.join(root, "cache.sqlite3")

        # Exact: only the byte-identical copy
        res = scan([root], level="Exact copies only", cache_path=cache, media="videos", workers=4)
        print(f"exact: {res.total_videos} videos, {len(res.groups)} groups")
        assert names(res) == [["movie - Copy.mp4", "movie.mp4"]], names(res)

        # Normal: re-encodes / other codecs / rotation tag; NOT the clip, intro or unrelated vlogs
        res = scan([root], level="Normal (resized / re-saved)", cache_path=cache, media="videos",
                   workers=4)
        print(f"normal: {len(res.groups)} groups in {res.elapsed:.1f}s")
        show(res)
        got = names(res)
        assert len(got) == 1, got
        must = {"movie.mp4", "movie - Copy.mp4", "movie_small.mp4", "movie.avi", "movie_hevc.mkv",
                "movie_rotated.mp4"}
        assert must <= set(got[0]), got
        assert not {"movie_clip.mp4", "movie_intro.mp4"} & set(got[0]), got

        # AI: everything derived from the movie, incl. trimmed clip and black intro;
        # the vlogs share an intro card but must stay separate. Photo -> video group.
        for run in (1, 2):  # 2nd run comes from cache
            res = scan([root], level="AI: edits, crops & rotations", cache_path=cache,
                       media="both", photo_video=True, workers=4)
            print(f"AI run {run}: {res.total_videos} videos + {res.total_images} images, "
                  f"{len(res.groups)} groups in {res.elapsed:.1f}s (AI on {res.ai_device})")
            if run == 1:
                show(res)
            assert names(res) == [movie_all], names(res)
            piv = names(res, "photo_in_video")
            assert len(piv) == 1 and "frame_from_movie.jpg" in piv[0], piv
            clip = next(m for g in res.groups for f, m in zip(g.files, g.match)
                        if f.name == "movie_clip.mp4")
            assert clip.startswith("Clip 0:04") or clip.startswith("Clip 0:05") or \
                clip.startswith("Clip 0:06"), clip
        print("VIDEO TESTS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
