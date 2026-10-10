"""Stock Analyzer - deterministic 0-100 stock scoring.

Design rule (read src/stock_analyzer/scoring.py): scoring and ranking are 100%
deterministic Python - same data in, same scores and ranks out. There is NO AI
or LLM anywhere in this project. The pipeline writes a single clean Markdown file
(ai_review_<date>.md) listing every stock with its 0-100 score and per-metric
breakdown, designed to be handed to an AI of your choice for qualitative review.
"""

__version__ = "2.0.0"
