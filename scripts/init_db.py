#!/usr/bin/env python
"""Create the database schema (idempotent).

    python scripts/init_db.py
"""
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from stock_analyzer.config import load_config
from stock_analyzer.logging_setup import setup_logging
from stock_analyzer.storage.db import Storage, _safe_url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.logging.level, cfg.resolve(cfg.logging.dir))
    storage = Storage(cfg.storage.db_url)
    storage.init_db()
    print(f"Schema initialised at: {_safe_url(cfg.storage.db_url)}")


if __name__ == "__main__":
    main()
