"""Offline verification of the backtest harness math (no network).

We inject a synthetic price_loader so forward returns are a known function of
score, and assert the harness correctly detects the (engineered) signal:
  * top score decile mean return > bottom decile,
  * top-N excess over universe > 0,
  * mean rank IC strongly positive.
Then a no-signal construction should yield an IC near zero.

Run:  python tests/test_backtest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer import backtest as bt


def _business_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _history(score_fn) -> pd.DataFrame:
    """4 score dates, 10 tickers; score assigned by score_fn(i)."""
    run_dates = ["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27"]
    rows = []
    for d in run_dates:
        for i in range(10):
            rows.append({"run_date": pd.Timestamp(d), "ticker": f"T{i}",
                         "final_score": float(score_fn(i))})
    return pd.DataFrame(rows)


def _make_loader(growth_fn):
    """Each ticker T{i} grows at daily rate growth_fn(i) over a long window."""
    dates = _business_days("2025-01-01", 90)

    def loader(ticker: str):
        i = int(ticker[1:])
        r = growth_fn(i)
        prices = 100.0 * np.power(1.0 + r, np.arange(len(dates)))
        return pd.Series(prices, index=dates)
    return loader


def test_fwd_return_basic():
    dates = _business_days("2025-01-01", 30)
    prices = pd.Series(100.0 * np.power(1.01, np.arange(len(dates))), index=dates)
    # 5-session forward return from day 0 ~= 1.01^5 - 1, full window
    r5, truncated = bt._fwd_return(prices, dates[0], 5)
    assert abs(r5 - (1.01 ** 5 - 1)) < 1e-9, r5
    assert truncated is False
    # scored on the very last trade -> nothing observed afterwards
    r, truncated = bt._fwd_return(prices, dates[-1], 5)
    assert r is None and truncated is True
    print(f"OK _fwd_return: 5d={r5:.4f}")


def test_fwd_return_truncated_yields_last_trade_loss():
    """A series that craters and STOPS mid-horizon returns the realized loss.

    This is the anti-survivorship fix: a delisting crash must count as the
    loss it was (real last-trade data, flagged truncated) instead of the
    ticker silently vanishing from the backtest.
    """
    dates = _business_days("2025-01-01", 10)
    # trades at 100, then collapses to 10 and stops trading (delisted)
    prices = pd.Series([100.0] * 8 + [50.0, 10.0], index=dates)
    r, truncated = bt._fwd_return(prices, dates[7], 21)  # horizon runs past the end
    assert truncated is True
    assert abs(r - (10.0 / 100.0 - 1.0)) < 1e-9, r  # -90% to the last real trade
    print(f"OK truncated series -> last-trade loss: {r:+.0%} (flagged)")


def test_dedupe_history_keeps_latest_run():
    """Same-day duplicate rows (pre-fix DBs) keep only the highest run_id."""
    d = pd.Timestamp("2025-01-06")
    history = pd.DataFrame([
        {"run_date": d, "ticker": "T0", "final_score": 10.0, "run_id": 1},
        {"run_date": d, "ticker": "T1", "final_score": 20.0, "run_id": 1},
        {"run_date": d, "ticker": "T0", "final_score": 99.0, "run_id": 2},  # rerun
    ])
    out = bt._dedupe_history(history)
    assert len(out) == 2, out
    t0 = out[out["ticker"] == "T0"]
    assert float(t0["final_score"].iloc[0]) == 99.0 and int(t0["run_id"].iloc[0]) == 2
    # no run_id column -> untouched (nothing to dedupe on)
    plain = history.drop(columns=["run_id"])
    assert bt._dedupe_history(plain) is plain
    print("OK dedupe keeps only the latest run per (date, ticker)")


def test_dropped_ticker_count_is_surfaced():
    """Tickers with no loadable price series show up as a per-date count."""
    history = _history(score_fn=lambda i: i * 10.0)
    good_loader = _make_loader(growth_fn=lambda i: 0.0005 * i)

    def loader(ticker):  # T7 has no price series at all
        return None if ticker == "T7" else good_loader(ticker)

    class _Cfg:
        class reporting:
            top_n = 3

    res = bt.run_backtest(history, _Cfg(), price_loader=loader,
                          horizons={"1w": 5})
    # 4 score dates, one dropped ticker on each
    assert sum(res.dropped_no_price.values()) == 4, res.dropped_no_price
    assert all(n == 1 for n in res.dropped_no_price.values()), res.dropped_no_price
    assert "dropped (no price series loadable): 4" in res.summary, res.summary
    print("OK dropped-ticker count surfaced:", res.dropped_no_price)


def test_signal_is_detected():
    # Higher ticker index -> higher score AND higher growth (clean signal).
    history = _history(score_fn=lambda i: i * 10.0)
    loader = _make_loader(growth_fn=lambda i: 0.0005 * i)  # T9 grows fastest

    class _Cfg:  # minimal stand-in for cfg.reporting.top_n
        class reporting:  # noqa: N801
            top_n = 3

    res = bt.run_backtest(history, _Cfg(), price_loader=loader, horizons={"1w": 5, "1m": 21})
    # IC should be strongly positive at both horizons.
    for hz in ("1w", "1m"):
        ic = res.info_coefficient[hz]
        assert ic is not None and ic > 0.9, (hz, ic)
        tv = res.topn_vs_universe[hz]
        assert tv["excess"] > 0, (hz, tv)
        dt = res.decile_tables[hz]
        top = dt.loc[dt["decile"] == dt["decile"].max(), "mean_return"].iloc[0]
        bot = dt.loc[dt["decile"] == dt["decile"].min(), "mean_return"].iloc[0]
        assert top > bot, (hz, top, bot)
    print("OK signal detected:",
          {hz: round(res.info_coefficient[hz], 3) for hz in ("1w", "1m")},
          "excess(1m)=%.4f" % res.topn_vs_universe["1m"]["excess"])


def test_no_signal_low_ic():
    # Score order is REVERSED vs growth order in a non-monotone way -> weak IC.
    # Growth depends on (i % 2) only; score is i*10. Rank corr should be near 0.
    history = _history(score_fn=lambda i: i * 10.0)
    loader = _make_loader(growth_fn=lambda i: 0.0003 * (i % 2))
    class _Cfg:
        class reporting:
            top_n = 3
    res = bt.run_backtest(history, _Cfg(), price_loader=loader, horizons={"1m": 21})
    ic = res.info_coefficient["1m"]
    assert ic is None or abs(ic) < 0.6, ic
    print(f"OK weak/zero signal: IC(1m)={ic}")


def test_insufficient_history():
    one = pd.DataFrame([{"run_date": pd.Timestamp("2025-01-06"), "ticker": "T0",
                         "final_score": 50.0}])
    class _Cfg:
        class reporting:
            top_n = 3
    res = bt.run_backtest(one, _Cfg())
    assert res.n_dates <= 1 and res.summary and not res.decile_tables
    print("OK insufficient history handled")


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL BACKTEST TESTS PASSED")


if __name__ == "__main__":
    _run_all()
