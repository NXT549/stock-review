"""TkAnimator — plays the ``ascii_art`` cast inside the windowed app.

The terminal build renders the animation by spraying ANSI escapes at a real TTY
(:class:`ascii_art.Animator`). The windowed ``.exe`` has no TTY, so this module
**reuses the exact same frame data** (:data:`ascii_art.ANIMATIONS` — plain
character grids plus per-scene ``color`` / ``accents`` / ``captions``) but swaps
the renderer for Tk colour tags and the background thread for ``root.after()``.

Design constraints:
  * Tk is **not** thread-safe, so every widget write happens on the main thread
    via the ``after`` loop. The worker thread only ever pushes progress numbers
    over a queue (see ``gui.QueueProgress``); the GUI hands them here through
    :meth:`TkAnimator.set_progress`.
  * Purely cosmetic: :meth:`_draw` is wrapped so any failure just freezes the
    last frame instead of taking down the app.
  * Consolas (ships with Windows, so present in the frozen bundle too) renders
    the block / arrow / tick glyphs directly, so no ASCII fallback is needed.
"""
from __future__ import annotations

import random
import time
from tkinter import font as tkfont

from . import ascii_art as A

# ANSI-256 palette name -> Tk hex. Decorative approximations of ascii_art.COLOR;
# exact shades don't matter, only that each scene reads in its own hue.
TK_COLOR = {
    "orange": "#ff8700", "cyan": "#00ffff", "green": "#3bfb3b",
    "yellow": "#ffe000", "magenta": "#ff2bff", "red": "#ff3b3b",
    "blue": "#3bafff", "teal": "#00d7d7", "white": "#ffffff",
    "gray": "#9a9a9a", "pink": "#ff9bff", "gold": "#d7af00", "purple": "#b58bff",
}
DIM = "#6a6a6a"        # HUD scaffolding (labels, separators, empty bar cells)


class TkAnimator:
    """Render :data:`ascii_art.ANIMATIONS` into a Tk ``Text`` widget.

    Lifecycle mirrors the terminal ``Animator``: :meth:`start`, feed progress via
    :meth:`set_progress`, :meth:`stop`. ``start`` resets all counters so the same
    instance can drive one run after another.
    """

    def __init__(self, root, text_widget, *, switch: float = A.SWITCH_SECONDS,
                 fps: float = A.FPS) -> None:
        self.root = root
        self.t = text_widget
        self.switch = max(1.0, switch)
        self.interval = int(1000 / max(1.0, fps))
        self.ai = self.fi = 0
        self.t0 = self.run0 = time.monotonic()
        self.prog = {"done": 0, "total": 1, "ok": 0, "failed": 0, "ticker": ""}
        self.tape: list[tuple[str, str]] = []   # (ticker, "ok" | "fail" | "retry")
        self.pulse = [3] * A.PULSE_WIDTH
        self.rng = random.Random(0xC1A0D)
        self.idle = False   # attract mode: animate, but show a tagline not a 0% bar
        self.retries = 0            # backoff waits seen this run (HUD counter)
        self.note: str | None = None   # one-line CLI status (e.g. retry rounds)
        self._job = None
        for name, hexv in TK_COLOR.items():
            self.t.tag_configure(name, foreground=hexv)
        self.t.tag_configure("dim", foreground=DIM)
        # Fit-to-panel: the scenes are a fixed 100x30 character canvas, so a
        # fixed 8pt font left them huddled in a corner of a maximised window.
        # Scale the font to the largest size where the full canvas + HUD fits
        # the widget, and centre the result with character/line padding. The
        # named Font object means a size change restyles the whole widget.
        self._cols = max(A.CANVAS_W, max(len(line) for a in A.ANIMATIONS
                                         for f in a["frames"] for line in f))
        self._rows = A._GLOBAL_H + 6   # + blank + caption/progress/tape/pulse HUD
        self._font = tkfont.Font(font=self.t.cget("font"))
        self._cur_size = int(self._font.actual("size"))
        self.t.configure(font=self._font)
        self._metrics: dict[int, tuple[int, int]] = {}   # size -> (char_w, line_h)
        self._pad_x = 0      # leading spaces per line (horizontal centring)
        self._pad_top = 0    # leading blank lines (vertical centring)
        self._too_small = False   # panel can't fit even the readable floor
        self._fit_job = None
        self.t.bind("<Configure>", self._on_resize, add=True)

    # -- public API ---------------------------------------------------------- #
    def start(self, *, idle: bool = False) -> None:
        """Reset counters and begin the ``after`` loop (idempotent).

        ``idle=True`` is *attract mode*: the scenes keep cycling as the window's
        centrepiece, but the HUD shows an invitation instead of a 0% progress bar.
        The GUI runs this whenever no review is in flight, and switches to
        ``idle=False`` for the duration of a run."""
        self.idle = idle
        self.ai = self.fi = 0
        self.t0 = self.run0 = time.monotonic()
        self.prog = {"done": 0, "total": 1, "ok": 0, "failed": 0, "ticker": ""}
        self.tape = []
        self.pulse = [3] * A.PULSE_WIDTH
        self.retries = 0
        self.note = None
        if self._job is None:
            self._tick()

    def set_progress(self, done=0, total=1, ok=0, failed=0, ticker="") -> None:
        """Absorb a progress snapshot pushed from the worker thread's queue."""
        self.idle = False  # progress is arriving -> a real run is under way
        prev_failed = self.prog["failed"]
        self.prog.update(done=done, total=max(1, total), ok=ok,
                         failed=failed, ticker=ticker)
        if ticker:
            self.tape.append((ticker, "fail" if failed > prev_failed else "ok"))
            self.tape = self.tape[-8:]

    def add_retry(self, ticker="", attempt=0, max_retries=0, delay=0.0) -> None:
        """A worker hit an error and is waiting out a backoff before trying the
        ticker again: count it and put the ticker on the tape with a » mark."""
        self.idle = False
        self.retries += 1
        if ticker:
            self.tape.append((ticker, "retry"))
            self.tape = self.tape[-8:]

    def set_note(self, text=None) -> None:
        """One-line status from the CLI (e.g. 'retry round 2/4: ...'), shown on
        the caption line until cleared with ``None``."""
        self.note = text or None

    def next_scene(self) -> str:
        """Skip straight to the next scene (the DOS title bar's "channel dial"
        easter egg — double-click it to flip channels). Returns the new title."""
        self.ai = (self.ai + 1) % len(A.ANIMATIONS)
        self.fi = 0
        self.t0 = time.monotonic()
        return A.ANIMATIONS[self.ai]["title"]

    def stop(self) -> None:
        """Cancel the loop, leaving the last frame frozen on screen."""
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
            self._job = None

    # -- internals ----------------------------------------------------------- #
    def _on_resize(self, _e=None) -> None:
        # A drag-resize floods <Configure>; debounce and refit once it settles.
        if self._fit_job is not None:
            try:
                self.root.after_cancel(self._fit_job)
            except Exception:  # noqa: BLE001
                pass
        self._fit_job = self.root.after(80, self._refit)

    def _measure(self, size: int) -> tuple[int, int]:
        """(char width, line height) in pixels for the panel font at ``size``."""
        if size not in self._metrics:
            f = tkfont.Font(family=self._font.cget("family"), size=size)
            self._metrics[size] = (max(1, f.measure("M")),
                                   max(1, f.metrics("linespace")))
        return self._metrics[size]

    _READABLE_FLOOR = 8   # below this the art is illegible even zoomed in

    def _refit(self) -> None:
        """Pick the largest font size where the whole canvas + HUD fits the
        panel, and recompute the centring pads.

        The old floor forced a 5px font onto whatever space remained, by the
        code's own admission "below this the art is unreadable anyway" — so a
        small window got the exact microscopic art that comment says nobody
        can read. Below the READABLE_FLOOR, skip the art entirely instead
        (see _draw's ``_too_small`` check) rather than render it illegibly.
        Cosmetic: never raises."""
        self._fit_job = None
        try:
            if not self.t.winfo_exists():
                return
            w, h = self.t.winfo_width(), self.t.winfo_height()
            if w <= 1 or h <= 1:
                return
            best = None
            for size in range(self._READABLE_FLOOR, 41):
                cw, lh = self._measure(size)
                if cw * self._cols > w or lh * self._rows > h:
                    break
                best = size
            if best is None:
                self._too_small = True
                if self._cur_size != self._READABLE_FLOOR:
                    self._cur_size = self._READABLE_FLOOR
                    self._font.configure(size=self._READABLE_FLOOR)
                return
            self._too_small = False
            cw, lh = self._measure(best)
            self._pad_x = max(0, (w - cw * self._cols) // (2 * cw))
            self._pad_top = max(0, (h - lh * self._rows) // (2 * lh))
            if best != self._cur_size:
                self._cur_size = best
                self._font.configure(size=best)
        except Exception:  # noqa: BLE001 - sizing is cosmetic; never crash
            pass

    def _tick(self) -> None:
        try:
            if not self.t.winfo_exists():  # window torn down -> stop quietly
                self._job = None
                return
        except Exception:  # noqa: BLE001
            self._job = None
            return
        try:
            self._draw()
        except Exception:  # noqa: BLE001 - the animation is cosmetic; never crash
            pass
        self.fi += 1
        if time.monotonic() - self.t0 >= self.switch:
            self.ai = (self.ai + 1) % len(A.ANIMATIONS)
            self.fi = 0
            self.t0 = time.monotonic()
        self._job = self.root.after(self.interval, self._tick)

    def _draw(self) -> None:
        anim = A.ANIMATIONS[self.ai]
        frame = anim["frames"][self.fi % len(anim["frames"])]
        # Pad to a FIXED height so the HUD below never jumps as scenes switch
        # (the terminal Animator does the same with _GLOBAL_H).
        art = list(frame) + [""] * (A._GLOBAL_H - len(frame))
        base = anim["color"]
        accents = anim.get("accents") or {}

        t = self.t
        t.configure(state="normal")
        t.delete("1.0", "end")

        if self._too_small:
            # The window is too small to render the 100x30 canvas at a
            # genuinely readable size — a short notice instead of forcing
            # microscopic, effectively unreadable art. The HUD (caption/
            # progress/tape/pulse) below is a handful of short lines, still
            # legible at this size, so it still renders as normal.
            t.insert("end", "\n[window too small to show the art]\n",
                    ("dim",))
            self._insert_hud(anim, base)
            t.configure(state="disabled")
            return

        pad = " " * self._pad_x
        if self._pad_top:
            t.insert("end", "\n" * self._pad_top)
        # Emit each art line as tag-coloured runs: base colour everywhere, accent
        # colour on the accent characters. Grouping equal-tag runs (rather than
        # one insert per char) keeps the redraw cheap and sidesteps tag-priority
        # ambiguity entirely — each character carries exactly one colour tag.
        for line in art:
            if pad:
                t.insert("end", pad)
            i, n = 0, len(line)
            while i < n:
                tag = accents.get(line[i], base)
                j = i + 1
                while j < n and accents.get(line[j], base) == tag:
                    j += 1
                t.insert("end", line[i:j], (tag,))
                i = j
            t.insert("end", "\n")
        self._insert_hud(anim, base)
        t.configure(state="disabled")

    # The HUD lines below re-implement Animator._{caption,progress,tape,pulse}_line
    # but emit PLAIN strings + Tk tags. We can't reuse those methods directly:
    # they bake in ANSI escapes (COLOR/RESET/BOLD/DIM) that would show as literal
    # "\033[..." garbage in a Text widget.
    def _insert_hud(self, anim: dict, base: str) -> None:
        self.t.insert("end", "\n")
        self._put_line(self._caption_segs(anim, base))
        self._put_line(self._progress_segs())
        self._put_line(self._tape_segs())
        self._put_line(self._pulse_segs(base))

    def _put_line(self, segments) -> None:
        """Insert one HUD line from ``(text, tag_or_None)`` segments."""
        if self._pad_x:
            self.t.insert("end", " " * self._pad_x)
        for text, tag in segments:
            self.t.insert("end", text, (tag,) if tag else ())
        self.t.insert("end", "\n")

    def _caption_segs(self, anim: dict, base: str):
        spin = A.SPINNER[self.fi % len(A.SPINNER)]   # ascii spinner: always renders
        if self.note:   # a CLI status (e.g. retry rounds) outranks the quips
            return [(f"  {spin} [{anim['title']}]  ", base), (self.note, "yellow")]
        captions = anim.get("captions") or [anim.get("caption", "")]
        elapsed = time.monotonic() - self.t0
        caption = captions[int(elapsed / A.CAPTION_SECONDS) % len(captions)]
        return [(f"  {spin} [{anim['title']}]  {caption}", base)]

    _IDLE_TIPS = ["press  R U N  to fetch live prices",
                  f"{len(A.ANIMATIONS)} scenes are standing by",
                  "your watchlist, reviewed while you watch"]

    def _progress_segs(self):
        if self.idle:
            # Attract mode: a gently cycling invitation where the bar would be.
            tip = self._IDLE_TIPS[int((time.monotonic() - self.run0)
                                      / A.CAPTION_SECONDS) % len(self._IDLE_TIPS)]
            return [("  ", None), ("READY", "green"), ("  ·  ", "dim"),
                    (tip, "white")]
        p = self.prog
        frac = min(1.0, p["done"] / p["total"])
        width = 44
        filled = int(width * frac)
        elapsed = max(1e-6, time.monotonic() - self.run0)
        rate = p["done"] / elapsed
        if p["done"] and rate > 0:
            eta = A.Animator._fmt_secs((p["total"] - p["done"]) / rate)
        else:
            eta = "??"
        segs = [
            ("  ", None),
            ("█" * filled, "green"),
            ("░" * (width - filled), "dim"),
            (f" {int(frac * 100):3d}%  {p['done']}/{p['total']} ", None),
            ("| ", "dim"),
            (f"ok {p['ok']}", "green"),
            (" | ", "dim"),
            (f"fail {p['failed']}", "red"),
            (f" | {rate:.1f}/s | eta {eta}", None),
        ]
        if self.retries:
            segs.append((" | ", "dim"))
            segs.append((f"retries {self.retries}", "yellow"))
        return segs

    # tape marks: ✓ fetched, ✗ gave up, » backing off before another attempt
    _TAPE_MARKS = {"ok": ("✓", "green"), "fail": ("✗", "red"),
                   "retry": ("»", "yellow")}

    def _tape_segs(self):
        if not self.tape:
            return [("  tape: warming up the ticker tape...", "dim")]
        segs = [("  tape: ", "dim")]
        for idx, (ticker, kind) in enumerate(self.tape):
            mark, color = self._TAPE_MARKS.get(kind, ("·", "dim"))
            if idx:
                segs.append((" · ", "dim"))
            segs.append((ticker + " ", None))
            segs.append((mark, color))
        return segs

    def _pulse_segs(self, base: str):
        last = self.pulse[-1]
        self.pulse.append(max(0, min(7, last + self.rng.choice((-1, -1, 0, 0, 0, 1, 1)))))
        self.pulse = self.pulse[-A.PULSE_WIDTH:]
        spark = "".join(A.PULSE_UNICODE[v] for v in self.pulse)
        return [("  vibes: ", "dim"), (spark, base)]
