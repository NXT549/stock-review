"""Tests for the Live board's pure logic (no Tk window is ever created).

Runs as a plain script:  python tests/test_gui_board.py   (pytest also works)

Scope: error classification, the run model (cumulative counts across retry
rounds, whole-run total never shrinking, honest tape), ETA/clock formatting, the
offline list preview, recent-report listing, and the fetch->progress hand-off
that carries each ticker's record to the board; plus the redesign's pure parts:
filters, sorting (a missing value is never ranked as 0), option validation, the
end-of-run summary, the table / mosaic geometry and a WCAG contrast check over
every colour pair of both themes.
"""
from __future__ import annotations

import queue
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_analyzer.review import fetch, gui_board as gb  # noqa: E402
from stock_analyzer.review import gui_board_table as gt  # noqa: E402
from stock_analyzer.review import gui_board_theme as th  # noqa: E402
from stock_analyzer.review.universe import DEFAULT_TICKERS, SOURCES  # noqa: E402


def test_classify_error():
    assert gb.classify_error("429 Too Many Requests")[0] == "rate"
    assert gb.classify_error("HTTPError 404 Client Error: Not Found")[0] == "missing"
    kind, text = gb.classify_error("x" * 200)
    assert kind == "error" and text.startswith("Failed: ") and len(text) < 90
    assert gb.classify_error(None) == ("error", "Failed: fetch failed")
    print("OK classify_error")


def test_formatters():
    assert gb.fmt_clock(52) == "0:52" and gb.fmt_clock(3725) == "62:05"
    assert gb.fmt_clock(-5) == "0:00"
    assert gb.eta_text(1, 10, 5) == ""          # too little data to say
    assert gb.eta_text(10, 10, 30) == ""        # nothing left
    assert gb.eta_text(20, 30, 40) == "about 20 s left"
    assert gb.eta_text(10, 100, 120) == "about 18 min left"
    print("OK formatters")


def _p(ticker, total=4, **kw):
    return {"ticker": ticker, "total": total, **kw}


def test_model_counts_and_retry_rounds():
    m = gb.BoardModel()
    m.on_progress(_p("A", name="A Inc", ret_1m=0.05))
    m.on_progress(_p("B", error="404 Not Found"))
    m.on_progress(_p("C", error="429 Too Many Requests"))
    c = m.counts()
    assert (c["ok"], c["fail"], c["retry"], c["total"]) == (1, 2, 0, 4), c
    assert [r.kind for r in m.failed()] == ["missing", "rate"]
    # a retry round: the failures are being re-attempted, not "failed" yet
    m.on_note("retry round 1/4: 2 ticker(s) still missing - pausing 5s")
    c = m.counts()
    assert (c["fail"], c["retry"], c["ok"]) == (0, 2, 1), c
    assert m.headline() == "1 of 4 in, 2 retrying", m.headline()
    assert "Retry round 1/4" in m.mood()
    # the round's batch only holds 2 tickers; the whole-run total must not shrink
    m.on_progress(_p("C", total=2, name="C Inc", ret_1m=-0.02))
    assert m.counts()["total"] == 4 and m.counts()["ok"] == 2
    assert m.rows["C"].state == "ok" and m.rows["C"].detail == "Fetched"
    m.on_note(None)
    c = m.counts()
    assert (c["ok"], c["fail"], c["retry"]) == (2, 1, 0), c
    assert abs(m.fraction() - 0.75) < 1e-9
    print("OK model counts / retry rounds")


def test_model_in_fetch_retry_and_mood():
    m = gb.BoardModel()
    m.on_retry({"ticker": "X", "attempt": 1, "max_retries": 3, "delay": 8.2})
    assert m.rows["X"].state == "retry" and "attempt 1 of 3 in 8 s" in m.rows["X"].detail
    assert m.counts()["retry"] == 1
    assert "X is slow to answer" in m.mood()
    m.on_progress(_p("X", total=1, name="X Inc"))
    assert m.counts()["retry"] == 0 and not m.wait
    print("OK model in-fetch retry")


def test_tape_never_invents_values():
    m = gb.BoardModel()
    m.on_progress(_p("A", ret_1m=0.10))
    m.on_progress(_p("B"))                       # no return available
    m.on_progress(_p("C", ret_1m=None))
    m.on_progress(_p("D", ret_1m=-0.03))
    m.on_progress(_p("E", error="404"))          # failures have no tape entry
    assert m.tape() == [("A", 0.10), ("D", -0.03)], m.tape()
    assert m.rows["B"].ret_1m is None            # kept None, never 0
    assert len(m.tape(1)) == 1 and m.tape(1)[0][0] == "D"
    print("OK tape")


def test_preload_and_reset():
    m = gb.BoardModel()
    m.reset(["AAA", "BBB"])
    assert [r.state for r in m.rows.values()] == ["queued", "queued"]
    assert m.counts()["total"] == 2 and m.counts()["queued"] == 2
    m.on_progress(_p("AAA", total=2, name="Triple A"))
    assert m.counts()["ok"] == 1 and m.counts()["queued"] == 1
    m.reset()
    assert not m.rows and m.counts()["total"] == 0
    print("OK preload / reset")


def test_preview_tickers():
    assert gb.preview_tickers({"mode": "curated"}) == list(DEFAULT_TICKERS)
    assert gb.preview_tickers({"mode": "custom", "tickers": "aapl, msft aapl"}) \
        == ["AAPL", "MSFT"]
    assert gb.preview_tickers({"mode": "custom", "tickers": " "}) == []
    assert gb.preview_tickers({"mode": "source", "source": "japan"}) is None
    assert gb.preview_tickers({"mode": "sector", "sector": "energy"}) is None
    assert gb.preview_tickers({"mode": "file", "file": "/no/such/file.txt"}) == []
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "t.txt"
        f.write_text("AAA\nBBB\n", encoding="utf-8")
        assert gb.preview_tickers({"mode": "file", "file": str(f)}) == ["AAA", "BBB"]
    print("OK preview_tickers")


def test_mode_title():
    assert gb.mode_title({"mode": "curated"}) == "Curated starter list"
    assert gb.mode_title({"mode": "sector", "sector": "energy", "region": "us"}) \
        == "Energy sector · S&P 1500 (all US)"   # the alias is shown by its name
    print("OK mode_title")


def test_recent_reports():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        assert gb.recent_reports(folder=d) == []
        for i, name in enumerate(("stock_review_2026-01-01.md", "stock_review_2026-01-02.csv",
                                  "notes.txt", "stock_review_2026-01-03.json")):
            p = d / name
            p.write_text("x" * 2048, encoding="utf-8")
            import os
            os.utime(p, (1_700_000_000 + i * 86400,) * 2)
        got = gb.recent_reports(2, folder=d)
        assert [g["path"].name for g in got] == ["stock_review_2026-01-03.json",
                                                 "stock_review_2026-01-02.csv"], got
        assert got[0]["detail"] == "json · 2 KB"
        assert gb.recent_reports(folder=d / "missing") == []
    print("OK recent_reports")


def test_boardprogress_carries_record_and_fetch_gates_it():
    q: queue.Queue = queue.Queue()
    prog = gb.BoardProgress(q)
    prog.update(1, ticker="A", ok=1, failed=0,
                rec={"ticker": "A", "name": "A Inc", "ret_1m": 0.04, "error": None})
    kind, msg = q.get_nowait()
    assert kind == "progress" and msg["name"] == "A Inc" and msg["ret_1m"] == 0.04
    assert msg["sector"] is None and msg["price"] is None       # absent stays absent
    prog.update(2, ticker="B", ok=1, failed=1, rec={"ticker": "B", "error": "404"})
    assert q.get_nowait()[1]["error"] == "404"

    # End to end through the real batch loop: BoardProgress gets the record, a
    # plain QueueProgress (the Win98 GUI's) keeps its old signature untouched.
    orig = fetch.fetch_one
    fetch.fetch_one = lambda t, **kw: ({"ticker": t, "name": t, "error": "404"}
                                       if t == "BAD" else
                                       {"ticker": t, "name": t + " Inc", "error": None,
                                        "ret_1m": 0.01})
    try:
        for cls, expect_rec in ((gb.BoardProgress, True),
                                (gb.QueueProgress, False)):
            q = queue.Queue()
            fetch._fetch_batch(["OK1", "BAD"], 1, cache_dir=None, run_day="2026-01-01",
                               animator=cls(q))
            msgs = []
            while not q.empty():
                msgs.append(q.get_nowait()[1])
            assert len(msgs) == 2
            assert all(("error" in m) == expect_rec for m in msgs), msgs
    finally:
        fetch.fetch_one = orig
    print("OK BoardProgress hand-off")


def test_record_fields_stay_missing_when_absent():
    m = gb.BoardModel()
    m.on_progress(_p("A", name="A Inc", sector="Energy", price=12.5, currency="AUD",
                     ret_1m=0.01))
    m.on_progress(_p("B", name="B Inc", sector="n/a", price=None, currency="USD"))
    m.on_progress(_p("C", name="C Inc", price=float("nan")))
    a, b, c = (m.rows[k] for k in "ABC")
    assert (a.sector, a.price, a.currency) == ("Energy", 12.5, "AUD")
    assert (b.sector, b.price, b.currency) == ("", None, "")   # n/a -> blank, not a guess
    assert c.price is None and c.ret_1m is None                # NaN is missing, not 0
    print("OK record fields")


def _rows():
    m = gb.BoardModel()
    m.on_progress(_p("BBB", name="Beta", sector="Tech", price=5.0, ret_1m=-0.10))
    m.on_progress(_p("AAA", name="alpha", sector="Energy", price=50.0, ret_1m=0.20))
    m.on_progress(_p("CCC", name="Gamma"))                      # no price / return
    m.on_progress(_p("DDD", error="404 Not Found"))
    m.on_retry({"ticker": "EEE", "attempt": 1, "max_retries": 3, "delay": 5})
    return m


def test_filter_rows():
    m = _rows()
    rows = list(m.rows.values())
    assert [r.ticker for r in gb.filter_rows(rows)] == ["BBB", "AAA", "CCC", "DDD", "EEE"]
    assert [r.ticker for r in gb.filter_rows(rows, "ok")] == ["BBB", "AAA", "CCC"]
    assert [r.ticker for r in gb.filter_rows(rows, "fail")] == ["DDD"]
    assert [r.ticker for r in gb.filter_rows(rows, "retry")] == ["EEE"]
    # between retry rounds a failed ticker reads as retrying, like the counts do
    assert [r.ticker for r in gb.filter_rows(rows, "retry", in_retry_round=True)] \
        == ["DDD", "EEE"]
    assert gb.filter_rows(rows, "fail", in_retry_round=True) == []
    assert [r.ticker for r in gb.filter_rows(rows, query=" ALPH ")] == ["AAA"]
    assert [r.ticker for r in gb.filter_rows(rows, query="cc")] == ["CCC"]
    assert gb.filter_rows(rows, "fail", query="alpha") == []
    print("OK filter_rows")


def test_sort_rows_missing_values_sink():
    rows = list(_rows().rows.values())
    t = lambda rs: [r.ticker for r in rs]  # noqa: E731
    assert t(gb.sort_rows(rows, running=True)) == ["BBB", "AAA", "CCC", "DDD", "EEE"]
    assert t(gb.sort_rows(rows))[:2] == ["DDD", "EEE"]          # failures first when done
    assert t(gb.sort_rows(rows, "ticker")) == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert t(gb.sort_rows(rows, "name"))[:3] == ["AAA", "BBB", "CCC"]   # case-insensitive
    # a missing price / return is never ranked as 0: it stays last in BOTH directions
    assert t(gb.sort_rows(rows, "ret"))[:2] == ["BBB", "AAA"]
    assert t(gb.sort_rows(rows, "ret", desc=True))[:2] == ["AAA", "BBB"]
    assert set(t(gb.sort_rows(rows, "ret"))[2:]) == {"CCC", "DDD", "EEE"}
    assert set(t(gb.sort_rows(rows, "ret", desc=True))[2:]) == {"CCC", "DDD", "EEE"}
    assert t(gb.sort_rows(rows, "price", desc=True))[:2] == ["AAA", "BBB"]
    assert t(gb.sort_rows(rows, "status"))[:2] == ["DDD", "EEE"]
    assert t(gb.sort_rows(rows, "nonsense", running=True)) == t(rows)
    print("OK sort_rows")


def test_source_names_cover_every_universe():
    keys = [k for k, _n in gb.source_choices()]
    assert "curated" not in keys and len(keys) == len(set(keys))
    assert set(keys) == {gb.canonical_source(k) for k in SOURCES} - {"curated"}
    # a universe added to review/universe.py needs a display name here
    assert [k for k in keys if k not in gb.SOURCE_NAMES] == []
    assert gb.canonical_source("america") == "sp1500" and gb.source_name("asx200") == "ASX 200"
    assert gb.region_choices()[0] == ("", gb.ALL_REGIONS)
    assert all(k in dict(gb.source_choices()) for k, _n in gb.region_choices()[1:])
    assert dict(gb.sector_choices())["realestate"] == "Real estate"
    assert gb.hint_text({"mode": "source", "source": "australia"}) == "Australia's top 200"
    assert gb.hint_text({"mode": "curated"})[0].isupper()
    print("OK source names")


def test_validation_and_blockers():
    assert gb.validate_settings({"limit": "", "workers": "6"}) == {}
    assert set(gb.validate_settings({"limit": "abc", "workers": "0"})) == {"limit", "workers"}
    assert "limit" in gb.validate_settings({"limit": "-3"})
    assert "limit" in gb.validate_settings({"limit": "\u0663"})      # non-ASCII digit
    assert gb.run_blocker({"mode": "curated"}) == ""
    assert "ticker" in gb.run_blocker({"mode": "custom", "tickers": " "}).lower()
    assert gb.run_blocker({"mode": "custom", "tickers": "AAPL", "limit": "x"}) != ""
    assert gb.options_summary({"fmt": "md"}) == "Markdown · every ticker"
    assert gb.options_summary({"fmt": "csv", "rich": True, "limit": "50", "no_names": True}) \
        == "CSV · rich · first 50 · no company names"
    base = Path("/data/app")
    assert gb.short_path(base / "reports" / "r.md", base) == str(Path("reports") / "r.md")
    assert gb.short_path("/a/b/c/d/r.md").endswith(str(Path("d") / "r.md"))
    print("OK validation / blockers")


def test_finish_summary_and_failure_line():
    m = _rows()
    m.on_progress(_p("EEE", error="429 Too Many Requests"))
    m.on_progress(_p("FFF", error="429 Too Many Requests"))
    fails = m.failed()
    kw = dict(counts=m.counts(), fails=fails, cancelled=False, finish_early=False,
              file_name="r.md", size_kb=12, elapsed=75)
    s = gb.finish_summary(code=0, wrote=True, **kw)
    assert s["kind"] == "warn" and s["title"] == "3 of 6 fetched · 3 failed"
    assert s["saved"] == "r.md · 12 KB · 1:15" and s["retry"] == "Retry 3 failed"
    assert s["actions"] and s["fails"] == 3
    line = gb.failure_line(fails)
    assert line == "Not found: DDD · Rate-limited: EEE, FFF", line
    short = gb.failure_line(fails, budget=16)                   # failures stay named/counted
    assert short.startswith("Not found: DDD") and short.endswith("+2 more"), short
    assert gb.failure_line([]) == ""
    ok = gb.finish_summary(code=0, wrote=True, **{**kw, "fails": []})
    assert ok["kind"] == "ok" and ok["retry"] == ""
    early = gb.finish_summary(code=130, wrote=True,
                              **{**kw, "cancelled": True, "finish_early": True})
    assert early["title"].endswith("finished early") and early["kind"] == "warn"
    bad = gb.finish_summary(code=2, wrote=False, error="list unreachable", **kw)
    assert bad["kind"] == "bad" and not bad["actions"] and bad["sub"] == "list unreachable"
    stale = gb.finish_summary(code=0, wrote=False, **kw)        # no fresh file = no report
    assert stale["kind"] == "bad" and "cached" in stale["sub"]
    print("OK finish_summary / failure_line")


def test_theme_contrast_wcag():
    for theme in (th.DARK, th.LIGHT):
        for fg, bg in th.TEXT_PAIRS:
            c = th.contrast(getattr(theme, fg), getattr(theme, bg))
            assert c >= 4.5, f"{theme.name}: {fg} on {bg} is {c:.2f}:1 (text needs 4.5)"
        for fg, bg in th.INDICATOR_PAIRS:
            c = th.contrast(getattr(theme, fg), getattr(theme, bg))
            assert c >= 3.0, f"{theme.name}: {fg} on {bg} is {c:.2f}:1 (marks need 3)"
    assert abs(th.contrast("#000000", "#ffffff") - 21.0) < 1e-6
    assert th.resolve_theme("light").name == "light"
    assert th.resolve_theme("nonsense", system="light").name == "light"
    assert th.resolve_theme(None, system="dark").name == "dark"
    assert th.pick_font(["dejavu sans", "fixed"], ["Segoe UI", "DejaVu Sans"], "x") \
        == "dejavu sans"
    assert th.pick_font([], ["Segoe UI"], "TkDefaultFont") == "TkDefaultFont"
    assert isinstance(th.detect_reduced_motion(), bool)   # never raises, any platform
    assert th.mix("#000000", "#ffffff", 0.5) == "#808080"
    # a missing return is a neutral grey: never the colour of a zero return
    assert th.heat(th.DARK, None) == th.DARK.line_strong != th.heat(th.DARK, 0.0)
    assert th.heat(th.DARK, 0.5) == th.heat(th.DARK, 0.2) == th.DARK.ok   # capped
    assert th.heat(th.LIGHT, -0.2) == th.LIGHT.bad
    print("OK theme contrast")


def test_table_geometry():
    cols = (gt.Column("a", "A", 100), gt.Column("b", "B", 100, weight=2),
            gt.Column("c", "C", 50, weight=1))
    assert gt.layout_columns(cols, 250) == [(0, 100), (100, 200), (200, 250)]
    spans = gt.layout_columns(cols, 400)
    assert spans[0] == (0, 100) and spans[-1][1] == 400         # spare shared, none lost
    assert spans[1][1] - spans[1][0] == 200
    assert gt.layout_columns(cols, 100)[-1][1] == 250           # too narrow: mins kept
    assert gt.layout_columns(cols, 500, scale=2.0)[0] == (0, 200)
    assert gt.rows_visible(95, 30) == 3 and gt.rows_visible(5, 30) == 1
    assert gt.clamp_first(99, 10, 4) == 6 and gt.clamp_first(-3, 10, 4) == 0
    assert gt.clamp_first(5, 3, 10) == 0
    assert gt.scroll_to_show(0, 7, 4, 10) == 4 and gt.scroll_to_show(5, 2, 4, 10) == 2
    assert gt.scroll_to_show(3, 4, 4, 10) == 3                  # already visible
    assert gt.thumb(0, 10, 20, 200) is None                     # everything fits
    pos, length = gt.thumb(0, 100, 10, 200)
    assert (pos, length) == (0, 24)
    assert gt.thumb(90, 100, 10, 200)[0] == 200 - 24
    assert gt.first_for_thumb(200 - 24, 100, 10, 200) == 90
    assert gt.first_for_thumb(0, 100, 10, 200) == 0
    assert gt.bar_extent(None, 20) == 0                          # missing draws nothing
    assert gt.bar_extent(0.0, 20) == 0
    assert gt.bar_extent(0.2, 20) == 20 and gt.bar_extent(-0.9, 20) == -20
    assert gt.bar_extent(0.001, 20) == 1                         # a real move still shows
    measure = len                                                # 1 px per character
    assert gt.fit_text("abcdef", 10, measure) == "abcdef"
    assert gt.fit_text("abcdefghij", 5, measure) == "abcd…"
    assert gt.fit_text("abc", 0, measure) == ""
    print("OK table geometry")


def test_mosaic_grid():
    for n in (1, 89, 200, 500, 1500):
        cols, rows, pitch, gap = gt.mosaic_grid(n, 300, 56)
        assert cols * rows >= n, (n, cols, rows)
        assert cols * pitch - gap <= 300 and rows * pitch - gap <= 56
    assert gt.mosaic_grid(0, 300, 56) == (0, 0, 0, 0)
    cols, rows, pitch, _gap = gt.mosaic_grid(9000, 300, 56)      # more than fits: capped
    assert cols * rows < 9000 and pitch == 2
    assert gb.mosaic_caption(9000, cols * rows).startswith("First 4,200 of 9,000")
    assert gb.mosaic_caption(89, 89) == "One square per ticker"
    assert gt.mosaic_index(0, 0, 10, 8, 25) == 0
    assert gt.mosaic_index(17, 9, 10, 8, 25) == 12
    assert gt.mosaic_index(40, 16, 10, 8, 25) is None            # past the last square
    assert gt.mosaic_index(81, 0, 10, 8, 25) is None             # right of the grid
    print("OK mosaic grid")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nall {len(tests)} board tests passed")
