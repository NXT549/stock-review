#!/usr/bin/env python3
"""stock_review_gui.py — desktop launcher for the stock-review tool.

A thin entry point (mirroring ``stock_review.py``) that opens the Tkinter
window instead of running the CLI. This is also the file PyInstaller freezes
into ``Stock Review.exe``.

    py stock_review_gui.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the packaged code importable whether or not it's installed (matches the
# CLI shim). Harmless when frozen: the bundle already has it on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from stock_analyzer.review.gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
