#!/usr/bin/env python
"""Daily run entry point (full universe from config).

One-shot (what an OS scheduler should call after market close):
    python scripts/run_daily.py

Run as a long-lived in-process scheduler instead (uses the `schedule` lib):
    python scripts/run_daily.py --schedule 16:30
(See scheduling/ for OS-level Task Scheduler / cron / Docker examples, and note
the multi-market timing caveat in the README.)
"""
from __future__ import annotations

import argparse
import time

import _bootstrap  # noqa: F401

from stock_analyzer.config import load_config
from stock_analyzer.logging_setup import setup_logging, get_logger
from stock_analyzer.pipeline import run_pipeline

log = get_logger("run_daily")


def run_once(config_path: str | None):
    cfg = load_config(config_path)
    setup_logging(cfg.logging.level, cfg.resolve(cfg.logging.dir))
    log.info("=== daily run start (universe=%s) ===", cfg.universe.source)
    try:
        res = run_pipeline(cfg)
        log.info("=== daily run done: run_id=%s, %d tickers, reports=%s ===",
                 res.run_id, res.n_tickers, list(res.report_paths))
        return res
    except Exception:  # noqa: BLE001
        log.exception("daily run FAILED")
        raise


def main():
    ap = argparse.ArgumentParser(description="Daily stock-analyzer run")
    ap.add_argument("--config", default=None)
    ap.add_argument("--schedule", metavar="HH:MM", default=None,
                    help="run every day at this local time in-process (blocks)")
    args = ap.parse_args()

    if not args.schedule:
        run_once(args.config)
        return

    import schedule
    # Load the real config BEFORE the first setup_logging() call: it's
    # idempotent, so a placeholder call here would permanently shadow the
    # config-driven level/dir every run_once() below tries to set.
    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.resolve(cfg.logging.dir))
    log.info("scheduling daily run at %s (local time). Ctrl-C to stop.", args.schedule)
    schedule.every().day.at(args.schedule).do(run_once, args.config)
    while True:
        # A failed daily run must NOT kill the long-lived scheduler: log it and
        # keep going so tomorrow's run still fires. (run_once still re-raises for
        # the one-shot OS-scheduler path, where a non-zero exit is the right thing.)
        try:
            schedule.run_pending()
        except Exception:  # noqa: BLE001
            log.exception("scheduled run crashed; keeping the scheduler alive")
        time.sleep(30)


if __name__ == "__main__":
    main()
