"""Live board: the dark, table-first Stock Review window (design concept "v5").

An opt-in second front-end next to the Win98 one in :mod:`.gui`. It reuses that
module's worker plumbing (``build_argv`` -> ``cli.main`` on a thread, fed through
a queue) and only replaces the *presentation*:

* a sidebar to pick what to review, recent reports and a link to options;
* a hero strip (small ASCII rocket, one plain sentence, a progress bar);
* a per-ticker table that fills in live (fetched / retrying / failed + why);
* a result card that names the failed tickers and offers Copy for AI;
* a keyboard-hint footer.

Run it with ``python stock_review_board.py`` or ``python -m
stock_analyzer.review.gui_board``. stdlib-only (tkinter + ttk), like the rest of
the GUI, so PyInstaller can still freeze it.

Everything on screen is real data: the table rows come from the run itself, the
header tape shows the 1-month return of tickers as they are fetched, and a
missing value is simply left out (never 0, never a guess). No score, no advice.

The pure parts (``classify_error``, ``BoardModel`` and the little formatters)
carry no Tk so ``tests/test_gui_board.py`` can cover them offline.
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
from .format import pct
from .gui import QueueProgress, build_argv, expected_output_path, universe_hint
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
            ret = d.get("ret_1m")
            row.ret_1m = ret if isinstance(ret, (int, float)) else None
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
                       ret_1m=rec.get("ret_1m"))
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
        return f"Region: {cfg.get('source') or '…'}"
    if mode == "sector":
        region = (cfg.get("region") or "").strip()
        return f"{(cfg.get('sector') or '…').title()} sector" + (f" · {region}" if region else "")
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
# Look
# --------------------------------------------------------------------------- #
class P:
    """Dark slate palette. One muted accent; status colours are for status only."""
    BG, PANEL, PANEL2, LINE = "#14171c", "#1b1f26", "#232830", "#2b313b"
    INK, MUTED = "#e4e8ee", "#8a94a3"
    ACC, ACC_INK = "#5aa9c9", "#0c1a22"
    SEL, SEL_INK = "#232c38", "#bfe3f3"
    OK, WARN, BAD = "#5fbf8f", "#d4a24c", "#e0707a"
    ROW_WARN, ROW_BAD = "#2a2517", "#2b1b1e"
    HERO, ASCII = "#0f1216", "#6fb7d4"


ROCKET = ("   /\\\n"
          "  /  \\\n"
          " | () |\n"
          " |ASX |\n"
          " /|__|\\\n"
          "  {flame}")
ROCKET_FLAMES = ("/##\\", "/%%\\", "/**\\")
OK_BADGE = "  __\n /  \\\n| ok |\n \\__/"

SOURCES_LIST = [("curated", "Curated starter", "about 40 stocks"),
                ("source", "Region…", None),
                ("sector", "Sector…", None),
                ("custom", "My tickers…", None),
                ("file", "From a file…", None)]


def _pick_font(root, names, fallback):
    from tkinter import font as tkfont
    have = set(tkfont.families(root))
    for n in names:
        if n in have:
            return n
    return fallback


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #
def run(test_close_ms: int | None = None) -> int:
    """Open the Live board window and block until it is closed."""
    import tkinter as tk
    from tkinter import filedialog, ttk

    os.chdir(_gui.BASE_DIR)  # cli's relative reports/ and data/ paths live here
    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg=P.BG)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(1060, sw - 40), min(680, sh - 80)
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
    root.minsize(min(880, w), min(520, h))
    icon = _gui._icon_path()
    if icon is not None:
        try:
            root.iconbitmap(default=str(icon))
        except Exception:  # noqa: BLE001 - icon is cosmetic (and Windows-only)
            pass

    UI = _pick_font(root, ("Segoe UI", "Helvetica Neue", "DejaVu Sans", "Helvetica"), "TkDefaultFont")
    MONO = _pick_font(root, ("Consolas", "DejaVu Sans Mono", "Menlo", "Courier New"), "TkFixedFont")
    f_ui, f_small = (UI, 10), (UI, 9)
    f_bold, f_title, f_big = (UI, 10, "bold"), (UI, 13, "bold"), (UI, 17, "bold")
    f_mono, f_mono_s = (MONO, 10), (MONO, 9)

    saved = load_board_settings()
    settings = {  # the Options dialog edits these
        "fmt": saved.get("fmt", "md"), "workers": saved.get("workers", "6"),
        "limit": saved.get("limit", ""), "rich": bool(saved.get("rich", False)),
        "no_names": bool(saved.get("no_names", False)), "outdir": saved.get("outdir", ""),
    }
    mode_var = tk.StringVar(value=saved.get("mode", "curated"))
    source_var = tk.StringVar(value=saved.get("source", "america"))
    sector_var = tk.StringVar(value=saved.get("sector", "energy"))
    region_var = tk.StringVar(value=saved.get("region", ""))
    tickers_var = tk.StringVar(value=saved.get("tickers", ""))
    file_var = tk.StringVar(value=saved.get("file", ""))

    model = BoardModel()
    st = {"running": False, "t0": None, "wall0": 0.0, "q": None, "cancel": None,
          "path": None, "finish_early": False, "flame": 0, "pulse": 0,
          "after_run": False}

    # ---- ttk dark theme ----------------------------------------------------- #
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Board.Treeview", background=P.PANEL, fieldbackground=P.PANEL,
                    foreground=P.INK, rowheight=26, borderwidth=0, font=f_ui)
    style.configure("Board.Treeview", bordercolor=P.LINE, lightcolor=P.PANEL,
                    darkcolor=P.PANEL)
    style.configure("Board.Treeview.Heading", background=P.PANEL2, foreground=P.MUTED,
                    relief="flat", font=f_bold, padding=(10, 6))
    style.map("Board.Treeview", background=[("selected", P.SEL)],
              foreground=[("selected", P.SEL_INK)])
    style.map("Board.Treeview.Heading", background=[("active", P.PANEL2)])
    style.configure("Board.Vertical.TScrollbar", background=P.PANEL2, troughcolor=P.PANEL,
                    bordercolor=P.PANEL, arrowcolor=P.MUTED, lightcolor=P.PANEL2,
                    darkcolor=P.PANEL2)
    style.configure("Board.TCombobox", fieldbackground=P.PANEL, background=P.PANEL2,
                    foreground=P.INK, arrowcolor=P.MUTED, bordercolor=P.LINE,
                    lightcolor=P.LINE, darkcolor=P.LINE, selectbackground=P.SEL,
                    selectforeground=P.SEL_INK)
    style.map("Board.TCombobox", fieldbackground=[("readonly", P.PANEL)],
              foreground=[("readonly", P.INK)])
    root.option_add("*TCombobox*Listbox.background", P.PANEL)
    root.option_add("*TCombobox*Listbox.foreground", P.INK)
    root.option_add("*TCombobox*Listbox.selectBackground", P.SEL)
    root.option_add("*TCombobox*Listbox.selectForeground", P.SEL_INK)

    def lbl(parent, text="", fg=P.INK, bg=None, font=f_ui, **kw):
        return tk.Label(parent, text=text, fg=fg, bg=bg or parent["bg"], font=font,
                        anchor=kw.pop("anchor", "w"), **kw)

    def button(parent, text, command, kind="plain"):
        """Flat button. kind: plain | primary | danger. Disabled = dim text on the
        panel colour with no border, so state is obvious without relying on hue."""
        fg, bg, border = {"primary": (P.ACC_INK, P.ACC, P.ACC),
                          "danger": (P.BAD, P.PANEL, "#6a3a40")}.get(
            kind, (P.INK, P.PANEL, "#394150"))
        b = tk.Button(parent, text=text, command=command, fg=fg, bg=bg, font=f_bold,
                      relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                      activebackground=bg, activeforeground=fg,
                      highlightthickness=1, highlightbackground=border,
                      highlightcolor=P.ACC, disabledforeground="#566070", takefocus=1)

        def _enter(_e):
            if str(b["state"]) != "disabled":
                b.configure(bg=P.SEL if kind != "primary" else "#78bdd8")

        def _leave(_e):
            b.configure(bg=bg)
        b.bind("<Enter>", _enter)
        b.bind("<Leave>", _leave)
        b._base = (fg, bg, border)  # for set_enabled

        return b

    def set_enabled(b, on: bool):
        fg, bg, border = b._base
        b.configure(state="normal" if on else "disabled",
                    bg=bg if on else P.BG,
                    highlightbackground=border if on else P.LINE,
                    cursor="hand2" if on else "arrow")

    # ---- layout skeleton ----------------------------------------------------- #
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(1, weight=1)

    top = tk.Frame(root, bg=P.PANEL, highlightthickness=1, highlightbackground=P.LINE)
    top.grid(row=0, column=0, columnspan=2, sticky="ew")
    lbl(top, "STOCK REVIEW", P.INK, font=(MONO, 11, "bold")).pack(side="left", padx=(16, 14), pady=9)
    tape = tk.Text(top, height=1, bg=P.PANEL, fg=P.MUTED, font=f_mono_s, bd=0,
                   highlightthickness=0, wrap="none", cursor="arrow", takefocus=0)
    tape.tag_configure("up", foreground=P.OK)
    tape.tag_configure("down", foreground=P.BAD)
    tape.pack(side="left", fill="x", expand=True, pady=9)
    run_btn = button(top, "Run review", lambda: start(), "primary")
    run_btn.pack(side="right", padx=14, pady=6)
    cancel_btn = button(top, "Cancel", lambda: cancel(), "danger")

    side = tk.Frame(root, bg=P.PANEL, width=210, highlightthickness=1,
                    highlightbackground=P.LINE)
    side.grid(row=1, column=0, sticky="ns")
    side.grid_propagate(False)
    main = tk.Frame(root, bg=P.BG)
    main.grid(row=1, column=1, sticky="nsew", padx=18, pady=14)
    main.grid_columnconfigure(0, weight=1)
    main.grid_rowconfigure(3, weight=1)

    foot = tk.Frame(root, bg=P.PANEL, highlightthickness=1, highlightbackground=P.LINE)
    foot.grid(row=2, column=0, columnspan=2, sticky="ew")
    keys_var = tk.StringVar()
    status_var = tk.StringVar(value="Ready · not financial advice")
    lbl(foot, textvariable=keys_var, fg=P.MUTED, font=f_small).pack(side="left", padx=14, pady=6)
    status_lbl = lbl(foot, textvariable=status_var, fg=P.MUTED, font=f_small, anchor="e")
    status_lbl.pack(side="right", padx=14)

    def status(msg: str, kind: str = "muted"):
        status_var.set(msg)
        status_lbl.configure(fg={"muted": P.MUTED, "warn": P.WARN, "bad": P.BAD,
                                 "ok": P.OK}.get(kind, P.MUTED))

    # ---- sidebar -------------------------------------------------------------- #
    def side_header(text):
        lbl(side, text.upper(), P.MUTED, font=(UI, 9, "bold")).pack(
            fill="x", padx=18, pady=(16, 4))

    source_items: dict[str, tuple[tk.Frame, list]] = {}
    side_header("Review")
    for key, title, sub in SOURCES_LIST:
        fr = tk.Frame(side, bg=P.PANEL, cursor="hand2")
        fr.pack(fill="x", padx=8)
        t = lbl(fr, title, P.INK, font=f_ui)
        t.pack(fill="x", padx=10, pady=(6, 0 if sub else 6))
        kids = [t]
        if sub:
            s = lbl(fr, sub, P.MUTED, font=f_small)
            s.pack(fill="x", padx=10, pady=(0, 6))
            kids.append(s)
        source_items[key] = (fr, kids)
        for wdg in (fr, *kids):
            wdg.bind("<Button-1>", lambda _e, k=key: set_mode(k))

    side_header("Recent reports")
    recent_box = tk.Frame(side, bg=P.PANEL)
    recent_box.pack(fill="x", padx=8)

    def refresh_recent():
        for child in recent_box.winfo_children():
            child.destroy()
        items = recent_reports()
        if not items:
            lbl(recent_box, "None yet", P.MUTED, font=f_small).pack(fill="x", padx=10, pady=4)
        for it in items:
            fr = tk.Frame(recent_box, bg=P.PANEL, cursor="hand2")
            fr.pack(fill="x")
            a = lbl(fr, it["label"], P.INK)
            a.pack(fill="x", padx=10, pady=(5, 0))
            b = lbl(fr, it["detail"], P.MUTED, font=f_small)
            b.pack(fill="x", padx=10, pady=(0, 5))
            for wdg in (fr, a, b):
                wdg.bind("<Button-1>", lambda _e, p=it["path"]: open_path(p))

    side_header("Options")
    opts_summary = lbl(side, "", P.MUTED, font=f_small, justify="left", wraplength=180)
    opts_summary.pack(fill="x", padx=18)
    opts_link = lbl(side, "More options…", P.ACC, font=f_small, cursor="hand2")
    opts_link.pack(fill="x", padx=18, pady=(4, 0))

    def refresh_opts_summary():
        bits = [settings["fmt"], f"{settings['workers'] or '6'} workers",
                f"limit {settings['limit']}" if settings["limit"] else "no limit"]
        folder = settings["outdir"] or "default folder"
        opts_summary.configure(text=" · ".join(bits) + f"\n{folder}")

    # ---- main: hero / result card ---------------------------------------------- #
    hero = tk.Frame(main, bg=P.HERO, highlightthickness=1, highlightbackground=P.LINE)
    hero.grid(row=0, column=0, sticky="ew")
    hero.grid_columnconfigure(1, weight=1)
    rocket = tk.Label(hero, text="", fg=P.ASCII, bg=P.HERO, font=(MONO, 9),
                      justify="left", anchor="nw", width=8)
    rocket.grid(row=0, column=0, rowspan=2, padx=(18, 10), pady=8)
    hero_text = tk.Frame(hero, bg=P.HERO)
    hero_text.grid(row=0, column=1, sticky="ew", pady=(12, 0))
    pulse = tk.Canvas(hero_text, width=14, height=14, bg=P.HERO, highlightthickness=0)
    pulse_dot = pulse.create_oval(3, 3, 11, 11, fill=P.OK, outline="")
    hero_title = lbl(hero_text, "", P.INK, P.HERO, font=f_title)
    hero_title.pack(side="left")
    hero_sub = lbl(hero, "", P.MUTED, P.HERO, wraplength=620, justify="left")
    hero_sub.grid(row=1, column=1, sticky="nw", pady=(0, 6))
    track = tk.Canvas(hero, height=14, bg=P.HERO, highlightthickness=0)
    finish_btn = button(hero, "Finish now", lambda: finish_early())

    card = tk.Frame(main, bg=P.PANEL, highlightthickness=1, highlightbackground=P.LINE)
    card_edge = tk.Frame(card, width=4, bg=P.WARN)
    card_edge.pack(side="left", fill="y")
    card_badge = tk.Label(card, text=OK_BADGE, fg=P.ASCII, bg=P.PANEL, font=(MONO, 9),
                          justify="left")
    card_badge.pack(side="left", padx=(14, 6), pady=8)
    card_body = tk.Frame(card, bg=P.PANEL)
    card_body.pack(side="left", fill="x", expand=True, padx=8, pady=10)
    card_title = lbl(card_body, "", P.INK, P.PANEL, font=f_big)
    card_title.pack(fill="x")
    card_sub = lbl(card_body, "", P.MUTED, P.PANEL, justify="left", wraplength=480)
    card_sub.pack(fill="x", pady=(2, 0))
    card_tags = tk.Frame(card_body, bg=P.PANEL)
    card_tags.pack(fill="x", pady=(6, 0))
    card_btns = tk.Frame(card_body, bg=P.PANEL)
    card_btns.pack(fill="x", pady=(10, 0))
    copy_btn = button(card_btns, "Copy for AI", lambda: copy_for_ai(), "primary")
    open_btn = button(card_btns, "Open report", lambda: open_report())
    folder_btn = button(card_btns, "Open folder", lambda: open_folder())
    retry_btn = button(card_btns, "Retry failed", lambda: start(), "danger")
    for b_ in (copy_btn, open_btn, folder_btn, retry_btn):
        b_.pack(side="left", padx=(0, 8))

    # ---- main: context row, stats, table, log ---------------------------------- #
    ctx = tk.Frame(main, bg=P.BG)
    ctx.grid(row=1, column=0, sticky="ew", pady=(12, 0))
    ctx.grid_columnconfigure(0, weight=1)
    ctx_title = lbl(ctx, "", P.INK, P.BG, font=f_big)
    ctx_title.grid(row=0, column=0, sticky="w")
    ctx_hint = lbl(ctx, "", P.MUTED, P.BG, font=f_small)
    ctx_hint.grid(row=1, column=0, sticky="w")
    inputs = tk.Frame(ctx, bg=P.BG)
    inputs.grid(row=2, column=0, sticky="ew", pady=(8, 0))

    stats = tk.Frame(main, bg=P.BG)
    stats.grid(row=2, column=0, sticky="ew", pady=(12, 0))
    stat_vars = {k: tk.StringVar() for k in ("ok", "retry", "fail", "time")}
    stat_lbls = {}
    for i, (k, word, col) in enumerate((("ok", "fetched", P.INK), ("retry", "retrying", P.WARN),
                                        ("fail", "failed", P.BAD), ("time", "", P.INK))):
        cell = tk.Frame(stats, bg=P.BG)
        cell.pack(side="left", padx=(0, 26))
        n = lbl(cell, "", col, P.BG, font=(UI, 17, "bold"))
        n.pack(side="left")
        w_ = lbl(cell, "", P.MUTED, P.BG)
        w_.pack(side="left", padx=(5, 0), pady=(5, 0))
        stat_lbls[k] = (n, w_, word, col)

    table_wrap = tk.Frame(main, bg=P.PANEL, highlightthickness=1, highlightbackground=P.LINE)
    table_wrap.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
    table_wrap.grid_columnconfigure(0, weight=1)
    table_wrap.grid_rowconfigure(0, weight=1)
    tree = ttk.Treeview(table_wrap, style="Board.Treeview", show="headings",
                        columns=("ticker", "name", "status", "ret"), selectmode="browse")
    for col, text, width, anchor, stretch in (("ticker", "Ticker", 110, "w", False),
                                              ("name", "Name", 250, "w", True),
                                              ("status", "Status", 300, "w", True),
                                              ("ret", "1-month", 90, "e", False)):
        tree.heading(col, text=text, anchor=anchor)
        tree.column(col, width=width, anchor=anchor, stretch=stretch, minwidth=60)
    for tag, fg, bg in (("ok", P.OK, None), ("retry", P.WARN, P.ROW_WARN),
                        ("fail", P.BAD, P.ROW_BAD), ("queued", P.MUTED, None)):
        tree.tag_configure(tag, foreground=fg, **({"background": bg} if bg else {}))
    vsb = ttk.Scrollbar(table_wrap, orient="vertical", style="Board.Vertical.TScrollbar",
                        command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    empty_lbl = lbl(table_wrap, "", P.MUTED, P.PANEL, justify="center", wraplength=420)

    log_wrap = tk.Frame(main, bg=P.PANEL, highlightthickness=1, highlightbackground=P.LINE)
    log = tk.Text(log_wrap, height=5, bg=P.PANEL, fg=P.MUTED, font=f_mono_s, bd=0,
                  wrap="word", state="disabled", takefocus=1, highlightthickness=1,
                  highlightbackground=P.PANEL, highlightcolor=P.ACC, padx=10, pady=6)
    log.pack(fill="both", expand=True)
    show_log = {"on": False}

    # ---- helpers ---------------------------------------------------------------- #
    def current_cfg() -> dict:
        return {"mode": mode_var.get(), "source": source_var.get().strip(),
                "sector": sector_var.get().strip(), "region": region_var.get().strip(),
                "tickers": tickers_var.get(), "file": file_var.get(),
                "limit": settings["limit"], "workers": settings["workers"],
                "fmt": settings["fmt"], "rich": settings["rich"],
                "no_names": settings["no_names"], "outdir": settings["outdir"]}

    def remember():
        data = {"mode": mode_var.get(), "source": source_var.get(),
                "sector": sector_var.get(), "region": region_var.get(),
                "tickers": tickers_var.get(), "file": file_var.get(), **settings}
        save_board_settings(data)

    def append_log(text: str):
        log.configure(state="normal")
        log.insert("end", text)
        log.see("end")
        log.configure(state="disabled")

    def toggle_log(_e=None):
        show_log["on"] = not show_log["on"]
        apply_log()

    def apply_log():
        if show_log["on"]:
            log_wrap.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        else:
            log_wrap.grid_remove()

    def open_path(path):
        try:
            p = Path(path)
            if sys.platform.startswith("win"):
                os.startfile(str(p))  # type: ignore[attr-defined]  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            status(f"Could not open it: {exc}", "bad")

    def report_exists() -> bool:
        p = st["path"]
        try:
            return p is not None and Path(p).is_file()
        except OSError:
            return False

    def open_report():
        if report_exists():
            open_path(st["path"])
        else:
            status("No report to open yet.", "warn")

    def open_folder():
        p = st["path"]
        if p is not None and Path(p).parent.is_dir():
            open_path(Path(p).parent)
        else:
            status("No folder to open yet.", "warn")

    def copy_for_ai(_e=None):
        if not report_exists():
            status("No report to copy yet.", "warn")
            return
        try:
            text = Path(st["path"]).read_text(encoding="utf-8")
        except OSError as exc:
            status(f"Could not read the report: {exc}", "bad")
            return
        root.clipboard_clear()
        root.clipboard_append(text)
        status(f"Copied {len(text) // 1024} KB. Paste it into your AI chat.", "ok")

    # ---- table / hero rendering ---------------------------------------------- #
    def fmt_ret(r: Row) -> str:
        return pct(r.ret_1m, sign=True) if r.ret_1m is not None else ""

    def fill_table():
        tree.delete(*tree.get_children())
        order = sorted(model.rows.values(),
                       key=lambda r: {"fail": 0, "retry": 1}.get(r.state, 2)) \
            if not st["running"] else list(model.rows.values())
        for r in order:
            mark = {"ok": "✓ ", "retry": "↻ ", "fail": "✕ ", "queued": "· "}[r.state]
            tree.insert("", "end", iid=r.ticker, tags=(r.state,),
                        values=(r.ticker, r.name, mark + r.detail, fmt_ret(r)))

    def update_row(t: str):
        r = model.rows[t]
        mark = {"ok": "✓ ", "retry": "↻ ", "fail": "✕ ", "queued": "· "}[r.state]
        vals = (r.ticker, r.name, mark + r.detail, fmt_ret(r))
        if tree.exists(t):
            tree.item(t, values=vals, tags=(r.state,))
        else:
            tree.insert("", "end", iid=t, values=vals, tags=(r.state,))
            tree.see(t)

    def set_empty(text: str):
        if text and not model.rows:
            empty_lbl.configure(text=text)
            empty_lbl.place(relx=0.5, rely=0.45, anchor="center")
        else:
            empty_lbl.place_forget()

    def draw_track():
        track.delete("all")
        wd = max(track.winfo_width(), 10)
        track.create_rectangle(0, 4, wd, 10, fill=P.PANEL2, outline="")
        c = model.counts()
        total = c["total"] or 1
        ok_w = int(wd * c["ok"] / total)
        fail_w = int(wd * c["fail_raw"] / total)
        track.create_rectangle(0, 4, ok_w, 10, fill=P.ACC, outline="")
        if fail_w:
            track.create_rectangle(ok_w, 4, ok_w + fail_w, 10, fill=P.WARN, outline="")
        track.create_text(min(ok_w + fail_w, wd - 6), 7, text=">", fill=P.ASCII,
                          font=f_mono_s)

    def render_tape():
        tape.configure(state="normal")
        tape.delete("1.0", "end")
        items = model.tape() if st["running"] or model.rows else []
        if not items:
            tape.insert("end", "Research screen, not financial advice")
        for i, (t, r) in enumerate(items):
            tape.insert("end", ("   " if i else "") + f"{t} 1m {pct(r, sign=True)}",
                        "up" if r >= 0 else "down")
        tape.configure(state="disabled")

    def render_run():
        c = model.counts()
        hero_title.configure(text=model.headline())
        hero_sub.configure(text=model.mood())
        draw_track()
        elapsed = time.monotonic() - st["t0"] if st["t0"] else 0
        for k, text in (("ok", str(c["ok"])), ("retry", str(c["retry"])),
                        ("fail", str(c["fail"]))):
            n, w_, word, col = stat_lbls[k]
            n.configure(text=text)
            w_.configure(text=word)
        n, w_, _word, _col = stat_lbls["time"]
        n.configure(text=fmt_clock(elapsed))
        w_.configure(text=eta_text(c["done"], c["total"], elapsed))
        has_wait = bool(model.wait) or bool(model.note and "retry" in model.note.lower())
        if has_wait and c["ok"]:
            finish_btn.grid(row=0, column=2, rowspan=2, padx=16)
        else:
            finish_btn.grid_remove()

    # ---- screens -------------------------------------------------------------- #
    def show_screen(which: str):
        """idle | run | done: which of hero / card / context / stats are visible."""
        for wdg in (hero, card, ctx, stats):
            wdg.grid_remove()
        if which == "idle":
            hero.grid(row=0, column=0, sticky="ew")
            ctx.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            hero_title.configure(text="Ready when you are")
            hero_sub.configure(text="Pick a list, press Run, and get one file to hand to an AI.")
            pulse.pack_forget()
            track.grid_remove()
            finish_btn.grid_remove()
        elif which == "run":
            hero.grid(row=0, column=0, sticky="ew")
            stats.grid(row=2, column=0, sticky="ew", pady=(12, 0))
            pulse.pack(side="left", padx=(0, 8), before=hero_title)
            track.grid(row=2, column=1, sticky="ew", padx=(0, 18), pady=(0, 14))
        else:
            card.grid(row=0, column=0, sticky="ew")
            ctx.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        st["screen"] = which
        keys_var.set({"idle": "F5  Run      Ctrl+L  Log      Ctrl+O  Open report",
                      "run": "Esc  Cancel      Ctrl+L  Log",
                      "done": "Ctrl+Shift+C  Copy for AI      Ctrl+O  Open report      F5  Run again"}[which])

    def animate():
        if st["running"]:
            st["flame"] = (st["flame"] + 1) % len(ROCKET_FLAMES)
            rocket.configure(text=ROCKET.format(flame=ROCKET_FLAMES[st["flame"]]))
            st["pulse"] ^= 1
            pulse.itemconfigure(pulse_dot, fill=P.OK if st["pulse"] else "#2f6b4d")
            if st.get("screen") == "run":
                render_run()
        elif st.get("screen") == "idle":
            rocket.configure(text=ROCKET.format(flame=ROCKET_FLAMES[0]))
        root.after(500, animate)

    # ---- context inputs (per source) ------------------------------------------- #
    def clear_inputs():
        for child in inputs.winfo_children():
            child.destroy()

    def combo(parent, var, values, width):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width,
                          state="readonly", style="Board.TCombobox", font=f_ui)
        cb.bind("<<ComboboxSelected>>", lambda _e: on_inputs_changed())
        return cb

    def entry(parent, var, placeholder=""):
        e = tk.Entry(parent, textvariable=var, bg=P.PANEL, fg=P.INK, font=f_ui,
                     insertbackground=P.INK, relief="flat", bd=0,
                     highlightthickness=1, highlightbackground="#394150",
                     highlightcolor=P.ACC)
        e.bind("<Control-Key-a>", lambda ev: (ev.widget.select_range(0, "end"), "break")[1])
        e.bind("<KeyRelease>", lambda _e: on_inputs_changed())
        if placeholder:
            ph = lbl(e, placeholder, P.MUTED, P.PANEL)
            ph.place(x=8, rely=0.5, anchor="w")
            ph.bind("<Button-1>", lambda _e: e.focus_set())

            def sync(*_):
                if var.get():
                    ph.place_forget()
                else:
                    ph.place(x=8, rely=0.5, anchor="w")
            tid = var.trace_add("write", sync)
            # the entry is rebuilt whenever the source changes; drop its trace
            e.bind("<Destroy>", lambda ev, t=tid: ev.widget is e and
                   var.trace_remove("write", t), add="+")
            sync()
        return e

    def build_inputs():
        clear_inputs()
        mode = mode_var.get()
        if mode == "source":
            combo(inputs, source_var, sorted(SOURCES), 24).pack(side="left", ipady=3)
        elif mode == "sector":
            combo(inputs, sector_var, sorted(SECTORS), 20).pack(side="left", ipady=3)
            lbl(inputs, "in region", P.MUTED, P.BG).pack(side="left", padx=10)
            combo(inputs, region_var, [""] + sorted(REGION_RECORDS), 18).pack(side="left", ipady=3)
        elif mode == "custom":
            entry(inputs, tickers_var, "Add tickers, e.g. AAPL MSFT BHP.AX").pack(
                side="left", fill="x", expand=True, ipady=6)
        elif mode == "file":
            entry(inputs, file_var, "Path to a text or CSV file of tickers").pack(
                side="left", fill="x", expand=True, ipady=6)
            button(inputs, "Browse…", pick_file).pack(side="left", padx=(8, 0))

    def pick_file():
        path = filedialog.askopenfilename(
            parent=root, title="Choose a tickers file",
            filetypes=[("Tickers", "*.txt *.csv"), ("All files", "*.*")])
        if path:
            file_var.set(path)
            on_inputs_changed()

    def refresh_preview():
        """Idle only: show the list a run would review, when known offline."""
        if st["running"] or st.get("screen") == "run":
            return
        cfg = current_cfg()
        ctx_title.configure(text=mode_title(cfg))
        hints = universe_hint(cfg["mode"], cfg["source"], cfg["sector"], cfg["region"])
        ctx_hint.configure(text=hints)
        tickers = preview_tickers(cfg)
        model.reset(tickers or ())
        fill_table()
        if tickers is None:
            set_empty("This list is looked up when you press Run.\n"
                      "Each ticker will appear here as it is fetched.")
        elif not tickers:
            set_empty("Nothing to review yet. Add some tickers above.")
        else:
            set_empty("")
        render_tape()

    def on_inputs_changed():
        refresh_preview()

    def set_mode(key: str):
        if st["running"]:
            return
        mode_var.set(key)
        for k, (fr, kids) in source_items.items():
            on = k == key
            for wdg in (fr, *kids):
                wdg.configure(bg=P.SEL if on else P.PANEL)
            kids[0].configure(fg=P.SEL_INK if on else P.INK,
                              font=f_bold if on else f_ui)
        if st.get("screen") == "done":
            show_screen("idle")
        build_inputs()
        refresh_preview()

    # ---- run control ------------------------------------------------------------ #
    def start(_e=None):
        if st["running"]:
            return
        cfg = current_cfg()
        try:
            argv = build_argv(cfg)
        except ValueError as exc:
            status(str(exc), "bad")
            return
        remember()
        preview = preview_tickers(cfg) or []
        model.reset(preview)
        st.update(running=True, t0=time.monotonic(), wall0=time.time(),
                  path=expected_output_path(cfg), finish_early=False,
                  q=queue.Queue(), cancel=threading.Event())
        run_btn.pack_forget()
        cancel_btn.pack(side="right", padx=14, pady=6)
        set_enabled(cancel_btn, True)
        set_empty("")
        fill_table()
        show_screen("run")
        status("Running. Hit Cancel any time, you still get a partial report.")
        root.title(f"{APP_NAME} · running")
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")
        append_log(f"$ stock_review {' '.join(argv)}\n\n")
        show_log["on"] = True
        apply_log()
        progress = BoardProgress(st["q"])
        threading.Thread(target=_gui._run_review,
                         args=(argv, st["q"], progress, st["cancel"]),
                         daemon=True).start()
        render_run()
        render_tape()
        root.after(80, drain)

    def cancel(_e=None):
        if st["running"]:
            st["cancel"].set()
            status("Cancelling... writing a partial report from what was fetched.", "warn")

    def finish_early():
        if st["running"]:
            st["finish_early"] = True
            cancel()

    def drain():
        q = st["q"]
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    append_log(payload)
                elif kind == "progress":
                    model.on_progress(payload)
                    update_row(payload["ticker"])
                elif kind == "retry":
                    model.on_retry(payload)
                    update_row(payload["ticker"])
                elif kind == "note":
                    model.on_note(payload)
                elif kind == "done":
                    finish(payload)
                    return
        except queue.Empty:
            pass
        render_run()
        render_tape()
        root.after(80, drain)

    def tag_chip(parent, text, fg):
        return tk.Label(parent, text=text, fg=fg, bg=P.PANEL2, font=f_mono_s,
                        padx=6, pady=1)

    def finish(result: dict):
        st["running"] = False
        elapsed = time.monotonic() - (st["t0"] or time.monotonic())
        cancel_btn.pack_forget()
        run_btn.pack(side="right", padx=14, pady=6)
        run_btn.configure(text="Run again")
        set_enabled(run_btn, True)
        root.title(APP_NAME)
        c = model.counts()
        fails = model.failed()
        try:
            wrote = Path(st["path"]).stat().st_mtime >= st["wall0"] - 2
        except (OSError, TypeError):
            wrote = False
        code, error = result.get("code", 1), result.get("error")
        for child in card_tags.winfo_children():
            child.destroy()
        warn = bool(fails) or result.get("cancelled")
        if code not in (0, 130) or not wrote:
            card_edge.configure(bg=P.BAD)
            card_title.configure(text="No report this time")
            card_sub.configure(
                text=(error or model.note or "Nothing could be fetched. Check your "
                      "connection, wait a few minutes and run again; fetched tickers "
                      "are cached, so a rerun resumes.").strip())
            card_badge.configure(text="  __\n /  \\\n| !! |\n \\__/")
            ok_actions = False
        else:
            card_edge.configure(bg=P.WARN if warn else P.OK)
            card_badge.configure(text=OK_BADGE)
            total = c["total"] or c["done"]
            title = f"{c['ok']} of {total} fetched"
            if fails:
                title += f" · {len(fails)} failed"
            if result.get("cancelled"):
                title += " · " + ("finished early" if st["finish_early"] else "cancelled")
            card_title.configure(text=title)
            card_sub.configure(text=f"Saved {Path(st['path']).name} · "
                                    f"{Path(st['path']).stat().st_size // 1024} KB · "
                                    f"{fmt_clock(elapsed)}. Research screen, not advice.")
            for r in fails[:6]:
                tag_chip(card_tags, f"{r.ticker} {r.detail.lower()}",
                         P.BAD).pack(side="left", padx=(0, 6))
            if len(fails) > 6:
                tag_chip(card_tags, f"+{len(fails) - 6} more", P.MUTED).pack(side="left")
            ok_actions = True
        for b_ in (copy_btn, open_btn, folder_btn):
            set_enabled(b_, ok_actions)
        if ok_actions and fails or result.get("cancelled"):
            retry_btn.pack(side="left", padx=(0, 8))
            retry_btn.configure(text=f"Retry {len(fails)} failed" if fails else "Run again")
        else:
            retry_btn.pack_forget()
        show_screen("done")
        model_rows_done()
        show_log["on"] = bool(code not in (0, 130) or not wrote)
        apply_log()
        if ok_actions and not warn:
            status(f"Done. {c['ok']} stocks, {fmt_clock(elapsed)}.", "ok")
        elif ok_actions:
            status(f"Partial: {c['ok']} ok, {len(fails)} failed. Research screen, not advice.", "warn")
        else:
            status("No report was written.", "bad")
        refresh_recent()
        render_tape()

    def model_rows_done():
        fill_table()
        set_empty("")

    # ---- options dialog ------------------------------------------------------------ #
    def open_options(_e=None):
        if st["running"]:
            return
        win = tk.Toplevel(root)
        win.title("Options")
        win.configure(bg=P.BG)
        win.transient(root)
        win.resizable(False, False)
        body = tk.Frame(win, bg=P.BG, padx=20, pady=16)
        body.pack()
        vars_ = {k: tk.StringVar(value=str(settings[k])) for k in ("fmt", "workers", "limit", "outdir")}
        rich_v = tk.BooleanVar(value=settings["rich"])
        nn_v = tk.BooleanVar(value=settings["no_names"])

        def row(r, text, widget):
            lbl(body, text, P.INK, P.BG).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 14))
            widget.grid(row=r, column=1, sticky="w", pady=6)

        cb = ttk.Combobox(body, textvariable=vars_["fmt"], values=("md", "csv", "json"),
                          width=8, state="readonly", style="Board.TCombobox", font=f_ui)
        row(0, "Format", cb)
        row(1, "Workers", entry(body, vars_["workers"]))
        row(2, "Limit (blank = all)", entry(body, vars_["limit"]))
        folder_row = tk.Frame(body, bg=P.BG)
        fe = entry(folder_row, vars_["outdir"], "default reports folder")
        fe.configure(width=34)
        fe.pack(side="left", ipady=4)

        def browse():
            d = filedialog.askdirectory(parent=win, title="Where should reports go?")
            if d:
                vars_["outdir"].set(d)
        button(folder_row, "Browse…", browse).pack(side="left", padx=(8, 0))
        row(3, "Report folder", folder_row)
        for r, text, var in ((4, "Rich (human-friendly) report", rich_v),
                             (5, "Drop company names", nn_v)):
            tk.Checkbutton(body, text=text, variable=var, fg=P.INK, bg=P.BG, font=f_ui,
                           selectcolor=P.PANEL, activebackground=P.BG,
                           activeforeground=P.INK, highlightthickness=1,
                           highlightbackground=P.BG, highlightcolor=P.ACC).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=2)
        btns = tk.Frame(body, bg=P.BG)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))

        def apply_():
            settings.update(fmt=vars_["fmt"].get() or "md",
                            workers=vars_["workers"].get().strip(),
                            limit=vars_["limit"].get().strip(),
                            outdir=vars_["outdir"].get().strip(),
                            rich=rich_v.get(), no_names=nn_v.get())
            refresh_opts_summary()
            remember()
            win.destroy()
        button(btns, "Cancel", win.destroy).pack(side="right", padx=(8, 0))
        button(btns, "Save", apply_, "primary").pack(side="right")
        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Return>", lambda _e: apply_())
        win.update_idletasks()
        win.geometry("+{}+{}".format(
            root.winfo_rootx() + max(0, (root.winfo_width() - win.winfo_width()) // 2),
            root.winfo_rooty() + max(0, (root.winfo_height() - win.winfo_height()) // 3)))
        win.grab_set()
        win.focus_set()

    opts_link.bind("<Button-1>", open_options)

    # ---- bindings & first paint ------------------------------------------------------ #
    root.bind("<F5>", start)
    root.bind("<Escape>", cancel)
    root.bind("<Control-o>", lambda _e: open_report())
    root.bind("<Control-O>", lambda _e: open_folder())
    root.bind("<Control-l>", toggle_log)
    root.bind("<Control-Shift-C>", copy_for_ai)
    root.bind("<Control-Shift-c>", copy_for_ai)

    def on_close():
        if st["running"]:
            st["cancel"].set()
        remember()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    # public handles for tests / demos (no effect on normal use)
    root._board = {"start": start, "cancel": cancel, "set_mode": set_mode,
                   "state": st, "model": model, "vars": {"tickers": tickers_var,
                                                         "mode": mode_var},
                   "settings": settings, "options": open_options}

    refresh_opts_summary()
    refresh_recent()
    apply_log()
    show_screen("idle")
    set_mode(mode_var.get())
    animate()
    if test_close_ms:
        root.after(test_close_ms, root.destroy)
    root.mainloop()
    return 0


def main(argv=None) -> int:  # argv kept for a uniform entry-point signature
    smoke = _gui.env_flag("STOCK_REVIEW_SMOKE_MS")
    return run(test_close_ms=smoke or None)


if __name__ == "__main__":
    sys.exit(main())
