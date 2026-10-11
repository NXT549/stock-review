"""Tests for the GUI's pure helpers (no Tk window is ever created).

Runs as a plain script (no pytest needed):  .venv313/Scripts/python tests/test_gui.py
Also works under pytest if installed.

Scope: build_argv (form dict -> cli argv, incl. the output-folder -> --out
derivation), expected_output_path (must mirror what build_argv/cli produce),
the settings round-trip helpers (now atomic), the env_flag switch parser,
and the worker's done_payload (cancelled = exit code 130).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_analyzer.review import gui  # noqa: E402

TODAY = date.today().isoformat()


def test_build_argv_modes():
    # custom tickers are positional and normalise commas
    argv = gui.build_argv({"mode": "custom", "tickers": "aapl, msft nvda"})
    assert argv[:3] == ["aapl", "msft", "nvda"]
    # source mode
    argv = gui.build_argv({"mode": "source", "source": "japan"})
    assert argv[:2] == ["--source", "japan"]
    # sector + region
    argv = gui.build_argv({"mode": "sector", "sector": "energy", "region": "us"})
    assert argv[:4] == ["--sector", "energy", "--source", "us"]
    # curated adds no universe flags (only the trailing --no-animation)
    argv = gui.build_argv({"mode": "curated"})
    assert argv == ["--no-animation"]
    print("OK build_argv: modes")


def test_build_argv_validation():
    for bad in ({"mode": "custom", "tickers": "  "},
                {"mode": "file", "file": ""},
                {"mode": "sector", "sector": ""},
                {"mode": "source", "source": ""},
                {"mode": "curated", "limit": "-3"},
                {"mode": "curated", "workers": "zero"}):
        try:
            gui.build_argv(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"build_argv accepted {bad!r}")
    print("OK build_argv: validation errors raise ValueError")


def test_build_argv_outdir():
    # a chosen folder becomes an explicit --out with the dated default filename
    argv = gui.build_argv({"mode": "curated", "outdir": r"C:\Users\me\Desktop"})
    i = argv.index("--out")
    assert argv[i + 1] == str(Path(r"C:\Users\me\Desktop") / f"stock_review_{TODAY}.md")
    # the extension follows the chosen format
    argv = gui.build_argv({"mode": "curated", "outdir": "D:/reports", "fmt": "csv"})
    i = argv.index("--out")
    assert argv[i + 1].endswith(f"stock_review_{TODAY}.csv")
    assert "--format" in argv and argv[argv.index("--format") + 1] == "csv"
    # blank folder -> no --out at all (CLI default applies)
    assert "--out" not in gui.build_argv({"mode": "curated", "outdir": "  "})
    print("OK build_argv: outdir -> --out derivation")


def test_expected_output_path_mirrors_argv():
    # with an outdir, the Open-report target equals the --out we pass the CLI
    cfg = {"mode": "curated", "outdir": r"C:\tmp\reps", "fmt": "json"}
    argv = gui.build_argv(cfg)
    assert str(gui.expected_output_path(cfg)) == argv[argv.index("--out") + 1]
    # without one, it's the CLI default under BASE_DIR
    p = gui.expected_output_path({"fmt": "md"})
    assert p == gui.BASE_DIR / "reports" / f"stock_review_{TODAY}.md"
    # an explicit 'out' (selftest path) wins over everything
    assert gui.expected_output_path({"out": r"C:\x\y.md", "outdir": "ignored"}) \
        == Path(r"C:\x\y.md")
    print("OK expected_output_path: mirrors --out / defaults")


def test_settings_roundtrip(tmp_path=None):
    # load_settings must never raise: missing, corrupt, or wrong-shape files
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        orig = gui.SETTINGS_FILE
        try:
            gui.SETTINGS_FILE = Path(td) / "gui_settings.json"
            assert gui.load_settings() == {}                      # missing
            gui.SETTINGS_FILE.write_text("{not json", encoding="utf-8")
            assert gui.load_settings() == {}                      # corrupt
            gui.SETTINGS_FILE.write_text("[1, 2]", encoding="utf-8")
            assert gui.load_settings() == {}                      # wrong shape
            gui.save_settings({"mode": "custom", "sounds": True, "tip_index": 3})
            data = gui.load_settings()                            # round-trip
            assert data == {"mode": "custom", "sounds": True, "tip_index": 3}
            # the write is atomic: no .tmp orphans next to the settings file
            leftovers = [p.name for p in Path(td).iterdir()
                         if p.name != "gui_settings.json"]
            assert not leftovers, leftovers
        finally:
            gui.SETTINGS_FILE = orig
    print("OK settings: save/load round-trip + corrupt-file tolerance + atomic")


def test_env_flag_parser():
    """'0', junk and negatives are OFF; only a positive int switches ON."""
    import os
    key = "STOCK_REVIEW_TEST_FLAG"
    cases = {None: 0, "": 0, "0": 0, "-5": 0, "junk": 0, "  ": 0,
             "1": 1, " 2 ": 2, "1500": 1500}
    try:
        for raw, want in cases.items():
            if raw is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = raw
            got = gui.env_flag(key)
            assert got == want, (raw, got, want)
    finally:
        os.environ.pop(key, None)
    print("OK env_flag: '0'/junk/negative are OFF, positive ints ON")


def test_done_payload_cancelled_from_exit_code():
    """cancelled comes from the CLI's own 130, never from the cancel event —
    a cancel that lands after a clean finish must stay a success."""
    assert gui.done_payload(0, None) == {"code": 0, "error": None,
                                         "cancelled": False}
    assert gui.done_payload(130, None)["cancelled"] is True
    p = gui.done_payload(1, "boom")
    assert p["cancelled"] is False and p["error"] == "boom"
    print("OK done_payload: cancelled == (exit code 130)")


def _contrast(fg: str, bg: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_status_colours_and_layout_constants():
    from stock_analyzer.review import gui_theme as T
    for fg in (T.STATUS_AMBER, T.STATUS_RED, T.TITLE_GREEN, T.GRAYTEXT):
        assert _contrast(fg, T.FACE) >= 4.5, fg
    # the window must fit a 768px-high laptop screen (taskbar + title bar spare)
    assert gui.MIN_H <= 768 - 80, gui.MIN_H
    print("OK status colours clear AA; MIN_H fits a 768px screen")


def test_still_failed_parser():
    m = gui._STILL_FAILED.search("Wrote r.md  (5 stocks, 2 from cache, 3 still failed)")
    assert m and int(m.group(1)) == 3
    assert gui._STILL_FAILED.search("Wrote r.md  (5 stocks)") is None
    print("OK still-failed parser")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("\nAll GUI helper tests passed.")
