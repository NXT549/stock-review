#!/usr/bin/env python3
"""stock_review_board.py — launcher for the dark "Live board" window.

Same engine as ``stock_review_gui.py``, different front-end (see
``src/stock_analyzer/review/gui_board.py``).

    py stock_review_board.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from stock_analyzer.review.gui_board import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
