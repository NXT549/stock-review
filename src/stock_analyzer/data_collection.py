"""Data collection via yfinance.

DATA-SOURCE HONESTY (read this):
  * yfinance is UNOFFICIAL and breakable. We pin a known-good version and treat
    failures as expected. "Rate-limit handling" here means local CACHING +
    THROTTLING + exponential-backoff RETRY, not reading any API rate-limit
    header (there isn't a documented one).
  * We NEVER fabricate a value to fill a gap. A field yfinance does not return is
    recorded as None (unavailable) and excluded downstream.
  * News/social sentiment and institutional flow are not reliably available for
    free; they come from optional paid providers (disabled by default) - see
    sentiment_providers.py.

Each collected record is a flat dict of RAW sourced fields (see metrics.py for
how these become scoring inputs). Records are cached per ticker per day under
data/cache so a re-run within a trading day does not re-hit the network; the
history cache is additionally keyed by the configured history_period, so a
config change never serves same-day data fetched under a different period.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import Config
from .indicators import compute_indicators
from .logging_setup import get_logger
from .metrics import UNKNOWN_SECTOR
from .sentiment_providers import fetch_sentiment

log = get_logger("data")


def _num(x):
    """Coerce to float or None (drops NaN/inf and non-numerics)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _slug(s: str) -> str:
    """Config value -> filename-safe token (lowercase, runs of other chars -> '-')."""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


# Windows refuses these as file/dir names (with or without an extension), and
# PRN is a real NYSE ETF ticker — see _safe_cache_name.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                     *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def _safe_cache_name(ticker: str) -> str:
    """Ticker -> a name safe to use as a cache directory on every OS.

    Slashes are path separators, an all-dots name ('.', '..') escapes a
    directory level, and Windows rejects reserved device names (so a PRN
    cache dir would fail to create and the ticker would silently collect as
    a sparse row every run). Distinct real tickers never collide: '_'-prefixed
    or all-dots symbols don't exist on any exchange.
    """
    safe = ticker.replace("/", "_").replace("\\", "_")
    if not safe or set(safe) == {"."}:
        return "_"
    stem = safe.split(".", 1)[0]
    if stem.upper() in _WINDOWS_RESERVED:
        return "_" + safe
    return safe


def _atomic_write(path: Path, write_fn: Callable[[Path], None]) -> None:
    """Write via a unique sibling temp file + os.replace, so a reader never
    sees a half-written cache file. The temp name embeds pid + random suffix:
    concurrent runs must not share the temp either. Cleanup is best-effort."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class DataCollector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dcfg = cfg.data
        self.cache_dir = cfg.resolve(self.dcfg.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------- retry / cache -----------------------------
    def _retry(self, fn: Callable, op: str):
        """Run fn() with exponential backoff + jitter. Returns None on total failure."""
        attempts = max(1, int(self.dcfg.max_retries))
        base = float(self.dcfg.backoff_base_seconds)
        for attempt in range(attempts):
            try:
                return fn()
            except ImportError as exc:
                # A missing/broken yfinance cannot be fixed by retrying — fail
                # fast per-op so a broken venv costs seconds, not hours.
                log.error("%s failed: %s (not retryable)", op, exc)
                return None
            except Exception as exc:  # noqa: BLE001 - yfinance throws many types
                wait = base * (2 ** attempt) + random.uniform(0, base)
                if attempt + 1 >= attempts:
                    log.error("%s failed after %d attempts: %s", op, attempts, exc)
                    return None
                log.warning("%s failed (attempt %d/%d): %s -> retry in %.1fs",
                            op, attempt + 1, attempts, exc, wait)
                time.sleep(wait)
        return None

    def _cache_file(self, ticker: str, kind: str, ext: str,
                    variant: str | None = None) -> Path:
        """Per-ticker per-day cache path. `variant` keys the filename by any
        config that shapes the fetched payload (e.g. history_period), so a
        config change can't serve stale same-day data fetched under another."""
        d = self.cache_dir / _safe_cache_name(ticker)
        d.mkdir(parents=True, exist_ok=True)
        suffix = f"_{variant}" if variant else ""
        return d / f"{date.today().isoformat()}_{kind}{suffix}.{ext}"

    def _fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        return age_h <= float(self.dcfg.cache_ttl_hours)

    # ------------------------------- fetchers --------------------------------
    def _load_history(self, ticker: str):
        """Daily OHLCV history for one ticker, cached to CSV (keyed by period)."""
        cache = self._cache_file(ticker, "hist", "csv",
                                 variant=_slug(self.dcfg.history_period))
        if self._fresh(cache):
            try:
                return pd.read_csv(cache, index_col=0, parse_dates=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("history cache unreadable for %s: %s", ticker, exc)

        def _do():
            import yfinance as yf
            df = yf.download(
                ticker, period=self.dcfg.history_period, interval="1d",
                auto_adjust=True, progress=False, threads=False,
            )
            if isinstance(df.columns, pd.MultiIndex):
                # single-ticker download can still come back multi-indexed
                df.columns = df.columns.get_level_values(0)
            return df

        df = self._retry(_do, f"history[{ticker}]")
        if df is not None and not df.empty:
            try:
                _atomic_write(cache, df.to_csv)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not write history cache for %s: %s", ticker, exc)
        return df

    def _load_info(self, ticker: str) -> dict:
        """yfinance .info dict for one ticker, cached to JSON. Throttled."""
        cache = self._cache_file(ticker, "info", "json")
        if self._fresh(cache):
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("info cache unreadable for %s: %s", ticker, exc)

        def _do():
            import yfinance as yf
            time.sleep(float(self.dcfg.request_pause_seconds))  # polite throttle
            tk = yf.Ticker(ticker)
            info = dict(tk.info or {})
            if not info:
                # An empty .info is the tell-tale of a 429 / unknown symbol. Raise
                # so _retry backs off, and so the tag-along key below can never make
                # an empty pull look like data (cached all day, data_ok=True).
                raise RuntimeError("empty info (rate-limited or unknown symbol)")
            info["_net_upgrades_90d"] = _net_upgrades_90d(tk)
            return info

        info = self._retry(_do, f"info[{ticker}]") or {}
        if info:
            try:
                payload = json.dumps(info, default=str)
                _atomic_write(cache, lambda p: p.write_text(payload, encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not write info cache for %s: %s", ticker, exc)
        return info

    # ------------------------------- assemble --------------------------------
    def collect_one(self, ticker: str) -> dict:
        hist = self._load_history(ticker)
        info = self._load_info(ticker)
        ind = compute_indicators(hist) if hist is not None else compute_indicators(pd.DataFrame())

        last_close = None
        last_volume = None
        if hist is not None and not hist.empty and "Close" in hist:
            closes = pd.to_numeric(hist["Close"], errors="coerce").dropna()
            if not closes.empty:
                last_close = float(closes.iloc[-1])
            if "Volume" in hist:
                vols = pd.to_numeric(hist["Volume"], errors="coerce").dropna()
                if not vols.empty:
                    last_volume = float(vols.iloc[-1])

        price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice")) or last_close
        volume = _num(info.get("regularMarketVolume")) or last_volume

        # Risk fields
        beta = _num(info.get("beta"))
        dollar_liquidity = None
        if ind.get("avg_volume_50d") and price:
            dollar_liquidity = ind["avg_volume_50d"] * price

        record = {
            "ticker": ticker,
            "name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector") or UNKNOWN_SECTOR,
            "industry": info.get("industry"),
            "exchange": info.get("fullExchangeName") or info.get("exchange"),
            "currency": info.get("currency") or info.get("financialCurrency"),
            # price / volume
            "price": price,
            "volume": volume,
            "avg_volume_50d": ind.get("avg_volume_50d"),
            "week52_high": ind.get("week52_high") or _num(info.get("fiftyTwoWeekHigh")),
            "week52_low": ind.get("week52_low") or _num(info.get("fiftyTwoWeekLow")),
            # indicators
            "sma20": ind.get("sma20"), "sma50": ind.get("sma50"),
            "sma100": ind.get("sma100"), "sma200": ind.get("sma200"),
            "rsi14": ind.get("rsi14"), "macd_hist": ind.get("macd_hist"),
            "bb_high": ind.get("bb_high"), "bb_low": ind.get("bb_low"),
            "bb_mid": ind.get("bb_mid"), "atr14": ind.get("atr14"),
            "ret_1m": ind.get("ret_1m"), "ret_3m": ind.get("ret_3m"),
            "ret_6m": ind.get("ret_6m"),
            # fundamentals
            "market_cap": _num(info.get("marketCap")),
            "revenue_growth": _num(info.get("revenueGrowth")),
            "earnings_growth": _num(info.get("earningsGrowth"))
            if info.get("earningsGrowth") is not None
            else _num(info.get("earningsQuarterlyGrowth")),
            "trailing_pe": _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "peg": _num(info.get("trailingPegRatio")) or _num(info.get("pegRatio")),
            "gross_margin": _num(info.get("grossMargins")),
            "operating_margin": _num(info.get("operatingMargins")),
            "net_margin": _num(info.get("profitMargins")),
            "roe": _num(info.get("returnOnEquity")),
            "roa": _num(info.get("returnOnAssets")),
            "debt_to_equity": _num(info.get("debtToEquity")),
            "free_cash_flow": _num(info.get("freeCashflow")),
            "total_cash": _num(info.get("totalCash")),
            "eps": _num(info.get("trailingEps")),
            "forward_eps": _num(info.get("forwardEps")),
            # analyst
            "recommendation_mean": _num(info.get("recommendationMean")),
            "target_mean_price": _num(info.get("targetMeanPrice")),
            "num_analysts": _num(info.get("numberOfAnalystOpinions")),
            "net_upgrades_90d": _num(info.get("_net_upgrades_90d")),
            # risk
            "beta": beta,
            "volatility_annual": ind.get("volatility_annual"),
            "max_drawdown_1y": ind.get("max_drawdown_1y"),
            "dollar_liquidity": dollar_liquidity,
            # provenance
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "data_ok": bool(hist is not None and not hist.empty) or bool(info),
        }

        # Optional sentiment providers (disabled by default -> Nones).
        record.update(fetch_sentiment(ticker, self.cfg.optional_providers))
        return record

    def collect(self, tickers: list[str]) -> list[dict]:
        """Collect records for all tickers. Failures yield a sparse record, not a crash."""
        if not self._version_check():
            log.error("yfinance is unavailable — every network fetch this run "
                      "will fail fast; only fresh same-day cache can serve data")
        records: list[dict] = []
        total = len(tickers)
        for i, tk in enumerate(tickers, 1):
            log.info("collecting %d/%d %s", i, total, tk)
            try:
                records.append(self.collect_one(tk))
            except Exception as exc:  # noqa: BLE001
                log.error("collect failed for %s: %s (recording sparse row)", tk, exc)
                records.append({"ticker": tk, "sector": UNKNOWN_SECTOR, "name": tk,
                                "data_ok": False,
                                "collected_at": datetime.now(timezone.utc).isoformat()})
        ok = sum(1 for r in records if r.get("data_ok"))
        log.info("collected %d/%d tickers with usable data", ok, total)
        return records

    def _version_check(self) -> bool:
        """True if yfinance imports. Cached same-day data works without it,
        so an unimportable yfinance is loud but not fatal (see collect())."""
        try:
            import yfinance as yf
        except Exception as exc:  # noqa: BLE001
            log.error("yfinance not importable: %s", exc)
            return False
        installed = getattr(yf, "__version__", "0")
        if _ver_tuple(installed) < _ver_tuple(self.dcfg.yfinance_min_version):
            log.warning("yfinance %s is older than configured minimum %s; "
                        "pulls may misbehave", installed, self.dcfg.yfinance_min_version)
        else:
            log.info("yfinance version %s", installed)
        return True


def _net_upgrades_90d(tk) -> float | None:
    """Best-effort upgrades minus downgrades in the last 90 days. Often missing."""
    try:
        ud = tk.upgrades_downgrades
    except Exception:  # noqa: BLE001
        return None
    if ud is None or len(ud) == 0:
        return None
    try:
        df = ud.copy()
        if "GradeDate" in df.columns:
            df = df.set_index("GradeDate")
        df.index = pd.to_datetime(df.index, errors="coerce")
        cutoff = pd.Timestamp.now(tz=df.index.tz) - pd.Timedelta(days=90)
        recent = df[df.index >= cutoff]
        if recent.empty:
            return 0.0
        actions = recent.get("Action")
        if actions is None:
            return None
        ups = int((actions.astype(str).str.lower() == "up").sum())
        downs = int((actions.astype(str).str.lower() == "down").sum())
        return float(ups - downs)
    except Exception:  # noqa: BLE001
        return None


def _ver_tuple(v: str) -> tuple:
    parts = []
    for p in str(v).split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)
