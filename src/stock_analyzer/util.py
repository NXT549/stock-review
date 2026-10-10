"""Small shared helpers (no third-party deps beyond pandas)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math

import pandas as pd


def df_to_markdown(df: pd.DataFrame, index: bool = False) -> str:
    """Render a DataFrame as a GitHub-flavoured Markdown table.

    Avoids the optional `tabulate` dependency that DataFrame.to_markdown needs.
    """
    if df is None or len(df) == 0:
        return "_(no rows)_"
    frame = df.reset_index() if index else df.copy()

    def esc(s: str) -> str:
        # Keep cell contents from breaking the table: a literal '|' would be read
        # as a column separator, and a newline would split the row.
        return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    def cell(v):
        # Robust null check first: covers None, float NaN/inf, numpy NaN, and the
        # pandas nullable <NA> (e.g. an Int64 rank for an unscored stock), all of
        # which should render as an empty cell rather than the text "<NA>".
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        if isinstance(v, float):
            return f"{v:.4g}"
        return esc(str(v))

    headers = [esc(str(c)) for c in frame.columns]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(cell(row[c]) for c in frame.columns) + " |")
    return "\n".join(lines)


def df_to_csv(df: pd.DataFrame, index: bool = False) -> str:
    """Render a DataFrame as compact CSV (token-efficient for AI consumption).

    Roughly half the characters of the equivalent Markdown pipe table: a single
    comma between cells, no padding, no '| --- |' divider row. Missing values are
    blank; integral floats lose the trailing '.0' ("100" not "100.0"); commas /
    quotes / newlines inside a cell are quoted per RFC 4180 (via the csv module).
    """
    if df is None or len(df) == 0:
        return ""
    frame = df.reset_index() if index else df

    def cell(v) -> str:
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        if isinstance(v, float):
            return str(int(v)) if v.is_integer() else f"{v:g}"
        return str(v)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([str(c) for c in frame.columns])
    for _, row in frame.iterrows():
        writer.writerow([cell(row[c]) for c in frame.columns])
    return buf.getvalue().rstrip("\n")


def _no_negzero(v: float, digits: int) -> float:
    """Round to `digits` places, stripping a sign that would only show as a
    misleading '-0.0' — either a literal -0.0 or a small negative value that
    rounds away to nothing at this precision (e.g. -0.00001 at 1 digit).
    Python's round() preserves the sign of an exact zero result; `r == 0` is
    True for both 0.0 and -0.0, so this catches both."""
    r = round(v, digits)
    return 0.0 if r == 0 else r


def fmt_pct(x, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    try:
        v = float(x) * 100
    except (TypeError, ValueError, OverflowError):
        return "n/a"
    return f"{_no_negzero(v, digits):.{digits}f}%"


def fmt_num(x, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return "n/a"
    if abs(v) >= 1e12:
        return f"{_no_negzero(v / 1e12, digits):.{digits}f}T"
    if abs(v) >= 1e9:
        return f"{_no_negzero(v / 1e9, digits):.{digits}f}B"
    if abs(v) >= 1e6:
        return f"{_no_negzero(v / 1e6, digits):.{digits}f}M"
    return f"{_no_negzero(v, digits):.{digits}f}"


def config_hash(scoring_cfg) -> str:
    """Stable short hash of the scoring methodology (weights etc.).

    Lets the backtest group results by methodology version.
    """
    payload = {
        "category_weights": scoring_cfg.category_weights,
        "metric_weights": scoring_cfg.metric_weights,
        "winsor_lower": scoring_cfg.winsor_lower,
        "winsor_upper": scoring_cfg.winsor_upper,
        "min_group_size": scoring_cfg.min_group_size,
        "min_rankable": scoring_cfg.min_rankable,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]
