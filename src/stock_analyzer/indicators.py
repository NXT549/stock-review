"""Technical indicators.

LIBRARY CHOICE: we use `ta` (bukosabino/ta), NOT pandas-ta. Rationale: `ta` is a
single, pure-Python dependency with a stable API and no compiled extensions, so
it pins cleanly. We deliberately avoid installing both libraries.

`compute_indicators()` takes a yfinance-style OHLCV history DataFrame (columns:
Open, High, Low, Close, Volume; DatetimeIndex ascending) and returns the LATEST
value of each indicator plus trailing returns and risk stats. Any indicator that
cannot be computed from the available history is returned as None - never faked.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _last_finite(series: pd.Series) -> float | None:
    """Last non-NaN, finite value of a series, or None."""
    if series is None or len(series) == 0:
        return None
    s = series.dropna()
    if s.empty:
        return None
    v = float(s.iloc[-1])
    return v if math.isfinite(v) else None


def _trailing_return(close: pd.Series, sessions: int) -> float | None:
    """Return over `sessions` trading days: close[-1]/close[-1-sessions] - 1."""
    c = close.dropna()
    if len(c) <= sessions:
        return None
    now = float(c.iloc[-1])
    then = float(c.iloc[-1 - sessions])
    if then <= 0 or not math.isfinite(now) or not math.isfinite(then):
        return None
    return now / then - 1.0


def _max_drawdown(close: pd.Series) -> float | None:
    """Worst peak-to-trough drawdown over the window (negative number)."""
    c = close.dropna()
    if len(c) < 2:
        return None
    running_max = c.cummax()
    drawdown = c / running_max - 1.0
    mdd = float(drawdown.min())
    return mdd if math.isfinite(mdd) else None


def compute_indicators(hist: pd.DataFrame) -> dict:
    """Compute indicators from an OHLCV history. Missing -> None (never faked)."""
    out: dict[str, float | None] = {
        "sma20": None, "sma50": None, "sma100": None, "sma200": None,
        "rsi14": None, "macd_hist": None,
        "bb_high": None, "bb_low": None, "bb_mid": None,
        "atr14": None,
        "ret_1m": None, "ret_3m": None, "ret_6m": None,
        "week52_high": None, "week52_low": None,
        "avg_volume_50d": None,
        "volatility_annual": None, "max_drawdown_1y": None,
    }
    if hist is None or hist.empty or "Close" not in hist:
        return out

    # Import here so the rest of the package imports without `ta` installed
    # (e.g. the synthetic/offline test path never reaches this point).
    import ta

    hist = hist.sort_index()
    close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    if close.empty:
        return out
    high = pd.to_numeric(hist.get("High", close), errors="coerce")
    low = pd.to_numeric(hist.get("Low", close), errors="coerce")
    volume = pd.to_numeric(hist.get("Volume", pd.Series(dtype=float)), errors="coerce")

    n = len(close)

    # Simple moving averages (need >= window points).
    for window, key in [(20, "sma20"), (50, "sma50"), (100, "sma100"), (200, "sma200")]:
        if n >= window:
            out[key] = _last_finite(ta.trend.sma_indicator(close, window=window))

    # RSI (14).
    if n >= 15:
        out["rsi14"] = _last_finite(ta.momentum.RSIIndicator(close, window=14).rsi())

    # MACD histogram (12/26/9). macd_diff() == MACD line - signal line.
    if n >= 35:
        out["macd_hist"] = _last_finite(ta.trend.MACD(close).macd_diff())

    # Bollinger Bands (20, 2 sigma).
    if n >= 20:
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        out["bb_high"] = _last_finite(bb.bollinger_hband())
        out["bb_low"] = _last_finite(bb.bollinger_lband())
        out["bb_mid"] = _last_finite(bb.bollinger_mavg())

    # ATR (14).
    if n >= 15 and high.notna().any() and low.notna().any():
        out["atr14"] = _last_finite(
            ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14)
            .average_true_range()
        )

    # Trailing returns (~21/63/126 trading days).
    out["ret_1m"] = _trailing_return(close, 21)
    out["ret_3m"] = _trailing_return(close, 63)
    out["ret_6m"] = _trailing_return(close, 126)

    # 52-week high/low from the trailing ~252 sessions (or all we have).
    window = close.tail(252)
    hi_window = high.tail(252).dropna()
    lo_window = low.tail(252).dropna()
    out["week52_high"] = float(hi_window.max()) if not hi_window.empty else float(window.max())
    out["week52_low"] = float(lo_window.min()) if not lo_window.empty else float(window.min())

    # Average daily volume (50d).
    if volume.notna().sum() >= 50:
        out["avg_volume_50d"] = _last_finite(volume.rolling(50).mean())
    elif volume.notna().any():
        out["avg_volume_50d"] = float(volume.dropna().mean())

    # Annualised volatility from daily log-ish returns.
    rets = close.pct_change().dropna()
    if len(rets) >= 20:
        vol = float(rets.std() * math.sqrt(252))
        out["volatility_annual"] = vol if math.isfinite(vol) else None

    # Max drawdown over (up to) the last year.
    out["max_drawdown_1y"] = _max_drawdown(close.tail(252))

    return out
