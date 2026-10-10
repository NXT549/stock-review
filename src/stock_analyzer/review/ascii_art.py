#!/usr/bin/env python3
"""
ascii_art.py — big-screen ASCII loading animations for stock_review.py.

While the script grinds through hundreds/thousands of tickers, an `Animator`
runs in a background thread and draws a full scene with a live HUD (progress
bar, rate, ETA, a scrolling ticker tape of recently processed symbols and a
little market-vibes sparkline), redrawing in place with ANSI escapes. It CYCLES
through every animation in `ANIMATIONS` in order, switching to the next one every `switch_seconds`.

This is the widescreen cut: every scene is ~100 columns x ~30 rows, built for
a fullscreen terminal you can leave running on a monitor. Scenes are composed
on a character canvas — hand-drawn sprites stamped over procedurally generated
backdrops (twinkling starfields, drifting bubbles, rolling waves, raining
banknotes, a spinning mirror ball) — so the environments move even while the
hero art holds still.

The cast (in order):
    1. Candlestick chart   — a live chart with axes, volume bars and an MA line
    2. To the moon         — rocket, giant cratered moon, shooting stars
    3. The bull            — big horns, steaming nostrils, pure optimism
    4. The whale           — an order-book whale cruising a whole ocean
    5. Money printer       — go brrrrr (now with real weather: raining cash)
    6. The trading desk    — three live monitors, one coffee, zero regrets
    7. Bear hibernation    — a moonlit campsite, do not wake the bears
    8. Diamond hands       — a geometrically faceted gem. we hold.
    9. Disco break         — a spinning mirror ball and questionable moves
   10. The exchange        — a grand stone exchange with a live LED marquee
   11. After hours         — the city at night, a thousand flickering windows
   12. The vault           — a giant spinning vault door, bullion and a mouse
   13. The harbour         — a gantry crane loading the S.S. DIVIDEND

Design notes:
  * Purely cosmetic. stock_review.py imports this defensively; if the module is
    missing or the terminal isn't a TTY, the script just falls back to its plain
    progress prints.
  * All animation output goes to stderr (stdout stays reserved for the final
    report path), and only when stderr.isatty() and ANSI is available.
  * The Animator is terminal-aware: it centres the scene horizontally, crops it
    if the window is small, and rescales the progress bar to the width — so it
    looks right fullscreen AND survives a tiny default console.
  * Hand-drawn sprites are RAW strings (note the r'''...''' — the art is full
    of backslashes). Keep the art free of the '''-sequence and of the double
    quote character so the triple-quoted r-strings stay happy.
  * Frames within an animation are derived from a single base drawing/canvas so
    the body never jitters between frames — only the parts that are meant to
    move, move.
  * Fancy Unicode (block bars, check marks, braille spinner, sparklines) is
    probed against the stream encoding at startup; if it can't be encoded the
    Animator transparently degrades to pure-ASCII stand-ins.

Preview the whole cast without running a fetch:
    py ascii_art.py                  # live demo, cycling through every scene
    py ascii_art.py --stills         # dump every frame of every scene, no ANSI
    py ascii_art.py --stills whale   # dump frames of one scene (name filter)
"""
from __future__ import annotations

import math
import os
import random
import sys
import threading
import time
from collections import deque

# --- ANSI helpers -----------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

# 256-colour foreground codes. Each animation has a base colour plus optional
# per-character accent colours (see the `accents` key in ANIMATIONS).
COLOR = {
    "orange":  "\033[38;5;208m",   # rocket flame / warm accents
    "cyan":    "\033[38;5;51m",    # rocket
    "green":   "\033[38;5;46m",    # bull / money / up-candles
    "yellow":  "\033[38;5;220m",   # money printer / stars
    "magenta": "\033[38;5;201m",   # disco
    "red":     "\033[38;5;196m",   # fails / down-candles
    "blue":    "\033[38;5;39m",    # whale / bear night sky
    "teal":    "\033[38;5;44m",    # water / bubbles / diamond
    "white":   "\033[38;5;231m",   # sparkle
    "gray":    "\033[38;5;245m",   # chart scaffolding / sleepy things
    "pink":    "\033[38;5;213m",   # disco accents
    "gold":    "\033[38;5;178m",   # coins
    "purple":  "\033[38;5;141m",   # desk
}
GREEN = COLOR["green"]
RED = COLOR["red"]

# Two spinners: braille for capable terminals, the classic for everything else.
SPINNER = ["|", "/", "-", "\\"]
SPINNER_BRAILLE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Sparkline glyph ramps for the market-vibes pulse line.
PULSE_UNICODE = "▁▂▃▄▅▆▇█"
PULSE_ASCII = "_.-=+*^#"
PULSE_WIDTH = 44

# How long each animation is shown before cycling to the next, the frame rate,
# and how often the caption under an animation rotates to the next quip.
SWITCH_SECONDS = 10.0
FPS = 8.0
CAPTION_SECONDS = 2.5

# Characters the HUD wants that older code pages may not have, with ASCII
# stand-ins used when the stream can't encode them.
_FANCY_PROBE = "█░▒✓✗·▲▼" + PULSE_UNICODE + "".join(SPINNER_BRAILLE)
_ASCII_FALLBACK = str.maketrans({
    "█": "#", "▓": "#", "▒": "%", "░": ".",
    "✓": "+", "✗": "x", "·": "|", "▲": "^", "▼": "v",
    "▁": "_", "▂": ".", "▃": "-", "▄": "=", "▅": "+", "▆": "*", "▇": "^",
})

# Every scene is drawn on a canvas this size; the Animator centres/crops it to
# the real terminal at draw time.
CANVAS_W = 100
CANVAS_H = 30
FRAMES_PER_SCENE = 12   # scene loops are periodic in this many frames


def enable_ansi() -> bool:
    """Best-effort: make sure ANSI escapes are honoured. Returns True if usable.

    On non-Windows this is a no-op (True). On Windows, flip on
    ENABLE_VIRTUAL_TERMINAL_PROCESSING for the std handles (modern Windows
    Terminal / PowerShell support it); if that fails we report False so the
    caller disables the animation rather than spraying raw escape codes.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ok = False
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
                ok = True
        return ok
    except Exception:
        return False


# =============================================================================
# Frame/canvas helpers. An animation's frames are lists of line-strings, all
# padded to a common height so the in-place redraw never jitters vertically.
# Scenes are composed on a mutable character canvas: hand-drawn sprites get
# stamped onto procedurally drawn backdrops, then the canvas becomes a frame.
# =============================================================================
def _lines(block: str) -> list[str]:
    """Split one raw art block into lines, dropping the framing newlines."""
    return block.strip("\n").split("\n")


def _pad(frames: list[list[str]]) -> list[list[str]]:
    """Pad every frame with blank lines so they all share the tallest height."""
    height = max(len(f) for f in frames)
    return [f + [""] * (height - len(f)) for f in frames]


def _sub(base: list[str], *pairs: tuple[str, str]) -> list[str]:
    """Copy `base` applying substring replacements — the moving parts."""
    out = []
    for ln in base:
        for old, new in pairs:
            ln = ln.replace(old, new)
        out.append(ln)
    return out


def _edit(base: list[str], edits: dict[int, str]) -> list[str]:
    """Copy `base` replacing whole rows by index (negative indices fine)."""
    out = list(base)
    for idx, new in edits.items():
        out[idx] = new
    return out


def _blank(w: int = CANVAS_W, h: int = CANVAS_H) -> list[list[str]]:
    return [[" "] * w for _ in range(h)]


def _stamp(cv: list[list[str]], art: list[str], x: int, y: int) -> None:
    """Blit sprite lines onto the canvas; spaces are transparent."""
    for r, ln in enumerate(art):
        yy = y + r
        if not 0 <= yy < len(cv):
            continue
        row = cv[yy]
        for c, ch in enumerate(ln):
            xx = x + c
            if ch != " " and 0 <= xx < len(row):
                row[xx] = ch


def _put(cv: list[list[str]], x: int, y: int, ch: str, only_blank: bool = False) -> None:
    if 0 <= y < len(cv) and 0 <= x < len(cv[0]):
        if not only_blank or cv[y][x] == " ":
            cv[y][x] = ch


def _cv(cv: list[list[str]]) -> list[str]:
    return ["".join(row).rstrip() for row in cv]


def _seg(cv: list[list[str]], x0: int, y0: int, x1: int, y1: int,
         ch: str | None = None) -> None:
    """Draw a straight line; if no char given, pick one that suits the slope."""
    dx, dy = x1 - x0, y1 - y0
    if ch is None:
        if dx == 0:
            ch = "|"
        elif dy == 0:
            ch = "_"
        elif (dx > 0) == (dy > 0):
            ch = "\\"
        else:
            ch = "/"
    n = max(abs(dx), abs(dy), 1)
    for i in range(n + 1):
        _put(cv, round(x0 + dx * i / n), round(y0 + dy * i / n), ch)


def _oval(cv: list[list[str]], cx: float, cy: float, rx: float, ry: float) -> None:
    """Outline an ellipse with slope-matched characters."""
    steps = max(48, int((rx + ry) * 8))   # dense enough to leave no gaps on top
    for i in range(steps):
        t = 2.0 * math.pi * i / steps
        x = cx + rx * math.cos(t)
        y = cy + ry * math.sin(t)
        tx, ty = -rx * math.sin(t), ry * math.cos(t)     # tangent, screen coords
        ang = math.degrees(math.atan2(ty, tx)) % 180.0
        if ang < 22.5 or ang >= 157.5:
            ch = "_" if math.sin(t) > 0.3 else "-"
        elif ang < 67.5:
            ch = "\\"
        elif ang < 112.5:
            ch = "|"
        else:
            ch = "/"
        _put(cv, round(x), round(y), ch)


def _clear_disc(cv: list[list[str]], cx: float, cy: float, rx: float, ry: float) -> None:
    """Blank out the inside of an ellipse (so stars don't shine through it)."""
    for y in range(len(cv)):
        for x in range(len(cv[0])):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                cv[y][x] = " "


def _stars(cv: list[list[str]], seed: int, n: int, ymax: int, f: int,
           chars: str = ".*+.") -> None:
    """Twinkling starfield: fixed positions per seed, blink phase per frame."""
    rng = random.Random(seed)
    for _ in range(n):
        x = rng.randrange(0, CANVAS_W)
        y = rng.randrange(0, ymax)
        ch = rng.choice(chars)
        phase = rng.randrange(4)
        if (f + phase) % 4 == 0:      # each star winks out one beat in four
            continue
        _put(cv, x, y, ch, only_blank=True)


def _speckle(cv: list[list[str]], seed: int, y0: int, y1: int, n: int,
             chars: str = ".,'`") -> None:
    """Static ground texture (sand, grass) between rows y0..y1 inclusive."""
    rng = random.Random(seed)
    for _ in range(n):
        _put(cv, rng.randrange(0, CANVAS_W), rng.randrange(y0, y1 + 1),
             rng.choice(chars), only_blank=True)


_FISH_L = "<')))><"      # faces left, drifts left
_FISH_R = "><((('>"      # faces right, drifts right


# =============================================================================
# 1) CANDLESTICK CHART — generated in code: a random-walk OHLC chart draws
#    itself candle by candle, now with a price axis, a moving-average line
#    and a volume strip underneath. Live last-price readout in the title.
# =============================================================================
def _make_chart_frames() -> list[list[str]]:
    rng = random.Random(20260703)
    n = 44
    candles: list[tuple[float, float, float, float]] = []
    vols: list[int] = []
    price = 48.0
    for _ in range(n):
        o = price
        c = o + rng.uniform(-4.2, 4.2) + 0.55  # gentle upward drift, obviously
        hi = max(o, c) + rng.uniform(0.4, 2.6)
        lo = min(o, c) - rng.uniform(0.4, 2.6)
        candles.append((o, hi, lo, c))
        vols.append(rng.randint(1, 3))
        price = c
    closes = [c for (_, _, _, c) in candles]
    ma = [sum(closes[max(0, i - 5): i + 1]) / len(closes[max(0, i - 5): i + 1])
          for i in range(n)]
    h = 20
    vals = [v for cd in candles for v in cd]
    vlo, vhi = min(vals), max(vals)
    span = (vhi - vlo) or 1.0

    def rowof(v: float) -> int:
        return int(round((vhi - v) / span * (h - 1)))

    frames = []
    reveals = [8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 44, 44]
    for fi, reveal in enumerate(reveals):
        plot = [[" "] * (n * 2) for _ in range(h)]
        for i in range(reveal):
            o, hi, lo, c = candles[i]
            x = i * 2
            up = c >= o
            for r in range(rowof(hi), rowof(lo) + 1):
                plot[r][x] = "|"
            a, b = sorted((rowof(o), rowof(c)))
            for r in range(a, b + 1):
                plot[r][x] = "█" if up else "▒"
            mr = rowof(ma[i])
            if plot[mr][x + 1] == " ":
                plot[mr][x + 1] = "+"
        last = candles[reveal - 1][3]
        first = candles[0][0]
        pct = (last - first) / first * 100.0
        arrow = "▲" if last >= first else "▼"
        cursor = " " if (fi % 2 and reveal == n) else "_"
        lines = [f"      .:[ STOCK REVIEW -- MARKET FEED ]:.     "
                 f"last {last:6.2f} {arrow} {pct:+5.1f}%   {cursor}", ""]
        for r in range(h):
            v = vhi - r / (h - 1) * span
            label = f"{v:7.2f} |" if r % 4 == 0 else "        |"
            body = "".join(plot[r])
            if r == rowof(first):
                body = "".join(ch if ch != " " else "." for ch in body)
            lines.append(label + body)
        lines.append("        +" + "-" * (n * 2) + ">")
        # volume strip
        vplot = [[" "] * (n * 2) for _ in range(3)]
        for i in range(reveal):
            o, _, _, c = candles[i]
            ch = "█" if c >= o else "▒"
            for r in range(vols[i]):
                vplot[2 - r][i * 2] = ch
        for r in range(3):
            lines.append(("    vol |" if r == 1 else "        |") + "".join(vplot[r]))
        lines.append("         " + "'     " * (n // 3))
        frames.append(lines)
    return _pad(frames)


CHART = _make_chart_frames()


# =============================================================================
# 2) TO THE MOON — a rocket bobbing on its exhaust under a giant procedurally
#    cratered moon, twinkling starfield, shooting star, tiny Earth left behind.
# =============================================================================
_ROCKET = _lines(r'''
         /\
        /  \
       /    \
      /      \
     /        \
    /__________\
    |    __    |
    |   /  \   |
    |  | () |  |
    |   \__/   |
    |          |
    | A S X 26 |
    |          |
   /|          |\
  / |          | \
 /  |          |  \
/  /|          |\  \
|_/ |__________| \_|
    |_|      |_|
''')
_FLAMES = [
    _lines(r'''
    /##########\
     \########/
      \%%%%%%/
       )%%%%(
        \%%/
         ~~
'''),
    _lines(r'''
    /##########\
    (##########)
     \%%%%%%%%/
      \%%%%%%/
       (%%%%)
        \%%/
'''),
    _lines(r'''
     /########\
     \#%%%%%%#/
      )%%%%%%(
       \%%%%/
        )%%(
         ~~
'''),
]
_CRATERS = [
    (66, 6, [" __ ", "(__)"]),
    (78, 9, ["o"]),
    (70, 13, [" _ ", "(_)"]),
    (80, 15, ["O"]),
    (62, 11, ["o"]),
    (74, 17, ["( )"]),
]


def _make_moon_frames() -> list[list[str]]:
    bobs = [2, 2, 1, 1, 0, 0, 0, 0, 1, 1, 2, 2]
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        _stars(cv, 4242, 40, 24, f)
        # the moon (interior cleared so stars don't shine through)
        _clear_disc(cv, 72, 11, 19, 9.5)
        _oval(cv, 72, 11, 19, 9.5)
        for cx, cy, art in _CRATERS:
            _stamp(cv, art, cx, cy)
        # tiny Earth we left behind
        _clear_disc(cv, 10, 26, 7, 3)
        _oval(cv, 10, 26, 7, 3)
        for x in range(5, 16):
            if (x + f // 6) % 3 == 0:
                _put(cv, x, 26, "~", only_blank=True)
        # shooting star streaks across the top every loop
        sx = f * 9 - 8
        if 0 <= f < 8:
            _stamp(cv, ["*"], sx + 3, 2)
            _stamp(cv, ["--"], sx, 2)
            _stamp(cv, ["."], sx - 3, 3)
        # the rocket rides its own exhaust
        y = 3 + bobs[f]
        _stamp(cv, _ROCKET, 18, y)
        _stamp(cv, _FLAMES[f % 3], 18, y + 19)
        _put(cv, 16, y + 22 + (f % 2), "o", only_blank=True)
        _put(cv, 40, y + 23 - (f % 2), "o", only_blank=True)
        frames.append(_cv(cv))
    return _pad(frames)


ROCKET = _make_moon_frames()


# =============================================================================
# 3) THE BULL — big horns, heavy brow, steam out the nostrils. Numbers only
#    go up while this guy is on screen (results may vary).
# =============================================================================
_BULL_BASE = _lines(r'''
        _.--~~~~--._                                                      _.--~~~~--._
      .'            '-..                                              ..-'            '.
     /                  '-..                                      ..-'                  \
    |      .--.             '-.     ___..--------..___         .-'             .--.      |
    |     /    \               '..-~                  ~-..'                   /    \     |
     \    \     '.              .'                        '.                .'     /    /
      \    '.     '-.          /                            \            .-'     .'    /
       '.    '-.     '-.      |                              |        .-'     .-'    .'
         '.     '-.     '--.  |                              |    .--'     .-'     .'
           '-.     '--..    '-|                              |-'      ..--'     .-'
              '-..      '--.. |     ______        ______     |  ..--'       ..-'
                  '--..       |   .'      '.    .'      '.   |        ..--'
                       '--..  |  |   (@@)   |  |   (@@)   |  |  ..--'
                             '|   '.______.'    '.______.'   |'
                              |                              |
                              |       ._____________.        |
                               \     /               \      /
                                '-. /                 \ .-'
                          ~ ~      |   (::)     (::)   |      ~ ~
                         ~ ~       |                   |       ~ ~
                                    \    '-._____.-'   /
                                     '-.__   ___   __.-'
                                          | (   ) |
                                          |  '-'  |
                                           '-...-'
                              [  B U L L I S H .  A L W A Y S  ]
''')
_BULL_STEAM_BIG = {
    18: r'                       ~ ~~        |   (::)     (::)   |        ~~ ~',
    19: r'                      ~ ~ ~        |                   |        ~ ~ ~',
}
_BULL_STEAM_NONE = {
    18: r'                                   |   (::)     (::)   |',
    19: r'                                   |                   |',
}
_BULL_V = {
    "calm":  _edit(_BULL_BASE, _BULL_STEAM_NONE),
    "puff":  _BULL_BASE,
    "blast": _edit(_BULL_BASE, _BULL_STEAM_BIG),
    "blink": _edit(_sub(_BULL_BASE, ("(@@)", "(--)")), _BULL_STEAM_NONE),
    "snort": _edit(_sub(_BULL_BASE, ("(@@)", "(OO)"),
                        ("[  B U L L I S H .  A L W A Y S  ]",
                         "[  S N O R T . C H A R G E . R E P E A T  ]")),
                   _BULL_STEAM_BIG),
}
_BULL_SEQ = ["calm", "calm", "puff", "puff", "blast", "puff", "blink", "calm",
             "puff", "snort", "snort", "blast"]

BULL = _pad([_BULL_V[k] for k in _BULL_SEQ])


# =============================================================================
# 4) THE WHALE — an ocean scene: the whale glides across the frame while the
#    spout grows, clouds drift, waves roll, and a fish school flees below.
# =============================================================================
_WHALE_BODY = _lines(r'''
                         ______..........______
                 __..--~~                      ~~--..__
            _.-~~              /\                      ~~-..___
         .-~                  /  \                             ~~--..__
       .~                      ~~                                      ~-._
      /     ___                                                            '-.       ___
     |    .~   ~.                                                             '.     /  _|
     |   |  (o)  |                                                             \    / _/
     |    '.___.'                                                               |  | /
      \                                                                        _|__|/
       '.    \______                                                       _.-~
         '-..__     ~~~~----......________________......----~~~~     __.-~
               ~~~--....._________________________________.....--~~~
''')
_SPOUT_STAGES = [
    [],
    ["  ."],
    ["  o", "  ."],
    [" o O o", "  o o", "   o", "   ."],
    ["o O . O o", " o  O  o", "   o o", "    o", "    ."],
    [" o  .  o", "  .   .", "    o", "    ."],
]
_CLOUD = _lines(r'''
     .--.
  .-(    )-.
 (__.__)__.__)
''')


def _make_whale_frames() -> list[list[str]]:
    glide = [0, 1, 3, 5, 7, 9, 10, 9, 7, 5, 3, 1]
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        # drifting clouds
        _stamp(cv, _CLOUD, (4 + f) % 110 - 8, 0)
        _stamp(cv, _CLOUD, (58 + f) % 110 - 4, 2)
        # whale + spout (whale rides right on the waterline)
        wx = glide[f]
        _stamp(cv, _WHALE_BODY, wx, 8)
        stage = _SPOUT_STAGES[min(f, len(_SPOUT_STAGES) - 1)] if f < 6 else \
            _SPOUT_STAGES[max(0, 11 - f)]
        if stage:
            _stamp(cv, stage, wx + 24, 8 - len(stage))
        # rolling waterline
        wave = ("~~~^~~~.~~~~^~~~" * 8)
        off = (f * 2) % 16
        for x in range(CANVAS_W):
            _put(cv, x, 21, wave[(x + off) % len(wave)], only_blank=True)
            if (x + off) % 7 == 0:
                _put(cv, x, 22, "~", only_blank=True)
        # the deep: fleeing fish school + bubbles + seafloor
        for i, (fy, phase) in enumerate(((24, 0), (25, 5), (26, 9))):
            fx = (90 - (f * 6 + phase * 4)) % (CANVAS_W + 24) - 12
            _stamp(cv, [_FISH_L], fx, fy)
        for bx, phase in ((30, 0), (62, 3), (78, 7)):
            y = 27 - ((f + phase) * 2) % 6
            _put(cv, bx, y, "o", only_blank=True)
        _speckle(cv, 9410, 28, 29, 46, ".,'~")
        _stamp(cv, ["*"], 22, 28)
        _stamp(cv, [")(", ")("], 70, 27)
        frames.append(_cv(cv))
    return _pad(frames)


WHALE = _make_whale_frames()


# =============================================================================
# 5) MONEY PRINTER — go brrrrr. A proper industrial unit: spinning dials, a
#    heat gauge, a fill bar, and a permanent weather system of falling bills.
# =============================================================================
_PRINTER_BASE = _lines(r'''
              money printer go BRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRR
      __________________________________________________________________
     /                                                                 /|
    /    [#]   A S X   R E P O R T - O - M A T   9 0 0 0             / |
   /_________________________________________________________________/  |
   |     _____________________________________________________       |  |
   |    |                                                     |      |  |
   |    |   STATUS: CRUNCHING THE NUMBERS...                  |      |  |
   |    |_____________________________________________________|      |  |
   |                                                                  |  |
   |     (|)     (/)     (-)      [||||||||          ]   .--------.   |  |
   |    ALPHA   BETA    GAMMA                            | BRRRR  |   | /
   |                              HEAT [~~~~      ]      '--------'   |/
   |__________________________________________________________________|
      H|     __________________________________________________   |H
      H|    |   ______      ______      ______      ______     |  |H
      H|    |  | $100 |    | $100 |    | $100 |    | $100 |    |  |H
      H|    |  |_(50)_|    |_(50)_|    |_(50)_|    |_(50)_|    |  |H
      H|    |__________________________________________________|  |H
      H|__________________________________________________________|H
     ###                                                          ###
''')
_PRINTER_V = [
    _PRINTER_BASE,
    _sub(_PRINTER_BASE,
         ("STATUS: CRUNCHING THE NUMBERS...  ", "STATUS: SCORING YOUR TICKERS...   "),
         ("(|)     (/)     (-)", "(/)     (-)     (\\)"),
         ("[||||||||          ]", "[||||||||||        ]"),
         ("HEAT [~~~~      ]", "HEAT [~~~~~~    ]"),
         ("| BRRRR  |", "| brrrr  |")),
    _sub(_PRINTER_BASE,
         ("STATUS: CRUNCHING THE NUMBERS...  ", "STATUS: APPLYING TECHNICALS...    "),
         ("(|)     (/)     (-)", "(-)     (\\)     (|)"),
         ("[||||||||          ]", "[||||||||||||      ]"),
         ("HEAT [~~~~      ]", "HEAT [~~~~~~~~  ]"),
         ("| BRRRR  |", "| BRRRR! |")),
    _sub(_PRINTER_BASE,
         ("STATUS: CRUNCHING THE NUMBERS...  ", "STATUS: PRINTING THE REPORT...    "),
         ("(|)     (/)     (-)", "(\\)     (|)     (/)"),
         ("[||||||||          ]", "[||||||||||||||||  ]"),
         ("HEAT [~~~~      ]", "HEAT [~~~~~~~~~~]"),
         ("| BRRRR  |", "|BRRRRR!!|")),
]
_BILLS = ["[$100]", "[$50]", "[$20]", "$", "$", "[$100]", "$", "[$50]"]


def _make_printer_frames() -> list[list[str]]:
    rng = random.Random(555)
    # one bill per 7-column slot so falling notes never overlap mid-air
    drops = [(4 + i * 7 + rng.randrange(2), rng.randrange(9), rng.choice(_BILLS))
             for i in range(13)]
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        _stamp(cv, _PRINTER_V[(f // 3) % 4], 12, 0)
        # it is raining money below (and beside) the machine
        for x, phase, bill in drops:
            y = 20 + (f + phase) % 9
            sway = ((f + phase) // 2) % 2
            _stamp(cv, [bill], x + sway, y)
        _speckle(cv, 556, 29, 29, 30, "$.")
        frames.append(_cv(cv))
    return _pad(frames)


PRINTER = _make_printer_frames()


# =============================================================================
# 6) THE TRADING DESK — three live monitors: a scrolling line chart, a
#    watchlist with wobbling quotes, and a scanner with pulsing bars.
#    Plus the keyboard of destiny and a mug with animated steam.
# =============================================================================
_MONITOR = _lines(r'''
.------------------------------.
|                              |
|                              |
|                              |
|                              |
|                              |
|                              |
|                              |
|                              |
'--------------.  .------------'
               |  |
          _____|  |_____
''')
_DESKTOP = _lines(r'''
  _______________________________________________________________________________________________
 /                                                                                               /|
|     _________________________________________      ___                            .--------.  | |
|    [_::_::_::_::_::_::_::_::_::_::_::_::_::_]     / o /       .------.    (   )   | do a   |  | |
|    [_::_::_::_::_::_::_::_::_::_::_::_::_::_]    /___/        | BTFD |    |___|   |  hodl  |  | |
|       the keyboard of destiny                    mouse        '------'     mug    '--------'  | |
|________________________________________________________________________________________________|/
''')
_WATCH_ROWS = [("CBA.AX", 171.20), ("BHP.AX", 43.85), ("CSL.AX", 268.40),
               ("WES.AX", 78.15), ("NAB.AX", 39.60), ("FMG.AX", 22.05)]
_SCAN_ROWS = ["BHP ", "RIO ", "CBA ", "WOW ", "CSL ", "QAN "]


def _mon_interior(title: str) -> list[str]:
    return [(" " + title).ljust(28)[:28]]


_CHAIR = _lines(r'''
 .________.
 |        |
 '--.  .--'
     ||
   __||__
  (o)  (o)
''')
_BIN = _lines(r'''
\ ~ /
 |  |
 |__|
''')


def _make_desk_frames() -> list[list[str]]:
    rng = random.Random(777)
    walk = [3]
    for _ in range(200):
        walk.append(max(0, min(5, walk[-1] + rng.choice((-1, -1, 0, 0, 1, 1)))))
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        # wall clock (runs suspiciously fast during a fetch)
        mm = (f * 5) % 60
        _stamp(cv, [" .------. ", f"( 9:{mm:02d}am )", " '------' "], 44, 1)
        for mx in (0, 34, 68):
            _stamp(cv, _MONITOR, mx, 5)
        # M1: live feed — scrolling random-walk line
        _stamp(cv, _mon_interior("LIVE FEED  .  ASX"), 1, 6)
        for i in range(26):
            lvl = walk[(f * 2 + i) % len(walk)]
            ch = "o" if i == 25 else "."
            _put(cv, 2 + i, 13 - lvl, ch)
        # M2: watchlist — quotes wobble around their base price
        _stamp(cv, _mon_interior("WATCHLIST"), 35, 6)
        for i, (tick, px) in enumerate(_WATCH_ROWS):
            wob = math.sin((f + i * 2) / 12.0 * 2 * math.pi)
            d = wob * (0.4 + i * 0.11)
            arrow = "^" if d >= 0 else "v"
            row = f" {tick:<7}{px + d:8.2f}  {arrow}{abs(d):4.1f}%"
            _stamp(cv, [row.ljust(28)[:28]], 35, 7 + i)
        # M3: scanner — pulsing conviction bars
        _stamp(cv, _mon_interior("SCANNER  .  momentum"), 69, 6)
        for i, name in enumerate(_SCAN_ROWS):
            w = 5 + int(4.9 * (0.5 + 0.5 * math.sin((f + i * 3) / 12.0 * 2 * math.pi)))
            row = f" {name} [" + "#" * w + "." * (10 - w) + f"] {w * 10:3d}"
            _stamp(cv, [row.ljust(28)[:28]], 69, 7 + i)
        # blinking cursor on the watchlist monitor
        if f % 2:
            _put(cv, 61, 13, "_")
        # the desk itself, the chair, the bin of rejected tickers
        _stamp(cv, _DESKTOP, 0, 17)
        _stamp(cv, _CHAIR, 44, 24)
        _stamp(cv, _BIN, 74, 26)
        _put(cv, 72, 28, "*", only_blank=True)
        _speckle(cv, 778, 28, 29, 18, ".")
        # coffee steam
        _put(cv, 77 + (f % 3) - 1, 19, "~", only_blank=True)
        _put(cv, 78 + (1 - f % 3), 18, "~", only_blank=True)
        _put(cv, 78 + (f % 2), 17, "~", only_blank=True)
        frames.append(_cv(cv))
    return _pad(frames)


DESK = _make_desk_frames()


# =============================================================================
# 7) BEAR HIBERNATION — a moonlit forest campsite. Two bears in a cave, pines,
#    fireflies, an owl, and Zzz drifting up into a twinkling sky.
# =============================================================================
_CAVE = _lines(r'''
              ____....------------------....____
         _.-~~                                  ~~-._
       .~                                            ~.
      /                                                \
     /        .--.                       .--.           \
    |        ( o: )___               ___( :o )            |
    |     .-~ '--'    ~~-.       .-~~    '--' ~-.         |
    |    /   - -          \     /           - -  \        |
    |   |   ( __ )         |   |          ( __ )  |       |
    |    \   ~~           /     \            ~~  /        |
    |     '-.______..--~~        ~~--..______.-'          |
    |                                                     |
''')
_PINE = _lines(r'''
    /\
   /  \
  /    \
 /______\
   /  \
  /    \
 /______\
    ||
    ||
''')
_SIGN = _lines(r'''
 .----------------.
 | do not wake    |
 | until next Qtr |
 '----------------'
        ||
        ||
''')
_OWL = _lines(r'''
 .---.
( o o )
 \ - /
  ^^^
''')


def _make_bear_frames() -> list[list[str]]:
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        _stars(cv, 1313, 34, 12, f)
        # moon, top right
        _clear_disc(cv, 88, 4, 8, 4)
        _oval(cv, 88, 4, 8, 4)
        _stamp(cv, ["o"], 85, 3)
        _stamp(cv, ["( )"], 89, 5)
        # ground
        _seg(cv, 0, 26, CANVAS_W - 1, 26, "_")
        _speckle(cv, 1314, 27, 29, 60, ".,'`")
        # pines + owl perched on the tall one
        _stamp(cv, _PINE, 2, 17)
        _stamp(cv, _PINE, 12, 19)
        owl = _OWL if f % 6 else _sub(_OWL, ("( o o )", "( - - )"))
        _stamp(cv, owl, 3, 13)
        # the cave of sleeping bears
        _stamp(cv, _CAVE, 18, 14)
        # the sign, planted clear of the cave
        _stamp(cv, _SIGN, 80, 20)
        # Zzz drifting up and to the right from each bear
        for bx, by, phase in ((32, 20, 0), (50, 20, 6)):
            for k, ch in enumerate("zzZ"):
                step = (f + phase + k * 4) % 12
                _put(cv, bx + 4 + step, by - 2 - step // 2, ch if step < 9 else " ",
                     only_blank=True)
        # fireflies in the grass
        for fx, fy, phase in ((10, 28, 1), (30, 27, 4), (62, 28, 7), (74, 27, 9)):
            if (f + phase) % 3:
                _put(cv, fx + (f + phase) % 2, fy, "*", only_blank=True)
        frames.append(_cv(cv))
    return _pad(frames)


BEAR = _make_bear_frames()


# =============================================================================
# 8) DIAMOND HANDS — a geometrically constructed brilliant-cut gem (real facet
#    lines, drawn with a line rasteriser) over cradling hands. Sparkle sweep
#    included. We hold.
# =============================================================================
_HANDS = _lines(r'''
  __                                                                    __
  \ ~~--..___                                                ___..--~~ /
   \         ~~~--..___     W E   H O L D   T H E S E    ___..--~~~   /
    '--..___           ~~--..______________________..--~~         ___..--'
            ~~~~-----......______________________......-----~~~~
''')


def _make_diamond_frames() -> list[list[str]]:
    # All crown facets run at exactly 45 degrees so they rasterise as clean
    # unbroken slashes; the girdle and table are drawn last for a crisp rim.
    cx, table_y, girdle_y, apex_y = 50, 2, 10, 24
    t_l, t_r = cx - 13, cx + 13          # table corners
    g_l, g_r = t_l - 8, t_r + 8          # girdle corners (45-degree sides)
    base = _blank()
    _seg(base, t_l, table_y, g_l, girdle_y)                    # crown sides
    _seg(base, t_r, table_y, g_r, girdle_y)
    _seg(base, t_l, table_y, t_l + 8, girdle_y)                # star facets (45s)
    _seg(base, cx, table_y, cx - 8, girdle_y)
    _seg(base, cx, table_y, cx + 8, girdle_y)
    _seg(base, t_r, table_y, t_r - 8, girdle_y)
    for gp in (g_l, cx - 11, cx, cx + 11, g_r):                # pavilion facets
        _seg(base, gp, girdle_y, cx, apex_y)
    _seg(base, g_l, girdle_y, g_r, girdle_y, "_")              # the girdle
    _seg(base, t_l, table_y, t_r, table_y, "_")                # the table
    _put(base, cx, apex_y, "V")
    _stamp(base, _HANDS, 13, 25)
    base_lines = _cv(base)

    sparkle_pts = [(30, 4), (72, 3), (20, 12), (82, 13), (38, 1), (64, 1),
                   (10, 8), (92, 7), (14, 22), (26, 18), (74, 18), (86, 22),
                   (50, 0)]
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = [list(ln.ljust(CANVAS_W)) for ln in base_lines]
        while len(cv) < CANVAS_H:
            cv.append([" "] * CANVAS_W)
        # light sweeping through the pavilion facets
        for y in range(girdle_y + 1, apex_y):
            half = (g_r - g_l) / 2 * (apex_y - y) / (apex_y - girdle_y)
            for x in range(g_l + 1, g_r):
                if cv[y][x] == " " and abs(x - cx) < half - 1 and \
                        (x + 2 * y + f * 4) % 26 < 2:
                    cv[y][x] = "."
        # twinkles around the gem
        for i, (sx, sy) in enumerate(sparkle_pts):
            ch = "*+.'"[(f + i) % 4]
            _put(cv, sx, sy, ch, only_blank=True)
        frames.append(_cv(cv))
    return _pad(frames)


DIAMOND = _make_diamond_frames()


# =============================================================================
# 9) DISCO BREAK — a procedurally spinning mirror ball (phase-shifted facet
#     checkerboard), rotating rays, five dancers with moves, and a lit floor.
# =============================================================================
_DANCERS = [
    _lines(" \\o/ \n  |  \n  |  \n / \\ "),
    _lines("  o/ \n /|  \n  |  \n / \\ "),
    _lines(" _o_ \n  |  \n  |  \n > < "),
    _lines(" \\o  \n  |\\ \n  |  \n / > "),
]


def _make_disco_frames() -> list[list[str]]:
    cx, cy, rx, ry = 50, 8, 12, 6
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        # hang the ball
        _seg(cv, cx, 0, cx, 1, "|")
        _stamp(cv, ["_|_"], cx - 1, 0)
        # the ball: checkerboard facets, phase-shifted = spin
        for y in range(cy - ry, cy + ry + 1):
            for x in range(cx - rx, cx + rx + 1):
                if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                    cv[y][x] = "#" if ((x + f) // 2 + y) % 2 else ":"
        # travelling glint on the rim
        gt = 2 * math.pi * f / FRAMES_PER_SCENE
        _put(cv, round(cx + (rx - 1) * math.cos(gt)),
             round(cy + (ry - 1) * math.sin(gt)), "*")
        # rotating rays
        for d in range(8):
            ang = d * math.pi / 4 + (f % 2) * math.pi / 8
            ch = "'" if (d + f) % 2 else "."
            for rr in range(3, 7):
                _put(cv, round(cx + (rx + rr) * math.cos(ang)),
                     round(cy + (ry + rr - 2) * math.sin(ang)), ch, only_blank=True)
        # the dancers
        for i, dx in enumerate((10, 26, 46, 64, 82)):
            pose = _DANCERS[(f // 2 + i) % 4]
            _stamp(cv, pose, dx, 19)
        # the floor: lit tiles chase each other
        for row, y in ((0, 24), (1, 27)):
            for i in range(16):
                lit = (i + f + row) % 4 == 0
                tile = "|####|" if lit else "|____|"
                _stamp(cv, [tile], 2 + i * 6, y)
                _stamp(cv, ["|    |"], 2 + i * 6, y + 1)
        _seg(cv, 1, 29, 98, 29, "~")
        frames.append(_cv(cv))
    return _pad(frames)


DISCO = _make_disco_frames()


# =============================================================================
# 10) THE EXCHANGE — a grand old stone exchange at golden hour: pediment,
#     clock, six columns, brass doors and wide steps, with a live LED marquee
#     scrolling quotes across the facade. Flags flutter, birds drift past,
#     and traders shuffle in for the open.
#     NOTE: keep '-' out of the architecture — in this scene it's an accent
#     (red, for the marquee's down moves), as are '+', '%', '*' and 'o'.
# =============================================================================
_MARQUEE = ("  AAPL +1.2%    MSFT +0.8%    NVDA +3.1%    BHP -0.4%    CBA +0.6%   "
            " TSLA -2.2%    GOLD +0.3%    OIL -1.1%    SPX +0.5%    ASX +0.7%  ")


def _make_exchange_frames() -> list[list[str]]:
    frames = []
    tape = _MARQUEE * 2
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        # first stars of the evening
        _stars(cv, 4101, 22, 6, f, "..*.")
        # stepped pediment: underscore ledges climbing to a flat apex, with the
        # exchange clock and a proud cornerstone date inside
        prev = None
        for x0, x1, y in ((45, 55, 2), (40, 60, 3), (35, 65, 4), (30, 70, 5),
                          (25, 75, 6), (20, 80, 7), (16, 84, 8)):
            if prev is None:
                _seg(cv, x0, y, x1, y, "_")
            else:
                _seg(cv, x0 + 1, y, prev[0] - 1, y, "_")
                _seg(cv, prev[1] + 1, y, x1 - 1, y, "_")
                _put(cv, x0, y, "/")
                _put(cv, x1, y, "\\")
            prev = (x0, x1)
        _stamp(cv, ["(O)"], 48, 4)
        _stamp(cv, ["EST. 1998"], 45, 6)
        # frieze
        _stamp(cv, ["T H E   S T O C K   E X C H A N G E"], 32, 9)
        _seg(cv, 16, 10, 84, 10, "_")
        # the LED marquee, scrolling four columns per frame
        _seg(cv, 18, 11, 82, 11, "=")
        off = (f * 4) % len(_MARQUEE)
        _stamp(cv, ["[" + tape[off:off + 63] + "]"], 18, 12)
        _seg(cv, 18, 13, 82, 13, "=")
        # colonnade (base line first so the plinths stamp over it)
        _seg(cv, 16, 24, 84, 24, "_")
        for cx in (20, 30, 40, 56, 66, 76):
            _stamp(cv, ["/==\\"], cx, 14)
            for y in range(15, 24):
                _put(cv, cx + 1, y, "|")
                _put(cv, cx + 2, y, "|")
            _stamp(cv, ["[==]"], cx, 24)
        # brass doors between the middle columns
        _seg(cv, 45, 16, 55, 16, "_")
        for y in range(17, 24):
            _put(cv, 45, y, "|")
            _put(cv, 50, y, "|")
            _put(cv, 55, y, "|")
        _put(cv, 48, 20, "o")
        _put(cv, 52, 20, "o")
        # steps down to the street
        _seg(cv, 40, 25, 60, 25, "_")
        _seg(cv, 36, 26, 64, 26, "_")
        _seg(cv, 0, 27, CANVAS_W - 1, 27, "_")
        _speckle(cv, 4102, 28, 29, 30, ".,'")
        # fluttering corner flags
        for px in (16, 84):
            _seg(cv, px, 4, px, 7, "|")
            _put(cv, px, 3, ".")
            _stamp(cv, ["~" * (2 + f % 3)], px + 1, 4)
        # birds heading home
        bx = 92 - f * 6
        _stamp(cv, ["~v~"], bx, 2)
        _stamp(cv, ["v"], bx + 9, 4)
        # traders shuffling toward the doors
        for wx, stride in ((3 + (f * 2) % 26, f % 2),
                           (96 - (f * 2) % 26, (f + 1) % 2)):
            _put(cv, wx, 26, "o")
            _put(cv, wx, 27, "A" if stride else "|")
        frames.append(_cv(cv))
    return _pad(frames)


EXCHANGE = _make_exchange_frames()


# =============================================================================
# 11) AFTER HOURS — the city at night: a skyline of flickering lit windows,
#     a cratered moon, a blinking rooftop beacon, a shooting star and late
#     traffic heading home. The market sleeps; the compounding doesn't.
# =============================================================================
_NIGHT_BUILDINGS = [(2, 10, 12), (13, 9, 8), (23, 12, 15), (36, 11, 5),
                    (48, 9, 13), (58, 12, 9), (71, 9, 16), (81, 12, 11)]


def _night_windows() -> list[tuple[int, int, bool, int]]:
    """Fixed window grid: (x, y, lit at rest, flicker phase) per window."""
    rng = random.Random(0x9147)
    wins = []
    for bx, bw, top in _NIGHT_BUILDINGS:
        for wy in range(top + 2, 25, 2):
            for wx in range(bx + 2, bx + bw - 1, 3):
                wins.append((wx, wy, rng.random() < 0.62, rng.randrange(10)))
    return wins


_NIGHT_WINS = _night_windows()


def _make_night_frames() -> list[list[str]]:
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        _stars(cv, 5150, 55, 11, f)
        # the moon (after the stars, so none shine through it)
        _clear_disc(cv, 88, 4, 6.5, 3.2)
        _oval(cv, 88, 4, 6, 3)
        _put(cv, 86, 4, "o")
        _put(cv, 90, 3, "o")
        _put(cv, 89, 5, ".")
        # a shooting star crosses the first half of the loop
        if f < 6:
            sx, sy = 6 + f * 7, 2 + f // 2
            _put(cv, sx, sy, "*", only_blank=True)
            _put(cv, sx - 2, sy, ".", only_blank=True)
            _put(cv, sx - 4, sy - 1, ".", only_blank=True)
        # the skyline
        for bx, bw, top in _NIGHT_BUILDINGS:
            _seg(cv, bx, top, bx + bw - 1, top, "_")
            for y in range(top + 1, 26):
                _put(cv, bx, y, "|")
                _put(cv, bx + bw - 1, y, "|")
        for wx, wy, lit, phase in _NIGHT_WINS:
            on = lit ^ ((f + phase) % 10 == 0)   # the odd office lamp toggles
            _put(cv, wx, wy, "#" if on else "'")
        # blinking beacon on the tallest tower
        bx, bw, top = _NIGHT_BUILDINGS[3]
        mast = bx + bw // 2
        _seg(cv, mast, top - 3, mast, top - 1, "|")
        _put(cv, mast, top - 4, "@" if f % 2 else ".")
        # the street: ground line and late traffic in both lanes
        _seg(cv, 0, 26, CANVAS_W - 1, 26, "_")
        _stamp(cv, ["<o==[]"], CANVAS_W - (f * 9) % (CANVAS_W + 12), 27)
        _stamp(cv, ["[]==o>"], (f * 7) % (CANVAS_W + 12) - 6, 28)
        _speckle(cv, 5151, 29, 29, 26, ".'.")
        frames.append(_cv(cv))
    return _pad(frames)


NIGHT = _make_night_frames()


# =============================================================================
# 12) THE VAULT — the walk-in kind: a giant steel door with a spinning
#     handwheel and riveted rim, racks of glinting gold bars, coin stacks,
#     a sweeping security beam, a blinking camera and one guard mouse who
#     has never once been consulted about asset allocation.
# =============================================================================
def _make_vault_frames() -> list[list[str]]:
    cx, cy, rx, ry = 27, 15, 15, 10.5   # the door
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        # the security beam sweeps first, so the solid door/racks draw over it
        _seg(cv, 92, 3, 10 + (f * 9) % 80, 26, ".")
        # camera in the corner, status lamp blinking
        _stamp(cv, ["[o]=", "   \\"], 93, 2)
        _put(cv, 92, 2, "@" if f % 2 else "*")
        # vault ceiling + floor
        _seg(cv, 0, 1, CANVAS_W - 1, 1, "_")
        _seg(cv, 0, 27, CANVAS_W - 1, 27, "_")
        _speckle(cv, 6101, 28, 29, 24, ",`")
        # the door: cleared disc, double ring, rivets round the rim
        _clear_disc(cv, cx, cy, rx + 1.6, ry + 1.2)
        _oval(cv, cx, cy, rx, ry)
        _oval(cv, cx, cy, rx - 3, ry - 2.2)
        for i in range(12):
            t = 2 * math.pi * i / 12
            _put(cv, round(cx + (rx - 1.5) * math.cos(t)),
                 round(cy + (ry - 1.0) * math.sin(t)), "o")
        # the handwheel: six spokes, turning one notch per frame
        for d in range(6):
            ang = d * math.pi / 3 + f * math.pi / 12
            _seg(cv, cx, cy, round(cx + 6 * math.cos(ang)),
                 round(cy + 4.2 * math.sin(ang)))
        _stamp(cv, ["(+)"], cx - 1, cy)
        _stamp(cv, ["No.5"], cx - 2, cy + 6)
        # hinges + door jamb on the right
        for hy in (10, 15, 20):
            _stamp(cv, ["]="], cx + rx + 2, hy)
        # racks of gold bars, brick-laid two courses deep
        for sy in (10, 16, 22):
            _seg(cv, 52, sy, 96, sy, "_")
            for bx in range(54, 90, 7):
                _stamp(cv, ["[$$$]"], bx, sy - 1)
            for bx in range(57, 90, 14):
                _stamp(cv, ["[$$$]"], bx, sy - 2)
        for sx in (52, 96):   # rack rails
            for sy2 in range(8, 27):
                _put(cv, sx, sy2, "|", only_blank=True)
        # loose coin stacks between door and racks
        for sx, hgt in ((44, 3), (48, 2), (41, 1)):
            for k in range(hgt):
                _stamp(cv, ["(o)"], sx, 26 - k)
        # travelling glints across the bullion
        rng = random.Random(9200 + f)
        dollars = [(x, y) for y in range(7, 23)
                   for x in range(52, 97) if cv[y][x] == "$"]
        for gx, gy in rng.sample(dollars, min(5, len(dollars))):
            cv[gy][gx] = "*"
        # the guard mouse, on patrol
        _stamp(cv, ["<:3"], 88 - (f * 6) % 76, 26)
        frames.append(_cv(cv))
    return _pad(frames)


VAULT = _make_vault_frames()


# =============================================================================
# 13) THE HARBOUR — dividend delivery day: a loaded container ship idles at
#     the quay while the gantry crane lowers one more box aboard; gulls loop,
#     the swell rolls through, smoke drifts, and the sun sits low on the water.
# =============================================================================
def _make_harbour_frames() -> list[list[str]]:
    frames = []
    for f in range(FRAMES_PER_SCENE):
        cv = _blank()
        # low sun + shimmer rays
        _oval(cv, 10, 3, 5, 2.2)
        for d in range(5):
            ang = math.pi * (0.15 + 0.175 * d)
            _put(cv, round(10 + 8 * math.cos(ang)), round(3 - 3.4 * math.sin(ang)),
                 "'", only_blank=True)
        # gulls looping the masts
        _stamp(cv, ["~v~"], 20 + (f * 5) % 70, 2)
        _stamp(cv, ["v"], 88 - (f * 3) % 58, 3)
        # --- the gantry crane (right) ---------------------------------------- #
        _seg(cv, 58, 5, 97, 5, "=")
        _stamp(cv, ["[==]"], 58, 4)              # counterweight
        _stamp(cv, ["[##]"], 91, 4)              # operator cab
        for lx in (64, 92):                      # portal legs
            for y in range(6, 24):
                _put(cv, lx, y, "|")
                _put(cv, lx + 1, y, "|")
        # trolley rides the beam; its cable pays out, lowers, then hauls back
        tx = 66 + (f * 3) % 22
        drop = 4 + (f if f < 6 else 11 - f)
        _stamp(cv, ["[T]"], tx - 1, 4)
        for y in range(6, 6 + drop):
            _put(cv, tx, y, "|")
        _stamp(cv, ["[###]"], tx - 2, 6 + drop)
        # --- the ship (left) -------------------------------------------------- #
        _seg(cv, 4, 20, 56, 20, "_")             # deck line
        _seg(cv, 4, 21, 9, 24)                   # bow
        _seg(cv, 56, 21, 51, 24)                 # stern
        _seg(cv, 9, 24, 51, 24, "_")             # keel at the waterline
        _stamp(cv, ["S.S. DIVIDEND"], 19, 22)
        for px in range(12, 45, 6):              # portholes along the hull
            _put(cv, px, 21, "o", only_blank=True)
        # deck cargo: a pyramid of containers in mixed liveries
        fills = ("#", "=", ":")
        for tier in range(3):
            y = 19 - tier
            for i, bx in enumerate(range(10, 42 - tier * 8, 8)):
                _stamp(cv, ["[" + fills[(i + tier) % 3] * 5 + "]"], bx, y)
        # the bridge castle astern, radar sweeping
        _seg(cv, 46, 14, 55, 14, "_")
        for y in range(15, 20):
            _put(cv, 46, y, "|")
            _put(cv, 55, y, "|")
        _stamp(cv, ["# # #"], 48, 15)
        _stamp(cv, ["|/-\\"[f % 4]], 52, 12)   # the radar sweeps
        _put(cv, 52, 13, "|")
        # funnel + smoke drifting up-right
        _stamp(cv, ["(##)"], 46, 13)
        for k in range(3):
            _put(cv, 47 + k * 2 + f % 3, 11 - k * 2 - f % 2,
                 "o" if (k + f) % 2 else "O", only_blank=True)
        # --- the quay (right) and the sea ------------------------------------- #
        _seg(cv, 58, 24, 99, 24, "_")
        for qx in range(60, 99, 8):
            _put(cv, qx, 25, "|")
            _put(cv, qx, 26, "|")
        _stamp(cv, ["[=====]", "[#####]"], 76, 22)
        _stamp(cv, ["[:::::]"], 84, 23)
        # rolling swell, phase-shifted per row
        for y in range(25, 30):
            for x in range((y + f) % 3, 58 if y < 27 else CANVAS_W, 3):
                _put(cv, (x + f + y) % CANVAS_W if y >= 27 else x, y, "~",
                     only_blank=True)
        frames.append(_cv(cv))
    return _pad(frames)


HARBOUR = _make_harbour_frames()


# =============================================================================
# The playbill. Each entry: title, base colour, frames, rotating captions and
# optional per-character accent colours painted over the base colour.
# =============================================================================
ANIMATIONS = [
    {"title": "Candles", "color": "gray", "frames": CHART,
     "captions": ["hand-crafting artisanal candlesticks...",
                  "support? resistance? yes.",
                  "the moving average is on the move...",
                  "drawing lines until they mean something..."],
     "accents": {"█": "green", "▒": "red", "▲": "green", "▼": "red",
                 "+": "gold"}},
    {"title": "To the moon", "color": "cyan", "frames": ROCKET,
     "captions": ["STONKS only go up (allegedly)...",
                  "engaging yolo thrusters...",
                  "escape velocity: pending...",
                  "the moon is made of blue chips..."],
     "accents": {"*": "yellow", "+": "yellow", "#": "orange", "%": "yellow",
                 "o": "gray", "O": "gray", "~": "teal"}},
    {"title": "The bull", "color": "green", "frames": BULL,
     "captions": ["charging through the ticker list...",
                  "horns up, stop-losses tight...",
                  "running of the bulls (and the numbers)...",
                  "steam-powered optimism..."],
     "accents": {"@": "red", "~": "white", ":": "gold"}},
    {"title": "The whale", "color": "blue", "frames": WHALE,
     "captions": ["a whale surfaces in the order book...",
                  "counting plankton dividends...",
                  "making waves, absorbing spreads...",
                  "the school of hard stocks swims by..."],
     "accents": {"~": "teal", "^": "teal", "o": "white", "O": "white",
                 "<": "gold", ">": "gold", "*": "yellow"}},
    {"title": "Money printer", "color": "yellow", "frames": PRINTER,
     "captions": ["printing you a fresh report...",
                  "ink levels: bullish...",
                  "brrr intensifies...",
                  "quantitative easing, artisanal edition..."],
     "accents": {"$": "green", "~": "red", "#": "white"}},
    {"title": "Trading desk", "color": "purple", "frames": DESK,
     "captions": ["three monitors, zero regrets...",
                  "coffee in, analysis out...",
                  "the intern is on it...",
                  "alt-tabbing between conviction and doubt..."],
     "accents": {"^": "green", "v": "red", "~": "white", "o": "gold",
                 "#": "green"}},
    {"title": "Bear nap", "color": "blue", "frames": BEAR,
     "captions": ["shh... the bears are hibernating...",
                  "do not feed the bears (they short)...",
                  "bear-y quiet in here...",
                  "wake them after the next earnings call..."],
     "accents": {"z": "gray", "Z": "gray", "*": "yellow", "+": "white",
                 "o": "gold"}},
    {"title": "Diamond hands", "color": "teal", "frames": DIAMOND,
     "captions": ["hands: diamond. plan: hold...",
                  "polishing the gains...",
                  "unrealised? more like unREALised...",
                  "58 facets of pure conviction..."],
     "accents": {"*": "white", "+": "yellow", ".": "white", "'": "white"}},
    {"title": "Disco break", "color": "magenta", "frames": DISCO,
     "captions": ["vibing until the data lands...",
                  "bust a move, not the budget...",
                  "liquidity? more like liquid moves...",
                  "the floor is lava-lamp..."],
     "accents": {"#": "white", ":": "pink", "o": "gold", "*": "yellow",
                 "'": "yellow", ".": "yellow"}},
    {"title": "The exchange", "color": "gold", "frames": EXCHANGE,
     "captions": ["ring the opening bell...",
                  "columns: doric. spreads: tight...",
                  "the marquee waits for no one...",
                  "old stone, new numbers..."],
     "accents": {"+": "green", "-": "red", "%": "white", "*": "yellow",
                 "o": "white"}},
    {"title": "After hours", "color": "blue", "frames": NIGHT,
     "captions": ["markets closed. compounding continues...",
                  "a thousand windows, one open position...",
                  "the night shift watches the futures...",
                  "somewhere up there, a server hums..."],
     "accents": {"#": "gold", "*": "white", "@": "red", "o": "white",
                 ">": "yellow", "<": "yellow", "'": "gray"}},
    {"title": "The vault", "color": "gold", "frames": VAULT,
     "captions": ["your gains, now with a door...",
                  "spinning the combination...",
                  "audited daily by one small mouse...",
                  "gold bars: emotionally load-bearing..."],
     "accents": {"$": "yellow", "*": "white", "@": "red", ".": "red",
                 "o": "yellow", "<": "gray", ":": "gray", "3": "gray"}},
    {"title": "The harbour", "color": "teal", "frames": HARBOUR,
     "captions": ["your dividends are on the boat...",
                  "one more container of compounding...",
                  "the crane never chases the market...",
                  "fresh numbers, shipped FOB..."],
     "accents": {"#": "orange", "~": "blue", "o": "white", "O": "white",
                 "'": "yellow", "v": "white", "=": "gray", ":": "green",
                 "$": "yellow"}},
]

# All scenes share one height so switching animations never leaves stale rows.
_GLOBAL_H = max(len(f) for a in ANIMATIONS for f in a["frames"])


# =============================================================================
# The animator: a background thread that redraws frame + HUD in place.
# HUD = spinner + scene title + rotating caption, progress bar with rate & ETA,
# a scrolling ticker tape of recent symbols, and a market-vibes sparkline.
# Terminal-aware: centres the scene, crops to fit, rescales the bar.
# =============================================================================
class Animator:
    """Plays `ANIMATIONS` in a background thread, cycling every switch_seconds.

    Call start(), feed progress via update(), and stop() when done. stop()
    erases the animation block and restores the cursor, leaving a clean console
    for the caller's own summary line.
    """

    def __init__(self, total: int, stream=None, *, switch_seconds: float = SWITCH_SECONDS,
                 fps: float = FPS):
        self.total = max(1, int(total))
        self.stream = stream or sys.stderr
        self.switch_seconds = max(1.0, switch_seconds)
        self.frame_interval = 1.0 / max(1.0, fps)
        self._done = 0
        self._ok = 0
        self._failed = 0
        self._last_ticker = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_height = 0
        self._anim_idx = 0
        self._frame_idx = 0
        self._anim_started = time.monotonic()
        self._run_started = time.monotonic()
        # ticker tape of recently finished symbols: (ticker, succeeded)
        self._tape: deque[tuple[str, bool]] = deque(maxlen=8)
        self._prev_failed = 0
        # market-vibes sparkline: a gentle random walk, purely decorative
        self._rng = random.Random(0xC1A0D)
        self._pulse: deque[int] = deque([3] * PULSE_WIDTH, maxlen=PULSE_WIDTH)
        self._unicode_ok = self._probe_unicode()

    # -- public API ----------------------------------------------------------
    def start(self) -> None:
        self.stream.write(HIDE_CURSOR)
        self.stream.flush()
        self._run_started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, done: int, *, ticker: str = "", ok: int = 0, failed: int = 0) -> None:
        with self._lock:
            self._done = done
            self._last_ticker = ticker
            self._ok = ok
            if ticker:
                self._tape.append((ticker, failed <= self._prev_failed))
            self._prev_failed = failed
            self._failed = failed

    def log(self, msg: str) -> None:
        """Print a persistent line ABOVE the animation (scrolls into history)."""
        with self._lock:
            self._erase_block()
            self.stream.write(msg.rstrip("\n") + "\n")
            self._last_height = 0  # force a fresh redraw below the new log line
            self.stream.flush()

    def stop(self, final_msg: str | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._lock:
            self._erase_block()
            self.stream.write(SHOW_CURSOR)
            if final_msg:
                self.stream.write(final_msg + "\n")
            self.stream.flush()

    # -- internals -----------------------------------------------------------
    def _probe_unicode(self) -> bool:
        enc = getattr(self.stream, "encoding", None) or "ascii"
        try:
            _FANCY_PROBE.encode(enc)
            return True
        except (UnicodeEncodeError, LookupError):
            return False

    def _fancy(self, s: str) -> str:
        return s if self._unicode_ok else s.translate(_ASCII_FALLBACK)

    def _term_size(self) -> tuple[int, int]:
        try:
            sz = os.get_terminal_size(self.stream.fileno())
            return max(40, sz.columns), max(12, sz.lines)
        except Exception:
            return 120, 42

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._draw()
            time.sleep(self.frame_interval)
            self._frame_idx += 1
            if time.monotonic() - self._anim_started >= self.switch_seconds:
                self._anim_idx = (self._anim_idx + 1) % len(ANIMATIONS)
                self._frame_idx = 0
                self._anim_started = time.monotonic()

    def _paint(self, line: str, base: str, accents: dict[str, str]) -> str:
        """Colour one art line: base colour plus per-character accent pops."""
        if accents and any(ch in line for ch in accents):
            out = [base]
            for ch in line:
                acc = accents.get(ch)
                if acc:
                    out.append(COLOR[acc] + ch + base)
                else:
                    out.append(ch)
            out.append(RESET)
            return "".join(out)
        return base + line + RESET

    def _draw(self) -> None:
        cols, rows = self._term_size()
        anim = ANIMATIONS[self._anim_idx]
        base = COLOR.get(anim["color"], "")
        accents = anim.get("accents") or {}
        frames = anim["frames"]
        frame = frames[self._frame_idx % len(frames)]
        art = list(frame) + [""] * (_GLOBAL_H - len(frame))
        avail = max(6, rows - 7)          # leave room for the 5-line HUD
        if len(art) > avail:
            art = art[:avail]
        artw = max((len(ln) for ln in art), default=0)
        maxw = cols - 1
        pad = " " * max(0, (maxw - artw) // 2)
        lines = [self._paint((pad + self._fancy(ln))[:maxw], base, accents)
                 for ln in art]
        lines.append("")
        lines.append(pad + self._caption_line(anim, base))
        lines.append(pad + self._progress_line(cols))
        lines.append(pad + self._tape_line(maxw - len(pad)))
        lines.append(pad + self._pulse_line(base))
        self._render(lines)

    def _caption_line(self, anim: dict, base: str) -> str:
        spinner = SPINNER_BRAILLE if self._unicode_ok else SPINNER
        spin = spinner[self._frame_idx % len(spinner)]
        captions = anim.get("captions") or [anim.get("caption", "")]
        elapsed = time.monotonic() - self._anim_started
        caption = captions[int(elapsed / CAPTION_SECONDS) % len(captions)]
        return f"  {BOLD}{base}{spin} [{anim['title']}]  {caption}{RESET}"

    def _progress_line(self, cols: int) -> str:
        frac = min(1.0, self._done / self.total)
        width = max(24, min(56, cols - 64))
        filled = int(width * frac)
        full_ch, empty_ch = ("█", "░") if self._unicode_ok else ("#", ".")
        bar = GREEN + full_ch * filled + DIM + empty_ch * (width - filled) + RESET
        sep = "·" if self._unicode_ok else "|"
        elapsed = max(1e-6, time.monotonic() - self._run_started)
        rate = self._done / elapsed
        if self._done and rate > 0:
            eta = self._fmt_secs((self.total - self._done) / rate)
        else:
            eta = "??"
        return (f"  {bar} {int(frac * 100):3d}%  {self._done}/{self.total} "
                f"{sep} {GREEN}ok {self._ok}{RESET} {sep} {RED}fail {self._failed}{RESET} "
                f"{sep} {rate:.1f}/s {sep} eta {eta}")

    def _tape_line(self, maxw: int) -> str:
        if not self._tape:
            return f"  {DIM}tape: warming up the ticker tape...{RESET}"
        good = self._fancy("✓")
        bad = self._fancy("✗")
        sep = self._fancy(" · ")
        cells = []
        for ticker, succeeded in self._tape:
            mark = f"{GREEN}{good}{RESET}" if succeeded else f"{RED}{bad}{RESET}"
            cells.append((f"{ticker} {mark}", len(ticker) + 2))
        # keep only as many cells as actually fit the terminal width
        out, used = [], 8
        for cell, w in reversed(cells):
            if used + w + 3 > maxw:
                break
            out.append(cell)
            used += w + 3
        return f"  {DIM}tape:{RESET} " + sep.join(reversed(out))

    def _pulse_line(self, base: str) -> str:
        last = self._pulse[-1]
        step = self._rng.choice((-1, -1, 0, 0, 0, 1, 1))
        self._pulse.append(max(0, min(7, last + step)))
        ramp = PULSE_UNICODE if self._unicode_ok else PULSE_ASCII
        spark = "".join(ramp[v] for v in self._pulse)
        return f"  {DIM}vibes:{RESET} {base}{spark}{RESET}"

    @staticmethod
    def _fmt_secs(secs: float) -> str:
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m{secs % 60:02d}s"
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"

    def _render(self, lines: list[str]) -> None:
        parts = []
        if self._last_height:
            parts.append(f"\033[{self._last_height}A")  # cursor up to block top
        for ln in lines:
            parts.append("\r\033[K" + ln + "\n")         # clear line, then draw
        self.stream.write("".join(parts))
        self.stream.flush()
        self._last_height = len(lines)

    def _erase_block(self) -> None:
        """Clear the currently drawn block and park the cursor at its top."""
        if not self._last_height:
            return
        self.stream.write(f"\033[{self._last_height}A")
        for _ in range(self._last_height):
            self.stream.write("\r\033[K\n")
        self.stream.write(f"\033[{self._last_height}A")
        self._last_height = 0


# =============================================================================
# Standalone previews:
#   py ascii_art.py                  live demo (Ctrl-C to stop)
#   py ascii_art.py --stills [name]  dump frames as plain text, no ANSI
# =============================================================================
_DEMO_TICKERS = ["BHP.AX", "CBA.AX", "CSL.AX", "WES.AX", "NAB.AX", "WBC.AX",
                 "ANZ.AX", "FMG.AX", "WOW.AX", "TLS.AX", "RIO.AX", "MQG.AX",
                 "GMG.AX", "TCL.AX", "STO.AX", "QBE.AX", "ALL.AX", "COL.AX"]


def _stills(name_filter: str = "") -> None:
    """Print every frame of every animation as plain text (alignment check)."""
    for anim in ANIMATIONS:
        if name_filter and name_filter.lower() not in anim["title"].lower():
            continue
        print(f"{'=' * 100}\n{anim['title']}  ({len(anim['frames'])} frames)\n{'=' * 100}")
        for i, frame in enumerate(anim["frames"]):
            print(f"--- frame {i + 1} ---")
            for ln in frame:
                print(ln)
        print()


def _demo() -> None:
    if not enable_ansi():
        print("This terminal doesn't support ANSI escapes; animation disabled.")
        return
    total = 500
    a = Animator(total, stream=sys.stdout, switch_seconds=SWITCH_SECONDS)
    print("Preview - cycling through the whole cast (Ctrl-C to stop):\n")
    a.start()
    try:
        fails = 0
        for i in range(total + 1):
            if i and i % 40 == 0:
                fails += 1
            a.update(i, ticker=_DEMO_TICKERS[i % len(_DEMO_TICKERS)],
                     ok=i - fails, failed=fails)
            time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        a.stop("Preview done. The closing bell rings.")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if "--stills" in sys.argv:
        idx = sys.argv.index("--stills")
        name = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        _stills(name)
    else:
        _demo()
