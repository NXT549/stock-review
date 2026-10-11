"""Windows 98 look-and-feel — but with the colour turned up.

Keeps the classic 3D silver chrome (chiselled bevels, groove group boxes, sunken
white fields, MS Sans Serif) that makes the app read as genuine Win98, and layers
on the *fun*: the classic navy→blue **gradient title bars**, **coloured 3D push
buttons** (green Run, red Cancel, blue Open) that brighten on hover, and coloured
group-box titles. The ASCII-art "MS-DOS Prompt" screen stays the star of the show.

Classic ``tk`` widgets (not ``ttk``) are used throughout because they honour
``relief`` / ``bd`` / ``bg`` directly — exactly the toolkit Win98 shipped. Purely
cosmetic and stdlib-only (``tkinter``), so PyInstaller still freezes it into a
standalone ``.exe`` with no extra runtime deps.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

# --- The classic "3D Objects" palette --------------------------------------- #
FACE = "#c0c0c0"      # silver — window / button / group background
HILIGHT = "#ffffff"   # bevel: outer top-left highlight
LIGHT = "#dfdfdf"     # bevel: inner highlight
SHADOW = "#808080"    # bevel: inner bottom-right shadow
DARKEDGE = "#000000"  # bevel: outer shadow
NAVY = "#000080"      # active title bar (gradient start) / selection
TITLE_BLUE = "#1084d0"  # active title bar gradient end (the Win98 fade)
NAVY_TXT = "#ffffff"
FIELD = "#ffffff"     # sunken input background
INK = "#000000"
# Darkened from #808080 (2.17:1 against FACE, below WCAG AA's 4.5:1) to
# #4d4d4d (4.65:1) — used for genuinely-disabled text too, but ALSO for
# active de-emphasized reading (the universe hint line, the Workers label),
# which needs to actually be readable, not just decorative grey.
GRAYTEXT = "#4d4d4d"
STATUS_RED = "#981f1f"  # status-bar/caption text only (4.53:1 against FACE);
                        # kept separate from BTN_RED, whose white-on-face
                        # button-label contrast (5.62:1) was already fine —
                        # darkening BTN_RED itself would have been solving a
                        # problem that button didn't have.
TIP_BG = "#ffffe1"    # tooltip background (the classic InfoWindow yellow)
CRT_BG = "#000000"    # the DOS-box background for the ASCII animation
CRT_FG = "#d6dde6"

# Coloured button faces — the playful twist on the silver default. Tk derives the
# 3D bevel shades from the face colour, so these still read as chiselled buttons.
# BTN_GREEN/BTN_TEAL darkened so their WHITE button-label text clears 4.5:1
# (were 3.62:1 / 4.18:1); BTN_BLUE/BTN_PURPLE/BTN_RED already passed as-is.
BTN_GREEN = "#098828"   # Run
BTN_RED   = "#c62828"   # Cancel
BTN_BLUE  = "#1668c8"   # Open report
BTN_TEAL  = "#108282"   # Open folder / Browse
BTN_PURPLE = "#7a1fa2"  # Copy path / Tip of the Day
GOLD = "#b8860b"        # Tip-of-the-Day lightbulb accent (decoration only — too light for text)
STATUS_AMBER = "#664200"  # "done, but partial" status text (4.9:1 against FACE)

# Coloured group-box titles (bold, on the silver groove).
TITLE_PURPLE = "#7a1fa2"
# Darkened from #0a7d2e (2.90:1 against FACE) to #075c22 (4.50:1) —
# TITLE_PURPLE's ~4.53:1 was the passing reference this was calibrated against.
TITLE_GREEN  = "#075c22"

# MS Sans Serif is the true Win98 UI font; Tk substitutes silently (Tahoma /
# Segoe) if the family is missing, which is fine — the bevels carry the look.
# Bumped from the authentic-but-cramped 8pt to 9pt (readability finding); bold
# stays reserved for primary actions (button98's fill=... buttons) and group
# headings, not general body text — this pair only changes the SIZE.
UI_FONT = ("MS Sans Serif", 9)
UI_FONT_B = ("MS Sans Serif", 9, "bold")
UI_FONT_LG = ("MS Sans Serif", 10, "bold")   # dialog headings
UI_FONT_STR = "{MS Sans Serif} 9"   # option-database form (braces = multi-word family)
TITLE_FONT = ("MS Sans Serif", 9, "bold")
MONO_FONT = ("Consolas", 8)         # CRT panel — 100 cols must fit the window


def _mix(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colours (t=0 -> a, t=1 -> b)."""
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg_, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg_ - ag) * t)
    bl = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _lighten(c: str, t: float = 0.28) -> str:
    return _mix(c, "#ffffff", t)


def _entry_select_all(event) -> str:
    event.widget.select_range(0, "end")
    event.widget.icursor("end")
    return "break"


def apply_root(root: tk.Tk) -> None:
    """Silver background + sane global defaults for the whole widget tree."""
    root.configure(bg=FACE)
    # option_add values must be STRINGS — a font tuple won't apply through the
    # option database. Per-widget ``font=UI_FONT`` (the helpers below) is the
    # real mechanism; these are only catch-all defaults.
    root.option_add("*Font", UI_FONT_STR)
    root.option_add("*background", FACE)
    root.option_add("*foreground", INK)
    # Tk's Entry binds Ctrl+A to "line start" (emacs style); Windows users
    # expect select-all, and a half-replaced ticker list is an easy mistake.
    root.bind_class("Entry", "<Control-Key-a>", _entry_select_all)
    root.bind_class("Entry", "<Control-Key-A>", _entry_select_all)


# --------------------------------------------------------------------------- #
# Gradient title bar (the classic Win98 active-window fade)
# --------------------------------------------------------------------------- #
class TitleBar(tk.Canvas):
    """A navy→blue horizontal gradient strip with a white bold caption — the
    signature Win98 active title bar. Redraws itself to fill its width."""

    def __init__(self, parent, text: str, *, height: int = 20, left: str = NAVY,
                 right: str = TITLE_BLUE, fg: str = NAVY_TXT, font=TITLE_FONT):
        super().__init__(parent, height=height, bd=0, highlightthickness=0, bg=left)
        self._text, self._left, self._right = text, left, right
        self._fg, self._font, self._h = fg, font, height
        self.bind("<Configure>", self._redraw)

    def set_text(self, text: str) -> None:
        self._text = text
        self._redraw()

    def _redraw(self, _e=None) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = self._h
        steps = 48
        r1, g1, b1 = self.winfo_rgb(self._left)
        r2, g2, b2 = self.winfo_rgb(self._right)
        for i in range(steps):
            x0 = int(w * i / steps)
            x1 = int(w * (i + 1) / steps) + 1
            r = int(r1 + (r2 - r1) * i / steps) >> 8
            g = int(g1 + (g2 - g1) * i / steps) >> 8
            b = int(b1 + (b2 - b1) * i / steps) >> 8
            self.create_rectangle(x0, 0, x1, h, outline="", fill=f"#{r:02x}{g:02x}{b:02x}")
        self.create_text(7, h // 2 + 1, text=self._text, anchor="w",
                         fill=self._fg, font=self._font)


# --------------------------------------------------------------------------- #
# Colour + hover push button (still a real bevelled Win98 button)
# --------------------------------------------------------------------------- #
class _Button98(tk.Button):
    """A tk.Button whose face follows its state. Tk only dims the label of a
    disabled button, which on a coloured face (blue/teal/purple/red) leaves it
    looking clickable — so disabling also swaps the face to flat silver, and
    enabling restores the accent colour."""

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        res = super().configure(cnf, **kw)
        state = (cnf.get("state") if isinstance(cnf, dict) else None) or kw.get("state")
        if state is not None and getattr(self, "_accent", None):
            if str(state) == "disabled":
                super().configure(bg=FACE, fg=GRAYTEXT, relief="groove",
                                  cursor="arrow")
            else:
                super().configure(bg=self._accent, fg=self._accent_fg,
                                  relief="raised", cursor="hand2")
        return res

    config = configure


def button98(parent: tk.Misc, *, fill: str | None = None, fg: str | None = None,
             **kw) -> tk.Button:
    """A raised, bevelled Win98 push button. Pass ``fill=`` for a coloured face
    (white label, brightens on hover); omit it for the classic silver button.
    Coloured buttons go flat silver with a GRAYTEXT label while disabled."""
    base = fill or FACE
    hover = _lighten(base) if fill else LIGHT
    txt = fg or (NAVY_TXT if fill else INK)
    b = _Button98(parent, relief="raised", bd=2, bg=base, fg=txt,
                  activebackground=hover, activeforeground=txt,
                  disabledforeground=GRAYTEXT,
                  # 2px dark ring so keyboard focus shows on any face colour
                  highlightthickness=2, highlightbackground=FACE,
                  highlightcolor=INK,
                  font=UI_FONT_B if fill else UI_FONT, padx=10, pady=3,
                  cursor="hand2", **kw)
    b._accent, b._accent_fg = fill, txt
    if fill and str(b["state"]) == "disabled":
        b.configure(state="disabled")  # apply the silver face to a born-disabled button

    def _enter(_e):
        if str(b["state"]) != "disabled":
            b.configure(bg=hover)

    def _leave(_e):
        if str(b["state"]) != "disabled":
            b.configure(bg=base)

    b.bind("<Enter>", _enter)
    b.bind("<Leave>", _leave)
    return b


def group98(parent: tk.Misc, text: str, *, fg: str = NAVY) -> tk.LabelFrame:
    """An etched Win98 group box (``groove`` relief) with a bold coloured title."""
    return tk.LabelFrame(parent, text=text, relief="groove", bd=2,
                         bg=FACE, fg=fg, font=UI_FONT_B, padx=8, pady=6)


def label98(parent: tk.Misc, **kw) -> tk.Label:
    """A flat label on the silver background. Pass ``text=`` or ``textvariable=``."""
    kw.setdefault("bg", FACE)
    kw.setdefault("fg", INK)
    kw.setdefault("font", UI_FONT)
    return tk.Label(parent, **kw)


def entry98(parent: tk.Misc, textvariable: tk.Variable, width: int | None = None) -> tk.Entry:
    """A sunken white input field; greys out cleanly when disabled."""
    e = tk.Entry(parent, textvariable=textvariable, relief="sunken", bd=2,
                 bg=FIELD, fg=INK, insertbackground=INK, highlightthickness=2,
                 highlightbackground=FIELD, highlightcolor=NAVY, font=UI_FONT,
                 disabledbackground=FACE, disabledforeground=GRAYTEXT)
    if width:
        e.configure(width=width)
    return e


def field98(widget: tk.Widget) -> None:
    """Restyle an existing Entry/Text/Listbox as a sunken Win98 field."""
    widget.configure(relief="sunken", bd=2, bg=FIELD, fg=INK,
                     highlightthickness=2, highlightbackground=FIELD,
                     highlightcolor=NAVY, font=UI_FONT)


def check98(parent: tk.Misc, text: str, variable: tk.Variable, command=None) -> tk.Checkbutton:
    """A classic square-box check button (white interior when ticked)."""
    return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                          bg=FACE, fg=INK, activebackground=FACE,
                          activeforeground=INK, selectcolor=FIELD, font=UI_FONT,
                          highlightthickness=2, highlightbackground=FACE,
                          highlightcolor=NAVY, bd=0, anchor="w", cursor="hand2")


def radio98(parent: tk.Misc, text: str, variable: tk.Variable, value: str,
            command=None) -> tk.Radiobutton:
    """A classic round-indicator radio button."""
    return tk.Radiobutton(parent, text=text, variable=variable, value=value,
                          command=command, bg=FACE, fg=INK, activebackground=FACE,
                          activeforeground=INK, selectcolor=FIELD, font=UI_FONT,
                          highlightthickness=2, highlightbackground=FACE,
                          highlightcolor=NAVY, bd=0, anchor="w", cursor="hand2")


class Dropdown98(tk.Menubutton):
    """The picker behind :func:`option98`: a bevelled Win98 menubutton that opens
    a combobox-style popup (sunken white listbox, navy selection) instead of a
    native menu. Native menus ignore the window they belong to — on a small
    window the 45-entry source list sprawled way outside the app — so this popup
    is measured and clamped to fit *inside* the app window: it scrolls when the
    list is taller than the space and flips to open upward when there's more
    room above the button than below."""

    _MIN_ROWS = 3   # flip upward rather than show fewer rows than this

    def __init__(self, parent: tk.Misc, variable: tk.Variable, values,
                 width: int | None = None) -> None:
        super().__init__(parent, textvariable=variable, relief="raised", bd=2,
                         bg=FACE, fg=INK, activebackground=FACE,
                         activeforeground=INK, highlightthickness=2,
                         highlightbackground=FACE, highlightcolor=NAVY,
                         font=UI_FONT, anchor="w", indicatoron=True, padx=6,
                         pady=1, cursor="hand2", disabledforeground=GRAYTEXT)
        self._var = variable
        self._values = [str(v) for v in values] or [""]
        self._popup: tk.Toplevel | None = None
        self._unpost_id: str | None = None   # root <Configure> binding while open
        if width:
            self.configure(width=width)
        self.bind("<ButtonPress-1>", self._press)
        # Mouse-only before this: a Menubutton takes keyboard focus (Tab
        # reaches it) but had no key that could ever open it — a hard
        # keyboard blocker for the two controls (Sector's region picker,
        # Format) that have no other way to change their value.
        self.bind("<Return>", self._press)
        self.bind("<space>", self._press)
        self.bind("<Down>", self._press)
        self.bind("<Destroy>", lambda _e: self._close())

    def _press(self, _e):
        if str(self["state"]) != "disabled":
            if self._popup is not None:
                self._close()
            else:
                self._open()
        return "break"   # we have no tk menu; suppress the Menubutton post logic

    def _open(self) -> None:
        win = self.winfo_toplevel()
        self.update_idletasks()
        bx, by = self.winfo_rootx(), self.winfo_rooty()
        bw, bh = self.winfo_width(), self.winfo_height()
        wx, wy = win.winfo_rootx(), win.winfo_rooty()
        ww, wh = win.winfo_width(), win.winfo_height()

        # How many rows fit between the button and the window's edges (8px for
        # the popup border + breathing room)? Prefer dropping down; flip up when
        # down is cramped and up is roomier.
        n = len(self._values)
        line_h = max(1, tkfont.Font(font=UI_FONT).metrics("linespace"))
        below = max(0, (wy + wh - (by + bh) - 8) // line_h)
        above = max(0, (by - wy - 8) // line_h)
        drop_up = below < min(n, self._MIN_ROWS) and above > below
        rows = max(1, min(n, above if drop_up else below))

        top = tk.Toplevel(self, bg=DARKEDGE)   # 1px dark frame around the list
        top.withdraw()
        top.overrideredirect(True)
        lb = tk.Listbox(top, height=rows, width=0, bg=FIELD, fg=INK, font=UI_FONT,
                        selectbackground=NAVY, selectforeground=NAVY_TXT,
                        activestyle="none", relief="flat", bd=0,
                        highlightthickness=0, exportselection=False)
        for v in self._values:
            lb.insert("end", v)
        sb: tk.Scrollbar | None = None

        def _add_scrollbar():
            nonlocal sb
            if sb is None:
                sb = tk.Scrollbar(top, orient="vertical", command=lb.yview)
                lb.configure(yscrollcommand=sb.set)
                sb.pack(side="right", fill="y", padx=(0, 1), pady=1)

        if rows < n:   # clipped -> scrollbar
            _add_scrollbar()
        lb.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        cur = self._var.get()
        if cur in self._values:
            i = self._values.index(cur)
            lb.selection_set(i)
            lb.activate(i)
            lb.see(i)

        # The row estimate used the bare font linespace, but real listbox rows
        # carry a few extra pixels each — measure the built popup and trim rows
        # until it genuinely fits the space it's dropping into.
        top.update_idletasks()
        avail = (by - wy - 2) if drop_up else (wy + wh - (by + bh) - 2)
        over = top.winfo_reqheight() - avail
        if over > 0 and rows > 1:
            per_row = max(1, -(-lb.winfo_reqheight() // rows))   # ceil px/row
            rows = max(1, rows - -(-over // per_row))
            lb.configure(height=rows)
            if rows < n:
                _add_scrollbar()
            top.update_idletasks()

        # Size and place it: at least as wide as the button, never wider than
        # the window, right edge pulled in so it can't stick out of the app.
        w = min(max(bw, top.winfo_reqwidth()), ww)
        h = top.winfo_reqheight()
        x = max(wx, min(bx, wx + ww - w))
        x = max(0, min(x, self.winfo_screenwidth() - w))    # and stay on screen
        y = (by - h) if drop_up else (by + bh)
        y = max(0, min(y, self.winfo_screenheight() - h))
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.deiconify()
        try:   # render above the app window even if that window is topmost
            top.attributes("-topmost", True)
            top.lift()
        except tk.TclError:
            pass

        def _choose(_e=None):
            sel = lb.curselection()
            if sel:
                self._var.set(self._values[sel[0]])
            self._close()

        def _outside(e):
            # The grab routes every click in the app here; only clicks that land
            # inside the popup itself are not a dismissal.
            target = top.winfo_containing(e.x_root, e.y_root)
            if target is None or target.winfo_toplevel() is not top:
                self._close()

        lb.bind("<ButtonRelease-1>", _choose)
        lb.bind("<Return>", _choose)
        top.bind("<Escape>", lambda _e: self._close())
        top.bind("<ButtonPress-1>", _outside)

        # Moving/resizing the window would leave the popup floating: close. But
        # only on a REAL move — Windows also delivers spurious/late <Configure>
        # events (focus flips, a resize that landed just before we opened), so
        # compare against the geometry the popup was placed for.
        placed_at = (wx, wy, ww, wh)

        def _win_moved(_e):
            w_ = self.winfo_toplevel()
            now = (w_.winfo_rootx(), w_.winfo_rooty(),
                   w_.winfo_width(), w_.winfo_height())
            if now != placed_at:
                self._close()

        self._unpost_id = win.bind("<Configure>", _win_moved, add=True)

        self._popup = top
        lb.focus_set()
        try:
            top.grab_set()
        except tk.TclError:   # not viewable yet on a slow machine: one retry
            top.after(30, lambda: self._popup is top and top.grab_set())

    def _close(self) -> None:
        top, self._popup = self._popup, None
        if top is None:
            return
        if self._unpost_id is not None:
            try:
                self.winfo_toplevel().unbind("<Configure>", self._unpost_id)
            except tk.TclError:
                pass
            self._unpost_id = None
        try:
            top.grab_release()
            top.destroy()
        except tk.TclError:
            pass


def option98(parent: tk.Misc, variable: tk.Variable, values,
             width: int | None = None) -> Dropdown98:
    """A dropdown that reads as a raised Win98 menubutton with a ``▼`` indicator.

    Win98 had no flat combobox; a bevelled menubutton is the period-correct cue.
    The popup is a listbox clamped to the app window (see :class:`Dropdown98`),
    so it never sticks out of a small window the way a native menu does."""
    return Dropdown98(parent, variable, values, width=width)


def tooltip98(widget: tk.Widget, text: str, *, delay: int = 550) -> None:
    """Attach a classic Win98 tooltip: the pale-yellow box with a 1px border
    that appears under the pointer after a short hover. Purely additive —
    binds with ``add=True`` so it never clobbers a widget's own handlers, and
    every failure path just means 'no tip', never an error."""
    state: dict = {"job": None, "tip": None}

    def _cancel() -> None:
        if state["job"] is not None:
            try:
                widget.after_cancel(state["job"])
            except tk.TclError:
                pass
            state["job"] = None

    def _hide(_e=None) -> None:
        _cancel()
        tip, state["tip"] = state["tip"], None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass

    def _show() -> None:
        state["job"] = None
        if state["tip"] is not None:
            return
        try:
            tip = tk.Toplevel(widget)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tk.Label(tip, text=text, bg=TIP_BG, fg=INK, font=UI_FONT,
                     justify="left", relief="solid", bd=1, padx=5, pady=2,
                     wraplength=280).pack()
            tip.update_idletasks()
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            x = max(0, min(x, tip.winfo_screenwidth() - tip.winfo_reqwidth()))
            y = max(0, min(y, tip.winfo_screenheight() - tip.winfo_reqheight()))
            tip.geometry(f"+{x}+{y}")
            state["tip"] = tip
        except tk.TclError:
            state["tip"] = None

    def _enter(_e=None) -> None:
        _cancel()
        state["job"] = widget.after(delay, _show)

    widget.bind("<Enter>", _enter, add=True)
    widget.bind("<Leave>", _hide, add=True)
    widget.bind("<ButtonPress>", _hide, add=True)
    widget.bind("<Destroy>", _hide, add=True)
    # Mouse-only before this: a keyboard user tabbing through the form never
    # saw a tooltip at all, since hover was the only trigger. FocusIn/FocusOut
    # reuse the exact same _show/_hide (they only read the widget's current
    # screen position, which is unaffected by how focus/hover was reached).
    widget.bind("<FocusIn>", _enter, add=True)
    widget.bind("<FocusOut>", _hide, add=True)


def menu98(parent: tk.Misc, **kw) -> tk.Menu:
    """A drop-down menu styled like Win98: silver face, navy highlight bar."""
    return tk.Menu(parent, bg=FACE, fg=INK, activebackground=NAVY,
                   activeforeground=NAVY_TXT, activeborderwidth=1,
                   font=UI_FONT, bd=1, relief="raised", tearoff=0, **kw)


_SUPPRESSED_ROOT_KEYS = ("<F5>", "<Return>", "<Escape>")


def dialog98(root: tk.Misc, title: str, caption: str) -> tuple[tk.Toplevel, tk.Frame]:
    """A silver Win98 dialog shell: OS window + our gradient caption strip.

    Returns ``(window, body)`` — put content in ``body`` and call
    ``window.deiconify()`` sizing yourself if needed. The dialog is transient
    (stays on top of the main window) but does NOT grab, so it can never wedge
    the app or the headless smoke test.

    Because it doesn't grab, the main window's own F5/Enter/Escape shortcuts
    (Run/Run/Cancel) would otherwise ALSO fire while this dialog is open —
    e.g. Escape closing an About box could simultaneously cancel a run behind
    it. Those three root bindings are suspended for as long as this dialog
    exists and restored (verbatim, via the exact Tcl command string Tk had
    bound) the instant it's destroyed, by any path: Escape (bound here to a
    plain destroy), a dialog's own OK/Close button, or the OS close button.
    A caller whose close needs to do more than destroy (persisting a 'seen'
    flag, say) should re-bind <Escape> to its own handler after this returns —
    the same override-friendly shape already used for WM_DELETE_WINDOW."""
    win = tk.Toplevel(root, bg=FACE)
    win.title(title)
    win.transient(root)
    win.resizable(False, False)

    top_root = root.winfo_toplevel()
    _saved_root_binds = {seq: top_root.bind(seq) for seq in _SUPPRESSED_ROOT_KEYS}
    for _seq in _SUPPRESSED_ROOT_KEYS:
        top_root.unbind(_seq)

    def _restore_root_binds(_e=None) -> None:
        for seq, cmd in _saved_root_binds.items():
            if cmd:
                top_root.tk.call("bind", top_root, seq, cmd)

    win.bind("<Destroy>", _restore_root_binds, add=True)
    win.bind("<Escape>", lambda _e: win.destroy())

    outer = tk.Frame(win, bg=FACE, bd=2, relief="raised")
    outer.pack(fill="both", expand=True)
    TitleBar(outer, caption, height=22).pack(fill="x", padx=2, pady=2)
    body = tk.Frame(outer, bg=FACE)
    body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
    return win, body


def center_over(win: tk.Toplevel, root: tk.Misc) -> None:
    """Place a dialog centred over the main window (clamped to the screen)."""
    win.update_idletasks()
    try:
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        x = max(0, min(x, win.winfo_screenwidth() - win.winfo_width()))
        y = max(0, min(y, win.winfo_screenheight() - win.winfo_height()))
        win.geometry(f"+{x}+{y}")
    except tk.TclError:  # pragma: no cover - placement is cosmetic only
        pass
