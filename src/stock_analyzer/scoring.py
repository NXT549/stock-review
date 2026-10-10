"""Deterministic cross-sectional scoring engine.

=============================================================================
DETERMINISM CONTRACT
  Same input frame -> same percentiles -> same category scores -> same final
  scores -> same ranking. There is no randomness, no time-dependence, and no
  LLM anywhere in this module. All ranking uses pandas' deterministic rank():
  metric percentiles use method='average' (ties share the mean percentile);
  the final display rank uses method='min' (a tie for best is 1, 1, 3 — ranks
  stay whole numbers).
=============================================================================

Pipeline per day, for the WHOLE universe at once:

  1. For each metric, winsorize raw values to [winsor_lower, winsor_upper]
     quantiles, then convert to a 0-100 cross-sectional PERCENTILE.
       - direction higher_better -> ascending rank  (biggest value -> ~100)
       - direction lower_better  -> descending rank  (smallest value -> ~100)
     Missing values stay NaN (excluded; never 0).
       - Sector-relative metrics rank WITHIN sector. A sector with fewer than
         `min_group_size` non-null members falls back to universe-wide ranking
         for those tickers (avoids a lone stock auto-scoring 100).
       - Tickers whose sector is UNKNOWN_SECTOR (or literally NaN) ALWAYS rank
         universe-wide, no matter how many there are: "Unknown" marks missing
         sector data, not a comparable peer group.
       - A metric with fewer than `min_rankable` non-null values across the
         whole universe is dropped for the day (too few points to rank).

  2. Category score = weighted mean of that category's AVAILABLE metric
     percentiles, with weights renormalised over the available metrics. A
     category with no available metric for a stock -> NaN.

  3. Final 0-100 = weighted sum of AVAILABLE category scores, with category
     weights renormalised over the categories that stock actually has. So a
     missing 'sentiment' category pushes its weight onto the others rather than
     silently scoring the stock 0 there.

NOTE on winsorizing + rank: percentile rank is invariant to monotonic
transforms, so clipping mainly collapses extreme outliers into ties at the
boundary. We still clip (a) because the spec asks for it and (b) so the same
config generalises if you swap rank for a z-score scaler.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import REGISTRY, REGISTRY_BY_KEY, CATEGORIES, MetricDef, UNKNOWN_SECTOR


@dataclass
class ScoringResult:
    metric_percentiles: pd.DataFrame   # ticker x metric_key, 0-100 (NaN = unavailable)
    category_scores: pd.DataFrame      # ticker x category, 0-100 (NaN = no data)
    category_coverage: pd.DataFrame    # ticker x category, int = #metrics used
    final: pd.DataFrame                # ticker-indexed: final_score, rank, categories, coverage
    dropped_metrics: list[str]         # metrics dropped universe-wide (too few points)


def _winsorized_percentile(values: pd.Series, direction: str,
                           winsor_lower: float, winsor_upper: float) -> pd.Series:
    """0-100 percentile of `values` after winsorizing. NaNs preserved."""
    s = values.dropna().astype("float64")
    if s.empty:
        return pd.Series(index=values.index, dtype="float64")
    lo = s.quantile(winsor_lower)
    hi = s.quantile(winsor_upper)
    clipped = s.clip(lower=lo, upper=hi)
    ascending = direction == "higher_better"
    pct = clipped.rank(pct=True, ascending=ascending, method="average") * 100.0
    return pct.reindex(values.index)


def _rank_one_metric(values: pd.Series, sector: pd.Series, mdef: MetricDef,
                     cfg) -> pd.Series:
    """Percentile-rank a single metric across the universe (with sector logic)."""
    universe_pct = _winsorized_percentile(
        values, mdef.direction, cfg.winsor_lower, cfg.winsor_upper
    )
    if not mdef.sector_relative:
        return universe_pct

    # Sector-relative: rank within each sufficiently-large sector. Everything
    # else keeps the pre-seeded universe percentile: small sectors, tickers
    # whose sector is literally NaN (groupby drops them), and the whole
    # UNKNOWN_SECTOR group — "Unknown" marks missing sector data, so its
    # members must never be ranked against each other as if they were peers.
    result = universe_pct.copy()
    for sec, idx in sector.groupby(sector).groups.items():
        if sec == UNKNOWN_SECTOR:
            continue
        grp_vals = values.loc[idx]
        if int(grp_vals.notna().sum()) >= cfg.min_group_size:
            result.loc[idx] = _winsorized_percentile(
                grp_vals, mdef.direction, cfg.winsor_lower, cfg.winsor_upper
            )
    return result


def compute_metric_percentiles(metric_frame: pd.DataFrame, sector: pd.Series,
                               cfg) -> tuple[pd.DataFrame, list[str]]:
    """Return (ticker x metric) percentile frame + list of dropped metric keys."""
    cols: dict[str, pd.Series] = {}
    dropped: list[str] = []
    for mdef in REGISTRY:
        if mdef.key not in metric_frame.columns:
            dropped.append(mdef.key)
            continue
        values = metric_frame[mdef.key]
        if int(values.notna().sum()) < cfg.min_rankable:
            dropped.append(mdef.key)
            continue
        cols[mdef.key] = _rank_one_metric(values, sector, mdef, cfg)
    pct = pd.DataFrame(cols, index=metric_frame.index)
    # Preserve a stable column order matching the registry.
    ordered = [m.key for m in REGISTRY if m.key in pct.columns]
    return pct[ordered], dropped


def _metric_weight(key: str, cfg) -> float:
    """Per-metric weight: config override, else the registry default."""
    override = (cfg.metric_weights or {}).get(key)
    if override is not None:
        return float(override)
    return float(REGISTRY_BY_KEY[key].weight)


def compute_category_scores(metric_pct: pd.DataFrame, cfg
                            ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate metric percentiles into per-category 0-100 scores.

    Returns (category_scores, coverage_counts). For each (ticker, category) the
    score is the weighted mean over AVAILABLE metrics, weights renormalised over
    what is present. coverage_counts records how many metrics fed each cell.
    """
    cat_scores = pd.DataFrame(index=metric_pct.index, columns=CATEGORIES, dtype="float64")
    coverage = pd.DataFrame(0, index=metric_pct.index, columns=CATEGORIES, dtype="int64")

    for cat in CATEGORIES:
        keys = [m.key for m in REGISTRY if m.category == cat and m.key in metric_pct.columns]
        if not keys:
            continue
        sub = metric_pct[keys]
        weights = np.array([_metric_weight(k, cfg) for k in keys], dtype="float64")
        # Per-row weighted mean ignoring NaNs (renormalise weights over present).
        mask = sub.notna().to_numpy()
        vals = sub.to_numpy(dtype="float64")
        w = np.where(mask, weights[None, :], 0.0)
        wsum = w.sum(axis=1)
        weighted = np.where(mask, vals * w, 0.0).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            score = np.where(wsum > 0, weighted / wsum, np.nan)
        cat_scores[cat] = score
        coverage[cat] = mask.sum(axis=1)

    return cat_scores, coverage


def compute_final_scores(cat_scores: pd.DataFrame, cfg) -> pd.Series:
    """Combine category scores into a final 0-100 (renormalised category weights)."""
    weights = cfg.category_weights
    cats = [c for c in CATEGORIES if c in cat_scores.columns]
    w = np.array([float(weights.get(c, 0.0)) for c in cats], dtype="float64")
    vals = cat_scores[cats].to_numpy(dtype="float64")
    mask = ~np.isnan(vals)
    wmat = np.where(mask, w[None, :], 0.0)
    wsum = wmat.sum(axis=1)
    weighted = np.where(mask, vals * wmat, 0.0).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        final = np.where(wsum > 0, weighted / wsum, np.nan)
    return pd.Series(final, index=cat_scores.index, name="final_score")


def score_universe(metric_frame: pd.DataFrame, sector: pd.Series, cfg) -> ScoringResult:
    """Full deterministic scoring for one universe snapshot."""
    metric_pct, dropped = compute_metric_percentiles(metric_frame, sector, cfg)
    cat_scores, coverage = compute_category_scores(metric_pct, cfg)
    final_score = compute_final_scores(cat_scores, cfg)

    final = cat_scores.copy()
    final.insert(0, "final_score", final_score)
    final["total_coverage"] = coverage.sum(axis=1)
    final["sector"] = sector.reindex(final.index)

    # Rank: highest score = rank 1. Stocks with no score at all are unranked.
    final = final.sort_values("final_score", ascending=False, na_position="last")
    ranks = final["final_score"].rank(ascending=False, method="min")
    final["rank"] = ranks.astype("Int64")  # nullable int; NaN score -> <NA> rank

    return ScoringResult(
        metric_percentiles=metric_pct,
        category_scores=cat_scores,
        category_coverage=coverage,
        final=final,
        dropped_metrics=dropped,
    )


def rankings_table(result: ScoringResult, round_to: int = 1) -> pd.DataFrame:
    """Human-facing ranked table: Ticker | Score | per-category breakdown."""
    cols = ["rank", "final_score"] + CATEGORIES + ["sector", "total_coverage"]
    cols = [c for c in cols if c in result.final.columns]
    out = result.final[cols].copy()
    num_cols = ["final_score"] + [c for c in CATEGORIES if c in out.columns]
    out[num_cols] = out[num_cols].round(round_to)
    out.index.name = "ticker"
    return out
