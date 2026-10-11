#!/usr/bin/env python3
"""frozen_entry.py — the single script PyInstaller freezes into BOTH exes.

The installer ships two executables that share one ``_internal/`` bundle:

    * ``Stock Review.exe``           -> the dark Live board window (console=False)
    * ``Stock Review Classic.exe``   -> the original Win98 window (console=False)
    * ``Stock Review Terminal.exe``  -> the real CLI with ASCII animations
                                        (console=True)

They are byte-for-byte the same frozen code; they differ only by **filename**
and PyInstaller's console flag. This entry point picks its behaviour from the
running exe's own name, so there is exactly one Analysis and zero code
duplication — the GUI and CLI can never drift apart.

The ``STOCK_REVIEW_SELFTEST`` env var always forces the headless GUI worker
selftest (see ``gui.main``), regardless of which exe launched it, so the build
pipeline can smoke-test either binary.
"""
import sys
from pathlib import Path

# When running from a source checkout (not frozen) make the packaged code
# importable; harmless in the frozen bundle, which already has it on sys.path.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.review.gui import env_flag  # noqa: E402  (light import; Tk deferred)

# "Terminal" in the exe name => the console build => run the CLI (real console =>
# stderr.isatty() is True => the ASCII animations turn on). The selftest always
# routes through the GUI worker path instead. env_flag, not truthiness: a
# leftover STOCK_REVIEW_SELFTEST=0 must not hijack the Terminal exe.
_terminal = ("terminal" in Path(sys.executable).stem.lower()
             and not env_flag("STOCK_REVIEW_SELFTEST"))

# "Classic" in the exe name => the original Win98 window. The plain
# "Stock Review.exe" now opens the dark Live board (same engine, newer UI).
# The selftest always uses the classic GUI worker path.
_classic = ("classic" in Path(sys.executable).stem.lower()
            or env_flag("STOCK_REVIEW_SELFTEST"))

if _terminal:
    from stock_analyzer.review.cli import main   # full ASCII art, all CLI flags
elif _classic:
    from stock_analyzer.review.gui import main    # Win98 window (or selftest)
else:
    from stock_analyzer.review.gui_board import main  # dark Live board (default)

if __name__ == "__main__":
    raise SystemExit(main())
