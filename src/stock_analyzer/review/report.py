"""Serialise a completed run into the single clean Markdown file — the deliverable
handed to an AI. Lean (default) = token-light CSV grid; --rich = verbose."""
from __future__ import annotations

import csv
import io
import json
import statistics
from collections import Counter

from .format import (NA, SIZE_ORDER, _active_columns, _compact_columns,
                     _compaction_note, _num)


def _sorted_ok(rows: list[dict]) -> list[dict]:
    """Successfully-fetched rows, ordered mega -> micro by USD market cap."""
    ok = [r for r in rows if not r.get("error")]
    ok.sort(key=lambda r: (_num(r.get("usd_market_cap")) or -1), reverse=True)
    return ok


# Columns whose rendered cell is free-form text (a company/sector/currency
# name, an analyst-rating word, ...) rather than a formatted number. Only
# these get the CSV-injection guard below — a numeric cell legitimately
# starts with '-' for a negative value, and must never be touched.
_TEXT_COLUMN_KEYS = frozenset({"ticker", "name", "sector", "size", "currency", "rec"})


def _csv_safe_text(s: str) -> str:
    """Apostrophe-prefix a text cell that starts with =, +, -, or @ — the
    classic CSV-formula-injection trigger characters Excel/Sheets treat as
    'this cell is a formula'. A leading apostrophe forces plain-text import
    without changing the visible value (standard CSV-injection mitigation)."""
    return "'" + s if s and s[0] in ("=", "+", "-", "@") else s


def _grid_csv(ok: list[dict], *, lean: bool,
              drop_names: bool) -> tuple[str, list]:
    """Render the stock grid to CSV text. Returns (csv_text, dropped_columns) where
    dropped_columns feeds the compaction note. Header and cells come from the SAME
    column spec (see COLUMNS), so a header can never drift from the cell beneath it."""
    cols = _active_columns(lean=lean, drop_names=drop_names)
    dropped: list = []
    if lean:
        cols, dropped = _compact_columns(cols, ok)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c.header(lean) for c in cols])
    for r in ok:
        w.writerow([_csv_safe_text(c.render(r, lean)) if c.key in _TEXT_COLUMN_KEYS
                   else c.render(r, lean) for c in cols])
    return buf.getvalue().rstrip("\n"), dropped


def stocks_csv(rows: list[dict], *, lean: bool = True,
               drop_names: bool = False) -> str:
    """The stock grid as raw CSV (header + one row per stock) — no Markdown wrapper
    or prose, so it's directly pipeable. Same columns/compaction as the report grid."""
    grid, _ = _grid_csv(_sorted_ok(rows), lean=lean, drop_names=drop_names)
    return grid


def stocks_json(rows: list[dict], failures: list[dict], run_day: str, *,
                lean: bool = True, drop_names: bool = False) -> str:
    """The run as JSON: the sorted stock records plus the failures. For power users
    who want the raw numbers rather than the Markdown/CSV grid.

    Honours the grid's column switches, as --format's help promises: lean (the
    default) restricts each record to the lean grid's columns — raw values, not
    formatted strings — and drop_names removes the company name; --rich
    (lean=False) emits the full untrimmed records."""
    ok = _sorted_ok(rows)
    if lean:
        keys = [c.key for c in _active_columns(lean=True, drop_names=drop_names)]
        stocks = [{k: r.get(k) for k in keys} for r in ok]
    else:
        stocks = ok
    payload = {
        "run_day": run_day,
        "stocks": stocks,
        "failures": [{"ticker": f.get("ticker"), "error": f.get("error")}
                     for f in failures],
    }
    return json.dumps(payload, indent=2, default=str)


REVIEW_INSTRUCTIONS = """\
You are reviewing a watchlist of stocks from the data below. Work **only** from
these numbers — do not pull in outside prices or invent figures. Please:

1. **Name the 3–5 strongest and 3–5 weakest** picks, and say *why* in terms of the
   columns shown (valuation, quality/profitability, momentum, analyst view). These
   are **raw, unranked, non-sector-normalized** values, not a composite score — a
   miner's D/E and a bank's D/E aren't a fair comparison on raw terms, so weigh a
   metric against peers in the **same sector/industry** column, not the whole list.
2. **Flag likely data problems** — anything that looks impossible or internally
   inconsistent (e.g. a P/E with a negative margin, price far above the 52-week
   high, a ratio that looks mis-scaled). Call these out rather than trusting them.
3. **Group by theme** where useful (cheap-but-slow, expensive-but-growing,
   momentum leaders rolling over, etc.).
4. Keep it grounded: a clean ranking here is a *screen*, **not** a price forecast
   or financial advice. Note what you'd want to check before acting.

**What this is, and isn't:** this is raw sourced data with no score, no rank, and
no backtest behind it — a different, separate tool from this project's scored
daily pipeline (`run_daily.py`), which weighs and ranks each stock and has a
backtest harness measuring whether that ranking has ever meant anything. Nothing
here has been checked against forward returns.

Fields marked **n/a** were unavailable from the data source and were not guessed.
For international names, **Price and Target are in the listing currency** (see the
Curr column; `GBp` = British pence), while **Mkt cap is normalised to USD** so the
size ranking is comparable across markets.
"""


LEGEND = """\
**Column legend** — Size: USD market-cap bucket (Mega ≥$200B · Large ≥$10B · Mid
≥$2B · Small ≥$300M · Micro <$300M) · Curr: listing currency of Price/Target ·
Mkt cap: market cap in USD · P/E: trailing price/earnings · Fwd P/E: forward P/E ·
PEG: P/E-to-growth · P/B: price/book · P/S: price/sales · ROE: return on equity ·
Margin: net profit margin · Rev gr: trailing revenue growth · D/E: debt-to-equity
(yfinance scale, ~100 ≈ 1.0x) · Div: dividend yield · 1m/3m/1y: trailing price
return · vs 200d: price vs 200-day moving average · 52w: from 52-week high/low ·
Target ↑: mean analyst target vs current price.
"""


LEAN_INSTRUCTIONS = """\
Review these stocks using ONLY the numbers below (no outside data or invented figures):
1. Name the 3-5 strongest and 3-5 weakest, with reasons (valuation, quality, momentum, analyst).
   Raw, unranked, non-sector-normalized values, not a composite score - compare within the
   same sector/industry, not straight across the whole list (e.g. a miner's D/E vs a bank's
   D/E isn't a fair raw comparison).
2. Flag likely data errors (e.g. a P/E with a negative margin, price above the 52-week high, a mis-scaled ratio).
3. Group by theme where useful (cheap-but-slow, expensive-but-growing, momentum rolling over, etc.).
4. This is a research screen, not a forecast or financial advice.
Raw sourced data only - no score, no rank, no backtest (a separate tool from this
project's scored daily pipeline, run_daily.py, which has both).
A **blank cell = unavailable** (not zero, not guessed). Prices are in each row's listing currency (see Curr, or the note above the table when a single currency lets it be dropped).
"""


LEAN_LEGEND = """\
**Columns** — Price (and Target, when shown): listing currency (Curr; GBp = pence). MktCapB: market cap in USD billions.
PE, FwdPE, PEG, PB, PS, Beta: ratios. DE: debt/equity (yfinance scale, ~100 = 1.0x).
ROE, Margin, RevGr, Div, 1m, 3m, 6m, 1y, vs50d, vs200d, Hi, Lo, Upside: **percent values, number only**
(16.6 means +16.6%, -8.7 means -8.7%). Hi/Lo: % from 52-week high/low. vs50d/vs200d: % vs moving average.
Upside: mean analyst target vs price. nAn: number of analysts covering the stock (not a missing-value marker,
despite the resemblance to "NaN" — blank still means unavailable, same as every other column).
Sector codes: Tech=Technology, CommSvc=Communication Services, ConsCyc=Consumer Cyclical, ConsDef=Consumer Defensive,
Enrgy=Energy, Fin=Financial Services, Hlth=Healthcare, Indu=Industrials, Matl=Basic Materials, RE=Real Estate, Util=Utilities.
Analyst codes: SB=strong buy, B=buy, O=outperform, H=hold, U=underperform, S=sell, SS=strong sell (blank=no rating).
Size (from MktCapB, USD): Mega ≥200, Large ≥10, Mid ≥2, Small ≥0.3, Micro <0.3 (billions); the per-set counts are in the header.
**Blank = unavailable.**
"""


def build_markdown(rows: list[dict], failures: list[dict], run_day: str,
                   *, lean: bool = False, group_label: str | None = None,
                   excluded: list[dict] | None = None, drop_names: bool = False) -> str:
    """Serialise the run into one Markdown file. ``lean`` (the default) emits the
    token-light table: same values, blank cells, units in the legend, short sector/
    analyst codes, and no redundant Size column (it's derivable from MktCapB).
    ``drop_names`` additionally omits the company Name column (Ticker still IDs each
    row) for maximum token savings. ``group_label`` records which group was requested;
    ``excluded`` lists names dropped by the sector precision pass."""
    # Order mega -> micro (by USD market cap) so the file reads top-down by size.
    ok = _sorted_ok(rows)

    # Set-level context (helps the AI judge each name relative to the group)
    pes = [v for r in ok if (v := _num(r.get("pe"))) and v > 0]
    r1y = [v for r in ok if (v := _num(r.get("ret_1y"))) is not None]
    sectors = sorted({r["sector"] for r in ok if r.get("sector") and r["sector"] != NA})
    currencies = sorted({r["currency"] for r in ok if r.get("currency")})
    size_counts = Counter(r.get("size") for r in ok)

    out: list[str] = []
    out.append(f"# Stock watchlist review — {run_day}")
    out.append("")
    out.append(
        f"Snapshot of **{len(ok)} stocks** pulled from yfinance on {run_day}. "
        "This file is built to be handed to an AI for qualitative review. "
        "**Research screen, not financial advice.**"
    )
    out.append("")
    # quick set overview
    overview = [f"- **Stocks:** {len(ok)}"]
    if group_label:
        overview.append(f"- **Group:** {group_label}")
    size_line = ", ".join(f"{b}: {size_counts[b]}" for b in SIZE_ORDER if size_counts[b])
    if size_line:
        overview.append(f"- **By size (market cap):** {size_line}")
    if len(currencies) > 1:
        overview.append(f"- **Currencies / markets:** {', '.join(currencies)} "
                        "(prices in listing currency; market caps normalised to USD)")
    if sectors:
        overview.append(f"- **Sectors covered:** {', '.join(sectors)}")
    if pes:
        overview.append(f"- **Median trailing P/E (set):** {statistics.median(pes):.1f}")
    if r1y:
        overview.append(f"- **Median 1-year return (set):** {statistics.median(r1y)*100:+.1f}%")
    if excluded:
        ex = ", ".join(f"{e['ticker']} ({e.get('industry', NA)})" for e in excluded)
        overview.append(f"- **Excluded ({len(excluded)} off-sector per data source):** {ex}")
    if failures:
        overview.append(f"- **Could not fetch:** {', '.join(f['ticker'] for f in failures)}")
    out.append("\n".join(overview))
    out.append("")

    out.append("## How to review this list")
    out.append("")
    out.append(LEAN_INSTRUCTIONS if lean else REVIEW_INSTRUCTIONS)
    out.append("")

    # --- One CSV row per stock: identifiers + valuation/quality + momentum/analyst.
    # CSV (not a Markdown pipe table) roughly halves the tokens for this grid and
    # LLMs parse it natively. csv.writer quotes any field containing a comma (e.g.
    # "Amazon.com, Inc.") so names stay intact. The default lean table drops per-cell
    # units, '+' signs and "n/a" (units move to the legend; a blank cell means
    # unavailable), codes the long Sector/Analyst strings, and omits the redundant
    # Size column (derivable from MktCapB) to cut tokens further. --rich restores the
    # verbose per-cell format; --no-names also drops the Name column.
    out.append("## Stocks (CSV)")
    out.append("")
    out.append(LEAN_LEGEND if lean else LEGEND)

    # Header and cells come from the SAME column spec (see COLUMNS), so a header can
    # never drift out of sync with the cell beneath it. The lean table additionally
    # sheds, for THIS set, columns it can reconstruct or that are empty everywhere
    # (single-currency Curr, the derivable Target, all-blank columns) — each announced
    # in a one-line note so nothing is dropped silently. Rich stays verbose and stable.
    grid, dropped = _grid_csv(ok, lean=lean, drop_names=drop_names)
    if lean:
        note = _compaction_note(dropped, ok)
        if note:
            out.append(note)
    out.append("")

    out.append("```csv")
    out.append(grid)
    out.append("```")
    out.append("")

    if failures:
        out.append("## Could not fetch")
        out.append("")
        for f in failures:
            out.append(f"- **{f['ticker']}** — {f.get('error', 'unknown error')}")
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        "_Generated by `stock_review.py`. Data: yfinance (unofficial, can be stale "
        "or wrong — that's why we ask the reviewer to flag anomalies). No AI ran "
        "here; this file is the input to your AI review._"
    )
    out.append("")
    return "\n".join(out)
