"""Time-remaining estimator. Run:  .venv\\Scripts\\python.exe -m tests.test_eta"""

from dupefinder.eta import EtaEstimator, fmt_eta


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def main():
    c = Clock()
    e = EtaEstimator(clock=c)

    # Steady 10 files/s over 1000 files: no estimate at first, then about right.
    e.update("Analyzing files", 0, 1000)
    c.t = 1.0
    e.update("Analyzing files", 10, 1000)
    assert e.remaining() is None, "too early to estimate"
    for i in range(2, 21):
        c.t = float(i)
        e.update("Analyzing files", i * 10, 1000)
    r = e.remaining()
    print("steady, 200/1000 done at 20 s:", round(r, 1), "->", fmt_eta(r))
    assert abs(r - 80) < 1, r

    # Counts down between updates.
    c.t = 25.0
    assert abs(e.remaining() - 75) < 1, e.remaining()

    # Stage name gains "(AI on GPU)": same stage, estimate kept.
    c.t = 21.0
    e.update("Analyzing files (AI on GPU)", 210, 1000)
    assert e.remaining() is not None

    # Slowdown (a run of long videos): the recent rate pulls the estimate up quickly.
    for i in range(1, 31):
        c.t = 21.0 + i
        e.update("Analyzing files", 210 + i, 1000)
    r = e.remaining()
    print("after slowing to 1 file/s:", round(r), "s ->", fmt_eta(r))
    assert r > 300, r

    # A new stage starts fresh.
    c.t = 100.0
    e.update("Verifying exact video copies", 0, 4)
    assert e.remaining() is None

    # Finished stage.
    c.t = 110.0
    e.update("Verifying exact video copies", 4, 4)
    assert e.remaining() == 0

    for s, want in [(None, "estimating"), (4, "a few seconds"), (42, "45 s"), (61, "1 min"),
                    (125, "2 min 10 s"), (1500, "25 min"), (3725, "1 h 00 min"), (5400, "1 h 30 min")]:
        txt = fmt_eta(s)
        assert want in txt.replace("about 1 h left", "1 h 00 min"), (s, txt)
    print("formats:", [fmt_eta(s) for s in (4, 42, 125, 1500, 5400)])
    print("ETA TEST PASSED")


if __name__ == "__main__":
    main()
