"""Offline tests for review/universe.py: the table reader, the index parsers, the
loader (cache, concurrency, retries, stale fallback) and the sector group logic.

No sockets: pages are hand-written fixtures modelled on the real markup, and the
loader is given a fake fetcher. Run:  python tests/test_universe.py
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.review import universe as u
from stock_analyzer.review.universe import IndexRecord

TODAY = date(2026, 3, 10)

# Modelled on the live pages: linked header cells, a footnote marker, a <br>, a
# non-breaking space, a decoy table above and a change-history table below.
US_PAGE = """<html><head><style>table { color: red }</style>
<script>var t = "<table><tr><td>NOPE</td></tr></table>";</script></head><body>
<table class="wikitable"><tr><th>Year</th><th>Closing level</th></tr>
<tr><td>2024</td><td>5,881</td></tr><tr><td>2025</td><td>6,000</td></tr></table>
<table class="wikitable sortable" id="constituents"><tbody>
<tr><th><a href="/wiki/Ticker_symbol">Symbol</a><sup class="reference">[1]</sup></th>
<th>Security</th><th><a href="/wiki/GICS">GICS</a> Sector</th>
<th>GICS<br>Sub-Industry</th><th>Founded</th></tr>
<tr><td><a href="#">MMM</a>
</td><td>3M</td><td>Industrials</td><td>Industrial Conglomerates</td><td>1902</td></tr>
<tr><td>BRK.B</td><td>Berkshire</td><td>Financials</td><td>Multi-Sector Holdings</td>
<td>1839</td></tr>
<tr><td>NEM</td><td>Newmont</td><td>Materials</td><td>Gold</td><td>1921</td></tr>
<tr><td>T</td><td>AT&amp;T</td><td>Communication&nbsp;Services</td>
<td>Integrated Telecommunication Services</td><td>1983</td></tr>
<tr><td></td><td>blank symbol row</td><td>x</td><td>y</td><td>z</td></tr>
</tbody></table>
<table class="wikitable" id="changes"><tr><th>Date</th><th colspan="2">Added</th></tr>
<tr><th>Ticker</th><th>Security</th></tr><tr><td>2025</td><td>ZZZ</td><td>Zed</td></tr></table>
</body></html>"""

ASX_PAGE = """<table class="wikitable sortable"><tr><th>Code</th><th>Company</th>
<th>Sector</th><th>Market Capitalisation (A$)</th></tr>
<tr><td>BHP</td><td>BHP Group</td><td>Materials</td><td>220,000,000,000</td></tr>
<tr><td>IGO</td><td>IGO</td><td>Materials</td><td>3,900,000,000</td></tr>
<tr><td>TPG</td><td>TPG Telecom</td><td>Communication Services</td><td>4,100,000,000</td></tr>
<tr><td>REA</td><td>REA Group</td><td>Communication Services</td><td></td></tr>
</table>"""

ASX_CSV = """ASX listed companies as at Tue Mar 10 10:00:00 AEDT 2026

Company name,ASX code,GICS industry group
"NANOSONICS LIMITED","NAN","Health Care Equipment & Services"
"FORTESCUE, AND SONS","FMG","Materials"
"SOME ETF","ETF","Not Applic"
"TELSTRA GROUP","TLS","Telecommunication Services"
"""


def _spec(key, **changes):
    return replace(u.INDEXES[key], **changes)


class FakeWeb:
    """A fake fetcher: serves bodies by URL, counts calls, can fail or block."""

    def __init__(self, bodies, *, barrier=None):
        self.bodies = dict(bodies)
        self.calls: list[str] = []
        self.barrier = barrier
        self.opened = self.closed = 0
        self._lock = threading.Lock()

    def fetch(self, url):
        with self._lock:
            self.calls.append(url)
        if self.barrier is not None:
            self.barrier.wait(timeout=5)     # only passes if fetches overlap
        body = self.bodies[url]
        if isinstance(body, BaseException):
            raise body
        return body

    @contextmanager
    def open(self):
        self.opened += 1
        try:
            yield self.fetch
        finally:
            self.closed += 1


def _mini_indexes():
    """Three tiny scraped indexes + a static one, with test-sized row floors."""
    return {
        "sp500": _spec("sp500", url="mem://sp500", min_rows=2),
        "asx200": _spec("asx200", url="mem://asx200", min_rows=2),
        "asx-all": _spec("asx-all", url="mem://asx-all", min_rows=2),
        "curated": u.INDEXES["curated"],
    }


BODIES = {"mem://sp500": US_PAGE, "mem://asx200": ASX_PAGE, "mem://asx-all": ASX_CSV}


def _loader(cache_dir, web, *, today=TODAY, **kw):
    return u.UniverseLoader(cache_dir, indexes=_mini_indexes(), open_fetcher=web.open,
                            today=lambda: today, **kw)


class _Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


@contextmanager
def _logs():
    logger, handler = logging.getLogger("stock_review.universe"), _Captured()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False                 # keep expected warnings off the console
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _raises(exc_type, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


# --- registries ---------------------------------------------------------------
def test_registries_keep_every_public_name():
    """The CLI/GUI choices are a contract: no --source or region may vanish."""
    assert sorted(u.SOURCES) == sorted("""a200 all america america-smallcap asx asx-all
        asx200 au-smallcap australia australia-all australia-smallcap canada curated
        dax everything ftse100 germany global global-smallcap hongkong hsi india
        international intl japan midcap nifty50 nikkei sap smallcap sp1500 sp400 sp500
        sp600 tsx60 uk us us-smallcap world world-smallcap""".split())
    assert sorted(u.REGION_RECORDS) == sorted("""a200 america america-smallcap asx
        asx-all asx200 au-smallcap australia australia-all australia-smallcap global
        global-smallcap midcap sap smallcap sp1500 sp400 sp500 sp600 us us-smallcap
        world-smallcap""".split())
    assert len(u.SECTORS) == 12 and set(u.SECTOR_ALIASES.values()) <= set(u.SECTORS)
    names = [n for key, uni in u.UNIVERSES.items() for n in (key, *uni.aliases)]
    assert len(names) == len(set(names)), "an alias is defined twice"
    for uni in u.UNIVERSES.values():
        assert all(part.index in u.INDEXES for part in uni.parts)
    assert u.SOURCES["curated"]() == list(u.DEFAULT_TICKERS)      # no network needed
    assert u.SOURCES["japan"]() == list(u.NIKKEI_CURATED)
    print("OK registries: 40 sources, 22 sector regions, 12 sectors, no dangling names")


# --- HTML table reader --------------------------------------------------------
def test_table_reader_text_cleanup_and_order():
    tables = list(u.iter_html_tables(US_PAGE))
    assert [t.attrs.get("id", "") for t in tables] == ["", "constituents", "changes"]
    header = [text for text, _, _ in tables[1].rows[0]]
    # link text joined, footnote dropped, <br> and &nbsp; become one space
    assert header == ["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Founded"]
    assert tables[1].rows[4][1][0] == "AT&T"
    assert tables[1].rows[4][2][0] == "Communication Services"
    print("OK table reader: script/style ignored, entities, <br>, footnotes, order")


def test_table_reader_spans_nesting_and_broken_markup():
    grid = u._expand_spans(list(u.iter_html_tables(
        "<table><tr><th rowspan=2>A</th><th colspan=2>B</th></tr>"
        "<tr><td>b1</td><td>b2</td></tr><tr><td>x</td><td>y</td><td>z</td></tr></table>"
    ))[0].rows)
    assert grid == [["A", "B", "B"], ["A", "b1", "b2"], ["x", "y", "z"]], grid
    outer, inner = u.iter_html_tables(
        "<table><tr><td>o1<table><tr><td>i1</td></tr></table></td><td>o2</td></tr></table>")
    assert [[c[0] for c in r] for r in outer.rows] == [["o1", "o2"]]
    assert [[c[0] for c in r] for r in inner.rows] == [["i1"]]
    # no closing tags at all, an unclosed <sup>, absurd spans, a truncated page
    broken = list(u.iter_html_tables(
        "<table><tr><td rowspan=-3 colspan=junk>a<sup>[1]<td>b<tr><td>c<td colspan=9999>d"))
    assert [[c[0] for c in r] for r in broken[0].rows] == [["a", "b"], ["c", "d"]]
    assert broken[0].rows[0][0][1:] == (1, 1) and broken[0].rows[1][1][2] == u._MAX_SPAN
    print("OK table reader: rowspan/colspan, nested tables, unclosed/truncated markup")


def test_table_reader_stops_once_the_caller_has_enough():
    """Tables are produced lazily, so the long tail of a page is never parsed."""
    fed: list[int] = []
    original = u._TableReader.feed

    def counting_feed(self, data):
        fed.append(len(data))
        return original(self, data)

    page = "<table><tr><td>first</td></tr></table>" + "<p>filler</p>" * 5000
    u._TableReader.feed = counting_feed
    try:
        first = next(u.iter_html_tables(page, chunk_size=256))
    finally:
        u._TableReader.feed = original
    assert first.rows[0][0][0] == "first"
    assert sum(fed) <= 256 < len(page), (sum(fed), len(page))
    print(f"OK table reader: lazy ({sum(fed)} of {len(page)} chars parsed for table 1)")


# --- index parsers ------------------------------------------------------------
def test_us_table_parse():
    recs = _spec("sp500", min_rows=2).parse(US_PAGE)
    assert [r.symbol for r in recs] == ["MMM", "BRK-B", "NEM", "T"]       # blank row skipped
    assert recs[1] == IndexRecord("BRK-B", sector="financials",
                                  subind="multi-sector holdings", has_subind=True)
    assert recs[3].sector == "communication services"
    assert all(r.mcap_local is None for r in recs)        # never 0 for "not published"
    # the real spec's row floor rejects a half-loaded page instead of caching it
    err = _raises(u.LayoutError, u.INDEXES["sp500"].parse, US_PAGE)
    assert "layout changed" in str(err) and isinstance(err, SystemExit)
    _raises(u.LayoutError, _spec("sp500", min_rows=2).parse, "<html>captive portal</html>")
    print("OK US table: symbols cleaned, GICS labels normalised, row floor enforced")


def test_missing_optional_columns_degrade_gracefully():
    """Losing the sector columns must not take the plain ticker list down."""
    page = US_PAGE.replace("GICS</a> Sector", "GICS</a> Industry").replace(
        "GICS<br>Sub-Industry", "Sub group")
    recs = _spec("sp500", min_rows=2).parse(page)
    assert [r.symbol for r in recs] == ["MMM", "BRK-B", "NEM", "T"]
    assert not any(r.sector or r.subind or r.has_subind for r in recs)
    web = FakeWeb({**BODIES, "mem://sp500": page})
    loader = _loader(None, web)
    assert u.universe_tickers("sp500", loader=loader) == ["MMM", "BRK-B", "NEM", "T"]
    err = _raises(u.LayoutError, u.tickers_for_sector, "energy", "sp500", loader=loader)
    assert "sector column" in str(err)
    # likewise the small-cap sleeve when the market-cap column disappears
    web = FakeWeb({**BODIES, "mem://asx200": ASX_PAGE.replace("Market Capitalisation (A$)", "Cap")})
    err = _raises(u.LayoutError, u.universe_tickers, "australia-smallcap",
                  loader=_loader(None, web))
    assert "market caps" in str(err)
    print("OK missing sector / market-cap column: tickers survive, groups say why not")


def test_international_tables_and_market_caps():
    recs = _spec("asx200", min_rows=2).parse(ASX_PAGE)
    assert [(r.symbol, r.sector, r.mcap_local) for r in recs] == [
        ("BHP.AX", "materials", 220e9), ("IGO.AX", "materials", 3.9e9),
        ("TPG.AX", "communication services", 4.1e9),
        ("REA.AX", "communication services", None)]              # blank cap stays None
    assert not any(r.has_subind for r in recs)

    def one(key, header, cell):
        html = f"<table class='wikitable'><tr><th>Name</th><th>{header}</th></tr>" \
               f"<tr><td>Co</td><td>{cell}</td></tr><tr><td>Co2</td><td></td></tr></table>"
        return [r.symbol for r in _spec(key, min_rows=2).parse(html)]

    assert one("hsi", "Ticker", "<a>SEHK</a><span>:</span>&nbsp;<a>5</a>") == ["0005.HK"]
    assert one("ftse100", "Ticker", "BT.A") == ["BT-A.L"]
    assert one("ftse100", "EPIC", "RR.") == ["RR-.L"]
    assert one("dax", "Ticker", "ADS.DE") == ["ADS.DE"]
    assert one("tsx60", "Symbol", "BAM.A") == ["BAM-A.TO"]
    assert one("nifty50", "Symbol", "RELIANCE <sup>[2]</sup>") == ["RELIANCE.NS"]
    print("OK international tables: per-exchange symbols, caps parsed, blanks stay None")


def test_asx_listing_csv():
    recs = _spec("asx-all", min_rows=2).parse(ASX_CSV)
    assert [(r.symbol, r.igroup) for r in recs] == [
        ("NAN.AX", "health care equipment & services"),    # the code "NAN" survives
        ("FMG.AX", "materials"),                           # quoted comma in the name
        ("ETF.AX", ""),                                    # 'Not Applic' -> no group
        ("TLS.AX", "telecommunication services")]
    assert "only 4 rows" in str(_raises(u.LayoutError, u.INDEXES["asx-all"].parse, ASX_CSV))
    err = _raises(u.LayoutError, _spec("asx-all", min_rows=1).parse, "a,b\n1,2\n")
    assert "code column" in str(err)
    print("OK ASX listing CSV: preamble skipped, NAN kept, ETFs ungrouped, floors")


# --- sector groups ------------------------------------------------------------
def test_sector_groups_and_coarse_matches():
    web = FakeWeb(BODIES)
    loader = _loader(None, web)
    indexes = _mini_indexes()
    assert u.tickers_for_sector("materials", "sp500", loader=loader) == ["NEM"]
    assert u.tickers_for_sector("financials", "sp500", loader=loader) == ["BRK-B"]
    # sub-industry concept: precise on the US table (NEM is 'gold'; 3M is not)
    assert u.tickers_for_sector("mining", "sp500", loader=loader) == ["NEM"]
    assert u.tickers_for_sector("telecom", "sp500", loader=loader) == ["T"]
    assert u.loose_sector_tickers("mining", "sp500", loader=loader) == set()
    # ASX 200 has only a sector column -> whole-sector fallback, flagged as coarse
    assert u.tickers_for_sector("mining", "asx200", loader=loader) == ["BHP.AX", "IGO.AX"]
    assert u.loose_sector_tickers("mining", "asx200", loader=loader) == {"BHP.AX", "IGO.AX"}
    assert u.loose_sector_tickers("materials", "asx200", loader=loader) == set()
    # full-ASX CSV: industry group; 'materials' is coarse for mining, telecom is exact
    assert u.tickers_for_sector("mining", "asx-all", loader=loader) == ["FMG.AX"]
    assert u.loose_sector_tickers("mining", "asx-all", loader=loader) == {"FMG.AX"}
    assert u.tickers_for_sector("telecom", "asx-all", loader=loader) == ["TLS.AX"]
    assert u.loose_sector_tickers("telecom", "asx-all", loader=loader) == set()
    assert u.tickers_for_sector("healthcare", "asx-all", loader=loader) == ["NAN.AX"]
    # small-cap tail: under A$5bn, and a missing cap is never treated as small
    assert u.universe_tickers("australia-smallcap", loader=loader) == ["IGO.AX", "TPG.AX"]
    assert len(web.calls) == len(indexes) - 1, web.calls      # each page fetched once
    _raises(KeyError, u.region_records, "japan", loader=loader)   # no sector data there
    print("OK sector groups: GICS sector, sub-industry, industry-group and coarse paths")


def test_verify_loose_industry():
    rows = [{"ticker": "BHP.AX", "industry": "Other Industrial Metals & Mining"},
            {"ticker": "ORI.AX", "industry": "Specialty Chemicals"},
            {"ticker": "IGO.AX", "industry": "n/a"},
            {"ticker": "S32.AX", "industry": None},
            {"ticker": "DOW", "industry": "Chemicals"}]            # precise: untouched
    loose = {"BHP.AX", "ORI.AX", "IGO.AX", "S32.AX"}
    kept, excluded = u.verify_loose_industry(rows, loose, u.SECTORS["mining"])
    assert [r["ticker"] for r in excluded] == ["ORI.AX"]
    assert [r["ticker"] for r in kept] == ["BHP.AX", "IGO.AX", "S32.AX", "DOW"]
    assert u.verify_loose_industry(rows, set(), u.SECTORS["mining"]) == (rows, [])
    assert u.verify_loose_industry(rows, loose, u.SECTORS["energy"]) == (rows, [])
    print("OK precision pass: contradicted rows dropped, missing evidence kept")


def test_sector_keys_group_labels_and_listing():
    assert u.resolve_sector_key(" Tech ") == "technology"
    assert u.resolve_sector_key("real-estate") == "realestate"
    assert u.resolve_sector_key("Oil_Gas") == "energy"
    err = _raises(SystemExit, u.resolve_sector_key, "crypto")
    assert "Unknown --sector 'crypto'" in str(err) and "mining" in str(err)
    args = type("Args", (), {"source": "uk"})()
    assert u.describe_group(args, "mining", None) == \
        "sector **mining** · region **global** (US + Australia)"
    assert u.describe_group(args, "mining", "australia") == \
        "sector **mining** · region **australia**"
    assert u.describe_group(args, None, None) == "region/universe **uk**"
    assert u.describe_group(type("Args", (), {"source": None})(), None, None) is None
    text = u.format_groups()
    assert text.endswith("--source australia --sector mining\n")
    assert all(key in text for key in u.SECTORS) and "tech->technology" in text
    print("OK sector keys/aliases, report group label, --list-groups text")


# --- loader -------------------------------------------------------------------
def test_loader_fetches_concurrently_and_keeps_order():
    barrier = threading.Barrier(3)            # deadlocks unless 3 fetches overlap
    web = FakeWeb(BODIES, barrier=barrier)
    loaded = _loader(None, web).load(["asx-all", "curated", "sp500", "asx200", "sp500"])
    assert list(loaded) == ["asx-all", "curated", "sp500", "asx200"]     # asked order
    assert sorted(web.calls) == sorted(BODIES) and not barrier.broken
    assert (web.opened, web.closed) == (1, 1)             # one session, and it is closed
    assert [r.symbol for r in loaded["sp500"]] == ["MMM", "BRK-B", "NEM", "T"]
    print("OK loader: 3 pages fetched in parallel over one session, order preserved")


def test_loader_memo_and_day_cache():
    with tempfile.TemporaryDirectory() as d:
        web = FakeWeb(BODIES)
        loader = _loader(Path(d), web)
        first = u.universe_tickers("sp500", loader=loader)
        u.tickers_for_sector("mining", "sp500", loader=loader)       # same page, memo
        assert web.calls == ["mem://sp500"]
        entry = Path(d) / "_universe" / TODAY.isoformat() / "sp500.json"
        saved = json.loads(entry.read_text(encoding="utf-8"))
        assert saved["schema"] == u.CACHE_SCHEMA and saved["index"] == "sp500"
        assert saved["records"][0] == {"symbol": "MMM", "sector": "industrials",
                                       "subind": "industrial conglomerates",
                                       "has_subind": True}          # defaults left out
        assert not list(entry.parent.glob("*.tmp"))                 # atomic, no debris
        rerun = FakeWeb({})                                         # any fetch -> KeyError
        assert u.universe_tickers("sp500", loader=_loader(Path(d), rerun)) == first
        assert u.region_records("sp500", loader=_loader(Path(d), rerun)) == \
            u.region_records("sp500", loader=loader)                # full round trip
        assert rerun.calls == []
        tomorrow = FakeWeb(BODIES)                                  # a new day re-scrapes
        u.universe_tickers("sp500", loader=_loader(Path(d), tomorrow,
                                                   today=TODAY + timedelta(days=1)))
        assert tomorrow.calls == ["mem://sp500"]
    print("OK loader: one fetch per page per day, compact atomic cache, round trip")


def test_loader_no_cache_and_unwritable_cache():
    with tempfile.TemporaryDirectory() as d:
        web = FakeWeb(BODIES)
        u.universe_tickers("sp500", loader=_loader(None, web))       # --no-cache
        u.universe_tickers("sp500", loader=_loader(None, web))
        assert len(web.calls) == 2 and not list(Path(d).iterdir())
        blocker = Path(d) / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")                    # mkdir will fail
        with _logs() as lines:
            got = _loader(blocker, FakeWeb(BODIES)).load(["sp500", "asx200"])
        assert len(got["sp500"]) == 4 and len(got["asx200"]) == 4    # run still succeeds
        warned = [line for line in lines if "cannot write the constituent cache" in line]
        assert len(warned) == 1, lines                               # said once, not twice
    print("OK loader: --no-cache writes nothing; an unwritable cache only warns")


def test_loader_ignores_corrupt_and_foreign_cache_entries():
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "_universe" / TODAY.isoformat()
        folder.mkdir(parents=True)
        for junk in ('{"schema": 2, "index": "sp500", "records": [{"symbol": "AAA"',   # cut off
                     '["AAA", "BBB"]',                                 # the old format
                     '{"schema": 2, "index": "sp400", "records": [{"symbol": "AAA"}]}',
                     '{"schema": 2, "index": "sp500", "records": [{"sector": "x"}]}',
                     '{"schema": 2, "index": "sp500", "records": []}'):
            (folder / "sp500.json").write_text(junk, encoding="utf-8")
            web = FakeWeb(BODIES)
            with _logs():
                got = u.universe_tickers("sp500", loader=_loader(Path(d), web))
            assert got == ["MMM", "BRK-B", "NEM", "T"] and web.calls == ["mem://sp500"], junk
    print("OK loader: truncated / old-format / mismatched cache files are re-scraped")


def test_loader_falls_back_to_a_recent_stale_copy():
    with tempfile.TemporaryDirectory() as d:
        yesterday = TODAY - timedelta(days=1)
        u.universe_tickers("sp500", loader=_loader(Path(d), FakeWeb(BODIES), today=yesterday))
        down = FakeWeb({**BODIES, "mem://sp500": ConnectionError("offline")})
        with _logs() as lines:
            loader = _loader(Path(d), down)
            got = u.universe_tickers("sp500", loader=loader)
            u.universe_tickers("sp500", loader=loader)                # no second attempt
        assert got == ["MMM", "BRK-B", "NEM", "T"] and down.calls == ["mem://sp500"]
        assert any("WARNING" in line and "ConnectionError: offline" in line
                   and yesterday.isoformat() in line for line in lines), lines
        # never re-saved under today's date: it must not start looking fresh
        assert not (Path(d) / "_universe" / TODAY.isoformat() / "sp500.json").exists()
        # a layout change falls back the same way
        changed = FakeWeb({**BODIES, "mem://sp500": "<html>redesigned</html>"})
        with _logs():
            assert len(u.universe_tickers("sp500", loader=_loader(Path(d), changed))) == 4
        # too old to trust -> the real error surfaces
        late = TODAY + timedelta(days=u.STALE_DAYS)
        err = _raises(ConnectionError, u.universe_tickers, "sp500",
                      loader=_loader(Path(d), down, today=late))
        assert "offline" in str(err)
        # and with the cache off there is nothing to fall back on
        _raises(ConnectionError, u.universe_tickers, "sp500", loader=_loader(None, down))
    print(f"OK loader: scrape failure served from a <={u.STALE_DAYS}-day-old copy, loudly")


def test_loader_partial_failures_are_cached_and_reported():
    with tempfile.TemporaryDirectory() as d:
        web = FakeWeb({**BODIES, "mem://asx200": TimeoutError("slow"),
                       "mem://asx-all": "<html>not a csv</html>"})
        err = _raises(u.IndexLoadError, _loader(Path(d), web).load,
                      ["sp500", "asx200", "asx-all"])
        assert set(err.failures) == {"asx200", "asx-all"}
        assert "asx200: TimeoutError: slow" in str(err) and "asx-all: LayoutError" in str(err)
        same = u.IndexLoadError({"a": OSError("down a"), "b": ValueError("bad"),
                                 "c": OSError("down c")})
        assert str(same) == "a, c: OSError: down a; b: ValueError: bad"   # one per kind
        # the page that did load was cached, so the rerun only repeats the failures
        fixed = FakeWeb(BODIES)
        assert len(_loader(Path(d), fixed).load(["sp500", "asx200", "asx-all"])) == 3
        assert sorted(fixed.calls) == ["mem://asx-all", "mem://asx200"]
        # a single failure keeps its own type (the CLI words those messages itself)
        one = FakeWeb({**BODIES, "mem://asx200": TimeoutError("slow")})
        _raises(TimeoutError, _loader(None, one).load, ["sp500", "asx200"])
        # a bug is not a scrape failure: it propagates untouched
        bug = FakeWeb({**BODIES, "mem://sp500": RuntimeError("bug")})
        _raises(RuntimeError, _loader(None, bug).load, ["sp500"])
        assert bug.closed == 1                                       # session still closed
    print("OK loader: successes cached before raising; every failed index is named")


def test_cache_prunes_only_its_own_old_folders():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "_universe"
        old, recent = TODAY - timedelta(days=30), TODAY - timedelta(days=2)
        for day in (old, recent):
            (base / day.isoformat()).mkdir(parents=True)
            (base / day.isoformat() / "fetch_index_records_sp500.json").write_text("[]")
        (base / "notes").mkdir()
        (base / "notes" / "keep.json").write_text("{}")
        mixed = base / (TODAY - timedelta(days=40)).isoformat()
        mixed.mkdir()
        (mixed / "mine.txt").write_text("not ours")
        (mixed / "sp500.json").write_text("{}")
        u.universe_tickers("sp500", loader=_loader(Path(d), FakeWeb(BODIES)))
        assert not (base / old.isoformat()).exists()                 # too old: gone
        assert (base / recent.isoformat()).exists()                  # still a fallback
        assert (base / "notes" / "keep.json").exists()               # not a day folder
        assert (mixed / "mine.txt").exists() and not (mixed / "sp500.json").exists()
        assert (base / TODAY.isoformat() / "sp500.json").exists()
    print("OK cache: day folders older than the fallback window are pruned, nothing else")


def test_configure_swaps_the_default_loader():
    saved = u._default_loader
    try:
        with tempfile.TemporaryDirectory() as d:
            u.DayCache(Path(d)).save("ftse100", [IndexRecord("AAA.L"), IndexRecord("BBB.L")])
            u.configure(cache_dir=Path(d))
            assert u._default_loader is not saved
            assert u.SOURCES["uk"]() == ["AAA.L", "BBB.L"]           # served from that dir
            assert u.SOURCES["ftse100"]() == ["AAA.L", "BBB.L"]
    finally:
        u._default_loader = saved
    print("OK configure(): the zero-argument SOURCES callables follow the configured cache")


# --- HTTP ---------------------------------------------------------------------
def test_http_fetcher_retries_transient_failures_only():
    import requests

    def response(status, body="", **headers):
        resp = requests.Response()
        resp.status_code, resp._content, resp.url = status, body.encode(), "mem://x"
        resp._content_consumed = True          # a hand-built response has no socket
        resp.headers.update(headers)
        return resp

    class FakeSession:
        script: list = []
        closed = 0

        def __init__(self):
            self.headers, self.calls = {}, 0

        def get(self, url, timeout):
            assert timeout == u.HTTP_TIMEOUT
            type(self).last = self
            self.calls += 1
            step = type(self).script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            type(self).closed += 1

    def run(script):
        FakeSession.script, sleeps = list(script), []
        with _logs() as lines, u.http_fetcher(sleep=sleeps.append) as fetch:
            try:
                return fetch("mem://x"), sleeps, lines
            except Exception as exc:  # noqa: BLE001 - the test inspects whatever came out
                return exc, sleeps, lines

    real, requests.Session = requests.Session, FakeSession
    try:
        body, sleeps, lines = run([response(429, **{"Retry-After": "4"}),
                                   requests.ConnectionError("reset"), response(200, "ok")])
        assert body == "ok" and sleeps == [4.0, 2.0], (body, sleeps)
        assert FakeSession.last.headers == u.HTTP_HEADERS and FakeSession.last.calls == 3
        assert sum("retry" in line for line in lines) == 2
        body, sleeps, _ = run([response(503, **{"Retry-After": "86400"}), response(200, "ok")])
        assert sleeps == [u.HTTP_MAX_DELAY]                  # a huge Retry-After is capped
        err, sleeps, _ = run([response(404)])
        assert isinstance(err, requests.HTTPError) and sleeps == []      # not transient
        err, sleeps, _ = run([requests.Timeout("t")] * u.HTTP_ATTEMPTS)
        assert isinstance(err, requests.Timeout) and len(sleeps) == u.HTTP_ATTEMPTS - 1
        err, sleeps, _ = run([response(429)] * u.HTTP_ATTEMPTS)
        assert isinstance(err, requests.HTTPError) and "429" in str(err)
        assert FakeSession.closed == 5                       # every session was closed
        # a host that ran out of retries is not asked again in the same batch
        FakeSession.script = [requests.ConnectionError("down")] * u.HTTP_ATTEMPTS \
            + [response(200, "other host")]
        with _logs(), u.http_fetcher(sleep=lambda _: None) as fetch:
            first = _raises(requests.ConnectionError, fetch, "https://a.example/1")
            again = _raises(requests.ConnectionError, fetch, "https://a.example/2")
            assert again is first and FakeSession.last.calls == u.HTTP_ATTEMPTS
            assert fetch("https://b.example/1") == "other host"
    finally:
        requests.Session = real
    assert u._retry_delay(1, "Wed, 21 Oct 2026 07:28:00 GMT") == u.HTTP_BACKOFF
    print("OK http: 429/5xx/timeouts retried with capped backoff; 404 and a dead host fail fast")


# --- tickers file -------------------------------------------------------------
def test_tickers_file_validation():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "list.txt"
        f.write_bytes("Ticker;Name\r\naapl;Apple\r\n\r\n# note\r\nbhp.ax;BHP\r\n".encode("utf-16"))
        assert u.read_tickers_file(f) == ["AAPL", "BHP.AX"]          # Notepad "Unicode"
        f.write_bytes(b"PK\x03\x04\x00\x00\x08\x00spreadsheet")
        err = _raises(SystemExit, u.read_tickers_file, f)
        assert "not text" in str(err) and "list.txt" in str(err)
        f.write_bytes(b"A\n" * (u.MAX_TICKERS_FILE_BYTES // 2 + 1))
        assert "larger than 5 MB" in str(_raises(SystemExit, u.read_tickers_file, f))
        err = _raises(SystemExit, u.read_tickers_file, Path(d) / "missing.csv")
        assert "Could not read tickers file" in str(err)
        err = _raises(SystemExit, u.read_tickers_file, Path(d))        # a folder
        assert "Could not read tickers file" in str(err)
    assert u.parse_tickers_text("\n# c\n'aapl'\t x\n\"msft\"\nticker\n,skipme\n") == \
        ["AAPL", "MSFT", "TICKER"]            # a header word after data is just data
    assert u.parse_tickers_text("Name,Code\nApple,AAPL\nshort\n") == ["AAPL"]
    assert u.parse_tickers_text("") == []
    print("OK tickers file: UTF-16, binary and oversize rejected, clean read errors")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} universe tests...\n")
    for fn in fns:
        fn()
    print("\nALL UNIVERSE TESTS PASSED")


if __name__ == "__main__":
    _run_all()
