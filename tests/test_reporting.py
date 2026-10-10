"""Smoke tests for the condensed AI-review report (the project's deliverable).

Runs as a plain script (no pytest needed):  python tests/test_reporting.py
Also works under pytest if installed.

These lock the output CONTRACT so it cannot silently regress:
  * the file is ONE Markdown file whose payload is a single CSV block
    (one wide row per ranked stock: identifiers + category scores + percentiles),
  * EVERY scored stock appears (large-universe friendly, one row each),
  * percentile-matrix cells stay within 0-100,
  * there is NO AI/LLM leakage and no stray pandas <NA> / unescaped pipes,
  * `reporting.deep_dive_n` toggles the optional raw-value section.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.config import load_config
from stock_analyzer.metrics import build_metric_frame
from stock_analyzer.pipeline import synthetic_records
from stock_analyzer.scoring import score_universe, rankings_table
from stock_analyzer.reporting import (
    ReportContext, build_stock_views, render_ai_review, generate_reports,
    _scored_metric_keys, _percentile_matrix, _combined_frame,
)
from stock_analyzer.util import df_to_markdown, df_to_csv


def _ctx_and_cfg(limit=None):
    """Build a ReportContext from the offline synthetic fixture (no network/DB)."""
    cfg = load_config()
    records = synthetic_records()
    by_ticker = {r["ticker"]: r for r in records}
    frame, sector = build_metric_frame(records)
    result = score_universe(frame, sector, cfg.scoring)
    ctx = ReportContext(
        run_date=date(2026, 1, 1), universe_name="SYNTHETIC",
        rankings=rankings_table(result),
        views=build_stock_views(by_ticker, frame, result, limit=limit),
        dropped_metrics=result.dropped_metrics, synthetic=True,
    )
    return ctx, cfg, records


def test_single_csv_payload_with_schema():
    ctx, cfg, _ = _ctx_and_cfg()
    md = render_ai_review(ctx, cfg)
    assert "## Columns" in md, "column dictionary missing"
    assert "## Stocks (" in md, "data section missing"
    assert "```csv" in md, "data payload must be a fenced CSV block"
    # The CSV header must carry identifiers + the composite score on one line.
    header = md.split("```csv", 1)[1].splitlines()[1]
    for col in ("rank", "ticker", "sector", "score"):
        assert col in header, f"CSV header missing '{col}': {header}"
    print("OK single CSV payload + column dictionary present")


def test_every_stock_appears():
    ctx, cfg, records = _ctx_and_cfg()
    md = render_ai_review(ctx, cfg)
    for r in records:
        assert r["ticker"] in md, f"missing ticker {r['ticker']}"
    print(f"OK all {len(records)} tickers present in the file")


def test_no_ai_leakage_or_na_artifacts():
    ctx, cfg, _ = _ctx_and_cfg()
    md = render_ai_review(ctx, cfg)
    low = md.lower()
    assert "anthropic" not in low and "claude" not in low, "AI provider leaked into output"
    assert "llm" not in low, "stale LLM reference in output"
    assert "<NA>" not in md, "raw pandas <NA> leaked into a table cell"
    print("OK no AI/LLM leakage and no <NA> artifacts")


def test_percentile_matrix_in_range():
    ctx, cfg, _ = _ctx_and_cfg()
    keys = _scored_metric_keys(ctx)
    m = _percentile_matrix(ctx, keys)
    assert len(m) == len(ctx.views), "matrix must have one row per ranked stock"
    vals = m[keys].to_numpy(dtype="float64")
    finite = vals[~pd.isna(vals)]
    assert (finite >= 0).all() and (finite <= 100).all(), (finite.min(), finite.max())
    print(f"OK percentile matrix in [0,100]: {len(m)} rows x {len(keys)} metrics")


def test_combined_frame_one_row_per_ranked_stock():
    ctx, cfg, _ = _ctx_and_cfg()
    keys = _scored_metric_keys(ctx)
    frame = _combined_frame(ctx, keys)
    assert len(frame) == len(ctx.views), "one row per ranked stock"
    for col in ("rank", "ticker", "score"):
        assert col in frame.columns, f"missing identifier/summary column {col}"
    # Every retained metric column has at least one value (all-empty cols dropped).
    kept = [k for k in keys if k in frame.columns]
    for k in kept:
        assert not frame[k].isna().all(), f"all-empty column {k} should be dropped"
    print(f"OK combined frame: {len(frame)} rows x {len(frame.columns)} cols")


def test_df_to_csv_blanks_na_and_trims_floats():
    df = pd.DataFrame({
        "rank": pd.array([1, 2], dtype="Int64"),
        "ticker": ["AAA", "BBB"],
        "score": [82.7, float("nan")],   # NaN -> blank
        "pct": [100.0, 50.0],            # integral floats -> "100","50" (no .0)
    })
    csv = df_to_csv(df, index=False)
    lines = csv.splitlines()
    assert lines[0] == "rank,ticker,score,pct"
    assert lines[1] == "1,AAA,82.7,100"
    assert lines[2] == "2,BBB,,50", lines[2]   # blank score, no trailing .0
    assert "<NA>" not in csv and "nan" not in csv
    print("OK df_to_csv blanks NA and trims integral floats")


def test_build_views_limit():
    ctx_all, _, _ = _ctx_and_cfg(limit=None)
    n_ranked = int(ctx_all.rankings["final_score"].notna().sum())
    assert len(ctx_all.views) == n_ranked, "limit=None must yield every ranked stock"
    ctx3, _, _ = _ctx_and_cfg(limit=3)
    assert len(ctx3.views) == 3
    print(f"OK build_stock_views: all={n_ranked}, capped=3")


def test_deep_dive_toggle():
    ctx, cfg, _ = _ctx_and_cfg()
    cfg.reporting.deep_dive_n = 0
    assert "deep-dive" not in render_ai_review(ctx, cfg).lower(), "condensed default leaked a deep-dive"
    cfg.reporting.deep_dive_n = 3
    md = render_ai_review(ctx, cfg)
    assert "deep-dive" in md.lower()
    print("OK deep_dive_n toggles the raw-value section (0 -> none, present at 3)")


def test_deep_dive_is_a_second_csv_block_not_markdown_tables():
    """CODE review DESIGN finding: the deep-dive used to render N per-stock
    Markdown pipe tables (verbose, duplicates the main CSV row); it must now
    be a second CSV block, same blank=unavailable convention as the main
    table (no separate 'Unavailable for X: ...' prose line)."""
    ctx, cfg, _ = _ctx_and_cfg()
    cfg.reporting.deep_dive_n = 3
    md = render_ai_review(ctx, cfg)
    assert "### #" not in md, "old per-stock Markdown-table headers must be gone"
    assert "Unavailable for" not in md, "old separate missing-data convention must be gone"
    # exactly two fenced csv blocks: the main table and the deep-dive
    assert md.count("```csv") == 2, md.count("```csv")
    deep_block = md.split("```csv")[2].split("```")[0]
    deep_lines = [l for l in deep_block.splitlines() if l.strip()]
    assert deep_lines[0].split(",")[:3] == ["rank", "ticker", "name"], deep_lines[0]
    assert len(deep_lines) - 1 == 3, "3 header + 3 data rows expected"
    print("OK deep-dive renders as a second CSV block, not N Markdown tables")


def test_overview_header_has_size_sector_currency_context():
    """DESIGN finding: reporting.py's header was two lines (title +
    universe/scored/generated); review/report.py's header computes size/
    sector/currency mix + medians. Ported so both meet the same bar."""
    ctx, cfg, _ = _ctx_and_cfg()
    md = render_ai_review(ctx, cfg)
    header = md.splitlines()[2]  # the universe=... line (+ overview, same line)
    assert "sectors=" in header, header
    assert "median trailing P/E=" in header or "median 6m return=" in header, header
    print(f"OK overview header carries orienting context: {header}")


def test_sparse_rows_note_flags_thin_rows_without_touching_cells():
    """A stock missing most scored metrics gets flagged in a compact note
    instead of the reader having to count commas across a 28+-column CSV
    line; the CSV cells themselves stay genuinely blank either way."""
    import numpy as np
    from stock_analyzer.metrics import REGISTRY
    cfg = load_config()
    cfg.scoring.min_rankable = 1   # a 2-ticker fixture can't hit the default of 3
    tickers = ["FULL", "SPARSE"]
    frame = pd.DataFrame(np.nan, index=tickers,
                         columns=[m.key for m in REGISTRY], dtype="float64")
    # FULL has every fundamentals metric; SPARSE has just one metric overall.
    fundamentals_keys = [m.key for m in REGISTRY if m.category == "fundamentals"]
    for k in fundamentals_keys:
        frame.loc["FULL", k] = 0.1
    frame.loc["SPARSE", "roe"] = 0.1
    sector = pd.Series({t: "Tech" for t in tickers})
    result = score_universe(frame, sector, cfg.scoring)
    by_ticker = {t: {"ticker": t, "sector": "Tech"} for t in tickers}
    ctx = ReportContext(
        run_date=date(2026, 1, 1), universe_name="SPARSETEST",
        rankings=rankings_table(result),
        views=build_stock_views(by_ticker, frame, result),
        dropped_metrics=result.dropped_metrics,
    )
    md = render_ai_review(ctx, cfg)
    assert "Sparse rows" in md, md
    assert "SPARSE" in md.split("Sparse rows")[1].split(".")[0]
    print("OK sparse-rows note flags the thin row; cells stay blank in the CSV")


def test_metric_weight_note_in_columns_section():
    """A category note either states 'weighted equally' (the default) or
    shows each metric's nominal share when config.yaml overrides a weight —
    a reader couldn't previously reconstruct why one metric dominated a
    category score."""
    ctx, cfg, _ = _ctx_and_cfg()
    md_default = render_ai_review(ctx, cfg)
    assert "weighted equally" in md_default, "default (equal) weights should say so"

    cfg.scoring.metric_weights = {"roe": 3.0}
    md_weighted = render_ai_review(ctx, cfg)
    assert "nominal" in md_weighted, "an overridden weight should show a % breakdown"
    print("OK metric-weight note: 'weighted equally' by default, % breakdown when overridden")


def test_how_to_read_explains_comma_runs():
    ctx, cfg, _ = _ctx_and_cfg()
    md = render_ai_review(ctx, cfg)
    assert "consecutive commas" in md or "comma" in md.lower()
    print("OK 'How to read' explains long comma runs are expected, not misaligned")


def test_render_ai_review_handles_zero_ranked():
    """No stock scored today (e.g. every fetch failed) must render a plain
    explanatory section, not crash _combined_frame's merge (CODE-15): an
    empty ctx.views makes _percentile_matrix build a columnless frame, and
    merging that on 'ticker' used to raise a KeyError."""
    import numpy as np
    from stock_analyzer.metrics import REGISTRY
    cfg = load_config()
    tickers = ["AAA", "BBB", "CCC"]
    frame = pd.DataFrame(np.nan, index=tickers,
                         columns=[m.key for m in REGISTRY], dtype="float64")
    sector = pd.Series({t: "Unknown" for t in tickers})
    result = score_universe(frame, sector, cfg.scoring)
    by_ticker = {t: {"ticker": t, "sector": "Unknown"} for t in tickers}
    ctx = ReportContext(
        run_date=date(2026, 1, 1), universe_name="EMPTY",
        rankings=rankings_table(result),
        views=build_stock_views(by_ticker, frame, result),
        dropped_metrics=result.dropped_metrics,
    )
    assert not ctx.views, "fixture must produce zero ranked stocks"
    md = render_ai_review(ctx, cfg)  # must not raise
    assert "No stocks ranked today (0/3)" in md, md
    assert "## Backtest status" in md
    print("OK render_ai_review: zero-ranked universe renders cleanly, no crash")


def test_generate_reports_writes_single_file():
    ctx, cfg, _ = _ctx_and_cfg()
    tmp = Path(tempfile.mkdtemp())
    cfg.reporting.output_dir = str(tmp)
    paths = generate_reports(ctx, cfg)
    assert list(paths) == ["ai_review"], paths
    files = list(tmp.glob("*"))
    assert len(files) == 1 and files[0].suffix == ".md", files
    print(f"OK exactly one file written: {files[0].name}")


def test_df_to_markdown_escapes_pipes_and_blanks_na():
    df = pd.DataFrame({
        "rank": pd.array([1, pd.NA], dtype="Int64"),
        "name": ["ok", "a|b"],
        "score": [70.5, float("nan")],
    })
    md = df_to_markdown(df, index=False)
    assert "<NA>" not in md, "Int64 <NA> must render blank"
    assert r"a\|b" in md, "literal pipe must be escaped"
    assert "| 1 | ok | 70.5 |" in md
    print("OK df_to_markdown escapes pipes and blanks NA/NaN")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} reporting tests...\n")
    for fn in fns:
        fn()
    print("\nALL REPORTING TESTS PASSED")


if __name__ == "__main__":
    _run_all()
