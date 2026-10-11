"""Metric registry + derivation of scoring inputs.

This module is the single source of truth for:
  * which metrics are scored,
  * each metric's category,
  * its scoring DIRECTION (higher_better / lower_better),
  * whether it is ranked WITHIN SECTOR or across the whole universe.

`data_collection` produces a RAW sourced record per ticker (prices, indicators,
yfinance `.info` fields). `derive_metric_inputs()` turns that raw record into the
registry's metric keys, applying the few comparability transforms that are
documented inline (e.g. absolute free cash flow -> FCF yield so large- and
small-caps are comparable). No transform invents data: if an input is missing
the derived metric is None (recorded as unavailable, never zero).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

CATEGORIES = ["fundamentals", "technicals", "momentum", "analyst", "sentiment"]

# Sentinel for a missing/unmappable GICS sector. Tickers labelled this way are
# never ranked against each other (see scoring._rank_one_metric): "Unknown"
# records an absence of data, not membership of a comparable peer group.
UNKNOWN_SECTOR = "Unknown"

HIGHER = "higher_better"
LOWER = "lower_better"


@dataclass(frozen=True)
class MetricDef:
    key: str
    category: str
    direction: str          # HIGHER or LOWER
    sector_relative: bool   # True -> rank within GICS sector; False -> whole universe
    label: str
    description: str
    weight: float = 1.0     # default weight within its category (override in config)


# -----------------------------------------------------------------------------
# THE REGISTRY.  Fundamentals are sector-relative (software vs banks vs miners
# have structurally different margins/leverage). Technicals/momentum/analyst are
# ranked across the whole universe (price behaviour is comparable cross-sector).
# -----------------------------------------------------------------------------
REGISTRY: list[MetricDef] = [
    # ---- Fundamentals (sector-relative) ----
    MetricDef("revenue_growth", "fundamentals", HIGHER, True,
              "Revenue growth (yoy)", "Higher top-line growth ranks better."),
    MetricDef("earnings_growth", "fundamentals", HIGHER, True,
              "Earnings growth (yoy)", "Higher earnings growth ranks better."),
    MetricDef("pe", "fundamentals", LOWER, True,
              "Trailing P/E", "Lower P/E ranks better. Negative P/E is treated as missing."),
    MetricDef("forward_pe", "fundamentals", LOWER, True,
              "Forward P/E", "Lower forward P/E ranks better. Non-positive treated as missing."),
    MetricDef("peg", "fundamentals", LOWER, True,
              "PEG ratio", "Lower PEG ranks better. Often unavailable -> excluded when missing."),
    MetricDef("gross_margin", "fundamentals", HIGHER, True,
              "Gross margin", "Higher gross margin ranks better."),
    MetricDef("operating_margin", "fundamentals", HIGHER, True,
              "Operating margin", "Higher operating margin ranks better."),
    MetricDef("net_margin", "fundamentals", HIGHER, True,
              "Net margin", "Higher net margin ranks better."),
    MetricDef("roe", "fundamentals", HIGHER, True,
              "Return on equity", "Higher ROE ranks better."),
    MetricDef("roa", "fundamentals", HIGHER, True,
              "Return on assets", "Higher ROA ranks better."),
    MetricDef("debt_to_equity", "fundamentals", LOWER, True,
              "Debt / equity", "Lower leverage ranks better. Negative (negative-equity) is treated as missing."),
    MetricDef("fcf_yield", "fundamentals", HIGHER, True,
              "Free cash flow yield", "FCF / market cap. Higher ranks better (size-neutral vs raw FCF)."),
    MetricDef("cash_to_marketcap", "fundamentals", HIGHER, True,
              "Cash / market cap", "Balance-sheet cushion. Higher ranks better."),

    # ---- Technicals (universe-relative) ----
    MetricDef("price_vs_sma50", "technicals", HIGHER, False,
              "Price vs 50d MA", "Price / 50d MA - 1. Above the average ranks better (trend)."),
    MetricDef("price_vs_sma200", "technicals", HIGHER, False,
              "Price vs 200d MA", "Price / 200d MA - 1. Above the average ranks better (trend)."),
    MetricDef("trend_alignment", "technicals", HIGHER, False,
              "Trend alignment", "Fraction of price>20>50>100>200 stack satisfied. Higher = cleaner uptrend."),
    MetricDef("rsi_health", "technicals", HIGHER, False,
              "RSI neutrality", "50 - |RSI-14 - 50|. Higher = closer to neutral (penalises over-bought/sold). "
                                 "MODELLING CHOICE: arguable; flip to a momentum reading by ranking raw RSI."),
    MetricDef("macd_hist", "technicals", HIGHER, False,
              "MACD histogram", "Latest MACD histogram. Positive/rising ranks better."),

    # ---- Momentum (universe-relative) ----
    MetricDef("return_1m", "momentum", HIGHER, False,
              "1-month return", "Trailing ~21-session price return. Higher ranks better."),
    MetricDef("return_3m", "momentum", HIGHER, False,
              "3-month return", "Trailing ~63-session price return. Higher ranks better."),
    MetricDef("return_6m", "momentum", HIGHER, False,
              "6-month return", "Trailing ~126-session price return. Higher ranks better."),
    MetricDef("proximity_52w_high", "momentum", HIGHER, False,
              "Proximity to 52w high", "Price / 52-week high. Closer to the high ranks better."),
    MetricDef("relative_volume", "momentum", HIGHER, False,
              "Relative volume", "Volume / 50d average volume. Higher = more current interest."),

    # ---- Analyst (universe-relative) ----
    MetricDef("recommendation_mean", "analyst", LOWER, False,
              "Consensus rating", "yfinance recommendationMean (1=Strong Buy .. 5=Sell). Lower ranks better."),
    MetricDef("target_upside", "analyst", HIGHER, False,
              "Target upside", "Mean target / price - 1. Higher implied upside ranks better."),
    MetricDef("net_upgrades_90d", "analyst", HIGHER, False,
              "Net upgrades (90d)", "Upgrades minus downgrades in last 90d. Higher ranks better. Often missing."),

    # ---- Sentiment (universe-relative) ----
    # DEFAULT-MISSING: free yfinance does not give reliable news/social sentiment.
    # These stay None unless an optional paid provider is configured, in which
    # case the whole 'sentiment' category is excluded and its weight redistributed.
    MetricDef("news_sentiment", "sentiment", HIGHER, False,
              "News sentiment", "Provider-supplied (-1..+1). Disabled by default (see optional_providers)."),
    MetricDef("social_sentiment", "sentiment", HIGHER, False,
              "Social sentiment", "Provider-supplied (-1..+1). Disabled by default."),
]

REGISTRY_BY_KEY: dict[str, MetricDef] = {m.key: m for m in REGISTRY}

# Fields we COLLECT and DISPLAY but deliberately do NOT feed into the composite
# score (size descriptors and risk fields). Risk metrics are shown in the report
# as key figures (useful context for the downstream AI reviewer), but the spec's
# weight scheme has no Risk category - documented in README. Add a risk category
# by editing the registry + category_weights if you want it scored.
DISPLAY_FIELDS = [
    "name", "sector", "industry", "exchange", "currency",
    "price", "market_cap", "eps", "forward_eps",
    "week52_high", "week52_low", "volume", "avg_volume_50d",
    "beta", "volatility_annual", "max_drawdown_1y", "dollar_liquidity",
]


def _pos(x: Any) -> float | None:
    """Return x as float if it is a positive finite number, else None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return v if v > 0 else None


def _num(x: Any) -> float | None:
    """Return x as a finite float, else None (no positivity constraint)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def derive_metric_inputs(raw: dict) -> dict[str, float | None]:
    """Turn one raw sourced record into the registry's metric keys.

    Returns a dict keyed by every metric in REGISTRY; value is a float or None
    (None == unavailable, which the scorer excludes and re-weights around).
    """
    g = raw.get  # shorthand
    price = _pos(g("price"))
    mcap = _pos(g("market_cap"))

    def ratio(numer_key: str, denom: float | None) -> float | None:
        n = _num(g(numer_key))
        if n is None or denom is None or denom == 0:
            return None
        return n / denom

    out: dict[str, float | None] = {}

    # Fundamentals
    out["revenue_growth"] = _num(g("revenue_growth"))
    out["earnings_growth"] = _num(g("earnings_growth"))
    out["pe"] = _pos(g("trailing_pe"))            # negative P/E -> meaningless to rank -> missing
    out["forward_pe"] = _pos(g("forward_pe"))
    out["peg"] = _pos(g("peg"))
    out["gross_margin"] = _num(g("gross_margin"))
    out["operating_margin"] = _num(g("operating_margin"))
    out["net_margin"] = _num(g("net_margin"))
    out["roe"] = _num(g("roe"))
    out["roa"] = _num(g("roa"))
    # Negative D/E means negative book equity, not "less leverage": ranked as-is it
    # would be the best (lowest) leverage score. Treat as missing, like negative P/E.
    de = _num(g("debt_to_equity"))
    out["debt_to_equity"] = de if de is not None and de >= 0 else None
    out["fcf_yield"] = ratio("free_cash_flow", mcap)       # size-neutral
    out["cash_to_marketcap"] = ratio("total_cash", mcap)

    # Technicals
    sma50 = _pos(g("sma50"))
    sma200 = _pos(g("sma200"))
    out["price_vs_sma50"] = (price / sma50 - 1.0) if (price and sma50) else None
    out["price_vs_sma200"] = (price / sma200 - 1.0) if (price and sma200) else None
    out["trend_alignment"] = _trend_alignment(
        price, _pos(g("sma20")), sma50, _pos(g("sma100")), sma200
    )
    rsi = _num(g("rsi14"))
    out["rsi_health"] = (50.0 - abs(rsi - 50.0)) if rsi is not None else None
    out["macd_hist"] = _num(g("macd_hist"))

    # Momentum
    out["return_1m"] = _num(g("ret_1m"))
    out["return_3m"] = _num(g("ret_3m"))
    out["return_6m"] = _num(g("ret_6m"))
    w52h = _pos(g("week52_high"))
    out["proximity_52w_high"] = (price / w52h) if (price and w52h) else None
    vol = _num(g("volume"))
    avgvol = _pos(g("avg_volume_50d"))
    out["relative_volume"] = (vol / avgvol) if (vol is not None and avgvol) else None

    # Analyst
    out["recommendation_mean"] = _pos(g("recommendation_mean"))
    tgt = _pos(g("target_mean_price"))
    out["target_upside"] = (tgt / price - 1.0) if (tgt and price) else None
    out["net_upgrades_90d"] = _num(g("net_upgrades_90d"))

    # Sentiment (default None)
    out["news_sentiment"] = _num(g("news_sentiment"))
    out["social_sentiment"] = _num(g("social_sentiment"))

    return out


def _trend_alignment(price, sma20, sma50, sma100, sma200) -> float | None:
    """Fraction of the bullish MA stack (price>=20>=50>=100>=200) satisfied.

    Only consecutive pairs where BOTH values exist are counted, so short
    histories (no 200d MA yet) still produce a partial reading rather than None.
    """
    chain = [price, sma20, sma50, sma100, sma200]
    satisfied = 0
    valid = 0
    for a, b in zip(chain, chain[1:]):
        if a is None or b is None:
            continue
        valid += 1
        if a >= b:
            satisfied += 1
    if valid == 0:
        return None
    return satisfied / valid


def build_metric_frame(records: list[dict]) -> tuple[pd.DataFrame, pd.Series]:
    """Build the (ticker x metric) input frame + a sector Series for scoring.

    Returns:
        metric_frame: DataFrame indexed by ticker, one column per registry metric
                      (float or NaN). NaN == unavailable.
        sector:       Series indexed by ticker with the GICS sector (or 'Unknown').
    """
    rows: dict[str, dict] = {}
    sectors: dict[str, str] = {}
    for rec in records:
        ticker = rec["ticker"]
        rows[ticker] = derive_metric_inputs(rec)
        sectors[ticker] = rec.get("sector") or UNKNOWN_SECTOR

    frame = pd.DataFrame.from_dict(rows, orient="index")
    # Guarantee every registry column exists even if every value was None.
    for m in REGISTRY:
        if m.key not in frame.columns:
            frame[m.key] = pd.NA
    frame = frame[[m.key for m in REGISTRY]].astype("float64")
    sector = pd.Series(sectors, name="sector")
    return frame, sector
