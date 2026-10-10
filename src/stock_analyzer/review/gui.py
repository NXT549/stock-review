"""Tkinter desktop front-end for the stock-review tool.

Wraps :func:`stock_analyzer.review.cli.main` in a window styled like classic
**Windows 98**: pick a universe (curated / region / sector / custom tickers / a
tickers file), tweak a few options, then click **Run review**. The fetch runs on
a background thread so the window stays responsive; while it runs, the app shows
the ASCII-art loading animation on an embedded "CRT" panel —
the same cast the terminal build plays, rendered here with Tk colour tags (see
:mod:`stock_analyzer.review.gui_anim`). When it finishes you get one-click
buttons to open the report or its folder.

The look is provided by :mod:`stock_analyzer.review.gui_theme` (classic ``tk``
widgets + a silver 3D palette). Both helper modules are stdlib-only so
PyInstaller can freeze this into a standalone ``.exe`` with no extra runtime deps.

Quality-of-life touches, all in the Win98 spirit: a menu bar (File / Extras /
Help), a "Where it lands" output-folder picker (wired to the CLI's ``--out``),
form + window-geometry persistence (``gui_settings.json`` next to the reports),
keyboard shortcuts (F5 / Esc / Ctrl+O / Ctrl+L / Ctrl+D), completion sounds, a
tray-style status-bar clock and run timer, a Tip of the Day box, an About
dialog, and a channel-dial easter egg on the DOS title bar.

Run it directly during development with::

    py stock_review_gui.py
    #  or
    py -m stock_analyzer.review.gui
"""
from __future__ import annotations

import json
import os
import queue
import random
import sys
import threading
import time
import traceback
import uuid
from datetime import date
from pathlib import Path

APP_VERSION = "2.3"
APP_AUTHOR = "NXT"
MIN_W, MIN_H = 720, 860   # smallest window the full layout fits into (see run())


# --------------------------------------------------------------------------- #
# Base directory / working directory
# --------------------------------------------------------------------------- #
def _known_folder_documents() -> Path | None:
    """FOLDERID_Documents via the Windows Known Folder API, or None on failure."""
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        folder_id = _GUID(0xFDD39AD0, 0x238F, 0x46AF,
                          (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85,
                                               0x48, 0x03, 0x69, 0xC7))
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(ptr)) == 0:
            path = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            if path:
                return Path(path)
    except Exception:  # noqa: BLE001 - any failure -> caller tries the next root
        pass
    return None


def _documents_candidates() -> list[Path]:
    """Ordered candidate 'Documents' roots to hold the app's data folder.

    Windows makes this genuinely hard: when Documents is redirected to OneDrive,
    the classic ``%USERPROFILE%\\Documents`` path is a cloud *placeholder* that
    reports as a directory yet rejects new subfolders (WinError 2), and even the
    Known Folder API / registry still point at it. So we gather every plausible
    root and let the caller pick the first that actually accepts a write."""
    cands: list[Path] = []

    def _add(p: Path | None) -> None:
        if p is not None and p not in cands:
            cands.append(p)

    _add(_known_folder_documents())              # correct on non-redirected machines
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        _add(Path(onedrive) / "Documents")        # the writable one when redirected
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if profile:
        _add(Path(profile) / "Documents")
    return cands


def _is_writable_dir(path: Path) -> bool:
    """True only if we can create ``path`` AND write a file inside it.

    ``exists()`` / ``mkdir`` alone are not enough on Windows: OneDrive placeholder
    folders pass those yet reject real writes, so round-trip a marker file."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".stock_review_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _base_dir() -> Path:
    """Where reports/, data/ and the cache live. **Never raises** — resolving this
    runs at import, so a failure here must not stop the app from starting.

    Frozen (PyInstaller): the first *actually writable* ``<Documents>\\Stock
    Review`` we can find, so reports are easy to locate and survive
    uninstall/reinstall; if none is writable we fall back to the exe's own folder
    (writable in a per-user install, the pre-2.0 behaviour). Source checkout: the
    project root (four parents up), so ``py stock_review_gui.py`` is unchanged."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[3]
    try:
        for docs in _documents_candidates():
            base = docs / "Stock Review"
            if _is_writable_dir(base):
                return base
    except Exception:  # noqa: BLE001 - resolution must never crash startup
        pass
    return Path(sys.executable).resolve().parent


def _icon_path() -> Path | None:
    """Locate the bundled window icon (``stock_review.ico``), if present.

    Frozen: PyInstaller unpacks bundled datas into ``sys._MEIPASS``. Source: the
    icon lives in the project's ``packaging/`` folder."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parents[3] / "packaging"
    ico = base / "stock_review.ico"
    return ico if ico.exists() else None


BASE_DIR = _base_dir()
SETTINGS_FILE = BASE_DIR / "gui_settings.json"


# --------------------------------------------------------------------------- #
# Settings persistence  (remember the form between launches)
# --------------------------------------------------------------------------- #
def load_settings() -> dict:
    """Read the persisted form state; any problem just means fresh defaults."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - missing/corrupt settings are not an error
        return {}


def save_settings(data: dict) -> None:
    """Best-effort ATOMIC write; the app must never fail because settings
    couldn't, and a crash mid-write must never leave a corrupt file behind
    (unique sibling temp + os.replace — same shape as the pipeline's cache
    writes; load_settings tolerates corruption, but silently losing every
    remembered setting is still a bad morning)."""
    target = SETTINGS_FILE
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def env_flag(name: str) -> int:
    """An env var as a positive-int switch: unset, '0', junk, negative -> 0.

    Replaces plain truthiness checks — ``STOCK_REVIEW_SELFTEST=0`` used to
    count as ON because any non-empty string is truthy."""
    raw = os.environ.get(name, "").strip()
    try:
        v = int(raw)
    except ValueError:
        return 0
    return v if v > 0 else 0


def done_payload(code: int, error) -> dict:
    """The worker's terminal queue message. ``cancelled`` derives from the
    CLI's OWN exit code (130 = cancel), not from re-reading the cancel event:
    a cancel click that lands after the run already finished cleanly must not
    relabel a successful report as cancelled."""
    return {"code": code, "error": error, "cancelled": code == 130}


# Tip of the Day — the classic Win98 startup ritual. Rotates one per launch.
TIPS = [
    "Press F5 (or Enter) to run a review. Esc cancels it — and you still "
    "get a partial report of everything fetched so far.",
    "Double-click the MS-DOS title bar to flip to the next ASCII scene. "
    "It's a channel dial!",
    "Use the 'Where it lands' folder picker to send reports somewhere handy, "
    "like your Desktop. Leave it blank for the app's own reports folder.",
    "The Format dropdown also does csv and json — great for spreadsheets "
    "and scripts.",
    "Leave Limit blank to review a whole universe, or set it to 10 for a "
    "quick taste of a big one.",
    "Big universes get rate-limited. Rerun the SAME setup a few minutes "
    "later — cached tickers are skipped, so it resumes and fills the gaps.",
    "Tick 'Rich (human-friendly)' for the verbose report; the compact lean "
    "grid is the default.",
    "Everything on this form (and the window size) is remembered for next "
    "launch. Delete gui_settings.json to start fresh.",
    "Ctrl+O opens the last report, Ctrl+L copies its path, Ctrl+D toggles "
    "the details log.",
    "The 'vibes' sparkline under the animation is 100% scientifically "
    "meaningless. We just like it.",
    "Hover over almost any control for a tooltip explaining what it does.",
    "The Crystal Ball (Extras menu, or Ctrl+8) reads your market fortune. It is "
    "exactly as scientific as the vibes sparkline.",
    "File > Recent reports remembers your last five reports - one click to "
    "reopen any of them.",
    "The About box has an odometer: your lifetime runs and tickers reviewed.",
    "The grey hint under 'What to review' explains whatever universe you've "
    "picked - handy for the cryptic ones like sp1500 or a200.",
    "The Limit and Workers boxes turn pink the moment a value isn't a "
    "positive whole number.",
    "There are 13 ASCII channels now - a stock exchange, a city at night, a "
    "bank vault and a container harbour. Double-click the DOS bar to surf.",
    "Maximise the window: the ASCII art scales itself up to fill whatever "
    "screen it's given.",
    "In a hurry? Quick picks (in the menu bar) fills the whole form in one "
    "click - ASX 200, S&P 500, US tech and friends.",
    "This report is a research screen, not financial advice. Always "
    "sanity-check a number before acting on it.",
]

# Success banners for the status bar — one at random per finished run. Kept
# playful but stopped short of implying the tool itself vouches for the
# numbers ("approves"/"assessed" dropped) — the static DONE_TAGLINE below
# carries that disclaimer instead, every time, non-randomized.
DONE_MSGS = [
    "Done! Your report is ready - hit Open report.",
    "Ta-da! Fresh numbers, hot off the money printer.",
    "All wrapped up - the report awaits your eyeballs.",
    "Report delivered. The intern demands a snack.",
    "Fresh off the press - mind the wet ink.",
    "The closing bell rings - your report is in.",
]
DONE_TAGLINE = "Done - research screen, not advice."

# Crystal-ball answers (Extras > Crystal Ball). Entertainment only.
FORTUNES = [
    "Outlook bullish. Snacks recommended.",
    "The candles whisper: green, eventually.",
    "Timing the market? Ask a different crystal ball.",
    "Ask again after the dip buys itself.",
    "A diamond hand appears. It is yours.",
    "The ball sees... fees. Watch the fees.",
    "The moon is closer than it appears.",
    "All signs point to: more coffee.",
    "Patience. Also, hydrate.",
    "The whale nods approvingly at your watchlist.",
    "It is not a loss until you look at it.",
    "Fortune favours the patient investor.",
    "The printer goes brrr in your general direction.",
    "Concentrate and ask again (market's closed).",
    "Your snack ratio? Pretty excellent.",
    "Beware of bears bearing bad vibes.",
    "Past performance guarantees a good story.",
    "The tape is long, but it bends towards dividends.",
]


# --------------------------------------------------------------------------- #
# Universe hints  (the grey line that decodes cryptic source names)
# --------------------------------------------------------------------------- #
# Alias -> canonical name, mirroring the CLI's --source groupings.
_SOURCE_ALIAS = {
    "sap": "sp500", "midcap": "sp400", "smallcap": "sp600",
    "us": "sp1500", "america": "sp1500",
    "us-smallcap": "america-smallcap",
    "asx200": "australia", "a200": "australia", "asx": "australia",
    "australia-all": "asx-all",
    "au-smallcap": "australia-smallcap",
    "intl": "international",
    "world": "everything", "all": "everything",
    "world-smallcap": "global-smallcap",
    "nikkei": "japan", "ftse100": "uk", "dax": "germany", "hsi": "hongkong",
    "tsx60": "canada", "nifty50": "india",
}
_SOURCE_HINTS = {
    "curated": "the hand-picked starter watchlist - quick and friendly",
    "sp500": "S&P 500 - the big US large caps (~500 names)",
    "sp400": "S&P MidCap 400 - the US mid caps",
    "sp600": "S&P SmallCap 600 - the US small caps",
    "sp1500": "S&P 1500 - the whole US board (~1,500 names, long run)",
    "america-smallcap": "the US small-cap sleeve",
    "australia": "ASX 200 - Australia's top 200",
    "asx-all": "every ASX listing (~2,000 - big and slow, pack a lunch)",
    "australia-smallcap": "the Australian small-cap sleeve",
    "japan": "Nikkei 225 - Japan's blue chips",
    "uk": "FTSE 100 - London's big board",
    "germany": "DAX - Germany's flagship index",
    "hongkong": "Hang Seng - Hong Kong's heavyweights",
    "canada": "TSX 60 - Canada's largest",
    "india": "NIFTY 50 - India's top 50",
    "global": "the US + Australia + major markets combo",
    "global-smallcap": "small caps across markets (long run)",
    "international": "the non-US markets combined",
    "everything": "all of it. every universe at once. pack two lunches",
}


def universe_hint(mode: str, source: str = "", sector: str = "",
                  region: str = "") -> str:
    """One grey line of plain English describing the current form selection."""
    if mode == "curated":
        return _SOURCE_HINTS["curated"]
    if mode == "source":
        key = (source or "").strip().lower()
        return _SOURCE_HINTS.get(_SOURCE_ALIAS.get(key, key),
                                 "a named universe - run it and see")
    if mode == "sector":
        where = f"the '{region}' region" if (region or "").strip() else \
            "US + Australia"
        return f"every {sector or 'sector'} name across {where}"
    if mode == "custom":
        return "your own list - space or comma separated, e.g. AAPL MSFT BHP.AX"
    if mode == "file":
        return "one ticker per line, or a CSV with a 'ticker' column"
    return ""


# --------------------------------------------------------------------------- #
# argv building  (pure, unit-testable, no Tk)
# --------------------------------------------------------------------------- #
def build_argv(cfg: dict) -> list[str]:
    """Translate the form state (a plain dict) into a cli.main() argv list.

    ``cfg`` keys: mode ('curated'|'source'|'sector'|'custom'|'file'), source,
    sector, region, tickers, file, limit, workers, fmt, rich, no_names, outdir.
    Raises ValueError with a human message when a required field is missing."""
    argv: list[str] = []
    mode = cfg.get("mode", "curated")

    if mode == "custom":
        raw = (cfg.get("tickers") or "").replace(",", " ").split()
        if not raw:
            raise ValueError("Enter at least one ticker (e.g. AAPL MSFT NVDA).")
        argv += raw  # positional tickers override everything else
    elif mode == "file":
        path = (cfg.get("file") or "").strip()
        if not path:
            raise ValueError("Choose a tickers file, or pick a different mode.")
        argv += ["--file", path]
    elif mode == "sector":
        sector = (cfg.get("sector") or "").strip()
        if not sector:
            raise ValueError("Pick a sector.")
        argv += ["--sector", sector]
        region = (cfg.get("region") or "").strip()
        if region:
            argv += ["--source", region]
    elif mode == "source":
        source = (cfg.get("source") or "").strip()
        if not source:
            raise ValueError("Pick a region / source.")
        argv += ["--source", source]
    # mode == "curated": no universe flags -> the built-in default list

    limit = str(cfg.get("limit") or "").strip()
    if limit:
        if not limit.isdigit() or int(limit) <= 0:
            raise ValueError("Limit must be a positive whole number (or blank).")
        argv += ["--limit", limit]

    workers = str(cfg.get("workers") or "").strip()
    if workers:
        if not workers.isdigit() or int(workers) <= 0:
            raise ValueError("Workers must be a positive whole number (or blank).")
        argv += ["--workers", workers]

    fmt = (cfg.get("fmt") or "md").strip() or "md"
    if fmt != "md":
        argv += ["--format", fmt]

    # A chosen output folder becomes an explicit --out path (folder + the same
    # dated filename the CLI would pick itself). Blank = the CLI default.
    outdir = (cfg.get("outdir") or "").strip()
    if outdir:
        argv += ["--out",
                 str(Path(outdir) / f"stock_review_{date.today().isoformat()}.{fmt}")]

    if cfg.get("rich"):
        argv.append("--rich")
    if cfg.get("no_names"):
        argv.append("--no-names")

    # The CLI's own ANSI animation would only spam a non-terminal stream, so keep
    # it off here. The GUI drives its own Tk animation (gui_anim) instead.
    argv.append("--no-animation")
    return argv


def expected_output_path(cfg: dict) -> Path:
    """Where cli.main will write, so the 'Open report' button knows the target.
    Mirrors the default in cli.main (reports/stock_review_<date>.<fmt>) and the
    --out path build_argv derives from a chosen output folder."""
    fmt = (cfg.get("fmt") or "md").strip() or "md"
    name = f"stock_review_{date.today().isoformat()}.{fmt}"
    out = (cfg.get("out") or "").strip()
    if out:
        return Path(out)
    outdir = (cfg.get("outdir") or "").strip()
    if outdir:
        return Path(outdir) / name
    return BASE_DIR / "reports" / name


# --------------------------------------------------------------------------- #
# stdout/stderr -> queue  (so the worker thread can feed the log pane)
# --------------------------------------------------------------------------- #
class _StreamToQueue:
    """A minimal text stream that forwards writes to a queue.

    ``isatty()`` returns False so the CLI auto-disables its animation / colour."""

    def __init__(self, q: "queue.Queue[tuple[str, object]]") -> None:
        self._q = q

    def write(self, text: str) -> int:
        if text:
            self._q.put(("log", text))
        return len(text)

    def flush(self) -> None:  # noqa: D401 - stream protocol
        pass

    def isatty(self) -> bool:
        return False


class QueueProgress:
    """Injected progress sink handed to ``cli.main`` from the GUI worker thread.

    Duck-typed to the ``ascii_art.Animator`` interface (``start`` / ``update`` /
    ``stop`` / ``.total``) but it does no drawing — it just forwards each progress
    update onto the Tk queue as a ``("progress", {...})`` message. The actual
    animation is rendered on the *main* thread by ``gui_anim.TkAnimator`` (Tk is
    not thread-safe), so nothing here ever touches a widget."""

    def __init__(self, q: "queue.Queue[tuple[str, object]]") -> None:
        self._q = q
        self.total = 1

    def start(self) -> None:  # lifecycle owned by the GUI; nothing to do here
        pass

    def update(self, done, *, ticker="", ok=0, failed=0) -> None:
        self._q.put(("progress", {"done": done, "total": self.total,
                                   "ok": ok, "failed": failed, "ticker": ticker}))

    def retry(self, *, ticker="", attempt=0, max_retries=0, delay=0.0) -> None:
        """A fetch worker is backing off before re-attempting a ticker; surface
        it (HUD + log) instead of letting the re-attempt happen invisibly."""
        self._q.put(("retry", {"ticker": ticker, "attempt": attempt,
                               "max_retries": max_retries, "delay": delay}))

    def note(self, text=None) -> None:
        """A one-line CLI status (e.g. 'retry round 2/4 ...') for the HUD."""
        self._q.put(("note", text))

    def stop(self, final_msg=None) -> None:
        pass


def _run_review(argv: list[str], q: "queue.Queue[tuple[str, object]]",
                progress: "QueueProgress | None" = None,
                cancel_event=None) -> None:
    """Worker-thread body: redirect streams, run the CLI, report the result."""
    from .cli import main  # imported lazily so the window opens instantly

    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _StreamToQueue(q)
    code, error = 1, None
    if progress is not None:
        try:
            progress.start()
        except Exception:  # noqa: BLE001 - progress is cosmetic, never fatal
            pass
    try:
        code = int(main(argv, progress=progress, cancel_event=cancel_event) or 0)
    except SystemExit as exc:  # resolve_tickers raises SystemExit(message)
        if isinstance(exc.code, int):
            code = exc.code
        else:
            code, error = 2, str(exc.code or "")
            if error:
                print(error)
    except Exception as exc:  # noqa: BLE001 - surface anything to the log
        code, error = 1, repr(exc)
        traceback.print_exc()
    finally:
        if progress is not None:
            try:
                progress.stop()
            except Exception:  # noqa: BLE001
                pass
        sys.stdout, sys.stderr = old_out, old_err
    q.put(("done", done_payload(code, error)))


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #
def run(test_close_ms: int | None = None) -> int:
    """Open the window and block in the Tk event loop.

    ``test_close_ms`` is a smoke-test hook: when set, the window auto-closes
    after that many milliseconds so a headless check can build every widget
    without hanging in ``mainloop``."""
    # Crisper text on high-DPI displays. Must run BEFORE the Tk root exists;
    # guarded because it's Windows-only and purely cosmetic.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - old Windows / no shcore: skip quietly
            pass

    import tkinter as tk
    from tkinter import filedialog

    from . import gui_theme as T

    # The ASCII animation is a nice-to-have: if it can't import, the app still
    # runs reviews (just without the CRT panel).
    try:
        from . import gui_anim
    except Exception:  # noqa: BLE001
        gui_anim = None  # type: ignore[assignment]

    # Populate the dropdowns from the live universe definitions.
    from .universe import SECTORS, SOURCES

    sources = sorted(SOURCES)
    sectors = sorted(SECTORS)
    regions = ["", "global", "us", "australia", "sp500", "sp400", "sp600"]

    # Reports/cache should land in a predictable place (next to the exe).
    os.chdir(BASE_DIR)

    root = tk.Tk()
    T.apply_root(root)
    root.title("Stock Review")
    _ico = _icon_path()
    if _ico is not None:
        try:
            root.iconbitmap(str(_ico))
        except Exception:  # noqa: BLE001 - a bad/locked icon must never stop the app
            pass
    # The 100-col MS-DOS screen is the always-on centrepiece, with the form below
    # it — so the window is sized to show both the full animation and the controls
    # (incl. the "Where it lands" row and the status bar under everything).
    # First launch: size to the screen (the CRT font scales with the window, so
    # a bigger default = bigger art). A remembered geometry overrides this below.
    root.minsize(MIN_W, MIN_H)
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        def_w = max(MIN_W, min(1150, int(sw * 0.62)))
        def_h = max(MIN_H, min(1250, int(sh * 0.88)))
    except tk.TclError:
        def_w, def_h = 760, 900
    root.geometry(f"{def_w}x{def_h}")

    log_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
    state = {"path": None, "run_t0": None}  # last report path / run start time
    cancel_event = threading.Event()  # set by the Cancel button to stop a run

    # ---- form variables (seeded from the last session's settings) ---------- #
    saved = load_settings()

    # Recent reports (File menu) and the lifetime odometer, both persisted.
    state["recent"] = [p for p in (saved.get("recent") or [])
                       if isinstance(p, str)][:5]
    for _key in ("runs_total", "tickers_total"):
        try:
            state[_key] = max(0, int(saved.get(_key, 0)))
        except (TypeError, ValueError):
            state[_key] = 0

    def _remembered(key: str, default: str, allowed=None) -> str:
        """A persisted string value, falling back when missing or no longer valid."""
        val = saved.get(key)
        if not isinstance(val, str) or (allowed is not None and val not in allowed):
            return default
        return val

    mode_var = tk.StringVar(value=_remembered(
        "mode", "source", ("curated", "source", "sector", "custom", "file")))
    source_var = tk.StringVar(value=_remembered(
        "source", "curated" if "curated" in sources else sources[0], sources))
    sector_var = tk.StringVar(value=_remembered(
        "sector", sectors[0] if sectors else "", sectors))
    region_var = tk.StringVar(value=_remembered("region", "", regions))
    tickers_var = tk.StringVar(value=_remembered("tickers", "AAPL MSFT NVDA"))
    file_var = tk.StringVar(value=_remembered("file", ""))
    limit_var = tk.StringVar(value=_remembered("limit", ""))
    workers_var = tk.StringVar(value=_remembered("workers", "6"))
    fmt_var = tk.StringVar(value=_remembered("fmt", "md", ("md", "csv", "json")))
    outdir_var = tk.StringVar(value=_remembered("outdir", ""))
    rich_var = tk.BooleanVar(value=bool(saved.get("rich", False)))
    nonames_var = tk.BooleanVar(value=bool(saved.get("no_names", False)))
    details_var = tk.BooleanVar(value=bool(saved.get("details", False)))
    crt_var = tk.BooleanVar(value=bool(saved.get("crt_shown", True)))
    sounds_var = tk.BooleanVar(value=bool(saved.get("sounds", True)))
    tips_var = tk.BooleanVar(value=bool(saved.get("show_tips", True)))

    # Restore the last window size/position. minsize only constrains interactive
    # resizes, so reject any remembered size the layout no longer fits into.
    geo = saved.get("geometry")
    if isinstance(geo, str) and "x" in geo:
        try:
            w, h = (int(v) for v in geo.split("+", 1)[0].split("x"))
            if w >= MIN_W and h >= MIN_H:
                root.geometry(geo)
        except (ValueError, tk.TclError):
            pass

    outer = tk.Frame(root, bg=T.FACE)
    outer.pack(fill="both", expand=True, padx=6, pady=6)

    # ---- app title bar (the classic Win98 gradient) ------------------------ #
    T.TitleBar(outer, f"  Stock Review  —  pick · run · read  ·  by {APP_AUTHOR}",
               height=24).pack(fill="x")

    # ---- HERO: the MS-DOS Prompt screen, always animating ------------------ #
    # This is the centrepiece: the ASCII scenes cycle here in "attract mode" while
    # you set up a run, then the same screen shows the live progress HUD.
    # NOTE: dos/formrow/save/bar/statusbar are all packed together in the layout
    # block further down — bottom-up, so a too-short window squeezes the DOS
    # screen rather than clipping the form or the status bar.
    dos = tk.Frame(outer, bg=T.FACE)
    dos_title = T.TitleBar(dos, " MS-DOS Prompt  -  STOCK REVIEW", height=20)
    dos_title.pack(fill="x")
    screen_wrap = tk.Frame(dos, relief="sunken", bd=3, bg=T.CRT_BG)
    screen_wrap.pack(fill="both", expand=True)
    screen = tk.Text(screen_wrap, bg=T.CRT_BG, fg=T.CRT_FG, wrap="none",
                     width=100, height=36, bd=0, highlightthickness=0,
                     font=T.MONO_FONT, state="disabled", cursor="arrow")
    screen.pack(fill="both", expand=True, padx=3, pady=3)

    tk_anim = None
    if gui_anim is not None:
        try:
            tk_anim = gui_anim.TkAnimator(root, screen)
        except Exception:  # noqa: BLE001 - fall back to no animation
            tk_anim = None

    def _anim(method: str, **kw):
        """Call a TkAnimator method if the animator exists. The animation is
        cosmetic, so any failure returns None rather than surfacing."""
        if tk_anim is None:
            return None
        try:
            return getattr(tk_anim, method)(**kw)
        except Exception:  # noqa: BLE001
            return None

    # ---- form: two Win98 group boxes, side by side ------------------------- #
    formrow = tk.Frame(outer, bg=T.FACE)

    uni = T.group98(formrow, "What to review", fg=T.NAVY)
    uni.pack(side="left", fill="both", expand=True)
    uni.columnconfigure(1, weight=1)

    def _radio(row: int, value: str, label: str):
        T.radio98(uni, label, mode_var, value, command=lambda: _sync()).grid(
            row=row, column=0, sticky="w", pady=3)

    _radio(0, "curated", "Curated default list")

    _radio(1, "source", "Region / source")
    source_cb = T.option98(uni, source_var, sources, width=24)
    source_cb.grid(row=1, column=1, sticky="w", padx=6, pady=3)

    _radio(2, "sector", "Sector")
    sector_row = tk.Frame(uni, bg=T.FACE)
    sector_row.grid(row=2, column=1, sticky="w", padx=6, pady=3)
    # Matches source_cb's width so the FIRST dropdown in every row shares one
    # column boundary — the nested label + region_cb after it are the part
    # that legitimately extends further right for this one row.
    sector_cb = T.option98(sector_row, sector_var, sectors, width=24)
    sector_cb.pack(side="left")
    T.label98(sector_row, text="  in region:").pack(side="left")
    region_cb = T.option98(sector_row, region_var, regions, width=11)
    region_cb.pack(side="left", padx=(2, 0))

    _radio(3, "custom", "Custom tickers")
    tickers_entry = T.entry98(uni, tickers_var)
    tickers_entry.grid(row=3, column=1, sticky="ew", padx=6, pady=3)

    _radio(4, "file", "From a file")
    file_row = tk.Frame(uni, bg=T.FACE)
    file_row.grid(row=4, column=1, sticky="ew", padx=6, pady=3)
    file_row.columnconfigure(0, weight=1)
    file_entry = T.entry98(file_row, file_var)
    file_entry.grid(row=0, column=0, sticky="ew")

    # A plain-English decoder for whatever is selected above ("sp1500"? "a200"?).
    # Grid at column=1 (not columnspan=2 from column=0) so its left edge lines
    # up with the dropdowns/entries of the active row above it, instead of
    # starting flush with the static radio-label column — a small connector
    # glyph makes that visual tether explicit too.
    hint_var = tk.StringVar()
    T.label98(uni, textvariable=hint_var, fg=T.GRAYTEXT, wraplength=320,
              justify="left", anchor="w").grid(row=5, column=1,
                                               sticky="ew", padx=6, pady=(5, 0))

    def _pick_file():
        chosen = filedialog.askopenfilename(
            title="Choose a tickers file",
            filetypes=[("Tickers", "*.txt *.csv"), ("All files", "*.*")])
        if chosen:
            file_var.set(chosen)
            mode_var.set("file")
            _sync()

    T.button98(file_row, text="Browse...", command=_pick_file,
               fill=T.BTN_TEAL).grid(row=0, column=1, padx=(6, 0))

    opt = T.group98(formrow, "Options", fg=T.TITLE_GREEN)
    opt.pack(side="left", fill="both", padx=(6, 0))
    T.label98(opt, text="Limit").grid(row=0, column=0, sticky="w", pady=3)
    limit_entry = T.entry98(opt, limit_var, width=8)
    limit_entry.grid(row=0, column=1, sticky="w", padx=4, pady=3)
    limit_caption = T.label98(opt, text="", fg=T.STATUS_RED, font=("MS Sans Serif", 7))
    limit_caption.grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
    # De-emphasized: Workers is a secondary/advanced tuning knob (parallel
    # fetch count), not a primary decision like Limit or Format.
    T.label98(opt, text="Workers", fg=T.GRAYTEXT).grid(
        row=2, column=0, sticky="w", pady=3)
    workers_entry = T.entry98(opt, workers_var, width=8)
    workers_entry.grid(row=2, column=1, sticky="w", padx=4, pady=3)
    workers_caption = T.label98(opt, text="", fg=T.STATUS_RED, font=("MS Sans Serif", 7))
    workers_caption.grid(row=3, column=0, columnspan=2, sticky="w", padx=4)
    T.label98(opt, text="Format").grid(row=4, column=0, sticky="w", pady=3)
    fmt_cb = T.option98(opt, fmt_var, ["md", "csv", "json"], width=6)
    fmt_cb.grid(row=4, column=1, sticky="w", padx=4, pady=3)
    rich_chk = T.check98(opt, "Rich (human-friendly)", rich_var)
    rich_chk.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
    nonames_chk = T.check98(opt, "Drop company names", nonames_var)
    nonames_chk.grid(row=6, column=0, columnspan=2, sticky="w")

    # Numbers-only fields blush pink the moment they hold something invalid,
    # instead of waiting for Run to complain — and now carry TWO more cues
    # that don't rely on color alone: a thicker border (a colorblind user
    # gets nothing from the pink tint) and an inline caption spelling out
    # what's expected, instead of only a status-bar complaint after Run.
    def _tint_numeric(entry: "tk.Entry", var: "tk.StringVar",
                      caption: "tk.Label") -> None:
        def _chk(*_):
            s = var.get().strip()
            ok = (not s) or (s.isdigit() and int(s) > 0)
            entry.configure(bg=T.FIELD if ok else "#ffd9d9",
                           highlightthickness=0 if ok else 2,
                           highlightbackground=T.INK, highlightcolor=T.INK)
            caption.configure(text="" if ok else "positive whole number (or blank)")
        var.trace_add("write", _chk)
        _chk()

    _tint_numeric(limit_entry, limit_var, limit_caption)
    _tint_numeric(workers_entry, workers_var, workers_caption)

    # ---- where the report is saved (blank = the app's own reports folder) -- #
    save = T.group98(outer, "Where it lands", fg=T.TITLE_PURPLE)
    save.columnconfigure(1, weight=1)
    T.label98(save, text="Folder").grid(row=0, column=0, sticky="w")
    outdir_entry = T.entry98(save, outdir_var)
    outdir_entry.grid(row=0, column=1, sticky="ew", padx=6)

    def _pick_outdir():
        start = outdir_var.get().strip() or str(BASE_DIR / "reports")
        chosen = filedialog.askdirectory(title="Choose where reports are saved",
                                         initialdir=start)
        if chosen:
            outdir_var.set(os.path.normpath(chosen))

    T.button98(save, text="Browse...", command=_pick_outdir,
               fill=T.BTN_TEAL).grid(row=0, column=2)
    T.button98(save, text="Default", command=lambda: outdir_var.set("")).grid(
        row=0, column=3, padx=(6, 0))
    # Live preview of exactly where the next report will land.
    next_var = tk.StringVar()
    T.label98(save, textvariable=next_var, fg=T.GRAYTEXT, anchor="w").grid(
        row=1, column=0, columnspan=4, sticky="ew", pady=(3, 0))

    def _update_next(*_):
        cfg = {"fmt": fmt_var.get(), "outdir": outdir_var.get()}
        next_var.set(f"Next report:  {expected_output_path(cfg)}")

    outdir_var.trace_add("write", _update_next)
    fmt_var.trace_add("write", _update_next)
    _update_next()

    # ---- action buttons (coloured Win98 3D push buttons) ------------------- #
    bar = tk.Frame(outer, bg=T.FACE)
    run_btn = T.button98(bar, text="▶ Run review", command=lambda: _start(),
                         fill=T.BTN_GREEN)
    run_btn.pack(side="left")
    cancel_btn = T.button98(bar, text="■ Cancel", command=lambda: _cancel(),
                            fill=T.BTN_RED, state="disabled")
    cancel_btn.pack(side="left", padx=(6, 0))
    open_btn = T.button98(bar, text="Open report", command=lambda: _open(state["path"]),
                          fill=T.BTN_BLUE, state="disabled")
    open_btn.pack(side="left", padx=(6, 0))
    folder_btn = T.button98(bar, text="Open folder", command=lambda: _open_report_folder(),
                            fill=T.BTN_TEAL, state="disabled")
    folder_btn.pack(side="left", padx=(6, 0))
    copy_btn = T.button98(bar, text="Copy path", command=lambda: _copy_path(),
                          fill=T.BTN_PURPLE, state="disabled")
    copy_btn.pack(side="left", padx=(6, 0))
    details_chk = T.check98(bar, "Details", details_var,
                            command=lambda: _toggle_details())
    details_chk.pack(side="right")

    # ---- collapsible details log ------------------------------------------- #
    details_wrap = tk.Frame(outer, bg=T.FACE)  # packed on demand (see _toggle_details)
    log = tk.Text(details_wrap, height=7, wrap="word", state="disabled",
                  background=T.CRT_BG, foreground=T.CRT_FG, insertbackground=T.CRT_FG,
                  relief="sunken", bd=2, highlightthickness=0, font=T.MONO_FONT)
    log.pack(fill="both", expand=True)

    # ---- status bar (message cell + run timer + tray clock, all sunken) ----- #
    statusbar = tk.Frame(outer, bg=T.FACE)
    status_cell = tk.Frame(statusbar, relief="sunken", bd=1, bg=T.FACE)
    status_cell.pack(side="left", fill="x", expand=True)
    status_var = tk.StringVar(value="Ready when you are.")
    status_lbl = T.label98(status_cell, textvariable=status_var, anchor="w")
    status_lbl.pack(side="left", padx=4)
    clock_cell = tk.Frame(statusbar, relief="sunken", bd=1, bg=T.FACE)
    clock_cell.pack(side="right", padx=(4, 0))
    clock_var = tk.StringVar()
    T.label98(clock_cell, textvariable=clock_var).pack(padx=6)
    timer_cell = tk.Frame(statusbar, relief="sunken", bd=1, bg=T.FACE)
    timer_cell.pack(side="right", padx=(4, 0))
    timer_var = tk.StringVar(value="idle")
    T.label98(timer_cell, textvariable=timer_var, fg=T.GRAYTEXT).pack(padx=6)

    def _tick_clock():
        """Once a second: the little tray clock, and the elapsed-run timer."""
        clock_var.set(time.strftime("%I:%M %p").lstrip("0"))
        t0 = state["run_t0"]
        if t0 is not None:
            secs = int(time.monotonic() - t0)
            timer_var.set(f"run {secs // 60}:{secs % 60:02d}")
        root.after(1000, _tick_clock)

    def _status(msg: str, color: str = T.INK):
        """Set the status line text and tint it (navy busy / green ok / red error)."""
        status_var.set(msg)
        status_lbl.configure(fg=color)

    # ---- layout ------------------------------------------------------------- #
    # Pack the fixed rows bottom-up FIRST so they always get their space, then
    # let the DOS screen expand into (and absorb any squeeze in) the middle.
    # Pack order = allocation priority; the details pane slots in above the
    # statusbar on demand (see _toggle_details).
    statusbar.pack(side="bottom", fill="x", pady=(6, 0))
    bar.pack(side="bottom", fill="x", pady=(6, 0))
    save.pack(side="bottom", fill="x", pady=(6, 0))
    formrow.pack(side="bottom", fill="x", pady=(6, 0))
    dos.pack(fill="both", expand=True, pady=(6, 0))

    # ---- tooltips (the classic hover-yellow ones) --------------------------- #
    for _w, _tip in (
        (source_cb, "A named universe: an index (sp500, a200), a whole market "
                    "(japan, uk), or a combo (global). The grey line under the "
                    "form says what the current pick means."),
        (sector_cb, "One sector/industry group, e.g. mining or technology."),
        (region_cb, "Optionally pin the sector to one region. Blank = "
                    "US + Australia."),
        (tickers_entry, "Your own tickers, separated by spaces or commas. "
                        "Exchange suffixes work: BHP.AX, 7203.T, RIO.L."),
        (file_entry, "A .txt with one ticker per line, or a .csv with a "
                     "'ticker' column."),
        (limit_entry, "Cap the number of tickers - handy for a quick taste of "
                      "a big universe. Blank = review all of them."),
        (workers_entry, "Parallel fetchers. Lower it (2-3) if a big run hits "
                        "lots of rate limits."),
        (fmt_cb, "md = the full Markdown report. csv/json = just the data "
                 "grid, for spreadsheets and scripts."),
        (rich_chk, "Verbose, human-friendly cells ($3.2T, +12.6%) instead of "
                   "the compact AI-ready grid. Applies to every format - md, "
                   "csv, and json all use the same column set."),
        (nonames_chk, "Drop the company-name column for maximum token savings "
                      "on large runs. Applies to every format, not just md."),
        (outdir_entry, "Where reports land. Blank = the app's own reports "
                       "folder."),
        (run_btn, "Fetch everything and write the report (F5 or Enter)."),
        (cancel_btn, "Stop the run (Esc). Whatever was already fetched is "
                     "still written as a partial report."),
        (open_btn, "Open the finished report (Ctrl+O)."),
        (folder_btn, "Open the folder the report landed in."),
        (copy_btn, "Copy the report's full path (Ctrl+L)."),
        (details_chk, "Show the raw run log - progress lines, retries, "
                      "errors (Ctrl+D)."),
    ):
        T.tooltip98(_w, _tip)

    # ---- behaviour --------------------------------------------------------- #
    def _sync(*_):
        """Refresh the grey plain-English hint describing the current selection.

        Every sibling control below stays clickable regardless of the active
        mode (see the auto-select traces just below) — mirroring Browse,
        which was never disabled either — so nothing needs enabling/disabling
        here; the mode radios are the visual indicator of what's active."""
        m = mode_var.get()
        hint_var.set("↳ " + universe_hint(m, source_var.get(), sector_var.get(),
                                              region_var.get()))

    # Picking a value in ANY of these sibling controls auto-selects the mode
    # radio it belongs to — the same behaviour Browse already had (it was
    # never disabled, and always set mode_var itself). Previously these
    # controls were disabled until their radio was picked FIRST, so this also
    # removes a click-order dependency: e.g. jumping straight into the Region
    # dropdown from "curated" mode now switches to "source" mode on the spot.
    def _pick_source(*_):
        mode_var.set("source")
        _sync()

    def _pick_sector(*_):
        mode_var.set("sector")
        _sync()

    def _pick_custom(*_):
        mode_var.set("custom")
        _sync()

    source_var.trace_add("write", _pick_source)
    sector_var.trace_add("write", _pick_sector)
    region_var.trace_add("write", _pick_sector)   # region is sector-mode-only
    tickers_var.trace_add("write", _pick_custom)
    file_var.trace_add("write", lambda *_: (mode_var.set("file"), _sync()))

    def _toggle_details(grow: bool = True):
        """Show/hide the log pane. ``grow`` resizes the window to make room so
        the animation keeps its full height; the startup restore skips it (the
        remembered geometry already includes the pane)."""
        shown = bool(details_wrap.winfo_manager())
        if details_var.get() and not shown:
            details_wrap.pack(side="bottom", fill="both", pady=(6, 0),
                              after=statusbar)
            delta = 1
        elif not details_var.get() and shown:
            details_wrap.pack_forget()
            delta = -1
        else:
            return
        if not grow:
            return
        try:
            root.update_idletasks()
            size, _, pos = root.geometry().partition("+")
            w, h = (int(v) for v in size.split("x"))
            h += delta * (details_wrap.winfo_reqheight() + 6)
            root.geometry(f"{w}x{h}" + (f"+{pos}" if pos else ""))
        except (ValueError, tk.TclError):  # resize is best-effort polish
            pass

    def _toggle_crt():
        """Collapse/expand the always-on ASCII/CRT panel — it's by far the
        single biggest visual element (the review's ~3:1 area-dominance
        finding), and this makes that a choice instead of a redesign. Unlike
        Details this doesn't resize the window: the panel is the window's
        dominant element by design, so shrinking the window to match would
        need to fight root.minsize (sized assuming the panel is present)
        rather than just complement it — collapsing simply frees the screen
        area for whatever else is showing."""
        shown = bool(dos.winfo_manager())
        if crt_var.get() and not shown:
            dos.pack(fill="both", expand=True, pady=(6, 0))
        elif not crt_var.get() and shown:
            dos.pack_forget()

    def _append(text: str):
        log.configure(state="normal")
        log.insert("end", text)
        log.see("end")
        log.configure(state="disabled")

    def _open(path):
        if not path:
            return
        try:
            os.startfile(str(path))  # Windows
        except AttributeError:  # pragma: no cover - non-Windows fallback
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(path)])
        except OSError as exc:
            _append(f"\n[could not open {path}: {exc}]\n")

    def _current_cfg() -> dict:
        return {
            "mode": mode_var.get(), "source": source_var.get(),
            "sector": sector_var.get(), "region": region_var.get(),
            "tickers": tickers_var.get(), "file": file_var.get(),
            "limit": limit_var.get(), "workers": workers_var.get(),
            "fmt": fmt_var.get(), "rich": rich_var.get(), "no_names": nonames_var.get(),
            "outdir": outdir_var.get(),
        }

    def _collect_settings() -> dict:
        """Everything worth remembering for the next launch."""
        data = _current_cfg()
        data.update(details=details_var.get(), crt_shown=crt_var.get(),
                    sounds=sounds_var.get(),
                    show_tips=tips_var.get(), tip_index=state.get("tip_index", 0),
                    welcomed=bool(state.get("welcomed") or saved.get("welcomed")),
                    geometry=root.geometry(),
                    recent=state.get("recent", []),
                    runs_total=state.get("runs_total", 0),
                    tickers_total=state.get("tickers_total", 0))
        return data

    def _remember_report(path) -> None:
        """Put a freshly written report at the top of File > Recent reports."""
        p = str(path)
        state["recent"] = ([p] + [r for r in state.get("recent", [])
                                  if r != p])[:5]

    def _bump_odometer() -> None:
        """Roll the lifetime counters shown in the About box. Each retry round
        is its own batch with its own ok-count, so add the finished batches
        (odo_ok) plus the still-current one."""
        prog = state.get("last_prog") or {}
        state["runs_total"] = int(state.get("runs_total", 0)) + 1
        state["tickers_total"] = (int(state.get("tickers_total", 0))
                                  + int(state.get("odo_ok", 0))
                                  + int(prog.get("ok", 0) or 0))

    def _jingle(kind: str):
        """The period-correct system beeps: ding on success, quieter neutral
        beep on a cancelled-but-successful partial report, chord on trouble.
        Windows-only and optional (Extras menu) — everywhere else it's silent."""
        if not sounds_var.get():
            return
        try:
            import winsound
            sound = {"ok": winsound.MB_ICONASTERISK,
                     "partial": winsound.MB_OK}.get(kind, winsound.MB_ICONHAND)
            winsound.MessageBeep(sound)
        except Exception:  # noqa: BLE001 - sound is garnish, never a failure
            pass

    def _copy_path():
        path = state["path"]
        if not path:
            return
        root.clipboard_clear()
        root.clipboard_append(str(path))
        _status("Report path copied to the clipboard.", T.NAVY)

    def _open_report_folder():
        path = state["path"]
        folder = Path(path).parent if path else expected_output_path(_current_cfg()).parent
        _open(folder)

    def _start():
        if str(run_btn["state"]) == "disabled":
            return  # a run is already going (F5 / menu can bypass the button)
        cfg = _current_cfg()
        try:
            argv = build_argv(cfg)
        except ValueError as exc:
            _status(str(exc), T.STATUS_RED)
            return
        save_settings(_collect_settings())  # a run is a commitment: remember it
        state["path"] = expected_output_path(cfg)
        state["run_t0"] = time.monotonic()
        state["run_wall0"] = time.time()  # to tell a fresh report from stale
        state["last_prog"] = {}           # the current batch's progress snapshot
        state["odo_ok"] = 0               # ok-counts of batches already finished
        state["last_note"] = None         # most recent CLI status note (failure text lands here)
        cancel_event.clear()
        run_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")
        open_btn.configure(state="disabled")
        folder_btn.configure(state="disabled")
        copy_btn.configure(state="disabled")
        _status("Running... big universes take a while - hit Cancel anytime.", T.NAVY)
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        _append(f"$ stock_review {' '.join(argv)}\n\n")

        # Flip the always-on screen from attract mode to the live progress HUD.
        _anim("start", idle=False)
        progress = QueueProgress(log_queue) if tk_anim is not None else None
        threading.Thread(target=_run_review,
                         args=(argv, log_queue, progress, cancel_event),
                         daemon=True).start()

    def _cancel():
        """Signal the worker to stop; it winds down the in-flight fetches and
        reports back through the queue, landing us in _finish()."""
        if str(cancel_btn["state"]) == "disabled":
            return  # nothing running (Esc / menu can fire any time)
        cancel_event.set()
        cancel_btn.configure(state="disabled")
        _status("Cancelling... finishing the fetches already in flight. "
                "A partial report will still be written.", T.NAVY)

    def _title_progress(p: dict):
        """Mirror run progress into the window title, so the taskbar shows it."""
        try:
            pct = int(100 * min(1.0, p["done"] / max(1, p["total"])))
            root.title(f"Stock Review — {pct}%  ({p['done']}/{p['total']})")
        except Exception:  # noqa: BLE001 - the title is decoration
            pass

    def _drain():
        try:
            while True:
                kind, payload = log_queue.get_nowait()
                if kind == "log":
                    _append(payload)  # type: ignore[arg-type]
                elif kind == "progress":
                    # Feed the odometer: a counter reset means a retry round
                    # started a new batch, so bank the finished batch's oks.
                    prev = state.get("last_prog") or {}
                    if (payload["done"] < prev.get("done", 0)
                            or payload["total"] != prev.get("total")):
                        state["odo_ok"] = (state.get("odo_ok", 0)
                                           + int(prev.get("ok", 0) or 0))
                    state["last_prog"] = payload
                    _anim("set_progress", **payload)  # type: ignore[arg-type]
                    _title_progress(payload)  # type: ignore[arg-type]
                elif kind == "retry":
                    _anim("add_retry", **payload)  # type: ignore[arg-type]
                    _append("  [retry] {ticker}: waiting {delay:.0f}s, then "
                            "attempt {attempt}/{max_retries}\n".format(**payload))
                elif kind == "note":
                    # Keep the most recent substantive note (e.g. cli.main's
                    # total-failure text) so a failed run's status bar can
                    # show the ACTUAL reason instead of a bare exit code.
                    state["last_note"] = payload
                    _anim("set_note", text=payload)
                elif kind == "done":
                    _finish(payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        finally:
            # In finally so a one-off error in a handler above (a bad payload,
            # a widget mid-teardown) can't kill the pump for the whole session.
            root.after(100, _drain)

    def _finish(result: dict):
        # Return the always-on screen to attract mode (don't stop it).
        _anim("start", idle=True)
        run_btn.configure(state="normal")
        cancel_btn.configure(state="disabled")
        elapsed = (time.monotonic() - state["run_t0"]
                   if state["run_t0"] is not None else 0)
        state["run_t0"] = None
        timer_var.set("idle")
        root.title("Stock Review")

        def _summary() -> str:
            """'198 ok, 2 failed, 3:42' — from the batches this run pushed."""
            prog = state.get("last_prog") or {}
            n_ok = int(state.get("odo_ok", 0)) + int(prog.get("ok", 0) or 0)
            n_fail = int(prog.get("failed", 0) or 0)
            bits = f"{n_ok} ok" + (f", {n_fail} failed" if n_fail else "")
            return f"{bits}, {int(elapsed) // 60}:{int(elapsed) % 60:02d}"
        # A cancelled run writes a PARTIAL report from whatever it fetched (see
        # cli.main), so check for that first. The worker derives ``cancelled``
        # from cli.main's own 130 exit code — more truthful than re-reading
        # cancel_event here, which a late Cancel click can set AFTER the run
        # already completed successfully. A file older than this run doesn't
        # count — today's earlier report would otherwise pass for the partial.
        if result.get("cancelled"):
            path = state["path"]
            try:
                wrote = (path is not None and
                         Path(path).stat().st_mtime >= state.get("run_wall0", 0) - 2)
            except OSError:
                wrote = False
            if wrote:
                open_btn.configure(state="normal")
                folder_btn.configure(state="normal")
                copy_btn.configure(state="normal")
                _remember_report(path)
                _bump_odometer()
                # Amber, not navy: navy means "still busy" (Running.../Cancelling...);
                # this run is DONE, just partially. A quiet neutral beep (not the
                # full success chime, not the error buzz) matches that in-between.
                _status(f"Cancelled - partial report written ({_summary()}). "
                        "Rerun the same setup to fill the gaps.", T.GOLD)
                _jingle("partial")
            else:
                _status("Cancelled - nothing fetched yet, so nothing was written.",
                        T.GOLD)
            return
        code = result.get("code", 1)
        if code == 0:
            prog = state.get("last_prog") or {}
            n_fail = int(prog.get("failed", 0) or 0)
            if n_fail:
                # A partial-failure run isn't the same as a clean one: a fixed,
                # measured lead line and an amber tone instead of a cheerful
                # random pick from DONE_MSGS.
                lead, color = "Partial success - some tickers failed.", T.GOLD
            else:
                lead, color = random.choice(DONE_MSGS), T.TITLE_GREEN
            _status(f"{lead}  ({_summary()})  {DONE_TAGLINE}", color)
            _jingle("ok")
            path = state["path"]
            if path and Path(path).exists():
                open_btn.configure(state="normal")
                folder_btn.configure(state="normal")
                copy_btn.configure(state="normal")
                _remember_report(path)
            _bump_odometer()
        else:
            # cli.main's total-failure branch pushes a _note() with the ACTUAL
            # reason (rate-limited, network down, ...) right before returning;
            # _drain banks it as state["last_note"]. Prefer that over a bare
            # exit code whenever it's available.
            last_note = state.get("last_note")
            if last_note:
                _status(last_note, T.STATUS_RED)
            else:
                msg = result.get("error") or f"exit code {code}"
                _status(f"Finished with errors ({msg}).", T.STATUS_RED)
            if not details_var.get():
                details_var.set(True)
                _toggle_details()
            _jingle("bad")

    # ---- dialogs: About & Tip of the Day ------------------------------------ #
    _LOGO_MINI = "\n".join((
        "        |",
        "    |  [#]   |",
        "   [#]  |   [#]",
        "    |      [#]",
        "  __|_______|__",
    ))
    _BULB = "\n".join((
        "  \\  |  /",
        "   .---.",
        "  /     \\",
        "  \\     /",
        "   |___|",
        "   |###|",
        "    '='",
    ))
    _BALL = "\n".join((
        "       .--~~~--.",
        "     .'  *   .   '.",
        "    /   .       *  \\",
        "   |  *    (8)      |",
        "    \\    .      *  /",
        "     '.   *   .  .'",
        "       '--.__.--'",
        "      ___|    |___",
        "     (____________)",
    ))

    def _show_about():
        win, body = T.dialog98(root, "About Stock Review", " About Stock Review")
        crt = tk.Frame(body, bg=T.CRT_BG, relief="sunken", bd=2)
        crt.pack(fill="x")
        tk.Label(crt, text=_LOGO_MINI, bg=T.CRT_BG, fg="#3bfb3b",
                 font=("Consolas", 10, "bold"), justify="left").pack(padx=16, pady=8)
        T.label98(body, text=f"Stock Review {APP_VERSION}",
                  font=T.UI_FONT_LG).pack(pady=(8, 0))
        T.label98(body, text="pick · run · read",
                  fg=T.NAVY).pack()
        T.label98(body, text=f"Created by {APP_AUTHOR}",
                  font=T.UI_FONT_B, fg=T.TITLE_PURPLE).pack(pady=(4, 0))
        T.label98(body, text=("A silly lil vibe-coded project, made for fun.\n"
                              "Not financial advice."),
                  justify="center").pack(pady=(4, 0))
        T.label98(body, text=(f"Odometer: {state.get('runs_total', 0)} runs · "
                              f"{state.get('tickers_total', 0):,} tickers "
                              "reviewed all-time"),
                  fg=T.TITLE_GREEN).pack(pady=(6, 0))
        T.label98(body, text=(f"Python {sys.version.split()[0]} · Tk {tk.TkVersion}\n"
                              f"Reports live in:  {BASE_DIR}"),
                  fg=T.GRAYTEXT, justify="center").pack(pady=(6, 0))
        T.button98(body, text="OK", command=win.destroy,
                   fill=T.BTN_BLUE).pack(pady=(10, 0))
        T.center_over(win, root)

    def _show_fortune():
        """Crystal Ball — a fortune teller. Zero predictive power,
        one hundred percent Win98 energy."""
        win, body = T.dialog98(root, "Crystal Ball", " Crystal Ball — Fortune Teller")
        crt = tk.Frame(body, bg=T.CRT_BG, relief="sunken", bd=2)
        crt.pack(fill="x")
        tk.Label(crt, text=_BALL, bg=T.CRT_BG, fg="#b58bff",
                 font=("Consolas", 10, "bold"), justify="left").pack(padx=24, pady=8)
        T.label98(body, text="You may ask the ball about stonks.",
                  fg=T.NAVY).pack(pady=(8, 0))
        fortune_txt = tk.StringVar()
        T.label98(body, textvariable=fortune_txt, font=T.UI_FONT_B,
                  wraplength=300, justify="center").pack(fill="x", pady=(4, 0))
        T.label98(body, text="Entertainment only - not financial advice.",
                  fg=T.GRAYTEXT).pack(pady=(6, 0))
        last = {"i": -1}

        def _ask():
            i = random.randrange(len(FORTUNES))
            if i == last["i"]:   # never the same fortune twice in a row
                i = (i + 1) % len(FORTUNES)
            last["i"] = i
            fortune_txt.set(FORTUNES[i])

        _ask()
        bottom = tk.Frame(body, bg=T.FACE)
        bottom.pack(fill="x", pady=(10, 0))
        T.button98(bottom, text="Close", command=win.destroy).pack(side="right")
        T.button98(bottom, text="Ask again", command=_ask,
                   fill=T.BTN_PURPLE).pack(side="right", padx=(0, 6))
        T.center_over(win, root)

    def _show_welcome():
        """One-time first-launch hello (tracked via 'welcomed' in settings)."""
        state["welcomed"] = True
        win, body = T.dialog98(root, "Welcome", " Thanks for installing!")
        crt = tk.Frame(body, bg=T.CRT_BG, relief="sunken", bd=2)
        crt.pack(fill="x")
        tk.Label(crt, text=_LOGO_MINI, bg=T.CRT_BG, fg="#3bfb3b",
                 font=("Consolas", 10, "bold"), justify="left").pack(padx=16, pady=8)
        T.label98(body, text="Thanks for installing Stock Review!",
                  font=T.UI_FONT_LG).pack(pady=(8, 0))
        T.label98(body, text=("This is a silly lil vibe-coded project made for fun —\n"
                              "no warranties, no financial advice, just ASCII art\n"
                              "and fresh numbers. Have a poke around!"),
                  justify="center").pack(pady=(4, 0))
        T.label98(body, text=f"— {APP_AUTHOR}", font=T.UI_FONT_B,
                  fg=T.TITLE_PURPLE).pack(pady=(2, 0))

        def _ok():
            save_settings(_collect_settings())  # never show the welcome again
            win.destroy()

        T.button98(body, text="Let's go!", command=_ok,
                   fill=T.BTN_GREEN).pack(pady=(10, 0))
        win.protocol("WM_DELETE_WINDOW", _ok)
        # Override dialog98's default Escape=destroy: this dialog's close has
        # to persist "seen" first, or Escape would silently skip that and the
        # welcome would show again next launch.
        win.bind("<Escape>", lambda _e: _ok())
        T.center_over(win, root)

    try:
        state["tip_index"] = int(saved.get("tip_index", 0)) % len(TIPS)
    except (TypeError, ValueError):
        state["tip_index"] = 0

    def _show_tip_dialog():
        win, body = T.dialog98(root, "Tip of the Day", " Tip of the Day")
        row = tk.Frame(body, bg=T.FACE)
        row.pack(fill="both", expand=True)
        bulb_box = tk.Frame(row, bg=T.FIELD, relief="sunken", bd=2)
        bulb_box.pack(side="left", fill="y", padx=(0, 10))
        tk.Label(bulb_box, text=_BULB, bg=T.FIELD, fg=T.GOLD,
                 font=("Consolas", 9, "bold"), justify="left").pack(padx=8, pady=8)
        right = tk.Frame(row, bg=T.FACE)
        right.pack(side="left", fill="both", expand=True)
        T.label98(right, text="Did you know...",
                  font=T.UI_FONT_LG, fg=T.TITLE_PURPLE,
                  anchor="w").pack(fill="x")
        tip_txt = tk.StringVar()
        T.label98(right, textvariable=tip_txt, wraplength=300, justify="left",
                  anchor="nw").pack(fill="both", expand=True, pady=(6, 0))

        def _advance():
            i = int(state.get("tip_index", 0)) % len(TIPS)
            tip_txt.set(TIPS[i])
            state["tip_index"] = (i + 1) % len(TIPS)  # next launch: next tip

        _advance()
        bottom = tk.Frame(body, bg=T.FACE)
        bottom.pack(fill="x", pady=(10, 0))
        T.check98(bottom, "Show tips at startup", tips_var).pack(side="left")
        T.button98(bottom, text="Close", command=win.destroy).pack(side="right")
        T.button98(bottom, text="Next Tip", command=_advance,
                   fill=T.BTN_PURPLE).pack(side="right", padx=(0, 6))
        T.center_over(win, root)

    # ---- splash screen (every good 90s program made you wait a moment) ------ #
    def _show_splash(done) -> None:
        """A brief CRT-style splash before the window appears; click, Space,
        or Enter skips it. Calls ``done()`` exactly once after the main
        window is shown. Only the very first launch (paired with the
        one-time welcome dialog) gets the full-length splash — every launch
        after that gets a much shorter flourish instead of the same two
        stacked modals on every single relaunch."""
        root.withdraw()
        sp = tk.Toplevel(root, bg=T.DARKEDGE)
        sp.overrideredirect(True)
        try:
            sp.attributes("-topmost", True)
        except tk.TclError:
            pass
        inner = tk.Frame(sp, bg=T.CRT_BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, text=_LOGO_MINI, bg=T.CRT_BG, fg="#3bfb3b",
                 font=("Consolas", 11, "bold"), justify="left").pack(
            padx=30, pady=(16, 6))
        tk.Label(inner, text=f"S T O C K   R E V I E W   {APP_VERSION}",
                 bg=T.CRT_BG, fg="#3bfb3b",
                 font=("Consolas", 12, "bold")).pack()
        tk.Label(inner, text=f"by {APP_AUTHOR}  ·  warming up the money printer...",
                 bg=T.CRT_BG, fg=T.CRT_FG, font=("Consolas", 8)).pack(pady=(3, 16))
        sp.update_idletasks()
        x = (sp.winfo_screenwidth() - sp.winfo_reqwidth()) // 2
        y = (sp.winfo_screenheight() - sp.winfo_reqheight()) // 3
        sp.geometry(f"+{x}+{y}")
        # An override-redirect Toplevel bypasses the window manager, so it
        # doesn't automatically receive keyboard focus the way a normal
        # window would — force it so Space/Enter actually reach the bindings
        # below instead of going nowhere.
        sp.focus_force()
        fired = {"done": False}

        def _dismiss(_e=None):
            if fired["done"]:
                return
            fired["done"] = True
            try:
                sp.destroy()
            except tk.TclError:
                pass
            root.deiconify()
            done()

        sp.bind("<Button-1>", _dismiss)
        sp.bind("<space>", _dismiss)
        sp.bind("<Return>", _dismiss)
        sp.bind("<KP_Enter>", _dismiss)
        # An override-redirect window bypasses the window manager, so a
        # WM_DELETE_WINDOW protocol handler may simply never fire on it.
        # <Destroy> catches every teardown path (fired-flag makes _dismiss
        # re-entry safe); the protocol handler stays as harmless extra cover.
        sp.bind("<Destroy>", _dismiss)
        sp.protocol("WM_DELETE_WINDOW", _dismiss)
        first_launch = not saved.get("welcomed")
        sp.after(1500 if first_launch else 400, _dismiss)

    # ---- easter egg: the DOS title bar is a channel dial -------------------- #
    def _flip_channel(_e=None):
        title = _anim("next_scene")
        if title:
            _status(f"Channel flipped - now showing '{title}'.", T.NAVY)

    dos_title.bind("<Double-Button-1>", _flip_channel)

    # ---- close = remember everything (and never strand a running fetch) ----- #
    closing = {"armed": False}

    def _on_close():
        save_settings(_collect_settings())
        # Closing mid-run gets the same graceful path as Cancel: signal the
        # worker, let it write its partial report, and only then destroy —
        # a bare destroy() would strand the fetch threads and lose everything
        # already fetched. A second click on X (or a hung worker after the
        # timeout) force-closes.
        if str(run_btn["state"]) == "disabled" and not closing["armed"]:
            closing["armed"] = True
            cancel_event.set()
            cancel_btn.configure(state="disabled")
            _status("Closing - cancelling the run and saving the partial "
                    "report first (click X again to force quit)...", T.NAVY)
            deadline = time.monotonic() + 10.0

            def _poll_closed():
                # _drain keeps pumping; _finish re-enables Run when "done" lands.
                if (str(run_btn["state"]) != "disabled"
                        or time.monotonic() > deadline):
                    root.destroy()
                else:
                    root.after(100, _poll_closed)

            _poll_closed()
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    # ---- menu bar (File / Extras / Help, silver with navy highlight) -------- #
    menubar = T.menu98(root)
    m_file = T.menu98(menubar)
    m_file.add_command(label="Run review", accelerator="F5", command=_start)
    m_file.add_command(label="Cancel run", accelerator="Esc", command=_cancel)
    m_file.add_separator()
    m_file.add_command(label="Open last report", accelerator="Ctrl+O",
                       command=lambda: _open(state["path"]))
    m_file.add_command(label="Open report folder", accelerator="Ctrl+Shift+O",
                       command=_open_report_folder)
    m_file.add_command(label="Copy report path", accelerator="Ctrl+L",
                       command=_copy_path)

    def _open_recent(p: str) -> None:
        if Path(p).exists():
            _open(p)
        else:
            _status(f"That report has moved or been deleted: {Path(p).name}",
                    T.STATUS_RED)

    def _fill_recent() -> None:
        """Rebuild the Recent reports submenu each time it posts."""
        m_recent.delete(0, "end")
        recent = state.get("recent") or []
        if not recent:
            m_recent.add_command(label="(no reports yet)", state="disabled")
            return
        for p in recent:
            pp = Path(p)
            m_recent.add_command(label=f"{pp.name}   ({pp.parent.name})",
                                 command=lambda q=p: _open_recent(q))
        m_recent.add_separator()
        m_recent.add_command(label="Clear list",
                             command=lambda: state.update(recent=[]))

    m_recent = T.menu98(m_file, postcommand=_fill_recent)
    m_file.add_cascade(label="Recent reports", menu=m_recent)
    m_file.add_separator()
    m_file.add_command(label="Exit", accelerator="Alt+F4", command=_on_close)
    menubar.add_cascade(label="File", menu=m_file)

    # ---- Quick picks: one click fills the whole form ------------------------ #
    # Third element is the keyboard-mnemonic index into the label (Alt, arrow to
    # Quick picks, then the underlined letter jumps straight to that preset).
    # Chosen to be distinct across all seven labels — "ASX..." and "Aussie..."
    # both start with A, so the latter's mnemonic sits on the 'm' of "miners".
    presets = [
        ("ASX 200 (Australia)", {"mode": "source", "source": "australia"}, 0),
        ("S&P 500 (US)", {"mode": "source", "source": "sp500"}, 0),
        ("US tech", {"mode": "sector", "sector": "technology", "region": "us"}, 0),
        ("Aussie miners", {"mode": "sector", "sector": "mining",
                           "region": "australia"}, 7),
        ("Global small caps", {"mode": "source", "source": "global-smallcap"}, 0),
        ("Quick taste: 10 of the S&P 500", {"mode": "source", "source": "sp500",
                                            "limit": "10"}, 0),
        ("Curated shortlist", {"mode": "curated"}, 0),
    ]

    def _apply_preset(label: str, p: dict) -> None:
        """Fill the form from a preset. Limit is part of the preset (usually
        blank), so a leftover '10' never silently truncates a full run — that
        reset is deliberate (see the wipe note below), not an oversight."""
        had_limit = bool(limit_var.get().strip())
        new_limit = p.get("limit", "")
        mode_var.set(p.get("mode", "curated"))
        if "source" in p:
            source_var.set(p["source"])
        if "sector" in p:
            sector_var.set(p["sector"])
            region_var.set(p.get("region", ""))
        limit_var.set(new_limit)
        _sync()
        wiped_note = (" (Limit cleared by preset.)"
                     if had_limit and not new_limit else "")
        _status(f"Quick pick: {label}. Hit Run review (F5) when ready.{wiped_note}",
                T.NAVY)

    m_picks = T.menu98(menubar)
    for _label, _p, _underline in presets:
        # a preset survives only if its universe still exists in this build
        if _p.get("source") and _p["source"] not in sources:
            continue
        if _p.get("sector") and _p["sector"] not in sectors:
            continue
        m_picks.add_command(label=_label, underline=_underline,
                            command=lambda l=_label, p=_p: _apply_preset(l, p))
    menubar.add_cascade(label="Quick picks", menu=m_picks, underline=0)

    m_extras = T.menu98(menubar)
    m_extras.add_checkbutton(label="Show CRT/ASCII panel", variable=crt_var,
                             command=_toggle_crt)
    m_extras.add_checkbutton(label="Completion sounds", variable=sounds_var)
    m_extras.add_checkbutton(label="Tip of the Day at startup", variable=tips_var)
    m_extras.add_separator()
    m_extras.add_command(label="Ask the Crystal Ball...", accelerator="Ctrl+8",
                         command=_show_fortune)
    m_extras.add_command(label="Flip ASCII channel  (or double-click the DOS bar)",
                         command=_flip_channel)
    menubar.add_cascade(label="Extras", menu=m_extras)
    m_help = T.menu98(menubar)
    m_help.add_command(label="Tip of the Day...", command=_show_tip_dialog)
    m_help.add_command(label="About Stock Review...", command=_show_about)
    menubar.add_cascade(label="Help", menu=m_help)
    root.config(menu=menubar)

    # ---- keyboard shortcuts -------------------------------------------------- #
    root.bind("<F5>", lambda _e: _start())
    root.bind("<Return>", lambda _e: _start())
    root.bind("<Escape>", lambda _e: _cancel())
    root.bind("<Control-o>", lambda _e: _open(state["path"]))
    root.bind("<Control-Shift-O>", lambda _e: _open_report_folder())
    root.bind("<Control-l>", lambda _e: _copy_path())
    root.bind("<Control-d>",
              lambda _e: (details_var.set(not details_var.get()), _toggle_details()))
    root.bind("<Control-Key-8>", lambda _e: _show_fortune())  # the 8-ball key

    _sync()
    _toggle_details(grow=False)  # restore the persisted Details pane state
    _toggle_crt()                # restore the persisted CRT-panel visibility
    _tick_clock()
    _anim("start", idle=True)    # kick off attract mode immediately
    root.after(100, _drain)

    # The startup ritual — splash first, then a one-time welcome on the very
    # first launch or the rotating Tip of the Day after that. All of it is
    # skipped in the headless smoke test.
    def _after_splash():
        if not saved.get("welcomed"):
            root.after(300, _show_welcome)
        elif tips_var.get():
            root.after(300, _show_tip_dialog)

    if test_close_ms is None:
        _show_splash(_after_splash)
    else:
        root.after(test_close_ms, root.destroy)
    root.mainloop()
    return 0


def _selftest() -> int:
    """Headless end-to-end check used to smoke-test the frozen .exe.

    Runs a one-ticker review through the real worker path (exercising the whole
    frozen import chain: yfinance, curl_cffi, pandas, ...) and writes the outcome
    to ``selftest_result.txt`` next to the exe. Triggered by the environment
    variable ``STOCK_REVIEW_SELFTEST=1`` so it needs no console window."""
    q: "queue.Queue[tuple[str, object]]" = queue.Queue()
    out = BASE_DIR / "reports" / "_selftest.md"
    _run_review(["AAPL", "--no-cache", "--no-animation", "--out", str(out)], q)
    lines, code = [], 1
    while not q.empty():
        kind, payload = q.get()
        if kind == "log":
            lines.append(str(payload))
        elif kind == "done":
            code = int(payload.get("code", 1))  # type: ignore[union-attr]
    (BASE_DIR / "selftest_result.txt").write_text(
        f"exit={code}\nreport_exists={out.exists()}\n\n" + "".join(lines),
        encoding="utf-8")
    return code


def main(argv=None) -> int:  # argv kept for a uniform entry-point signature
    if env_flag("STOCK_REVIEW_SELFTEST"):
        return _selftest()
    # Build-verification hook: open the real window, auto-close after N ms.
    # Lets a script smoke-test the frozen GUI exe without a hanging mainloop.
    smoke_ms = env_flag("STOCK_REVIEW_SMOKE_MS")
    if smoke_ms:
        return run(test_close_ms=max(200, smoke_ms))
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
