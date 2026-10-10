#!/usr/bin/env python3
"""stock_review.py — thin compatibility shim.

The tool used to live entirely in this file; it now lives in the
``stock_analyzer.review`` package (split into symbols / format / fetch / universe /
report / cli). This shim keeps the original entry point working unchanged:

    py stock_review.py --source japan
    py stock_review.py AAPL MSFT NVDA

It also re-exports the package's full flat namespace, so ``import stock_review``
still exposes ``clean_symbol``, ``build_markdown``, ``fetch_one``, ``main``, etc.
directly (used by the test-suite and any existing importers). New code should
import from ``stock_analyzer.review`` instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the packaged code importable whether or not it's installed.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from stock_analyzer.review import (  # noqa: E402
    cli, fetch, format, report, symbols, universe,
)
from stock_analyzer.review.cli import main  # noqa: E402,F401

# Flatten every public + private name from the submodules into this module, so the
# old single-file namespace (stock_review.<anything>) keeps resolving. Dunders are
# skipped so we never clobber this module's own __name__/__doc__/etc.
for _mod in (symbols, format, fetch, universe, report, cli):
    for _k, _v in vars(_mod).items():
        if not _k.startswith("__"):
            globals()[_k] = _v
del _mod, _k, _v

if __name__ == "__main__":
    raise SystemExit(main())
