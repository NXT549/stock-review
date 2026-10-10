"""Report generation: ONE token-lean Markdown file, optimised for an AI to review.

There is NO AI/LLM in this project. The deterministic engine scores every stock
0-100; this module serialises the whole ranked universe into a single
self-contained Markdown file (`ai_review_<date>.md`) built to stay small enough
to feed to an AI even for a large universe.

The payload is ONE CSV block - one row per ranked stock carrying everything about
it on a single line:

    rank, ticker, sector, score, <category scores>, cov, <per-metric percentiles>

CSV (not a Markdown pipe table) because it is ~half the tokens for a numeric grid
and LLMs parse it natively. Identifiers appear once (not duplicated across two
tables). Unscored names are collapsed to a one-line note rather than blank rows.
An optional `reporting.deep_dive_n` appends a second, smaller CSV block of raw
(un-percentiled) values for just the top N names (data sanity) — same CSV format
and blank-cell convention as the main table, not a separate Markdown breakdown.

Design notes
  * Pure-Python (only pandas) so it always works in a minimal install -- no
    plotly / kaleido / reportlab, no chart images, no HTML/PDF side-files.
  * The data block is CSV; the surrounding narrative/schema stays Markdown prose.
  * EVERY report carries the disclaimer below (once).
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from .config import Config
from .logging_setup import get_logger
from .metrics import REGISTRY, REGISTRY_BY_KEY, DISPLAY_FIELDS, CATEGORIES
from .scoring import ScoringResult, _metric_weight
from .util import df_to_csv

log = get_logger("reporting")


@dataclass
class ReportContext:
    run_date: date
    universe_name: str
    rankings: pd.DataFrame
    views: list[dict]
    dropped_metrics: list[str] = field(default_factory=list)
    backtest_summary: str | None = None
    synthetic: bool = False     # True when built from the offline synthetic fixture


# --------------------------------------------------------------------------
# Build the structured per-stock "view" the report serialises.
# --------------------------------------------------------------------------
def build_stock_views(records: dict[str, dict], metric_frame: pd.DataFrame,
                      result: ScoringResult, limit: int | None = None) -> list[dict]:
    """One view per ranked stock (highest score first).

    `limit=None` returns every ranked stock; an integer caps the count.
    """
    ranked = result.final[result.final["final_score"].notna()]
    tickers = list(ranked.index if limit is None else ranked.head(limit).index)
    pct = result.metric_percentiles
    views = []
    for ticker in tickers:
        rec = records.get(ticker, {"ticker": ticker})
        metrics = []
        missing = []
        for m in REGISTRY:
            raw = metric_frame.loc[ticker, m.key] if m.key in metric_frame.columns else None
            available = raw is not None and pd.notna(raw)
            percentile = (pct.loc[ticker, m.key]
                          if (m.key in pct.columns and ticker in pct.index) else None)
            if percentile is not None and pd.isna(percentile):
                percentile = None
            if not available:
                missing.append(m.key)
            metrics.append({
                "key": m.key, "label": m.label, "category": m.category,
                "direction": m.direction,
                "raw_value": float(raw) if available else None,
                "percentile": float(percentile) if percentile is not None else None,
                "available": bool(available),
            })
        cat_scores = {c: (float(result.category_scores.loc[ticker, c])
                          if pd.notna(result.category_scores.loc[ticker, c]) else None)
                      for c in CATEGORIES}
        cat_cov = {c: int(result.category_coverage.loc[ticker, c]) for c in CATEGORIES}
        display = {fld: rec.get(fld) for fld in DISPLAY_FIELDS}
        views.append({
            "ticker": ticker,
            "name": rec.get("name", ticker),
            "sector": rec.get("sector", "Unknown"),
            "rank": int(result.final.loc[ticker, "rank"]) if pd.notna(result.final.loc[ticker, "rank"]) else None,
            "final_score": float(result.final.loc[ticker, "final_score"]),
            "category_scores": cat_scores,
            "category_coverage": cat_cov,
            "metrics": metrics,
            "missing_metrics": missing,
            "display": display,
        })
    return views


def _deep_dive_frame(ctx: ReportContext, keys: list[str], n: int) -> pd.DataFrame:
    """Second CSV block: raw (un-percentiled) values for the top N stocks,
    one row each — identifiers + sourced key figures + every scored metric's
    raw value, keyed by the same registry key the Columns dictionary and the
    main percentile table already use. An unavailable metric is a blank
    cell here too (df_to_csv's usual convention), never a dropped row."""
    rows = []
    for v in ctx.views[:n]:
        raw = {m["key"]: m["raw_value"] for m in v["metrics"]}
        d = v["display"]
        row = {
            "rank": v["rank"], "ticker": v["ticker"], "name": v["name"],
            "sector": v["sector"], "score": round(v["final_score"], 1),
            "price": d.get("price"), "market_cap": d.get("market_cap"),
            "eps": d.get("eps"), "beta": d.get("beta"),
            "volatility_annual": d.get("volatility_annual"),
            "max_drawdown_1y": d.get("max_drawdown_1y"),
        }
        for k in keys:
            row[k] = raw.get(k)
        rows.append(row)
    return pd.DataFrame(rows)


def _scored_metric_keys(ctx: ReportContext) -> list[str]:
    """Registry-ordered metric keys that were actually ranked today."""
    dropped = set(ctx.dropped_metrics)
    return [m.key for m in REGISTRY if m.key not in dropped]


def _percentile_matrix(ctx: ReportContext, keys: list[str]) -> pd.DataFrame:
    """One row per stock: its 0-100 percentile for each scored metric (100=best)."""
    rows = []
    for v in ctx.views:
        pct = {m["key"]: m["percentile"] for m in v["metrics"]}
        row: dict = {"rank": v["rank"], "ticker": v["ticker"]}
        for k in keys:
            p = pct.get(k)
            row[k] = round(p) if p is not None else None
        rows.append(row)
    return pd.DataFrame(rows)


def _combined_frame(ctx: ReportContext, keys: list[str]) -> pd.DataFrame:
    """One wide row per RANKED stock: identifiers + category scores + percentiles.

    Merges the ranking summary (rank/score/category scores/coverage) with the
    per-metric percentile matrix so each stock is a single CSV line. Columns that
    are entirely empty for today's universe (e.g. the sentiment category when no
    provider is configured) are dropped so they cost nothing.
    """
    pm = _percentile_matrix(ctx, keys)  # rank, ticker, <metric keys>
    rk = ctx.rankings.reset_index().rename(
        columns={"final_score": "score", "total_coverage": "cov"})
    summary_cols = [c for c in ["ticker", "sector", "score", *CATEGORIES, "cov"]
                    if c in rk.columns]
    merged = pm.merge(rk[summary_cols], on="ticker", how="left")

    lead = [c for c in ["rank", "ticker", "sector", "score", *CATEGORIES, "cov"]
            if c in merged.columns]
    merged = merged[lead + [c for c in merged.columns if c not in lead]]

    id_cols = {"rank", "ticker", "sector"}
    empty = [c for c in merged.columns if c not in id_cols and merged[c].isna().all()]
    return merged.drop(columns=empty)


_SIZE_ORDER = ["Mega", "Large", "Mid", "Small", "Micro"]


def _size_bucket(usd_market_cap: float | None) -> str | None:
    """Same thresholds as review/format.py's size_bucket, so both report
    generators classify size consistently."""
    if usd_market_cap is None:
        return None
    v = usd_market_cap
    if v >= 200e9:
        return "Mega"
    if v >= 10e9:
        return "Large"
    if v >= 2e9:
        return "Mid"
    if v >= 300e6:
        return "Small"
    return "Micro"


def _overview_lines(ctx: ReportContext) -> list[str]:
    """Orienting context ported from review/format.py's overview line (size/
    sector/currency mix, set medians) — this generator's header used to be
    just a title and a scored-count, the thinnest header of the project's two
    report generators despite serving the flagship full-universe run.

    Ported, not shared code: this pipeline collects no ret_1y (only up to
    6m) and doesn't FX-normalise market_cap the way review/cli.py does, so a
    size-bucket breakdown is only shown for a single-currency universe —
    bucketing native-currency caps from a mixed-currency universe together
    would misrepresent relative size.
    """
    views = ctx.views
    sectors = sorted({v["sector"] for v in views
                      if v.get("sector") and v["sector"] != "Unknown"})
    currencies = sorted({v["display"].get("currency") for v in views
                         if v["display"].get("currency")})
    pes = [m["raw_value"] for v in views for m in v["metrics"]
           if m["key"] == "pe" and m["available"] and m["raw_value"] > 0]
    ret6 = [m["raw_value"] for v in views for m in v["metrics"]
            if m["key"] == "return_6m" and m["available"]]

    lines: list[str] = []
    if sectors:
        lines.append(f"sectors={', '.join(sectors)}")
    if len(currencies) == 1:
        caps = [v["display"].get("market_cap") for v in views
                if v["display"].get("market_cap") is not None]
        counts = Counter(b for c in caps if (b := _size_bucket(c)) is not None)
        size_line = ", ".join(f"{b} {counts[b]}" for b in _SIZE_ORDER if counts.get(b))
        if size_line:
            lines.append(f"by size={size_line} (market cap, {currencies[0]})")
    elif currencies:
        lines.append(f"currencies={', '.join(currencies)} (market caps NOT "
                     "FX-normalised - native currency, not comparable across names)")
    if pes:
        lines.append(f"median trailing P/E={statistics.median(pes):.1f}")
    if ret6:
        lines.append(f"median 6m return={statistics.median(ret6) * 100:+.1f}%")
    return lines


def _sparse_rows_note(ctx: ReportContext, keys: list[str], threshold: float = 0.5) -> str | None:
    """Compact one-line summary of stocks missing a large share of the scored
    metrics — a human "skimming top-to-bottom" (the report's own stated
    first-pass reader) can't reliably count commas across a 28+-column CSV
    line to tell a genuinely sparse row from a misaligned one. Cells stay
    genuinely blank either way (never zero-filled); this is a legibility
    note, not a change to the data."""
    scored = set(keys)
    if not scored:
        return None
    sparse = []
    for v in ctx.views:
        missing_scored = [k for k in v["missing_metrics"] if k in scored]
        if len(missing_scored) / len(scored) >= threshold:
            sparse.append((v["ticker"], len(missing_scored), len(scored)))
    if not sparse:
        return None
    bits = ", ".join(f"{t} ({m}/{n} missing)" for t, m, n in sparse)
    return (f"_Sparse rows ({int(threshold * 100)}%+ of scored metrics missing "
            f"for this stock, long comma runs expected): {bits}._")


# --------------------------------------------------------------------------
# The single, CONDENSED AI-review Markdown file.
# --------------------------------------------------------------------------
def render_ai_review(ctx: ReportContext, cfg: Config) -> str:
    L: list[str] = []
    rankings = ctx.rankings
    n_total = len(rankings)
    n_ranked = (int(rankings["final_score"].notna().sum())
                if "final_score" in rankings.columns else n_total)
    cw = cfg.scoring.category_weights
    keys = _scored_metric_keys(ctx)

    title_tag = " (SYNTHETIC SAMPLE DATA)" if ctx.synthetic else ""
    header_line = (f"universe={ctx.universe_name} | scored={n_ranked}/{n_total} | "
                   f"generated={date.today().isoformat()}")
    overview = _overview_lines(ctx)
    if overview:
        header_line += " | " + " | ".join(overview)
    L += [f"# Stock Analyzer - AI Review - {ctx.run_date.isoformat()}{title_tag}", "",
          header_line, ""]
    if ctx.synthetic:
        L += ["> SYNTHETIC fixture data - fabricated inputs for a deterministic demo, "
              "NOT real market data.", ""]
    L += ["> NOT FINANCIAL ADVICE. Deterministic same-day cross-sectional ranking of "
          "the configured universe - no AI model produced any number. A high score is "
          "a relative rank, not a forecast or buy signal, and is unproven pending the "
          "backtest. Data is best-effort from yfinance (unofficial); missing fields are "
          "blank, never fabricated.", ""]

    if not ctx.views:
        # Nothing ranked: _percentile_matrix would build a columnless frame and
        # the merge in _combined_frame (on "ticker") would crash with a
        # KeyError. An empty universe/all-failed-collection day is a real
        # outcome, not a bug -- say so plainly instead of raising.
        L += ["## Stocks", "",
              f"_No stocks ranked today (0/{n_total}) - every ticker was either "
              "unavailable or had too little data to score. Check the run's "
              "errors log for the collection failures._", "",
              "## Backtest status", "",
              ctx.backtest_summary or
              "_No backtest run yet. Accumulate daily runs, then run "
              "scripts/run_backtest.py._", ""]
        return "\n".join(L)

    frame = _combined_frame(ctx, keys)

    # --- How to read + scoring method (tight) ---
    L += ["## How to read", "",
          "One CSV row per stock, ranked best first. Every score/percentile column is "
          "0-100 where **higher is always better** (each metric's direction is already "
          "baked in); **blank = data unavailable**. A row with many missing metrics for "
          "this stock shows a long run of bare consecutive commas - that's expected, not "
          "a misaligned row (see the sparse-row note below the table for anything "
          "unusually thin). Reason ONLY from these numbers.", "",
          "**Your job:** name the strongest and weakest stocks and explain *why* from "
          "the columns; flag likely data bugs (e.g. a top-ranked name with a glaring "
          "weak metric, or contradictory columns); note where missing data weakens a "
          "conclusion. This is a research screen, not advice.", "",
          "**score** = weighted blend of the category scores ("
          + ", ".join(f"{k} {float(v):.0%}" for k, v in cw.items())
          + "). Fundamentals are ranked within GICS sector; technicals/momentum/analyst "
          "across the whole universe. Missing metrics are dropped and the weights "
          "renormalised (never zero-filled).", ""]

    # --- Column dictionary (matches the CSV header exactly) ---
    cat_cols = [c for c in CATEGORIES if c in frame.columns]
    L += ["## Columns", "",
          "`rank,ticker,sector` identify the stock. `score` is the composite 0-100. "
          + (f"Category scores 0-100: {', '.join(f'`{c}`' for c in cat_cols)}. "
             if cat_cols else "")
          + "`cov` = number of metrics with data. The remaining columns are each "
          "metric's 0-100 percentile (100 = best in its ranking group):", ""]
    # Per-metric weights only lived in config.yaml before this - a reader
    # couldn't reconstruct why e.g. a middling fundamentals score coexists
    # with one standout metric. Show each metric's share of its category's
    # weight (never a real percentage per stock, since compute_category_scores
    # renormalises over each stock's AVAILABLE metrics - this is the nominal
    # split); skip it for a category whose metrics are all weighted equally
    # (the common/default case) to keep the note compact.
    for cat in CATEGORIES:
        cat_keys = [k for k in keys if k in frame.columns
                    and REGISTRY_BY_KEY[k].category == cat]
        if cat_keys:
            weights = {k: _metric_weight(k, cfg.scoring) for k in cat_keys}
            wsum = sum(weights.values()) or 1.0
            if len(set(weights.values())) <= 1:
                legend = "; ".join(f"`{k}`={REGISTRY_BY_KEY[k].label}" for k in cat_keys)
                L += [f"- **{cat}** (each metric weighted equally): {legend}"]
            else:
                legend = "; ".join(
                    f"`{k}`={REGISTRY_BY_KEY[k].label} ({weights[k] / wsum:.0%} nominal)"
                    for k in cat_keys)
                L += [f"- **{cat}:** {legend}"]
    if ctx.dropped_metrics:
        L += ["", f"_Metrics dropped today (too few data points to rank): "
              f"{', '.join(ctx.dropped_metrics)}._"]
    L += [""]

    # --- The data: a single CSV block (one row per ranked stock) ---
    L += [f"## Stocks ({len(frame)} ranked, CSV)", "",
          "```csv", df_to_csv(frame, index=False), "```", ""]

    sparse_note = _sparse_rows_note(ctx, keys)
    if sparse_note:
        L += [sparse_note, ""]

    # Unscored names collapsed to one line (insufficient data to rank) - cheaper
    # and clearer than carrying near-empty rows in the CSV above.
    if "final_score" in rankings.columns:
        unscored = [str(t) for t in rankings.index[rankings["final_score"].isna()]]
        if unscored:
            L += [f"_Unscored ({len(unscored)}, insufficient data): "
                  f"{', '.join(unscored)}._", ""]

    # --- Optional raw-value deep-dive for the top N names ---
    # A second, smaller CSV block (this module's own docstring justifies CSV
    # over Markdown specifically because it's "roughly half the tokens") that
    # keeps the SAME blank=unavailable convention as the main table above,
    # instead of the old per-stock Markdown tables, which dropped unavailable
    # metrics as rows entirely and appended a separate prose line for them —
    # a second, unflagged convention right after a reader had just learned
    # the first one.
    n_deep = max(0, int(cfg.reporting.deep_dive_n))
    if n_deep and ctx.views:
        deep_frame = _deep_dive_frame(ctx, keys, n_deep)
        L += [f"## Top {len(deep_frame)} deep-dive (raw sourced values, CSV)", "",
              "Same blank = unavailable convention as the table above; see "
              "**Columns** above for what each metric key after `max_drawdown_1y` means.",
              "", "```csv", df_to_csv(deep_frame, index=False), "```", ""]

    # --- Backtest status ---
    L += ["## Backtest status", "",
          ctx.backtest_summary or
          "_No backtest run yet. Accumulate daily runs, then run "
          "scripts/run_backtest.py._", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------
# Orchestration -- writes exactly one file.
# --------------------------------------------------------------------------
def generate_reports(ctx: ReportContext, cfg: Config) -> dict[str, str]:
    out_dir = cfg.resolve(cfg.reporting.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"ai_review_{ctx.run_date.isoformat()}"
    if ctx.synthetic:
        tag += "_synthetic"

    p = out_dir / f"{tag}.md"
    p.write_text(render_ai_review(ctx, cfg), encoding="utf-8")
    log.info("wrote %s", p)
    return {"ai_review": str(p)}
