"""Qt (PySide6) desktop interface for Image Duplicate Finder."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import (QObject, QRunnable, QSettings, QSize, Qt, QThread, QThreadPool,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListView, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QProgressDialog,
                               QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)
from send2trash import send2trash

from . import __version__
from .engine import (DEFAULT_LEVEL, MEDIA_TYPES, SIMILARITY_LEVELS, ImageRecord, ScanCancelled,
                     ScanResult, scan)
from .ai import ai_available
from .eta import EtaEstimator, fmt_eta
from .hashing import HEIF_SUPPORTED, _flatten_alpha, open_oriented
from .video import fmt_duration, is_video, thumbnail_strip

COL_FILE, COL_SIZE, COL_DIM, COL_DUR, COL_MATCH, COL_FOLDER = range(6)
KIND_ROLE = Qt.ItemDataRole.UserRole + 1

THUMB = 220
PATH_ROLE = Qt.ItemDataRole.UserRole
MARK_BG = QColor(255, 120, 120, 70)

KEEP_RULES = {
    "Highest resolution": lambda r: (-r.pixels, -r.bitrate, -round(r.duration), -r.size,
                                     r.mtime_ns, len(r.path)),
    "Largest file size": lambda r: (-r.size, -r.pixels, r.mtime_ns, len(r.path)),
    "Longest duration (videos)": lambda r: (-round(r.duration), -r.pixels, -r.bitrate, -r.size,
                                            len(r.path)),
    "Oldest file": lambda r: (r.mtime_ns, -r.pixels, len(r.path)),
    "Newest file": lambda r: (-r.mtime_ns, -r.pixels, len(r.path)),
    "Shortest path": lambda r: (len(r.path), r.path),
}


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- background work


class ScanThread(QThread):
    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, folders: list[str], recursive: bool, level: str, rotations: bool = True,
                 media: str = "both", photo_video: bool = False):
        super().__init__()
        self.folders, self.recursive, self.level = folders, recursive, level
        self.rotations, self.media, self.photo_video = rotations, media, photo_video
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            res = scan(self.folders, self.recursive, self.level,
                       progress=self.progress.emit, cancelled=lambda: self._cancel,
                       ai_rotations=self.rotations, media=self.media,
                       photo_video=self.photo_video)
        except ScanCancelled:
            self.cancelled.emit()
            return
        except Exception as e:  # surface anything unexpected to the user
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.done.emit(res)


class FileOpThread(QThread):
    """Sends files to the Recycle Bin, or moves them to a folder, off the UI thread.

    Recycle Bin: files go in small batches (one Windows shell call per batch is
    much faster than one per file); the batch size adapts so the progress bar
    updates a few times per second. A failed batch is retried file-by-file so
    each error is reported individually."""

    progress = Signal(int, int, object, str)   # files done, total, bytes done, current name
    finished_op = Signal(list, list, bool)      # succeeded paths, error messages, stopped early

    def __init__(self, op: str, paths: list[str], sizes: dict[str, int], dest: str | None = None):
        super().__init__()
        self.op, self.paths, self.sizes, self.dest = op, paths, sizes, dest
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _trash_batch(self, batch, ok, errors):
        try:
            send2trash(batch)
            ok.extend(batch)
        except Exception:
            for p in batch:
                try:
                    send2trash(p)
                    ok.append(p)
                except Exception as e:
                    errors.append(f"{p}: {e}")

    def _move_one(self, p, ok, errors):
        try:
            base, ext = os.path.splitext(os.path.basename(p))
            target, n = os.path.join(self.dest, base + ext), 1
            while os.path.exists(target):
                target = os.path.join(self.dest, f"{base}_{n}{ext}")
                n += 1
            shutil.move(p, target)
            ok.append(p)
        except Exception as e:
            errors.append(f"{p}: {e}")

    def run(self):
        import time

        ok: list[str] = []
        errors: list[str] = []
        total, done, done_bytes = len(self.paths), 0, 0
        chunk = 4
        i = 0
        while i < total and not self._cancel:
            batch = self.paths[i:i + (chunk if self.op == "trash" else 1)]
            self.progress.emit(done, total, done_bytes, os.path.basename(batch[0]))
            t = time.perf_counter()
            if self.op == "trash":
                self._trash_batch(batch, ok, errors)
                dt = time.perf_counter() - t
                if dt < 0.15:
                    chunk = min(chunk * 2, 64)
                elif dt > 0.6:
                    chunk = max(chunk // 2, 1)
            else:
                self._move_one(batch[0], ok, errors)
            i += len(batch)
            done += len(batch)
            done_bytes += sum(self.sizes.get(p, 0) for p in batch)
        self.progress.emit(done, total, done_bytes, "")
        self.finished_op.emit(ok, errors, self._cancel and done < total)


class _ThumbSignals(QObject):
    loaded = Signal(str, QImage)


class ThumbTask(QRunnable):
    def __init__(self, path: str, signals: _ThumbSignals):
        super().__init__()
        self.path, self.signals = path, signals

    def run(self):
        try:
            if is_video(self.path):
                im = thumbnail_strip(self.path, THUMB).convert("RGB")
            else:
                src, _, _ = open_oriented(self.path, THUMB)
                try:
                    im = _flatten_alpha(src).convert("RGB")
                finally:
                    src.close()
            im.thumbnail((THUMB, THUMB))
            w, h = im.size
            qimg = QImage(im.tobytes("raw", "RGB"), w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        except Exception:
            qimg = QImage()
        self.signals.loaded.emit(self.path, qimg)


# --------------------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Image & Video Duplicate Finder {__version__}")
        self.resize(1300, 820)
        self.setAcceptDrops(True)
        self.settings = QSettings("ImageDupeFinder", "ImageDupeFinder2")  # separate from v1
        self.records: dict[str, ImageRecord] = {}
        self.scan_thread: ScanThread | None = None
        self._file_op: FileOpThread | None = None
        self.eta = EtaEstimator()
        self._last_progress: tuple[str, int, int] | None = None
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)   # count the estimate down between progress updates
        self._eta_timer.timeout.connect(self._show_progress_text)
        self.thumbs: dict[str, QPixmap] = {}
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max(2, min(6, (os.cpu_count() or 2))))
        self.thumb_signals = _ThumbSignals()
        self.thumb_signals.loaded.connect(self._thumb_loaded)
        self._shown_group: QTreeWidgetItem | None = None
        self._syncing = False

        self._build_ui()
        self._load_settings()
        self._update_summary()

    # ----- UI construction
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # Folders
        fbox = QGroupBox("Folders to scan  (drag && drop folders here)")
        fl = QHBoxLayout(fbox)
        self.folder_list = QListWidget()
        self.folder_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.folder_list.setMaximumHeight(100)
        fl.addWidget(self.folder_list, 1)
        fb = QVBoxLayout()
        for text, slot in (("Add folder…", self.add_folder), ("Remove", self.remove_folders),
                           ("Clear", self.folder_list.clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            fb.addWidget(b)
        fb.addStretch()
        fl.addLayout(fb)
        root.addWidget(fbox)

        # Options + scan
        opt = QHBoxLayout()
        self.recursive_cb = QCheckBox("Include subfolders")
        self.recursive_cb.setChecked(True)
        opt.addWidget(self.recursive_cb)
        opt.addSpacing(12)
        opt.addWidget(QLabel("Scan:"))
        self.media_combo = QComboBox()
        self.media_combo.addItems(list(MEDIA_TYPES))
        opt.addWidget(self.media_combo)
        opt.addSpacing(12)
        opt.addWidget(QLabel("Match:"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(list(SIMILARITY_LEVELS))
        self.ai_ok, ai_why = ai_available()
        model = self.level_combo.model()
        for i, (name, lvl) in enumerate(SIMILARITY_LEVELS.items()):
            if lvl.ai is not None and not self.ai_ok:
                model.item(i).setEnabled(False)
                model.item(i).setToolTip(ai_why)
        self.level_combo.setToolTip(
            "Exact copies: same file size and identical bytes.\n"
            "Strict / Normal / Loose: perceptual hashing finds the same picture even if\n"
            "resized, re-compressed, converted or lightly edited.\n"
            "AI: a local vision model (DINOv2, runs on this PC) also finds crops,\n"
            "rotations, filters, watermarks and heavier edits.\n\n"
            "Videos: Normal levels find re-encoded / resized / converted copies;\n"
            "AI levels also find trimmed clips and edited versions.")
        self.level_combo.currentTextChanged.connect(self._level_changed)
        opt.addWidget(self.level_combo)
        self.rot_cb = QCheckBox("Detect rotated copies")
        self.rot_cb.setChecked(True)
        self.rot_cb.setToolTip("AI modes: also match images rotated 90°/180°/270° (about 4× AI work).")
        opt.addWidget(self.rot_cb)
        self.pv_cb = QCheckBox("Match photos to video frames")
        self.pv_cb.setToolTip("AI modes: list photos that show a frame from a video (e.g. stills or\n"
                              "Live Photos). Shown as separate groups that are never auto-marked.")
        opt.addWidget(self.pv_cb)
        opt.addStretch()
        self.scan_btn = QPushButton("  Scan for duplicates  ")
        self.scan_btn.setDefault(True)
        f = self.scan_btn.font()
        f.setBold(True)
        self.scan_btn.setFont(f)
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_scan)
        opt.addWidget(self.scan_btn)
        opt.addWidget(self.cancel_btn)
        root.addLayout(opt)

        prog = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        self.status_lbl = QLabel("Add one or more folders, then click Scan.")
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.status_lbl, 2)
        root.addLayout(prog)

        # Results: tree + preview
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Size", "Dimensions", "Duration", "Match", "Folder"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemChanged.connect(self._tree_item_changed)
        self.tree.itemDoubleClicked.connect(self._open_item)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, w in enumerate((250, 75, 90, 65, 230, 280)):
            self.tree.setColumnWidth(col, w)
        split.addWidget(self.tree)

        self.preview = QListWidget()
        self.preview.setViewMode(QListView.ViewMode.IconMode)
        self.preview.setIconSize(QSize(THUMB, THUMB))
        self.preview.setGridSize(QSize(THUMB + 30, THUMB + 80))
        self.preview.setResizeMode(QListView.ResizeMode.Adjust)
        self.preview.setMovement(QListView.Movement.Static)
        self.preview.setWordWrap(True)
        self.preview.setSpacing(6)
        self.preview.itemChanged.connect(self._preview_item_changed)
        self.preview.itemDoubleClicked.connect(self._open_item)
        self.preview.itemClicked.connect(self._preview_clicked)
        split.addWidget(self.preview)
        split.setSizes([700, 600])
        root.addWidget(split, 1)

        # Actions
        act = QHBoxLayout()
        act.addWidget(QLabel("Keep in each group:"))
        self.keep_combo = QComboBox()
        self.keep_combo.addItems(list(KEEP_RULES))
        act.addWidget(self.keep_combo)
        b = QPushButton("Auto-mark duplicates")
        b.setToolTip("In every group, keep one file (by the rule on the left) and mark the rest.")
        b.clicked.connect(self.auto_mark)
        act.addWidget(b)
        b = QPushButton("Clear marks")
        b.clicked.connect(self.clear_marks)
        act.addWidget(b)
        act.addStretch()
        b = QPushButton("Export CSV…")
        b.clicked.connect(self.export_csv)
        act.addWidget(b)
        b = QPushButton("Move marked to folder…")
        b.clicked.connect(self.move_marked)
        act.addWidget(b)
        self.trash_btn = QPushButton("Send marked to Recycle Bin")
        self.trash_btn.clicked.connect(self.trash_marked)
        act.addWidget(self.trash_btn)
        root.addLayout(act)

        self.setCentralWidget(central)
        self.summary_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.summary_lbl)
        heif = "HEIC supported" if HEIF_SUPPORTED else "HEIC not available"
        ai = "AI model ready" if self.ai_ok else "AI model unavailable"
        self.statusBar().showMessage(f"v{__version__} · all processing is local · {heif} · {ai}")

    def _level_changed(self, name: str):
        lvl = SIMILARITY_LEVELS.get(name)
        ai = bool(lvl and lvl.ai is not None)
        self.rot_cb.setVisible(ai)
        if hasattr(self, "pv_cb"):
            self.pv_cb.setVisible(ai)

    # ----- settings
    def _load_settings(self):
        for f in self.settings.value("folders", [], list) or []:
            if os.path.isdir(f):
                self.folder_list.addItem(f)
        self.recursive_cb.setChecked(self.settings.value("recursive", True, bool))
        level = self.settings.value("level", DEFAULT_LEVEL, str)
        if level not in SIMILARITY_LEVELS or (SIMILARITY_LEVELS[level].ai and not self.ai_ok):
            level = DEFAULT_LEVEL
        self.level_combo.setCurrentText(level)
        self.rot_cb.setChecked(self.settings.value("rotations", True, bool))
        self.pv_cb.setChecked(self.settings.value("photo_video", False, bool))
        self.media_combo.setCurrentText(self.settings.value("media", "Images and videos", str))
        self._level_changed(level)
        self.keep_combo.setCurrentText(self.settings.value("keep", "Highest resolution", str))
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, e):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self.scan_thread.wait(5000)
        op = getattr(self, "_file_op", None)
        if op is not None and op.isRunning():  # finish the current batch, then stop
            op.cancel()
            op.wait()
        self.settings.setValue("folders", self._folders())
        self.settings.setValue("recursive", self.recursive_cb.isChecked())
        self.settings.setValue("level", self.level_combo.currentText())
        self.settings.setValue("rotations", self.rot_cb.isChecked())
        self.settings.setValue("photo_video", self.pv_cb.isChecked())
        self.settings.setValue("media", self.media_combo.currentText())
        self.settings.setValue("keep", self.keep_combo.currentText())
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(e)

    # ----- folders
    def _folders(self) -> list[str]:
        return [self.folder_list.item(i).text() for i in range(self.folder_list.count())]

    def _add_folder_path(self, path: str):
        path = os.path.normpath(path)
        if os.path.isdir(path) and path not in self._folders():
            self.folder_list.addItem(path)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder to scan")
        if d:
            self._add_folder_path(d)

    def remove_folders(self):
        for it in self.folder_list.selectedItems():
            self.folder_list.takeItem(self.folder_list.row(it))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            self._add_folder_path(p if os.path.isdir(p) else os.path.dirname(p))

    # ----- scanning
    def start_scan(self):
        folders = self._folders()
        if not folders:
            QMessageBox.information(self, "No folders", "Add at least one folder to scan.")
            return
        self.tree.clear()
        self.preview.clear()
        self._shown_group = None
        self.records.clear()
        self._update_summary()
        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setRange(0, 0)
        self.scan_thread = ScanThread(folders, self.recursive_cb.isChecked(),
                                      self.level_combo.currentText(), self.rot_cb.isChecked(),
                                      MEDIA_TYPES[self.media_combo.currentText()],
                                      self.pv_cb.isChecked())
        self.scan_thread.progress.connect(self._on_progress)
        self.scan_thread.done.connect(self._on_done)
        self.scan_thread.failed.connect(self._on_failed)
        self.scan_thread.cancelled.connect(self._on_cancelled)
        self.scan_thread.finished.connect(self._scan_finished)
        self.eta.reset()
        self._last_progress = None
        self._eta_timer.start()
        self.scan_thread.start()

    def cancel_scan(self):
        if self.scan_thread:
            self.scan_thread.cancel()
            self._eta_timer.stop()
            self._last_progress = None
            self.status_lbl.setText("Cancelling…")
            self.cancel_btn.setEnabled(False)

    def _on_progress(self, stage: str, done: int, total: int):
        if self.scan_thread and self.scan_thread._cancel:
            return  # keep showing "Cancelling…"
        self.eta.update(stage, done, total)
        self._last_progress = (stage, done, total)
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)
        self._show_progress_text()

    def _show_progress_text(self):
        if not self._last_progress:
            return
        stage, done, total = self._last_progress
        if total:
            self.status_lbl.setText(f"{stage}: {done:,} / {total:,} · {fmt_eta(self.eta.remaining())}")
        else:
            self.status_lbl.setText(f"{stage}… {done:,}" if done else f"{stage}…")

    def _scan_finished(self):
        self._eta_timer.stop()
        self._last_progress = None
        self.scan_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 1)

    def _on_cancelled(self):
        self._last_progress = None
        self.progress.setValue(0)
        self.status_lbl.setText("Scan cancelled.")

    def _on_failed(self, msg: str):
        self._last_progress = None
        self.progress.setValue(0)
        self.status_lbl.setText("Scan failed.")
        QMessageBox.critical(self, "Scan failed", msg)

    def _on_done(self, res: ScanResult):
        self._last_progress = None
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        dupes = sum(len(g.files) - 1 for g in res.groups if g.kind == "duplicate")
        ndup = sum(g.kind == "duplicate" for g in res.groups)
        npv = len(res.groups) - ndup
        msg = (f"Scanned {res.total_images:,} images and {res.total_videos:,} videos in "
               f"{res.elapsed:.1f}s — {ndup:,} groups, {dupes:,} duplicates, "
               f"{human_size(sum(g.reclaimable for g in res.groups))} reclaimable.")
        if npv:
            msg += f"  {npv} video(s) with matching photos."
        if res.ai_device in ("GPU", "CPU"):
            msg += f"  AI ran on {res.ai_device}."
        if res.unreadable:
            msg += f"  {len(res.unreadable)} file(s) could not be read."
        self.status_lbl.setText(msg)
        self._populate(res)

    def _populate(self, res: ScanResult):
        self.tree.blockSignals(True)
        self.tree.setUpdatesEnabled(False)
        bold = QFont(self.tree.font())
        bold.setBold(True)
        gnum = 0
        for g in res.groups:
            gitem = QTreeWidgetItem(self.tree)
            gitem.setFont(0, bold)
            gitem.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            gitem.setData(0, PATH_ROLE, None)
            gitem.setData(0, KIND_ROLE, g.kind)
            for f, m in zip(g.files, g.match):
                self.records[f.path] = f
                c = QTreeWidgetItem(gitem, [
                    f.name, human_size(f.size),
                    f"{f.width}×{f.height}" if f.width else "?",
                    fmt_duration(f.duration) if f.is_video else "",
                    m, f.folder])
                c.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                           | Qt.ItemFlag.ItemIsUserCheckable)
                c.setCheckState(0, Qt.CheckState.Unchecked)
                c.setData(0, PATH_ROLE, f.path)
                for col in (COL_SIZE, COL_DUR):
                    c.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                tip = f.path
                if f.is_video:
                    tip += (f"\n{f.codec} · {f.fps:.2f} fps · {f.bitrate // 1000:,} kb/s · "
                            f"{fmt_duration(f.duration)}")
                c.setToolTip(COL_FILE, tip)
                c.setToolTip(COL_MATCH, m)
            if g.kind == "duplicate":
                gnum += 1
                gitem.setText(0, f"Group {gnum}")
            else:
                gitem.setText(0, "Photos from a video")
            self._refresh_group_header(gitem)
            gitem.setExpanded(True)
        if res.unreadable:
            uitem = QTreeWidgetItem(self.tree, [f"Unreadable files ({len(res.unreadable)})"])
            uitem.setFirstColumnSpanned(True)
            uitem.setData(0, PATH_ROLE, "__unreadable__")
            for r in res.unreadable:
                c = QTreeWidgetItem(uitem, [r.name, human_size(r.size), "", "", r.error or "",
                                            r.folder])
                c.setData(0, PATH_ROLE, r.path)
                c.setToolTip(COL_MATCH, r.error or "")
        self.tree.setUpdatesEnabled(True)
        self.tree.blockSignals(False)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._update_summary()

    # ----- tree helpers
    def _groups(self):
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            if g.data(0, PATH_ROLE) != "__unreadable__":
                yield g

    @staticmethod
    def _children(g: QTreeWidgetItem):
        return [g.child(i) for i in range(g.childCount())]

    def _refresh_group_header(self, g: QTreeWidgetItem):
        recs = [self.records[c.data(0, PATH_ROLE)] for c in self._children(g)]
        name = g.text(0).split("  ")[0]
        if g.data(0, KIND_ROLE) == "photo_in_video":
            g.setText(0, f"{name}  ·  {len(recs) - 1} photo(s) match frames of {recs[0].name}"
                         f"  ·  not auto-marked")
        else:
            total = sum(r.size for r in recs)
            best = max((r.size for r in recs), default=0)
            nv = sum(r.is_video for r in recs)
            what = (f"{nv} videos" if nv == len(recs) else
                    f"{len(recs)} images" if nv == 0 else f"{len(recs)} files")
            g.setText(0, f"{name}  ·  {what}  ·  {human_size(total - best)} reclaimable")
        g.setFirstColumnSpanned(True)

    def _marked_items(self) -> list[QTreeWidgetItem]:
        return [c for g in self._groups() for c in self._children(g)
                if c.checkState(0) == Qt.CheckState.Checked]

    def _update_summary(self):
        marked = self._marked_items()
        size = sum(self.records[c.data(0, PATH_ROLE)].size for c in marked)
        ngroups = sum(1 for _ in self._groups())
        self.summary_lbl.setText(f"{ngroups:,} groups  |  {len(marked):,} marked "
                                 f"({human_size(size)})")

    # ----- marking
    def auto_mark(self):
        rule = KEEP_RULES[self.keep_combo.currentText()]
        self.tree.blockSignals(True)
        for g in self._groups():
            if g.data(0, KIND_ROLE) != "duplicate":
                continue  # photo <-> video groups are informational only
            kids = self._children(g)
            keep = min(kids, key=lambda c: rule(self.records[c.data(0, PATH_ROLE)]))
            for c in kids:
                self._set_marked(c, c is not keep)
        self.tree.blockSignals(False)
        self._sync_preview_checks()
        self._update_summary()

    def clear_marks(self):
        self.tree.blockSignals(True)
        for g in self._groups():
            for c in self._children(g):
                self._set_marked(c, False)
        self.tree.blockSignals(False)
        self._sync_preview_checks()
        self._update_summary()

    def _set_marked(self, c: QTreeWidgetItem, marked: bool):
        c.setCheckState(0, Qt.CheckState.Checked if marked else Qt.CheckState.Unchecked)
        brush = QBrush(MARK_BG) if marked else QBrush()
        for col in range(self.tree.columnCount()):
            c.setBackground(col, brush)

    def _tree_item_changed(self, item: QTreeWidgetItem, col: int):
        if col != 0 or self._syncing or item.parent() is None:
            return
        self.tree.blockSignals(True)
        self._set_marked(item, item.checkState(0) == Qt.CheckState.Checked)
        self.tree.blockSignals(False)
        self._sync_preview_checks()
        self._update_summary()

    # ----- preview pane
    def _current_changed(self, cur: QTreeWidgetItem | None, _prev):
        if cur is None:
            return
        g = cur if cur.parent() is None else cur.parent()
        if g is not self._shown_group:
            self._show_group(g)
        path = cur.data(0, PATH_ROLE)
        for i in range(self.preview.count()):
            it = self.preview.item(i)
            it.setSelected(it.data(PATH_ROLE) == path)

    def _show_group(self, g: QTreeWidgetItem):
        self._shown_group = g
        self._syncing = True
        self.preview.clear()
        unreadable = g.data(0, PATH_ROLE) == "__unreadable__"
        for c in self._children(g):
            path = c.data(0, PATH_ROLE)
            rec = self.records.get(path)
            if rec is None:
                continue
            dims = f"{rec.width}×{rec.height}"
            if rec.is_video:
                dims += f" · {fmt_duration(rec.duration)}"
            text = f"{rec.name}\n{human_size(rec.size)} · {dims}\n{c.text(COL_MATCH)}"
            it = QListWidgetItem(text)
            it.setData(PATH_ROLE, path)
            it.setToolTip(path)
            if not unreadable:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(c.checkState(0))
            if path in self.thumbs:
                it.setIcon(self.thumbs[path])
            else:
                self.pool.start(ThumbTask(path, self.thumb_signals))
            self.preview.addItem(it)
        self._syncing = False
        self._sync_preview_checks()

    def _sync_preview_checks(self):
        if self._shown_group is None:
            return
        try:
            kids = {c.data(0, PATH_ROLE): c for c in self._children(self._shown_group)}
        except RuntimeError:  # group item was deleted
            self._shown_group = None
            return
        self._syncing = True
        for i in range(self.preview.count()):
            it = self.preview.item(i)
            c = kids.get(it.data(PATH_ROLE))
            if c is None or not (it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            it.setCheckState(c.checkState(0))
            it.setBackground(QBrush(MARK_BG) if c.checkState(0) == Qt.CheckState.Checked else QBrush())
        self._syncing = False

    def _preview_item_changed(self, it: QListWidgetItem):
        if self._syncing or self._shown_group is None:
            return
        path = it.data(PATH_ROLE)
        for c in self._children(self._shown_group):
            if c.data(0, PATH_ROLE) == path:
                if c.checkState(0) != it.checkState():
                    c.setCheckState(0, it.checkState())  # triggers _tree_item_changed
                break

    def _preview_clicked(self, it: QListWidgetItem):
        if self._shown_group is None:
            return
        for c in self._children(self._shown_group):
            if c.data(0, PATH_ROLE) == it.data(PATH_ROLE):
                self.tree.blockSignals(True)
                self.tree.setCurrentItem(c)
                self.tree.blockSignals(False)
                break

    def _thumb_loaded(self, path: str, img: QImage):
        if img.isNull():
            return
        pm = QPixmap.fromImage(img)
        if len(self.thumbs) > 1500:
            self.thumbs.clear()
        self.thumbs[path] = pm
        for i in range(self.preview.count()):
            it = self.preview.item(i)
            if it.data(PATH_ROLE) == path:
                it.setIcon(pm)

    # ----- open / context menu
    def _item_path(self, item) -> str | None:
        p = item.data(0, PATH_ROLE) if isinstance(item, QTreeWidgetItem) else item.data(PATH_ROLE)
        return p if p and p != "__unreadable__" else None

    def _open_item(self, item, *_):
        p = self._item_path(item)
        if p:
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        p = self._item_path(item) if item else None
        if not p:
            return
        menu = QMenu(self)
        menu.addAction("Open", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
        menu.addAction("Show in Explorer", lambda: show_in_explorer(p))
        menu.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(p))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ----- file operations
    def _confirm_marked(self, verb: str) -> list[str] | None:
        marked = self._marked_items()
        if not marked:
            QMessageBox.information(self, "Nothing marked",
                                    "Tick the files you want to remove, or use Auto-mark duplicates.")
            return None
        paths = [c.data(0, PATH_ROLE) for c in marked]
        size = sum(self.records[p].size for p in paths)
        whole = [g for g in self._groups() if g.data(0, KIND_ROLE) == "duplicate"
                 and all(c.checkState(0) == Qt.CheckState.Checked for c in self._children(g))]
        text = f"{verb} {len(paths):,} file(s), {human_size(size)}?"
        icon = QMessageBox.Icon.Question
        if whole:
            text += (f"\n\nWarning: in {len(whole)} group(s) EVERY copy is marked, so no copy of "
                     f"those images would be left behind.")
            icon = QMessageBox.Icon.Warning
        box = QMessageBox(icon, "Confirm", text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return paths if box.exec() == QMessageBox.StandardButton.Yes else None

    def trash_marked(self):
        paths = self._confirm_marked("Send to the Recycle Bin")
        if not paths:
            return
        self._run_file_op("trash", paths, None, "Sending to Recycle Bin", "sent to the Recycle Bin")

    def move_marked(self):
        dest = QFileDialog.getExistingDirectory(self, "Move marked files to…")
        if not dest:
            return
        paths = self._confirm_marked(f"Move to {dest}:")
        if not paths:
            return
        self._run_file_op("move", paths, dest, f"Moving to {dest}", "moved")

    def _run_file_op(self, op: str, paths: list[str], dest: str | None, title: str, what: str):
        sizes = {p: self.records[p].size for p in paths if p in self.records}
        total_bytes = sum(sizes.values())
        dlg = QProgressDialog(f"{title}…", "Stop", 0, len(paths), self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumWidth(460)
        dlg.setValue(0)

        th = FileOpThread(op, paths, sizes, dest)
        self._file_op = th  # keep a reference while running

        def on_progress(done, total, done_bytes, name):
            dlg.setValue(done)
            text = (f"{title}…\n\n{done:,} of {total:,} files  ·  "
                    f"{human_size(done_bytes)} of {human_size(total_bytes)}")
            if name:
                text += f"\n{name}"
            dlg.setLabelText(text)

        state = {"finished": False}

        def on_finished(ok, errors, stopped):
            state["finished"] = True
            dlg.hide()            # (close() would emit canceled)
            dlg.deleteLater()
            th.wait()
            self._file_op = None
            self._after_removal(ok, errors, what + (" (stopped early)" if stopped else ""))

        def on_cancel():  # Stop button, Esc, or closing the dialog
            if state["finished"] or th._cancel:
                return
            th.cancel()
            dlg.setLabelText("Stopping after the current batch…")
            dlg.setCancelButton(None)
            dlg.show()            # Qt hides the dialog on cancel; keep it until the batch ends

        th.progress.connect(on_progress)
        th.finished_op.connect(on_finished)
        dlg.canceled.connect(on_cancel)
        dlg.rejected.connect(on_cancel)
        th.start()

    def _after_removal(self, done: list[str], errors: list[str], what: str):
        gone = set(done)
        self.tree.blockSignals(True)
        for i in reversed(range(self.tree.topLevelItemCount())):
            g = self.tree.topLevelItem(i)
            for c in self._children(g):
                if c.data(0, PATH_ROLE) in gone:
                    g.removeChild(c)
            if g.data(0, PATH_ROLE) == "__unreadable__":
                if g.childCount() == 0:
                    self.tree.takeTopLevelItem(i)
            elif g.childCount() < 2:
                if g is self._shown_group:
                    self._shown_group = None
                self.tree.takeTopLevelItem(i)
            else:
                self._refresh_group_header(g)
        self.tree.blockSignals(False)
        for p in gone:
            self.thumbs.pop(p, None)
        self._shown_group = None
        self.preview.clear()
        cur = self.tree.currentItem()
        if cur is not None:
            self._current_changed(cur, None)
        self._update_summary()
        msg = f"{len(done):,} file(s) {what}."
        self.status_lbl.setText(msg)
        if errors:
            QMessageBox.warning(self, "Some files failed",
                                msg + "\n\nFailed:\n" + "\n".join(errors[:20])
                                + ("\n…" if len(errors) > 20 else ""))

    def export_csv(self):
        if not any(True for _ in self._groups()):
            QMessageBox.information(self, "No results", "Run a scan first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export results", "duplicates.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["group", "group_type", "path", "media", "size_bytes", "width", "height",
                        "duration_s", "match", "marked"])
            for gi, g in enumerate(self._groups(), start=1):
                for c in self._children(g):
                    r = self.records[c.data(0, PATH_ROLE)]
                    w.writerow([gi, g.data(0, KIND_ROLE), r.path, r.media, r.size, r.width,
                                r.height, round(r.duration, 2), c.text(COL_MATCH),
                                c.checkState(0) == Qt.CheckState.Checked])
        self.status_lbl.setText(f"Exported to {path}")


def show_in_explorer(path: str):
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Image Duplicate Finder")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    return app.exec()
