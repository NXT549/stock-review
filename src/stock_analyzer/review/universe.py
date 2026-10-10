"""Ticker universes: named --source lists, Wikipedia/ASX index scrapers, and the
sector/region GROUP logic (--sector). Group membership comes from the index tables'
GICS columns, so deciding who's in a group needs no per-ticker fetch."""
from __future__ import annotations

import functools
import io
import json
import re
from datetime import date
from pathlib import Path

from .format import NA, _num
from .symbols import _intl_symbol, clean_symbol


# --- On-disk cache for the constituent / index-record scrapes -----------------
# The per-ticker fetch already resumes from disk, but the index CONSTITUENT lists
# were re-scraped from Wikipedia/ASX on EVERY run — a fragile network round-trip
# each time, for lists that change at most daily. Cache them per calendar day too
# (under the same dir as the ticker cache). cli.main points CACHE_DIR at
# --cache-dir, or sets it to None for --no-cache.
CACHE_DIR: Path | None = Path("data/review_cache")


def _universe_cache_file(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return CACHE_DIR / "_universe" / date.today().isoformat() / f"{safe}.json"


def _universe_cache_load(key: str):
    if CACHE_DIR is None:
        return None
    p = _universe_cache_file(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None  # corrupt/partial cache -> just re-scrape


def _universe_cache_save(key: str, data) -> None:
    if CACHE_DIR is None:
        return
    try:
        p = _universe_cache_file(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass  # caching is best-effort; never fail a run over it


def _disk_cached(fn):
    """Cache a scraper's JSON-serialisable result on disk for the calendar day, so
    the constituent lists aren't re-scraped on every rerun. Keyed by function name
    + args; only truthy results are cached; honours CACHE_DIR (None disables)."""
    @functools.wraps(fn)
    def wrapper(*args):
        key = "_".join([fn.__name__, *(str(a) for a in args)])
        hit = _universe_cache_load(key)
        if hit is not None:
            return hit
        result = fn(*args)
        if result:
            _universe_cache_save(key, result)
        return result
    return wrapper


DEFAULT_TICKERS = [
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
]


WIKI_UA = {"User-Agent": "Mozilla/5.0 (stock_review universe fetch)"}


INDEX_PAGES = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}


INTL_INDEXES = {
    "asx200": {  # Australia
        "url": "https://en.wikipedia.org/wiki/S%26P/ASX_200",
        "col_keys": ("code",), "suffix": ".AX", "min_rows": 50,
    },
    "ftse100": {  # United Kingdom (prices quoted in pence, GBp)
        "url": "https://en.wikipedia.org/wiki/FTSE_100_Index",
        "col_keys": ("ticker", "epic"), "suffix": ".L", "dot_to_dash": True, "min_rows": 50,
    },
    "dax": {  # Germany (Wikipedia ticker already carries the .DE/.PA suffix)
        "url": "https://en.wikipedia.org/wiki/DAX",
        "col_keys": ("ticker",), "suffix": "", "keep_dots": True, "min_rows": 30,
    },
    "hsi": {  # Hong Kong ("SEHK:700" -> "0700.HK")
        "url": "https://en.wikipedia.org/wiki/Hang_Seng_Index",
        "col_keys": ("ticker",), "suffix": ".HK", "digits_only": True, "zfill": 4, "min_rows": 30,
    },
    "tsx60": {  # Canada
        "url": "https://en.wikipedia.org/wiki/S%26P/TSX_60",
        "col_keys": ("symbol",), "suffix": ".TO", "dot_to_dash": True, "min_rows": 40,
    },
    "nifty50": {  # India
        "url": "https://en.wikipedia.org/wiki/NIFTY_50",
        "col_keys": ("symbol",), "suffix": ".NS", "min_rows": 40,
    },
}


NIKKEI_CURATED = [
    "7203.T", "6758.T", "6861.T", "9984.T", "8306.T", "6098.T", "7974.T", "9983.T",
    "4063.T", "8035.T", "6501.T", "6902.T", "7267.T", "6594.T", "4502.T", "8316.T",
    "8411.T", "9433.T", "9432.T", "4519.T", "6367.T", "7741.T", "6981.T", "8058.T",
    "8001.T", "8031.T", "2914.T", "4661.T", "6273.T", "6954.T", "7751.T", "6752.T",
    "8766.T", "4568.T", "4503.T", "5108.T", "7011.T", "9020.T", "8053.T", "6201.T",
]


def _get_soup(url):
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise SystemExit(
            "--source needs requests + beautifulsoup4:  pip install requests beautifulsoup4"
        ) from e
    r = requests.get(url, headers=WIKI_UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


@_disk_cached
def fetch_index_constituents(which: str) -> list[str]:
    """Scrape the symbol column from a US Wikipedia S&P constituents table."""
    url = INDEX_PAGES[which]
    soup = _get_soup(url)
    table = soup.find("table", {"id": "constituents"}) or soup.find("table", {"class": "wikitable"})
    if table is None:
        raise SystemExit(f"Could not find the constituents table on {url} (layout changed?).")
    syms = [clean_symbol(tr.find_all(["td", "th"])[0].get_text())
            for tr in table.find_all("tr")[1:] if tr.find_all(["td", "th"])]
    syms = [s for s in syms if s]
    if not syms:
        raise SystemExit(f"Parsed 0 symbols from {url} (layout changed?).")
    return syms


@_disk_cached
def scrape_intl_index(key: str) -> list[str]:
    """Scrape an international index, locating the ticker column by header keyword."""
    cfg = INTL_INDEXES[key]
    soup = _get_soup(cfg["url"])
    for table in soup.find_all("table", {"class": "wikitable"}):
        rows = table.find_all("tr")
        if len(rows) < cfg.get("min_rows", 30):
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col = next((i for i, h in enumerate(headers)
                    if any(k in h for k in cfg["col_keys"])), None)
        if col is None:
            continue
        out = []
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= col:
                continue
            raw = cells[col].get_text(strip=True)
            sym = _intl_symbol(raw, cfg)
            if sym:
                out.append(sym)
        if out:
            return out
    raise SystemExit(f"Could not parse constituents for {key} from {cfg['url']} (layout changed?).")


def read_tickers_file(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError as e:  # missing/unreadable file -> clean message, not a traceback
        raise SystemExit(f"Could not read tickers file '{path}': {e}") from e
    # utf-8-sig drops the BOM Excel/Notepad put on "CSV UTF-8" files (it would
    # otherwise glue itself to the first ticker); fall back to cp1252 for files
    # saved with a legacy Windows code page instead of crashing on the decode.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    tickers: list[str] = []
    col = 0               # index of the ticker column (set by a header row)
    seen_data = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip().strip('"').strip("'") for c in re.split(r"[,;\t]", line)]
        # A header row ("ticker", "Symbol,Name", ...) is recognised before the
        # first data line, even after leading comments/blank lines, and tells us
        # which column holds the tickers.
        if not seen_data:
            lowered = [c.lower() for c in cells]
            hit = next((i for i, c in enumerate(lowered)
                        if c in {"ticker", "tickers", "symbol", "symbols", "code"}), None)
            if hit is not None:
                col = hit
                seen_data = True
                continue
        seen_data = True
        first = cells[col] if col < len(cells) else ""
        if first:
            tickers.append(first.upper())
    return tickers


def _sp1500():
    return (fetch_index_constituents("sp500")
            + fetch_index_constituents("sp400")
            + fetch_index_constituents("sp600"))


def _international():
    """Every supported non-US market: AU, JP, UK, DE, HK, CA, IN."""
    out = list(NIKKEI_CURATED)
    for key in INTL_INDEXES:
        out += scrape_intl_index(key)
    return out


SECTOR_PAGES = {
    "sp500": {"url": INDEX_PAGES["sp500"], "sym": "Symbol", "sector": "GICS Sector",
              "subind": "GICS Sub-Industry", "suffix": "", "us": True},
    "sp400": {"url": INDEX_PAGES["sp400"], "sym": "Symbol", "sector": "GICS Sector",
              "subind": "GICS Sub-Industry", "suffix": "", "us": True},
    "sp600": {"url": INDEX_PAGES["sp600"], "sym": "Symbol", "sector": "GICS Sector",
              "subind": "GICS Sub-Industry", "suffix": "", "us": True},
    "asx200": {"url": INTL_INDEXES["asx200"]["url"], "sym": "Code", "sector": "Sector",
               "mcap": "Market Capitalisation (A$)", "suffix": ".AX", "us": False},
}


AU_SMALLCAP_MAX_AUD = 5_000_000_000


_RECORD_CACHE: dict[str, list[dict]] = {}


@_disk_cached
def fetch_index_records(key: str) -> list[dict]:
    """Scrape one index page into [{symbol, sector, subind, mcap_local}], cached.

    Kept separate from the plain ticker scrapers (so their behaviour is unchanged);
    this path additionally captures the sector/sub-industry and, for the ASX, the
    market-cap columns that the sector and small-cap groups need.
    """
    if key in _RECORD_CACHE:
        return _RECORD_CACHE[key]
    try:
        import pandas as pd
        import requests
    except ImportError as e:
        raise SystemExit("sector/region groups need pandas + requests + lxml:  "
                         "pip install pandas requests lxml beautifulsoup4") from e
    cfg = SECTOR_PAGES[key]
    resp = requests.get(cfg["url"], headers=WIKI_UA, timeout=30)
    resp.raise_for_status()
    table = None
    for t in pd.read_html(io.StringIO(resp.text)):
        cols = [str(c).strip() for c in t.columns]
        if cfg["sym"] in cols and cfg["sector"] in cols:
            t.columns = cols
            table = t
            break
    if table is None:
        raise SystemExit(f"Could not find a sector table for {key} on {cfg['url']} (layout changed?).")
    recs: list[dict] = []
    for _, row in table.iterrows():
        raw = str(row[cfg["sym"]]).strip()
        if not raw or raw.lower() == "nan":
            continue
        sym = clean_symbol(raw) if cfg["us"] else raw.split()[0].upper() + cfg["suffix"]
        subind = ""
        if cfg.get("subind") and cfg["subind"] in table.columns:
            subind = str(row[cfg["subind"]]).strip()
            if subind.lower() == "nan":
                subind = ""
        mcap = None
        if cfg.get("mcap") and cfg["mcap"] in table.columns:
            mcap = _num(re.sub(r"[^0-9.]", "", str(row[cfg["mcap"]])) or None)
        recs.append({"symbol": sym, "sector": str(row[cfg["sector"]]).strip(),
                     "subind": subind, "has_subind": bool(cfg.get("subind")),
                     "mcap_local": mcap})
    if not recs:
        raise SystemExit(f"Parsed 0 rows for {key} from {cfg['url']} (layout changed?).")
    _RECORD_CACHE[key] = recs
    return recs


def us_records() -> list[dict]:
    """All S&P 1500 (US large + mid + small) names with their GICS sectors."""
    return (fetch_index_records("sp500") + fetch_index_records("sp400")
            + fetch_index_records("sp600"))


def au_records() -> list[dict]:
    return list(fetch_index_records("asx200"))


def au_smallcap_records() -> list[dict]:
    """The small/mid-cap tail of the ASX 200 (market cap < AU_SMALLCAP_MAX_AUD)."""
    return [r for r in au_records()
            if r["mcap_local"] is not None and r["mcap_local"] < AU_SMALLCAP_MAX_AUD]


def global_records() -> list[dict]:
    return us_records() + au_records()


def global_smallcap_records() -> list[dict]:
    """US small caps (S&P 600) + the small-cap tail of the ASX 200."""
    return list(fetch_index_records("sp600")) + au_smallcap_records()


ASX_ALL_CSV = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"


@_disk_cached
def fetch_asx_all_records() -> list[dict]:
    """Every ASX-listed name from the official CSV, tagged with its GICS group."""
    key = "asx-all"
    if key in _RECORD_CACHE:
        return _RECORD_CACHE[key]
    try:
        import pandas as pd
        import requests
    except ImportError as e:
        raise SystemExit("the full-ASX group needs pandas + requests:  "
                         "pip install pandas requests") from e
    resp = requests.get(ASX_ALL_CSV, headers=WIKI_UA, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), skiprows=1)  # 1 preamble line before header
    code_col = next((c for c in df.columns if "code" in c.lower()), None)
    ig_col = next((c for c in df.columns if "industry" in c.lower()), None)
    if code_col is None:
        raise SystemExit(f"Could not find an ASX code column in {list(df.columns)} (format changed?).")
    recs: list[dict] = []
    for _, row in df.iterrows():
        raw = str(row[code_col]).strip().upper()
        if not raw or raw == "NAN":
            continue
        igroup = str(row[ig_col]).strip() if ig_col else ""
        if igroup.lower() in ("nan", "not applic", "class pend"):
            igroup = ""  # ETFs / trusts / pending — no usable sector
        recs.append({"symbol": f"{raw}.AX", "sector": "", "subind": "",
                     "has_subind": False, "igroup": igroup, "mcap_local": None})
    if not recs:
        raise SystemExit("Parsed 0 rows from the ASX listed-companies CSV (format changed?).")
    _RECORD_CACHE[key] = recs
    return recs


REGION_RECORDS = {
    "america": us_records, "us": us_records, "sp1500": us_records,
    "sp500": lambda: fetch_index_records("sp500"), "sap": lambda: fetch_index_records("sp500"),
    "sp400": lambda: fetch_index_records("sp400"), "midcap": lambda: fetch_index_records("sp400"),
    "sp600": lambda: fetch_index_records("sp600"), "smallcap": lambda: fetch_index_records("sp600"),
    "america-smallcap": lambda: fetch_index_records("sp600"),
    "us-smallcap": lambda: fetch_index_records("sp600"),
    "australia": au_records, "asx200": au_records, "a200": au_records,
    "australia-smallcap": au_smallcap_records, "au-smallcap": au_smallcap_records,
    "asx-all": fetch_asx_all_records, "australia-all": fetch_asx_all_records,
    "asx": fetch_asx_all_records,
    "global": global_records,
    "global-smallcap": global_smallcap_records, "world-smallcap": global_smallcap_records,
}


SECTORS = {
    "energy":         {"sectors": {"energy"}, "igroups": {"energy"}},
    "materials":      {"sectors": {"materials"}, "igroups": {"materials"}},
    "industrials":    {"sectors": {"industrials"},
                       "igroups": {"capital goods", "commercial & professional services",
                                   "transportation"}},
    "technology":     {"sectors": {"information technology"},
                       "igroups": {"software & services", "technology hardware & equipment",
                                   "semiconductors & semiconductor equipment"}},
    "financials":     {"sectors": {"financials"},
                       "igroups": {"financial services", "banks", "insurance",
                                   "diversified financials"}},
    "healthcare":     {"sectors": {"health care", "healthcare"},
                       "igroups": {"health care equipment & services",
                                   "pharmaceuticals, biotechnology & life sciences"}},
    "consumer":       {"sectors": {"consumer discretionary", "consumer staples"},
                       "igroups": {"consumer discretionary distribution & retail",
                                   "consumer staples distribution & retail",
                                   "consumer durables & apparel", "consumer services",
                                   "automobiles & components", "food, beverage & tobacco",
                                   "household & personal products", "food & staples retailing",
                                   "retailing"}},
    "utilities":      {"sectors": {"utilities"}, "igroups": {"utilities"}},
    "realestate":     {"sectors": {"real estate"},
                       "igroups": {"equity real estate investment trusts (reits)",
                                   "real estate management & development", "real estate"}},
    "communications": {"sectors": {"communication services"},
                       "igroups": {"telecommunication services", "media & entertainment"}},
    "mining":         {"subind_kw": {"metals & mining", "mining", "gold", "silver",
                                     "steel", "copper", "aluminum", "aluminium", "coal",
                                     "precious metals", "diversified metals"},
                       "fallback_sectors": {"materials"}, "igroups": {"materials"},
                       # 'materials' is broader than mining (chemicals/packaging/building
                       # mats), so those inclusions are coarse -> verified post-fetch
                       # against each name's yfinance industry via yf_industry_kw.
                       "coarse_igroups": {"materials"},
                       "yf_industry_kw": {"mining", "gold", "silver", "copper",
                                          "aluminum", "steel", "coal", "metals",
                                          "platinum", "uranium"}},
    "telecom":        {"subind_kw": {"telecom", "wireless"},
                       "fallback_sectors": {"communication services"},
                       # the 'telecommunication services' industry group IS telecom, so
                       # full-ASX matches are precise (no coarse_igroups) and kept as-is;
                       # only the ASX 200 whole-sector fallback needs verification.
                       "igroups": {"telecommunication services"},
                       "yf_industry_kw": {"telecom", "wireless"}},
}


SECTOR_ALIASES = {
    "tech": "technology", "it": "technology", "infotech": "technology",
    "finance": "financials", "financial": "financials", "banks": "financials",
    "health": "healthcare", "comms": "communications", "communication": "communications",
    "telco": "telecom", "telecoms": "telecom", "property": "realestate",
    "reit": "realestate", "reits": "realestate", "miners": "mining",
    "resources": "mining", "metals": "mining", "gold": "mining", "oil": "energy",
    "oilgas": "energy", "power": "utilities", "utility": "utilities",
    "industrial": "industrials", "staples": "consumer", "discretionary": "consumer",
}


def _norm_sector(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def resolve_sector_key(name: str) -> str:
    """Map a user-supplied --sector value (incl. aliases) to a canonical key."""
    key = re.sub(r"[\s_-]+", "", _norm_sector(name))
    key = SECTOR_ALIASES.get(key, key)
    if key not in SECTORS:
        raise SystemExit(
            f"Unknown --sector '{name}'. Choose from: {', '.join(sorted(SECTORS))} "
            "(aliases: tech, finance, health, telco, miners, oil, property, ...).")
    return key


def _sector_match(rec: dict, spec: dict) -> bool:
    sector = _norm_sector(rec.get("sector", ""))
    sub = _norm_sector(rec.get("subind", ""))
    igroup = _norm_sector(rec.get("igroup", ""))  # GICS industry group (full-ASX CSV)
    if "sectors" in spec:  # plain GICS-sector group
        return (bool(sector) and sector in spec["sectors"]) or \
               (bool(igroup) and igroup in spec.get("igroups", set()))
    # sub-sector concept (mining/telecom)
    if rec.get("has_subind"):  # precise sub-industry match where the table has one (US)
        return any(kw in sub for kw in spec["subind_kw"])
    if igroup:  # full-ASX CSV: match the industry group, refine later via yfinance
        return igroup in spec.get("igroups", set())
    return sector in spec.get("fallback_sectors", set())  # ASX 200 has only a sector col


def tickers_for_sector(sector_key: str, region: str) -> list[str]:
    """Names in `sector_key` within region scope `region`, from index records."""
    spec = SECTORS[sector_key]
    return [r["symbol"] for r in REGION_RECORDS[region]() if _sector_match(r, spec)]


def _is_coarse_match(rec: dict, spec: dict) -> bool:
    """True if `rec` entered a sub-industry group (mining/telecom) via a COARSE path
    — a whole-sector fallback (ASX 200) or a broad industry group (full-ASX
    'Materials' for mining). Those are the ones worth re-checking against yfinance.
    Precise matches (US sub-industry, or the telecom-specific industry group) aren't.
    """
    if "subind_kw" not in spec or rec.get("has_subind"):
        return False
    igroup = _norm_sector(rec.get("igroup", ""))
    if igroup:  # full-ASX CSV row
        return igroup in spec.get("coarse_igroups", set())
    return _norm_sector(rec.get("sector", "")) in spec.get("fallback_sectors", set())


def loose_sector_tickers(sector_key: str, region: str) -> set[str]:
    """Symbols in the group that were matched coarsely and so should be verified
    against yfinance's own industry after the fetch (see `_is_coarse_match`)."""
    spec = SECTORS[sector_key]
    return {r["symbol"] for r in REGION_RECORDS[region]() if _is_coarse_match(r, spec)}


def verify_loose_industry(rows: list[dict], loose: set[str],
                          spec: dict) -> tuple[list[dict], list[dict]]:
    """Drop loose (fallback-matched) rows whose yfinance industry contradicts the
    group. Returns (kept_rows, excluded_rows). Rows outside `loose` are untouched;
    a loose row with no industry data is kept (we don't drop on missing evidence).
    """
    kw = spec.get("yf_industry_kw")
    if not loose or not kw:
        return rows, []
    kept, excluded = [], []
    for r in rows:
        if r["ticker"] not in loose:
            kept.append(r)
            continue
        industry = _norm_sector(r.get("industry", ""))
        if not industry or industry == NA or any(k in industry for k in kw):
            kept.append(r)
        else:
            excluded.append(r)
    return kept, excluded


def describe_group(args, sector_key: str | None, region: str | None) -> str | None:
    """One-line label of what group this run pulled, for the report header."""
    if sector_key:
        reg = region or "global"
        scope = " (US + Australia)" if reg == "global" else ""
        return f"sector **{sector_key}** · region **{reg}**{scope}"
    if args.source:
        return f"region/universe **{args.source}**"
    return None


SOURCES = {
    # United States
    "curated": lambda: list(DEFAULT_TICKERS),
    "sp500": lambda: fetch_index_constituents("sp500"),
    "sp400": lambda: fetch_index_constituents("sp400"),
    "midcap": lambda: fetch_index_constituents("sp400"),
    "sp600": lambda: fetch_index_constituents("sp600"),
    "smallcap": lambda: fetch_index_constituents("sp600"),
    "sp1500": _sp1500,
    "us": _sp1500,
    "america": _sp1500,
    "sap": lambda: fetch_index_constituents("sp500"),
    "america-smallcap": lambda: fetch_index_constituents("sp600"),
    "us-smallcap": lambda: fetch_index_constituents("sp600"),
    # Single international markets
    "australia": lambda: scrape_intl_index("asx200"),
    "asx200": lambda: scrape_intl_index("asx200"),
    "a200": lambda: scrape_intl_index("asx200"),
    # Region small-cap sleeves & a focused global scope (US 1500 + ASX 200)
    "australia-smallcap": lambda: [r["symbol"] for r in au_smallcap_records()],
    "au-smallcap": lambda: [r["symbol"] for r in au_smallcap_records()],
    # The ENTIRE ASX (~2,000 listings) — wider than the ASX 200 (big/slow to fetch)
    "asx-all": lambda: [r["symbol"] for r in fetch_asx_all_records()],
    "australia-all": lambda: [r["symbol"] for r in fetch_asx_all_records()],
    "asx": lambda: [r["symbol"] for r in fetch_asx_all_records()],
    "global": lambda: [r["symbol"] for r in global_records()],
    "global-smallcap": lambda: [r["symbol"] for r in global_smallcap_records()],
    "world-smallcap": lambda: [r["symbol"] for r in global_smallcap_records()],
    "japan": lambda: list(NIKKEI_CURATED),
    "nikkei": lambda: list(NIKKEI_CURATED),
    "uk": lambda: scrape_intl_index("ftse100"),
    "ftse100": lambda: scrape_intl_index("ftse100"),
    "germany": lambda: scrape_intl_index("dax"),
    "dax": lambda: scrape_intl_index("dax"),
    "hongkong": lambda: scrape_intl_index("hsi"),
    "hsi": lambda: scrape_intl_index("hsi"),
    "canada": lambda: scrape_intl_index("tsx60"),
    "tsx60": lambda: scrape_intl_index("tsx60"),
    "india": lambda: scrape_intl_index("nifty50"),
    "nifty50": lambda: scrape_intl_index("nifty50"),
    # Combos
    "international": _international,
    "intl": _international,
    "world": lambda: _sp1500() + _international(),
    "everything": lambda: _sp1500() + _international(),
    "all": lambda: _sp1500() + _international(),
}


def print_groups() -> None:
    """List the region sources and sector groups, then return (no network)."""
    print("Region groups / named universes  (--source NAME):")
    print("  United States : sp500/sap, sp400/midcap, sp600/smallcap,")
    print("                  sp1500/us/america, america-smallcap")
    print("  Australia     : australia/asx200/a200 (the 200), australia-smallcap,")
    print("                  asx-all/australia-all (EVERY ASX listing, ~2,000 - big/slow)")
    print("  Other markets : japan, uk, germany, hongkong, canada, india")
    print("  Combos        : global, global-smallcap, international, world/everything")
    print()
    print("Sector groups  (--sector NAME, spans US + Australia, or pin with --source):")
    print(f"  {', '.join(sorted(SECTORS))}")
    aliases = ", ".join(f"{a}->{c}" for a, c in sorted(SECTOR_ALIASES.items()))
    print(f"  aliases: {aliases}")
    print()
    print("Examples:")
    print("  stock_review.py --source australia-smallcap")
    print("  stock_review.py --sector mining")
    print("  stock_review.py --source australia --sector mining")
