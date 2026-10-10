"""Central logging configuration.

Logs go to three places, each dated under logs/:
  * stderr            - our own progress/INFO (stays readable)
  * stock_analyzer_<date>.log - full detail, DEBUG and up
  * errors_<date>.log - WARNING and up only, so failures are easy to find

Noisy third-party libraries (yfinance especially) are routed INTO the log files
rather than left to spew unformatted to the console via logging's "last resort"
handler. yfinance's HTTP 404 "no fundamentals" lines are EXPECTED failures (see
data_collection.py) - they belong in errors_<date>.log, not on screen.

Call setup_logging() once near the start of any entry point.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

_CONFIGURED = False
_FIRST_CALL_ARGS: tuple[str, str] | None = None

# Third-party loggers whose output we capture to the log files instead of
# letting it flood the console. Their failures are expected and non-fatal.
_NOISY_LIBS = ("yfinance", "urllib3", "requests", "peewee")


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> logging.Logger:
    """Configure logging (idempotent). Returns the package logger.

    Idempotent means the SECOND call is a no-op — a caller who configures with
    placeholder values before the real config is loaded (e.g. a scheduler loop
    that needs a logger before it can even read the config file) would
    silently pin the app to those placeholders forever. We warn once instead
    of failing, since the first caller might be intentional.
    """
    global _CONFIGURED, _FIRST_CALL_ARGS
    logger = logging.getLogger("stock_analyzer")
    args = (str(level), str(log_dir))
    if _CONFIGURED:
        if args != _FIRST_CALL_ARGS:
            logger.warning(
                "setup_logging(%r) ignored - already configured with %r "
                "(idempotent; the first caller wins)", args, _FIRST_CALL_ARGS)
        return logger
    _FIRST_CALL_ARGS = args

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    log_file = log_path / f"stock_analyzer_{today}.log"
    error_file = log_path / f"errors_{today}.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    level_value = getattr(logging, str(level).upper(), logging.INFO)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.setLevel(level_value)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)  # full-detail file keeps everything

    # Dedicated error log: WARNING and above only. This is where the expected
    # yfinance 404 "no fundamentals" lines and any retry/collect failures land.
    error_handler = logging.FileHandler(error_file, encoding="utf-8")
    error_handler.setFormatter(fmt)
    error_handler.setLevel(logging.WARNING)

    logger.setLevel(level_value)
    logger.propagate = False
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    logger.addHandler(error_handler)

    # Capture noisy third-party libraries into the SAME files (handlers can be
    # shared across loggers). propagate=False keeps them off the console's
    # last-resort handler, so the terminal shows our progress, not 329 404s.
    for name in _NOISY_LIBS:
        lib = logging.getLogger(name)
        lib.handlers.clear()
        lib.setLevel(logging.WARNING)
        lib.addHandler(file_handler)
        lib.addHandler(error_handler)
        lib.propagate = False

    _CONFIGURED = True
    logger.debug("Logging initialised -> %s (errors -> %s)", log_file, error_file)
    return logger


def get_logger(name: str = "stock_analyzer") -> logging.Logger:
    return logging.getLogger(name if name.startswith("stock_analyzer") else f"stock_analyzer.{name}")
