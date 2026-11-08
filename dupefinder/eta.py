"""Estimated time remaining for a progress stage.

Files in one scan can take very different amounts of time (a photo takes
milliseconds, a long video several seconds), so the rate is a blend of the
recent rate (last WINDOW seconds, reacts to changes) and the overall rate
since the stage started (steady). No estimate is given until enough of the
stage has run for the numbers to mean something.
"""

from __future__ import annotations

import math
import time
from collections import deque

WINDOW = 30.0          # seconds of history for the "recent" rate
MIN_ELAPSED = 3.0      # don't estimate before this many seconds...
MIN_FRACTION = 0.02    # ...or before this share of the stage is done
RECENT_WEIGHT = 0.6


class EtaEstimator:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._key = None
        self._start = 0.0
        self._samples: deque[tuple[float, int]] = deque()
        self._total = 0
        self._remaining = None   # seconds left, as of the last sample
        self._at = 0.0           # when that estimate was made

    @staticmethod
    def stage_key(stage: str, total: int):
        # "Analyzing files (AI on GPU)" is the same stage as "Analyzing files"
        return stage.split(" (")[0], total

    def reset(self):
        self._key = None
        self._samples.clear()
        self._remaining = None

    def update(self, stage: str, done: int, total: int):
        now = self._clock()
        key = self.stage_key(stage, total)
        if key != self._key or not total:
            self.reset()
            self._key = key
            self._start = now
        self._total = total
        if not total:
            return
        if self._samples and done < self._samples[-1][1]:   # went backwards: restart
            self._samples.clear()
            self._start = now
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[1][0] >= WINDOW:
            self._samples.popleft()

        elapsed = now - self._start
        left = total - done
        if left <= 0:
            self._remaining, self._at = 0.0, now
            return
        if elapsed < MIN_ELAPSED or done < max(1, MIN_FRACTION * total):
            self._remaining = None
            return
        overall = done / elapsed
        t0, d0 = self._samples[0]
        span = now - t0
        recent = (done - d0) / span if span >= 5.0 else overall
        rate = RECENT_WEIGHT * recent + (1 - RECENT_WEIGHT) * overall
        if rate <= 0:
            self._remaining = None
            return
        self._remaining, self._at = left / rate, now

    def remaining(self) -> float | None:
        """Seconds left now (counts down between updates), or None if unknown."""
        if self._remaining is None:
            return None
        return max(0.0, self._remaining - (self._clock() - self._at))


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "estimating time left…"
    if seconds < 10:
        return "a few seconds left"
    if seconds < 60:
        return f"about {int(math.ceil(seconds / 5) * 5)} s left"
    if seconds < 600:
        m, s = divmod(int(math.ceil(seconds / 10) * 10), 60)
        return f"about {m} min {s:02d} s left" if s else f"about {m} min left"
    if seconds < 3600:
        return f"about {int(round(seconds / 60))} min left"
    h, m = divmod(int(round(seconds / 300) * 5), 60)
    return f"about {h} h {m:02d} min left" if m else f"about {h} h left"
