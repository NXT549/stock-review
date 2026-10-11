"""Fetch one ticker's fundamentals + price history from yfinance, with a per-day
resume cache, FX normalisation and concurrent batching. Never fabricates a value:
anything unavailable stays None."""
from __future__ import annotations

import json
import logging
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency: yfinance.  Install with:  pip install yfinance pandas")

# yfinance logs every 404/429 itself; we already report failures cleanly, so quiet
# its internal logger to keep the console readable on big multi-thousand-name runs.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Our own logger. Silent by default (no handler configured, root defaults to
# WARNING); enable with ``logging.basicConfig(level=logging.DEBUG)`` to surface the
# debug diagnostics (e.g. a systemic history-fetch failure) when investigating.
log = logging.getLogger("stock_review")

# Optional ASCII loading animation (see ascii_art.py). It's purely cosmetic, so a
# missing module must never stop a fetch: fall back to None and the plain prints.
try:
    from . import ascii_art
except Exception:  # pragma: no cover - animation is a nice-to-have, not required
    ascii_art = None

from .format import NA, _num


def dividend_yield_fraction(info: dict, cur_price) -> float | None:
    """Return dividend yield as a clean fraction (0.0038 = 0.38%), or None.

    Only unambiguous sources are used: the annual dividend rate / price, then the
    trailing-annual yield (a true fraction). The raw ``dividendYield`` field is
    deliberately NOT used as a fallback: it is inconsistent across yfinance
    versions (some return 0.0038, some 0.38 for the same 0.38% yield), so guessing
    its scale can emit a yield that is wrong by 100x. Per the honesty rule we would
    rather leave Div blank (None) than report a possibly-wrong number.
    """
    rate = _num(info.get("dividendRate")) or _num(info.get("trailingAnnualDividendRate"))
    p = _num(cur_price)
    if rate is not None and p:
        return rate / p
    taq = _num(info.get("trailingAnnualDividendYield"))
    if taq is not None and 0 <= taq < 0.5:  # sane fraction
        return taq
    return None  # no unambiguous source -> honest blank, never a scale guess


def _fx_base(currency: str) -> str:
    """Map a yfinance currency code to its FX base. GBp/GBX (pence) -> GBP."""
    c = (currency or "USD").upper()
    return "GBP" if c in ("GBP", "GBX") else c


def _fetch_fx_one(base: str):
    """Fetch one {base}USD rate. Returns (base, rate) or (base, None) if unpriceable."""
    try:
        h = yf.Ticker(f"{base}USD=X").history(period="5d")
        close = h["Close"].dropna() if h is not None and not h.empty else None
        if close is not None and len(close):
            return base, float(close.iloc[-1])
    except Exception:
        pass
    return base, None


def fetch_fx_rates(currencies, *, workers: int = 6) -> dict[str, float]:
    """Return {currency_base: rate_to_USD}. Only fetches the non-USD ones seen.

    Rates are pulled concurrently (an ``international`` run needs 6-7 of them; doing
    them serially tacked a needless wait onto the end of every such run). Any
    currency we can't price is reported (its rows then show an 'n/a' USD market cap
    and Size) rather than silently dropped, so the gap is visible not invented.
    """
    rates = {"USD": 1.0}
    needed = sorted({_fx_base(c) for c in currencies} - {"USD"})
    missing: list[str] = []
    if needed:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(needed)))) as ex:
            for base, rate in ex.map(_fetch_fx_one, needed):  # ex.map preserves order
                if rate is not None:
                    rates[base] = rate
                else:
                    missing.append(base)
    if missing:
        print(f"  FX unavailable for {', '.join(missing)}; USD market cap and Size "
              "show n/a for those rows.", file=sys.stderr)
    return rates


def to_usd_market_cap(row: dict, fx: dict[str, float]):
    """USD-normalise a row's local-currency market cap using fetched FX rates."""
    mc = _num(row.get("market_cap"))
    if mc is None:
        return None
    rate = fx.get(_fx_base(row.get("currency")))
    return mc * rate if rate is not None else None


def _cache_file(cache_dir: Path, ticker: str, run_day: str) -> Path:
    safe = ticker.replace("/", "_").replace("\\", "_")
    return cache_dir / run_day / f"{safe}.json"


def _read_cache(cache_dir, ticker: str, run_day: str):
    if cache_dir is None:
        return None
    p = _cache_file(cache_dir, ticker, run_day)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None  # unreadable/partial cache -> just refetch
    return data if isinstance(data, dict) and not data.get("error") else None


def _write_cache(cache_dir, ticker: str, run_day: str, rec: dict) -> None:
    if cache_dir is None or rec.get("error"):
        return
    try:
        p = _cache_file(cache_dir, ticker, run_day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass  # caching is best-effort; never fail the fetch over it


def _is_rate_limit(err) -> bool:
    s = (type(err).__name__ + " " + str(err)).lower()
    return any(k in s for k in ("ratelimit", "rate limit", "429", "too many requests"))


def fetch_one(ticker: str, retries: int = 3, base_backoff: float = 1.5,
              *, cache_dir=None, run_day: str = "", jitter: float = 0.0,
              cancel_event=None, on_retry=None) -> dict:
    """Pull current fundamentals + price history for one ticker.

    Returns a flat dict of derived, display-ready values. Anything unavailable is
    left as None (rendered "n/a"). Retries with exponential backoff + jitter, and
    backs off harder on an explicit rate-limit error. An empty pull (no name, cap,
    or price — the tell-tale of a 429) is treated as a failure and retried, so it
    lands honestly in "Could not fetch" rather than as a silent blank row.

    If `cache_dir` is set, a previously cached success for (ticker, run_day) is
    returned immediately (the resume path), and fresh successes are cached.

    ``cancel_event`` (a ``threading.Event``) lets the caller abort a run: it is
    checked before starting and between retries, and the backoff sleep waits on it
    so a cancel interrupts the wait instead of blocking on it. When set, we return
    a lightweight ``{"cancelled": True}`` record that the batch loop skips.

    ``on_retry(ticker, attempt, max_retries, delay)`` is called (from this worker
    thread) just before each backoff wait, so the caller can surface in-flight
    re-attempts instead of them happening invisibly. Purely informational — any
    exception it raises is swallowed.
    """
    if cancel_event is not None and cancel_event.is_set():
        return {"ticker": ticker, "name": ticker, "cancelled": True}
    cached = _read_cache(cache_dir, ticker, run_day)
    if cached is not None:
        cached["_cached"] = True
        return cached
    last_err = None
    for attempt in range(retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            return {"ticker": ticker, "name": ticker, "cancelled": True}
        try:
            # Stagger worker bursts to ease 429s. Inside the try so a bad jitter
            # value (time.sleep raises on negatives) degrades to a retryable
            # failure instead of escaping the worker uncaught; first attempt
            # only — the backoff already spaces the retries.
            if jitter and attempt == 0:
                time.sleep(random.uniform(0.0, jitter))
            tk = yf.Ticker(ticker)
            info = tk.info or {}

            # 1y daily history for momentum + moving averages + 52-week range
            closes = None
            highs = lows = None
            try:
                hist = tk.history(period="1y", auto_adjust=True)
                if hist is not None and not hist.empty:
                    closes = hist["Close"].dropna()
                    highs = hist["High"].dropna()
                    lows = hist["Low"].dropna()
            except Exception as hist_err:
                # A valid .info but a failed history pull leaves this name with no
                # momentum/MA/52-week columns — otherwise indistinguishable from a
                # market that genuinely has no history. Log at DEBUG so a systemic
                # failure (e.g. a yfinance API-shape change) is visible on demand,
                # without noising up a normal run. The fetch still succeeds.
                log.debug("history fetch failed for %s: %s", ticker, hist_err)

            def ret(days):
                if closes is None or len(closes) <= days:
                    return None
                base = closes.iloc[-1 - days]
                if not base:
                    return None
                return float(closes.iloc[-1]) / float(base) - 1.0

            last = float(closes.iloc[-1]) if closes is not None and len(closes) else None
            cur_price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice")) or last

            # Only report a 1-year return with ~a full year of history. Otherwise the
            # first available close (e.g. an IPO/spin-off a few months ago) would be
            # reported under the "1y" column as if it spanned a year — a mislabel. The
            # shorter windows already gate on length via ret(); this makes 1y consistent.
            ret_1y = None
            if closes is not None and len(closes) >= 240:
                first = float(closes.iloc[0])
                if first:
                    ret_1y = last / first - 1.0

            ma50 = float(closes.tail(50).mean()) if closes is not None and len(closes) >= 50 else _num(info.get("fiftyDayAverage"))
            ma200 = float(closes.tail(200).mean()) if closes is not None and len(closes) >= 200 else _num(info.get("twoHundredDayAverage"))

            hi52 = _num(info.get("fiftyTwoWeekHigh")) or (float(highs.max()) if highs is not None and len(highs) else None)
            lo52 = _num(info.get("fiftyTwoWeekLow")) or (float(lows.min()) if lows is not None and len(lows) else None)

            def dist(p, ref):
                if p is None or ref in (None, 0):
                    return None
                return p / ref - 1.0

            # analyst upside vs current price
            target = _num(info.get("targetMeanPrice"))
            upside = dist(target, cur_price) if (target and cur_price) else None

            # PEG moved around across yfinance versions; try both spellings
            peg = _num(info.get("trailingPegRatio")) or _num(info.get("pegRatio"))

            # An empty pull (no name/cap/price) means rate-limited or unknown symbol.
            # Raise so it retries and, if still empty, fails honestly (no blank row).
            if not (info.get("shortName") or info.get("longName")
                    or info.get("marketCap") or cur_price is not None):
                raise RuntimeError("empty result (rate-limited or unknown symbol)")

            rec = {
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName") or ticker,
                "sector": info.get("sector") or NA,
                "industry": info.get("industry") or NA,
                "price": cur_price,
                "market_cap": _num(info.get("marketCap")),  # in 'currency' below
                # keep raw code (don't upper) so pence 'GBp' stays distinct from 'GBP'
                "currency": info.get("currency") or "USD",
                # 'usd_market_cap' and 'size' are filled in after FX normalisation
                # valuation
                "pe": _num(info.get("trailingPE")),
                "fwd_pe": _num(info.get("forwardPE")),
                "peg": peg,
                "pb": _num(info.get("priceToBook")),
                "ps": _num(info.get("priceToSalesTrailing12Months")),
                # quality / profitability
                "roe": _num(info.get("returnOnEquity")),
                "margin": _num(info.get("profitMargins")),
                "rev_growth": _num(info.get("revenueGrowth")),
                "earnings_growth": _num(info.get("earningsGrowth")),
                "debt_equity": _num(info.get("debtToEquity")),
                "div_yield": dividend_yield_fraction(info, cur_price),
                # momentum / technicals
                "ret_1m": ret(21),
                "ret_3m": ret(63),
                "ret_6m": ret(126),
                "ret_1y": ret_1y,
                "ma50": ma50,
                "ma200": ma200,
                "vs_ma50": dist(cur_price, ma50),
                "vs_ma200": dist(cur_price, ma200),
                "hi52": hi52,
                "lo52": lo52,
                "from_hi": dist(cur_price, hi52),
                "from_lo": dist(cur_price, lo52),
                "beta": _num(info.get("beta")),
                # analyst
                "rec": (info.get("recommendationKey") or NA).replace("_", " "),
                "n_analysts": _num(info.get("numberOfAnalystOpinions")),
                "target": target,
                "upside": upside,
                "error": None,
            }
            _write_cache(cache_dir, ticker, run_day, rec)
            return rec
        except Exception as e:  # noqa: BLE001 - we want to keep going across tickers
            last_err = e
            if attempt < retries:
                delay = base_backoff * (2 ** attempt) + random.uniform(0, 0.5)
                if _is_rate_limit(e):
                    delay = max(delay, 8 + random.uniform(0, 7))  # 429: wait longer
                if on_retry is not None:
                    try:
                        on_retry(ticker, attempt + 1, retries, delay)
                    except Exception:  # noqa: BLE001 - display must never kill a fetch
                        pass
                # A cancellable wait: Event.wait returns True the moment the caller
                # cancels, so a 429 backoff (up to ~15s) doesn't stall the abort.
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        return {"ticker": ticker, "name": ticker, "cancelled": True}
                else:
                    time.sleep(delay)
    return {"ticker": ticker, "name": ticker, "error": str(last_err) or "fetch failed"}


def _fetch_batch(tickers: list[str], workers: int, *, cache_dir=None,
                 run_day: str = "", jitter: float = 0.0, animate: bool = False,
                 anim_seconds: float = 10.0, animator=None, cancel_event=None
                 ) -> tuple[list[dict], list[dict], int]:
    """Fetch a list of tickers concurrently; return (rows, failures, n_cached).

    If ``animate`` is set (and ascii_art is importable and stderr is a TTY), a
    background ASCII animation with a live progress bar plays for the duration
    and the per-ticker progress prints are suppressed in its favour. Otherwise
    we keep the plain milestone prints (every 25, plus each failure).

    ``animator`` lets a caller **inject** its own progress sink (e.g. the GUI's
    ``QueueProgress``, which forwards to a Tk animation) instead of the terminal
    ANSI ``Animator``. An injected animator is *owned by the caller* — we only
    feed it ``update()`` and never call ``start()``/``stop()`` — so its animation
    runs continuously across the initial fetch and every retry round."""
    rows: list[dict] = []
    failures: list[dict] = []
    done = 0
    n_cached = 0
    total = len(tickers)
    lock = threading.Lock()

    owns_animator = False
    if animator is None and animate and ascii_art is not None and sys.stderr.isatty():
        animator = ascii_art.Animator(total, switch_seconds=anim_seconds)
        animator.start()
        owns_animator = True
    elif animator is not None:
        animator.total = total  # tell the injected sink this batch's size

    # Surface in-fetch re-attempts (they used to happen invisibly): an injected
    # sink with a ``retry`` method (the GUI's QueueProgress) gets them as events;
    # plain-print mode logs a line per backoff. The terminal ANSI animator owns
    # the screen, so nothing is printed under it — its HUD shows the totals.
    if animator is not None and hasattr(animator, "retry"):
        def on_retry(ticker, attempt, max_retries, delay):
            animator.retry(ticker=ticker, attempt=attempt,
                           max_retries=max_retries, delay=delay)
    elif animator is None:
        def on_retry(ticker, attempt, max_retries, delay):
            print(f"  [retry] {ticker}: waiting {delay:.0f}s, then attempt "
                  f"{attempt}/{max_retries}", file=sys.stderr)
    else:
        on_retry = None

    def _absorb(rec: dict) -> None:
        nonlocal done, n_cached
        if rec.get("cancelled"):
            return  # a worker that bailed on cancel — don't count it
        with lock:
            done += 1
            if rec.get("error"):
                failures.append(rec)
                flag = "FAIL"
            elif rec.pop("_cached", False):
                rows.append(rec)
                n_cached += 1
                flag = "cached"
            else:
                rows.append(rec)
                flag = "ok"
            if animator is not None:
                extra = {"rec": rec} if getattr(animator, "wants_rec", False) else {}
                animator.update(done, ticker=rec["ticker"],
                                ok=len(rows), failed=len(failures), **extra)
            elif done % 25 == 0 or done == total or rec.get("error"):
                print(f"  [{done:>4}/{total}] {rec['ticker']:<8} {flag}", file=sys.stderr)

    absorbed: set = set()
    cancelled_early = False
    futures: dict = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_one, t, cache_dir=cache_dir, run_day=run_day,
                                 jitter=jitter, cancel_event=cancel_event,
                                 on_retry=on_retry): t
                       for t in tickers}
            for fut in as_completed(futures):
                # Cancelled mid-run: stop pulling results, drop any not-yet-started
                # tasks, and let the executor drain the few already in flight (they
                # each bail fast on the same event). Partial rows are returned as-is.
                if cancel_event is not None and cancel_event.is_set():
                    for f in futures:
                        f.cancel()
                    cancelled_early = True
                    break
                absorbed.add(fut)
                _absorb(fut.result())
        # Leaving the with-block joined the workers still in flight. On an early
        # cancel, harvest anything that completed for real anyway — it's already
        # fetched (and cached), so it belongs in the partial rows rather than
        # being dropped on the floor.
        if cancelled_early:
            for f in futures:
                if f not in absorbed and f.done() and not f.cancelled():
                    _absorb(f.result())
    finally:
        if owns_animator and animator is not None:
            animator.stop()  # injected animators are stopped by their owner
    return rows, failures, n_cached
