"""Regression tests for bugs found in review. Nothing here touches the Recycle Bin.

Run:  .venv\\Scripts\\python.exe -m tests.test_fixes

(GUI imports stay inside functions: worker processes re-import this module.)
"""

import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dupefinder import engine  # noqa: E402
from dupefinder.hashing import analyze, transient  # noqa: E402
from tests.test_engine import make_photo  # noqa: E402

NORMAL = "Normal (resized / re-saved)"
EXACT = "Exact copies only"
AI = "AI: edits, crops & rotations"


# ---- stand-ins for the worker entry point (module level: sent to worker processes)

def crashy_analyze(task):
    """Like analyze(), but a file named crash*.jpg kills its worker process outright."""
    if os.path.basename(task[0]).startswith("crash"):
        os._exit(3)
    return analyze(task)


def slow_analyze(task):
    time.sleep(60)
    return analyze(task)


# ---- engine

def test_empty_video_cache(root):
    """A video with no usable AI frames (all black) used to make every later AI scan fail."""
    from dupefinder.ai import ai_available
    from tests import make_videos as mv
    if not ai_available()[0]:
        print("  (skipped: AI model not installed)")
        return
    d = os.path.join(root, "black")
    os.makedirs(d)
    mv.write(os.path.join(d, "black.mp4"), mv.black(2.0))
    shutil.copy(os.path.join(d, "black.mp4"), os.path.join(d, "black copy.mp4"))
    cache = os.path.join(root, "black.sqlite3")
    for run in (1, 2):
        res = engine.scan([d], level=AI, cache_path=cache)
        assert len(res.groups) == 1, (run, res.groups)   # still found, as exact copies


def test_partial_vs_full_hash(root):
    """A big video with a full hash kept from an earlier scan must still match a new copy."""
    d = os.path.join(root, "big")
    os.makedirs(d)
    data = os.urandom(300_000)
    for name in ("a.mp4", "c.mp4"):
        open(os.path.join(d, name), "wb").write(data)
    cache = os.path.join(root, "big.sqlite3")
    old = engine.PARTIAL_HASH_MIN
    engine.PARTIAL_HASH_MIN = 1000   # treat these as "big": partial hash first
    try:
        res = engine.scan([d], level=EXACT, cache_path=cache)
        assert len(res.groups) == 1                      # a + c collide -> both get a full hash
        os.remove(os.path.join(d, "c.mp4"))
        shutil.copy(os.path.join(d, "a.mp4"), os.path.join(d, "b.mp4"))
        res = engine.scan([d], level=EXACT, cache_path=cache)
        names = [sorted(f.name for f in g.files) for g in res.groups]
        assert names == [["a.mp4", "b.mp4"]], names
        assert res.groups[0].match[1] == "Exact copy"
    finally:
        engine.PARTIAL_HASH_MIN = old


def test_symlinks_skipped(root):
    """A link to a file is not a copy of it. (Faked: making links needs admin rights.)"""
    d = os.path.join(root, "links")
    os.makedirs(d)
    real = os.path.join(d, "a.jpg")
    make_photo(1).save(real)

    class Entry:
        def __init__(self, name, link):
            self.name, self.path, self._link = name, os.path.join(d, name), link

        def is_symlink(self):
            return self._link

        def is_dir(self, follow_symlinks=True):
            return False

        def stat(self):
            return os.stat(real)

    orig = os.scandir
    os.scandir = lambda p: [Entry("a.jpg", False), Entry("link.jpg", True)]
    try:
        recs = engine.find_media([d], True, lambda: False, lambda *a: None)
    finally:
        os.scandir = orig
    assert [r.name for r in recs] == ["a.jpg"], [r.name for r in recs]


def test_worker_crash(root):
    """One file that kills its worker must not abort the scan."""
    d = os.path.join(root, "crash")
    os.makedirs(d)
    for i in range(10):
        make_photo(i).save(os.path.join(d, f"p{i}.jpg"))
    shutil.copy(os.path.join(d, "p3.jpg"), os.path.join(d, "p3 copy.jpg"))
    make_photo(50).save(os.path.join(d, "crash.jpg"))
    orig = engine.analyze
    engine.analyze = crashy_analyze
    try:
        res = engine.scan([d], level=NORMAL, cache_path=None, workers=4)
    finally:
        engine.analyze = orig
    assert [sorted(f.name for f in g.files) for g in res.groups] == [["p3 copy.jpg", "p3.jpg"]]
    bad = [(r.name, r.error) for r in res.unreadable]
    assert len(bad) == 1 and bad[0][0] == "crash.jpg" and "crashed" in bad[0][1], bad
    print("  crash reported as:", bad[0][1])


def test_cancel_is_prompt(root):
    """Cancel must not wait for the files being read (here: 60 s each)."""
    d = os.path.join(root, "slow")
    os.makedirs(d)
    for i in range(6):
        make_photo(i).save(os.path.join(d, f"s{i}.jpg"))
    stop = threading.Event()
    out = {}

    def run():
        try:
            engine.scan([d], level=NORMAL, cache_path=None, workers=2, cancelled=stop.is_set)
            out["r"] = "finished"
        except engine.ScanCancelled:
            out["r"] = "cancelled"

    orig = engine.analyze
    engine.analyze = slow_analyze
    try:
        th = threading.Thread(target=run)
        th.start()
        time.sleep(3)          # workers are up and sleeping
        t0 = time.monotonic()
        stop.set()
        th.join(15)
        took = time.monotonic() - t0
    finally:
        engine.analyze = orig
    assert out.get("r") == "cancelled" and took < 5, (out, took)
    print(f"  cancelled in {took:.1f} s")


def _lock(path):
    """Open a file so nothing else can read it (like a program holding it open)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateFileW(path, 0x80000000, 0, None, 3, 0, None)   # GENERIC_READ, share none, OPEN_EXISTING
    assert h not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    return lambda: k32.CloseHandle(h)


def test_locked_file_is_retried(root):
    """A file that was locked during one scan used to be cached as unreadable for good."""
    d = os.path.join(root, "locked")
    os.makedirs(d)
    make_photo(7).save(os.path.join(d, "a.jpg"))
    make_photo(7).resize((200, 150)).save(os.path.join(d, "a small.jpg"))
    cache = os.path.join(root, "locked.sqlite3")
    unlock = _lock(os.path.join(d, "a.jpg"))
    try:
        res = engine.scan([d], level=NORMAL, cache_path=cache)
    finally:
        unlock()
    assert [r.name for r in res.unreadable] == ["a.jpg"] and not res.groups, res
    print("  while locked:", res.unreadable[0].error)
    res = engine.scan([d], level=NORMAL, cache_path=cache)
    assert not res.unreadable and len(res.groups) == 1, (res.unreadable, res.groups)
    assert transient(PermissionError(13, "x")) and transient(OSError(22, "x"))
    assert not transient(OSError("image file is truncated")) and not transient(ValueError("x"))


def test_damaged_cache(root):
    """A damaged cache (file or single rows) must never stop a scan."""
    from tests import make_videos as mv
    d = os.path.join(root, "cache")
    os.makedirs(d)
    make_photo(4).save(os.path.join(d, "a.jpg"))
    shutil.copy(os.path.join(d, "a.jpg"), os.path.join(d, "b.jpg"))
    mv.write(os.path.join(d, "v.mp4"), mv.black(1.0))
    cache = os.path.join(root, "cache.sqlite3")

    open(cache, "wb").write(b"this is not a database" * 100)
    res = engine.scan([d], level=NORMAL, cache_path=cache)
    assert len(res.groups) == 1
    db = sqlite3.connect(cache)
    assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 3   # rebuilt and in use
    assert db.execute("SELECT prop_ph FROM vmeta").fetchone()[0] is not None
    db.execute("UPDATE vmeta SET prop_ph = x'010203'")                    # wrong length
    db.execute("UPDATE files SET phash = 'not hex' WHERE path LIKE '%a.jpg'")
    db.commit()
    db.close()
    res = engine.scan([d], level=NORMAL, cache_path=cache)
    assert len(res.groups) == 1 and not res.unreadable


def test_image_size_limit(root):
    """A tiny file claiming to be a 40,000 x 40,000 image is refused, not decoded."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    p = os.path.join(root, "bomb.png")
    open(p, "wb").write(b"\x89PNG\r\n\x1a\n"
                        + chunk(b"IHDR", struct.pack(">IIBBBBB", 40000, 40000, 8, 2, 0, 0, 0))
                        + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b""))
    res = analyze((p, True, False, False))
    assert "DecompressionBomb" in res.get("error", ""), res
    assert "retry" not in res   # a property of the file: no point trying again next scan


def test_recycle_check():
    from dupefinder.recycle import not_recyclable
    gb = 1 << 30

    def info(root):
        return {"C:\\": (True, 50 * gb), "E:\\": (False, None), "\\\\NAS\\PHOTOS\\": (False, None)}[root]

    sizes = {r"C:\p\small.mp4": gb, r"c:\p\huge.mp4": 60 * gb, r"E:\DCIM\a.jpg": 5,
             r"\\nas\photos\b.jpg": 5}
    lost = not_recyclable(list(sizes), sizes, info)
    assert lost == [r"c:\p\huge.mp4", r"E:\DCIM\a.jpg", r"\\nas\photos\b.jpg"], lost
    if sys.platform == "win32":   # the real lookup runs and calls this drive recyclable or not
        here = os.path.abspath(__file__)
        assert not_recyclable([here], {here: 1}) in ([], [here])


# ---- GUI

def test_gui(root):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from dupefinder import gui
    from dupefinder.engine import DuplicateGroup, ImageRecord, ScanResult

    app = QApplication.instance() or QApplication(sys.argv)
    asked, warned = [], []
    QMessageBox.exec = lambda self: (asked.append(self.text()), QMessageBox.StandardButton.No)[1]
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[2]))
    w = gui.MainWindow()
    try:
        w.settings.clear()
        w.folder_list.clear()

        # dropping a web link must not add a folder (it used to add ".", the app's own folder)
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/cat.jpg")])
        w.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        assert w._folders() == [], w._folders()

        def vid(name, w_, h_, dur):
            return ImageRecord(rf"C:\x\{name}", 1000, 1, w_, h_, media="video", duration=dur)

        def img(name):
            return ImageRecord(rf"C:\x\{name}", 500, 1, 100, 100)

        clip, full, small = vid("clip 4k.mp4", 3840, 2160, 6), vid("full.mp4", 1280, 720, 60), \
            vid("full small.mp4", 640, 360, 60.4)
        a, b, v = img("a.jpg"), img("b.jpg"), vid("holiday.mp4", 640, 360, 30)
        res = ScanResult([
            DuplicateGroup([clip, full, small], ["Reference (best quality)", "Contains clip", "Same"], [100] * 3),
            DuplicateGroup([a, b], ["Reference (best quality)", "Exact copy"], [100, 100]),
            DuplicateGroup([v, a, b], ["Video", "Photo matches frame at 0:04", "Photo matches frame at 0:09"],
                           [100, 95, 94], kind="photo_in_video"),
        ], 2, total_videos=4)
        w._populate(res)
        g_vid, g_img, g_pv = list(w._groups())

        # auto-mark: the sharp clip is the "best" file, but the full video must survive
        w.keep_combo.setCurrentText("Highest resolution")
        w.auto_mark()
        marked = sorted(c.text(0) for c in w._marked_items())
        assert marked == ["b.jpg", "full small.mp4"], marked
        for rule in gui.KEEP_RULES:   # no rule may mark both full-length copies
            w.keep_combo.setCurrentText(rule)
            w.auto_mark()
            m = {c.text(0) for c in w._marked_items()}
            assert not {"full.mp4", "full small.mp4"} <= m, (rule, m)

        # the same photo ticked in two groups: counted and deleted once, and the
        # "every copy" warning sees that a.jpg is going too
        w.clear_marks()
        for g, name in ((g_img, "b.jpg"), (g_pv, "a.jpg")):
            next(c for c in w._children(g) if c.text(0) == name).setCheckState(0, Qt.CheckState.Checked)
        assert "2 marked" in w.summary_lbl.text(), w.summary_lbl.text()
        assert w._confirm_marked("Send to the Recycle Bin") is None      # (patched: answers No)
        assert "2 file(s)" in asked[-1] and "EVERY copy" in asked[-1], asked[-1]
        g_pv.child(1).setCheckState(0, Qt.CheckState.Checked)            # a.jpg, ticked twice...
        g_img.child(0).setCheckState(0, Qt.CheckState.Checked)
        w._confirm_marked("Send to the Recycle Bin")
        assert "2 file(s)" in asked[-1], asked[-1]                       # ...is still one file

        # Recycle Bin warning when Windows would delete permanently
        gui.not_recyclable = lambda paths, sizes: [paths[0]]
        w._confirm_marked("Send to the Recycle Bin", recycle=True)
        assert "PERMANENTLY" in asked[-1] and "1 of these files" in asked[-1], asked[-1]
        w._confirm_marked("Move to X:")
        assert "PERMANENTLY" not in asked[-1]

        # a "photos from a video" group goes away with its video (two photos remain in it)
        w._after_removal([v.path], [], "moved")
        kinds = [g.data(0, gui.KIND_ROLE) for g in w._groups()]
        assert kinds == ["duplicate", "duplicate"], kinds

        # export to a place that can't be written: a message, not a silent failure
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a_, **k: (os.path.join(root, "no such folder", "out.csv"), ""))
        w.export_csv()
        assert warned and "Could not write" in warned[-1], warned

        # moving a file into the folder it is already in is refused, not renamed
        d = os.path.join(root, "move")
        os.makedirs(d)
        p = os.path.join(d, "keep.jpg")
        make_photo(2).save(p)
        ok, errors = [], []
        gui.FileOpThread("move", [p], {}, d)._move_one(p, ok, errors)
        assert not ok and "already in that folder" in errors[0] and os.listdir(d) == ["keep.jpg"]
        w.close()
    finally:
        gui.QSettings("ImageDupeFinder", "ImageDupeFinder2").clear()   # don't leave test settings


def main():
    root = tempfile.mkdtemp(prefix="dupefix_")
    try:
        for test in (test_empty_video_cache, test_partial_vs_full_hash, test_symlinks_skipped,
                     test_worker_crash, test_cancel_is_prompt, test_locked_file_is_retried,
                     test_damaged_cache, test_image_size_limit):
            print(test.__name__)
            test(root)
        print("test_recycle_check")
        test_recycle_check()
        print("test_gui")
        test_gui(root)
        print("FIX TESTS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
