#!/usr/bin/env python
"""Run the backtest over accumulated score history and write a report.

    python scripts/run_backtest.py

Needs several days of stored scores (run_daily.py over time). Forward returns are
pulled from REAL price history; scores cannot be backfilled (fundamentals are
point-in-time only), so history must genuinely accrue.

This is the mechanism for tuning weights: edit scoring.category_weights /
metric_weights in config.yaml, re-run daily for a while, then re-run this.
"""
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from stock_analyzer.config import load_config
from stock_analyzer.logging_setup import setup_logging
from stock_analyzer.backtest import run_backtest, write_backtest_report
from stock_analyzer.storage.db import Storage


def main():
    ap = argparse.ArgumentParser(description="Backtest stored scores vs forward returns")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.resolve(cfg.logging.dir))

    storage = Storage(cfg.storage.db_url)
    storage.init_db()
    history = storage.load_score_history()
    result = run_backtest(history, cfg)

    path = write_backtest_report(result, cfg.resolve(cfg.reporting.output_dir))
    print("\n================ BACKTEST ================")
    print(result.summary)
    print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
