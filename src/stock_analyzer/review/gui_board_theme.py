"""Design tokens for the Live board: two themes, contrast maths, font choice.

Nothing here imports Tk, so ``tests/test_gui_board.py`` can check every
text/background pair against WCAG without opening a window. The rules the
tokens encode (see DESIGN.md "Live board"):

* one accent (the teal of the app icon, tuned per theme) for actions, focus and
  selection;
* status colours (``ok`` / ``warn`` / ``bad``) are used for status and for the
  sign of a return, never as decoration, and always next to a word or a shape so
  nothing depends on hue alone;
* ``faint`` is for hairlines and decoration only. It does not clear 4.5:1 and
  must never carry text.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    # surfaces, darkest role first
    bg: str          # window
    surface: str     # sidebar, header, table, drawer
    raised: str      # status band, table header, chips
    hover: str       # row / item under the pointer
    field: str       # text inputs
    line: str        # hairlines
    line_strong: str  # button and chip borders (the label identifies them)
    field_line: str  # input borders: the only edge of a field, so 3:1
    # text
    ink: str
    muted: str
    faint: str       # decoration only, never text
    # accent
    accent: str      # primary button face, focus ring, progress fill
    accent_hover: str
    accent_ink: str  # text on the accent face
    accent_text: str  # accent-coloured text on a surface (links, sort arrow)
    sel: str         # selected row / nav item background
    sel_ink: str
    # status
    ok: str
    warn: str
    bad: str
    ok_bg: str
    warn_bg: str
    bad_bg: str
    track: str       # empty part of the progress bar


DARK = Theme(
    name="dark",
    bg="#0e1114", surface="#151a1f", raised="#1b2128", hover="#202730",
    field="#0f1317", line="#262d36", line_strong="#3a4552", field_line="#66727f",
    ink="#e6eaef", muted="#9ca7b4", faint="#56616e",
    accent="#35b8c6", accent_hover="#5fcbd6", accent_ink="#041c20",
    accent_text="#5fcbd6", sel="#143338", sel_ink="#b5ecf2",
    ok="#6cc777", warn="#e2ad52", bad="#f2858e",
    ok_bg="#14261a", warn_bg="#2a2313", bad_bg="#2e191c",
    track="#2a323c",
)

LIGHT = Theme(
    name="light",
    bg="#f1f3f1", surface="#ffffff", raised="#f6f8f6", hover="#edf1ee",
    field="#ffffff", line="#dde2de", line_strong="#b4bcb7", field_line="#858e89",
    ink="#161b1e", muted="#566067", faint="#a5adb1",
    accent="#0b7480", accent_hover="#095f69", accent_ink="#ffffff",
    accent_text="#0a6772", sel="#dcf0f2", sel_ink="#07505a",
    ok="#1c7a3e", warn="#855600", bad="#b3261e",
    ok_bg="#e6f4ea", warn_bg="#fbf1d8", bad_bg="#fbe9e7",
    track="#dfe4e0",
)

THEMES = {"dark": DARK, "light": LIGHT}
THEME_CHOICES = ("auto", "dark", "light")

# Every (text, background) pair the window actually draws. The test suite walks
# this list for both themes; add a pair here when a new combination appears.
TEXT_PAIRS = (
    ("ink", "bg"), ("ink", "surface"), ("ink", "raised"), ("ink", "hover"),
    ("ink", "field"), ("ink", "sel"),
    ("muted", "bg"), ("muted", "surface"), ("muted", "raised"), ("muted", "hover"),
    ("muted", "field"),
    ("accent_ink", "accent"), ("accent_ink", "accent_hover"),
    ("accent_text", "bg"), ("accent_text", "surface"), ("accent_text", "raised"),
    ("sel_ink", "sel"),
    ("ok", "bg"), ("ok", "surface"), ("ok", "raised"), ("ok", "hover"),
    ("ok", "sel"), ("ok", "ok_bg"),
    ("warn", "bg"), ("warn", "surface"), ("warn", "raised"), ("warn", "hover"),
    ("warn", "sel"), ("warn", "warn_bg"),
    ("bad", "bg"), ("bad", "surface"), ("bad", "raised"), ("bad", "hover"),
    ("bad", "sel"), ("bad", "bad_bg"),
)
# Non-text marks that must still be findable (WCAG 1.4.11, 3:1).
INDICATOR_PAIRS = (
    ("accent", "bg"), ("accent", "surface"), ("accent", "raised"), ("accent", "track"),
    ("warn", "track"), ("field_line", "field"), ("field_line", "surface"),
    ("field_line", "bg"),
)


def _channel(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    """WCAG relative luminance of ``#rrggbb``."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours (1.0 to 21.0)."""
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """Colour ``t`` of the way from ``a`` to ``b`` (0 = a, 1 = b)."""
    t = max(0.0, min(1.0, float(t)))
    pa, pb = a.lstrip("#"), b.lstrip("#")
    ch = [round(int(pa[i:i + 2], 16) * (1 - t) + int(pb[i:i + 2], 16) * t)
          for i in (0, 2, 4)]
    return "#{:02x}{:02x}{:02x}".format(*ch)


HEAT_CAP = 0.20      # a 20 % move (either way) is the deepest shade


def heat(theme: "Theme", value, cap: float = HEAT_CAP) -> str:
    """Mosaic fill for a 1-month return: green above zero, red below, deeper
    with size. ``None`` (no return known) is a neutral grey, never the colour of
    a zero return."""
    if value is None:
        return theme.line_strong
    k = 0.28 + 0.72 * min(1.0, abs(float(value)) / cap)
    return mix(theme.raised, theme.ok if value >= 0 else theme.bad, k)


def detect_system_theme() -> str:
    """'light' or 'dark' from the Windows "app mode" setting; 'dark' anywhere
    the question cannot be answered with the standard library."""
    if not sys.platform.startswith("win"):
        return "dark"
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if int(value) else "dark"
    except Exception:  # noqa: BLE001 - older Windows or a locked-down registry
        return "dark"


def resolve_theme(choice, system: str | None = None) -> Theme:
    """Map a stored preference ('auto' | 'dark' | 'light', or junk) to a Theme."""
    choice = str(choice or "auto").strip().lower()
    if choice not in THEMES:
        choice = system if system in THEMES else detect_system_theme()
    return THEMES[choice]


UI_FONTS = ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue",
            "Inter", "DejaVu Sans", "Liberation Sans", "Helvetica")
MONO_FONTS = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "JetBrains Mono",
              "DejaVu Sans Mono", "Liberation Mono", "Courier New")


def pick_font(available, candidates, fallback: str) -> str:
    """First candidate family that is installed. Family names are compared
    case-insensitively because X11 reports them lower-cased."""
    have = {str(a).lower(): str(a) for a in available}
    for name in candidates:
        hit = have.get(name.lower())
        if hit:
            return hit
    return fallback
