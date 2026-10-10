"""Build the ticker universe from config.

Sources:
  csv    -> a local CSV with a 'ticker' column (custom watchlists, the sample20).
  sp500  -> S&P 500 constituents scraped from Wikipedia.
  nasdaq -> NASDAQ-listed symbols from nasdaqtrader.com.
  asx    -> ASX-listed companies (symbols suffixed '.AX' for Yahoo).

Network sources are best-effort and wrapped in retries. ASX coverage in yfinance
is patchier than US - documented in the README.
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pandas as pd

from .config import Config
from .logging_setup import get_logger

log = get_logger("universe")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-analyzer/1.0)"}


def _get(url: str, retries: int = 3, timeout: int = 30):
    import requests  # lazy: only network universe sources need it
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2.0 * (2 ** attempt)
            log.warning("GET %s failed (%d/%d): %s -> retry %.1fs", url, attempt + 1, retries, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"failed to fetch {url}: {last}")


def build_universe(cfg: Config) -> list[str]:
    src = (cfg.universe.source or "csv").lower()
    if src == "csv":
        tickers = _from_csv(cfg.resolve(cfg.universe.csv_path))
    elif src == "sp500":
        tickers = _sp500()
    elif src == "nasdaq":
        tickers = _nasdaq()
    elif src == "asx":
        tickers = _asx()
    else:
        raise ValueError(f"Unknown universe.source: {cfg.universe.source!r}")

    # de-dupe preserving order
    seen, out = set(), []
    for t in tickers:
        t = str(t).strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    if cfg.universe.limit:
        out = out[: int(cfg.universe.limit)]
    log.info("universe '%s' -> %d tickers", src, len(out))
    return out


def _from_csv(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"universe CSV not found: {path}")
    # dtype=str / keep_default_na=False: tickers like NA, NAN or 0700 must not be
    # read as NaN / numbers (silently dropped or stripped of leading zeros).
    df = pd.read_csv(path, comment="#", dtype=str, keep_default_na=False)
    cols = {c.lower(): c for c in df.columns}
    if "ticker" not in cols:
        raise ValueError(f"universe CSV {path} must have a 'ticker' column; got {list(df.columns)}")
    return [t for t in (str(t).strip() for t in df[cols["ticker"]].tolist()) if t]


def _sp500() -> list[str]:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = _get(url).text
    tables = pd.read_html(io.StringIO(html))
    for tbl in tables:
        if "Symbol" in tbl.columns:
            # Yahoo uses '-' where S&P uses '.' (e.g. BRK.B -> BRK-B)
            return [str(s).replace(".", "-").strip() for s in tbl["Symbol"].dropna()]
    raise RuntimeError("could not find S&P 500 constituents table")


def _nasdaq() -> list[str]:
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    text = _get(url).text
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str, keep_default_na=False)
    # Last row is a file-creation footer; 'Test Issue' marks non-tradable symbols.
    df = df[df.get("Test Issue", "N") == "N"]
    syms = df["Symbol"].dropna().astype(str)
    return [s.strip() for s in syms if s and "File Creation Time" not in s]


def _asx() -> list[str]:
    url = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
    text = _get(url).text
    # File has a couple of preamble lines before the header row.
    df = pd.read_csv(io.StringIO(text), skiprows=1, dtype=str, keep_default_na=False)
    code_col = next((c for c in df.columns if "code" in c.lower()), None)
    if code_col is None:
        raise RuntimeError(f"could not find ASX code column in {list(df.columns)}")
    return [f"{c.strip()}.AX" for c in df[code_col] if c.strip()]
