"""Recycle Bin progress test. The real Recycle Bin is NOT touched: send2trash is
replaced with a slow fake that deletes nothing.

Run:  .venv\\Scripts\\python.exe -m tests.test_trash_progress
"""

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QPushButton  # noqa: E402

from dupefinder import gui  # noqa: E402
from tests.test_engine import make_photo  # noqa: E402
from tests.test_gui import wait_for  # noqa: E402

N = 80


def scan_window(root):
    w = gui.MainWindow()
    w.settings.clear()
    w.folder_list.clear()
    w._add_folder_path(root)
    w.level_combo.setCurrentText("Exact copies only")
    w.show()
    w.start_scan()
    wait_for(lambda: w.scan_thread.isFinished() and w.scan_btn.isEnabled())
    w.auto_mark()
    return w


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="dupetrash_")
    try:
        src = os.path.join(root, "photo.jpg")
        make_photo(7).save(src)
        for i in range(N):
            shutil.copy(src, os.path.join(root, f"copy_{i:03}.jpg"))

        QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes  # confirm dialog
        warnings = []
        QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a[2]))

        trashed, calls = [], []
        bad = os.path.join(root, "copy_013.jpg")

        def fake_send2trash(paths):
            batch = paths if isinstance(paths, list) else [paths]
            calls.append(len(batch))
            time.sleep(0.02 * len(batch))
            if bad in batch:
                raise OSError("Access is denied")
            trashed.extend(batch)

        gui.send2trash = fake_send2trash

        # --- full run: progress, batching, per-file error
        w = scan_window(root)
        marked = len(w._marked_items())
        assert marked == N, marked
        seen = []
        orig_set_value = QProgressDialog.setValue
        QProgressDialog.setValue = lambda self, v: (seen.append(v), orig_set_value(self, v))[1]
        w.trash_marked()
        wait_for(lambda: w._file_op is None, 60)
        QProgressDialog.setValue = orig_set_value
        print(f"progress updates: {len(seen)} -> {seen[:6]}...{seen[-3:]}")
        print(f"send2trash calls: {len(calls)} (batch sizes {calls[:8]}...)")
        print("status:", w.status_lbl.text())
        assert seen == sorted(seen) and seen[-1] == N, seen
        assert len(seen) >= 5, "progress bar should update several times"
        assert len(calls) < N, "files should be sent in batches"
        assert len(trashed) == N - 1 and bad not in trashed
        assert warnings and "copy_013.jpg" in warnings[0] and "Access is denied" in warnings[0]
        # the failed file stays in the results; the trashed ones are gone
        left = [c.data(0, gui.PATH_ROLE) for g in w._groups() for c in w._children(g)]
        assert bad in left and len(left) == 2, left
        w.close()

        # --- Stop button: stops after the current batch, reports partial result
        trashed.clear()
        calls.clear()
        warnings.clear()
        w = scan_window(root)
        w.trash_marked()
        wait_for(lambda: len(trashed) > 0, 10)
        dlg = w.findChild(QProgressDialog)
        stop = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Stop")
        stop.click()
        print("dialog after Stop:", dlg.labelText().replace("\n", " "), "| visible:", dlg.isVisible())
        wait_for(lambda: w._file_op is None, 30)
        print("after Stop:", w.status_lbl.text())
        assert 0 < len(trashed) < N - 1, len(trashed)
        assert "stopped early" in w.status_lbl.text()
        left = [c.data(0, gui.PATH_ROLE) for g in w._groups() for c in w._children(g)]
        assert len(left) == N + 1 - len(trashed), "untouched files must stay in the list"
        w.close()

        # --- Esc key also stops
        trashed.clear()
        w = scan_window(root)
        w.trash_marked()
        wait_for(lambda: len(trashed) > 0, 10)
        w.findChild(QProgressDialog).reject()  # what Esc does
        wait_for(lambda: w._file_op is None, 30)
        print("after Esc:", w.status_lbl.text())
        assert "stopped early" in w.status_lbl.text() and len(trashed) < N - 1
        w.close()
        print("TRASH PROGRESS TEST PASSED")
    finally:
        gui.QSettings("ImageDupeFinder", "ImageDupeFinder2").clear()  # don't leave test settings
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
