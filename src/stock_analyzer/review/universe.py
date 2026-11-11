"""Ticker universes for the watchlist review: the named ``--source`` lists, the index
scrapers behind them and the ``--sector`` group logic.

The module is three layers, each usable and tested on its own:

* **Definitions** are declarative tables: which index pages exist (``INDEXES``), how
  the named universes are assembled from them (``UNIVERSES``) and which GICS labels
  make up a sector group (``SECTORS``). Adding a market means adding a row.
* **Loading** is ``UniverseLoader``: in-memory memo -> per-day disk cache ->
  concurrent scrape over one pooled, retrying HTTP session -> a recent stale copy
  as the last resort. Every index page is downloaded and parsed at most once a day,
  whether a run needs its tickers, its sectors or both.
* **Group logic** is pure functions over the loaded records: sector membership, the
  coarse-match set the CLI re-checks against yfinance, and the tickers-file reader.

Group membership comes from the index tables' own GICS columns, so deciding who is
in a group needs no per-ticker fetch. Nothing here invents data: a row with no
usable symbol is skipped, a missing market cap stays ``None``.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, fields
from datetime import date
from functools import partial
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, Self, TypeAlias
from urllib.parse import urlsplit

from .format import NA, _num
from .symbols import _intl_symbol, clean_symbol

__all__ = [
    "DEFAULT_TICKERS", "INDEXES", "REGION_RECORDS", "SECTORS", "SECTOR_ALIASES",
    "SOURCES", "UNIVERSES", "IndexLoadError", "IndexRecord", "LayoutError",
    "SectorSpec", "UniverseError", "UniverseLoader", "configure", "describe_group",
    "format_groups", "loose_sector_tickers", "print_groups", "read_tickers_file",
    "region_records", "resolve_sector_key", "tickers_for_sector", "universe_tickers",
    "verify_loose_industry",
]

# Child of fetch.py's "stock_review" logger, so one switch turns both on. Silent by
# default below WARNING; ``logging.basicConfig(level=logging.INFO)`` shows cache hits,
# scrape timings and retries.
_log = logging.getLogger("stock_review.universe")

DEFAULT_CACHE_DIR: Final = Path("data/review_cache")
CACHE_SUBDIR: Final = "_universe"
CACHE_SCHEMA: Final = 2
# A cached list this many days old may stand in when today's scrape fails; older
# day folders are deleted. Index membership changes a few times a quarter, so a
# week-old list is a far better answer than no run at all.
STALE_DAYS: Final = 7

MAX_FETCH_WORKERS: Final = 4      # enough to overlap the latency, polite to Wikipedia
HTTP_TIMEOUT: Final = (10.0, 30.0)                    # (connect, read) seconds
HTTP_ATTEMPTS: Final = 3
HTTP_BACKOFF: Final = 1.0         # first retry delay; doubles each attempt
HTTP_MAX_DELAY: Final = 10.0      # cap, also applied to a server's Retry-After
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
HTTP_HEADERS: Final = {"User-Agent": "Mozilla/5.0 (stock_review universe fetch)"}

AU_SMALLCAP_MAX_AUD: Final = 5_000_000_000
MAX_TICKERS_FILE_BYTES: Final = 5 * 1024 * 1024


# --- Errors -------------------------------------------------------------------
class UniverseError(SystemExit):
    """A final, user-facing failure: bad input, a changed page layout, a missing
    package. The message is the whole story and needs no traceback.

    It subclasses ``SystemExit`` on purpose. This module has always signalled such
    failures that way, and the CLI and both GUIs depend on it to show the message
    and stop cleanly.
    """


class LayoutError(UniverseError):
    """A page was fetched but no longer has the table or columns we read."""


class IndexLoadError(RuntimeError):
    """Several index pages failed in one batch. ``failures`` maps key -> cause.

    The message names every page but quotes each kind of error once: nine pages
    that are all unreachable should read as one problem, not nine.
    """

    def __init__(self, failures: Mapping[str, BaseException]) -> None:
        self.failures: dict[str, BaseException] = dict(failures)
        by_kind: dict[type[BaseException], list[str]] = {}
        for key, exc in self.failures.items():
            by_kind.setdefault(type(exc), []).append(key)
        super().__init__("; ".join(
            f"{', '.join(keys)}: {kind.__name__}: {self.failures[keys[0]]}"
            for kind, keys in by_kind.items()))


# --- Records ------------------------------------------------------------------
def _squash(text: str) -> str:
    """Collapse every run of whitespace (non-breaking spaces included) to one space."""
    return " ".join(text.split())


def _label(text: str) -> str:
    """Normalise a GICS label once, at parse time, so matching is a set lookup."""
    return _squash(text).lower()


@dataclass(frozen=True, slots=True)
class IndexRecord:
    """One index constituent. The GICS labels are stored normalised (see ``_label``)."""

    symbol: str
    sector: str = ""               # GICS sector
    subind: str = ""               # GICS sub-industry (the US tables)
    igroup: str = ""               # GICS industry group (the full-ASX CSV)
    has_subind: bool = False       # the source table had a sub-industry column
    mcap_local: float | None = None    # listing currency; None = not published

    def to_json(self) -> dict[str, Any]:
        """Compact dict: fields still at their default are left out."""
        return {f.name: value for f in _RECORD_FIELDS
                if (value := getattr(self, f.name)) != f.default}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Self:
        """Rebuild a cached record; raises ``ValueError``/``TypeError`` on bad data."""
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"record without a symbol: {raw!r}")
        mcap = raw.get("mcap_local")
        if mcap is not None and (isinstance(mcap, bool) or not isinstance(mcap, int | float)):
            raise TypeError(f"non-numeric market cap for {symbol}: {mcap!r}")
        return cls(symbol=symbol, sector=str(raw.get("sector", "")),
                   subind=str(raw.get("subind", "")), igroup=str(raw.get("igroup", "")),
                   has_subind=bool(raw.get("has_subind", False)),
                   mcap_local=None if mcap is None else float(mcap))


_RECORD_FIELDS: Final = fields(IndexRecord)
Records: TypeAlias = tuple[IndexRecord, ...]


# --- HTML tables --------------------------------------------------------------
_RawCell: TypeAlias = tuple[str, int, int]     # (text, rowspan, colspan)
_MAX_SPAN: Final = 100                         # ignore absurd rowspan/colspan values


class _OpenTable:
    """A ``<table>`` being read: its attributes plus rows of raw cells."""

    __slots__ = ("_parts", "_row", "_span", "attrs", "rows")

    def __init__(self, attrs: dict[str, str]) -> None:
        self.attrs = attrs
        self.rows: list[list[_RawCell]] = []
        self._row: list[_RawCell] | None = None
        self._parts: list[str] | None = None
        self._span = (1, 1)

    def start_row(self) -> None:
        self.end_row()
        self._row = []

    def end_row(self) -> None:
        self.end_cell()
        if self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def start_cell(self, rowspan: int, colspan: int) -> None:
        self.end_cell()
        if self._row is None:          # a cell with no <tr>: tolerate it
            self._row = []
        self._parts = []
        self._span = (rowspan, colspan)

    def end_cell(self) -> None:
        if self._parts is not None and self._row is not None:
            self._row.append((_squash("".join(self._parts)), *self._span))
        self._parts = None

    def add_text(self, text: str) -> None:
        if self._parts is not None:
            self._parts.append(text)


class _TableReader(HTMLParser):
    """Collect every ``<table>`` on a page as rows of plain cell text.

    No DOM is built: a constituents page is one to two megabytes and we want a few
    hundred short strings out of it, so a single streaming pass is both the fastest
    and the lightest way to get them (and needs nothing outside the stdlib). Only
    what such a table needs is modelled: rows, cells, spans, nested tables, and text
    clean-up (footnote markers and ``<style>``/``<script>`` bodies are dropped,
    ``<br>`` becomes a space).
    """

    _IGNORED: ClassVar[frozenset[str]] = frozenset({"style", "script", "sup"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_OpenTable] = []     # document order, outer before inner
        self.complete = 0                      # tables[:complete] are fully read
        self._open: list[_OpenTable] = []      # tables not closed yet (innermost last)
        self._ignoring = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            table = _OpenTable({name: value or "" for name, value in attrs})
            self.tables.append(table)
            self._open.append(table)
        elif tag in self._IGNORED:
            self._ignoring += 1
        elif self._open:
            table = self._open[-1]
            if tag == "tr":
                table.start_row()
            elif tag in ("td", "th"):
                # A footnote marker cannot span cells, so an unclosed <sup> must
                # not swallow the rest of the table.
                self._ignoring = 0
                spans = dict(attrs)
                table.start_cell(_span(spans.get("rowspan")), _span(spans.get("colspan")))
            elif tag == "br":
                table.add_text(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED:
            self._ignoring = max(0, self._ignoring - 1)
        elif self._open:
            table = self._open[-1]
            if tag == "table":
                table.end_row()
                self._open.pop()
                if not self._open:
                    self.complete = len(self.tables)
            elif tag == "tr":
                table.end_row()
            elif tag in ("td", "th"):
                table.end_cell()

    def handle_data(self, data: str) -> None:
        if self._open and not self._ignoring:
            self._open[-1].add_text(data)

    def close(self) -> None:
        super().close()
        for table in self._open:               # truncated page: keep what we have
            table.end_row()
        self._open.clear()
        self.complete = len(self.tables)


def _span(value: str | None) -> int:
    """A rowspan/colspan attribute as a sane positive int (1 when absent or junk)."""
    try:
        return min(max(int(value or 1), 1), _MAX_SPAN)
    except ValueError:
        return 1


_Carried: TypeAlias = dict[int, tuple[str, int]]   # column -> (text, rows still covered)


def _carry_down(line: list[str], carried: _Carried, still: _Carried) -> None:
    """Append every cell that a rowspan above pushes into ``line``'s next columns."""
    while (hit := carried.get(len(line))) is not None:
        text, rows_left = hit
        if rows_left > 1:
            still[len(line)] = (text, rows_left - 1)
        line.append(text)


def _expand_spans(rows: Sequence[Sequence[_RawCell]]) -> list[list[str]]:
    """Repeat spanned cells so that column ``i`` means the same thing in every row."""
    if all(rowspan == 1 == colspan for cells in rows for _, rowspan, colspan in cells):
        return [[text for text, _, _ in cells] for cells in rows]   # the usual case
    grid: list[list[str]] = []
    carried: _Carried = {}
    for cells in rows:
        line: list[str] = []
        still: _Carried = {}
        for text, rowspan, colspan in cells:
            _carry_down(line, carried, still)
            for _ in range(colspan):
                if rowspan > 1:
                    still[len(line)] = (text, rowspan - 1)
                line.append(text)
        _carry_down(line, carried, still)
        grid.append(line)
        carried = still
    return grid


def iter_html_tables(html: str, *, chunk_size: int = 1 << 16) -> Iterator[_OpenTable]:
    """Yield the page's tables in document order, parsing only as far as the caller
    reads: the constituents table usually sits above long change-history tables and
    navigation boxes that are never needed."""
    reader = _TableReader()
    served = 0
    for start in range(0, len(html), chunk_size):
        reader.feed(html[start:start + chunk_size])
        while served < reader.complete:
            yield reader.tables[served]
            served += 1
    reader.close()
    yield from reader.tables[served:]


# --- Index definitions and their parsers ---------------------------------------
def _column(header: Sequence[str], name: str | None) -> int | None:
    """Index of the column whose header is exactly ``name`` (already lower-case)."""
    if name is None:
        return None
    return next((i for i, cell in enumerate(header) if cell == name), None)


def _cell(row: Sequence[str], col: int | None) -> str:
    return row[col] if col is not None and col < len(row) else ""


def _amount(text: str) -> float | None:
    """A printed figure such as ``3,420,074,734`` as a float; ``None`` if absent."""
    return _num(re.sub(r"[^0-9.]", "", text) or None)


@dataclass(frozen=True, slots=True)
class WikiTable:
    """A Wikipedia constituents table, found by its headers rather than its position.

    ``symbol_keys`` are keywords that name the ticker column (substring match), and
    ``to_symbol`` turns that cell into a yfinance symbol, returning ``""`` to skip the
    row. The sector, sub-industry and market-cap columns are optional extras matched
    on their exact header: losing one degrades the sector groups, never the plain
    ticker list. ``min_rows`` (header included) rejects look-alike tables and a
    truncated or vandalised page.
    """

    url: str
    symbol_keys: tuple[str, ...]
    to_symbol: Callable[[str], str]
    min_rows: int
    sector: str | None = None
    subind: str | None = None
    mcap: str | None = None

    def parse(self, body: str) -> list[IndexRecord]:
        for table in iter_html_tables(body):
            is_candidate = ("wikitable" in table.attrs.get("class", "").split()
                            or table.attrs.get("id") == "constituents")
            if (is_candidate and len(table.rows) >= self.min_rows
                    and (records := self._read(_expand_spans(table.rows)))):
                return records
        raise LayoutError(f"Could not find the constituents table on {self.url} "
                          "(layout changed?).")

    def _read(self, grid: Sequence[Sequence[str]]) -> list[IndexRecord]:
        header = [cell.lower() for cell in grid[0]]
        symbol_col = next((i for i, cell in enumerate(header)
                           if any(key in cell for key in self.symbol_keys)), None)
        if symbol_col is None:
            return []
        sector_col = _column(header, self.sector)
        subind_col = _column(header, self.subind)
        mcap_col = _column(header, self.mcap)
        records: list[IndexRecord] = []
        for row in grid[1:]:
            symbol = self.to_symbol(_cell(row, symbol_col))
            if symbol:
                records.append(IndexRecord(
                    symbol=symbol,
                    sector=_label(_cell(row, sector_col)),
                    subind=_label(_cell(row, subind_col)),
                    has_subind=subind_col is not None,
                    mcap_local=_amount(_cell(row, mcap_col))))
        return records


@dataclass(frozen=True, slots=True)
class AsxListing:
    """The ASX's official CSV of every listed company: code + GICS industry group."""

    url: str
    min_rows: int = 500

    # Industry-group values that carry no usable sector (ETFs, trusts, pending).
    _NO_GROUP: ClassVar[frozenset[str]] = frozenset({"", "not applic", "class pend"})
    _PREAMBLE_LINES: ClassVar[int] = 10    # the header sits within the first few lines

    def parse(self, body: str) -> list[IndexRecord]:
        # The stdlib reader keeps every cell as text, so the code "NAN" (Nanosonics)
        # can never be mistaken for a missing value.
        rows = csv.reader(io.StringIO(body))
        try:
            code_col, group_col = self._find_header(rows)
            records = [IndexRecord(symbol=f"{code}.AX", igroup=self._group(row, group_col))
                       for row in rows
                       if (code := _cell(row, code_col).strip().upper())]
        except csv.Error as exc:
            raise LayoutError(f"Could not read the ASX listed-companies CSV ({exc}; "
                              "format changed?).") from exc
        if len(records) < self.min_rows:
            raise LayoutError(f"Parsed only {len(records)} rows from the ASX "
                              "listed-companies CSV (format changed?).")
        return records

    @classmethod
    def _find_header(cls, rows: Iterator[list[str]]) -> tuple[int, int | None]:
        """Skip the preamble; return the (code, industry-group) column indexes."""
        for row in islice(rows, cls._PREAMBLE_LINES):
            header = [cell.strip().lower() for cell in row]
            code_col = next((i for i, cell in enumerate(header) if "code" in cell), None)
            if code_col is not None and len(header) > 1:
                group_col = next((i for i, cell in enumerate(header)
                                  if "industry" in cell), None)
                return code_col, group_col
        raise LayoutError("Could not find an ASX code column in the listed-companies "
                          "CSV (format changed?).")

    @classmethod
    def _group(cls, row: Sequence[str], col: int | None) -> str:
        group = _label(_cell(row, col))
        return "" if group in cls._NO_GROUP else group


@dataclass(frozen=True, slots=True)
class StaticList:
    """A built-in list: no network, no cache."""

    tickers: tuple[str, ...]


IndexSpec: TypeAlias = WikiTable | AsxListing | StaticList


def _exchange(suffix: str, **rules: bool | int) -> Callable[[str], str]:
    """Cell text -> yfinance symbol for one exchange (rules: see ``_intl_symbol``)."""
    return partial(_intl_symbol, cfg={"suffix": suffix, **rules})


def _sp_table(page: str, min_rows: int) -> WikiTable:
    return WikiTable(url=f"https://en.wikipedia.org/wiki/{page}",
                     symbol_keys=("symbol",), to_symbol=clean_symbol, min_rows=min_rows,
                     sector="gics sector", subind="gics sub-industry")


DEFAULT_TICKERS: Final[tuple[str, ...]] = (
    # Mega-cap tech / communication
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "CRM", "ADBE",
    "AMD", "CSCO", "NFLX", "QCOM", "TXN", "INTU", "NOW", "AMAT", "MU", "INTC",
    # Consumer
    "TSLA", "WMT", "COST", "HD", "LOW", "MCD", "NKE", "SBUX", "PG", "KO", "PEP",
    "TJX", "BKNG", "UBER", "ABNB",
    # Health care
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN", "ISRG",
    # Financials
    "BRK-B", "JPM", "BAC", "WFC", "V", "MA", "GS", "MS", "AXP", "SPGI", "BLK",
    # Energy / industrials / materials
    "XOM", "CVX", "CAT", "BA", "GE", "DE", "HON", "RTX", "UNP", "LIN",
    # Comms / media / telecom
    "DIS", "CMCSA", "T", "VZ",
    # Smaller / mid-cap sleeve (the Size column will tag these Mid/Small)
    "SHAK", "PLNT", "CAKE", "SFM", "CROX", "DECK", "WING", "FIVE", "CELH", "BJ",
    "TXRH", "PSTG", "RMBS", "AAON", "FN", "POWI", "SAIA", "EXPO",
)

# Japan: the Nikkei 225 page carries no machine-readable constituents table.
NIKKEI_CURATED: Final[tuple[str, ...]] = (
    "7203.T", "6758.T", "6861.T", "9984.T", "8306.T", "6098.T", "7974.T", "9983.T",
    "4063.T", "8035.T", "6501.T", "6902.T", "7267.T", "6594.T", "4502.T", "8316.T",
    "8411.T", "9433.T", "9432.T", "4519.T", "6367.T", "7741.T", "6981.T", "8058.T",
    "8001.T", "8031.T", "2914.T", "4661.T", "6273.T", "6954.T", "7751.T", "6752.T",
    "8766.T", "4568.T", "4503.T", "5108.T", "7011.T", "9020.T", "8053.T", "6201.T",
)

# The minimum row counts sit well under each index's real size: they exist to
# reject a wrong or half-loaded table, not to track membership.
INDEXES: Final[Mapping[str, IndexSpec]] = {
    "curated": StaticList(DEFAULT_TICKERS),
    "nikkei": StaticList(NIKKEI_CURATED),
    "sp500": _sp_table("List_of_S%26P_500_companies", 400),
    "sp400": _sp_table("List_of_S%26P_400_companies", 300),
    "sp600": _sp_table("List_of_S%26P_600_companies", 450),
    "asx200": WikiTable(                                            # Australia
        url="https://en.wikipedia.org/wiki/S%26P/ASX_200",
        symbol_keys=("code",), to_symbol=_exchange(".AX"), min_rows=50,
        sector="sector", mcap="market capitalisation (a$)"),
    "ftse100": WikiTable(                       # United Kingdom (quoted in pence, GBp)
        url="https://en.wikipedia.org/wiki/FTSE_100_Index",
        symbol_keys=("ticker", "epic"), to_symbol=_exchange(".L", dot_to_dash=True),
        min_rows=50),
    "dax": WikiTable(          # Germany (the page's ticker already carries .DE / .PA)
        url="https://en.wikipedia.org/wiki/DAX",
        symbol_keys=("ticker",), to_symbol=_exchange("", keep_dots=True), min_rows=30),
    "hsi": WikiTable(                               # Hong Kong ("SEHK: 700" -> 0700.HK)
        url="https://en.wikipedia.org/wiki/Hang_Seng_Index",
        symbol_keys=("ticker",), to_symbol=_exchange(".HK", digits_only=True, zfill=4),
        min_rows=30),
    "tsx60": WikiTable(                                             # Canada
        url="https://en.wikipedia.org/wiki/S%26P/TSX_60",
        symbol_keys=("symbol",), to_symbol=_exchange(".TO", dot_to_dash=True),
        min_rows=40),
    "nifty50": WikiTable(                                           # India
        url="https://en.wikipedia.org/wiki/NIFTY_50",
        symbol_keys=("symbol",), to_symbol=_exchange(".NS"), min_rows=40),
    "asx-all": AsxListing(                  # every ASX listing (~2,000; big and slow)
        url="https://www.asx.com.au/asx/research/ASXListedCompanies.csv"),
}


# --- Universe definitions ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Part:
    """One index inside a universe, optionally narrowed to its small-cap tail."""

    index: str
    smallcap: bool = False     # keep only rows under AU_SMALLCAP_MAX_AUD


@dataclass(frozen=True, slots=True)
class Universe:
    """A named ``--source``: index parts concatenated in order, plus its aliases.

    ``sector_region`` marks universes whose every index carries GICS data, which is
    what lets them scope a ``--sector`` run.
    """

    parts: tuple[Part, ...]
    aliases: tuple[str, ...] = ()
    sector_region: bool = False


def _universe(*parts: str | Part, aliases: Sequence[str] = (),
              sector_region: bool = False) -> Universe:
    return Universe(tuple(p if isinstance(p, Part) else Part(p) for p in parts),
                    tuple(aliases), sector_region)


_US: Final = ("sp500", "sp400", "sp600")
_INTERNATIONAL: Final = ("nikkei", "asx200", "ftse100", "dax", "hsi", "tsx60", "nifty50")
_ASX_SMALL: Final = Part("asx200", smallcap=True)

UNIVERSES: Final[Mapping[str, Universe]] = {
    "curated": _universe("curated"),
    # United States
    "sp500": _universe("sp500", aliases=("sap",), sector_region=True),
    "sp400": _universe("sp400", aliases=("midcap",), sector_region=True),
    "sp600": _universe("sp600", sector_region=True,
                       aliases=("smallcap", "america-smallcap", "us-smallcap")),
    "sp1500": _universe(*_US, aliases=("us", "america"), sector_region=True),
    # Australia: the 200, its small-cap tail, and every listing
    "asx200": _universe("asx200", aliases=("australia", "a200"), sector_region=True),
    "australia-smallcap": _universe(_ASX_SMALL, aliases=("au-smallcap",),
                                    sector_region=True),
    "asx-all": _universe("asx-all", aliases=("australia-all", "asx"),
                         sector_region=True),
    # A focused global scope (US 1500 + ASX 200) and its small-cap sleeve
    "global": _universe(*_US, "asx200", sector_region=True),
    "global-smallcap": _universe("sp600", _ASX_SMALL, aliases=("world-smallcap",),
                                 sector_region=True),
    # Other single markets
    "nikkei": _universe("nikkei", aliases=("japan",)),
    "ftse100": _universe("ftse100", aliases=("uk",)),
    "dax": _universe("dax", aliases=("germany",)),
    "hsi": _universe("hsi", aliases=("hongkong",)),
    "tsx60": _universe("tsx60", aliases=("canada",)),
    "nifty50": _universe("nifty50", aliases=("india",)),
    # Combos
    "international": _universe(*_INTERNATIONAL, aliases=("intl",)),
    "world": _universe(*_US, *_INTERNATIONAL, aliases=("everything", "all")),
}

_BY_NAME: Final[Mapping[str, Universe]] = {
    name: universe for canonical, universe in UNIVERSES.items()
    for name in (canonical, *universe.aliases)
}


# --- Sector group definitions --------------------------------------------------
@dataclass(frozen=True, slots=True)
class SectorSpec:
    """Which GICS labels put a record in a ``--sector`` group.

    A plain sector group sets ``sectors`` (plus the matching ``igroups`` for the
    full-ASX CSV, which only publishes industry groups). A sub-sector concept such as
    mining sets ``subind_kw`` instead and is matched as precisely as each table
    allows; whatever enters through a coarser path is re-checked against yfinance's
    own industry using ``yf_industry_kw``.
    """

    sectors: frozenset[str] = frozenset()
    igroups: frozenset[str] = frozenset()
    subind_kw: frozenset[str] = frozenset()
    fallback_sectors: frozenset[str] = frozenset()
    coarse_igroups: frozenset[str] = frozenset()
    yf_industry_kw: frozenset[str] = frozenset()

    def matches(self, rec: IndexRecord) -> bool:
        if self.sectors:                   # plain GICS-sector group
            return rec.sector in self.sectors or rec.igroup in self.igroups
        if rec.has_subind:                 # US tables: precise sub-industry match
            return any(kw in rec.subind for kw in self.subind_kw)
        if rec.igroup:                     # full-ASX CSV: industry group
            return rec.igroup in self.igroups
        return rec.sector in self.fallback_sectors    # ASX 200: sector column only

    def is_coarse_match(self, rec: IndexRecord) -> bool:
        """True if ``rec`` joins a sub-sector group through a coarse path: a
        whole-sector fallback (ASX 200) or a broad industry group (the full ASX's
        'materials' for mining). Precise matches need no second look."""
        if not self.subind_kw or rec.has_subind:
            return False
        if rec.igroup:
            return rec.igroup in self.coarse_igroups
        return rec.sector in self.fallback_sectors


def _sector(**groups: Iterable[str]) -> SectorSpec:
    return SectorSpec(**{name: frozenset(values) for name, values in groups.items()})


SECTORS: Final[Mapping[str, SectorSpec]] = {
    "energy": _sector(sectors={"energy"}, igroups={"energy"}),
    "materials": _sector(sectors={"materials"}, igroups={"materials"}),
    "industrials": _sector(
        sectors={"industrials"},
        igroups={"capital goods", "commercial & professional services", "transportation"}),
    "technology": _sector(
        sectors={"information technology"},
        igroups={"software & services", "technology hardware & equipment",
                 "semiconductors & semiconductor equipment"}),
    "financials": _sector(
        sectors={"financials"},
        igroups={"financial services", "banks", "insurance", "diversified financials"}),
    "healthcare": _sector(
        sectors={"health care", "healthcare"},
        igroups={"health care equipment & services",
                 "pharmaceuticals, biotechnology & life sciences"}),
    "consumer": _sector(
        sectors={"consumer discretionary", "consumer staples"},
        igroups={"consumer discretionary distribution & retail",
                 "consumer staples distribution & retail", "consumer durables & apparel",
                 "consumer services", "automobiles & components",
                 "food, beverage & tobacco", "household & personal products",
                 "food & staples retailing", "retailing"}),
    "utilities": _sector(sectors={"utilities"}, igroups={"utilities"}),
    "realestate": _sector(
        sectors={"real estate"},
        igroups={"equity real estate investment trusts (reits)",
                 "real estate management & development", "real estate"}),
    "communications": _sector(
        sectors={"communication services"},
        igroups={"telecommunication services", "media & entertainment"}),
    # 'materials' is broader than mining (chemicals, packaging, building materials),
    # so those inclusions are coarse and get verified after the fetch.
    "mining": _sector(
        subind_kw={"metals & mining", "mining", "gold", "silver", "steel", "copper",
                   "aluminum", "aluminium", "coal", "precious metals",
                   "diversified metals"},
        fallback_sectors={"materials"}, igroups={"materials"},
        coarse_igroups={"materials"},
        yf_industry_kw={"mining", "gold", "silver", "copper", "aluminum", "steel",
                        "coal", "metals", "platinum", "uranium"}),
    # The 'telecommunication services' industry group IS telecom, so full-ASX
    # matches are precise (no coarse_igroups); only the ASX 200's whole-sector
    # fallback needs verifying.
    "telecom": _sector(
        subind_kw={"telecom", "wireless"},
        fallback_sectors={"communication services"},
        igroups={"telecommunication services"},
        yf_industry_kw={"telecom", "wireless"}),
}

SECTOR_ALIASES: Final[Mapping[str, str]] = {
    "tech": "technology", "it": "technology", "infotech": "technology",
    "finance": "financials", "financial": "financials", "banks": "financials",
    "health": "healthcare", "comms": "communications", "communication": "communications",
    "telco": "telecom", "telecoms": "telecom", "property": "realestate",
    "reit": "realestate", "reits": "realestate", "miners": "mining",
    "resources": "mining", "metals": "mining", "gold": "mining", "oil": "energy",
    "oilgas": "energy", "power": "utilities", "utility": "utilities",
    "industrial": "industrials", "staples": "consumer", "discretionary": "consumer",
}


# --- HTTP ----------------------------------------------------------------------
Fetcher: TypeAlias = Callable[[str], str]                       # url -> decoded body
FetcherFactory: TypeAlias = Callable[[], AbstractContextManager[Fetcher]]


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retry number ``attempt``: exponential backoff, raised
    to a numeric ``Retry-After`` if the server sent one, capped at HTTP_MAX_DELAY."""
    delay: float = HTTP_BACKOFF * 2.0 ** (attempt - 1)
    if retry_after is not None:
        with suppress(ValueError):     # an HTTP date: the backoff is close enough
            delay = max(delay, float(retry_after))
    return min(delay, HTTP_MAX_DELAY)


@contextmanager
def http_fetcher(*, sleep: Callable[[float], None] = time.sleep) -> Iterator[Fetcher]:
    """Yield ``fetch(url) -> text`` backed by one pooled ``requests.Session``.

    Transient failures (connection errors, timeouts, 429 and 5xx) are retried with a
    capped backoff; anything else raises at once. Once a host has used up its
    retries, the other pages on it fail immediately with the same error, so being
    offline costs seconds rather than a timeout per page. The session is shared by
    the loader's worker threads (pages on one host reuse a connection) and is closed
    when the block exits.
    """
    try:
        import requests
    except ImportError as exc:         # imported lazily: most runs never scrape
        raise UniverseError("--source/--sector lists need the 'requests' package:  "
                            "pip install requests") from exc
    transient = (requests.ConnectionError, requests.Timeout,
                 requests.exceptions.ChunkedEncodingError)

    gave_up: dict[str, Exception] = {}     # host -> the failure that ended its retries

    with requests.Session() as session:
        session.headers.update(HTTP_HEADERS)

        def fetch(url: str) -> str:
            host = urlsplit(url).netloc
            if (cause := gave_up.get(host)) is not None:
                raise cause
            for attempt in range(1, HTTP_ATTEMPTS + 1):
                failure: Exception
                try:
                    with session.get(url, timeout=HTTP_TIMEOUT) as response:
                        if response.status_code not in RETRY_STATUSES:
                            response.raise_for_status()
                            return response.text
                        retry_after = response.headers.get("Retry-After")
                        failure = requests.HTTPError(
                            f"HTTP {response.status_code} for url: {url}", response=response)
                except transient as exc:
                    failure, retry_after = exc, None
                if attempt == HTTP_ATTEMPTS:
                    gave_up[host] = failure
                    raise failure
                delay = _retry_delay(attempt, retry_after)
                _log.info("universe: %s (%s); retry %d of %d in %.0fs", url,
                          type(failure).__name__, attempt, HTTP_ATTEMPTS - 1, delay)
                sleep(delay)
            raise AssertionError("unreachable: the last attempt returns or raises")

        yield fetch


# --- Day cache -----------------------------------------------------------------
def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, so a crash or a concurrent run can
    never leave a half-written cache entry behind."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)    # already moved on success


class DayCache:
    """Per-calendar-day JSON cache: ``<root>/_universe/<YYYY-MM-DD>/<key>.json``.

    ``root=None`` turns every method into a no-op (``--no-cache``). Nothing in here
    raises: a cache that cannot be read or written is logged and skipped, because it
    must never be the reason a run fails.
    """

    def __init__(self, root: Path | None, *, today: Callable[[], date] = date.today) -> None:
        self._base = None if root is None else Path(root) / CACHE_SUBDIR
        self._today = today
        self._writable = True

    def load(self, key: str) -> Records | None:
        """Today's entry, or ``None``."""
        return None if self._base is None else self._read(self._today(), key)

    def load_stale(self, key: str) -> tuple[date, Records] | None:
        """The newest entry from the previous STALE_DAYS days, with its date."""
        today = self._today()
        for day, _ in sorted(self._day_dirs(), reverse=True):
            if (day < today and (today - day).days <= STALE_DAYS
                    and (records := self._read(day, key)) is not None):
                return day, records
        return None

    def save(self, key: str, records: Sequence[IndexRecord]) -> None:
        if self._base is None or not self._writable:
            return
        path = self._path(self._today(), key)
        payload = json.dumps({"schema": CACHE_SCHEMA, "index": key,
                              "records": [r.to_json() for r in records]},
                             separators=(",", ":"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, payload)
        except OSError as exc:
            self._writable = False     # say it once, not once per index
            _log.warning("universe: cannot write the constituent cache in %s (%s); "
                         "carrying on without it", path.parent, exc)

    def prune(self) -> None:
        """Delete day folders too old to serve even as a stale fallback."""
        today = self._today()
        for day, folder in self._day_dirs():
            if (today - day).days <= STALE_DAYS:
                continue
            try:
                for entry in (*folder.glob("*.json"), *folder.glob("*.tmp")):
                    entry.unlink(missing_ok=True)
                folder.rmdir()         # fails, harmlessly, if anything else is in it
                _log.debug("universe: pruned cache folder %s", folder)
            except OSError as exc:
                _log.debug("universe: could not prune %s (%s)", folder, exc)

    def _path(self, day: date, key: str) -> Path:
        assert self._base is not None
        return self._base / day.isoformat() / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.json"

    def _day_dirs(self) -> list[tuple[date, Path]]:
        if self._base is None:
            return []
        found: list[tuple[date, Path]] = []
        try:
            for entry in self._base.iterdir():
                try:
                    day = date.fromisoformat(entry.name)
                except ValueError:
                    continue           # not one of our day folders: leave it alone
                if day.isoformat() == entry.name:
                    found.append((day, entry))
        except OSError:
            return []                  # no cache folder yet
        return found

    def _read(self, day: date, key: str) -> Records | None:
        path = self._path(day, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA
                    or payload.get("index") != key):
                raise ValueError("not a current cache entry")
            records = tuple(IndexRecord.from_json(raw) for raw in payload["records"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # Corrupt, truncated or foreign file: treat as a miss and re-scrape.
            _log.warning("universe: ignoring unreadable cache entry %s (%s: %s)",
                         path, type(exc).__name__, exc)
            return None
        return records or None


# --- Loader --------------------------------------------------------------------
# What a failed scrape looks like: network or I/O trouble (requests' exceptions are
# OSErrors), an undecodable body, or a page whose layout changed. Anything else is a
# bug and is left to propagate.
_SCRAPE_ERRORS: Final = (OSError, ValueError, LayoutError)


class UniverseLoader:
    """Turns index keys into records, doing as little network work as possible.

    Lookup order per index: this instance's memo, today's disk cache, a live scrape,
    then the newest stale cache entry. Everything that needs the network is fetched
    in one concurrent batch over a single HTTP session.

    Thread-safe: ``load`` holds a lock, so overlapping callers queue up instead of
    scraping the same page twice. The worker threads only download and parse; all
    cache and memo writes happen on the calling thread.
    """

    def __init__(self, cache_dir: Path | None = DEFAULT_CACHE_DIR, *,
                 indexes: Mapping[str, IndexSpec] = INDEXES,
                 open_fetcher: FetcherFactory = http_fetcher,
                 max_workers: int = MAX_FETCH_WORKERS,
                 today: Callable[[], date] = date.today) -> None:
        self._indexes = indexes
        self._open_fetcher = open_fetcher
        self._max_workers = max(1, max_workers)
        self._today = today
        self._cache = DayCache(cache_dir, today=today)
        self._memo: dict[tuple[date, str], Records] = {}
        self._lock = threading.RLock()
        self._pruned = False

    def load(self, keys: Iterable[str]) -> dict[str, Records]:
        """Records for each index key. Raises the scrape error if one index cannot
        be loaded at all, or ``IndexLoadError`` naming every one if several cannot;
        the indexes that did load are cached first, so a rerun only repeats the rest."""
        wanted = tuple(dict.fromkeys(keys))
        with self._lock:
            day = self._today()
            loaded: dict[str, Records] = {}
            for key in wanted:
                if (records := self._lookup(day, key)) is not None:
                    loaded[key] = records
            if missing := [key for key in wanted if key not in loaded]:
                loaded.update(self._scrape(day, missing))
            return {key: loaded[key] for key in wanted}

    def _lookup(self, day: date, key: str) -> Records | None:
        spec = self._indexes[key]
        if isinstance(spec, StaticList):
            return tuple(IndexRecord(symbol) for symbol in spec.tickers)
        if (records := self._memo.get((day, key))) is not None:
            return records
        if (records := self._cache.load(key)) is not None:
            _log.info("universe: index=%s rows=%d source=cache", key, len(records))
            self._memo[(day, key)] = records
        return records

    def _scrape(self, day: date, keys: Sequence[str]) -> dict[str, Records]:
        outcomes = self._fetch_all(keys)
        loaded: dict[str, Records] = {}
        failures: dict[str, BaseException] = {}
        for key in keys:
            outcome = outcomes[key]
            if isinstance(outcome, BaseException):
                stale = self._cache.load_stale(key)
                if stale is None:
                    failures[key] = outcome
                    continue
                # Served but never re-saved: a stale list must not start looking fresh.
                stale_day, records = stale
                _log.warning("universe: could not refresh the '%s' list (%s: %s); "
                             "using the copy cached on %s", key,
                             type(outcome).__name__, outcome, stale_day)
            else:
                records = outcome
                self._cache.save(key, records)
            loaded[key] = self._memo[(day, key)] = records
        if loaded and not self._pruned:
            self._pruned = True
            self._cache.prune()
        if len(failures) == 1:
            raise next(iter(failures.values()))
        if failures:
            raise IndexLoadError(failures)
        return loaded

    def _fetch_all(self, keys: Sequence[str]) -> dict[str, Records | BaseException]:
        """Download and parse ``keys`` concurrently; map each to records or its error."""
        pool = ThreadPoolExecutor(max_workers=min(self._max_workers, len(keys)),
                                  thread_name_prefix="universe")
        try:
            with self._open_fetcher() as fetch:
                futures = {key: pool.submit(self._scrape_one, fetch, key) for key in keys}
                return {key: _outcome(future) for key, future in futures.items()}
        finally:
            # Normally a no-op (every future is done). On Ctrl-C it drops the queued
            # downloads instead of waiting out their timeouts.
            pool.shutdown(wait=False, cancel_futures=True)

    def _scrape_one(self, fetch: Fetcher, key: str) -> Records:
        spec = self._indexes[key]
        assert not isinstance(spec, StaticList)
        started = time.perf_counter()
        records = tuple(spec.parse(fetch(spec.url)))
        _log.info("universe: index=%s rows=%d source=network secs=%.2f",
                  key, len(records), time.perf_counter() - started)
        return records


def _outcome(future: Future[Records]) -> Records | BaseException:
    try:
        return future.result()
    except _SCRAPE_ERRORS as exc:
        return exc


# The registries below are zero-argument callables (the CLI's contract), so they
# need one process-wide loader to act on. It is the module's only mutable state.
_default_loader = UniverseLoader()


def configure(*, cache_dir: Path | None) -> None:
    """Start a fresh default loader caching under ``cache_dir`` (``None`` = no disk
    cache). The CLI calls this once per run, which also drops the previous run's memo
    so a long-lived GUI never serves yesterday's lists from memory."""
    global _default_loader
    _default_loader = UniverseLoader(cache_dir)


# --- Universes -----------------------------------------------------------------
def _resolve(universe: Universe, loader: UniverseLoader | None, *,
             need_sectors: bool = False) -> list[IndexRecord]:
    active = _default_loader if loader is None else loader
    loaded = active.load(part.index for part in universe.parts)
    out: list[IndexRecord] = []
    for part in universe.parts:
        records: Sequence[IndexRecord] = loaded[part.index]
        if need_sectors and not any(r.sector or r.igroup for r in records):
            raise LayoutError(f"The '{part.index}' list no longer carries a sector column "
                              "(layout changed?), so it cannot be filtered by sector.")
        if part.smallcap:
            if all(r.mcap_local is None for r in records):
                raise LayoutError(f"The '{part.index}' list no longer carries market caps "
                                  "(layout changed?), so its small caps cannot be picked out.")
            records = [r for r in records
                       if r.mcap_local is not None and r.mcap_local < AU_SMALLCAP_MAX_AUD]
        out.extend(records)
    return out


def universe_tickers(name: str, *, loader: UniverseLoader | None = None) -> list[str]:
    """Every ticker of the named universe (canonical name or alias), in order."""
    return [r.symbol for r in _resolve(_BY_NAME[name], loader)]


def region_records(name: str, *, loader: UniverseLoader | None = None) -> list[IndexRecord]:
    """Records, with GICS data, of a universe that can scope a ``--sector`` run."""
    universe = _BY_NAME[name]
    if not universe.sector_region:
        raise KeyError(name)
    return _resolve(universe, loader, need_sectors=True)


SOURCES: Final[dict[str, Callable[[], list[str]]]] = {
    name: partial(universe_tickers, name) for name in _BY_NAME
}
REGION_RECORDS: Final[dict[str, Callable[[], list[IndexRecord]]]] = {
    name: partial(region_records, name)
    for name, universe in _BY_NAME.items() if universe.sector_region
}


# --- Sector groups -------------------------------------------------------------
def resolve_sector_key(name: str) -> str:
    """Map a user-supplied ``--sector`` value (aliases included) to its canonical key."""
    key = re.sub(r"[\s_-]+", "", name.strip().lower())
    key = SECTOR_ALIASES.get(key, key)
    if key not in SECTORS:
        raise UniverseError(
            f"Unknown --sector '{name}'. Choose from: {', '.join(sorted(SECTORS))} "
            "(aliases: tech, finance, health, telco, miners, oil, property, ...).")
    return key


def tickers_for_sector(sector_key: str, region: str, *,
                       loader: UniverseLoader | None = None) -> list[str]:
    """Names in ``sector_key`` within region scope ``region``, from index records."""
    spec = SECTORS[sector_key]
    return [r.symbol for r in region_records(region, loader=loader) if spec.matches(r)]


def loose_sector_tickers(sector_key: str, region: str, *,
                         loader: UniverseLoader | None = None) -> set[str]:
    """Symbols that joined the group through a coarse match and so should be checked
    against yfinance's own industry after the fetch (``SectorSpec.is_coarse_match``)."""
    spec = SECTORS[sector_key]
    return {r.symbol for r in region_records(region, loader=loader)
            if spec.is_coarse_match(r)}


def verify_loose_industry(rows: list[dict[str, Any]], loose: set[str],
                          spec: SectorSpec) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop loose (coarsely matched) rows whose yfinance industry contradicts the
    group. Returns ``(kept, excluded)``. Rows outside ``loose`` are untouched, and a
    loose row with no industry data is kept: missing evidence is not a reason to drop.
    """
    keywords = spec.yf_industry_kw
    if not loose or not keywords:
        return rows, []
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        industry = _label(str(row.get("industry") or ""))
        known = bool(industry) and industry != NA
        if row["ticker"] in loose and known and not any(kw in industry for kw in keywords):
            excluded.append(row)
        else:
            kept.append(row)
    return kept, excluded


class _SourceArgs(Protocol):
    @property
    def source(self) -> str | None: ...


def describe_group(args: _SourceArgs, sector_key: str | None,
                   region: str | None) -> str | None:
    """One-line label of what group this run pulled, for the report header."""
    if sector_key:
        scope_region = region or "global"
        scope = " (US + Australia)" if scope_region == "global" else ""
        return f"sector **{sector_key}** · region **{scope_region}**{scope}"
    if args.source:
        return f"region/universe **{args.source}**"
    return None


def format_groups() -> str:
    """The ``--list-groups`` text: region sources and sector groups."""
    aliases = ", ".join(f"{alias}->{key}" for alias, key in sorted(SECTOR_ALIASES.items()))
    return "\n".join((
        "Region groups / named universes  (--source NAME):",
        "  United States : sp500/sap, sp400/midcap, sp600/smallcap,",
        "                  sp1500/us/america, america-smallcap",
        "  Australia     : australia/asx200/a200 (the 200), australia-smallcap,",
        "                  asx-all/australia-all (EVERY ASX listing, ~2,000 - big/slow)",
        "  Other markets : japan, uk, germany, hongkong, canada, india",
        "  Combos        : global, global-smallcap, international, world/everything",
        "",
        "Sector groups  (--sector NAME, spans US + Australia, or pin with --source):",
        f"  {', '.join(sorted(SECTORS))}",
        f"  aliases: {aliases}",
        "",
        "Examples:",
        "  stock_review.py --source australia-smallcap",
        "  stock_review.py --sector mining",
        "  stock_review.py --source australia --sector mining",
        "",
    ))


def print_groups() -> None:
    """Write the group listing to stdout (it is the command's output, not a log)."""
    sys.stdout.write(format_groups())


# --- Tickers file --------------------------------------------------------------
_TICKER_HEADERS: Final = frozenset({"ticker", "tickers", "symbol", "symbols", "code"})
_FIELD_SEPARATOR: Final = re.compile(r"[,;\t]")
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")


def _decode_tickers(raw: bytes, path: Path) -> str:
    if raw.startswith(_UTF16_BOMS):        # Notepad's "Unicode" / Excel "Unicode Text"
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw:
        raise UniverseError(f"Tickers file '{path}' is not text (a spreadsheet?). "
                            "Save it as CSV or plain text first.")
    # utf-8-sig drops the BOM Excel/Notepad put on "CSV UTF-8" files (it would
    # otherwise glue itself to the first ticker); fall back to cp1252 for files
    # saved with a legacy Windows code page instead of crashing on the decode.
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_tickers_text(text: str) -> list[str]:
    """Tickers from file text: one per line, or the ticker column of a CSV.

    Blank lines and ``#`` comments are skipped. A header row (``ticker``,
    ``Symbol,Name``, ...) is recognised before the first data line, even after
    leading comments, and tells us which column holds the tickers.
    """
    tickers: list[str] = []
    column = 0
    header_possible = True
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cells = [cell.strip().strip('"').strip("'") for cell in _FIELD_SEPARATOR.split(line)]
        if header_possible:
            header_possible = False
            hit = next((i for i, cell in enumerate(cells)
                        if cell.lower() in _TICKER_HEADERS), None)
            if hit is not None:
                column = hit
                continue
        if ticker := _cell(cells, column):
            tickers.append(ticker.upper())
    return tickers


def read_tickers_file(path: Path) -> list[str]:
    """Read a tickers file; any problem becomes a clean message, not a traceback."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TICKERS_FILE_BYTES + 1)
    except OSError as exc:
        raise UniverseError(f"Could not read tickers file '{path}': {exc}") from exc
    if len(raw) > MAX_TICKERS_FILE_BYTES:
        raise UniverseError(f"Tickers file '{path}' is larger than "
                            f"{MAX_TICKERS_FILE_BYTES // (1024 * 1024)} MB; "
                            "that is not a ticker list.")
    return parse_tickers_text(_decode_tickers(raw, Path(path)))
