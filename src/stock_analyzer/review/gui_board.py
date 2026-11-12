"""Live board: the table-first Stock Review window ("the ledger").

The main window of the Windows build; the Win98 one in :mod:`.gui` is the
Classic edition. It reuses that module's worker plumbing (``build_argv`` ->
``cli.main`` on a thread, fed through a queue) and only replaces the
*presentation*:

* a sidebar to pick what to review and reopen recent reports;
* a command header: what will run, where the file lands, and the Run button;
* one status band in a fixed slot that reads idle / running / done;
* filter chips that double as the live counts, and a find box;
* a per-ticker table that fills in live (see :mod:`.gui_board_table`);
* an options drawer, a log panel and a keyboard-hint footer;
* dark and light themes (see :mod:`.gui_board_theme`).

Run it with ``python stock_review_board.py`` or ``python -m
stock_analyzer.review.gui_board``. stdlib-only (tkinter + ttk), like the rest of
the GUI, so PyInstaller can still freeze it.

Everything on screen is real data: the table rows come from the run itself, the
header tape shows the 1-month return of tickers as they are fetched, and a
missing value is simply left out (never 0, never a guess). No score, no advice.

The pure parts (``classify_error``, ``BoardModel``, the filters, sorters and
formatters) carry no Tk so ``tests/test_gui_board.py`` can cover them offline.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import gui as _gui
from . import gui_board_theme as _theme
from .format import NA, _num, pct, price as fmt_price
from .gui import QueueProgress, build_argv, expected_output_path, universe_hint
from .gui_board_table import BoardTable, Cell, Column, Mosaic, draw_mark
from .symbols import clean_symbol
from .universe import DEFAULT_TICKERS, REGION_RECORDS, SECTORS, SOURCES

APP_NAME = "Stock Review"
BOARD_SETTINGS = _gui.BASE_DIR / "gui_board_settings.json"

# --------------------------------------------------------------------------- #
# Pure model  (no Tk)
# --------------------------------------------------------------------------- #
_RATE_WORDS = ("ratelimit", "rate limit", "429", "too many requests")
_MISSING_WORDS = ("404", "not found", "no data", "delisted", "no price data")


def classify_error(err) -> tuple[str, str]:
    """Turn a raw fetch error into (kind, plain-English text).

    kind is ``rate`` (throttled, worth a rerun), ``missing`` (the symbol does not
    resolve) or ``error`` (anything else, shown verbatim but shortened)."""
    text = str(err or "").strip()
    low = text.lower()
    if any(k in low for k in _RATE_WORDS):
        return "rate", "Rate-limited"
    if any(k in low for k in _MISSING_WORDS):
        return "missing", "Not found"
    first = text.splitlines()[0] if text else "fetch failed"
    if len(first) > 70:
        first = first[:69] + "…"
    return "error", f"Failed: {first}"


def _known(value) -> str:
    """A text field from a fetch record, or "" when Yahoo had nothing (the
    record uses the ``n/a`` sentinel for that)."""
    text = str(value or "").strip()
    return "" if text == NA else text


def fmt_clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def eta_text(done: int, total: int, elapsed: float) -> str:
    """'about 10 s left', or '' while there is too little data to say."""
    if done < 2 or total <= done or elapsed < 2:
        return ""
    left = elapsed / done * (total - done)
    if left >= 90:
        return f"about {round(left / 60)} min left"
    return f"about {max(1, round(left / 5) * 5)} s left"


@dataclass
class Row:
    ticker: str
    name: str = ""
    state: str = "queued"   # queued | ok | retry | fail
    detail: str = "Queued"
    kind: str = ""          # for fail rows: rate | missing | error
    ret_1m: float | None = None
    sector: str = ""        # "" = unknown (never a guess)
    price: float | None = None   # in the listing currency below
    currency: str = ""


class BoardModel:
    """Everything the table, hero and result card show, derived from the run's
    own events. Counts are cumulative across the whole run, so a retry round
    (whose batch is only the failures) never resets 'ok' to zero."""

    def __init__(self) -> None:
        self.reset()

    def reset(self, tickers=()) -> None:
        self.rows: dict[str, Row] = {}
        self.total = 0
        self.note: str | None = None
        self.wait: dict[str, float] = {}      # ticker -> retry delay (s)
        self._ok_order: list[str] = []
        self.preload(tickers)

    def preload(self, tickers) -> None:
        for t in tickers:
            if t not in self.rows:
                self.rows[t] = Row(t, detail="Will review")
        self.total = max(self.total, len(self.rows))

    def _row(self, ticker: str) -> Row:
        row = self.rows.get(ticker)
        if row is None:
            row = self.rows[ticker] = Row(ticker)
        return row

    def on_progress(self, d: dict) -> None:
        row = self._row(d.get("ticker", ""))
        # A retry round's batch only holds the failures, so its total is smaller;
        # never shrink the whole-run total.
        self.total = max(self.total, int(d.get("total") or 0), len(self.rows))
        self.wait.pop(row.ticker, None)
        err = d.get("error")
        if err:
            row.state, (row.kind, row.detail) = "fail", classify_error(err)
        else:
            row.state, row.kind, row.detail = "ok", "", "Fetched"
            row.name = d.get("name") or row.name
            row.ret_1m = _num(d.get("ret_1m"))
            row.sector = _known(d.get("sector"))
            row.price = _num(d.get("price"))
            row.currency = _known(d.get("currency")) if row.price is not None else ""
            if row.ticker in self._ok_order:
                self._ok_order.remove(row.ticker)
            self._ok_order.append(row.ticker)

    def on_retry(self, d: dict) -> None:
        row = self._row(d.get("ticker", ""))
        delay = float(d.get("delay") or 0)
        row.state, row.kind = "retry", ""
        row.detail = (f"Retrying, attempt {d.get('attempt', '?')} of "
                      f"{d.get('max_retries', '?')} in {delay:.0f} s")
        self.wait[row.ticker] = delay
        self.total = max(self.total, len(self.rows))

    def on_note(self, text) -> None:
        self.note = text or None

    # -- derived -------------------------------------------------------------
    @property
    def in_retry_round(self) -> bool:
        return bool(self.note and self.note.lower().startswith("retry round"))

    def counts(self) -> dict:
        """Cumulative whole-run counts. Between the CLI's retry rounds the failed
        tickers are still being worked on, so they read as 'retrying', not as
        'failed' (they only become failed once the rounds are over)."""
        c = {"ok": 0, "retry": 0, "fail": 0, "queued": 0}
        for r in self.rows.values():
            c[r.state] += 1
        c["fail_raw"] = c["fail"]   # processed-and-failed, for the progress bar
        if self.in_retry_round:
            c["retry"] += c["fail"]
            c["fail"] = 0
        c["done"] = c["ok"] + c["fail"]
        c["total"] = max(self.total, len(self.rows))
        return c

    def fraction(self) -> float:
        c = self.counts()
        return min(1.0, (c["ok"] + c["fail_raw"]) / c["total"]) if c["total"] else 0.0

    def failed(self) -> list[Row]:
        return [r for r in self.rows.values() if r.state == "fail"]

    def headline(self) -> str:
        c = self.counts()
        if not c["total"]:
            return "Starting up"
        head = f"{c['done']} of {c['total']} in"
        if c["retry"]:
            head += f", {c['retry']} retrying"
        return head

    def mood(self) -> str:
        """One plain sentence about what is happening right now."""
        c = self.counts()
        if self.note and self.note.lower().startswith("retry round"):
            return self.note[0].upper() + self.note[1:]
        if self.wait:
            t = min(self.wait, key=self.wait.get)
            more = f" (and {len(self.wait) - 1} more)" if len(self.wait) > 1 else ""
            return f"{t} is slow to answer{more}, trying again shortly."
        if c["fail"]:
            return f"{c['fail']} could not be fetched so far. Failures are listed at the end."
        if c["done"]:
            return "Fetching. Rate limits are normal, the app waits and retries for you."
        return "Looking up the list and fetching prices."

    def tape(self, n: int = 8) -> list[tuple[str, float]]:
        """(ticker, 1-month return as a fraction) for the latest fetched names.
        Tickers whose return is unknown are skipped, never shown as 0."""
        out = []
        for t in reversed(self._ok_order):
            r = self.rows[t].ret_1m
            if r is not None:
                out.append((t, r))
            if len(out) >= n:
                break
        return list(reversed(out))


class BoardProgress(QueueProgress):
    """QueueProgress that also receives each ticker's record (``wants_rec``) so
    the table can show names, errors and returns."""

    wants_rec = True

    def update(self, done, *, ticker="", ok=0, failed=0, rec=None) -> None:
        msg = {"done": done, "total": self.total, "ok": ok, "failed": failed,
               "ticker": ticker}
        if rec:
            msg.update(error=rec.get("error"), name=rec.get("name"),
                       ret_1m=rec.get("ret_1m"), sector=rec.get("sector"),
                       price=rec.get("price"), currency=rec.get("currency"))
        self._q.put(("progress", msg))


# --------------------------------------------------------------------------- #
# Settings + recent reports  (pure, no Tk)
# --------------------------------------------------------------------------- #
def load_board_settings() -> dict:
    try:
        data = json.loads(BOARD_SETTINGS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - missing/corrupt settings are not an error
        return {}


def save_board_settings(data: dict) -> None:
    """Best-effort atomic write; same shape as ``gui.save_settings``."""
    tmp = BOARD_SETTINGS.with_name(
        f"{BOARD_SETTINGS.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, BOARD_SETTINGS)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def recent_reports(limit: int = 3, folder: Path | None = None) -> list[dict]:
    """Newest report files in the reports folder: label, detail and path."""
    folder = folder or (_gui.BASE_DIR / "reports")
    try:
        files = [p for p in folder.glob("stock_review_*.*")
                 if p.suffix in (".md", ".csv", ".json") and p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out = []
    today = date.today()
    for p in files[:limit]:
        st = p.stat()
        when = datetime.fromtimestamp(st.st_mtime)
        days = (today - when.date()).days
        label = (f"Today {when:%H:%M}" if days == 0 else
                 "Yesterday" if days == 1 else f"{when:%b} {when.day}")
        out.append({"label": label, "detail": f"{p.suffix[1:]} · {st.st_size // 1024} KB",
                    "path": p})
    return out


def mode_title(cfg: dict) -> str:
    mode = cfg.get("mode", "curated")
    if mode == "curated":
        return "Curated starter list"
    if mode == "source":
        return source_name(cfg.get("source")) or "Region"
    if mode == "sector":
        region = (cfg.get("region") or "").strip()
        return (f"{sector_name(cfg.get('sector')) or '…'} sector"
                + (f" · {source_name(region)}" if region else ""))
    if mode == "custom":
        return "Your tickers"
    return "Tickers from a file"


def preview_tickers(cfg: dict) -> list[str] | None:
    """The list a run would review, when it is known WITHOUT the network
    (curated, typed tickers, a file). None = it is looked up when Run is pressed."""
    mode = cfg.get("mode", "curated")
    if mode == "curated":
        return list(DEFAULT_TICKERS)
    if mode == "custom":
        raw = (cfg.get("tickers") or "").replace(",", " ").split()
        seen: list[str] = []
        for t in (clean_symbol(x) for x in raw):
            if t and t not in seen:
                seen.append(t)
        return seen
    if mode == "file":
        path = (cfg.get("file") or "").strip()
        if path and Path(path).is_file():
            try:
                from .universe import read_tickers_file
                return [clean_symbol(t) for t in read_tickers_file(Path(path))]
            except Exception:  # noqa: BLE001 - a bad file just means no preview
                return []
        return []
    return None


# --------------------------------------------------------------------------- #
# Names, filters, sorting, validation, summaries  (pure, no Tk)
# --------------------------------------------------------------------------- #
MODES = (("curated", "Starter list", f"{len(DEFAULT_TICKERS)} hand-picked stocks"),
         ("source", "Region", "An index or a whole market"),
         ("sector", "Sector", "One industry, across regions"),
         ("custom", "My tickers", "Type your own list"),
         ("file", "From a file", "A .txt or .csv of tickers"))
MODE_KEYS = tuple(m[0] for m in MODES)

FORMAT_NAMES = {"md": "Markdown", "csv": "CSV", "json": "JSON"}

# Short display names for the canonical --source universes. A universe added to
# review/universe.py without a name here still works (it shows its key);
# tests/test_gui_board.py fails until it gets one, so it is not forgotten.
SOURCE_NAMES = {
    "sp500": "S&P 500", "sp400": "S&P MidCap 400", "sp600": "S&P SmallCap 600",
    "sp1500": "S&P 1500 (all US)", "america-smallcap": "US small caps",
    "australia": "ASX 200", "asx-all": "Every ASX listing",
    "australia-smallcap": "Australian small caps",
    "japan": "Nikkei 225 (Japan)", "uk": "FTSE 100 (UK)", "germany": "DAX (Germany)",
    "hongkong": "Hang Seng (Hong Kong)", "canada": "TSX 60 (Canada)",
    "india": "NIFTY 50 (India)", "global": "Global: US, Australia, majors",
    "global-smallcap": "Global small caps", "international": "International (non-US)",
    "everything": "Everything",
}
SECTOR_NAMES = {"realestate": "Real estate"}
ALL_REGIONS = "All regions (US + Australia)"


def canonical_source(key) -> str:
    """Fold a --source alias ('america', 'asx200') onto its canonical name."""
    key = str(key or "").strip().lower()
    return _gui._SOURCE_ALIAS.get(key, key)


def source_name(key) -> str:
    key = canonical_source(key)
    return SOURCE_NAMES.get(key, key)


def sector_name(key) -> str:
    key = str(key or "").strip()
    return SECTOR_NAMES.get(key, key.capitalize())


def source_choices(pool=None) -> list[tuple[str, str]]:
    """(key, display name) for every canonical universe in ``pool`` (default: all
    --source names), in the order of the hint table, 'curated' left out because
    it has its own sidebar entry."""
    pool = SOURCES if pool is None else pool
    canon = {canonical_source(k) for k in pool} - {"curated"}
    ordered = [k for k in _gui._SOURCE_HINTS if k in canon]
    ordered += sorted(canon - set(ordered))
    return [(k, source_name(k)) for k in ordered]


def region_choices() -> list[tuple[str, str]]:
    """Regions a sector can be limited to; '' means the default (US + Australia)."""
    return [("", ALL_REGIONS)] + source_choices(REGION_RECORDS)


def sector_choices() -> list[tuple[str, str]]:
    return [(k, sector_name(k)) for k in sorted(SECTORS)]


def effective_state(row: Row, in_retry_round: bool = False) -> str:
    """A row's state as the counts read it: between the CLI's retry rounds a
    failed ticker is still being worked on, so it reads as 'retry'."""
    return "retry" if in_retry_round and row.state == "fail" else row.state


def filter_rows(rows, state: str = "all", query: str = "",
                in_retry_round: bool = False) -> list[Row]:
    """Rows matching the chip filter ('all' | 'ok' | 'retry' | 'fail') and the
    find box (case-insensitive, ticker or company name)."""
    q = (query or "").strip().casefold()
    out = []
    for r in rows:
        if state != "all" and effective_state(r, in_retry_round) != state:
            continue
        if q and q not in r.ticker.casefold() and q not in (r.name or "").casefold():
            continue
        out.append(r)
    return out


_STATE_RANK = {"fail": 0, "retry": 1, "queued": 2, "ok": 3}
SORT_KEYS = ("ticker", "name", "sector", "price", "ret", "status")


def sort_rows(rows, key: str | None = None, desc: bool = False,
              running: bool = False) -> list[Row]:
    """Order rows for display.

    No key: run order while running; afterwards failures first, so the rows
    that need attention are on top. With a key: sorted by that column, and a
    row with no value for it always sinks to the bottom (in either direction),
    so a missing number is never ranked as if it were 0."""
    rows = list(rows)
    if key not in SORT_KEYS:
        if running:
            return rows
        return sorted(rows, key=lambda r: {"fail": 0, "retry": 1}.get(r.state, 2))
    if key == "status":
        return sorted(rows, key=lambda r: (_STATE_RANK.get(r.state, 9), r.ticker),
                      reverse=desc)
    get = {"ticker": lambda r: r.ticker.casefold(),
           "name": lambda r: (r.name or "").casefold() or None,
           "sector": lambda r: (r.sector or "").casefold() or None,
           "price": lambda r: r.price,
           "ret": lambda r: r.ret_1m}[key]
    have = [r for r in rows if get(r) is not None]
    missing = [r for r in rows if get(r) is None]
    return sorted(have, key=get, reverse=desc) + missing


def validate_settings(settings: dict) -> dict[str, str]:
    """Field -> message for option values the CLI would refuse (mirrors the
    checks in ``gui.build_argv`` so they show while typing, not at Run)."""
    out = {}
    for field in ("limit", "workers"):
        raw = str(settings.get(field) or "").strip()
        if raw and (not (raw.isascii() and raw.isdigit()) or int(raw) <= 0):
            out[field] = "Use a positive whole number, or leave it blank."
    return out


def run_blocker(cfg: dict) -> str:
    """Why Run cannot start with this form state, or '' when it can."""
    try:
        build_argv(cfg)
    except ValueError as exc:
        return str(exc)
    return ""


def options_summary(settings: dict) -> str:
    """One short line describing the output options, for the command header."""
    bits = [FORMAT_NAMES.get(settings.get("fmt") or "md", "Markdown")]
    if settings.get("rich"):
        bits.append("rich")
    limit = str(settings.get("limit") or "").strip()
    bits.append(f"first {limit}" if limit else "every ticker")
    if settings.get("no_names"):
        bits.append("no company names")
    return " · ".join(bits)


def short_path(path, base=None) -> str:
    """A path short enough for one line: relative to the data folder when it is
    inside it, otherwise the last two parts."""
    p = Path(path)
    if base is not None:
        try:
            return str(p.relative_to(base))
        except ValueError:
            pass
    parts = p.parts
    return str(p) if len(parts) <= 3 else str(Path("…", *parts[-2:]))


_FAIL_LABEL = {"rate": "Rate-limited", "missing": "Not found"}


def failure_line(fails, budget: int = 100) -> str:
    """The failed tickers by name, grouped by reason, within ``budget``
    characters: 'Not found: XYZ.AX · Rate-limited: AAA, BBB +3 more'. Project
    rule 5: a failure is named, never silently dropped; the Failed filter lists
    every one of them in the table."""
    fails = list(fails)
    groups: dict[str, list[str]] = {}
    for r in fails:
        groups.setdefault(_FAIL_LABEL.get(r.kind, "Failed"), []).append(r.ticker)
    out, named = "", 0
    for label, names in groups.items():
        for k, name in enumerate(names):
            piece = (((" · " if out else "") + f"{label}: ") if k == 0 else ", ") + name
            if named and len(out) + len(piece) > budget:
                return out + f" +{len(fails) - named} more"
            out += piece
            named += 1
    return out


def finish_summary(*, counts: dict, fails, code: int, wrote: bool, cancelled: bool,
                   finish_early: bool, file_name: str, size_kb: int, elapsed: float,
                   error=None, note=None) -> dict:
    """What the status band says when a run ends. ``kind`` is 'ok' | 'warn' |
    'bad'; ``saved`` describes the file; ``fails`` is how many tickers failed
    (the band names them with :func:`failure_line`)."""
    fails = list(fails)
    if code not in (0, 130) or not wrote:
        return {"kind": "bad", "title": "No report this time", "saved": "",
                "sub": (error or note or "Nothing could be fetched. Check your "
                        "connection, wait a few minutes and run again; fetched "
                        "tickers are cached, so a rerun resumes.").strip(),
                "fails": len(fails), "actions": False, "retry": ""}
    total = counts.get("total") or counts.get("done") or 0
    title = f"{counts.get('ok', 0)} of {total} fetched"
    if fails:
        title += f" · {len(fails)} failed"
    if cancelled:
        title += " · " + ("finished early" if finish_early else "cancelled")
    return {"kind": "warn" if fails or cancelled else "ok", "title": title,
            "saved": f"{file_name} · {size_kb} KB · {fmt_clock(elapsed)}",
            "sub": "Saved. Paste it into any AI chat. Research screen, not advice.",
            "fails": len(fails), "actions": True,
            "retry": (f"Retry {len(fails)} failed" if fails
                      else "Run again" if cancelled else "")}


def hint_text(cfg: dict) -> str:
    """The plain-English line under the title. Reuses the Classic window's hint
    table; when a hint opens by repeating the title ("ASX 200 - Australia's top
    200" under the heading "ASX 200") only the part after it is kept."""
    hint = universe_hint(cfg.get("mode", "curated"), cfg.get("source", ""),
                         cfg.get("sector", ""), cfg.get("region", ""))
    if cfg.get("mode") == "source":
        lead = source_name(cfg.get("source")).split(" (")[0] + " - "
        if hint.startswith(lead):
            hint = hint[len(lead):]
    return hint[:1].upper() + hint[1:]


def mosaic_caption(n: int, shown: int) -> str:
    """The line under the mosaic. Says so when not every ticker has a square."""
    if not n:
        return "One square per ticker appears here"
    if shown < n:
        return f"First {shown:,} of {n:,} tickers"
    return "One square per ticker"


def fit_note(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def status_cell_text(row: Row) -> str:
    return row.detail or {"ok": "Fetched", "queued": "Queued"}.get(row.state, "")


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #
ROCKET = ("    /\\\n"
          "   /  \\\n"
          "  | () |\n"
          "  |    |\n"
          " /|____|\\\n"
          "   /##\\")
TRAIL = ("~=>", "-=>")          # the small rocket that rides the progress bar

COLUMNS = (Column("ticker", "Ticker", 96, mono=True),
           Column("name", "Name", 130, weight=2),
           Column("sector", "Sector", 80, weight=1),
           Column("price", "Price", 112, anchor="e", mono=True, sub=True),
           Column("ret", "1-month", 140, anchor="e", mono=True, bar=True),
           Column("status", "Status", 150, weight=2))

FILTERS = (("all", "All", ""), ("ok", "Fetched", "dot"),
           ("retry", "Retrying", "ring"), ("fail", "Failed", "cross"))

READY = "Ready · research screen, not financial advice"

KEY_HINTS = {
    "idle": "F5  Run      Ctrl+F  Find      Ctrl+,  Options      Ctrl+L  Log",
    "run": "Esc  Cancel      Ctrl+F  Find      Ctrl+L  Log",
    "done": "Ctrl+Shift+C  Copy for AI      Ctrl+O  Open report      F5  Run again",
    "bad": "F5  Run again      Ctrl+L  Log",
}


class BoardApp:
    """The Live board window. All widget work happens on the Tk thread; the
    worker only puts messages on ``self.q`` (drained by :meth:`drain`).

    The view is rebuilt from state by :meth:`build`, which is how a theme
    switch works: nothing a run produced lives only in a widget."""

    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.tk, self.root = tk, root
        root.title(APP_NAME)
        try:
            scaling = float(root.tk.call("tk", "scaling"))
        except Exception:  # noqa: BLE001 - cosmetic
            scaling = 96 / 72
        # Pixel constants below are written for 96 dpi and multiplied by this.
        self.scale = max(0.75, min(4.0, scaling / (96 / 72)))
        px = self.px
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(px(1240), sw - 40), min(px(760), sh - 80)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(px(960), w), min(px(560), h))
        icon = _gui._icon_path()
        if icon is not None:
            try:
                root.iconbitmap(default=str(icon))
            except Exception:  # noqa: BLE001 - icon is cosmetic (and Windows-only)
                pass

        families = tkfont.families(root)
        ui = _theme.pick_font(families, _theme.UI_FONTS, "TkDefaultFont")
        mono = _theme.pick_font(families, _theme.MONO_FONTS, "TkFixedFont")
        self.f = {"ui": (ui, 10), "small": (ui, 9), "bold": (ui, 10, "bold"),
                  "head": (ui, 9, "bold"), "h2": (ui, 12, "bold"),
                  "title": (ui, 17, "bold"), "brand": (ui, 11, "bold"),
                  "mono": (mono, 10), "mono_s": (mono, 9), "mono_b": (mono, 10, "bold"),
                  "art": (mono, 9)}
        self.line = {k: tkfont.Font(font=self.f[k]).metrics("linespace")
                     for k in ("ui", "h2", "small")}
        self._mono_s = tkfont.Font(font=self.f["mono_s"])

        saved = load_board_settings()
        self.settings = {  # the options drawer edits these
            "fmt": saved.get("fmt") if saved.get("fmt") in FORMAT_NAMES else "md",
            "workers": str(saved.get("workers", "6")),
            "limit": str(saved.get("limit", "")),
            "rich": bool(saved.get("rich", False)),
            "no_names": bool(saved.get("no_names", False)),
            "outdir": str(saved.get("outdir", "")),
            "theme": (saved.get("theme") if saved.get("theme") in _theme.THEME_CHOICES
                      else "auto"),
        }
        self.mode = saved.get("mode") if saved.get("mode") in MODE_KEYS else "curated"
        self.sources, self.regions = source_choices(), region_choices()
        self.sectors = sector_choices()
        src = canonical_source(saved.get("source", "america"))
        self.source = src if src in dict(self.sources) else self.sources[0][0]
        sec = saved.get("sector", "energy")
        self.sector = sec if sec in dict(self.sectors) else self.sectors[0][0]
        reg = canonical_source(saved.get("region", ""))
        self.region = reg if reg in dict(self.regions) else ""
        self.tickers_var = tk.StringVar(value=str(saved.get("tickers", "")))
        self.file_var = tk.StringVar(value=str(saved.get("file", "")))
        self.find_var = tk.StringVar()
        self.limit_var = tk.StringVar(value=self.settings["limit"])
        self.workers_var = tk.StringVar(value=self.settings["workers"])
        self.status_var = tk.StringVar(value=READY)
        self.status_kind = "muted"
        self.tickers_var.trace_add("write", lambda *_: self.refresh_preview())
        self.file_var.trace_add("write", lambda *_: self.refresh_preview())
        self.find_var.trace_add("write", lambda *_: self.refresh_table())
        self.limit_var.trace_add("write", lambda *_: self._on_number("limit", self.limit_var))
        self.workers_var.trace_add("write",
                                   lambda *_: self._on_number("workers", self.workers_var))

        self.model = BoardModel()
        self.screen, self.running = "idle", False
        self.t0, self.wall0 = None, 0.0
        self.q, self.cancel_ev, self.path = None, None, None
        self.finish_early_flag = False
        self.summary: dict | None = None
        self.tick = 0
        self.filter, self.sort_key, self.sort_desc = "all", None, False
        self.empty = ("", "")
        self.log_buf: list[str] = []
        self.log_open = self.log_unread = self.drawer_open = self.log_auto = False
        self.nav_hover = None
        self._band_built = None
        self.shell = None

        self.build()
        self.refresh_preview()

        root.bind("<F5>", self.start)
        root.bind("<Escape>", self._on_escape)
        root.bind("<Control-o>", lambda _e: self.open_report())
        root.bind("<Control-O>", lambda _e: self.open_folder())
        root.bind("<Control-l>", lambda _e: self.toggle_log())
        root.bind("<Control-Shift-C>", lambda _e: self.copy_for_ai())
        root.bind("<Control-Shift-c>", lambda _e: self.copy_for_ai())
        root.bind("<Control-f>", lambda _e: self.focus_find())
        root.bind("<Control-comma>", lambda _e: self.toggle_drawer())
        for i, key in enumerate(MODE_KEYS, 1):
            root.bind(f"<Control-Key-{i}>", lambda _e, k=key: self.set_mode(k))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.animate()

    # ====================================================================== #
    # small helpers
    # ====================================================================== #
    def px(self, n: float) -> int:
        return max(1, round(n * self.scale))

    def panel(self, parent, bg, border=None, **kw):
        """A bordered container. Tk paints a frame's border in ``highlightcolor``
        (black by default) whenever something inside it has the focus, so both
        colours are pinned to the border colour."""
        border = border or self.t.line
        return self.tk.Frame(parent, bg=bg, highlightthickness=1,
                             highlightbackground=border, highlightcolor=border, **kw)

    def label(self, parent, text="", fg=None, font=None, **kw):
        return self.tk.Label(parent, text=text, fg=fg or self.t.ink,
                             bg=kw.pop("bg", parent["bg"]), font=font or self.f["ui"],
                             anchor=kw.pop("anchor", "w"), **kw)

    def button(self, parent, text, command, kind="plain", font=None):
        """Flat button. kind: primary | plain | danger | ghost. A disabled
        button drops its fill and border, so its state does not rely on hue."""
        t, px = self.t, self.px
        base = parent["bg"]
        fg, bg, border, hover = {
            "primary": (t.accent_ink, t.accent, t.accent, t.accent_hover),
            "danger": (t.bad, base, t.bad, t.bad_bg),
            "ghost": (t.muted, base, base, t.hover),
        }.get(kind, (t.ink, base, t.line_strong, t.hover))
        b = self.tk.Button(
            parent, text=text, command=command, fg=fg, bg=bg,
            font=font or (self.f["ui"] if kind == "ghost" else self.f["bold"]),
            relief="flat", bd=0, padx=px(10 if kind == "ghost" else 14), pady=px(5),
            cursor="hand2", activebackground=hover, activeforeground=fg,
            highlightthickness=1, highlightbackground=border, highlightcolor=t.ink,
            disabledforeground=t.faint, takefocus=1)
        b._look = (fg, bg, border, hover, base)
        b.bind("<Enter>", lambda _e: str(b["state"]) != "disabled" and b.configure(bg=hover))
        b.bind("<Leave>", lambda _e: str(b["state"]) != "disabled" and b.configure(bg=bg))
        return b

    def set_enabled(self, b, on: bool) -> None:
        fg, bg, border, _hover, base = b._look
        b.configure(state="normal" if on else "disabled", bg=bg if on else base,
                    highlightbackground=border if on else self.t.line,
                    cursor="hand2" if on else "arrow")

    def clickable(self, frame, parts, command) -> None:
        """Make a frame behave like a button: click anywhere on it, or focus it
        and press Enter / Space."""
        for wdg in (frame, *parts):
            wdg.bind("<Button-1>", lambda _e: (frame.focus_set(), command())[1])
        frame.bind("<Return>", lambda _e: command())
        frame.bind("<space>", lambda _e: command())

    def entry(self, parent, var, placeholder="", width=None, mono=False):
        """Bordered text field with an optional placeholder. Returns the outer
        frame; the Entry itself is ``frame.entry``."""
        tk, t, px = self.tk, self.t, self.px
        box = tk.Frame(parent, bg=t.field, highlightthickness=1,
                       highlightbackground=t.field_line, highlightcolor=t.accent)
        e = tk.Entry(box, textvariable=var, bg=t.field, fg=t.ink,
                     font=self.f["mono" if mono else "ui"], insertbackground=t.ink,
                     relief="flat", bd=0, highlightthickness=0,
                     selectbackground=t.sel, selectforeground=t.sel_ink,
                     disabledbackground=t.field, disabledforeground=t.muted,
                     **({"width": width} if width else {}))
        e.pack(fill="both", expand=True, padx=px(8), pady=px(5))
        e.bind("<Control-Key-a>", lambda ev: (ev.widget.select_range(0, "end"), "break")[1])
        e.bind("<FocusIn>", lambda _e: box.configure(highlightbackground=t.accent))
        e.bind("<FocusOut>", lambda _e: box.configure(
            highlightbackground=getattr(box, "_edge", t.field_line)))
        if placeholder:
            ph = self.label(box, placeholder, t.muted, bg=t.field)
            ph.bind("<Button-1>", lambda _e: e.focus_set())

            def sync(*_):
                if var.get():
                    ph.place_forget()
                else:
                    ph.place(x=px(8), rely=0.5, anchor="w")
            tid = var.trace_add("write", sync)
            # fields are rebuilt with the view; drop the trace with the widget
            e.bind("<Destroy>", lambda ev, i=tid: ev.widget is e and
                   var.trace_remove("write", i), add="+")
            sync()
        box.entry = e
        return box

    def combo(self, parent, choices, current_key, on_pick, width):
        """Read-only dropdown over (key, display name) pairs."""
        from tkinter import ttk
        names = [n for _k, n in choices]
        by_name = {n: k for k, n in choices}
        var = self.tk.StringVar(value=dict(choices).get(current_key, names[0]))
        cb = ttk.Combobox(parent, textvariable=var, values=names, width=width,
                          state="readonly", style="Board.TCombobox", font=self.f["ui"],
                          height=min(len(names), 14))
        cb._var = var   # keep the variable alive with the widget
        cb.bind("<<ComboboxSelected>>",
                lambda _e: (cb.selection_clear(), on_pick(by_name.get(var.get(), "")))[1])
        return cb

    def segmented(self, parent, options, current, on_pick):
        """A row of mutually exclusive choices. Left / Right move it."""
        tk, t, px = self.tk, self.t, self.px
        fr = tk.Frame(parent, bg=t.line_strong, highlightthickness=1,
                      highlightbackground=t.line_strong, highlightcolor=t.ink,
                      takefocus=1)
        keys = [k for k, _n in options]
        state = {"cur": current}
        cells = {}

        def paint():
            for k, lab in cells.items():
                on = k == state["cur"]
                lab.configure(bg=t.sel if on else t.field,
                              fg=t.sel_ink if on else t.muted,
                              font=self.f["bold" if on else "ui"])

        def pick(k):
            if k != state["cur"]:
                state["cur"] = k
                paint()
                on_pick(k)

        def step(d):
            pick(keys[(keys.index(state["cur"]) + d) % len(keys)])
        for i, (k, name) in enumerate(options):
            lab = tk.Label(fr, text=name, padx=px(12), pady=px(5), cursor="hand2")
            lab.pack(side="left", padx=(1 if i else 0, 0))
            lab.bind("<Button-1>", lambda _e, k=k: (fr.focus_set(), pick(k))[1])
            cells[k] = lab
        fr.bind("<Left>", lambda _e: step(-1))
        fr.bind("<Right>", lambda _e: step(1))
        paint()
        return fr

    def check(self, parent, text, key):
        tk, t, px = self.tk, self.t, self.px
        bg = parent["bg"]
        fr = tk.Frame(parent, bg=bg, cursor="hand2", takefocus=1, highlightthickness=1,
                      highlightbackground=bg, highlightcolor=t.ink)
        s = px(16)
        box = tk.Canvas(fr, width=s, height=s, bg=bg, highlightthickness=0)
        box.pack(side="left", padx=(px(2), px(8)), pady=px(4))
        lab = self.label(fr, text)
        lab.pack(side="left", pady=px(4))

        def paint():
            box.delete("all")
            if self.settings[key]:
                box.create_rectangle(0, 0, s - 1, s - 1, fill=t.accent, outline=t.accent)
                box.create_line(s * .22, s * .52, s * .42, s * .72, s * .78, s * .28,
                                fill=t.accent_ink, width=px(2))
            else:
                box.create_rectangle(0, 0, s - 1, s - 1, fill=t.field,
                                     outline=t.field_line)

        def toggle():
            self.settings[key] = not self.settings[key]
            paint()
            self.on_settings_changed()
        self.clickable(fr, (box, lab), toggle)
        paint()
        return fr

    def status(self, msg: str, kind: str = "muted") -> None:
        self.status_var.set(msg)
        self.status_kind = kind
        self._paint_status()

    def _paint_status(self) -> None:
        t = self.t
        self.status_lbl.configure(fg={"warn": t.warn, "bad": t.bad,
                                      "ok": t.ok}.get(self.status_kind, t.muted))

    # ====================================================================== #
    # build
    # ====================================================================== #
    def build(self) -> None:
        tk = self.tk
        self.t = t = _theme.resolve_theme(self.settings.get("theme"))
        self.root.configure(bg=t.bg)
        self._style()
        self._titlebar()
        if self.shell is not None:
            self.shell.destroy()
        self._band_built = None
        self.shell = shell = tk.Frame(self.root, bg=t.bg)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(1, weight=1)
        self._build_header(shell)
        self.body = body = tk.Frame(shell, bg=t.bg)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_sidebar(body)
        self._build_main(body)
        self._build_footer(shell)
        self._build_drawer(body)
        shell.pack(fill="both", expand=True)
        self.root.update_idletasks()     # real widths, so the band knows its room
        self.sync()
        self.band.bind("<Configure>", self._on_band_resize)

    def _style(self) -> None:
        from tkinter import ttk
        t, root = self.t, self.root
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001 - keep whatever theme exists
            pass
        style.configure("Board.TCombobox", fieldbackground=t.field, background=t.raised,
                        foreground=t.ink, arrowcolor=t.muted, bordercolor=t.field_line,
                        lightcolor=t.field, darkcolor=t.field, selectbackground=t.field,
                        selectforeground=t.ink, padding=self.px(5), arrowsize=self.px(14))
        style.map("Board.TCombobox",
                  fieldbackground=[("readonly", t.field)], foreground=[("readonly", t.ink)],
                  selectbackground=[("readonly", t.field)],
                  selectforeground=[("readonly", t.ink)],
                  bordercolor=[("focus", t.accent)], background=[("active", t.hover)])
        for opt, val in (("background", t.field), ("foreground", t.ink),
                         ("selectBackground", t.sel), ("selectForeground", t.sel_ink),
                         ("font", self.f["ui"])):
            root.option_add(f"*TCombobox*Listbox.{opt}", val)

    def _titlebar(self) -> None:
        """Match the Windows title bar to the theme (Windows 10 2004+ / 11).
        Purely cosmetic; anything else leaves the system default."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1 if self.t.name == "dark" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:  # noqa: BLE001
            pass

    def _build_header(self, shell) -> None:
        tk, t, px = self.tk, self.t, self.px
        top = self.panel(shell, t.surface)
        top.grid(row=0, column=0, sticky="ew")
        s = px(18)
        mark = tk.Canvas(top, width=s, height=s, bg=t.surface, highlightthickness=0)
        for i, frac in enumerate((0.45, 0.7, 1.0)):       # three rising bars
            x0 = round(i * s / 3) + 1
            mark.create_rectangle(x0, round(s * (1 - frac)), x0 + round(s / 3) - 2, s,
                                  fill=t.accent, outline="")
        mark.pack(side="left", padx=(px(18), px(8)), pady=px(11))
        self.label(top, APP_NAME, font=self.f["brand"]).pack(side="left", padx=(0, px(18)))
        self.options_btn = self.button(top, "Options", self.toggle_drawer, "ghost")
        self.options_btn.pack(side="right", padx=(0, px(10)))
        other = "Light mode" if t.name == "dark" else "Dark mode"
        self.theme_btn = self.button(
            top, other, lambda: self.set_theme("light" if t.name == "dark" else "dark"),
            "ghost")
        self.theme_btn.pack(side="right")
        self.log_btn = self.button(top, "Log", self.toggle_log, "ghost")
        self.log_btn.pack(side="right")
        self.tape = tk.Text(top, height=1, bg=t.surface, fg=t.muted, font=self.f["mono_s"],
                            bd=0, highlightthickness=0, wrap="none", cursor="arrow",
                            takefocus=0, width=10)
        self.tape.tag_configure("up", foreground=t.ok)
        self.tape.tag_configure("down", foreground=t.bad)
        self.tape.pack(side="left", fill="x", expand=True, padx=(0, px(12)))

    def _build_sidebar(self, body) -> None:
        tk, t, px = self.tk, self.t, self.px
        side = self.panel(body, t.surface, width=px(216))
        side.grid(row=0, column=0, sticky="ns")
        side.pack_propagate(False)

        def header(text):
            self.label(side, text.upper(), t.muted, self.f["head"]).pack(
                fill="x", padx=px(18), pady=(px(16), px(6)))

        header("Review")
        self.nav: dict[str, tuple] = {}
        order = []
        for key, title, sub in MODES:
            fr = tk.Frame(side, bg=t.surface, cursor="hand2", takefocus=1,
                          highlightthickness=1, highlightbackground=t.surface,
                          highlightcolor=t.ink)
            fr.pack(fill="x", padx=px(8), pady=(0, px(2)))
            bar = tk.Frame(fr, bg=t.surface, width=px(3))
            bar.pack(side="left", fill="y")
            txt = tk.Frame(fr, bg=t.surface)
            txt.pack(side="left", fill="x", expand=True, padx=(px(9), px(6)), pady=px(5))
            a = self.label(txt, title)
            a.pack(fill="x")
            b = self.label(txt, sub, t.muted, self.f["small"])
            b.pack(fill="x")
            self.nav[key] = (fr, bar, txt, a, b)
            order.append(fr)
            self.clickable(fr, (bar, txt, a, b), lambda k=key: self.set_mode(k))
            fr.bind("<Enter>", lambda _e, k=key: self._nav_hover(k))
            fr.bind("<Leave>", lambda e, f=fr: self._nav_leave(e, f))
        for i, fr in enumerate(order):
            fr.bind("<Up>", lambda _e, i=i: order[(i - 1) % len(order)].focus_set())
            fr.bind("<Down>", lambda _e, i=i: order[(i + 1) % len(order)].focus_set())

        header("Recent reports")
        self.recent_box = tk.Frame(side, bg=t.surface)
        self.recent_box.pack(fill="x", padx=px(8))
        self.label(side, f"Version {_gui.APP_VERSION}", t.muted, self.f["small"]).pack(
            side="bottom", fill="x", padx=px(18), pady=px(12))

    def _build_main(self, body) -> None:
        tk, t, px = self.tk, self.t, self.px
        self.main = main = tk.Frame(body, bg=t.bg)
        main.grid(row=0, column=1, sticky="nsew", padx=px(22), pady=(px(16), px(14)))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(4, weight=1)

        # -- row 0: command header ------------------------------------------- #
        cmd = tk.Frame(main, bg=t.bg)
        cmd.grid(row=0, column=0, sticky="ew")
        cmd.grid_columnconfigure(0, weight=1)
        self.title_lbl = self.label(cmd, font=self.f["title"])
        self.title_lbl.grid(row=0, column=0, sticky="w")
        self.hint_lbl = self.label(cmd, fg=t.muted)
        self.hint_lbl.grid(row=1, column=0, sticky="w")
        meta = tk.Frame(cmd, bg=t.bg)
        meta.grid(row=2, column=0, sticky="w", pady=(px(2), 0))
        self.meta_lbl = self.label(meta, fg=t.muted, font=self.f["small"])
        self.meta_lbl.pack(side="left")
        self.change_lbl = self.tk.Label(
            meta, text="Change", fg=t.accent_text, bg=t.bg, font=self.f["small"],
            cursor="hand2", takefocus=1, highlightthickness=1,
            highlightbackground=t.bg, highlightcolor=t.ink)
        self.change_lbl.pack(side="left", padx=(px(8), 0))
        self.clickable(self.change_lbl, (), self.toggle_drawer)
        acts = tk.Frame(cmd, bg=t.bg)
        acts.grid(row=0, column=1, rowspan=3, sticky="e")
        self.run_btn = self.button(acts, "Run review", self.start, "primary",
                                   font=self.f["h2"])
        self.run_btn.configure(padx=px(22), pady=px(8))
        self.cancel_btn = self.button(acts, "Cancel", self.cancel, "danger",
                                      font=self.f["h2"])
        self.cancel_btn.configure(padx=px(22), pady=px(8))

        # -- row 1: per-source inputs ---------------------------------------- #
        self.inputs = tk.Frame(main, bg=t.bg)

        # -- row 2: the status band (idle / running / done share the slot) ---- #
        self.band = band = self.panel(main, t.raised)
        band.grid(row=2, column=0, sticky="ew", pady=(px(14), 0))
        self.band_edge = tk.Frame(band, width=px(4), bg=t.line_strong)
        self.band_edge.pack(side="left", fill="y")
        self.band_body = tk.Frame(band, bg=t.raised)
        self.band_body.pack(side="left", fill="both", expand=True, padx=px(16),
                            pady=px(12))
        main.grid_rowconfigure(2, minsize=px(24) + self.line["h2"] + self.line["ui"] +
                               px(2 + 8 + 32) + 2)

        # -- row 3: filter chips + find --------------------------------------- #
        tools = tk.Frame(main, bg=t.bg)
        tools.grid(row=3, column=0, sticky="ew", pady=(px(14), px(8)))
        self.chips: dict[str, tuple] = {}
        for key, word, shape in FILTERS:
            fr = tk.Frame(tools, bg=t.surface, cursor="hand2", takefocus=1,
                          highlightthickness=1, highlightbackground=t.line_strong,
                          highlightcolor=t.ink)
            fr.pack(side="left", padx=(0, px(8)))
            parts = []
            if shape:
                cv = tk.Canvas(fr, width=px(12), height=px(12), bg=t.surface,
                               highlightthickness=0)
                cv.pack(side="left", padx=(px(10), 0))
                parts.append(cv)
            word_l = self.label(fr, word)
            word_l.pack(side="left", padx=(px(6 if shape else 12), 0), pady=px(4))
            num_l = self.label(fr, "", font=self.f["mono_b"])
            num_l.pack(side="left", padx=(px(6), px(12)), pady=px(4))
            parts += [word_l, num_l]
            self.chips[key] = (fr, parts, shape)
            self.clickable(fr, parts, lambda k=key: self.set_filter(k))
        self.find_box = self.entry(tools, self.find_var, "Find ticker or name", width=24)
        self.find_box.pack(side="right")

        # -- row 4: the table -------------------------------------------------- #
        fonts = {"ui": self.f["ui"], "mono": self.f["mono"], "head": self.f["head"],
                 "mono_s": self.f["mono_s"], "art": self.f["art"]}
        self.table = BoardTable(main, theme=t, fonts=fonts, columns=COLUMNS,
                                cell=self.cell, row_bg=self.row_bg, scale=self.scale,
                                on_sort=self.on_sort, on_activate=self.copy_ticker)
        self.table.frame.grid(row=4, column=0, sticky="nsew")
        self.table.body.bind("<Control-c>", lambda _e: self.copy_ticker(self.table.selected))

        # -- row 5: log --------------------------------------------------------- #
        self.log_wrap = self.panel(main, t.surface)
        head = tk.Frame(self.log_wrap, bg=t.surface)
        head.pack(fill="x")
        self.label(head, "LOG", t.muted, self.f["head"]).pack(side="left", padx=px(12),
                                                              pady=(px(6), 0))
        self.button(head, "Hide", self.toggle_log, "ghost", font=self.f["small"]).pack(
            side="right", padx=px(4), pady=(px(2), 0))
        self.log = tk.Text(self.log_wrap, height=7, bg=t.surface, fg=t.muted,
                           font=self.f["mono_s"], bd=0, wrap="word", state="disabled",
                           takefocus=1, highlightthickness=1,
                           highlightbackground=t.surface, highlightcolor=t.accent,
                           padx=px(12), pady=px(6), selectbackground=t.sel,
                           selectforeground=t.sel_ink)
        self.log.pack(fill="both", expand=True)
        if self.log_buf:
            self.log.configure(state="normal")
            self.log.insert("end", "".join(self.log_buf))
            self.log.see("end")
            self.log.configure(state="disabled")

    def _build_footer(self, shell) -> None:
        t, px = self.t, self.px
        foot = self.panel(shell, t.surface)
        foot.grid(row=2, column=0, sticky="ew")
        self.keys_lbl = self.label(foot, fg=t.muted, font=self.f["small"])
        self.keys_lbl.pack(side="left", padx=px(18), pady=px(6))
        self.status_lbl = self.label(foot, textvariable=self.status_var, fg=t.muted,
                                     font=self.f["small"], anchor="e")
        self.status_lbl.pack(side="right", padx=px(18))

    def _build_drawer(self, body) -> None:
        tk, t, px = self.tk, self.t, self.px
        from tkinter import filedialog
        self.drawer = d = self.panel(body, t.surface, t.line_strong, width=px(330))
        d.pack_propagate(False)
        head = tk.Frame(d, bg=t.surface)
        head.pack(fill="x", padx=px(18), pady=(px(14), px(4)))
        self.label(head, "Options", font=self.f["h2"]).pack(side="left")
        self.button(head, "Close", self.toggle_drawer, "ghost").pack(side="right")

        def section(text):
            self.label(d, text.upper(), t.muted, self.f["head"]).pack(
                fill="x", padx=px(18), pady=(px(14), px(6)))

        def note(text, fg=None):
            lab = self.label(d, text, fg or t.muted, self.f["small"], justify="left",
                             wraplength=px(290))
            lab.pack(fill="x", padx=px(18), pady=(px(4), 0))
            return lab

        section("Report format")
        self.segmented(d, tuple(FORMAT_NAMES.items()), self.settings["fmt"],
                       lambda k: self._set_option("fmt", k)).pack(anchor="w", padx=px(18))
        self.check(d, "Rich report (wider, easier to read)", "rich").pack(
            anchor="w", padx=px(16), pady=(px(8), 0))
        self.check(d, "Leave out company names", "no_names").pack(anchor="w", padx=px(16))

        section("Limit")
        self.limit_box = self.entry(d, self.limit_var, "Every ticker", width=12, mono=True)
        self.limit_box.pack(anchor="w", padx=px(18))
        self.limit_note = note("")
        section("Workers")
        self.workers_box = self.entry(d, self.workers_var, "6", width=12, mono=True)
        self.workers_box.pack(anchor="w", padx=px(18))
        self.workers_note = note("")

        section("Report folder")
        self.outdir_lbl = note("", t.ink)
        row = tk.Frame(d, bg=t.surface)
        row.pack(fill="x", padx=px(18), pady=(px(8), 0))

        def browse():
            path = filedialog.askdirectory(parent=self.root,
                                           title="Where should reports go?")
            if path:
                self._set_option("outdir", path)
        self.button(row, "Browse…", browse).pack(side="left")
        self.outdir_reset = self.button(row, "Use default",
                                        lambda: self._set_option("outdir", ""), "ghost")
        self.outdir_reset.pack(side="left", padx=(px(6), 0))

        section("Theme")
        self.segmented(d, (("auto", "Auto"), ("dark", "Dark"), ("light", "Light")),
                       self.settings["theme"], self.set_theme).pack(anchor="w", padx=px(18))
        note("Options are saved as you change them.").pack_configure(
            side="bottom", pady=(0, px(14)))

    # ====================================================================== #
    # sync: paint everything from state
    # ====================================================================== #
    def sync(self) -> None:
        self.paint_nav()
        self.build_inputs()
        self.refresh_recent()
        self.refresh_header()
        self.refresh_options()
        self.render_band()
        self.table.set_sort(self.sort_key, self.sort_desc)
        self.refresh_table()
        self.render_tape()
        self.apply_log()
        self.apply_drawer()
        self.paint_keys()
        self._paint_status()

    def current_cfg(self) -> dict:
        s = self.settings
        return {"mode": self.mode, "source": self.source, "sector": self.sector,
                "region": self.region, "tickers": self.tickers_var.get(),
                "file": self.file_var.get(), "limit": s["limit"], "workers": s["workers"],
                "fmt": s["fmt"], "rich": s["rich"], "no_names": s["no_names"],
                "outdir": s["outdir"]}

    def remember(self) -> None:
        save_board_settings({"mode": self.mode, "source": self.source,
                             "sector": self.sector, "region": self.region,
                             "tickers": self.tickers_var.get(),
                             "file": self.file_var.get(), **self.settings})

    # -- sidebar ------------------------------------------------------------- #
    def paint_nav(self) -> None:
        t = self.t
        for key, (fr, bar, txt, a, b) in self.nav.items():
            on = key == self.mode
            bg = t.sel if on else t.hover if key == self.nav_hover else t.surface
            for wdg in (fr, txt, a, b):
                wdg.configure(bg=bg)
            fr.configure(highlightbackground=bg)
            bar.configure(bg=t.accent if on else bg)
            a.configure(fg=t.sel_ink if on else t.ink,
                        font=self.f["bold" if on else "ui"])
            b.configure(fg=t.sel_ink if on else t.muted)

    def _nav_hover(self, key) -> None:
        if self.nav_hover != key:
            self.nav_hover = key
            self.paint_nav()

    def _nav_leave(self, e, frame) -> None:
        under = frame.winfo_containing(e.x_root, e.y_root)
        if under is None or not str(under).startswith(str(frame)):
            self.nav_hover = None
            self.paint_nav()

    def refresh_recent(self) -> None:
        tk, t, px = self.tk, self.t, self.px
        for child in self.recent_box.winfo_children():
            child.destroy()
        self.recent = recent_reports()
        if not self.recent:
            self.label(self.recent_box, "None yet", t.muted, self.f["small"]).pack(
                fill="x", padx=px(11), pady=px(4))
        for it in self.recent:
            fr = tk.Frame(self.recent_box, bg=t.surface, cursor="hand2", takefocus=1,
                          highlightthickness=1, highlightbackground=t.surface,
                          highlightcolor=t.ink)
            fr.pack(fill="x", pady=(0, px(2)))
            a = self.label(fr, it["label"])
            a.pack(fill="x", padx=px(11), pady=(px(4), 0))
            b = self.label(fr, it["detail"], t.muted, self.f["small"])
            b.pack(fill="x", padx=px(11), pady=(0, px(4)))
            self.clickable(fr, (a, b), lambda p=it["path"]: self.open_path(p))

            def paint(on, parts=(fr, a, b)):
                for wdg in parts:
                    wdg.configure(bg=t.hover if on else t.surface)
            fr.bind("<Enter>", lambda _e, f=paint: f(True))
            fr.bind("<Leave>", lambda e, f=paint, w=fr: (
                (lambda u: u is None or not str(u).startswith(str(w)))(
                    w.winfo_containing(e.x_root, e.y_root)) and f(False)))

    # -- command header -------------------------------------------------------- #
    def refresh_header(self) -> None:
        t = self.t
        cfg = self.current_cfg()
        self.title_lbl.configure(text=mode_title(cfg))
        self.hint_lbl.configure(text=hint_text(cfg))
        bad = validate_settings(self.settings)
        blocker = ""
        if not self.running:
            blocker = next((f"{k.capitalize()}: {v}" for k, v in bad.items()), "") \
                or run_blocker(cfg)
        self.blocker = blocker
        if blocker:
            self.meta_lbl.configure(text=blocker, fg=t.warn)
        else:
            where = short_path(expected_output_path(cfg), _gui.BASE_DIR)
            self.meta_lbl.configure(text=f"{options_summary(self.settings)} · saves to {where}",
                                    fg=t.muted)
        self.run_btn.configure(text="Run again" if self.screen == "done" else "Run review")
        self.run_btn.pack_forget()
        self.cancel_btn.pack_forget()
        if self.running:
            self.cancel_btn.pack()
        else:
            self.run_btn.pack()
            self.set_enabled(self.run_btn, not blocker)

    def build_inputs(self) -> None:
        tk, t, px = self.tk, self.t, self.px
        for child in self.inputs.winfo_children():
            child.destroy()
        mode = self.mode
        if mode == "curated":
            self.inputs.grid_remove()
            return
        self.inputs.grid(row=1, column=0, sticky="ew", pady=(px(12), 0))
        if mode == "source":
            self.combo(self.inputs, self.sources, self.source,
                       lambda k: self._pick("source", k), 34).pack(side="left")
        elif mode == "sector":
            self.combo(self.inputs, self.sectors, self.sector,
                       lambda k: self._pick("sector", k), 20).pack(side="left")
            self.label(self.inputs, "in", t.muted).pack(side="left", padx=px(10))
            self.combo(self.inputs, self.regions, self.region,
                       lambda k: self._pick("region", k), 30).pack(side="left")
        elif mode == "custom":
            box = self.entry(self.inputs, self.tickers_var,
                             "Type tickers, e.g. AAPL MSFT BHP.AX", mono=True)
            box.pack(side="left", fill="x", expand=True)
            box.entry.bind("<Return>", self.start)
        else:
            box = self.entry(self.inputs, self.file_var,
                             "Path to a .txt or .csv file of tickers")
            box.pack(side="left", fill="x", expand=True)
            self.button(self.inputs, "Browse…", self.pick_file).pack(
                side="left", padx=(px(8), 0))
        for child in self.inputs.winfo_children():
            if self.running:
                try:
                    (getattr(child, "entry", child)).configure(state="disabled")
                except tk.TclError:
                    pass

    def _pick(self, what: str, key: str) -> None:
        setattr(self, what, key)
        self.refresh_preview()

    def pick_file(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose a tickers file",
            filetypes=[("Tickers", "*.txt *.csv"), ("All files", "*.*")])
        if path:
            self.file_var.set(path)

    def set_mode(self, key: str) -> None:
        if self.running or key not in MODE_KEYS:
            return
        self.mode = key
        self.paint_nav()
        self.build_inputs()
        self.refresh_preview()

    def refresh_preview(self) -> None:
        """Not running: show the list a run would review, when known offline."""
        if self.running or self.shell is None:
            return
        cfg = self.current_cfg()
        tickers = preview_tickers(cfg)
        self.model.reset(tickers or ())
        if self.screen == "done":        # leaving a finished run: drop its leftovers
            self.status(READY)
            if self.log_auto:
                self.log_open = self.log_auto = False
                self.apply_log()
        self.screen, self.summary = "idle", None
        if tickers is None:
            self.empty = ("This list is looked up when you press Run review.\n"
                          "Each ticker appears here as it is fetched.", ROCKET)
        elif not tickers:
            self.empty = ("Nothing to review yet. Type some tickers above."
                          if cfg["mode"] == "custom" else
                          "Nothing to review yet. Choose a file of tickers above.", "")
        else:
            self.empty = ("", "")
        self.filter = "all"
        self.refresh_header()
        self.render_band()
        self.refresh_table()
        self.table.to_top()
        self.render_tape()
        self.paint_keys()

    # -- options ---------------------------------------------------------------- #
    def _set_option(self, key: str, value) -> None:
        self.settings[key] = value
        self.on_settings_changed()

    def _on_number(self, key: str, var) -> None:
        self.settings[key] = var.get().strip()
        self.on_settings_changed()

    def on_settings_changed(self) -> None:
        if self.shell is None:
            return
        self.remember()
        self.refresh_options()
        self.refresh_header()

    def refresh_options(self) -> None:
        t = self.t
        bad = validate_settings(self.settings)
        for key, box, lab, hint in (
                ("limit", self.limit_box, self.limit_note,
                 "Blank reviews every ticker in the list."),
                ("workers", self.workers_box, self.workers_note,
                 "Parallel downloads. 6 is polite to Yahoo.")):
            box._edge = t.bad if key in bad else t.field_line
            if self.root.focus_get() is not box.entry:
                box.configure(highlightbackground=box._edge)
            lab.configure(text=bad.get(key, hint), fg=t.bad if key in bad else t.muted)
        outdir = self.settings["outdir"]
        self.outdir_lbl.configure(
            text=outdir or f"Default: {short_path(_gui.BASE_DIR / 'reports')}")
        self.set_enabled(self.outdir_reset, bool(outdir))

    def set_theme(self, choice: str) -> None:
        if self.running or choice == self.settings["theme"]:
            return
        self.settings["theme"] = choice
        self.remember()
        # rebuild once this click has finished with the widget it came from
        self.root.after_idle(self.build)

    def toggle_drawer(self, _e=None) -> None:
        if self.running and not self.drawer_open:
            self.status("Options are locked while a run is in progress.", "warn")
            return
        self.drawer_open = not self.drawer_open
        self.apply_drawer()

    def apply_drawer(self) -> None:
        if self.drawer_open:
            self.drawer.place(relx=1.0, rely=0.0, anchor="ne", relheight=1.0)
            self.drawer.lift()
        else:
            self.drawer.place_forget()

    # -- log ---------------------------------------------------------------------- #
    def append_log(self, text: str) -> None:
        self.log_buf.append(text)
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")
        if not self.log_open and not self.log_unread:
            self.log_unread = True
            self.apply_log()

    def toggle_log(self, _e=None) -> None:
        self.log_open, self.log_auto = not self.log_open, False
        self.apply_log()

    def apply_log(self) -> None:
        t = self.t
        if self.log_open:
            self.log_unread = False
            self.log_wrap.grid(row=5, column=0, sticky="ew", pady=(self.px(10), 0))
        else:
            self.log_wrap.grid_remove()
        fg = t.ink if self.log_open else t.accent_text if self.log_unread else t.muted
        self.log_btn.configure(text="Log •" if self.log_unread else "Log", fg=fg,
                               activeforeground=fg)

    # -- table ---------------------------------------------------------------------- #
    def cell(self, row: Row, key: str) -> Cell:
        t = self.t
        if key == "ticker":
            return Cell(row.ticker, fg=t.muted if row.state == "queued" else t.ink)
        if key == "name":
            return Cell(row.name)
        if key == "sector":
            return Cell(row.sector, fg=t.muted)
        if key == "price":
            if row.price is None:
                return Cell()
            return Cell(fmt_price(row.price), sub=row.currency)
        if key == "ret":
            if row.ret_1m is None:
                return Cell()
            fg = t.ok if row.ret_1m >= 0 else t.bad
            return Cell(pct(row.ret_1m, sign=True), fg=fg, bar=row.ret_1m, bar_fg=fg)
        mark, mark_fg, fg = {"ok": ("dot", t.ok, t.muted),
                             "retry": ("ring", t.warn, t.warn),
                             "fail": ("cross", t.bad, t.bad)}.get(
            row.state, ("pip", t.muted, t.muted))
        return Cell(status_cell_text(row), fg=fg, mark=mark, mark_fg=mark_fg)

    def row_bg(self, row: Row) -> str:
        return {"fail": self.t.bad_bg, "retry": self.t.warn_bg}.get(row.state, "")

    def refresh_table(self) -> None:
        if self.shell is None:
            return
        m = self.model
        rows = filter_rows(m.rows.values(), self.filter, self.find_var.get(),
                           m.in_retry_round)
        rows = sort_rows(rows, self.sort_key, self.sort_desc, running=self.running)
        if m.rows and not rows:
            self.table.set_empty("No rows match this filter.", "")
        else:
            self.table.set_empty(*self.empty)
        self.table.set_rows(rows, follow=self.running and self.sort_key is None)
        self.paint_chips()
        self.refresh_mosaic()

    def paint_chips(self) -> None:
        t, px = self.t, self.px
        c = self.model.counts()
        nums = {"all": c["total"], "ok": c["ok"], "retry": c["retry"], "fail": c["fail"]}
        tone = {"ok": t.ok, "retry": t.warn, "fail": t.bad}
        for key, (fr, parts, shape) in self.chips.items():
            on = key == self.filter
            n = nums[key]
            bg = t.sel if on else t.surface
            fr.configure(bg=bg, highlightbackground=t.accent if on else t.line_strong)
            word_l, num_l = parts[-2], parts[-1]
            word_l.configure(bg=bg, fg=t.sel_ink if on else t.ink)
            num_l.configure(bg=bg, text=str(n) if (n or key != "all") else "",
                            fg=(t.sel_ink if on else t.ink) if n else t.muted)
            if shape:
                cv = parts[0]
                cv.configure(bg=bg)
                cv.delete("all")
                draw_mark(cv, shape, px(6), px(6), px(4),
                          tone[key] if n else t.muted, px(2))

    def set_filter(self, key: str) -> None:
        self.filter = key
        self.refresh_table()
        self.table.to_top()

    def on_sort(self, key: str) -> None:
        """Header click: ascending, then descending, then back to run order."""
        if self.sort_key != key:
            self.sort_key, self.sort_desc = key, key in ("price", "ret")
        elif self.sort_desc == (key in ("price", "ret")):
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = None, False
        self.table.set_sort(self.sort_key, self.sort_desc)
        self.refresh_table()

    def focus_find(self) -> None:
        self.find_box.entry.focus_set()
        self.find_box.entry.select_range(0, "end")

    def copy_ticker(self, ticker) -> None:
        if ticker:
            self.root.clipboard_clear()
            self.root.clipboard_append(ticker)
            self.status(f"Copied {ticker}.", "ok")

    # -- header tape ------------------------------------------------------------------ #
    def render_tape(self) -> None:
        tape = self.tape
        tape.configure(state="normal")
        tape.delete("1.0", "end")
        items = self.model.tape()
        if not items:
            tape.insert("end", "Research screen, not financial advice")
        for i, (tk_, r) in enumerate(reversed(items)):   # newest first: a narrow
            tape.insert("end", ("    " if i else "") + f"{tk_} ")   # window clips the oldest
            tape.insert("end", pct(r, sign=True), "up" if r >= 0 else "down")
        tape.configure(state="disabled")

    # -- status band -------------------------------------------------------------------- #
    def render_band(self) -> None:
        """(Re)build the band for the current screen. Every state is three rows
        of the same heights, so the layout below it never moves."""
        tk, t, px = self.tk, self.t, self.px
        for child in self.band_body.winfo_children():
            child.destroy()
        self._band_built = self.screen
        self.mosaic = None
        self._band_wide = self._band_is_wide()
        if self._band_wide:              # the mosaic takes the right of the band
            self._build_mosaic(self.band_body)
        body = tk.Frame(self.band_body, bg=t.raised)
        body.pack(side="left", fill="both", expand=True)
        self.band_text = body
        r1 = tk.Frame(body, bg=t.raised)
        r1.pack(fill="x")
        r2 = tk.Frame(body, bg=t.raised)
        r2.pack(fill="x", pady=(px(2), 0))
        r3 = tk.Frame(body, bg=t.raised, height=px(32))
        r3.pack(fill="x", pady=(px(8), 0))
        r3.pack_propagate(False)

        if self.screen == "run":
            self.band_edge.configure(bg=t.accent)
            s = px(12)
            self.pulse = tk.Canvas(r1, width=s, height=s, bg=t.raised, highlightthickness=0)
            self.pulse_dot = self.pulse.create_oval(2, 2, s - 2, s - 2, fill=t.accent,
                                                    outline="")
            self.pulse.pack(side="left", padx=(0, px(8)))
            self.run_title = self.label(r1, font=self.f["h2"])
            self.run_title.pack(side="left")
            self.run_eta = self.label(r1, fg=t.muted)
            self.run_eta.pack(side="right")
            self.run_clock = self.label(r1, font=self.f["mono_b"])
            self.run_clock.pack(side="right", padx=(0, px(10)))
            self.run_mood = self.label(r2, fg=t.muted)
            self.run_mood.pack(fill="x")
            self.finish_btn = self.button(r3, "Finish now", self.finish_early)
            self.track = tk.Canvas(r3, bg=t.raised, highlightthickness=0, height=px(32))
            self.track.pack(side="left", fill="both", expand=True)
            self.track.bind("<Configure>", lambda _e: self.draw_track())
            self.update_run()
            return

        if self.screen == "done" and self.summary:
            s = self.summary
            self.band_edge.configure(bg={"ok": t.ok, "warn": t.warn}.get(s["kind"], t.bad))
            self.label(r1, s["title"], font=self.f["h2"]).pack(side="left")
            if s["saved"]:
                self.label(r1, s["saved"], t.muted, self.f["small"]).pack(
                    side="right", padx=(px(12), 0))
            fails = self.model.failed()
            room = self.band.winfo_width() - px(56) - (
                px(self.MOSAIC_W + 20) if self._band_wide else 0)
            chars = max(30, room // px(7))
            if s["actions"] and fails:       # name them, as many as the line holds
                line = self.label(r2, "Could not fetch  " + failure_line(fails, chars - 17),
                                  t.bad, cursor="hand2")
                line.bind("<Button-1>", lambda _e: self.set_filter("fail"))
            else:                            # one line here; the log has the rest
                first = s["sub"].splitlines()[0] if s["sub"] else ""
                line = self.label(r2, fit_note(first, chars), t.muted)
            line.pack(fill="x")
            if not s["actions"]:
                self.label(r3, "The log below has the details. Fetched tickers are "
                               "cached, so running again resumes.", t.muted).pack(side="left")
                return
            btns = [self.button(r3, "Copy for AI", self.copy_for_ai, "primary"),
                    self.button(r3, "Open report", self.open_report),
                    self.button(r3, "Open folder", self.open_folder)]
            if s["retry"]:
                btns.append(self.button(r3, s["retry"], self.start))
            for b in btns:
                b.pack(side="left", padx=(0, px(8)))
            return

        self.band_edge.configure(bg=t.line_strong)
        self.label(r1, "One file, ready to hand to an AI", font=self.f["h2"]).pack(
            side="left")
        self.label(r2, "Pick a list on the left and press Run review. Every ticker fills "
                       "in below as it is fetched.", t.muted).pack(fill="x")
        latest = self.recent[0] if getattr(self, "recent", None) else None
        if latest:
            self.label(r3, f"Latest report · {latest['label']} · {latest['detail']}",
                       t.muted).pack(side="left", padx=(0, px(12)))
            self.button(r3, "Copy for AI",
                        lambda p=latest["path"]: self.copy_for_ai(path=p)).pack(
                side="left", padx=(0, px(8)))
            self.button(r3, "Open", lambda p=latest["path"]: self.open_path(p)).pack(
                side="left")
        else:
            self.label(r3, "No reports yet. They are saved to "
                           f"{short_path(_gui.BASE_DIR / 'reports')}.", t.muted).pack(
                side="left")

    # -- the mosaic: one square per ticker ------------------------------------------- #
    MOSAIC_W, BAND_WIDE = 300, 860       # px at 100 %: mosaic width, band width needing it

    def _band_is_wide(self) -> bool:
        return self.band.winfo_width() >= self.px(self.BAND_WIDE)

    def _on_band_resize(self, _e=None) -> None:
        if self._band_is_wide() != getattr(self, "_band_wide", None):
            self.render_band()           # the mosaic appears / steps aside

    def _build_mosaic(self, parent) -> None:
        tk, t, px = self.tk, self.t, self.px
        col = tk.Frame(parent, bg=t.raised)
        col.pack(side="right", fill="y", padx=(px(20), 0))
        foot = tk.Frame(col, bg=t.raised)
        foot.pack(side="bottom", fill="x", pady=(px(6), 0))
        self.mosaic_note = self.label(foot, "", t.muted, self.f["small"])
        self.mosaic_note.pack(side="left")
        self.mosaic_key = key = tk.Frame(foot, bg=t.raised)
        self.label(key, "-20%", t.muted, self.f["mono_s"]).pack(side="left")
        ramp = tk.Canvas(key, bg=t.raised, highlightthickness=0, height=px(10),
                         width=px(6 * 9))
        for i, v in enumerate((-0.2, -0.1, -0.03, 0.03, 0.1, 0.2)):
            fill = _theme.heat(t, v)
            ramp.create_rectangle(i * px(9), 1, i * px(9) + px(7), px(8) + 1, fill=fill,
                                  outline=fill)
        ramp.pack(side="left", padx=px(5))
        self.label(key, "+20%", t.muted, self.f["mono_s"]).pack(side="left")
        self.mosaic = Mosaic(col, bg=t.raised, colour=self.mosaic_colour, empty=t.track,
                             width=px(self.MOSAIC_W), height=px(10),
                             on_hover=self._mosaic_hover, on_pick=self._mosaic_pick,
                             on_layout=lambda: self._mosaic_hover(None))
        self.mosaic.canvas.pack(side="top", fill="both", expand=True)
        self.refresh_mosaic()

    def mosaic_colour(self, row: Row) -> tuple[str, str]:
        """(fill, outline) of a ticker's square. A failure is a hollow red
        square, so it cannot be mistaken for a solid red (falling) return."""
        t = self.t
        if row.state == "ok":
            return _theme.heat(t, row.ret_1m), ""
        if row.state == "fail":
            return t.raised, t.bad
        if row.state == "retry":
            return t.warn, ""
        return t.track, ""

    def refresh_mosaic(self) -> None:
        if getattr(self, "mosaic", None) is None:
            return
        self.mosaic.set_rows(self.model.rows.values(), self.model.counts()["total"])
        self._mosaic_hover(None)

    def _mosaic_hover(self, row) -> None:
        if getattr(self, "mosaic", None) is None:
            return
        t = self.t
        if row is not None:
            if row.state == "ok":
                text = (f"{row.ticker}  {pct(row.ret_1m, sign=True)} in a month"
                        if row.ret_1m is not None else f"{row.ticker}  no 1-month return")
            else:
                text = f"{row.ticker}  {status_cell_text(row)}"
            self.mosaic_note.configure(text=fit_note(text, 40), fg=t.ink)
            self.mosaic_key.pack_forget()
            return
        n, shown = self.mosaic.total, self.mosaic.shown()
        self.mosaic_note.configure(text=mosaic_caption(n, shown), fg=t.muted)
        if n:
            self.mosaic_key.pack(side="right")
        else:
            self.mosaic_key.pack_forget()

    def _mosaic_pick(self, row) -> None:
        """Click a square: show that ticker's row in the table."""
        if row.ticker not in self.table.index:
            self.filter = "all"
            self.find_var.set("")
            self.refresh_table()
        self.table.selected = row.ticker
        self.table.see(row.ticker)
        self.table.body.focus_set()

    def update_run(self) -> None:
        if self._band_built != "run":
            return
        t, m = self.t, self.model
        c = m.counts()
        elapsed = time.monotonic() - self.t0 if self.t0 else 0
        self.run_title.configure(text=m.headline())
        self.run_mood.configure(text=m.mood())
        self.run_clock.configure(text=fmt_clock(elapsed))
        # no estimate while the run is deliberately waiting: it would be wrong
        paused = m.in_retry_round or bool(m.wait)
        self.run_eta.configure(text="" if paused else
                               eta_text(c["done"], c["total"], elapsed))
        self.pulse.itemconfigure(self.pulse_dot, fill=t.accent if self.tick % 2 else t.track)
        waiting = bool(m.wait) or bool(m.note and "retry" in m.note.lower())
        if waiting and c["ok"]:
            self.finish_btn.pack(side="right", padx=(self.px(14), 0))
        else:
            self.finish_btn.pack_forget()
        self.draw_track()

    def draw_track(self) -> None:
        t, px, c = self.t, self.px, self.track
        c.delete("all")
        w, h = max(c.winfo_width(), 10), max(c.winfo_height(), 10)
        y0, y1 = h - px(11), h - px(5)
        counts = self.model.counts()
        total = counts["total"] or 1
        ok_w = int(w * counts["ok"] / total)
        fail_w = int(w * counts["fail_raw"] / total)
        c.create_rectangle(0, y0, w, y1, fill=t.track, outline="")
        if ok_w:
            c.create_rectangle(0, y0, ok_w, y1, fill=t.accent, outline="")
        if fail_w:
            c.create_rectangle(ok_w, y0, ok_w + fail_w, y1, fill=t.warn, outline="")
        trail = TRAIL[self.tick % len(TRAIL)]
        lead = max(ok_w + fail_w, self._mono_s.measure(trail))
        c.create_text(min(lead, w), y0 - px(1), text=trail, anchor="se",
                      fill=t.accent_text, font=self.f["mono_s"])

    def animate(self) -> None:
        self.tick += 1
        if self.running:
            self.update_run()
        self.root.after(500, self.animate)

    # ====================================================================== #
    # actions
    # ====================================================================== #
    def open_path(self, path) -> None:
        try:
            p = Path(path)
            if sys.platform.startswith("win"):
                os.startfile(str(p))  # type: ignore[attr-defined]  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            self.status(f"Could not open it: {exc}", "bad")

    def report_exists(self) -> bool:
        try:
            return self.path is not None and Path(self.path).is_file()
        except OSError:
            return False

    def open_report(self) -> None:
        if self.report_exists():
            self.open_path(self.path)
        else:
            self.status("No report to open yet.", "warn")

    def open_folder(self) -> None:
        if self.path is not None and Path(self.path).parent.is_dir():
            self.open_path(Path(self.path).parent)
        else:
            self.status("No folder to open yet.", "warn")

    def copy_for_ai(self, _e=None, path=None) -> None:
        target = path or (self.path if self.report_exists() else None)
        if target is None:
            self.status("No report to copy yet.", "warn")
            return
        try:
            text = Path(target).read_text(encoding="utf-8")
        except OSError as exc:
            self.status(f"Could not read the report: {exc}", "bad")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status(f"Copied {max(1, len(text) // 1024)} KB. Paste it into your AI chat.",
                    "ok")

    def paint_keys(self) -> None:
        no_report = self.screen == "done" and not (self.summary or {}).get("actions")
        self.keys_lbl.configure(text=KEY_HINTS["bad" if no_report else self.screen])

    def _on_escape(self, _e=None) -> None:
        if self.drawer_open:
            self.toggle_drawer()
        elif self.root.focus_get() is self.find_box.entry and self.find_var.get():
            self.find_var.set("")
        else:
            self.cancel()

    # -- run control ---------------------------------------------------------------- #
    def start(self, _e=None) -> None:
        if self.running:
            return
        cfg = self.current_cfg()
        try:
            argv = build_argv(cfg)
        except ValueError as exc:
            self.status(str(exc), "bad")
            return
        self.remember()
        self.drawer_open = False
        self.apply_drawer()
        preview = preview_tickers(cfg) or []
        self.model.reset(preview)
        self.running, self.screen, self.summary = True, "run", None
        self.t0, self.wall0 = time.monotonic(), time.time()
        self.path = expected_output_path(cfg)
        self.finish_early_flag = False
        self.q, self.cancel_ev = queue.Queue(), threading.Event()
        self.filter = "all"
        self.empty = ("Looking up the list. Tickers appear here as they are fetched.", "")
        self.log_buf = []
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_unread = False
        self.apply_log()
        self.append_log(f"$ stock_review {' '.join(argv)}\n\n")
        self.build_inputs()
        self.refresh_header()
        self.render_band()
        self.refresh_table()
        self.table.to_top()
        self.render_tape()
        self.paint_keys()
        self.status("Running. Cancel any time and you still get a partial report.")
        self.root.title(f"{APP_NAME} · running")
        progress = BoardProgress(self.q)
        threading.Thread(target=_gui._run_review,
                         args=(argv, self.q, progress, self.cancel_ev),
                         daemon=True).start()
        self.root.after(80, self.drain)

    def cancel(self, _e=None) -> None:
        if self.running:
            self.cancel_ev.set()
            self.status("Cancelling. Writing a partial report from what was fetched.",
                        "warn")

    def finish_early(self) -> None:
        if self.running:
            self.finish_early_flag = True
            self.cancel()

    def drain(self) -> None:
        changed = False
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "progress":
                    self.model.on_progress(payload)
                    changed = True
                elif kind == "retry":
                    self.model.on_retry(payload)
                    changed = True
                elif kind == "note":
                    self.model.on_note(payload)
                    changed = True
                elif kind == "done":
                    self.finish(payload)
                    return
        except queue.Empty:
            pass
        if changed:
            self.refresh_table()
            self.render_tape()
        self.update_run()
        self.root.after(80, self.drain)

    def finish(self, result: dict) -> None:
        self.running = False
        elapsed = time.monotonic() - (self.t0 or time.monotonic())
        try:
            stat = Path(self.path).stat()
            wrote, size_kb = stat.st_mtime >= self.wall0 - 2, stat.st_size // 1024
        except (OSError, TypeError):
            wrote, size_kb = False, 0
        fails = self.model.failed()
        self.summary = s = finish_summary(
            counts=self.model.counts(), fails=fails, code=result.get("code", 1),
            wrote=wrote, cancelled=bool(result.get("cancelled")),
            finish_early=self.finish_early_flag,
            file_name=Path(self.path).name if self.path else "", size_kb=size_kb,
            elapsed=elapsed, error=result.get("error"), note=self.model.note)
        self.screen = "done"
        self.root.title(APP_NAME)
        self.refresh_recent()
        self.build_inputs()
        self.refresh_header()
        self.render_band()
        self.refresh_table()
        self.table.to_top()
        self.render_tape()
        self.paint_keys()
        if not s["actions"]:
            self.log_auto = not self.log_open   # opened for the user, so closed for them
            self.log_open = True
            self.apply_log()
            self.status("No report was written.", "bad")
        elif s["kind"] == "ok":
            self.status(f"Done. {self.model.counts()['ok']} stocks, {fmt_clock(elapsed)}.",
                        "ok")
        else:
            self.status(f"Partial: {self.model.counts()['ok']} ok, {len(fails)} failed. "
                        "Research screen, not advice.", "warn")

    def on_close(self) -> None:
        if self.running and self.cancel_ev is not None:
            self.cancel_ev.set()
        self.remember()
        self.root.destroy()


def run(test_close_ms: int | None = None) -> int:
    """Open the Live board window and block until it is closed."""
    import tkinter as tk

    os.chdir(_gui.BASE_DIR)  # cli's relative reports/ and data/ paths live here
    if sys.platform.startswith("win"):
        try:  # crisp text on high-DPI displays; sizes below follow Tk's scaling
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - cosmetic
            pass
    root = tk.Tk()
    root._board = BoardApp(root)   # handle for demos / screenshots; unused otherwise
    if test_close_ms:
        root.after(test_close_ms, root.destroy)
    root.mainloop()
    return 0


def main(argv=None) -> int:  # argv kept for a uniform entry-point signature
    smoke = _gui.env_flag("STOCK_REVIEW_SMOKE_MS")
    return run(test_close_ms=smoke or None)


if __name__ == "__main__":
    sys.exit(main())
