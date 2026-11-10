"""Scanning, caching and duplicate grouping for images and videos.

Pipeline
--------
1. Walk the chosen folders and collect image and/or video files with their size.
2. File size: only files that share an exact byte size can be byte-for-byte
   copies, so SHA-256 is computed just for those. Videos over 64 MB get a
   quick partial hash first; the full hash runs only when partial hashes collide.
3. Image recognition: perceptual fingerprints per image (hashing.py), and
   per sampled video frame (video.py), computed in parallel worker processes.
4. AI modes: images and timed video frames go through a local DINOv2 vision
   model (ai.py) - finds crops, rotations, edits, trimmed clips.
5. Everything is cached in a local SQLite database so rescans are fast.
6. Grouping: exact copies, perceptual matches and AI matches are merged into
   duplicate groups with a union-find structure. Photos that show a frame
   of a video are reported in separate "photo in video" groups.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

from . import video_match
from .ai import EMBED_DIM, EMBED_VERSION, ai_available
from .hashing import IMAGE_EXTS, analyze
from .video import PARTIAL_HASH_MIN, VIDEO_EXTS, analyze_video, fmt_duration, timed_interval


@dataclass(frozen=True)
class Level:
    phash: tuple[int, int] | None   # (pHash, dHash) max bit distance; None = exact only
    ai: float | None = None         # min AI cosine similarity; None = AI off


SIMILARITY_LEVELS: dict[str, Level] = {
    "Exact copies only": Level(None),
    "Strict (near-identical)": Level((4, 6)),
    "Normal (resized / re-saved)": Level((8, 10)),
    "Loose (light edits)": Level((12, 16)),
    # Calibrated on real photos: unrelated photos score < ~0.83; recoloured or
    # re-cropped versions of the same picture score >= 0.90.
    "AI: edits, crops & rotations": Level((8, 10), 0.90),
    "AI loose: similar-looking (review!)": Level((8, 10), 0.87),
}
CLI_LEVELS = dict(zip(["exact", "strict", "normal", "loose", "ai", "ai-loose"], SIMILARITY_LEVELS))
DEFAULT_LEVEL = "Normal (resized / re-saved)"
MEDIA_TYPES = {"Images and videos": "both", "Images only": "images", "Videos only": "videos"}

ASPECT_TOLERANCE = 1.12  # perceptual matches need aspect ratios within 12%
AI_BATCH = 32
PHOTO_VIDEO_MIN = 0.90   # photo <-> video frame similarity

SKIP_DIRS = {"$recycle.bin", "system volume information", ".venv", "__pycache__"}

ProgressFn = Callable[[str, int, int], None]


class ScanCancelled(Exception):
    pass


@dataclass(eq=False)
class ImageRecord:
    """One media file (image or video)."""
    path: str
    size: int
    mtime_ns: int
    width: int = 0
    height: int = 0
    phash: int | None = None
    dhash: int | None = None
    sha256: str | None = None
    error: str | None = None
    embedding: np.ndarray | None = field(default=None, repr=False)  # images: (views, dim)
    media: str = "image"
    duration: float = 0.0
    fps: float = 0.0
    codec: str = ""
    bitrate: int = 0
    prop_ph: np.ndarray | None = field(default=None, repr=False)    # videos: (16,) uint64
    prop_dh: np.ndarray | None = field(default=None, repr=False)
    prop_ok: np.ndarray | None = field(default=None, repr=False)    # non-blank frames
    vtimes: np.ndarray | None = field(default=None, repr=False)     # videos: AI frame times
    vemb: np.ndarray | None = field(default=None, repr=False)       # videos: (frames, dim)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @property
    def is_video(self) -> bool:
        return self.media == "video"

    @property
    def full_sha(self) -> str | None:
        return self.sha256 if self.sha256 and not self.sha256.startswith("p:") else None


MediaRecord = ImageRecord


@dataclass
class DuplicateGroup:
    files: list[ImageRecord]          # files[0] is the reference (best quality)
    match: list[str]                  # human-readable match description per file
    similarity: list[float]           # 0-100 visual similarity to files[0]
    kind: str = "duplicate"           # "duplicate" or "photo_in_video" (never auto-marked)

    @property
    def reclaimable(self) -> int:
        return sum(f.size for f in self.files[1:]) if self.kind == "duplicate" else 0


@dataclass
class ScanResult:
    groups: list[DuplicateGroup]
    total_images: int
    unreadable: list[ImageRecord] = field(default_factory=list)
    elapsed: float = 0.0
    ai_device: str | None = None
    total_videos: int = 0


# --------------------------------------------------------------------------- cache


def default_cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "ImageDupeFinder2")   # separate from v1's cache
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "media_cache.sqlite3")


def _u64(a) -> bytes:
    return np.asarray(a, dtype=np.uint64).tobytes()


_SCHEMA = """CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                width INTEGER, height INTEGER, phash TEXT, dhash TEXT,
                perc_done INTEGER, sha256 TEXT, error TEXT);
               CREATE TABLE IF NOT EXISTS ai (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                model TEXT, views INTEGER, vec BLOB);
               CREATE TABLE IF NOT EXISTS vmeta (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                duration REAL, fps REAL, codec TEXT, bitrate INTEGER,
                prop_ph BLOB, prop_dh BLOB, prop_ok BLOB);
               CREATE TABLE IF NOT EXISTS vai (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                model TEXT, times BLOB, vec BLOB);"""


class HashCache:
    """SQLite cache keyed on path, invalidated by size or modification time.

    The cache only holds results that can be recomputed, so it must never stop
    a scan: a damaged file is replaced, a locked or unwritable one is skipped
    (the scan then runs without a saved cache)."""

    def __init__(self, path: str | None):
        self.path = path
        self.dead = False   # writing failed (disk full, ...): stop saving, keep scanning
        try:
            self.db = self._open(path)
        except sqlite3.OperationalError:   # locked by another copy of the app, or not writable
            self.db = self._open(None)
        except sqlite3.DatabaseError:      # not a database / malformed
            self.reset()

    @staticmethod
    def _open(path: str | None) -> sqlite3.Connection:
        db = sqlite3.connect(path or ":memory:")
        try:
            db.executescript(_SCHEMA)
        except sqlite3.Error:
            db.close()
            raise
        return db

    def reset(self) -> None:
        """Throw the cache file away and start an empty one (in memory if that fails)."""
        db = getattr(self, "db", None)
        if db is not None:
            db.close()
        try:
            if self.path:
                os.remove(self.path)
            self.db = self._open(self.path)
        except (OSError, sqlite3.Error):
            self.db = self._open(None)

    def _write(self, sql: str, rows: list) -> None:
        if self.dead or not rows:
            return
        try:
            self.db.executemany(sql, rows)
            self.db.commit()
        except sqlite3.Error:
            self.dead = True

    def _select(self, sql: str, recs: list[ImageRecord]) -> dict[str, tuple]:
        found: dict[str, tuple] = {}
        for i in range(0, len(recs), 500):
            chunk = recs[i : i + 500]
            q = ",".join("?" * len(chunk))
            for row in self.db.execute(sql.format(q=q), [r.path for r in chunk]):
                found[row[0]] = row
        return found

    def lookup(self, recs):
        return self._select("SELECT path,size,mtime_ns,width,height,phash,dhash,perc_done,sha256,error "
                            "FROM files WHERE path IN ({q})", recs)

    def lookup_ai(self, recs):
        return self._select("SELECT path,size,mtime_ns,model,views,vec FROM ai WHERE path IN ({q})", recs)

    def lookup_vmeta(self, recs):
        return self._select("SELECT path,size,mtime_ns,duration,fps,codec,bitrate,prop_ph,prop_dh,prop_ok "
                            "FROM vmeta WHERE path IN ({q})", recs)

    def lookup_vai(self, recs):
        return self._select("SELECT path,size,mtime_ns,model,times,vec FROM vai WHERE path IN ({q})", recs)

    def store(self, recs: Iterable[tuple[ImageRecord, bool]]) -> None:
        rows, vrows = [], []
        for r, perc in recs:
            rows.append((r.path, r.size, r.mtime_ns, r.width, r.height,
                         None if r.phash is None else f"{r.phash:016x}",
                         None if r.dhash is None else f"{r.dhash:016x}",
                         int(perc), r.sha256, r.error))
            if r.is_video:
                vrows.append((r.path, r.size, r.mtime_ns, r.duration, r.fps, r.codec, r.bitrate,
                              None if r.prop_ph is None else _u64(r.prop_ph),
                              None if r.prop_dh is None else _u64(r.prop_dh),
                              None if r.prop_ok is None else r.prop_ok.astype(np.uint8).tobytes()))
        self._write("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self._write("INSERT OR REPLACE INTO vmeta VALUES (?,?,?,?,?,?,?,?,?,?)", vrows)

    def store_ai(self, recs: Iterable[ImageRecord], model: str, views: int) -> None:
        rows = [(r.path, r.size, r.mtime_ns, model, views,
                 None if r.embedding is None else r.embedding.astype(np.float16).tobytes())
                for r in recs]
        self._write("INSERT OR REPLACE INTO ai VALUES (?,?,?,?,?,?)", rows)

    def store_vai(self, recs: Iterable[ImageRecord], model: str) -> None:
        rows = [(r.path, r.size, r.mtime_ns, model,
                 np.asarray(r.vtimes, np.float32).tobytes(),
                 np.clip(np.round(r.vemb * 127), -127, 127).astype(np.int8).tobytes())  # 8-bit
                for r in recs]
        self._write("INSERT OR REPLACE INTO vai VALUES (?,?,?,?,?,?)", rows)

    def close(self) -> None:
        self.db.close()


# Reading cached rows back. Each returns False for a damaged row (wrong length or
# type), which the caller then treats as "not cached" and analyses the file again.


def _load_file_row(r: ImageRecord, row: tuple) -> tuple[bool, bool]:
    """Fill r from its `files` row. Returns (perceptual hashes done, has a hash)."""
    try:
        _, _, _, w, h, ph, dh, perc_done, sha, err = row
        phash = int(ph, 16) if ph else None
        dhash = int(dh, 16) if dh else None
        if not (sha is None or isinstance(sha, str)):
            raise TypeError
        w, h = int(w or 0), int(h or 0)
    except (TypeError, ValueError):
        return False, False
    r.width, r.height, r.error = w, h, err
    r.phash, r.dhash, r.sha256 = phash, dhash, sha
    return bool(perc_done), sha is not None


def _load_vmeta(r: ImageRecord, row: tuple) -> bool:
    try:
        _, _, _, dur, fps, codec, bitrate, pp, pd, pk = row
        dur, fps, bitrate = float(dur or 0), float(fps or 0), int(bitrate or 0)
        frames = None
        if pp is not None:
            frames = (np.frombuffer(pp, np.uint64), np.frombuffer(pd, np.uint64),
                      np.frombuffer(pk, np.uint8).astype(bool))
            if not len(frames[0]) == len(frames[1]) == len(frames[2]):
                raise ValueError
    except (TypeError, ValueError):
        return False
    r.duration, r.fps, r.codec, r.bitrate = dur, fps, codec or "", bitrate
    if frames is not None:
        r.prop_ph, r.prop_dh, r.prop_ok = frames
    return True


def _load_vai(r: ImageRecord, row: tuple) -> bool:
    try:
        times = np.frombuffer(row[4], np.float32).astype(np.float64)
        vec = np.frombuffer(row[5], np.int8)
        if len(vec) != len(times) * EMBED_DIM:
            raise ValueError
    except (TypeError, ValueError):
        return False
    r.vtimes = times
    # (explicit width: a video with no usable frames is stored as zero rows)
    r.vemb = vec.reshape(len(times), EMBED_DIM).astype(np.float32) / 127.0
    return True


def _load_ai(r: ImageRecord, row: tuple, views: int) -> bool:
    try:
        if row[5] is not None:
            vec = np.frombuffer(row[5], np.float16)
            if len(vec) != views * EMBED_DIM:
                raise ValueError
            r.embedding = vec.reshape(views, EMBED_DIM)
    except (TypeError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- scanning


def find_media(folders: list[str], recursive: bool, cancelled: Callable[[], bool],
               progress: ProgressFn, media: str = "both") -> list[ImageRecord]:
    exts: dict[str, str] = {}
    if media in ("both", "images"):
        exts.update(dict.fromkeys(IMAGE_EXTS, "image"))
    if media in ("both", "videos"):
        exts.update(dict.fromkeys(VIDEO_EXTS, "video"))
    seen: set[str] = set()
    out: list[ImageRecord] = []
    stack = [os.path.abspath(f) for f in folders]
    visited_dirs: set[str] = set()
    last = 0.0
    while stack:
        if cancelled():
            raise ScanCancelled
        d = stack.pop()
        key = os.path.normcase(os.path.realpath(d))
        if key in visited_dirs:
            continue
        visited_dirs.add(key)
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                # A symbolic link is not a copy. Listing it next to its target would
                # invite deleting the real file and keeping a link that points nowhere.
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if recursive and e.name.lower() not in SKIP_DIRS:
                        stack.append(e.path)
                    continue
                kind = exts.get(os.path.splitext(e.name)[1].lower())
                if kind:
                    p = os.path.abspath(e.path)
                    k = os.path.normcase(p)
                    if k in seen:
                        continue
                    seen.add(k)
                    st = e.stat()
                    if st.st_size == 0:
                        continue
                    out.append(ImageRecord(p, st.st_size, st.st_mtime_ns, media=kind))
            except OSError:
                continue
        now = time.monotonic()
        if now - last >= 0.1:   # not once per folder: that floods the UI on big trees
            last = now
            progress("Finding files", len(out), 0)
    progress("Finding files", len(out), 0)
    return out


def find_images(folders, recursive, cancelled, progress):  # v1 compatibility
    return find_media(folders, recursive, cancelled, progress, "images")


class _UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _quality_key(r: ImageRecord):
    # Prefer most pixels, then (videos) higher bitrate and longer duration,
    # then biggest file (less compression), then oldest.
    return (-r.pixels, -r.bitrate, -round(r.duration), -r.size, r.mtime_ns, len(r.path))


def ai_similarity(a: ImageRecord, b: ImageRecord) -> tuple[float, int]:
    """(cosine similarity, rotation index 0-3) of image b relative to image a."""
    sims = (b.embedding @ a.embedding[0]).astype(np.float32)
    k = int(np.argmax(sims))
    if sims[k] - sims[0] < 0.05:  # only call it "rotated" when a rotation clearly fits better
        k = 0
    return float(sims[k]), k


def _match_images(records, idx_all, lvl, uf, cancelled):
    # Visually similar images: perceptual hash distance below threshold.
    if lvl.phash:
        tp, td = lvl.phash
        idx = np.array([i for i in idx_all if records[i].phash is not None], dtype=np.int64)
        m = len(idx)
        if m > 1:
            P = np.array([records[i].phash for i in idx], dtype=np.uint64)
            D = np.array([records[i].dhash for i in idx], dtype=np.uint64)
            AR = np.array([max(records[i].width, 1) / max(records[i].height, 1) for i in idx])
            cols = np.arange(m)
            block = max(16, min(2048, 4_000_000 // m))
            for s in range(0, m, block):
                if cancelled():
                    raise ScanCancelled
                e = min(s + block, m)
                dist = np.bitwise_count(P[s:e, None] ^ P[None, :])
                ok = (dist <= tp) & (cols[None, :] > np.arange(s, e)[:, None])
                a, b = np.nonzero(ok)
                if not len(a):
                    continue
                a = a + s
                keep = np.bitwise_count(D[a] ^ D[b]) <= td
                ratio = AR[a] / AR[b]
                keep &= (ratio < ASPECT_TOLERANCE) & (ratio > 1 / ASPECT_TOLERANCE)
                for x, y in zip(idx[a[keep]], idx[b[keep]]):
                    uf.union(int(x), int(y))

    # AI: cosine similarity of DINOv2 embeddings (any rotation of the other image).
    if lvl.ai is not None:
        idx = np.array([i for i in idx_all if records[i].embedding is not None], dtype=np.int64)
        m = len(idx)
        if m > 1:
            V = records[idx[0]].embedding.shape[0]
            E = np.stack([records[i].embedding for i in idx]).astype(np.float32)  # (m, V, dim)
            E0 = E[:, 0, :]
            Eall = E.reshape(m * V, -1)
            cols = np.arange(m)
            block = max(16, min(1024, 8_000_000 // (m * V)))
            for s in range(0, m, block):
                if cancelled():
                    raise ScanCancelled
                e = min(s + block, m)
                sim = (E0[s:e] @ Eall.T).reshape(e - s, m, V).max(axis=2)
                ok = (sim >= lvl.ai) & (cols[None, :] > np.arange(s, e)[:, None])
                a, b = np.nonzero(ok)
                for x, y in zip(idx[a + s], idx[b]):
                    uf.union(int(x), int(y))


def _image_label(f, ref, lvl) -> tuple[str, float]:
    d = _hamming(f.phash, ref.phash) if f.phash is not None and ref.phash is not None else 64
    if d == 0:
        return "Visually identical", 100.0
    if lvl.phash and d <= lvl.phash[0] or (lvl.ai is None and f.phash is not None):
        s = round(100.0 * (1 - d / 64), 1)
        return (f"Similar, resized ({s:.0f}%)" if f.pixels != ref.pixels else f"Similar ({s:.0f}%)"), s
    if f.embedding is not None and ref.embedding is not None:
        c, rot = ai_similarity(ref, f)
        s = round(100.0 * c, 1)
        what = "rotated" if rot else ("cropped" if abs(
            f.width / max(f.height, 1) - ref.width / max(ref.height, 1)) > 0.05 else "edited")
        return f"AI match, {what} ({s:.0f}%)", s
    return "Similar", 0.0


def _video_label(f, ref, links, members, records) -> tuple[str, float]:
    def describe(link, other):
        kind, score = link[0], link[1]
        if kind == "same":
            return f"Same video, re-encoded ({score}%)", float(score)
        if kind == "same_ai":
            return f"AI: same video, edited ({score}%)", float(score)
        if kind == "clip_of":
            return (f"Clip {fmt_duration(link[2])}–{fmt_duration(link[3])} of {other.name} "
                    f"({score}%)"), float(score)
        if kind == "contains":
            return f"Contains {other.name} at {fmt_duration(link[2])} ({score}%)", float(score)
        return "Similar video", 0.0

    gi_f, gi_ref = members[f], members[ref]
    if (gi_f, gi_ref) in links:
        return describe(links[(gi_f, gi_ref)], ref)
    for other, go in members.items():  # linked through another member of the group
        if other is not f and (gi_f, go) in links:
            return describe(links[(gi_f, go)], other)
    return "Similar video", 0.0


def group_duplicates(records: list[ImageRecord], level: str | Level,
                     cancelled: Callable[[], bool] = lambda: False,
                     photo_video: bool = False) -> list[DuplicateGroup]:
    lvl = SIMILARITY_LEVELS[level] if isinstance(level, str) else level
    n = len(records)
    uf = _UnionFind(n)
    links: dict[tuple[int, int], tuple] = {}

    # 1) Exact copies: identical size AND identical full SHA-256.
    exact: dict[tuple[int, str], list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        if r.full_sha:
            exact[(r.size, r.full_sha)].append(i)
    for idxs in exact.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)

    img_idx = [i for i, r in enumerate(records) if not r.is_video]
    vid = [(i, r) for i, r in enumerate(records) if r.is_video]

    # 2) Images
    _match_images(records, img_idx, lvl, uf, cancelled)

    # 3) Videos
    if vid and lvl.phash:
        video_match.match_prop(vid, *lvl.phash, ASPECT_TOLERANCE, uf.union, links)
    if vid and lvl.ai is not None:
        video_match.match_ai(vid, lvl.ai, lambda r: timed_interval(r.duration), uf.union, links,
                             cancelled)
    if cancelled():
        raise ScanCancelled

    buckets: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        buckets[uf.find(i)].append(i)

    groups: list[DuplicateGroup] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        files = sorted((records[i] for i in members), key=_quality_key)
        gidx = {records[i]: i for i in members}
        ref = files[0]
        match, sim = ["Reference (best quality)"], [100.0]
        for k, f in enumerate(files[1:], start=1):
            twin = next((o for o in files[:k] if f.full_sha and o.full_sha == f.full_sha
                         and o.size == f.size), None)
            if twin is ref:
                label = ("Exact copy", 100.0)
            elif twin is not None:
                label = (f"Exact copy of {twin.name}", 100.0)
            elif f.is_video:
                label = _video_label(f, ref, links, gidx, records)
            else:
                label = _image_label(f, ref, lvl)
            match.append(label[0])
            sim.append(label[1])
        groups.append(DuplicateGroup(files, match, sim))

    # 4) Photos that show a frame of a video (reported separately, never auto-marked)
    if photo_video and lvl.ai is not None and vid:
        imgs = [(i, records[i]) for i in img_idx]
        hits = video_match.match_photos(imgs, vid, max(lvl.ai, PHOTO_VIDEO_MIN))
        # one group per set of duplicate videos, shown against its best-quality copy
        by_root: dict[int, dict[int, tuple]] = defaultdict(dict)
        best_video: dict[int, int] = {}
        for gv, lst in hits.items():
            root = uf.find(gv)
            if root not in best_video or _quality_key(records[gv]) < _quality_key(records[best_video[root]]):
                best_video[root] = gv
            for gi, s, t in lst:
                if gi not in by_root[root] or s > by_root[root][gi][1]:
                    by_root[root][gi] = (gi, s, t, gv)   # best match, and the copy it was found in
        for root, photos in by_root.items():
            gv = best_video[root]
            v = records[gv]
            own = {gi: (gi, s, t) for gi, s, t in hits.get(gv, [])}
            lst = sorted(photos.values(), key=lambda h: h[2])
            files = [v] + [records[h[0]] for h in lst]
            match = ["Video"]
            for gi, s, t, src in lst:
                if gi in own:  # timestamp within the video shown as the group's reference
                    _, s, t = own[gi]
                    match.append(f"Photo matches frame at {fmt_duration(t)} ({100 * s:.0f}%)")
                else:
                    match.append(f"Photo matches {records[src].name} at {fmt_duration(t)} "
                                 f"({100 * s:.0f}%)")
            groups.append(DuplicateGroup(files, match, [100.0] + [100 * h[1] for h in lst],
                                         kind="photo_in_video"))

    groups.sort(key=lambda g: (g.kind != "duplicate", -g.reclaimable))
    return groups


MAX_CRASHES_IN_A_ROW = 10   # worker crashes with nothing read in between: not one bad file


def _stop_workers(ex: ProcessPoolExecutor) -> None:
    """End a pool now, without waiting for workers that are still reading a file
    (hashing a large video can take minutes). Workers only read, so this is safe."""
    try:
        stop = getattr(ex, "terminate_workers", None)   # Python 3.14+
        if stop is not None:
            stop()
            return
    except Exception:
        pass
    procs = list((getattr(ex, "_processes", None) or {}).values())
    ex.shutdown(wait=False, cancel_futures=True)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


def _crash_result(task: tuple, error: str = "Reading this file crashed the decoder, so it was skipped") -> dict:
    """Result for a file whose worker process died (or failed outright) while reading it."""
    kind, args = task
    out = {"path": args[0], "error": error}
    if kind == "video":
        out.update(prop_done=bool(args[3]), timed_done=bool(args[4]))
    else:
        out.update(perc_done=bool(args[1]), ai_done=bool(len(args) > 3 and args[3]))
    return out


def scan(folders: list[str], recursive: bool = True, level: str = DEFAULT_LEVEL,
         progress: ProgressFn = lambda *a: None,
         cancelled: Callable[[], bool] = lambda: False,
         cache_path: str | None = "default", workers: int | None = None,
         ai_rotations: bool = True, ai_gpu: bool = True, media: str = "both",
         photo_video: bool = False) -> ScanResult:
    t0 = time.perf_counter()
    lvl = SIMILARITY_LEVELS[level]
    need_perc_all = lvl.phash is not None
    need_ai = lvl.ai is not None
    views = 4 if ai_rotations else 1
    if need_ai:
        ok, why = ai_available()
        if not ok:
            raise RuntimeError(f"AI mode unavailable: {why}")
    VIDEO_AI_VERSION = EMBED_VERSION + "/video-v1"

    records = find_media(folders, recursive, cancelled, progress, media)
    n_vid = sum(r.is_video for r in records)

    # File size pre-filter: only files sharing a size can be exact copies.
    size_count: dict[int, int] = defaultdict(int)
    for r in records:
        size_count[r.size] += 1

    embedder = None
    cache = HashCache(default_cache_path() if cache_path == "default" else cache_path)
    try:
        vids = [r for r in records if r.is_video]
        try:
            cached = cache.lookup(records)
            cached_ai = cache.lookup_ai(records) if need_ai else {}
            cached_vm = cache.lookup_vmeta(vids) if vids else {}
            cached_vai = cache.lookup_vai(vids) if (vids and need_ai) else {}
        except sqlite3.DatabaseError:   # damaged cache file: start it again, scan from scratch
            cache.reset()
            cached, cached_ai, cached_vm, cached_vai = {}, {}, {}, {}
        by_path = {r.path: r for r in records}
        tasks: list[tuple] = []
        perc_state: dict[str, bool] = {}

        def fresh(row, r):
            return row is not None and row[1] == r.size and row[2] == r.mtime_ns

        for r in records:
            row = cached.get(r.path)
            have_perc = have_sha = have_ai = False
            if fresh(row, r):
                have_perc, have_sha = _load_file_row(r, row)
            want_sha = size_count[r.size] > 1 and not have_sha

            if r.is_video:
                vm = cached_vm.get(r.path)
                if not (fresh(vm, r) and _load_vmeta(r, vm)):
                    have_perc = False
                va = cached_vai.get(r.path)
                have_ai = fresh(va, r) and va[3] == VIDEO_AI_VERSION and _load_vai(r, va)
                perc_state[r.path] = have_perc
                sha_mode = None
                if want_sha:
                    sha_mode = "partial" if r.size > PARTIAL_HASH_MIN else "full"
                want_prop = need_perc_all and not have_perc
                want_timed = need_ai and not have_ai
                if sha_mode or want_prop or want_timed:
                    tasks.append(("video", (r.path, r.size, sha_mode, want_prop, want_timed)))
                continue

            arow = cached_ai.get(r.path)
            have_ai = (fresh(arow, r) and arow[3] == EMBED_VERSION and arow[4] == views
                       and _load_ai(r, arow, views))
            perc_state[r.path] = have_perc
            want_perc = need_perc_all and not have_perc
            want_ai = need_ai and not have_ai
            if want_perc or want_sha or want_ai:
                tasks.append(("image", (r.path, want_perc, want_sha, want_ai)))

        # Videos whose partial hashes collide need a full hash (2nd pass, below).
        pending_store: list[tuple[ImageRecord, bool]] = []
        pending_ai: list[ImageRecord] = []
        pending_vai: list[ImageRecord] = []
        ai_batch: list[tuple[ImageRecord, np.ndarray]] = []
        state = {"stage": "Analyzing files", "done": 0, "total": len(tasks), "last": 0.0}

        def get_embedder():
            nonlocal embedder
            if embedder is None:
                from .ai import Embedder
                embedder = Embedder(use_gpu=ai_gpu)
                state["stage"] = f"Analyzing files (AI on {embedder.device})"
            return embedder

        def flush_ai():
            if ai_batch:
                vecs = get_embedder().embed_views([p for _, p in ai_batch], ai_rotations)
                for (rec, _), v in zip(ai_batch, vecs):
                    rec.embedding = v
                    pending_ai.append(rec)
                ai_batch.clear()
            if pending_ai:
                cache.store_ai(pending_ai, EMBED_VERSION, views)
                pending_ai.clear()
            if pending_vai:
                cache.store_vai(pending_vai, VIDEO_AI_VERSION)
                pending_vai.clear()

        def handle(res: dict):
            r = by_path[res["path"]]
            # A failure that may not repeat (file locked, drive offline) is reported for
            # this scan but kept out of the cache, so the next scan tries the file again.
            keep = not res.get("retry")
            if "sha256" in res:
                r.sha256 = res["sha256"]
            if "width" in res:
                r.width, r.height = res["width"], res["height"]
            if r.is_video:
                for k in ("duration", "fps", "codec", "bitrate"):
                    if k in res:
                        setattr(r, k, res[k])
                if res.get("prop_done"):
                    perc_state[r.path] = keep
                    r.error = res.get("error")
                    ph = res.get("prop_phash")
                    if ph:
                        dh = res["prop_dhash"]
                        r.prop_ok = np.array([p is not None for p in ph])
                        r.prop_ph = np.array([p or 0 for p in ph], dtype=np.uint64)
                        r.prop_dh = np.array([d or 0 for d in dh], dtype=np.uint64)
                elif "error" in res:
                    r.error = res["error"]
                if res.get("timed_done"):
                    r.vtimes = np.asarray(res.get("timed_times", []), dtype=np.float64)
                    pix = res.get("timed_pixels")
                    r.vemb = (get_embedder().embed(pix) if pix is not None and len(pix)
                              else np.zeros((0, EMBED_DIM), np.float32))
                    if keep:
                        pending_vai.append(r)
                    if len(pending_vai) >= 20:
                        flush_ai()
            else:
                if res.get("perc_done"):
                    perc_state[r.path] = keep
                    r.error = res.get("error")
                    r.phash, r.dhash = res.get("phash"), res.get("dhash")
                elif "error" in res:
                    r.error = res["error"]
                if res.get("ai_done"):
                    if "ai_pixels" in res:
                        ai_batch.append((r, res["ai_pixels"]))
                    elif keep:  # blank or undecodable: remember so we don't retry
                        pending_ai.append(r)
                    if len(ai_batch) >= AI_BATCH:
                        flush_ai()
            pending_store.append((r, perc_state.get(r.path, False)))
            if len(pending_store) >= 200:
                cache.store(pending_store)
                pending_store.clear()

        def finish(res: dict):
            handle(res)
            state["done"] += 1
            now = time.monotonic()
            if now - state["last"] >= 0.25 or state["done"] == state["total"]:
                state["last"] = now
                progress(state["stage"], state["done"], state["total"])

        def run_pool(task_list):
            """Run per-file tasks in worker processes, a bounded number in flight.

            Cancel is checked twice a second, not only when a file finishes, and
            stops the workers at once. If a worker process dies (a damaged file can
            crash a decoder), the files that were in flight are run again one at a
            time: the one that kills its worker is reported as unreadable and the
            scan carries on with a new pool."""
            queue = deque(task_list)
            suspects: deque = deque()
            nworkers = workers or max(1, (os.cpu_count() or 2) - 1)
            crashes = 0   # in a row, with no file read successfully in between
            try:
                while queue or suspects:
                    solo = bool(suspects)
                    source = suspects if solo else queue
                    window = 1 if solo else nworkers * 3  # bounded: video results can be large
                    ex = ProcessPoolExecutor(max_workers=1 if solo else nworkers)
                    inflight: dict = {}
                    broken = False
                    try:
                        while (source or inflight) and not broken:
                            while source and len(inflight) < window:
                                t = source.popleft()
                                try:
                                    fut = ex.submit(analyze_video if t[0] == "video" else analyze, t[1])
                                except BrokenProcessPool:
                                    source.appendleft(t)
                                    broken = True
                                    break
                                inflight[fut] = t
                            if broken:
                                break
                            finished, _ = wait(inflight, timeout=0.5, return_when=FIRST_COMPLETED)
                            if cancelled():
                                raise ScanCancelled
                            for fut in finished:
                                t = inflight.pop(fut)
                                try:
                                    res = fut.result()
                                except BrokenProcessPool:
                                    broken = True
                                    if not solo:
                                        suspects.append(t)
                                        continue
                                    res = _crash_result(t)   # it ran alone, so this file is the cause
                                    crashes += 1
                                    if crashes >= MAX_CRASHES_IN_A_ROW:
                                        raise RuntimeError(
                                            "The processes that read files keep crashing, so the scan "
                                            f"was stopped. Last file: {t[1][0]}") from None
                                except Exception as e:   # a failure the worker didn't catch itself
                                    res = _crash_result(t, f"{type(e).__name__}: {e}")
                                else:
                                    crashes = 0
                                finish(res)
                        if broken:   # the pool is gone: keep what finished, re-run the rest alone
                            for fut, t in inflight.items():
                                if fut.done() and not fut.cancelled() and fut.exception() is None:
                                    finish(fut.result())
                                else:
                                    suspects.append(t)
                            inflight.clear()
                    finally:
                        if broken or inflight:   # crash, cancel or error: don't wait for workers
                            _stop_workers(ex)
                        else:
                            ex.shutdown(wait=False)
                flush_ai()
            finally:
                if pending_store:
                    cache.store(pending_store)
                    pending_store.clear()

        progress(state["stage"], 0, state["total"])
        run_pool(tasks)

        # Full SHA-256 for big videos that may still be exact copies: their quick
        # partial hashes collide, or a same-size file already has a full hash (kept
        # from an earlier scan), which a partial hash can't be compared with.
        by_size = defaultdict(list)
        for r in records:
            if r.sha256:
                by_size[r.size].append(r)
        full_tasks = []
        for grp in by_size.values():
            part = [r for r in grp if r.sha256.startswith("p:")]
            mixed = len(part) < len(grp)
            seen_hash = Counter(r.sha256 for r in part)
            full_tasks += [("video", (r.path, r.size, "full", False, False))
                           for r in part if mixed or seen_hash[r.sha256] > 1]
        if full_tasks:
            state.update(stage="Verifying exact video copies", done=0, total=len(full_tasks))
            run_pool(full_tasks)
    finally:
        cache.close()

    progress("Comparing files", 0, 0)
    groups = group_duplicates(records, lvl, cancelled, photo_video)
    unreadable = [r for r in records if r.error and (
        (r.is_video and r.prop_ph is None and r.vemb is None) or
        (not r.is_video and r.phash is None)) and (need_perc_all or need_ai)]
    device = embedder.device if embedder else ("cached" if need_ai else None)
    return ScanResult(groups, len(records) - n_vid, unreadable, time.perf_counter() - t0,
                      device, n_vid)
