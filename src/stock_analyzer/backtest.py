"""Backtest harness: did higher scores precede higher forward returns?

WHAT THIS IS / IS NOT
  This measures whether the stored daily scores had any relationship with
  subsequent price returns. It is the ONLY evidence that the 0-100 score means
  anything. UNTIL this shows positive, stable predictive value, the system's
  output is a RESEARCH SCREEN, not a buy signal. A clean score is not a forecast.

HOW IT WORKS
  * Scores come from the DB (accumulated by daily runs - fundamentals cannot be
    reconstructed for past dates, so history must genuinely accrue over time).
  * Forward returns come from REAL price history (prices ARE available
    historically), pulled per ticker and aligned to each score date.
  * We report, per horizon (default 1-week = 5 sessions, 1-month = 21 sessions):
      - mean forward return by score DECILE (decile 10 = highest scores),
      - top-N mean forward return vs the universe average,
      - mean rank information coefficient (Spearman corr of score vs fwd return).

The decile/IC math is split into pure functions so it can be unit-tested without
the network (inject a `price_loader`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .config import Config
from .logging_setup import get_logger
from .util import df_to_markdown

log = get_logger("backtest")

PriceLoader = Callable[[str], "pd.Series | None"]


@dataclass
class BacktestResult:
    horizons: dict[str, int]
    decile_tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    topn_vs_universe: dict[str, dict] = field(default_factory=dict)
    info_coefficient: dict[str, float] = field(default_factory=dict)
    n_observations: int = 0
    n_dates: int = 0
    summary: str = ""
    # tickers with NO loadable price series, counted per score date (ISO string).
    # These rows contribute no forward return; the count keeps the exclusion
    # visible instead of silently survivorship-biasing the results.
    dropped_no_price: dict[str, int] = field(default_factory=dict)


# ------------------------------- pure math -------------------------------
def _dedupe_history(history: pd.DataFrame) -> pd.DataFrame:
    """Keep only the LATEST run's score per (run_date, ticker).

    Same-day reruns used to stack duplicate rows (save_run now replaces, but
    already-polluted DBs keep their duplicates forever) — every duplicated
    ticker-date would otherwise count twice in deciles, top-N and IC.
    """
    if "run_id" not in history.columns:
        return history
    df = (history.sort_values("run_id", kind="stable")
                 .drop_duplicates(subset=["run_date", "ticker"], keep="last"))
    dropped = len(history) - len(df)
    if dropped:
        log.info("deduped score history: dropped %d stale same-day row(s)", dropped)
    return df


def _fwd_return(prices: pd.Series, d: pd.Timestamp,
                horizon: int) -> tuple[float | None, bool]:
    """(return, truncated) from the close on/just-before d to `horizon` sessions on.

    If the series ends before the full horizon (delisting, or history that
    simply stops), the return runs to the LAST REAL TRADE and truncated=True:
    observed data over a shorter window, never a fabricated fill. A delisting
    crash therefore counts as the loss it was instead of silently vanishing.
    With no trade at all after the score date, there is nothing observed ->
    (None, True).
    """
    if prices is None or prices.empty:
        return None, False
    prices = prices.sort_index()
    pos = int(prices.index.searchsorted(pd.Timestamp(d), side="right")) - 1
    if pos < 0:
        return None, False
    end = pos + horizon
    truncated = bool(end >= len(prices))
    if truncated:
        end = len(prices) - 1
        if end <= pos:
            return None, True
    p0 = float(prices.iloc[pos])
    p1 = float(prices.iloc[end])
    if p0 <= 0 or not np.isfinite(p0) or not np.isfinite(p1):
        return None, truncated
    return p1 / p0 - 1.0, truncated


def attach_forward_returns(history: pd.DataFrame, price_loader: PriceLoader,
                           horizons: dict[str, int]) -> pd.DataFrame:
    """Add a forward-return column per horizon to the score history.

    history: columns [run_date, ticker, final_score, ...].
    price_loader(ticker) -> close-price Series indexed by date (or None).

    Adds per horizon: fwd_<name> and fwd_<name>_truncated (True = the series
    ended early, so the return runs to the last real trade). Also adds
    price_missing (True = no loadable series at all for that ticker).
    """
    df = history.copy()
    df = df[df["final_score"].notna()]
    price_cache: dict[str, pd.Series | None] = {}

    def prices_for(tk: str):
        if tk not in price_cache:
            try:
                price_cache[tk] = price_loader(tk)
            except Exception as exc:  # noqa: BLE001
                log.warning("price load failed for %s: %s", tk, exc)
                price_cache[tk] = None
        return price_cache[tk]

    for name, hz in horizons.items():
        rets, truncs = [], []
        for tk, d in zip(df["ticker"], df["run_date"]):
            r, tr = _fwd_return(prices_for(tk), d, hz)
            rets.append(r)
            truncs.append(tr)
        df[f"fwd_{name}"] = rets
        df[f"fwd_{name}_truncated"] = truncs
    df["price_missing"] = [
        (p := prices_for(tk)) is None or p.empty for tk in df["ticker"]
    ]
    return df


def decile_table(df: pd.DataFrame, fwd_col: str, n_bins: int = 10) -> pd.DataFrame:
    """Mean forward return by score decile, pooled across dates.

    Deciles are assigned WITHIN each run_date (cross-sectional), then pooled, so
    a date with broadly high/low returns does not bias the decile comparison.
    """
    sub = df[["run_date", "final_score", fwd_col]].dropna()
    if sub.empty:
        return pd.DataFrame(columns=["decile", "mean_return", "median_return", "count"])

    def assign(group: pd.DataFrame) -> pd.Series:
        n = len(group)
        bins = min(n_bins, max(1, n))
        pct = group["final_score"].rank(pct=True, method="first")
        return np.ceil(pct * bins).clip(1, bins).astype(int)

    sub = sub.copy()
    sub["decile"] = (
        sub.groupby("run_date", group_keys=False)
        .apply(assign, include_groups=False)
        if _supports_include_groups()
        else sub.groupby("run_date", group_keys=False).apply(assign)
    )
    out = (sub.groupby("decile")[fwd_col]
           .agg(mean_return="mean", median_return="median", count="count")
           .reset_index())
    return out


def topn_vs_universe(df: pd.DataFrame, fwd_col: str, top_n: int) -> dict:
    """Mean forward return of the top-N (by score, per date) vs the universe mean."""
    sub = df[["run_date", "final_score", fwd_col]].dropna()
    if sub.empty:
        return {"top_n": top_n, "topn_mean": None, "universe_mean": None, "excess": None, "dates": 0}
    top_rets, uni_rets = [], []
    for _, g in sub.groupby("run_date"):
        g = g.sort_values("final_score", ascending=False)
        top_rets.append(g[fwd_col].head(top_n).mean())
        uni_rets.append(g[fwd_col].mean())
    topn_mean = float(np.nanmean(top_rets))
    uni_mean = float(np.nanmean(uni_rets))
    return {"top_n": top_n, "topn_mean": topn_mean, "universe_mean": uni_mean,
            "excess": topn_mean - uni_mean, "dates": int(sub["run_date"].nunique())}


def information_coefficient(df: pd.DataFrame, fwd_col: str) -> float | None:
    """Mean per-date Spearman rank correlation between score and forward return."""
    from scipy.stats import spearmanr
    sub = df[["run_date", "final_score", fwd_col]].dropna()
    ics = []
    for _, g in sub.groupby("run_date"):
        if len(g) >= 5 and g["final_score"].nunique() > 1 and g[fwd_col].nunique() > 1:
            rho, _ = spearmanr(g["final_score"], g[fwd_col])
            if rho == rho:  # not NaN
                ics.append(rho)
    return float(np.mean(ics)) if ics else None


# ------------------------------ orchestration ------------------------------
def _default_price_loader(period: str = "2y") -> PriceLoader:
    def loader(ticker: str):
        import yfinance as yf
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return pd.to_numeric(df["Close"], errors="coerce").dropna()
    return loader


def run_backtest(history: pd.DataFrame, cfg: Config,
                 price_loader: PriceLoader | None = None,
                 horizons: dict[str, int] | None = None) -> BacktestResult:
    """Run the backtest over a score-history DataFrame. Returns a BacktestResult."""
    horizons = horizons or {"1w": 5, "1m": 21}
    result = BacktestResult(horizons=horizons)

    if history is None or history.empty:
        result.summary = ("No score history found. Run the daily pipeline on "
                          "multiple trading days first; the backtest needs the "
                          "history to accrue (fundamentals cannot be backfilled).")
        log.warning(result.summary)
        return result

    history = _dedupe_history(history)
    n_dates = history["run_date"].nunique()
    max_h = max(horizons.values())
    if n_dates < 2:
        result.summary = (f"Only {n_dates} scored date(s) in the DB. Need at least "
                          f"a few dates spanning > {max_h} sessions for meaningful "
                          f"forward returns. This is expected on a fresh install.")
        result.n_dates = n_dates
        log.warning(result.summary)
        return result

    loader = price_loader or _default_price_loader()
    enriched = attach_forward_returns(history, loader, horizons)

    dropped = enriched[enriched["price_missing"]]
    result.dropped_no_price = {
        str(pd.Timestamp(d).date()): int(n)
        for d, n in dropped.groupby("run_date")["ticker"].count().items()
    }

    lines = []
    top_n = cfg.reporting.top_n
    for name, hz in horizons.items():
        col = f"fwd_{name}"
        result.decile_tables[name] = decile_table(enriched, col)
        result.topn_vs_universe[name] = topn_vs_universe(enriched, col, top_n)
        result.info_coefficient[name] = information_coefficient(enriched, col)

        tv = result.topn_vs_universe[name]
        ic = result.info_coefficient[name]
        lines.append(f"[{name}] horizon={hz} sessions")
        if tv["topn_mean"] is not None:
            lines.append(f"   top{top_n} mean fwd return: {tv['topn_mean']:+.2%} | "
                         f"universe: {tv['universe_mean']:+.2%} | "
                         f"excess: {tv['excess']:+.2%} (over {tv['dates']} dates)")
        lines.append(f"   mean rank IC (score vs fwd return): "
                     f"{ic:+.3f}" if ic is not None else "   mean rank IC: n/a")
        n_trunc = int((enriched[col].notna() & enriched[f"{col}_truncated"]).sum())
        if n_trunc:
            lines.append(f"   {n_trunc} return(s) truncated at end of price "
                         f"history (real last-trade returns over a shorter window)")

    total_dropped = sum(result.dropped_no_price.values())
    if total_dropped:
        per_date = ", ".join(f"{d}: {n}" for d, n
                             in sorted(result.dropped_no_price.items()))
        lines.append(f"dropped (no price series loadable): {total_dropped} "
                     f"observation(s) — {per_date}")
    else:
        lines.append("dropped (no price series loadable): 0")

    result.n_dates = int(n_dates)
    result.n_observations = int(enriched[[f"fwd_{n}" for n in horizons]].notna().any(axis=1).sum())
    result.summary = "\n".join(lines)
    log.info("backtest done: %d dates, %d observations", result.n_dates, result.n_observations)
    return result


def write_backtest_report(result: BacktestResult, out_dir: Path) -> Path:
    """Write a simple Markdown backtest report; returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest_{date.today().isoformat()}.md"
    lines = [
        "# Backtest report",
        "",
        "> **This is the only evidence the score means anything.** Until the numbers "
        "below show positive, stable predictive value, treat the system as a research "
        "screen, not a buy signal.",
        "",
        f"- Scored dates in history: **{result.n_dates}**",
        f"- Observations with forward returns: **{result.n_observations}**",
        f"- Dropped (no price series loadable): "
        f"**{sum(result.dropped_no_price.values())}**"
        + (" — " + ", ".join(f"{d}: {n}" for d, n
                             in sorted(result.dropped_no_price.items()))
           if result.dropped_no_price else ""),
        f"- Horizons: {', '.join(f'{k}={v} sessions' for k, v in result.horizons.items())}",
        "",
        "## Summary",
        "",
        "```",
        result.summary or "(no summary)",
        "```",
        "",
    ]
    for name, table in result.decile_tables.items():
        lines.append(f"## Forward return by score decile - {name}")
        lines.append("")
        if table is None or table.empty:
            lines.append("_insufficient data_")
        else:
            t = table.copy()
            t["mean_return"] = (t["mean_return"] * 100).round(2).astype(str) + "%"
            t["median_return"] = (t["median_return"] * 100).round(2).astype(str) + "%"
            lines.append(df_to_markdown(t, index=False))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote backtest report -> %s", path)
    return path


def _supports_include_groups() -> bool:
    """pandas >= 2.2 supports include_groups= on GroupBy.apply."""
    try:
        return tuple(int(x) for x in pd.__version__.split(".")[:2]) >= (2, 2)
    except Exception:  # noqa: BLE001
        return False
