"""Video-to-video and photo-to-video matching.

Input records carry per-video fingerprints computed by video.analyze_video:

* prop_ph / prop_dh / prop_ok  - perceptual hashes of 16 proportional frames
* vtimes / vemb                - timestamps + DINOv2 vectors of timed frames

Matching
--------
Normal levels  : same duration (+/-2 %), and >= 75 % of the aligned
                 proportional frames have matching perceptual hashes.
AI levels      : (a) same video  - same duration, >= 75 % of frames at the same
                     timestamps have AI similarity >= threshold;
                 (b) clip        - the shorter video's frames match the longer
                     video's frames at one consistent time offset for >= 60 %
                     of its (non-blank, non-common) frames.
Frames that look like frames in many other videos (channel intros, logos)
are ignored, and a frame that matches many moments of the other video
(static shots) only gets a fractional vote.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

import numpy as np

PROP_MATCH_FRACTION = 0.75
SAME_DURATION_REL = 0.03
CLIP_COVERAGE = 0.60
COMMON_MIN_VIDEOS = 2        # frame shared with >= this many *unrelated* other videos is "common"
PROBES_PER_VIDEO = 16


def _same_duration(a: float, b: float, rel: float = SAME_DURATION_REL) -> bool:
    return abs(a - b) <= max(1.0, rel * max(a, b))


def match_prop(videos: list, tp: int, td: int, aspect_tol: float,
               union: Callable[[int, int], None], links: dict) -> None:
    """Normal levels: same-length videos whose proportional frames match."""
    order = sorted(range(len(videos)), key=lambda k: videos[k][1].duration)
    for a_pos, ia in enumerate(order):
        ga, ra = videos[ia]
        if ra.prop_ph is None:
            continue
        for ib in order[a_pos + 1:]:
            gb, rb = videos[ib]
            if rb.duration - ra.duration > max(1.0, 0.02 * rb.duration):
                break
            if rb.prop_ph is None or len(rb.prop_ph) != len(ra.prop_ph):
                continue
            ar = (max(ra.width, 1) / max(ra.height, 1)) / (max(rb.width, 1) / max(rb.height, 1))
            if not (1 / aspect_tol < ar < aspect_tol):
                continue
            ok = ra.prop_ok & rb.prop_ok
            n = int(ok.sum())
            if n < 4:
                continue
            dp = np.bitwise_count(ra.prop_ph[ok] ^ rb.prop_ph[ok])
            dd = np.bitwise_count(ra.prop_dh[ok] ^ rb.prop_dh[ok])
            frac = float(((dp <= tp) & (dd <= td)).mean())
            if frac >= PROP_MATCH_FRACTION:
                union(ga, gb)
                links[(ga, gb)] = links[(gb, ga)] = ("same", round(100 * frac))


def _candidates(videos: list, thr: float) -> set[tuple[int, int]]:
    """Pairs of videos that share at least one strongly matching probe frame."""
    owners = np.concatenate([np.full(len(r.vemb), k) for k, (_, r) in enumerate(videos)])
    F = np.concatenate([r.vemb for _, r in videos]).astype(np.float32)
    pairs: set[tuple[int, int]] = set()
    for k, (_, r) in enumerate(videos):
        n = len(r.vemb)
        if n == 0:
            continue
        sel = np.unique(np.linspace(0, n - 1, min(n, PROBES_PER_VIDEO)).round().astype(int))
        hits = (r.vemb[sel].astype(np.float32) @ F.T) >= thr
        for o in np.unique(owners[hits.any(axis=0)]):
            if o != k:
                pairs.add((min(k, o), max(k, o)))
    return pairs


def _align(a, b, thr: float, common_a: np.ndarray, step: float):
    """Return ('same'|'clip', score, offset) or None. a is the shorter video."""
    S = a.vemb.astype(np.float32) @ b.vemb.astype(np.float32).T
    valid = ~common_a
    nvalid = int(valid.sum())
    if nvalid == 0:
        return None

    # (a) same video: frames at the same timestamps agree
    if _same_duration(a.duration, b.duration):
        tb = {round(t / step): j for j, t in enumerate(b.vtimes)}
        same = [S[i, tb[round(t / step)]] >= thr
                for i, t in enumerate(a.vtimes) if valid[i] and round(t / step) in tb]
        if len(same) >= min(3, nvalid) and np.mean(same) >= PROP_MATCH_FRACTION:
            return "same", round(100 * float(np.mean(same))), 0.0

    # (b) clip: vote for a consistent time offset (b_time - a_time)
    votes = defaultdict(float)
    frame_bins = []
    for i in np.nonzero(valid)[0]:
        js = np.nonzero(S[i] >= thr)[0]
        if len(js):
            bins = np.round((b.vtimes[js] - a.vtimes[i]) / step).astype(int)
            frame_bins.append(bins)
    if not frame_bins:
        return None
    for bins in frame_bins:
        for x in np.unique(bins):
            votes[x] += 1
    best, best_key = None, (0.0, 0.0)
    for center in votes:
        cov = sum(float(np.isin(bins, (center - 1, center, center + 1)).sum()) / len(bins)
                  for bins in frame_bins) / nvalid
        key = (round(cov, 6), votes[center])  # ties: prefer the most-voted offset
        if key > best_key:
            best, best_key = center, key
    best_cov = best_key[0]
    need = CLIP_COVERAGE if nvalid >= 3 else 1.0
    if best_cov < need:
        return None
    offset = _refine_offset(a, b, S, valid, thr, best, step)
    kind = "same" if _same_duration(a.duration, b.duration) and abs(offset) <= step else "clip"
    return kind, round(100 * best_cov), max(0.0, offset)


def _refine_offset(a, b, S, valid, thr, center, step) -> float:
    """Sub-sample offset estimate: for each frame, interpolate its best match time
    between neighbouring samples (weighted by similarity), then take the median."""
    offs = []
    lo, hi = (center - 1.5) * step, (center + 1.5) * step
    for i in np.nonzero(valid)[0]:
        cand = [j for j in range(len(b.vtimes))
                if S[i, j] >= thr and lo <= b.vtimes[j] - a.vtimes[i] <= hi]
        if not cand:
            continue
        j = max(cand, key=lambda j: S[i, j])
        nb = [k for k in (j - 1, j, j + 1) if 0 <= k < len(b.vtimes)]
        w = np.clip(S[i, nb] - (thr - 0.1), 0, None)
        if w.sum() <= 0:
            continue
        offs.append(float((w * b.vtimes[nb]).sum() / w.sum()) - a.vtimes[i])
    return float(np.median(offs)) if offs else center * step


def match_ai(videos: list, thr: float, step_of: Callable, union: Callable[[int, int], None],
             links: dict, cancelled: Callable[[], bool]) -> None:
    videos = [v for v in videos if v[1].vemb is not None and len(v[1].vemb)]
    if len(videos) < 2:
        return
    pairs = []
    for ka, kb in sorted(_candidates(videos, thr)):
        if videos[ka][1].duration > videos[kb][1].duration:
            ka, kb = kb, ka
        pairs.append((ka, kb))  # ka = shorter

    # Pass 1: align every candidate pair without suppression.
    no_mask = {k: np.zeros(len(r.vemb), bool) for k, (_, r) in enumerate(videos)}
    prelim = {}
    for ka, kb in pairs:
        if cancelled():
            return
        prelim[(ka, kb)] = _align(videos[ka][1], videos[kb][1], thr, no_mask[ka], step_of(videos[ka][1]))

    # "Common" frames (channel intros, logos): frames shared with >= COMMON_MIN_VIDEOS
    # videos that are otherwise unrelated (their pair did not align).
    sharers = {k: [set() for _ in range(len(r.vemb))] for k, (_, r) in enumerate(videos)}
    for ka, kb in pairs:
        if prelim[(ka, kb)] is not None:
            continue
        S = videos[ka][1].vemb.astype(np.float32) @ videos[kb][1].vemb.astype(np.float32).T >= thr
        for i in np.nonzero(S.any(axis=1))[0]:
            sharers[ka][i].add(kb)
        for j in np.nonzero(S.any(axis=0))[0]:
            sharers[kb][j].add(ka)
    common = {k: np.array([len(s) >= COMMON_MIN_VIDEOS - 1 for s in sl], dtype=bool)
              for k, sl in sharers.items()}

    # Pass 2: final alignment with common frames ignored.
    for ka, kb in pairs:
        if cancelled():
            return
        (ga, ra), (gb, rb) = videos[ka], videos[kb]
        res = prelim[(ka, kb)]
        if common[ka].any() and res is not None:
            res = _align(ra, rb, thr, common[ka], step_of(ra))
        if res is None:
            continue
        kind, score, offset = res
        union(ga, gb)
        if (ga, gb) in links:  # already explained by perceptual matching ("re-encoded")
            continue
        if kind == "same":
            links[(ga, gb)] = links[(gb, ga)] = ("same_ai", score)
        else:
            links[(ga, gb)] = ("clip_of", score, offset, offset + ra.duration)   # ga is a clip of gb
            links[(gb, ga)] = ("contains", score, offset, offset + ra.duration)


def match_photos(images: list, videos: list, thr: float) -> dict[int, list[tuple[int, float, float]]]:
    """For each video index: [(image index, similarity, time)] of photos showing one of its frames."""
    imgs = [(g, r) for g, r in images if r.embedding is not None]
    vids = [(g, r) for g, r in videos if r.vemb is not None and len(r.vemb)]
    out: dict[int, list] = defaultdict(list)
    if not imgs or not vids:
        return out
    E = np.stack([r.embedding[0] for _, r in imgs]).astype(np.float32)
    for gv, rv in vids:
        S = E @ rv.vemb.astype(np.float32).T
        best = S.argmax(axis=1)
        for k, j in enumerate(best):
            if S[k, j] >= thr:
                out[gv].append((imgs[k][0], float(S[k, j]), float(rv.vtimes[j])))
    return out
