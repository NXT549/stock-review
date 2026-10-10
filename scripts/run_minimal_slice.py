#!/usr/bin/env python
"""MINIMAL WORKING SLICE: collect -> score -> rank -> report on ~20 tickers.

Verify the numbers are sane here BEFORE trusting the full system. Writes a single
AI-review Markdown file you can hand to an AI for review.

  # fully offline, deterministic synthetic fixture (no network):
  python scripts/run_minimal_slice.py --synthetic

  # live data for the 20 tickers in config/universe/sample20.csv:
  python scripts/run_minimal_slice.py
"""
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401  (sys.path shim)

from stock_analyzer.config import load_config
from stock_analyzer.logging_setup import setup_logging
from stock_analyzer.pipeline import run_pipeline, synthetic_records


def main():
    ap = argparse.ArgumentParser(description="Minimal collect->score->rank->report slice")
    ap.add_argument("--synthetic", action="store_true",
                    help="use the offline synthetic fixture (no network)")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.resolve(cfg.logging.dir))

    if args.synthetic:
        records = synthetic_records()
        res = run_pipeline(
            cfg, records=records, synthetic=True,
            db_url_override="sqlite:///database/stock_analyzer_synthetic.db",
            backtest_summary=("Synthetic fixture: no real prices, so no backtest. "
                              "Use real daily runs + scripts/run_backtest.py."),
        )
    else:
        # Live: uses cfg.universe (sample20.csv by default).
        res = run_pipeline(cfg)

    print("\n================ RANKINGS ================")
    with_pd_opts()
    print(res.rankings.to_string())
    print("\nReport written (feed this single file to an AI to review):")
    for fmt, path in res.report_paths.items():
        print(f"  {fmt:9s} {path}")
    print(f"\nrun_id={res.run_id}  tickers={res.n_tickers}")


def with_pd_opts():
    import pandas as pd
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)


if __name__ == "__main__":
    main()
