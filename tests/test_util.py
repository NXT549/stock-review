"""First-ever direct unit tests for util.py's formatters (CODE-28/29).

Run:  python tests/test_util.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.util import fmt_num, fmt_pct


def test_fmt_pct_basic():
    assert fmt_pct(0.5) == "50.0%"
    assert fmt_pct(0.12345, digits=2) == "12.35%"
    assert fmt_pct(-0.25) == "-25.0%"
    assert fmt_pct(None) == "n/a"
    assert fmt_pct(float("nan")) == "n/a"
    assert fmt_pct(float("inf")) == "n/a"
    assert fmt_pct("garbage") == "n/a"
    print("OK fmt_pct: basic formatting + missing/non-finite -> n/a")


def test_fmt_num_basic():
    assert fmt_num(1234.5) == "1234.50"
    assert fmt_num(1.5e6, digits=1) == "1.5M"
    assert fmt_num(2.34e9) == "2.34B"
    assert fmt_num(9.9e12) == "9.90T"
    assert fmt_num(None) == "n/a"
    assert fmt_num(float("nan")) == "n/a"
    print("OK fmt_num: T/B/M scaling + missing -> n/a")


def test_fmt_no_negative_zero():
    """CODE-28: -0.0 and small negatives that round to zero must not print
    a misleading '-0.0%'/'-0.00' — the sign is stripped when the DISPLAYED
    value is exactly zero at the given precision."""
    assert fmt_pct(-0.0) == "0.0%"
    assert fmt_pct(-0.00001, digits=1) == "0.0%"   # rounds to zero at 1dp
    assert fmt_pct(-0.0006, digits=3) == "-0.060%"  # still materially nonzero
    assert fmt_num(-0.0) == "0.00"
    assert fmt_num(-0.00001, digits=2) == "0.00"
    assert fmt_num(-1e-10 * 1e6, digits=2) == "0.00"  # scaled-to-zero in the M branch
    print("OK -0.0 / near-zero: sign stripped only when the display rounds to zero")


def test_fmt_overflow_error_handled():
    """CODE-29: a Python int too large for float() must degrade to 'n/a',
    not raise OverflowError out of the formatter."""
    huge = 10 ** 400
    assert fmt_pct(huge) == "n/a"
    assert fmt_num(huge) == "n/a"
    assert fmt_num(-huge) == "n/a"
    print("OK OverflowError (huge int -> float) handled -> n/a")


def test_fmt_num_negative_magnitudes_unaffected():
    """A genuinely large negative value keeps its sign and scale suffix."""
    assert fmt_num(-1234567890123.0) == "-1.23T"
    assert fmt_num(-2.5e9) == "-2.50B"
    print("OK fmt_num: negative T/B/M values keep their sign")


def test_universe_csv_keeps_na_like_tickers():
    """Tickers NA / NAN / 0700 must survive the universe CSV read."""
    import tempfile
    from stock_analyzer.universe import _from_csv
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.csv"
        p.write_text("ticker\nNA\nNAN\n0700\nAAPL\n", encoding="utf-8")
        assert _from_csv(p) == ["NA", "NAN", "0700", "AAPL"], _from_csv(p)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} util tests...\n")
    for fn in fns:
        fn()
    print("\nALL UTIL TESTS PASSED")


if __name__ == "__main__":
    _run_all()
