"""Hand-verifiable tests for the pure helpers and report builder in stock_review.py.

Runs as a plain script (no pytest needed):  .venv313/Scripts/python tests/test_stock_review.py
Also works under pytest if installed.

Per project memory the committed .venv is broken; use .venv313 (Python 3.13).

Scope (Phase 0 safety net — pins CURRENT behaviour before the refactor):
  * the pure, honesty-critical formatters (money/price/pct/ratio and the lean
    _f/_fpct/_fbn family) — blank vs "n/a" for missing, magnitude scaling,
  * symbol normalisation (clean_symbol, _intl_symbol) incl. the BRK.B / 7203.T /
    pence / digits-only edge cases the docstrings call out,
  * dividend_yield_fraction's source-preference branches,
  * size_bucket boundaries,
  * a GOLDEN-FILE test on build_markdown (lean + rich) that asserts the exact CSV
    grid, so any future header/row drift (the four parallel column lists) is caught.
"""
from __future__ import annotations

import sys
from pathlib import Path

# stock_review.py lives at the repo root (one level up from tests/).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import stock_review as sr

GOLDEN = Path(__file__).resolve().parent / "golden"


# =============================================================================
# Symbol normalisation
# =============================================================================
def test_clean_symbol():
    # US class shares: dot -> dash
    assert sr.clean_symbol("BRK.B") == "BRK-B"
    assert sr.clean_symbol("BF.B") == "BF-B"
    # genuine exchange suffixes are kept intact
    assert sr.clean_symbol("7203.T") == "7203.T"
    assert sr.clean_symbol("BHP.AX") == "BHP.AX"
    assert sr.clean_symbol("ADS.DE") == "ADS.DE"
    assert sr.clean_symbol("RIO.L") == "RIO.L"
    # expanded allowlist (CODE-06): Euronext / Nordics / Asia
    assert sr.clean_symbol("ASML.AS") == "ASML.AS"
    assert sr.clean_symbol("NESN.SW") == "NESN.SW"
    assert sr.clean_symbol("005930.KS") == "005930.KS"
    assert sr.clean_symbol("D05.SI") == "D05.SI"
    # fallback by suffix length: unknown 1-letter = US class share -> dash;
    # unknown 2+-letter = exchange-shaped -> keep the dot
    assert sr.clean_symbol("HEI.A") == "HEI-A"
    assert sr.clean_symbol("ABC.XY") == "ABC.XY"
    # case / whitespace normalisation
    assert sr.clean_symbol("  aapl ") == "AAPL"
    print("OK clean_symbol: class-share dot->dash, exchange suffix kept, "
          "length-aware fallback")


def test_intl_symbol():
    idx = sr.INTL_INDEXES
    # Hong Kong: digits-only + zero-pad to 4 + .HK  ("SEHK: 700" -> "0700.HK")
    assert sr._intl_symbol("SEHK: 700", idx["hsi"]) == "0700.HK"
    assert sr._intl_symbol("5", idx["hsi"]) == "0005.HK"
    # UK / Canada: dot -> dash, then suffix
    assert sr._intl_symbol("BT.A", idx["ftse100"]) == "BT-A.L"
    # Germany: keep dots (ticker already carries the .DE/.PA suffix)
    assert sr._intl_symbol("ADS.DE", idx["dax"]) == "ADS.DE"
    # Australia default: strip stray trailing text, append suffix
    assert sr._intl_symbol("BHP", idx["asx200"]) == "BHP.AX"
    assert sr._intl_symbol("CBA (Commonwealth)", idx["asx200"]) == "CBA.AX"
    # empty stays empty
    assert sr._intl_symbol("", idx["asx200"]) == ""
    print("OK _intl_symbol: digits-only/zfill, dot->dash, keep-dots, suffix")


# =============================================================================
# Dividend yield source preference  (pins CURRENT behaviour; Phase 3 revisits
# the last-resort guess branch to return None instead of scaling)
# =============================================================================
def test_dividend_yield_prefers_rate_over_price():
    assert sr.dividend_yield_fraction({"dividendRate": 2.0}, 100.0) == 0.02
    # falls back to trailingAnnualDividendRate when dividendRate is absent
    assert sr.dividend_yield_fraction({"trailingAnnualDividendRate": 3.0}, 100.0) == 0.03
    print("OK dividend_yield: annual rate / price preferred")


def test_dividend_yield_trailing_fraction_gate():
    # a sane trailing-annual fraction is taken as-is
    assert sr.dividend_yield_fraction({"trailingAnnualDividendYield": 0.025}, None) == 0.025
    # >= 0.5 is rejected by the sanity gate; with no unambiguous source left the
    # result is a blank (None), NOT the guessed raw dividendYield field (Phase 3.1)
    v = sr.dividend_yield_fraction(
        {"trailingAnnualDividendYield": 0.7, "dividendYield": 0.6}, None)
    assert v is None, v
    print("OK dividend_yield: trailing fraction gate 0<=x<0.5")


def test_dividend_yield_ambiguous_field_not_guessed():
    """The unreliable raw dividendYield field is NOT scale-guessed — it returns
    None (blank) rather than a possibly-100x-wrong yield (honesty over coverage)."""
    assert sr.dividend_yield_fraction({"dividendYield": 38.0}, None) is None
    assert sr.dividend_yield_fraction({"dividendYield": 0.0038}, None) is None
    # nothing usable -> None (honest blank), never invented
    assert sr.dividend_yield_fraction({}, None) is None
    print("OK dividend_yield: ambiguous raw field left blank, not guessed")


# =============================================================================
# Buckets and formatters
# =============================================================================
def test_size_bucket_boundaries():
    assert sr.size_bucket(250e9) == "Mega"
    assert sr.size_bucket(200e9) == "Mega"
    assert sr.size_bucket(50e9) == "Large"
    assert sr.size_bucket(10e9) == "Large"
    assert sr.size_bucket(5e9) == "Mid"
    assert sr.size_bucket(2e9) == "Mid"
    assert sr.size_bucket(1e9) == "Small"
    assert sr.size_bucket(300e6) == "Small"
    assert sr.size_bucket(100e6) == "Micro"
    assert sr.size_bucket(None) == sr.NA
    print("OK size_bucket: Mega/Large/Mid/Small/Micro boundaries")


def test_money():
    assert sr.money(3.0e12) == "$3.00T"
    assert sr.money(845.2e9) == "$845.2B"
    assert sr.money(12.3e9) == "$12.3B"
    assert sr.money(950e6) == "$950.0M"
    assert sr.money(12345) == "$12,345"
    assert sr.money(None) == sr.NA
    assert sr.money(float("nan")) == sr.NA
    print("OK money: T/B/M/plain scaling, missing -> n/a")


def test_price_ratio_pct():
    assert sr.price(3306.0) == "3,306.00"
    assert sr.price(12.5) == "12.50"
    assert sr.price(None) == sr.NA
    assert sr.ratio(32.1) == "32.1"
    assert sr.ratio(2.1, 2) == "2.10"
    assert sr.ratio(140.0, 0) == "140"
    assert sr.ratio(None) == sr.NA
    assert sr.pct(0.253) == "25.3%"
    assert sr.pct(0.021, sign=True) == "+2.1%"
    assert sr.pct(-0.04, sign=True) == "-4.0%"
    assert sr.pct(12.3, already_percent=True) == "12.3%"
    assert sr.pct(None) == sr.NA
    print("OK price/ratio/pct: precision, sign, already_percent, missing")


def test_lean_formatters_blank_for_missing():
    # the lean family renders missing as "" (blank), NOT "n/a"
    assert sr._f(195.0, 2) == "195.00"
    assert sr._f(32.1) == "32.1"
    assert sr._f(None) == ""
    assert sr._fpct(1.47) == "147.0"
    assert sr._fpct(0.0044) == "0.4"
    assert sr._fpct(None) == ""
    # market cap -> bare billions, precision scaled to magnitude
    assert sr._fbn(3.0e12) == "3000"
    assert sr._fbn(3.0e11) == "300.0"
    assert sr._fbn(5.0e9) == "5.00"
    assert sr._fbn(5.0e8) == "0.50"
    assert sr._fbn(None) == ""
    print("OK lean _f/_fpct/_fbn: blank for missing, billions scaling")


def test_sector_and_rec_codes():
    assert sr._sector_code("Technology") == "Tech"
    assert sr._sector_code("Consumer Cyclical") == "ConsCyc"
    assert sr._sector_code("Financial Services") == "Fin"
    assert sr._sector_code("Some New Sector") == "Some New Sector"  # unknown -> raw
    assert sr._sector_code(None) == ""
    assert sr._sector_code(sr.NA) == ""
    assert sr._rec_code("buy") == "B"
    assert sr._rec_code("strong buy") == "SB"
    assert sr._rec_code("none") == ""       # 'none' rating -> blank
    assert sr._rec_code(None) == ""
    assert sr._rec_code("accumulate") == "accumulate"  # unknown -> raw
    print("OK sector/analyst codes: mapped, unknown->raw, missing->blank")


# =============================================================================
# Golden-file test on build_markdown  (catches header/row drift)
# =============================================================================
def _rec(**kw):
    """A full record with every field defaulting to None (the fetch_one shape)."""
    keys = ("sector", "industry", "price", "market_cap", "currency", "pe", "fwd_pe",
            "peg", "pb", "ps", "roe", "margin", "rev_growth", "earnings_growth",
            "debt_equity", "div_yield", "ret_1m", "ret_3m", "ret_6m", "ret_1y",
            "ma50", "ma200", "vs_ma50", "vs_ma200", "hi52", "lo52", "from_hi",
            "from_lo", "beta", "rec", "n_analysts", "target", "upside",
            "usd_market_cap", "size", "error")
    base = {k: None for k in keys}
    base.update(kw)
    return base


def _sample_rows():
    return [
        _rec(ticker="AAPL", name="Apple Inc.", sector="Technology",
             industry="Consumer Electronics", price=195.0, market_cap=3.0e12,
             currency="USD", pe=32.1, fwd_pe=28.4, peg=2.1, pb=48.0, ps=8.2,
             roe=1.47, margin=0.253, rev_growth=0.021, debt_equity=140.0,
             div_yield=0.0044, ret_1m=0.031, ret_3m=0.082, ret_6m=0.12, ret_1y=0.18,
             vs_ma50=0.03, vs_ma200=0.09, from_hi=-0.04, from_lo=0.35, beta=1.29,
             rec="buy", n_analysts=40, target=210.0, upside=0.0769,
             usd_market_cap=3.0e12, size="Mega"),
        _rec(ticker="7203.T", name="Toyota Motor Corporation", sector="Consumer Cyclical",
             industry="Auto Manufacturers", price=3306.0, market_cap=45.0e12,
             currency="JPY", pe=8.4, fwd_pe=9.1, pb=1.1, ps=0.9, roe=0.14, margin=0.10,
             rev_growth=0.06, debt_equity=105.0, div_yield=0.021, ret_1m=-0.02,
             ret_3m=0.05, ret_6m=0.11, ret_1y=0.22, vs_ma50=-0.01, vs_ma200=0.07,
             from_hi=-0.08, from_lo=0.40, beta=0.5, rec="strong buy", n_analysts=15,
             target=3800.0, upside=0.1494, usd_market_cap=3.0e11, size="Mega"),
        _rec(ticker="XYZ", name="Sparse Data Co, Inc.", price=12.5, market_cap=5.0e8,
             currency="USD", usd_market_cap=5.0e8, size="Small"),
    ]


def _failures():
    return [{"ticker": "BADTICK", "name": "BADTICK",
             "error": "empty result (rate-limited or unknown symbol)"}]


def _extract_csv(md: str) -> str:
    """Pull the single fenced ```csv block out of the report."""
    start = md.index("```csv\n") + len("```csv\n")
    end = md.index("\n```", start)
    return md[start:end]


def _check_golden(lean: bool, name: str):
    md = sr.build_markdown(_sample_rows(), _failures(), "2026-07-06", lean=lean)
    got = _extract_csv(md)
    golden_path = GOLDEN / name
    expected = golden_path.read_text(encoding="utf-8")
    assert got == expected, (
        f"CSV grid drifted from {golden_path.name}.\n--- got ---\n{got}\n"
        f"--- expected ---\n{expected}")
    print(f"OK golden {'lean' if lean else 'rich'}: CSV grid matches {name}")


def test_golden_lean_csv():
    _check_golden(lean=True, name="lean_stocks.csv")


def test_golden_rich_csv():
    _check_golden(lean=False, name="rich_stocks.csv")


def test_row_order_by_usd_market_cap():
    """Rows sort mega -> micro by USD market cap (AAPL > 7203.T > XYZ)."""
    md = sr.build_markdown(_sample_rows(), [], "2026-07-06", lean=True)
    csv_body = _extract_csv(md).splitlines()[1:]  # drop header
    order = [line.split(",")[0] for line in csv_body]
    assert order == ["AAPL", "7203.T", "XYZ"], order
    print("OK row order: sorted by USD market cap")


# =============================================================================
# Phase 2 — lean compaction (no data loss). Rich stays full (see golden rich).
# =============================================================================
def _lean_header(rows: list[dict]) -> list[str]:
    md = sr.build_markdown(rows, [], "2026-07-06", lean=True)
    return _extract_csv(md).splitlines()[0].split(",")


def _lean_note(rows: list[dict]) -> str:
    md = sr.build_markdown(rows, [], "2026-07-06", lean=True)
    for line in md.splitlines():
        if line.startswith("_Columns omitted for this set"):
            return line
    return ""


def test_2d_fprice_trailing_zero_trim():
    """_fprice trims trailing zeros losslessly (the value never changes)."""
    assert sr._fprice(3306.0) == "3306"      # JPY .00 removed
    assert sr._fprice(195.0) == "195"
    assert sr._fprice(12.5) == "12.5"
    assert sr._fprice(100.0) == "100"        # integer zeros protected by the '.'
    assert sr._fprice(0.32) == "0.32"        # non-zero decimals kept
    assert sr._fprice(20.5) == "20.5"
    assert sr._fprice(None) == ""
    print("OK 2d _fprice: trailing-zero trim, integer part protected")


def test_2a_single_currency_drops_curr():
    """A single-currency set drops the per-row Curr column and states it once."""
    us = [
        _rec(ticker="AAPL", name="Apple Inc.", sector="Technology", price=195.0,
             currency="USD", pe=32.1, usd_market_cap=3.0e12, size="Mega"),
        _rec(ticker="MSFT", name="Microsoft", sector="Technology", price=430.0,
             currency="USD", pe=36.0, usd_market_cap=3.1e12, size="Mega"),
    ]
    header = _lean_header(us)
    assert "Curr" not in header, header
    assert "**Curr**" in _lean_note(us)
    assert "in **USD**" in _lean_note(us)
    # a mixed-currency set KEEPS Curr (nothing to state once)
    assert "Curr" in _lean_header(_sample_rows())
    print("OK 2a: single-currency drops Curr (stated in note); mixed keeps it")


def test_2b_all_blank_column_dropped():
    """A column blank for EVERY row is dropped and named in the note."""
    rows = [
        _rec(ticker="AAPL", name="Apple Inc.", sector="Technology", price=195.0,
             currency="USD", pe=32.1, peg=None, usd_market_cap=3.0e12, size="Mega"),
        _rec(ticker="7203.T", name="Toyota", sector="Consumer Cyclical", price=3306.0,
             currency="JPY", pe=8.4, peg=None, usd_market_cap=3.0e11, size="Mega"),
    ]
    header = _lean_header(rows)
    assert "PEG" not in header, header          # no PEG anywhere -> dropped
    assert "PE" in header                        # PE has data -> kept
    assert "PEG" in _lean_note(rows)
    print("OK 2b: all-blank column dropped and named in note")


def test_2c_target_dropped_only_when_reconstructable():
    """Target (derivable) is dropped when Price+Upside can rebuild it, else kept."""
    # sample rows: every Target has Price+Upside -> reconstructable -> dropped
    assert "Target" not in _lean_header(_sample_rows())
    assert "Upside" in _lean_header(_sample_rows())  # kept: currency-neutral
    assert "**Target**" in _lean_note(_sample_rows())
    # a Target with no Upside can't be rebuilt -> Target is kept (nothing invented)
    no_upside = [
        _rec(ticker="ZZZ", name="No Upside Co", sector="Technology", price=50.0,
             currency="USD", target=100.0, upside=None,
             usd_market_cap=1.0e10, size="Large"),
    ]
    assert "Target" in _lean_header(no_upside), _lean_header(no_upside)
    print("OK 2c: Target dropped iff reconstructable from Price+Upside")


def test_2c_rich_keeps_target_and_curr():
    """Rich is the human format: it keeps Target and Curr and untrimmed prices."""
    md = sr.build_markdown(_sample_rows(), [], "2026-07-06", lean=False)
    header = _extract_csv(md).splitlines()[0].split(",")
    assert "Target" in header and "Curr" in header, header
    # rich price keeps 2dp + thousands separator (12.50, 3,306.00)
    assert "12.50" in _extract_csv(md)
    print("OK rich unaffected by lean compaction (Target/Curr/2dp kept)")


# =============================================================================
# Phase 3 — correctness / honesty
# =============================================================================
def test_history_failure_logged_not_fatal():
    """A failed history pull is logged at DEBUG but does NOT fail the fetch — the
    name still lands with its .info fields, just without momentum/MA/52w columns."""
    import logging

    class _FakeTicker:
        def __init__(self, sym):
            pass

        @property
        def info(self):
            return {"shortName": "Fake Co", "marketCap": 1e9,
                    "currentPrice": 10.0, "currency": "USD"}

        def history(self, *a, **k):
            raise RuntimeError("boom-history")

    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    sr.log.addHandler(handler)
    old_level, sr.log.level = sr.log.level, logging.DEBUG
    old_ticker, sr.yf.Ticker = sr.yf.Ticker, _FakeTicker
    try:
        rec = sr.fetch_one("FAKE", 0, cache_dir=None)  # retries=0, no sleeps
    finally:
        sr.yf.Ticker = old_ticker
        sr.log.removeHandler(handler)
        sr.log.setLevel(old_level)

    assert rec.get("error") is None, rec          # fetch still succeeded
    assert rec["price"] == 10.0
    assert rec["ret_1m"] is None                  # no history -> momentum blank
    assert any("boom-history" in m for m in records), records
    print("OK 3.2: history failure logged at DEBUG, fetch still succeeds")


# =============================================================================
# Phase 5 — performance (parallel FX, cached constituent scrapes)
# =============================================================================
def test_fx_rates_parallel_aggregation():
    """FX rates aggregate correctly, dedup GBp/GBX -> one GBP fetch, drop the
    unpriceable. _fetch_fx_one is stubbed so no network is touched."""
    calls = []

    def fake(base):
        calls.append(base)
        return base, {"JPY": 0.0067, "AUD": 0.66}.get(base)  # EUR/GBP -> None

    old, sr.fetch._fetch_fx_one = sr.fetch._fetch_fx_one, fake
    try:
        rates = sr.fetch_fx_rates(["USD", "JPY", "AUD", "EUR", "GBp", "GBX"])
    finally:
        sr.fetch._fetch_fx_one = old
    assert rates["USD"] == 1.0
    assert rates["JPY"] == 0.0067 and rates["AUD"] == 0.66
    assert "EUR" not in rates and "GBP" not in rates   # unpriceable -> excluded
    assert calls.count("GBP") == 1, calls              # GBp + GBX deduped to one
    print("OK 5.3 FX: parallel aggregation, GBp/GBX dedup, missing excluded")


def test_universe_disk_cache_short_circuits():
    """A day-cached constituent list is returned without re-scraping; --no-cache
    (CACHE_DIR=None) bypasses the cache and would hit the network."""
    import tempfile

    def boom(*a, **k):
        raise RuntimeError("network hit!")

    with tempfile.TemporaryDirectory() as d:
        old_dir, sr.universe.CACHE_DIR = sr.universe.CACHE_DIR, Path(d)
        old_soup, sr.universe._get_soup = sr.universe._get_soup, boom
        try:
            sr.universe._universe_cache_save(
                "fetch_index_constituents_sp500", ["AAA", "BBB"])
            got = sr.universe.fetch_index_constituents("sp500")  # cache hit
            assert got == ["AAA", "BBB"], got
            sr.universe.CACHE_DIR = None                         # --no-cache
            try:
                sr.universe.fetch_index_constituents("sp500")
                assert False, "expected a network call with caching disabled"
            except RuntimeError as e:
                assert "network hit" in str(e)
        finally:
            sr.universe.CACHE_DIR = old_dir
            sr.universe._get_soup = old_soup
    print("OK 5.2 universe cache: disk hit skips scrape; --no-cache bypasses")


# =============================================================================
# Phase 6 — --format escape hatch (raw CSV / JSON)
# =============================================================================
def test_stocks_csv_matches_report_grid():
    """The raw --format csv output is exactly the report's fenced grid (golden)."""
    assert sr.stocks_csv(_sample_rows(), lean=True) == \
        (GOLDEN / "lean_stocks.csv").read_text(encoding="utf-8")
    assert sr.stocks_csv(_sample_rows(), lean=False) == \
        (GOLDEN / "rich_stocks.csv").read_text(encoding="utf-8")
    print("OK 6.2 stocks_csv: raw grid matches the report's fenced CSV")


def test_csv_injection_guard_on_text_columns_only():
    """A company name starting with =/+/-/@ gets apostrophe-prefixed (CSV
    formula-injection guard); a legitimately negative NUMERIC cell (From-hi,
    a percentage) must NOT be touched."""
    import csv as _csv
    import io as _io
    rows = [
        _rec(ticker="=CMD|'/C calc'!A1", name="+1 Corp", sector="Technology",
             industry="Software", price=10.0, market_cap=1e9, currency="USD",
             rec="-buy", from_hi=-0.25, ret_1m=-0.03,
             usd_market_cap=1e9, size="Small"),
        _rec(ticker="@ATSIGN", name="-Dash Inc.", sector="Technology",
             industry="Software", price=5.0, market_cap=1e9, currency="USD",
             usd_market_cap=1e9, size="Small"),
    ]
    csv_text = sr.stocks_csv(rows, lean=True)
    reader = _csv.DictReader(_io.StringIO(csv_text))
    r1, r2 = list(reader)
    assert r1["Ticker"] == "'=CMD|'/C calc'!A1", r1["Ticker"]
    assert r1["Name"] == "'+1 Corp", r1["Name"]
    assert r1["Analyst"] == "'-buy", r1["Analyst"]
    assert r2["Ticker"] == "'@ATSIGN", r2["Ticker"]
    assert r2["Name"] == "'-Dash Inc.", r2["Name"]
    # numeric cells with a genuine leading '-' stay untouched
    assert r1["Hi"] == "-25.0", r1["Hi"]
    assert r1["1m"] == "-3.0", r1["1m"]
    print("OK CSV-injection guard: text cells prefixed, numeric '-' untouched")


def test_stocks_json_shape():
    """--format json emits run_day + market-cap-sorted records + failures."""
    import json as _json
    obj = _json.loads(sr.stocks_json(_sample_rows(), _failures(), "2026-07-06"))
    assert obj["run_day"] == "2026-07-06"
    assert [s["ticker"] for s in obj["stocks"]] == ["AAPL", "7203.T", "XYZ"]
    assert obj["failures"] == [
        {"ticker": "BADTICK", "error": "empty result (rate-limited or unknown symbol)"}]
    print("OK 6.2 stocks_json: sorted records + failures")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} stock_review tests...\n")
    for fn in fns:
        fn()
    print("\nALL STOCK_REVIEW TESTS PASSED")


if __name__ == "__main__":
    _run_all()
