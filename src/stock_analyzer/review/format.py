"""Honest number/percent formatters and the single column spec for the grid.

Missing values render as ``"n/a"`` (rich) or ``""`` (lean) — never invented. The
COLUMNS table is the ONE source of truth for both output formats, so a header can
never drift out of sync with the cell beneath it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


NA = "n/a"


def _num(x) -> float | None:
    """Return a finite float, or None if the value is missing/garbage (NaN/inf)."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def money(x) -> str:
    """Human-readable big USD figure: $3.21T, $845.2B, $12.3B, $950M."""
    f = _num(x)
    if f is None:
        return NA
    a = abs(f)
    if a >= 1e12:
        return f"${f/1e12:.2f}T"
    if a >= 1e9:
        return f"${f/1e9:.1f}B"
    if a >= 1e6:
        return f"${f/1e6:.1f}M"
    return f"${f:,.0f}"


def price(x) -> str:
    """Plain amount in the row's listing currency (the Curr column says which)."""
    f = _num(x)
    return NA if f is None else f"{f:,.2f}"


def ratio(x, dp: int = 1) -> str:
    f = _num(x)
    return NA if f is None else f"{f:.{dp}f}"


def pct(x, *, already_percent: bool = False, dp: int = 1, sign: bool = False) -> str:
    """Format a percentage. Pass already_percent=True if x is e.g. 12.3 not 0.123."""
    f = _num(x)
    if f is None:
        return NA
    if not already_percent:
        f *= 100.0
    s = f"{f:+.{dp}f}%" if sign else f"{f:.{dp}f}%"
    return s


def _txt(s) -> str:
    """Plain text cell: blank for missing values (None or the 'n/a' sentinel)."""
    return "" if (s is None or s == NA) else str(s)


def _f(x, dp: int = 1) -> str:
    """Bare fixed-point number, or '' if missing. No units, no thousands sep."""
    v = _num(x)
    return "" if v is None else f"{v:.{dp}f}"


def _fpct(x, dp: int = 1) -> str:
    """Fraction -> bare percentage number (0.166 -> '16.6'), or '' if missing."""
    v = _num(x)
    return "" if v is None else f"{v * 100:.{dp}f}"


def _fbn(x) -> str:
    """USD market cap -> bare number of billions, precision scaled to magnitude."""
    v = _num(x)
    if v is None:
        return ""
    b = v / 1e9
    if b >= 1000:
        return f"{b:.0f}"
    if b >= 10:
        return f"{b:.1f}"
    return f"{b:.2f}"


def _fprice(x) -> str:
    """Lean price cell: the 2-dp amount with trailing zeros trimmed (no data loss).

    Formatting every price at exactly two decimals leaves a wasted ``.00`` on every
    row of a high-nominal market (JPY, KRW): ``3306.00``, ``82320.00``. This renders
    the SAME rounded value with trailing zeros — and a bare trailing dot — stripped:
    ``3306.00`` -> ``3306``, ``12.50`` -> ``12.5``, ``0.32`` stays ``0.32``. Because
    it only ever removes trailing zeros, the value can never change (the ``.`` guards
    the integer part, so ``100.00`` -> ``100``, never ``1``).
    """
    v = _num(x)
    if v is None:
        return ""
    return f"{v:.2f}".rstrip("0").rstrip(".")


SECTOR_CODE = {
    "technology": "Tech", "communication services": "CommSvc",
    "consumer cyclical": "ConsCyc", "consumer defensive": "ConsDef",
    "energy": "Enrgy", "financial services": "Fin", "healthcare": "Hlth",
    "industrials": "Indu", "basic materials": "Matl", "real estate": "RE",
    "utilities": "Util",
}


ANALYST_CODE = {
    "strong buy": "SB", "buy": "B", "outperform": "O", "hold": "H",
    "underperform": "U", "sell": "S", "strong sell": "SS",
}


def _sector_code(s) -> str:
    """Long GICS sector -> short code for the lean table; unknown -> raw, missing -> ''."""
    if not s or s == NA:
        return ""
    return SECTOR_CODE.get(str(s).strip().lower(), str(s))


def _rec_code(s) -> str:
    """Analyst recommendation -> short code; 'none'/missing -> '' (no rating)."""
    if not s or s == NA:
        return ""
    key = str(s).strip().lower()
    if key == "none":
        return ""
    return ANALYST_CODE.get(key, str(s))


def size_bucket(usd_market_cap) -> str:
    """Classify by USD market cap so small caps are visible at a glance."""
    f = _num(usd_market_cap)
    if f is None:
        return NA
    if f >= 200e9:
        return "Mega"
    if f >= 10e9:
        return "Large"
    if f >= 2e9:
        return "Mid"
    if f >= 300e6:
        return "Small"
    return "Micro"


SIZE_ORDER = ["Mega", "Large", "Mid", "Small", "Micro", NA]


@dataclass(frozen=True)
class ColSpec:
    """One column of the stock grid.

    ``lean``/``rich`` are the column's header in each format; ``None`` means the
    column is absent from that format (e.g. Size is rich-only — lean derives it
    from MktCapB). ``lean_fmt``/``rich_fmt`` turn a record into that format's cell
    (the lean family renders missing as ``""``; the rich family as ``"n/a"``).
    ``derivable`` flags a column an AI reader can reconstruct from the others
    (e.g. Target = Price × (1 + Upside)); a later compaction pass consumes it.
    """
    key: str
    lean: str | None
    rich: str | None
    lean_fmt: Callable[[dict], str]
    rich_fmt: Callable[[dict], str]
    derivable: bool = False

    def header(self, lean: bool) -> str:
        return self.lean if lean else self.rich

    def render(self, r: dict, lean: bool) -> str:
        return self.lean_fmt(r) if lean else self.rich_fmt(r)


COLUMNS: list[ColSpec] = [
    ColSpec("ticker", "Ticker", "Ticker",
            lambda r: r["ticker"], lambda r: r["ticker"]),
    ColSpec("name", "Name", "Name",
            lambda r: r["name"], lambda r: r["name"]),
    ColSpec("sector", "Sector", "Sector",
            lambda r: _sector_code(r.get("sector")), lambda r: r.get("sector", NA)),
    ColSpec("size", None, "Size",  # rich-only; lean drops it (derivable from MktCapB)
            None, lambda r: r.get("size", NA)),
    ColSpec("currency", "Curr", "Curr",
            lambda r: _txt(r.get("currency")), lambda r: r.get("currency", NA)),
    ColSpec("price", "Price", "Price",
            lambda r: _fprice(r.get("price")), lambda r: price(r.get("price"))),
    ColSpec("usd_market_cap", "MktCapB", "Mkt cap",
            lambda r: _fbn(r.get("usd_market_cap")), lambda r: money(r.get("usd_market_cap"))),
    ColSpec("pe", "PE", "P/E",
            lambda r: _f(r.get("pe")), lambda r: ratio(r.get("pe"))),
    ColSpec("fwd_pe", "FwdPE", "Fwd P/E",
            lambda r: _f(r.get("fwd_pe")), lambda r: ratio(r.get("fwd_pe"))),
    ColSpec("peg", "PEG", "PEG",
            lambda r: _f(r.get("peg"), 2), lambda r: ratio(r.get("peg"), 2)),
    ColSpec("pb", "PB", "P/B",
            lambda r: _f(r.get("pb")), lambda r: ratio(r.get("pb"))),
    ColSpec("ps", "PS", "P/S",
            lambda r: _f(r.get("ps")), lambda r: ratio(r.get("ps"))),
    ColSpec("roe", "ROE", "ROE",
            lambda r: _fpct(r.get("roe")), lambda r: pct(r.get("roe"))),
    ColSpec("margin", "Margin", "Margin",
            lambda r: _fpct(r.get("margin")), lambda r: pct(r.get("margin"))),
    ColSpec("rev_growth", "RevGr", "Rev gr",
            lambda r: _fpct(r.get("rev_growth")), lambda r: pct(r.get("rev_growth"), sign=True)),
    ColSpec("debt_equity", "DE", "D/E",
            lambda r: _f(r.get("debt_equity"), 0), lambda r: ratio(r.get("debt_equity"), 0)),
    ColSpec("div_yield", "Div", "Div",
            lambda r: _fpct(r.get("div_yield")), lambda r: pct(r.get("div_yield"))),
    ColSpec("ret_1m", "1m", "1m",
            lambda r: _fpct(r.get("ret_1m")), lambda r: pct(r.get("ret_1m"), sign=True)),
    ColSpec("ret_3m", "3m", "3m",
            lambda r: _fpct(r.get("ret_3m")), lambda r: pct(r.get("ret_3m"), sign=True)),
    ColSpec("ret_6m", "6m", "6m",
            lambda r: _fpct(r.get("ret_6m")), lambda r: pct(r.get("ret_6m"), sign=True)),
    ColSpec("ret_1y", "1y", "1y",
            lambda r: _fpct(r.get("ret_1y")), lambda r: pct(r.get("ret_1y"), sign=True)),
    ColSpec("vs_ma50", "vs50d", "vs 50d",
            lambda r: _fpct(r.get("vs_ma50")), lambda r: pct(r.get("vs_ma50"), sign=True)),
    ColSpec("vs_ma200", "vs200d", "vs 200d",
            lambda r: _fpct(r.get("vs_ma200")), lambda r: pct(r.get("vs_ma200"), sign=True)),
    ColSpec("from_hi", "Hi", "From 52w hi",
            lambda r: _fpct(r.get("from_hi")), lambda r: pct(r.get("from_hi"), sign=True)),
    ColSpec("from_lo", "Lo", "From 52w lo",
            lambda r: _fpct(r.get("from_lo")), lambda r: pct(r.get("from_lo"), sign=True)),
    ColSpec("beta", "Beta", "Beta",
            lambda r: _f(r.get("beta"), 2), lambda r: ratio(r.get("beta"), 2)),
    ColSpec("rec", "Analyst", "Analyst",
            lambda r: _rec_code(r.get("rec")), lambda r: r.get("rec", NA)),
    ColSpec("n_analysts", "nAn", "# An.",
            lambda r: _f(r.get("n_analysts"), 0), lambda r: ratio(r.get("n_analysts"), 0)),
    ColSpec("target", "Target", "Target",
            lambda r: _fprice(r.get("target")), lambda r: price(r.get("target")),
            derivable=True),  # Target = Price × (1 + Upside)
    ColSpec("upside", "Upside", "Target ↑",
            lambda r: _fpct(r.get("upside")), lambda r: pct(r.get("upside"), sign=True)),
]


def _active_columns(*, lean: bool, drop_names: bool) -> list[ColSpec]:
    """Columns present for this format, in order. A column absent from the format
    (header ``None``) is skipped; in the lean table ``--no-names`` also drops the
    Name column (the Ticker still identifies each row). Rich always keeps Name."""
    cols = []
    for c in COLUMNS:
        if c.header(lean) is None:
            continue
        if lean and drop_names and c.key == "name":
            continue
        cols.append(c)
    return cols


def _target_reconstructable(ok: list[dict]) -> bool:
    """True if every row that HAS a Target also carries the Price and Upside needed
    to rebuild it (Target = Price × (1 + Upside)). Only then is dropping Target
    lossless; a lone Target with no price/upside stays, so nothing is invented."""
    for r in ok:
        if _num(r.get("target")) is None:
            continue
        if _num(r.get("price")) is None or _num(r.get("upside")) is None:
            return False
    return True


def _compact_columns(cols: list[ColSpec],
                     ok: list[dict]) -> tuple[list[ColSpec], list[tuple[ColSpec, str]]]:
    """Lean-only, data-driven column dropping that loses NOTHING for this set:
      * "currency" — Curr when the whole set is one currency (stated once in a note),
      * "blank"    — any column empty for every row (a blank already reads as N/A),
      * "derivable"— the data-bearing Target (reconstructable from Price + Upside).
    Returns (kept, dropped) where dropped is a list of (column, reason) so the note
    can explain each honestly. No-op on an empty set — there's nothing to infer."""
    if not ok:
        return cols, []
    currencies = {r["currency"] for r in ok if r.get("currency")}
    kept: list[ColSpec] = []
    dropped: list[tuple[ColSpec, str]] = []
    for c in cols:
        blank = all(c.lean_fmt(r) == "" for r in ok)
        if c.key == "currency" and len(currencies) <= 1:
            reason = "currency"                                             # 2a
        elif blank:
            reason = "blank"                                               # 2b
        elif c.derivable and (c.key != "target" or _target_reconstructable(ok)):
            reason = "derivable"                                           # 2c (has data)
        else:
            reason = ""
        if reason:
            dropped.append((c, reason))
        else:
            kept.append(c)
    return kept, dropped


def _compaction_note(dropped: list[tuple[ColSpec, str]], ok: list[dict]) -> str:
    """One honest line naming the columns dropped for this set and how to read the
    data they encoded — so the compaction is self-documenting, never silent."""
    if not dropped:
        return ""
    parts: list[str] = []
    if any(reason == "currency" for _, reason in dropped):
        curr = sorted({r["currency"] for r in ok if r.get("currency")})
        if len(curr) == 1:
            parts.append(f"**Curr** (all prices/targets in **{curr[0]}**)")
    if any(reason == "derivable" for _, reason in dropped):  # only a data-bearing Target
        parts.append("**Target** (reconstruct as Price × (1 + Upside/100))")
    blanks = [c.lean for c, reason in dropped if reason == "blank"]
    if blanks:
        parts.append(f"**empty here**: {', '.join(blanks)}")
    if not parts:
        return ""
    return "_Columns omitted for this set (no data lost) — " + "; ".join(parts) + "._"
