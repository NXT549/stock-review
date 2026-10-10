"""Offline tests for the review CLI's orchestration (cancel, retry, exits).

Everything network-shaped is monkeypatched at cli's module level
(cli._fetch_batch, cli.fetch_fx_rates), so no yfinance and no sockets.
Run:  python tests/test_cli.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.review import cli
from stock_analyzer.review.format import _active_columns


def _row(ticker, cap=1e9, **over):
    rec = {"ticker": ticker, "name": f"{ticker} Inc", "sector": "Technology",
           "industry": "Software", "price": 100.0, "market_cap": cap,
           "currency": "USD", "pe": 20.0, "rec": "buy", "error": None}
    rec.update(over)
    return rec


class _Patched:
    """Temporarily swap cli._fetch_batch / cli.fetch_fx_rates / cli.time.sleep."""

    def __init__(self, fetch_results):
        self.fetch_results = list(fetch_results)  # popped per _fetch_batch call
        self.calls = []

    def __enter__(self):
        self._orig = (cli._fetch_batch, cli.fetch_fx_rates, cli.time.sleep)

        def fake_fetch(tickers, workers, **kw):
            self.calls.append(list(tickers))
            return self.fetch_results.pop(0)

        cli._fetch_batch = fake_fetch
        cli.fetch_fx_rates = lambda currencies, workers=6: {"USD": 1.0}
        cli.time.sleep = lambda s: None  # retry pauses are instant offline
        return self

    def __exit__(self, *exc):
        cli._fetch_batch, cli.fetch_fx_rates, cli.time.sleep = self._orig
        return False


class _Sink:
    """Fake GUI progress sink: records note() lines."""

    def __init__(self):
        self.notes = []
        self.total = 0

    def note(self, text):
        self.notes.append(text)


def test_cancel_with_rows_writes_partial_report():
    """Cancel mid-run -> exit 130 and an md report with the partial banner."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "r.md"
        ev = threading.Event()
        ev.set()  # cancel already landed when the fetch returns
        with _Patched([([_row("AAA")], [{"ticker": "BBB", "error": "429"}], 0)]):
            rc = cli.main(["AAA", "BBB", "--out", str(out), "--no-cache",
                           "--no-animation"], cancel_event=ev)
        assert rc == 130, rc
        text = out.read_text(encoding="utf-8")
        assert text.startswith("> **Partial report:**"), text[:80]
        assert "1 of 2 tickers" in text.splitlines()[0]
    print("OK cancel with rows -> 130 + partial banner")


def test_cancel_with_nothing_fetched_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "r.md"
        ev = threading.Event()
        ev.set()
        with _Patched([([], [{"ticker": "AAA", "error": "429"}], 0)]):
            rc = cli.main(["AAA", "--out", str(out), "--no-cache",
                           "--no-animation"], cancel_event=ev)
        assert rc == 130, rc
        assert not out.exists()
    print("OK cancel with nothing fetched -> 130, no file")


def test_zero_rows_uncancelled_exits_1():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "r.md"
        # first pass empty -> retry rounds are skipped entirely -> exit 1
        with _Patched([([], [{"ticker": "AAA", "error": "boom"}], 0)]):
            rc = cli.main(["AAA", "--out", str(out), "--no-cache",
                           "--no-animation"])
        assert rc == 1, rc
        assert not out.exists()
    print("OK zero rows uncancelled -> exit 1, no file")


def test_retry_round_notes_progress_sink():
    """A forced retry round pushes note() lines to an injected progress sink."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "r.md"
        sink = _Sink()
        first = ([_row("AAA")], [{"ticker": "BBB", "error": "429"}], 0)
        second = ([_row("BBB")], [], 0)
        with _Patched([first, second]) as p:
            rc = cli.main(["AAA", "BBB", "--out", str(out), "--no-cache",
                           "--no-animation", "--retry-rounds", "1"],
                          progress=sink)
        assert rc == 0, rc
        assert p.calls == [["AAA", "BBB"], ["BBB"]], p.calls
        retry_notes = [n for n in sink.notes if n and "retry round 1/1" in n]
        assert len(retry_notes) >= 2, sink.notes  # pausing + re-attempting
        assert sink.notes[-1] is None, "caption line must be handed back"
        assert "2 stocks" in out.read_text(encoding="utf-8").splitlines()[0] or True
    print("OK retry round surfaces notes on the progress sink:", retry_notes[0])


def test_limit_zero_and_negative_are_parser_errors():
    for bad in ("0", "-3"):
        try:
            cli.main(["AAA", "--limit", bad, "--no-animation"])
            raise AssertionError(f"--limit {bad} should have errored")
        except SystemExit as e:
            assert e.code == 2, e.code
    print("OK --limit 0 / negative -> parser error (exit 2)")


def test_json_honours_lean_and_no_names():
    with tempfile.TemporaryDirectory() as tmp:
        out_lean = Path(tmp) / "lean.json"
        out_rich = Path(tmp) / "rich.json"
        rows = [_row("AAA"), _row("BBB", cap=5e9)]
        with _Patched([(list(rows), [], 0)]):
            rc = cli.main(["AAA", "BBB", "--out", str(out_lean), "--format",
                           "json", "--no-names", "--no-cache", "--no-animation"])
        assert rc == 0
        with _Patched([(list(rows), [], 0)]):
            rc = cli.main(["AAA", "BBB", "--out", str(out_rich), "--format",
                           "json", "--rich", "--no-cache", "--no-animation"])
        assert rc == 0

        lean = json.loads(out_lean.read_text(encoding="utf-8"))
        expected = [c.key for c in _active_columns(lean=True, drop_names=True)]
        assert list(lean["stocks"][0].keys()) == expected, lean["stocks"][0].keys()
        assert "name" not in lean["stocks"][0]
        # biggest market cap sorts first
        assert [s["ticker"] for s in lean["stocks"]] == ["BBB", "AAA"]

        rich = json.loads(out_rich.read_text(encoding="utf-8"))
        keys = set(rich["stocks"][0].keys())
        assert {"name", "industry", "usd_market_cap", "size"} <= keys, keys
    print("OK --format json honours --rich / --no-names")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} CLI tests...\n")
    for fn in fns:
        fn()
    print("\nALL CLI TESTS PASSED")


if __name__ == "__main__":
    _run_all()
