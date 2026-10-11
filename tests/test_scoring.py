"""Hand-verifiable tests for the deterministic scoring engine.

Runs as a plain script (no pytest needed):  python tests/test_scoring.py
Also works under pytest if installed.

These assert the properties the spec cares about:
  * scoring direction (higher_better vs lower_better) is applied correctly,
  * missing metrics are EXCLUDED and weights renormalised (never zero-filled),
  * a whole missing category redistributes its weight,
  * sector-relative ranking with a small-sector fallback to universe ranking,
  * "Unknown"/NaN sectors always rank universe-wide (never as a peer group),
  * final-rank tie semantics (method='min': 1, 1, 3),
  * determinism (same input -> identical output, twice).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.config import ScoringConfig
from stock_analyzer.metrics import REGISTRY
from stock_analyzer import scoring


def _empty_frame(tickers):
    cols = [m.key for m in REGISTRY]
    return pd.DataFrame(np.nan, index=tickers, columns=cols, dtype="float64")


def _cfg(**over):
    c = ScoringConfig()
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_direction_higher_better():
    """Highest revenue_growth should earn the highest percentile."""
    t = ["A", "B", "C", "D", "E"]
    f = _empty_frame(t)
    f["revenue_growth"] = [0.01, 0.05, 0.10, 0.20, 0.40]
    sec = pd.Series({k: "Tech" for k in t})
    # min_group_size high so we use universe ranking (one sector, 5 names)
    res = scoring.score_universe(f, sec, _cfg(min_group_size=99))
    pct = res.metric_percentiles["revenue_growth"]
    assert pct["E"] == pct.max() and pct["A"] == pct.min(), pct.to_dict()
    print("OK direction higher_better:", pct.round(1).to_dict())


def test_direction_lower_better():
    """Lowest P/E should earn the highest percentile (cheap = good)."""
    t = ["A", "B", "C", "D", "E"]
    f = _empty_frame(t)
    f["pe"] = [10, 15, 20, 30, 50]
    sec = pd.Series({k: "Tech" for k in t})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=99))
    pct = res.metric_percentiles["pe"]
    assert pct["A"] == pct.max() and pct["E"] == pct.min(), pct.to_dict()
    print("OK direction lower_better:", pct.round(1).to_dict())


def test_negative_pe_is_missing():
    """A non-positive P/E must be treated as unavailable, not 'cheapest'."""
    from stock_analyzer.metrics import derive_metric_inputs
    rec = {"ticker": "X", "trailing_pe": -12.0, "price": 100, "market_cap": 1e9}
    d = derive_metric_inputs(rec)
    assert d["pe"] is None, d["pe"]
    print("OK negative P/E -> missing")


def test_negative_debt_to_equity_is_missing():
    """Negative D/E (negative book equity) must not rank as 'least leveraged'."""
    from stock_analyzer.metrics import derive_metric_inputs
    assert derive_metric_inputs({"ticker": "X", "debt_to_equity": -150.0})["debt_to_equity"] is None
    assert derive_metric_inputs({"ticker": "X", "debt_to_equity": 0.0})["debt_to_equity"] == 0.0
    print("OK negative D/E -> missing")


def test_missing_metric_reweights_within_category():
    """A stock missing one fundamental metric is scored on the rest, not zeroed.

    B is missing 'peg'. Its fundamentals score must equal the (renormalised) mean
    of the metrics it DOES have - and must NOT equal the value you'd get by
    treating the missing metric as a 0 percentile. That distinction is the whole
    point of the missing-field rule.
    """
    t = ["A", "B", "C", "D", "E", "F"]
    f = _empty_frame(t)
    f["roe"] = [0.05, 0.05, 0.10, 0.10, 0.20, 0.20]
    f["net_margin"] = [0.05, 0.05, 0.10, 0.10, 0.20, 0.20]
    f["peg"] = [3.0, np.nan, 2.0, np.nan, 1.0, np.nan]  # B,D,F missing peg
    sec = pd.Series({k: "Tech" for k in t})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=99, min_rankable=3))
    cat = res.category_scores["fundamentals"]
    pct = res.metric_percentiles
    cov = res.category_coverage["fundamentals"]

    # B has exactly two available fundamental metrics (roe, net_margin), no peg.
    assert cov["A"] == 3 and cov["B"] == 2, cov.to_dict()
    reweighted = (pct.loc["B", "roe"] + pct.loc["B", "net_margin"]) / 2.0
    zero_filled = (pct.loc["B", "roe"] + pct.loc["B", "net_margin"] + 0.0) / 3.0
    assert abs(cat["B"] - reweighted) < 1e-9, (cat["B"], reweighted)
    assert abs(cat["B"] - zero_filled) > 1.0, "missing must NOT be treated as zero"
    print(f"OK re-weight within category: B={cat['B']:.2f} "
          f"(reweighted={reweighted:.2f}, zero-fill would be {zero_filled:.2f})")


def test_missing_category_redistributes_weight():
    """With sentiment entirely absent, final score == weighted mean of the
    present categories renormalised (sentiment weight redistributed)."""
    t = ["A", "B", "C", "D", "E"]
    f = _empty_frame(t)
    f["roe"] = [0.01, 0.05, 0.10, 0.20, 0.40]          # fundamentals
    f["price_vs_sma50"] = [-0.1, 0.0, 0.05, 0.1, 0.2]  # technicals
    sec = pd.Series({k: "Tech" for k in t})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=99))
    final = res.final["final_score"]
    cats = res.category_scores
    # Recompute expected for one ticker using only present categories.
    w = ScoringConfig().category_weights
    present = ["fundamentals", "technicals"]
    wsum = sum(w[c] for c in present)
    expected_C = sum(cats.loc["C", c] * w[c] for c in present) / wsum
    assert abs(final["C"] - expected_C) < 1e-9, (final["C"], expected_C)
    assert final.notna().all(), "no NaN finals expected here"
    print("OK missing category redistributes weight; final:", final.round(2).to_dict())


def test_sector_relative_with_small_sector_fallback():
    """Big sector ranks within itself; a 1-name sector falls back to universe."""
    t = ["A", "B", "C", "D", "E", "F", "Z"]
    f = _empty_frame(t)
    #            A    B    C    D    E    F    Z
    f["roe"] = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.99]
    sec = pd.Series({"A": "Bank", "B": "Bank", "C": "Bank",
                     "D": "Bank", "E": "Bank", "F": "Bank",
                     "Z": "Lonely"})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=5))
    pct = res.metric_percentiles["roe"]
    # Within the 6-name Bank sector, F has the highest ROE -> top of its sector.
    bank = pct[["A", "B", "C", "D", "E", "F"]]
    assert bank["F"] == bank.max(), bank.to_dict()
    # Z is alone -> falls back to universe ranking; with the highest ROE overall
    # it should score very high there too. Just assert it produced a value.
    assert not np.isnan(pct["Z"]), "lonely sector should fall back to universe"
    print("OK sector-relative + small-sector fallback:",
          {k: round(v, 1) for k, v in pct.to_dict().items()})


def test_unknown_sector_is_not_a_peer_group():
    """Unknown-sector tickers rank universe-wide even when >= min_group_size.

    'Unknown' records missing sector data, not a real peer group. Six Unknown
    tickers must get exactly the percentiles a universe-wide ranking gives
    them (identical to a min_group_size=99 run), and no mediocre Unknown
    ticker may outrank the genuinely best stock just by leading the unknowns.
    """
    t = ["U1", "U2", "U3", "U4", "U5", "U6", "R"]
    f = _empty_frame(t)
    f["roe"] = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 1.00]  # R is best overall
    sec = pd.Series({**{f"U{i}": "Unknown" for i in range(1, 7)}, "R": "Tech"})
    grouped = scoring.score_universe(f, sec, _cfg(min_group_size=5))
    universe = scoring.score_universe(f, sec, _cfg(min_group_size=99))
    pd.testing.assert_series_equal(
        grouped.metric_percentiles["roe"], universe.metric_percentiles["roe"]
    )
    pct = grouped.metric_percentiles["roe"]
    unknowns = pct[[f"U{i}" for i in range(1, 7)]]
    assert pct["R"] == pct.max() and (unknowns < pct["R"]).all(), pct.to_dict()
    print("OK Unknown sector ranks universe-wide:",
          {k: round(v, 1) for k, v in pct.to_dict().items()})


def test_nan_sector_gets_universe_percentile():
    """A literal-NaN sector must still get a universe percentile, not NaN.

    groupby(dropna=True) silently drops NaN sector keys, so before the
    pre-seeding fix these tickers ended the loop with no percentile at all.
    """
    t = ["A", "B", "C", "D", "E", "N"]
    f = _empty_frame(t)
    f["roe"] = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12]  # N is best overall
    sec = pd.Series({"A": "Bank", "B": "Bank", "C": "Bank", "D": "Bank",
                     "E": "Bank", "N": np.nan})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=5))
    pct = res.metric_percentiles["roe"]
    assert not np.isnan(pct["N"]), pct.to_dict()
    assert pct["N"] == pct.max(), pct.to_dict()
    print(f"OK NaN sector falls back to universe percentile: N={pct['N']:.1f}")


def test_final_rank_tie_semantics():
    """An engineered tie for the best final score -> ranks [1, 1, 3]."""
    t = ["A", "B", "C"]
    f = _empty_frame(t)
    f["roe"] = [0.20, 0.20, 0.05]  # A and B tie exactly, C trails
    sec = pd.Series({k: "Tech" for k in t})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=99))
    ranks = res.final["rank"]
    got = [int(ranks[k]) for k in ["A", "B", "C"]]
    assert got == [1, 1, 3], got
    print("OK final-rank ties use method='min':", got)


def test_metric_dropped_when_too_few_points():
    """A metric with < min_rankable non-null values is dropped (not ranked)."""
    t = ["A", "B", "C", "D", "E"]
    f = _empty_frame(t)
    f["roe"] = [0.05, 0.10, 0.15, 0.20, 0.25]
    f["peg"] = [1.0, np.nan, np.nan, np.nan, np.nan]  # only 1 point
    sec = pd.Series({k: "Tech" for k in t})
    res = scoring.score_universe(f, sec, _cfg(min_group_size=99, min_rankable=3))
    assert "peg" in res.dropped_metrics, res.dropped_metrics
    assert "peg" not in res.metric_percentiles.columns
    print("OK metric dropped when too few points:", res.dropped_metrics)


def test_determinism():
    """Identical inputs -> byte-identical scores, across two runs."""
    rng = np.random.default_rng(0)  # only to MANUFACTURE a fixed fixture, not in scoring
    t = [f"T{i}" for i in range(30)]
    f = _empty_frame(t)
    for key in ["revenue_growth", "roe", "pe", "price_vs_sma50", "return_3m",
                "target_upside", "macd_hist"]:
        f[key] = rng.normal(size=len(t))
    sectors = ["Tech", "Bank", "Energy"] * 10
    sec = pd.Series(dict(zip(t, sectors)))
    r1 = scoring.score_universe(f, sec, _cfg())
    r2 = scoring.score_universe(f, sec, _cfg())
    pd.testing.assert_series_equal(r1.final["final_score"], r2.final["final_score"])
    print("OK determinism: two runs identical")


def test_scores_in_range():
    """All produced scores sit within [0, 100]."""
    t = [f"T{i}" for i in range(40)]
    f = _empty_frame(t)
    rng = np.random.default_rng(1)
    for m in REGISTRY[:15]:
        f[m.key] = rng.normal(size=len(t))
    sec = pd.Series(dict(zip(t, (["Tech", "Bank", "Energy", "Health"] * 10))))
    res = scoring.score_universe(f, sec, _cfg())
    fin = res.final["final_score"].dropna()
    assert (fin >= 0).all() and (fin <= 100).all(), (fin.min(), fin.max())
    print(f"OK scores in range: min={fin.min():.1f} max={fin.max():.1f}")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} scoring tests...\n")
    for fn in fns:
        fn()
    print("\nALL SCORING TESTS PASSED")


if __name__ == "__main__":
    _run_all()
