"""Optional, clearly-named sentiment/flow providers.

DATA-SOURCE HONESTY: free yfinance does NOT reliably provide news sentiment,
social sentiment, or institutional flow. Rather than fabricate these, we leave
them DISABLED by default. If you have a paid key, enable the matching flag in
config.yaml -> optional_providers and implement the fetch below. Until then the
fields are recorded as unavailable and excluded from scoring (the 'sentiment'
category weight is redistributed - see scoring.py).
"""
from __future__ import annotations

import os

from .logging_setup import get_logger

log = get_logger("sentiment")

_warned_malformed: set[str] = set()


def _subsection(cfg_optional: dict, key: str) -> dict:
    """optional_providers[key] as a dict, tolerating a malformed child (e.g. a
    list) that a well-formed parent mapping doesn't rule out. Warned once per
    key so a bad config doesn't spam the log every ticker."""
    val = (cfg_optional or {}).get(key) or {}
    if not isinstance(val, dict):
        if key not in _warned_malformed:
            log.warning("optional_providers.%s must be a mapping, got %s; "
                        "treating as disabled", key, type(val).__name__)
            _warned_malformed.add(key)
        return {}
    return val


def fetch_sentiment(ticker: str, cfg_optional: dict) -> dict[str, float | None]:
    """Return {'news_sentiment': float|None, 'social_sentiment': float|None}.

    Reads optional_providers config. When disabled (the default) returns Nones.
    """
    result: dict[str, float | None] = {"news_sentiment": None, "social_sentiment": None}

    news = _subsection(cfg_optional, "news_sentiment")
    social = _subsection(cfg_optional, "social_sentiment")

    if news.get("enabled"):
        result["news_sentiment"] = _fetch_news_sentiment(ticker, news.get("provider", "none"))
    if social.get("enabled"):
        result["social_sentiment"] = _fetch_social_sentiment(ticker, social.get("provider", "none"))

    return result


def _fetch_news_sentiment(ticker: str, provider: str) -> float | None:
    # STUB: not implemented for v1. We refuse to invent a number. To wire a real
    # provider (e.g. Alpha Vantage NEWS_SENTIMENT, Finnhub news-sentiment), call
    # it here with the key from the environment and return a normalised score in
    # [-1, +1]. Returning None keeps the field honestly 'unavailable'.
    key = os.getenv("ALPHAVANTAGE_API_KEY") or os.getenv("FINNHUB_API_KEY")
    log.warning(
        "news_sentiment enabled (provider=%s) for %s but no implementation is "
        "shipped in v1; recording as unavailable. key_present=%s",
        provider, ticker, bool(key),
    )
    return None


def _fetch_social_sentiment(ticker: str, provider: str) -> float | None:
    # STUB: see _fetch_news_sentiment. No free, reliable source; not implemented.
    log.warning(
        "social_sentiment enabled (provider=%s) for %s but no implementation is "
        "shipped in v1; recording as unavailable.",
        provider, ticker,
    )
    return None
