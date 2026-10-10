"""Offline tests for data_collection's cache/retry plumbing (no network).

Everything here runs against a temp directory with a fake config object;
yfinance is never imported. Run:  python tests/test_data_collection.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer import data_collection as dc


def _collector(tmp_dir, **over) -> dc.DataCollector:
    data = SimpleNamespace(
        cache_dir=str(tmp_dir), cache_ttl_hours=24.0, history_period="1y",
        request_pause_seconds=0.0, max_retries=3, backoff_base_seconds=2.0,
        yfinance_min_version="0.2.40",
    )
    for k, v in over.items():
        setattr(data, k, v)
    cfg = SimpleNamespace(data=data, resolve=lambda p: Path(p))
    return dc.DataCollector(cfg)


def test_slug_is_filename_safe():
    assert dc._slug("1y") == "1y"
    assert dc._slug("6mo") == "6mo"
    assert dc._slug("MAX") == "max"
    assert dc._slug("2 years/max") == "2-years-max"
    for weird in ["a/b", "a\\b", "..", ". .", ""]:
        s = dc._slug(weird)
        assert s and "/" not in s and "\\" not in s and "." not in s, (weird, s)
    print("OK _slug: filename-safe variants")


def test_cache_path_varies_with_period():
    """Different history periods must not share a same-day cache file (CODE-03)."""
    with tempfile.TemporaryDirectory() as tmp:
        c1 = _collector(tmp, history_period="1y")
        c2 = _collector(tmp, history_period="6mo")
        p1 = c1._cache_file("AAPL", "hist", "csv", variant=dc._slug("1y"))
        p2 = c2._cache_file("AAPL", "hist", "csv", variant=dc._slug("6mo"))
        assert p1 != p2, (p1, p2)
        assert p1.name.endswith("_hist_1y.csv") and p2.name.endswith("_hist_6mo.csv")
    print("OK cache path varies with history_period")


def test_load_history_uses_period_variant():
    """_load_history actually writes to the period-keyed filename."""
    with tempfile.TemporaryDirectory() as tmp:
        col = _collector(tmp, history_period="6mo")
        df = pd.DataFrame({"Close": [1.0, 2.0]},
                          index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
        col._retry = lambda fn, op: df  # no network, no yfinance import
        out = col._load_history("AAPL")
        assert out is df
        expected = Path(tmp) / "AAPL" / f"{date.today().isoformat()}_hist_6mo.csv"
        assert expected.exists(), list((Path(tmp) / "AAPL").iterdir())
        # and a fresh same-day read serves it back from disk
        col._retry = lambda fn, op: (_ for _ in ()).throw(AssertionError("hit network"))
        again = col._load_history("AAPL")
        assert list(again["Close"]) == [1.0, 2.0]
    print("OK _load_history writes and re-reads the period-keyed cache")


def test_info_cache_path_unchanged():
    """.info has no fetch-shaping config: its filename must stay {date}_info.json."""
    with tempfile.TemporaryDirectory() as tmp:
        col = _collector(tmp)
        p = col._cache_file("AAPL", "info", "json")
        assert p.name == f"{date.today().isoformat()}_info.json", p.name
    print("OK info cache filename unchanged")


def test_retry_importerror_fails_fast():
    """ModuleNotFoundError -> exactly one attempt, zero sleeps (CODE-13)."""
    with tempfile.TemporaryDirectory() as tmp:
        col = _collector(tmp)
        sleeps: list[float] = []
        calls = {"import": 0, "value": 0}
        orig_sleep = dc.time.sleep
        dc.time.sleep = sleeps.append
        try:
            def _boom_import():
                calls["import"] += 1
                raise ModuleNotFoundError("No module named 'yfinance'")
            assert col._retry(_boom_import, "op") is None
            assert calls["import"] == 1 and sleeps == [], (calls, sleeps)

            def _boom_value():
                calls["value"] += 1
                raise ValueError("transient")
            assert col._retry(_boom_value, "op") is None
            # max_retries=3 -> three attempts with two backoff sleeps between
            assert calls["value"] == 3 and len(sleeps) == 2, (calls, sleeps)
        finally:
            dc.time.sleep = orig_sleep
    print("OK _retry: ImportError fails fast, ValueError gets full backoff")


def test_atomic_write():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "cache.json"
        # plain write, then overwrite
        dc._atomic_write(target, lambda p: p.write_text("one", encoding="utf-8"))
        assert target.read_text(encoding="utf-8") == "one"
        dc._atomic_write(target, lambda p: p.write_text("two", encoding="utf-8"))
        assert target.read_text(encoding="utf-8") == "two"

        # a failing writer leaves the destination untouched
        def _fail(p: Path):
            p.write_text("half", encoding="utf-8")
            raise OSError("disk went away")
        try:
            dc._atomic_write(target, _fail)
            raise AssertionError("expected OSError")
        except OSError:
            pass
        assert target.read_text(encoding="utf-8") == "two"

        # and no .tmp orphans remain in any case
        leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "cache.json"]
        assert not leftovers, leftovers
    print("OK _atomic_write: overwrite, failure isolation, no .tmp orphans")


def test_ticker_sanitization_stays_inside_cache_dir():
    """Dot-only and separator-bearing tickers cannot escape the cache dir (CODE-31)."""
    with tempfile.TemporaryDirectory() as tmp:
        col = _collector(tmp)
        root = Path(tmp).resolve()
        for evil in ["..", ".", "...", "a/b", "a\\b", ""]:
            p = col._cache_file(evil, "hist", "csv")
            assert root in p.resolve().parents, (evil, p)
    print("OK sanitizer: dot-only/separator tickers stay inside the cache dir")


def test_windows_reserved_names_are_prefixed():
    """PRN is a real NYSE ETF; reserved device names get a '_' prefix (CODE-31)."""
    assert dc._safe_cache_name("PRN") == "_PRN"
    assert dc._safe_cache_name("prn") == "_prn"
    assert dc._safe_cache_name("con.ax") == "_con.ax"  # reserved with extension too
    assert dc._safe_cache_name("COM1") == "_COM1"
    assert dc._safe_cache_name("LPT9") == "_LPT9"
    # non-reserved lookalikes pass through untouched
    assert dc._safe_cache_name("PRNT") == "PRNT"
    assert dc._safe_cache_name("COM10") == "COM10"
    assert dc._safe_cache_name("BRK.B") == "BRK.B"
    print("OK sanitizer: Windows reserved device names prefixed")


def test_empty_info_is_not_cached_or_ok():
    """An empty .info (429) must retry, not be cached all day as data_ok=True."""
    import types
    class TK:
        def __init__(self, t):
            self.info = {}
            self.upgrades_downgrades = None
    fake = types.SimpleNamespace(Ticker=TK, __version__="9.9",
                                 download=lambda *a, **k: pd.DataFrame())
    saved = sys.modules.get("yfinance")
    sys.modules["yfinance"] = fake
    try:
        with tempfile.TemporaryDirectory() as d:
            c = _collector(d, max_retries=2, backoff_base_seconds=0.0)
            c.cfg.optional_providers = {}
            rec = c.collect_one("AAPL")
            assert rec["data_ok"] is False, rec["data_ok"]
            assert not list(Path(d).rglob("*_info.json")), "empty info was cached"
    finally:
        if saved is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = saved
    print("OK empty info: retried, not cached, data_ok False")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} data-collection tests...\n")
    for fn in fns:
        fn()
    print("\nALL DATA-COLLECTION TESTS PASSED")


if __name__ == "__main__":
    _run_all()
