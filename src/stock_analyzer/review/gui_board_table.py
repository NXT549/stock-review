"""The Live board's table: a virtualised grid drawn on a Tk canvas.

``ttk.Treeview`` can only colour a whole row, so a fetched row had to be green
from end to end. Drawing the grid ourselves gives per-cell colour (a status
mark, a signed return with a small bar, neutral ink for everything else),
sortable headers and hover, and it only draws the rows that are on screen, so a
2,000-ticker run costs the same as a 20-ticker one.

The geometry is pure (no Tk) and lives in the functions at the top, which is
what ``tests/test_gui_board.py`` covers. :class:`BoardTable` is the widget;
:class:`Mosaic` and :class:`ScenePane` (the loading scene) live here too.
The table never formats or invents data: the owner passes a ``cell(row, key)``
callback returning a :class:`Cell`, and an empty ``Cell`` draws nothing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int            # px at 100 % scaling; the minimum for a stretch column
    anchor: str = "w"     # "w" | "e"
    weight: int = 0       # share of the spare width
    mono: bool = False
    sortable: bool = True
    bar: bool = False     # reserves the diverging-bar slot (signed return)
    sub: bool = False     # reserves the suffix slot (currency code)


@dataclass
class Cell:
    text: str = ""
    fg: str = ""          # "" = the theme's ink
    mark: str = ""        # status shape: "dot" | "ring" | "cross" | "pip"
    mark_fg: str = ""
    bar: float | None = None   # signed fraction for the diverging bar
    bar_fg: str = ""
    sub: str = ""         # small muted suffix in its own fixed slot (currency)


@dataclass(frozen=True)
class Scene:
    """One frame of an ASCII picture on a fixed character grid: the line art,
    accent runs laid over it, and a caption underneath. See :class:`ScenePane`."""
    lines: tuple            # one string per row
    runs: tuple             # (row, column, text) drawn over the lines in the accent
    caption: str = ""
    cols: int = 100
    rows: int = 30


# --------------------------------------------------------------------------- #
# Pure geometry
# --------------------------------------------------------------------------- #
def layout_columns(columns, total: int, scale: float = 1.0) -> list[tuple[int, int]]:
    """(x0, x1) per column. Fixed columns keep ``width``; spare pixels are shared
    by ``weight``. When the window is narrower than the minimums the stretch
    columns simply stay at their minimum and the last columns clip."""
    widths = [max(1, round(c.width * scale)) for c in columns]
    spare = int(total) - sum(widths)
    weights = sum(c.weight for c in columns)
    if spare > 0 and weights:
        given = 0
        stretch = [i for i, c in enumerate(columns) if c.weight]
        for i in stretch:
            add = spare * columns[i].weight // weights
            widths[i] += add
            given += add
        widths[stretch[-1]] += spare - given   # rounding crumbs
    out, x = [], 0
    for w in widths:
        out.append((x, x + w))
        x += w
    return out


def rows_visible(height: int, row_h: int) -> int:
    """Whole rows that fit in ``height`` (never fewer than one)."""
    return max(1, int(height) // max(1, int(row_h)))


def clamp_first(first: int, n: int, visible: int) -> int:
    """Keep the first visible row inside 0 .. n - visible."""
    return max(0, min(int(first), max(0, n - visible)))


def scroll_to_show(first: int, index: int, visible: int, n: int) -> int:
    """The first-row value that brings ``index`` on screen with the least move."""
    if index < first:
        first = index
    elif index >= first + visible:
        first = index - visible + 1
    return clamp_first(first, n, visible)


def thumb(first: int, n: int, visible: int, track: int,
          min_len: int = 24) -> tuple[int, int] | None:
    """(offset, length) of the scrollbar thumb, or None when everything fits."""
    if n <= visible or track <= 0:
        return None
    length = max(min(min_len, track), round(track * visible / n))
    travel = track - length
    pos = round(travel * first / (n - visible)) if n > visible else 0
    return max(0, min(pos, travel)), length


def first_for_thumb(pos: int, n: int, visible: int, track: int,
                    min_len: int = 24) -> int:
    """Inverse of :func:`thumb`: the first row for a thumb dragged to ``pos``."""
    t = thumb(0, n, visible, track, min_len)
    if t is None:
        return 0
    travel = track - t[1]
    if travel <= 0:
        return 0
    return clamp_first(round(pos / travel * (n - visible)), n, visible)


def bar_extent(value, half: int, cap: float = 0.20) -> int:
    """Signed pixel length of the return bar: ``cap`` (20 %) or more fills the
    half-width. None draws nothing (0), it is never treated as a zero return."""
    if value is None or not half:
        return 0
    frac = max(-1.0, min(1.0, float(value) / cap))
    px = round(abs(frac) * half)
    if px == 0 and value:
        px = 1                      # a real, tiny move still shows a sliver
    return px if value >= 0 else -px


def fit_text(text: str, width: int, measure) -> str:
    """``text`` shortened with an ellipsis until ``measure(text) <= width``."""
    if not text or measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + "…") if lo else ""


def fit_art(width: int, height: int, cols: int, rows: int, measure,
            sizes=range(7, 23)) -> int | None:
    """Largest font size in ``sizes`` at which a ``cols`` x ``rows`` character
    grid fits ``width`` x ``height``; ``measure(size) -> (char width, line
    height)``. None when even the smallest does not fit: the caller then shows
    something else rather than art too small to read."""
    best = None
    for size in sizes:
        cw, lh = measure(size)
        if cw * cols > width or lh * rows > height:
            break
        best = size
    return best


def mosaic_grid(n: int, width: int, height: int,
                max_cell: int = 16) -> tuple[int, int, int, int]:
    """(columns, rows, pitch, gap) for ``n`` squares in a ``width`` x ``height``
    box, using the largest squares that fit. ``pitch`` is square + gap. When
    even the smallest squares do not fit, the grid is full and the caller draws
    only ``columns * rows`` of them."""
    if n <= 0 or width <= 0 or height <= 0:
        return 0, 0, 0, 0
    for pitch in range(max_cell, 1, -1):
        gap = 2 if pitch >= 8 else 1 if pitch >= 4 else 0
        cols, rows = (width + gap) // pitch, (height + gap) // pitch
        if cols * rows >= n:
            return cols, -(-n // cols), pitch, gap
    cols, rows = width // 2, height // 2
    return cols, rows, 2, 0


def mosaic_index(x: int, y: int, cols: int, pitch: int, n: int) -> int | None:
    """Which square is under (x, y), or None."""
    if not cols or not pitch or x < 0 or y < 0:
        return None
    col, row = x // pitch, y // pitch
    if col >= cols:
        return None
    i = row * cols + col
    return i if i < n else None


def draw_mark(canvas, shape: str, x: int, y: int, r: int, fg: str, width: int = 2) -> None:
    """A status mark. The shapes differ as well as the colours, so a state is
    readable without colour: dot = fetched, ring = retrying, cross = failed,
    pip = queued."""
    if shape == "dot":
        canvas.create_oval(x - r, y - r, x + r, y + r, fill=fg, outline="")
    elif shape == "ring":
        canvas.create_oval(x - r, y - r, x + r, y + r, outline=fg, width=width)
    elif shape == "cross":
        canvas.create_line(x - r, y - r, x + r, y + r, fill=fg, width=width)
        canvas.create_line(x - r, y + r, x + r, y - r, fill=fg, width=width)
    elif shape == "pip":
        q = max(1, r // 2)
        canvas.create_oval(x - q, y - q, x + q, y + q, fill=fg, outline="")


# --------------------------------------------------------------------------- #
# The widget
# --------------------------------------------------------------------------- #
class BoardTable:
    """Header + virtualised body + slim scrollbar, all on canvases.

    ``rows`` is a list of objects with a ``ticker`` attribute (the row key).
    ``cell(row, column_key) -> Cell`` and ``row_bg(row) -> colour | ""`` decide
    what is drawn; the table only owns layout, scrolling, hover, selection and
    the sort indicator."""

    def __init__(self, parent, *, theme, fonts: dict, columns, cell, row_bg=None,
                 scale: float = 1.0, on_sort=None, on_select=None,
                 on_activate=None) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.t, self.columns, self.cell, self.row_bg = theme, list(columns), cell, row_bg
        self.scale = scale
        self.on_sort, self.on_select, self.on_activate = on_sort, on_select, on_activate
        self.rows: list = []
        self.index: dict[str, int] = {}
        self.first = 0
        self.hover: int | None = None
        self.selected: str | None = None
        self.sort_key: str | None = None
        self.sort_desc = False
        self.empty_text, self.empty_art = "", ""
        self._drag: int | None = None
        self._fit_cache: dict = {}

        px = self.px
        self.row_h, self.head_h, self.pad = px(30), px(32), px(12)
        self.f_ui, self.f_mono = fonts["ui"], fonts["mono"]
        self.f_head, self.f_sub, self.f_art = fonts["head"], fonts["mono_s"], fonts["art"]
        self._m = {f: tkfont.Font(font=f) for f in
                   (self.f_ui, self.f_mono, self.f_sub)}
        self.sub_w = self._measure(self.f_sub, "GBp") + px(6)
        self.bar_half = px(20)

        t = theme
        self.frame = tk.Frame(parent, bg=t.surface, highlightthickness=1,
                              highlightbackground=t.line, highlightcolor=t.accent)
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(1, weight=1)
        self.head = tk.Canvas(self.frame, height=self.head_h, bg=t.raised,
                              highlightthickness=0, bd=0)
        self.head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.body = tk.Canvas(self.frame, bg=t.surface, highlightthickness=0, bd=0,
                              takefocus=1)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.bar = tk.Canvas(self.frame, width=px(10), bg=t.surface,
                             highlightthickness=0, bd=0)
        self.bar.grid(row=1, column=1, sticky="ns")

        b = self.body
        b.bind("<Configure>", lambda _e: self.redraw())
        b.bind("<Motion>", self._on_motion)
        b.bind("<Leave>", lambda _e: self._set_hover(None))
        b.bind("<Button-1>", self._on_click)
        b.bind("<Double-Button-1>", lambda _e: self._activate())
        b.bind("<Return>", lambda _e: self._activate())
        b.bind("<MouseWheel>", self._on_wheel)
        b.bind("<Button-4>", lambda _e: self.scroll_by(-3))
        b.bind("<Button-5>", lambda _e: self.scroll_by(3))
        b.bind("<FocusIn>", lambda _e: self.frame.configure(highlightbackground=t.accent))
        b.bind("<FocusOut>", lambda _e: self.frame.configure(highlightbackground=t.line))
        for key, fn in (("<Up>", lambda: self.move(-1)), ("<Down>", lambda: self.move(1)),
                        ("<Prior>", lambda: self.move(-self.visible())),
                        ("<Next>", lambda: self.move(self.visible())),
                        ("<Home>", lambda: self.move(-len(self.rows))),
                        ("<End>", lambda: self.move(len(self.rows)))):
            b.bind(key, lambda _e, f=fn: (f(), "break")[1])
        self.head.bind("<Configure>", lambda _e: self._draw_head())
        self.head.bind("<Button-1>", self._on_head_click)
        self.head.bind("<Motion>", self._on_head_motion)
        self.bar.bind("<Button-1>", self._on_bar_press)
        self.bar.bind("<B1-Motion>", self._on_bar_drag)
        self.bar.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    # -- small helpers ------------------------------------------------------- #
    def px(self, n: float) -> int:
        return max(1, round(n * self.scale))

    def _measure(self, font, text: str) -> int:
        return self._m[font].measure(text)

    def _fit(self, font, text: str, width: int) -> str:
        key = (font, text, width)
        hit = self._fit_cache.get(key)
        if hit is None:
            if len(self._fit_cache) > 4000:
                self._fit_cache.clear()
            hit = self._fit_cache[key] = fit_text(
                text, width, lambda s: self._measure(font, s))
        return hit

    def _slot(self, col) -> int:
        """Width reserved at the right of a cell for its bar and / or suffix."""
        return ((2 * self.bar_half + self.px(10)) if col.bar else 0) + \
            (self.sub_w if col.sub else 0)

    def visible(self) -> int:
        return rows_visible(self.body.winfo_height(), self.row_h)

    def _spans(self) -> list[tuple[int, int]]:
        return layout_columns(self.columns, self.body.winfo_width(), self.scale)

    # -- data ---------------------------------------------------------------- #
    def set_rows(self, rows, follow: bool = False) -> None:
        """Replace the displayed rows. ``follow`` keeps the view pinned to the
        end when it already was (rows arriving during a run)."""
        was_end = self.at_end()
        self.rows = list(rows)
        self.index = {r.ticker: i for i, r in enumerate(self.rows)}
        if self.selected not in self.index:
            self.selected = None
        if follow and was_end:
            self.first = len(self.rows)
        self.first = clamp_first(self.first, len(self.rows), self.visible())
        self.redraw()

    def set_sort(self, key: str | None, desc: bool = False) -> None:
        self.sort_key, self.sort_desc = key, desc
        self._draw_head()

    def set_empty(self, text: str = "", art: str = "") -> None:
        self.empty_text, self.empty_art = text, art
        if not self.rows:
            self.redraw()

    def at_end(self) -> bool:
        return self.first >= len(self.rows) - self.visible()

    def to_top(self) -> None:
        self.first = 0
        self.redraw()

    def see(self, key: str) -> None:
        i = self.index.get(key)
        if i is not None:
            self.first = scroll_to_show(self.first, i, self.visible(), len(self.rows))
            self.redraw()

    def scroll_by(self, rows: int) -> None:
        new = clamp_first(self.first + rows, len(self.rows), self.visible())
        if new != self.first:
            self.first = new
            self.redraw()

    def move(self, delta: int) -> None:
        """Keyboard: move the selection by ``delta`` rows and keep it visible."""
        if not self.rows:
            return
        cur = self.index.get(self.selected or "", -1)
        new = max(0, min(len(self.rows) - 1, (cur if cur >= 0 else self.first) +
                         (delta if cur >= 0 else 0)))
        self.selected = self.rows[new].ticker
        self.first = scroll_to_show(self.first, new, self.visible(), len(self.rows))
        self.redraw()
        if self.on_select:
            self.on_select(self.selected)

    # -- events -------------------------------------------------------------- #
    def _row_at(self, y: int) -> int | None:
        i = self.first + int(y) // self.row_h
        return i if 0 <= i < len(self.rows) else None

    def _set_hover(self, i: int | None) -> None:
        if i != self.hover:
            self.hover = i
            self.redraw()

    def _on_motion(self, e) -> None:
        self._set_hover(self._row_at(e.y))

    def _on_click(self, e) -> None:
        self.body.focus_set()
        i = self._row_at(e.y)
        self.selected = self.rows[i].ticker if i is not None else None
        self.redraw()
        if self.on_select:
            self.on_select(self.selected)

    def _activate(self) -> None:
        if self.selected and self.on_activate:
            self.on_activate(self.selected)

    def _on_wheel(self, e) -> None:
        delta = getattr(e, "delta", 0)
        if delta:
            self.scroll_by(-3 if delta > 0 else 3)

    def _head_col(self, x: int):
        for col, (x0, x1) in zip(self.columns, self._spans()):
            if x0 <= x < x1:
                return col
        return None

    def _on_head_click(self, e) -> None:
        col = self._head_col(e.x)
        if col is not None and col.sortable and self.on_sort:
            self.on_sort(col.key)

    def _on_head_motion(self, e) -> None:
        col = self._head_col(e.x)
        self.head.configure(cursor="hand2" if col is not None and col.sortable else "")

    def _on_bar_press(self, e) -> None:
        t = thumb(self.first, len(self.rows), self.visible(), self.bar.winfo_height())
        if t is None:
            return
        pos, length = t
        if pos <= e.y <= pos + length:
            self._drag = e.y - pos
        else:                                   # click on the track: page
            self.scroll_by(-self.visible() if e.y < pos else self.visible())

    def _on_bar_drag(self, e) -> None:
        if self._drag is None:
            return
        new = first_for_thumb(e.y - self._drag, len(self.rows), self.visible(),
                              self.bar.winfo_height())
        if new != self.first:
            self.first = new
            self.redraw()

    # -- drawing ------------------------------------------------------------- #
    def _draw_head(self) -> None:
        c, t = self.head, self.t
        c.delete("all")
        w, h = c.winfo_width(), self.head_h
        c.create_line(0, h - 1, w, h - 1, fill=t.line)
        mid = h // 2
        for col, (x0, x1) in zip(self.columns, self._spans()):
            active = col.key == self.sort_key
            fg = t.accent_text if active else t.muted
            arrow = self.px(12) if active else 0
            if col.anchor == "e":
                # sit over the figures, not over the bar / currency slot beside them
                edge = x1 - self.pad - self._slot(col)
                c.create_text(edge - arrow, mid, text=col.title, anchor="e", fill=fg,
                              font=self.f_head)
                ax = edge - self.px(4)
            else:
                item = c.create_text(x0 + self.pad, mid, text=col.title, anchor="w",
                                     fill=fg, font=self.f_head)
                ax = c.bbox(item)[2] + self.px(8)
            if active:
                r = self.px(3)
                pts = ((ax - r, mid + r - 1, ax + r, mid + r - 1, ax, mid - r) if not self.sort_desc
                       else (ax - r, mid - r + 1, ax + r, mid - r + 1, ax, mid + r))
                c.create_polygon(*pts, fill=t.accent_text, outline="")

    def redraw(self) -> None:
        c, t = self.body, self.t
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        n = len(self.rows)
        if not n:
            self._draw_empty(w, h)
            self._draw_bar()
            return
        vis = self.visible()
        self.first = clamp_first(self.first, n, vis)
        spans = self._spans()
        pad, rh = self.pad, self.row_h
        for i in range(self.first, min(n, self.first + vis + 1)):
            row = self.rows[i]
            y0 = (i - self.first) * rh
            y1, mid = y0 + rh, y0 + rh // 2
            selected = row.ticker == self.selected
            bg = (t.sel if selected else t.hover if i == self.hover
                  else (self.row_bg(row) if self.row_bg else ""))
            if bg:
                c.create_rectangle(0, y0, w, y1 - 1, fill=bg, outline="")
            if selected:
                c.create_rectangle(0, y0, self.px(3), y1 - 1, fill=t.accent, outline="")
            c.create_line(0, y1 - 1, w, y1 - 1, fill=t.line)
            for col, (x0, x1) in zip(self.columns, spans):
                cell = self.cell(row, col.key)
                if cell is None:
                    continue
                font = self.f_mono if col.mono else self.f_ui
                left, right = x0 + pad, x1 - pad
                if cell.mark:
                    draw_mark(c, cell.mark, left + self.px(4), mid, self.px(4),
                              cell.mark_fg or t.muted, self.px(2))
                    left += self.px(16)
                if col.bar:                      # slot is reserved either way, so
                    if cell.bar is not None:     # figures align; nothing is drawn
                        axis = right - self.bar_half   # for a missing value
                        ext = bar_extent(cell.bar, self.bar_half)
                        c.create_line(axis, mid - self.px(5), axis, mid + self.px(5),
                                      fill=t.line_strong)
                        if ext:
                            c.create_rectangle(min(axis, axis + ext), mid - self.px(3),
                                               max(axis, axis + ext), mid + self.px(3),
                                               fill=cell.bar_fg or t.muted, outline="")
                    right -= 2 * self.bar_half + self.px(10)
                if col.sub:                      # fixed slot keeps the figures aligned
                    if cell.sub:
                        c.create_text(right, mid, text=cell.sub, anchor="e",
                                      fill=t.muted, font=self.f_sub)
                    right -= self.sub_w
                if not cell.text:
                    continue
                text = self._fit(font, cell.text, max(0, right - left))
                c.create_text(right if col.anchor == "e" else left, mid, text=text,
                              anchor=col.anchor, fill=cell.fg or t.ink, font=font)
        self._draw_bar()

    def _draw_empty(self, w: int, h: int) -> None:
        c, t = self.body, self.t
        y = h // 2
        if self.empty_art:
            art = c.create_text(w // 2, y, text=self.empty_art, fill=t.accent_text,
                                font=self.f_art, justify="left", anchor="s")
            y = c.bbox(art)[3] + self.px(14)
        if self.empty_text:
            c.create_text(w // 2, y, text=self.empty_text, fill=t.muted, font=self.f_ui,
                          justify="center", anchor="n" if self.empty_art else "center",
                          width=max(self.px(200), min(w - self.px(60), self.px(460))))

    def _draw_bar(self) -> None:
        c, t = self.bar, self.t
        c.delete("all")
        tb = thumb(self.first, len(self.rows), self.visible(), c.winfo_height())
        if tb is None:
            return
        pos, length = tb
        inset = self.px(3)
        c.create_rectangle(inset, pos + inset, c.winfo_width() - inset,
                           pos + length - inset, fill=t.line_strong, outline="")


class Mosaic:
    """One small square per ticker: the whole run at a glance.

    ``colour(row) -> (fill, outline)`` decides each square (the owner maps a
    return to a shade and a failure to a hollow square). Squares are reused
    between redraws and only recoloured, so a 2,000-ticker run stays cheap."""

    def __init__(self, parent, *, bg: str, colour, empty: str, width: int,
                 height: int, on_hover=None, on_pick=None, on_layout=None,
                 fresh: str = "") -> None:
        import tkinter as tk

        self.colour, self.on_hover, self.on_pick = colour, on_hover, on_pick
        self.on_layout = on_layout    # called when the grid (and so shown()) changes
        self.rows: list = []
        self.total = 0
        self.empty = empty            # fill of a slot whose ticker is not in yet
        self.fresh = fresh            # outline of a square that is being pointed out
        self._fresh: frozenset = frozenset()
        self._pos: dict[str, int] = {}
        self._grid = (0, 0, 0, 0)
        self._items: list[int] = []
        self._paint: list[tuple[str, str]] = []
        self._key = None
        self._hover: int | None = None
        self.canvas = c = tk.Canvas(parent, bg=bg, width=width, height=height,
                                    highlightthickness=0, bd=0)
        c.bind("<Configure>", lambda _e: self.redraw())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda _e: self._set_hover(None))
        c.bind("<Button-1>", self._on_click)

    def set_rows(self, rows, total: int = 0) -> None:
        """``total`` is the size of the whole run when it is known to be larger
        than the rows so far; the grid is laid out for it, so squares fill in
        instead of reflowing as tickers arrive."""
        self.rows = list(rows)
        self._pos = {r.ticker: i for i, r in enumerate(self.rows)}
        self.total = max(int(total or 0), len(self.rows))
        self.redraw()

    def index_of(self, ticker: str) -> int | None:
        return self._pos.get(ticker)

    def set_fresh(self, indices=()) -> None:
        """Outline these squares (by index) until told otherwise: a ticker
        that has just landed, or the sweep that ends a clean run."""
        indices = frozenset(indices)
        if indices != self._fresh:
            self._fresh = indices
            self.redraw()

    def grid(self) -> tuple[int, int]:
        """(columns, squares drawn) of the current layout."""
        return self._grid[0], len(self._items)

    def shown(self) -> int:
        cols, rows, _pitch, _gap = self._grid
        return min(self.total, cols * rows)

    def redraw(self) -> None:
        c = self.canvas
        w, h, n = c.winfo_width(), c.winfo_height(), self.total
        key = (n, w, h)
        if key != self._key:                      # geometry changed: lay out again
            self._key = key
            c.delete("all")
            self._grid = cols, rows, pitch, gap = mosaic_grid(n, w, h)
            self._items, self._paint = [], []
            side = pitch - gap
            for i in range(min(n, cols * rows)):
                x, y = (i % cols) * pitch, (i // cols) * pitch
                self._items.append(c.create_rectangle(x, y, x + side, y + side, width=1))
                self._paint.append(("", ""))
            if self.on_layout:
                self.on_layout()
        known = len(self.rows)
        for i, item in enumerate(self._items):
            paint = self.colour(self.rows[i]) if i < known else (self.empty, "")
            if i in self._fresh and self.fresh:
                paint = (paint[0], self.fresh)
            if paint != self._paint[i]:
                self._paint[i] = paint
                c.itemconfigure(item, fill=paint[0], outline=paint[1] or paint[0])

    def _at(self, e) -> int | None:
        cols, _rows, pitch, _gap = self._grid
        return mosaic_index(e.x, e.y, cols, pitch, min(len(self._items), len(self.rows)))

    def _set_hover(self, i: int | None) -> None:
        if i != self._hover:
            self._hover = i
            self.canvas.configure(cursor="hand2" if i is not None else "")
            if self.on_hover:
                self.on_hover(self.rows[i] if i is not None else None)

    def _on_motion(self, e) -> None:
        self._set_hover(self._at(e))

    def _on_click(self, e) -> None:
        i = self._at(e)
        if i is not None and self.on_pick:
            self.on_pick(self.rows[i])


class ScenePane:
    """The run-start loading scene: an ASCII picture playing on its own canvas.

    The owner *places* the canvas over the filter chips and the table while a
    list is still being looked up (there is nothing to filter or list yet) and
    destroys it when the first ticker lands, so the layout underneath never
    changes. The picture is drawn at the largest font that fits; when even the
    smallest does not, :meth:`draw` says so and the owner shows nothing."""

    def __init__(self, parent, *, theme, art_font, ui_font, scale: float = 1.0) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        self.t, self.scale = theme, scale
        self.canvas = tk.Canvas(parent, bg=theme.surface, bd=0, highlightthickness=1,
                                highlightbackground=theme.line)
        self._family = tkfont.Font(font=art_font).actual("family")
        self._ui = ui_font
        self._cap_h = tkfont.Font(font=ui_font).metrics("linespace")
        self._fonts: dict = {}
        self.size: int | None = None      # font size (px) of the last frame drawn

    def px(self, n: float) -> int:
        return max(1, round(n * self.scale))

    def _font(self, size: int):
        """(font, char width, line height) of the art font at ``size`` pixels."""
        hit = self._fonts.get(size)
        if hit is None:
            from tkinter import font as tkfont
            f = tkfont.Font(family=self._family, size=-size)
            hit = self._fonts[size] = (f, max(1, f.measure("M")),
                                       max(1, f.metrics("linespace")))
        return hit

    def draw(self, scene: Scene, width: int, height: int) -> bool:
        """Draw ``scene`` centred in ``width`` x ``height``. In a tight pane the
        caption is the first thing to go; False (and nothing drawn) when even
        then the art would be too small for anyone to make out."""
        c, t = self.canvas, self.t
        gap, margin = self.px(14), self.px(12)
        for caption in dict.fromkeys((scene.caption, "")):
            cap_h = self._cap_h + gap if caption else 0
            self.size = fit_art(width - 2 * margin, height - 2 * margin - cap_h,
                                scene.cols, scene.rows, lambda n: self._font(n)[1:],
                                range(self.px(7), self.px(22) + 1))
            if self.size is not None:
                break
        else:
            return False
        font, cw, lh = self._font(self.size)
        x0 = (width - cw * scene.cols) // 2
        y0 = (height - lh * scene.rows - cap_h) // 2
        c.delete("all")
        for row, line in enumerate(scene.lines):
            if line:
                c.create_text(x0, y0 + row * lh, text=line, anchor="nw", fill=t.muted,
                              font=font)
        for row, col, text in scene.runs:
            c.create_text(x0 + col * cw, y0 + row * lh, text=text, anchor="nw",
                          fill=t.accent_text, font=font)
        if caption:
            c.create_text(width // 2, y0 + lh * scene.rows + gap, text=caption,
                          anchor="n", fill=t.muted, font=self._ui)
        return True
