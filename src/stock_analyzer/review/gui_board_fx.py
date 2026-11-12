"""Live board motion: the run-start scene, the gliding progress bar, ticker
pops, milestone quips and the lift-off that ends a clean run.

Everything here is pure (no Tk), so ``tests/test_gui_board_fx.py`` covers it
offline. :class:`~.gui_board.BoardApp` only draws what these helpers return.

The rules the motion keeps (DESIGN.md "Live board"):

* **Decoration never shows a number the run did not produce**, and nothing
  reads as advice. A pop carries the ticker's own 1-month return or no figure
  at all; the scenes are the ASCII cast minus the ones that draw made-up
  prices or a market view, with captions about waiting (:data:`SCENE_QUIPS`).
* **The bar may lag the truth, never lead it** (:func:`ease`).
* **Status outranks fun.** Quips are offered here; the view only shows one
  while nothing is retrying or failing.
* **No randomness.** Every frame is a function of its index, so the same run
  animates the same way twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from . import ascii_art as _art

FX_MS = 66                 # animation tick (about 15 a second)
SCENE_DELAY = 0.30         # s of nothing happening before the scene appears
QUIP_SECONDS = 3.0
FRESH_SECONDS = 0.40       # how long a just-landed mosaic square stays outlined


def _clamp01(x) -> float:
    return max(0.0, min(1.0, float(x or 0.0)))


# --------------------------------------------------------------------------- #
# The bar
# --------------------------------------------------------------------------- #
def ease(shown: float, target: float, dt: float, rate: float = 7.0,
         snap: float = 0.002) -> float:
    """The fraction to draw this tick: ``shown`` moved toward ``target``.

    It closes most of the gap each second (``rate``) and lands exactly once it
    is within ``snap``. It never passes ``target``: the bar can be a moment
    behind the real count but can never claim progress that has not happened.
    If the real fraction drops (the total grew), it follows at once."""
    target = _clamp01(target)
    shown = _clamp01(shown)
    if shown >= target:
        return target
    new = shown + (target - shown) * min(1.0, max(0.0, rate * dt))
    return target if target - new <= snap else new


def bar_cells(width: int, ok: float, fail: float, cell: int = 8,
              gap: int = 2) -> list[tuple[int, int, str]]:
    """The segmented bar as ``(x0, x1, kind)`` rectangles, left to right.

    ``ok`` and ``fail`` are fractions of the whole run; the fetched share comes
    first, the failed share after it, the rest is ``empty``. The shares are
    measured in lit pixels (the gaps between cells are not counted), and a cell
    the leading edge is crossing is split, so the fill moves by the pixel, not
    by the cell. A real share too small for a pixel still gets one: something
    that happened is never drawn as nothing."""
    width, cell, gap = int(width), max(1, int(cell)), max(0, int(gap))
    spans, x = [], 0
    while x < width:
        spans.append((x, min(width, x + cell)))
        x += cell + gap
    usable = sum(x1 - x0 for x0, x1 in spans)
    if usable <= 0:
        return []
    ok, fail = _clamp01(ok), _clamp01(fail)
    a = max(round(usable * ok), 1 if ok > 0 else 0)
    f = max(round(usable * fail), 1 if fail > 0 else 0)
    over = a + f - usable
    if over > 0:                        # rounding: the larger share gives way
        if a >= f:
            a -= over
        else:
            f -= over
    out: list[tuple[int, int, str]] = []
    at = 0                              # lit pixels to the left of this cell
    for x0, x1 in spans:
        end = at + (x1 - x0)
        for lo, hi, kind in ((at, min(end, a), "ok"),
                             (max(at, a), min(end, a + f), "fail"),
                             (max(at, a + f), end, "empty")):
            if hi > lo:
                out.append((x0 + lo - at, x0 + hi - at, kind))
        at = end
    return out


def scanner(frame: int, cells: int, span: int = 4) -> tuple[int, int]:
    """Indices ``[lo, hi)`` of the lit cells while the size of the run is not
    known yet: a short block sweeping there and back. It says "working", it
    does not pretend to measure anything."""
    cells = max(0, int(cells))
    span = max(1, min(int(span), cells))
    travel = cells - span
    if travel <= 0:
        return 0, span if cells else 0
    pos = int(frame) % (2 * travel)
    if pos > travel:
        pos = 2 * travel - pos
    return pos, pos + span


FLAMES = ("  ~", " ~~", "~-~", " -~", "  -", " ~-")
ROCKET_BODY = "=>"


def rocket(frame: int) -> str:
    """The small rocket on the bar, exhaust flickering. Fixed width."""
    return FLAMES[(int(frame) // 2) % len(FLAMES)] + ROCKET_BODY


# --------------------------------------------------------------------------- #
# Ticker pops: the names that just landed, trailing the rocket
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Pop:
    ticker: str
    ret: float | None       # the ticker's 1-month return, None when unknown
    born: float


class Pops:
    """The last few fetched tickers, each shown briefly beside the rocket.

    A fast run lands many tickers a second; showing them all would be a blur,
    so a new pop is only accepted ``GAP`` seconds after the previous one. The
    ones skipped here are still in the table, the tape and the mosaic."""

    LIFE, GAP, CAP = 1.2, 0.30, 3

    def __init__(self) -> None:
        self.items: list[Pop] = []      # newest first
        self._last = float("-inf")

    def add(self, ticker: str, ret, now: float) -> bool:
        if not ticker or now - self._last < self.GAP:
            return False
        self._last = now
        self.items.insert(0, Pop(ticker, ret, now))
        del self.items[self.CAP:]
        return True

    def live(self, now: float) -> list[Pop]:
        """Pops still on screen, newest first."""
        self.items = [p for p in self.items if now - p.born < self.LIFE]
        return list(self.items)


def pop_rise(age: float, height: int, seconds: float = 0.15) -> int:
    """Pixels below its resting line a pop is drawn at ``age``: it comes up
    out of the bar and then sits still (it stays at full contrast throughout
    and simply leaves when its time is up)."""
    if age >= seconds or height <= 0:
        return 0
    return round(height * (1.0 - max(0.0, age) / seconds) ** 2)


def trail_layout(lead: int, rocket_w: int, widths, bar_w: int,
                 gap: int = 10) -> list[int | None]:
    """Left edge of each pop (newest first), or None where there is no room.

    Pops trail *behind* the rocket like exhaust; near the start of the bar,
    where there is no behind yet, they are set out *ahead* of it instead."""
    widths = list(widths)
    if not widths:
        return []
    out: list[int | None] = []
    if lead - rocket_w - gap >= widths[0]:
        x = lead - rocket_w - gap
        for w in widths:
            x -= w
            out.append(x if x >= 0 else None)
            x -= gap
    else:
        x = lead + gap
        for w in widths:
            out.append(x if x + w <= bar_w else None)
            x += w + gap
    return out


# --------------------------------------------------------------------------- #
# Milestone quips
# --------------------------------------------------------------------------- #
MILESTONES = ((0.25, "A quarter in. The rocket is warmed up."),
              (0.50, "Halfway there."),
              (0.75, "Home stretch. Three quarters in."))
MILESTONE_MIN_TOTAL = 8     # "a quarter in" after one of four tickers is noise


class Milestones:
    """Hands out each quip once, in order, as the real fraction passes it."""

    def __init__(self) -> None:
        self.fired = 0

    def hit(self, fraction: float, total: int) -> str | None:
        quip = None
        while self.fired < len(MILESTONES) and fraction >= MILESTONES[self.fired][0]:
            quip = MILESTONES[self.fired][1]    # a jump over two keeps the later one
            self.fired += 1
        if total < MILESTONE_MIN_TOTAL or fraction >= 1.0:
            return None
        return quip


# --------------------------------------------------------------------------- #
# The run-start scene
# --------------------------------------------------------------------------- #
# The ASCII cast the Live board plays, each with quips about the wait. Left out
# of the Classic window's thirteen: "Candles", "Trading desk" and "The exchange"
# draw invented prices and percentages, "Money printer" says it is scoring and
# "The bull" carries a banner with a market view. On this window every number
# is real, there is no score and nothing reads as advice; the captions are
# written here for the same reason rather than borrowed.
SCENE_QUIPS = {
    "To the moon": ("fuelling up while the list loads...",
                    "escape velocity: pending...",
                    "counting down from a very large number..."),
    "The whale": ("surfacing shortly...",
                  "making waves while you wait...",
                  "a deep breath before the data..."),
    "Bear nap": ("shh... the list is still loading...",
                 "bear-y quiet in here...",
                 "one more minute, says the bear..."),
    "Diamond hands": ("polishing while we wait...",
                      "58 facets, zero hurry...",
                      "holding... the line open..."),
    "Disco break": ("vibing until the data lands...",
                    "the floor is lava-lamp...",
                    "one more song, then the tickers..."),
    "After hours": ("the night shift is fetching the list...",
                    "somewhere up there, a server hums...",
                    "a thousand windows, one request in flight..."),
    "The vault": ("spinning the combination...",
                  "audited daily by one small mouse...",
                  "the door is heavy, the list is on its way..."),
    "The harbour": ("the list is on the boat...",
                    "the crane never hurries...",
                    "fresh numbers, being unloaded..."),
}
SCENE_TITLES = tuple(SCENE_QUIPS)
SCENES = [a for a in _art.ANIMATIONS if a["title"] in SCENE_QUIPS]
SCENE_COLS, SCENE_ROWS = _art.CANVAS_W, _art.CANVAS_H


@dataclass(frozen=True)
class SceneFrame:
    lines: tuple[str, ...]                       # the line art, accents blanked
    runs: tuple[tuple[int, int, str], ...]       # (row, column, accent text)


@lru_cache(maxsize=None)
def scene_frame(scene: int, frame: int) -> SceneFrame:
    """One frame as two layers the view draws in two theme colours: the line
    art, and the scene's accent characters as ``(row, column, text)`` runs.
    Laid over each other on a fixed character grid they rebuild the frame."""
    anim = SCENES[scene % len(SCENES)]
    art = anim["frames"][frame % len(anim["frames"])]
    accents = anim.get("accents") or {}
    lines, runs = [], []
    for row, line in enumerate(art[:SCENE_ROWS]):
        line = line[:SCENE_COLS]
        base = []
        col, n = 0, len(line)
        while col < n:
            if line[col] in accents:
                end = col
                while end < n and line[end] in accents:
                    end += 1
                runs.append((row, col, line[col:end]))
                base.append(" " * (end - col))
                col = end
            else:
                base.append(line[col])
                col += 1
        lines.append("".join(base).rstrip())
    return SceneFrame(tuple(lines), tuple(runs))


def scene_caption(scene: int, elapsed: float) -> str:
    """'To the moon · escape velocity: pending...' The quip rotates."""
    title = SCENES[scene % len(SCENES)]["title"]
    quips = SCENE_QUIPS[title]
    return f"{title} · {quips[int(max(0.0, elapsed) / _art.CAPTION_SECONDS) % len(quips)]}"


# --------------------------------------------------------------------------- #
# Lift-off: the short celebration after a clean run
# --------------------------------------------------------------------------- #
LIFTOFF_FRAMES = 26         # about 1.7 s at FX_MS

_BURST = (                  # six stages of one firework, 3 rows by 9 columns
    ("         ", "    .    ", "         "),
    ("    |    ", "   -*-   ", "    |    "),
    ("  \\ | /  ", " -- * -- ", "  / | \\  "),
    (" '.\\|/.' ", "--  +  --", " .'/|\\'. "),
    (" '  .  ' ", "-       -", " .  '  . "),
    (" .     . ", "         ", " '     ' "),
)
_BURSTS = ((0.20, 4), (0.52, 7), (0.82, 10), (0.36, 13), (0.68, 16))   # (x, first frame)
_ROCKET_FRAMES = 11         # frames the rocket takes to leave


def _runs(row: int, col: int, text: str, tone: str, cols: int):
    """Split ``text`` into its non-blank runs, clipped to the stage."""
    i, n = 0, len(text)
    while i < n:
        if text[i] == " ":
            i += 1
            continue
        j = i
        while j < n and text[j] != " ":
            j += 1
        lo, hi = max(col + i, 0), min(col + j, cols)
        if hi > lo:
            yield row, lo, text[lo - col:hi - col], tone
        i = j


def liftoff(frame: int, cols: int, rows: int) -> list[tuple[int, int, str, str]]:
    """What the lift-off draws at ``frame`` on a ``cols`` x ``rows`` character
    stage: ``(row, column, text, tone)`` with tone ``accent`` or ``muted``.
    The rocket leaves along the bottom row, then five fireworks open and fade.
    Past :data:`LIFTOFF_FRAMES` (or on a stage too small) it draws nothing."""
    out: list[tuple[int, int, str, str]] = []
    if cols < 12 or rows < 1 or not 0 <= frame < LIFTOFF_FRAMES:
        return out
    bottom = rows - 1
    if frame < _ROCKET_FRAMES:
        p = (frame / (_ROCKET_FRAMES - 1)) ** 2          # accelerating
        head = round(p * (cols + 8))
        ship = rocket(frame * 2).lstrip()
        trail = "".join("-" if k % 2 == 0 else " " for k in range(min(head, 14)))
        out.extend(_runs(bottom, head - len(ship) - len(trail), trail, "muted", cols))
        out.extend(_runs(bottom, head - len(ship), ship, "accent", cols))
    if rows >= 3:
        top = max(0, (bottom - 3) // 2) if rows > 3 else 0
        for x, first in _BURSTS:
            stage = (frame - first) // 2
            if frame < first or stage >= len(_BURST):
                continue
            left = round(x * cols) - 4
            tone = "accent" if stage < 4 else "muted"
            for dr, text in enumerate(_BURST[stage]):
                out.extend(_runs(top + dr, left, text, tone, cols))
    return out


def sweep(frame: int, cols: int, n: int) -> set[int]:
    """Mosaic squares to outline at ``frame`` of the lift-off: a diagonal band
    crossing the grid once, left to right."""
    if cols <= 0 or n <= 0 or not 0 <= frame < LIFTOFF_FRAMES:
        return set()
    rows = -(-n // cols)
    diagonals = cols + rows - 1
    band = max(1, diagonals // 10)
    at = round(frame / (LIFTOFF_FRAMES - 1) * (diagonals + band)) - band
    return {i for i in range(n) if at <= (i % cols) + (i // cols) < at + band}


# --------------------------------------------------------------------------- #
# One run's animation state
# --------------------------------------------------------------------------- #
class RunFx:
    """Everything that moves during one run. The view calls :meth:`land` for
    each fetched ticker and :meth:`step` once per animation tick."""

    def __init__(self, scene: int = 0) -> None:
        self.scene0 = int(scene) % len(SCENES)
        self.frame = 0
        self.shown = 0.0            # the eased fraction the bar draws
        self.quip = ""
        self.pops = Pops()
        self.milestones = Milestones()
        self._fresh: dict[str, float] = {}
        self._quip_until = 0.0
        self._t0: float | None = None
        self._t = 0.0

    def step(self, now: float, fraction: float, total: int) -> None:
        if self._t0 is None:
            self._t0 = self._t = now
        dt, self._t = now - self._t, now
        self.frame += 1
        self.shown = ease(self.shown, fraction, dt)
        quip = self.milestones.hit(fraction, total)
        if quip:
            self.quip, self._quip_until = quip, now + QUIP_SECONDS
        elif self.quip and now >= self._quip_until:
            self.quip = ""

    def land(self, ticker: str, ret, now: float) -> None:
        """A ticker was fetched: pop it by the rocket and flash its square."""
        self.pops.add(ticker, ret, now)
        self._fresh[ticker] = now + FRESH_SECONDS

    def fresh(self, now: float) -> set[str]:
        """Tickers that landed a moment ago (their mosaic squares are outlined)."""
        self._fresh = {t: until for t, until in self._fresh.items() if until > now}
        return set(self._fresh)

    def elapsed(self, now: float) -> float:
        return 0.0 if self._t0 is None else max(0.0, now - self._t0)

    def scene(self, now: float) -> int:
        """Which scene is on: the run's own, moving on every few seconds of a
        long wait."""
        return (self.scene0 + int(self.elapsed(now) / _art.SWITCH_SECONDS)) % len(SCENES)
