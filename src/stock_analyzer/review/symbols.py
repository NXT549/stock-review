"""Ticker-symbol normalisation for yfinance (exchange suffixes, class shares)."""
from __future__ import annotations

import re


# Known Yahoo exchange suffixes. Single letters stay minimal (T/L only) because
# a one-letter suffix is usually a US class share; two-letter codes are safe to
# list generously (US class shares are single letters).
EXCHANGE_SUFFIXES = frozenset({
    "AX", "T", "L", "DE", "PA", "HK", "TO", "NS",           # original set
    "AS", "SW", "MI", "MC", "BR", "LS", "CO", "ST", "HE",    # Euronext/SIX/Nordics
    "OL", "WA", "VI", "IR",                                  # Oslo/Warsaw/Vienna/Dublin
    "SI", "KS", "KQ", "TW", "SS", "SZ", "BO", "JK", "KL",    # Asia
    "NZ", "JO", "SA", "TA", "IS", "ME", "BK", "VX",          # NZ/ZA/BR/IL/TR/RU/TH/CH
})


def clean_symbol(sym: str) -> str:
    """Normalise a ticker for yfinance.

    US class shares use '-' not '.' (BRK.B -> BRK-B). Tickers that already carry a
    known exchange suffix (7203.T, BHP.AX, ADS.DE, RIO.L) are left intact. A regex
    that simply kept any ".X" suffix wrongly treated the ".B" class share as an
    exchange code, so BRK.B/BF.B fell through unconverted and failed to resolve.

    Unknown suffixes fall back by length: one letter is far more likely a US class
    share (-> dash), while two or more letters look like an exchange code this
    allowlist just doesn't know yet (-> keep the dot; dashing an international
    listing would break it, whereas 2+-letter US class shares don't exist).
    """
    s = sym.strip().upper()
    stem, dot, suffix = s.rpartition(".")
    if dot and suffix in EXCHANGE_SUFFIXES:  # genuine exchange suffix -> keep as-is
        return s
    if dot and stem and len(suffix) >= 2:    # unknown but exchange-shaped -> keep
        return s
    return s.replace(".", "-")


def _intl_symbol(raw: str, cfg: dict) -> str:
    s = raw.strip().upper()
    if not s:
        return ""
    if cfg.get("digits_only"):
        m = re.search(r"(\d+)", s)
        if not m:
            return ""
        s = m.group(1)
        if cfg.get("zfill"):
            s = s.zfill(cfg["zfill"])
    elif cfg.get("dot_to_dash"):
        s = s.replace(".", "-")
    elif not cfg.get("keep_dots"):
        s = s.split()[0]  # guard against stray trailing text
    return s + cfg.get("suffix", "")
