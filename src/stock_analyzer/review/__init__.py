"""stock_review — fetch a watchlist and write ONE clean Markdown file for an AI.

Folded into the stock_analyzer package (was the standalone stock_review.py) and
split into: symbols, format, fetch, universe, report, cli. Entry point: main()."""
from __future__ import annotations

from .cli import main

__all__ = ["main"]
