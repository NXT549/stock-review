"""Tests for the Live board's motion helpers (pure; no Tk window is created).

Runs as a plain script:  python tests/test_gui_board_fx.py   (pytest also works)

Scope: the rules the decoration must keep. The bar may lag the real fraction
but never lead it; a real share is never drawn as nothing; a pop never invents
a return; quips fire once and never outrank a retry or a failure; the loading
scenes carry no made-up market figures; and every frame is deterministic.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_analyzer.review import ascii_art  # noqa: E402
from stock_analyzer.review import gui_board as gb  # noqa: E402
from stock_analyzer.review import gui_board_fx as fx  # noqa: E402
from stock_analyzer.review import gui_board_table as gt  # noqa: E402


def test_ease_lags_but_never_leads():
    shown, seen = 0.0, []
    for _ in range(200):
        shown = fx.ease(shown, 0.6, fx.FX_MS / 1000)
        assert shown <= 0.6                       # never ahead of the truth
        seen.append(shown)
    assert seen == sorted(seen) and seen[0] > 0   # moves, monotonically
    assert seen[-1] == 0.6                        # and lands exactly
    assert fx.ease(0.9, 0.4, 0.066) == 0.4        # the truth dropped: follow at once
    assert fx.ease(0.2, 0.2, 0.066) == 0.2
    assert fx.ease(0.0, 5.0, 10.0) == 1.0         # clamped, however long the tick
    assert fx.ease(0.3, 0.8, 0.0) == 0.3          # no time passed, no movement
    print("OK ease")


def _px(cells, kind):
    return sum(x1 - x0 for x0, x1, k in cells if k == kind)


def test_bar_cells_cover_the_real_shares():
    cells = fx.bar_cells(200, 0.5, 0.1, cell=8, gap=2)
    assert all(x0 < x1 for x0, x1, _k in cells)
    assert [c[0] for c in cells] == sorted(c[0] for c in cells)      # left to right
    assert cells[-1][1] <= 200
    order = [k for _x0, _x1, k in cells]                             # ok, then fail, then empty
    assert order == sorted(order, key=("ok", "fail", "empty").index)
    # of 200 px: fetched is 0-100, failed 100-120, the rest empty (a boundary
    # may fall in the gap between two cells, so these are bounds)
    assert max(x1 for _x0, x1, k in cells if k == "ok") <= 100
    assert 100 <= min(x0 for x0, _x1, k in cells if k == "fail")
    assert max(x1 for _x0, x1, k in cells if k == "fail") <= 120
    assert 120 <= min(x0 for x0, _x1, k in cells if k == "empty")
    assert _px(cells, "ok") == 80 and _px(cells, "fail") == 16     # 10 and 2 cells of 8
    assert _px(fx.bar_cells(200, 0, 0), "ok") == 0 == _px(fx.bar_cells(200, 0, 0), "fail")
    full = fx.bar_cells(200, 1.0, 0.0)
    assert {k for _a, _b, k in full} == {"ok"}
    # a real share too small for a pixel still shows: 1 failure in 5,000
    tiny = fx.bar_cells(200, 0.5, 1 / 5000)
    assert _px(tiny, "fail") >= 1
    assert _px(fx.bar_cells(200, 1 / 5000, 0), "ok") >= 1
    assert _px(fx.bar_cells(200, 1.0, 1 / 5000), "fail") >= 1        # even on a full bar
    assert fx.bar_cells(0, 0.5, 0.1) == []
    # the leading cell is split, so the fill moves by the pixel
    a, b = fx.bar_cells(200, 0.50, 0), fx.bar_cells(200, 0.51, 0)
    assert _px(b, "ok") - _px(a, "ok") in (1, 2)
    print("OK bar_cells")


def test_scanner_sweeps_inside_the_bar():
    spans = [fx.scanner(f, 20) for f in range(80)]
    assert all(0 <= lo < hi <= 20 and hi - lo == 4 for lo, hi in spans)
    assert min(lo for lo, _hi in spans) == 0 and max(hi for _lo, hi in spans) == 20
    assert all(abs(a[0] - b[0]) == 1 for a, b in zip(spans, spans[1:]))   # there and back
    assert fx.scanner(3, 0) == (0, 0) and fx.scanner(3, 2) == (0, 2)
    assert len({len(fx.rocket(f)) for f in range(40)}) == 1               # fixed width
    assert all(fx.rocket(f).endswith(fx.ROCKET_BODY) for f in range(40))
    print("OK scanner / rocket")


def test_pops_are_few_brief_and_honest():
    pops = fx.Pops()
    assert pops.add("AAA", 0.05, 10.0)
    assert not pops.add("BBB", 0.01, 10.1)          # too soon after the last: skipped
    assert pops.add("CCC", None, 10.4)              # no return known: kept as None
    assert [p.ticker for p in pops.live(10.5)] == ["CCC", "AAA"]     # newest first
    assert pops.live(10.5)[0].ret is None           # never turned into a 0
    for i in range(6):
        pops.add(f"T{i}", 0.0, 11.0 + i * 0.3)
    assert len(pops.live(12.6)) <= fx.Pops.CAP
    assert pops.live(100.0) == []                   # they leave
    assert not pops.add("", 0.1, 200.0)
    assert fx.pop_rise(0.0, 6) == 6 and fx.pop_rise(0.2, 6) == 0
    print("OK pops")


def test_trail_layout_behind_then_ahead():
    # room behind the rocket: pops trail it, newest nearest, none overlapping
    spots = fx.trail_layout(400, 30, [60, 50, 40], 600, gap=10)
    assert spots == [300, 240, 190]
    assert spots[0] + 60 <= 400 - 30
    # no room behind yet (the start of the bar): they go ahead instead
    ahead = fx.trail_layout(30, 30, [60, 50], 600, gap=10)
    assert ahead == [40, 110]
    # whatever does not fit is left out, never drawn off the bar
    assert fx.trail_layout(100, 30, [60, 50], 600, gap=10) == [0, None]
    assert fx.trail_layout(30, 30, [60, 50], 120, gap=10) == [40, None]
    assert fx.trail_layout(400, 30, [], 600) == []
    print("OK trail_layout")


def test_milestones_fire_once_in_order():
    m = fx.Milestones()
    assert m.hit(0.10, 40) is None
    assert m.hit(0.26, 40) == fx.MILESTONES[0][1]
    assert m.hit(0.30, 40) is None                  # once only
    assert m.hit(0.80, 40) == fx.MILESTONES[2][1]   # a jump keeps the later one
    assert m.hit(0.99, 40) is None and m.fired == 3
    assert fx.Milestones().hit(1.0, 40) is None     # a finished run needs no quip
    assert fx.Milestones().hit(0.5, 4) is None      # too small a run to remark on
    print("OK milestones")


def test_quip_never_outranks_status():
    m = gb.BoardModel()
    m.preload([f"T{i}" for i in range(8)])
    m.on_progress({"ticker": "T0", "total": 8, "name": "T0 Inc"})
    assert m.calm() and gb.run_mood(m, "Halfway there.") == "Halfway there."
    assert gb.run_mood(m) == m.mood()
    m.on_retry({"ticker": "T1", "attempt": 1, "max_retries": 3, "delay": 5})
    assert not m.calm() and "slow to answer" in gb.run_mood(m, "Halfway there.")
    m.on_progress({"ticker": "T1", "total": 8, "error": "404 Not Found"})
    assert "could not be fetched" in gb.run_mood(m, "Halfway there.")
    m.on_note("retry round 1/4: 1 ticker(s) still missing - pausing 5s")
    assert gb.run_mood(m, "Halfway there.").startswith("Retry round 1/4")
    print("OK quips yield to status")


def test_scenes_show_no_invented_market_data():
    assert len(fx.SCENES) == len(fx.SCENE_TITLES) >= 5
    assert {a["title"] for a in fx.SCENES} == set(fx.SCENE_TITLES)   # all still exist
    figure = re.compile(r"\d\s*%|\d\.\d")           # a percentage or a decimal price
    for i, anim in enumerate(fx.SCENES):
        for line in (ln for frame in anim["frames"] for ln in frame):
            assert not figure.search(line), (anim["title"], line)
            assert "SCOR" not in line.upper() and "BULLISH" not in line.replace(" ", "")
        for t in range(0, 40, 2):                   # every caption is one of ours
            title, quip = fx.scene_caption(i, t).split(" · ")
            assert title == anim["title"] and quip in fx.SCENE_QUIPS[title]
    assert fx.scene_caption(0, 0.0) != fx.scene_caption(0, ascii_art.CAPTION_SECONDS)
    print("OK scenes carry no made-up figures")


def test_scene_layers_rebuild_the_frame():
    for i, anim in enumerate(fx.SCENES):
        accents = anim.get("accents") or {}
        for f, art in enumerate(anim["frames"]):
            sf = fx.scene_frame(i, f)
            assert len(sf.lines) <= fx.SCENE_ROWS
            grid = [list(line.ljust(fx.SCENE_COLS)) for line in sf.lines]
            for row, col, text in sf.runs:
                assert text and all(ch in accents for ch in text)
                assert all(grid[row][col + k] == " " for k in range(len(text)))
                grid[row][col:col + len(text)] = text
            assert all(ch not in accents for line in sf.lines for ch in line)
            want = [ln[:fx.SCENE_COLS].rstrip() for ln in art[:fx.SCENE_ROWS]]
            assert ["".join(r).rstrip() for r in grid] == want, (anim["title"], f)
    assert fx.scene_frame(0, 3) is fx.scene_frame(0, 3)          # cached
    assert fx.scene_frame(len(fx.SCENES), 0) == fx.scene_frame(0, 0)   # wraps
    print("OK scene layers")


def test_fit_art():
    measure = lambda size: (size * 0.6, size * 1.2)  # noqa: E731
    assert gt.fit_art(1000, 600, 100, 30, measure) == 16     # 9.6 x 19.2 px cells
    assert gt.fit_art(500, 600, 100, 30, measure) == 8       # width-bound
    assert gt.fit_art(1000, 300, 100, 30, measure) == 8      # height-bound
    assert gt.fit_art(420, 600, 100, 30, measure) == 7       # the smallest it goes
    assert gt.fit_art(400, 600, 100, 30, measure) is None    # too small to read
    assert gt.fit_art(5000, 5000, 100, 30, measure) == 22    # capped
    assert gt.fit_art(500, 600, 100, 30, measure, range(4, 9)) == 8
    print("OK fit_art")


def test_liftoff_is_deterministic_and_stays_on_stage():
    cols, rows = 80, 3
    seen = set()
    for f in range(fx.LIFTOFF_FRAMES):
        items = fx.liftoff(f, cols, rows)
        assert items == fx.liftoff(f, cols, rows)                 # no randomness
        for row, col, text, tone in items:
            assert 0 <= row < rows and 0 <= col and col + len(text) <= cols
            assert text and " " not in text and tone in ("accent", "muted")
        seen.update(text for _r, _c, text, _t in items)
    assert any(t.endswith(fx.ROCKET_BODY) for t in seen)          # the rocket leaves
    assert "*" in seen                                            # and a firework opens
    assert fx.liftoff(fx.LIFTOFF_FRAMES, cols, rows) == []        # then nothing
    assert fx.liftoff(-1, cols, rows) == [] and fx.liftoff(3, 5, rows) == []
    assert all(r == 0 for r, _c, _t, _n in fx.liftoff(2, cols, 1))    # one row: rocket only
    for r in (4, 6):
        assert all(0 <= row < r for f in range(fx.LIFTOFF_FRAMES)
                   for row, _c, _t, _n in fx.liftoff(f, cols, r))
    print("OK liftoff")


def test_sweep_crosses_every_square_once():
    cols, n = 19, 60
    hit = [0] * n
    for f in range(fx.LIFTOFF_FRAMES):
        for i in fx.sweep(f, cols, n):
            assert 0 <= i < n
            hit[i] += 1
    assert all(hit), hit                                          # none left out
    assert fx.sweep(fx.LIFTOFF_FRAMES, cols, n) == set()          # and it ends clear
    assert fx.sweep(3, 0, n) == set() and fx.sweep(3, cols, 0) == set()
    print("OK sweep")


def test_runfx_state():
    run = fx.RunFx(scene=len(fx.SCENES) + 1)
    assert run.scene0 == 1                          # the saved counter wraps
    run.step(100.0, 0.0, 0)
    assert run.shown == 0.0 and run.frame == 1 and run.quip == ""
    run.land("AAA", 0.05, 100.0)
    run.land("BBB", None, 100.05)                   # no pop (too soon) but still fresh
    assert run.fresh(100.1) == {"AAA", "BBB"}
    assert run.fresh(100.0 + fx.FRESH_SECONDS + 0.1) == set()
    t, frac = 100.0, 0.5
    for _ in range(30):                             # two seconds of ticks
        t += fx.FX_MS / 1000
        run.step(t, frac, 40)
        assert run.shown <= frac
    assert run.shown == frac and run.quip == fx.MILESTONES[1][1]
    run.step(t + fx.QUIP_SECONDS + 0.1, frac, 40)
    assert run.quip == ""                           # the quip is put away again
    assert run.scene(100.0) == 1
    assert run.scene(100.0 + ascii_art.SWITCH_SECONDS + 0.1) == 2 % len(fx.SCENES)
    assert fx.RunFx().elapsed(5.0) == 0.0           # before the first step
    print("OK RunFx")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nall {len(tests)} board motion tests passed")
