"""Offscreen GUI smoke test. Run:  .venv\\Scripts\\python.exe -m tests.test_gui [screenshot.png]"""

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from dupefinder import gui  # noqa: E402
from tests.test_engine import build_fixture  # noqa: E402


def wait_for(cond, timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: cond() and loop.quit())
    t.start(50)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()
    t.stop()
    assert cond(), "timed out"


def main():
    shot = sys.argv[1] if len(sys.argv) > 1 else None
    root = tempfile.mkdtemp(prefix="dupegui_")
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        build_fixture(root)
        dest = os.path.join(root, "moved")
        os.makedirs(dest)
        w = gui.MainWindow()
        w.settings.clear()
        w.folder_list.clear()
        w.level_combo.setCurrentText("Normal (resized / re-saved)")
        w.media_combo.setCurrentText("Images and videos")
        w._add_folder_path(os.path.join(root, "A"))
        w.resize(1400, 820)
        w.show()
        w.start_scan()
        wait_for(lambda: w.scan_thread is not None and w.scan_thread.isFinished() and w.scan_btn.isEnabled())
        groups = list(w._groups())
        print("status:", w.status_lbl.text())
        assert len(groups) == 2, len(groups)

        w.auto_mark()
        marked = w._marked_items()
        print("marked:", [c.text(0) for c in marked])
        assert len(marked) == 5  # 4 extra beach + 1 extra portrait
        w.tree.setCurrentItem(groups[0].child(1))
        wait_for(lambda: len(w.thumbs) >= groups[0].childCount(), 20)
        app.processEvents()
        if shot:
            w.grab().save(shot)
            print("screenshot:", shot)

        # Move marked files (dialogs patched to auto-answer)
        QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: dest)
        QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
        w.move_marked()
        wait_for(lambda: w._file_op is None, 30)  # runs in the background with a progress dialog
        moved = sorted(os.listdir(dest))
        print("moved:", moved)
        assert len(moved) == 5
        assert w.tree.topLevelItemCount() == 1  # only the "Unreadable files" node remains
        w.close()
        print("GUI TEST PASSED")
    finally:
        gui.QSettings("ImageDupeFinder", "ImageDupeFinder2").clear()  # don't leave test settings
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
