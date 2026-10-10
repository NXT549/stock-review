"""Offline tests for config loading and logging setup (CODE-12, CODE-27).

Run:  python tests/test_config.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.config import load_config
from stock_analyzer import logging_setup
from stock_analyzer.sentiment_providers import fetch_sentiment, _subsection


def _write_cfg(tmp: Path, optional_providers) -> Path:
    """A minimal but complete config.yaml with a given optional_providers value."""
    data = {
        "universe": {}, "markets": {}, "data": {}, "scoring": {},
        "reporting": {}, "storage": {}, "logging": {},
        "optional_providers": optional_providers,
    }
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_optional_providers_malformed_raises_at_startup():
    """CODE-12: optional_providers now goes through _section(), so a
    malformed value (a list, not a mapping) fails fast at load_config()
    instead of surfacing downstream as an AttributeError deep in
    sentiment_providers."""
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_cfg(Path(tmp), ["not", "a", "mapping"])
        try:
            load_config(p)
            raise AssertionError("expected ValueError for malformed optional_providers")
        except ValueError as exc:
            assert "optional_providers" in str(exc), exc
    print("OK malformed optional_providers -> ValueError at load_config()")


def test_optional_providers_well_formed_loads():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_cfg(Path(tmp), {"news_sentiment": {"enabled": False}})
        cfg = load_config(p)
        assert cfg.optional_providers == {"news_sentiment": {"enabled": False}}
    print("OK well-formed optional_providers loads unchanged")


def test_sentiment_tolerates_malformed_child_section():
    """A well-formed optional_providers dict with a malformed CHILD (e.g. a
    list instead of a mapping for news_sentiment) must degrade to disabled,
    not raise AttributeError on .get('enabled')."""
    assert _subsection({"news_sentiment": ["oops"]}, "news_sentiment") == {}
    result = fetch_sentiment("AAA", {"news_sentiment": ["oops"], "social_sentiment": None})
    assert result == {"news_sentiment": None, "social_sentiment": None}
    print("OK malformed optional_providers child tolerated, not raised")


def test_setup_logging_idempotent_warns_on_conflicting_args():
    """CODE-27: setup_logging is idempotent; a SECOND call with DIFFERENT
    args must not silently apply (or silently no-op) — it should warn so a
    placeholder-then-real-config double-call is visible instead of silently
    losing the real config forever."""
    logging_setup._CONFIGURED = False
    logging_setup._FIRST_CALL_ARGS = None
    logger = logging.getLogger("stock_analyzer")
    warnings: list[str] = []

    class _Catch(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    catch = _Catch()
    logger.addHandler(catch)
    tmp = Path(tempfile.mkdtemp())
    try:
        logging_setup.setup_logging("INFO", tmp / "a")   # first call: placeholder-ish
        logging_setup.setup_logging("DEBUG", tmp / "b")  # second call, different args
        assert any("ignored" in w and "already configured" in w for w in warnings), warnings
    finally:
        # Release the real FileHandlers setup_logging attached (Windows locks
        # open log files, so the temp dir can't be removed otherwise).
        for name in ("stock_analyzer", *logging_setup._NOISY_LIBS):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)
        logger.removeHandler(catch)
        logging_setup._CONFIGURED = False
        logging_setup._FIRST_CALL_ARGS = None
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK setup_logging warns when re-called with different args")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} config/logging tests...\n")
    for fn in fns:
        fn()
    print("\nALL CONFIG TESTS PASSED")


if __name__ == "__main__":
    _run_all()
