"""Tests for the Live board's pure logic (no Tk window is ever created).

Runs as a plain script:  python tests/test_gui_board.py   (pytest also works)

Scope: error classification, the run model (cumulative counts across retry
rounds, whole-run total never shrinking, honest tape), ETA/clock formatting, the
offline list preview, recent-report listing, and the fetch->progress hand-off
that carries each ticker's record to the board.
"""
from __future__ import annotations

import queue
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_analyzer.review import fetch, gui_board as gb  # noqa: E402
from stock_analyzer.review.universe import DEFAULT_TICKERS  # noqa: E402


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
        == "Energy sector · us"
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nall {len(tests)} board tests passed")
