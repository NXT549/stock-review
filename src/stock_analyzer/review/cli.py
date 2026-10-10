"""Command-line entry point: parse args, resolve the ticker universe, fetch,
FX-normalise and write the report. The root ``stock_review.py`` shim and
``python -m stock_analyzer.review`` both call ``main()``."""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

# Force UTF-8 on our console streams. Status lines contain non-ASCII (em-dashes, >=,
# arrows); on a legacy Windows code page a raw print can raise UnicodeEncodeError and
# kill a long run. errors="replace" degrades a stray glyph to '?' instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # non-reconfigurable stream (e.g. piped)
        pass

try:
    from . import ascii_art
except Exception:  # pragma: no cover - animation is a nice-to-have, not required
    ascii_art = None

from . import universe
from .fetch import _fetch_batch, fetch_fx_rates, to_usd_market_cap
from .format import size_bucket
from .report import build_markdown, stocks_csv, stocks_json
from .symbols import clean_symbol
from .universe import (DEFAULT_TICKERS, REGION_RECORDS, SECTORS, SOURCES,
                       describe_group, loose_sector_tickers, print_groups,
                       read_tickers_file, resolve_sector_key, tickers_for_sector,
                       verify_loose_industry)


def resolve_tickers(args) -> list[str]:
    # Several ticker sources can be supplied at once; say which one wins instead
    # of silently ignoring the rest. --sector + --source is a genuine combination
    # (source pins the region), not a conflict.
    given = [name for name, g in (("positional tickers", bool(args.tickers)),
                                  ("--file", bool(args.file)),
                                  ("--sector", bool(args.sector)),
                                  ("--source", bool(args.source))) if g]
    if len(given) > 1 and given != ["--sector", "--source"]:
        print(f"Note: {' and '.join(given[1:])} ignored — {given[0]} takes "
              f"precedence.", file=sys.stderr)
    if args.tickers:
        # accept "AAPL,MSFT" / "AAPL, MSFT" typed as one argument, not just spaced args
        tickers = [clean_symbol(t) for arg in args.tickers
                   for t in arg.replace(",", " ").split()]
    elif args.file:
        tickers = [clean_symbol(t) for t in read_tickers_file(Path(args.file))]
    elif args.sector:
        sector_key = resolve_sector_key(args.sector)
        region = args.source or "global"  # --sector alone spans US + Australia
        if region not in REGION_RECORDS:
            raise SystemExit(
                f"--sector needs a region source with sector data; got --source "
                f"'{region}'. Use one of: {', '.join(sorted(REGION_RECORDS))}.")
        print(f"Fetching '{region}' constituents and filtering to sector "
              f"'{sector_key}'...", file=sys.stderr)
        tickers = tickers_for_sector(sector_key, region)
        if not tickers:
            raise SystemExit(f"No '{sector_key}' names found in region '{region}'.")
    elif args.source:
        print(f"Fetching '{args.source}' constituent list...", file=sys.stderr)
        tickers = SOURCES[args.source]()
    else:
        tickers = list(DEFAULT_TICKERS)
    # de-dupe, preserve order
    seen, ordered = set(), []
    for t in tickers:
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    if args.limit:
        ordered = ordered[: args.limit]
    return ordered


def main(argv=None, *, progress=None, cancel_event=None) -> int:
    """Run a review. ``progress`` is an optional injected animation/progress sink
    (the GUI passes its ``QueueProgress``); terminal callers omit it and keep the
    real-TTY ANSI animation. It is threaded straight into ``_fetch_batch``.

    ``cancel_event`` (a ``threading.Event``, passed by the GUI's Cancel button) is
    threaded into the fetch and the retry pauses so a run stops promptly; when it
    is set we stop fetching, write a PARTIAL report from whatever was already
    fetched (nothing is written only when nothing was fetched), and return 130
    (the SIGINT code)."""

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _note(text) -> None:
        """Push a one-line status onto an injected progress sink (the GUI shows
        it on the animation HUD). Terminal callers just keep the stderr prints."""
        if progress is not None and hasattr(progress, "note"):
            try:
                progress.note(text)
            except Exception:  # noqa: BLE001 - status display is never fatal
                pass
    p = argparse.ArgumentParser(
        description="Grab current info on a watchlist and write one clean Markdown "
                    "file for an AI to review.")
    p.add_argument("tickers", nargs="*", help="Tickers to review (overrides --source/--file).")
    p.add_argument("--file", help="Path to a file of tickers (one per line, or a 'ticker' CSV column).")
    p.add_argument("--source", choices=sorted(SOURCES), help="Named universe / region group: "
                   "curated (default), US (sp500/sap, sp400/midcap, sp600/smallcap, sp1500/us/america), "
                   "small-cap sleeves (america-smallcap, australia-smallcap, global-smallcap), "
                   "Australia (australia/a200 = the ASX 200, asx-all/australia-all = every ASX "
                   "listing ~2,000), other markets (japan, uk, germany, hongkong, canada, india), "
                   "or combos (global, international/intl, world/everything/all).")
    p.add_argument("--sector", help="Filter to one sector/industry group: energy, mining, "
                   "telecom, materials, industrials, technology, financials, healthcare, "
                   "consumer, utilities, realestate, communications (aliases: tech, finance, "
                   "health, telco, miners, oil, property). Spans US + Australia by default; "
                   "combine with --source to pin the region (e.g. --source australia --sector mining).")
    p.add_argument("--limit", type=int, help="Cap the number of tickers.")
    p.add_argument("--out", help="Output Markdown path (default: reports/stock_review_<date>.md).")
    p.add_argument("--workers", type=int, default=6, help="Parallel fetch workers (default 6). "
                   "Lower it (e.g. 2-3) for big runs if you see lots of failures (rate limiting).")
    p.add_argument("--cache-dir", default="data/review_cache",
                   help="Per-ticker daily cache so a rate-limited run RESUMES on rerun "
                        "(skips tickers already fetched today). Default: data/review_cache")
    p.add_argument("--no-cache", action="store_true",
                   help="Disable the on-disk resume cache (always refetch every ticker).")
    p.add_argument("--retry-rounds", type=int, default=4,
                   help="Extra gentle retry passes for rate-limited tickers within one run "
                        "(default 4); each waits longer and uses fewer workers.")
    p.add_argument("--jitter", type=float, default=0.3,
                   help="Max random seconds to stagger each request, easing 429s (default 0.3).")
    p.add_argument("--rich", action="store_true",
                   help="Verbose, human-friendly output instead of the default lean table: "
                        "per-cell units ($3.2T, +12.6%%), 'n/a' for missing, spelled-out "
                        "sectors/analyst ratings, and a Size column. Use when a person, not an "
                        "AI, is the reader.")
    p.add_argument("--lean", action="store_true", help=argparse.SUPPRESS)  # back-compat: lean is now the default
    p.add_argument("--no-names", action="store_true",
                   help="Also drop the company Name column from the lean table (the Ticker "
                        "still identifies each row) for maximum token savings on large runs.")
    p.add_argument("--format", choices=["md", "csv", "json"], default="md",
                   help="Output format. md (default): the full Markdown report with prose + "
                        "legend. csv: just the raw stock grid (no wrapper/prose), pipeable. "
                        "json: the stock records + failures as JSON. csv/json still honour "
                        "--rich and --no-names for the grid columns.")
    p.add_argument("--list-groups", action="store_true",
                   help="List the available region (--source) and sector (--sector) groups, "
                        "then exit. No network access.")
    p.add_argument("--no-animation", action="store_true",
                   help="Disable the ASCII loading animation that plays "
                        "during the fetch. Auto-disabled anyway when output isn't a terminal.")
    p.add_argument("--anim-seconds", type=float, default=10.0,
                   help="Seconds each loading animation shows before cycling to the next "
                        "(default 10; the widescreen scenes deserve a moment).")
    args = p.parse_args(argv)

    if args.limit is not None and args.limit <= 0:
        # 0 used to silently mean "no limit" and negatives sliced from the END
        # of the list; both are surprises, not caps.
        p.error("--limit must be a positive whole number")
    args.jitter = max(0.0, args.jitter)  # a negative value would crash time.sleep

    if args.list_groups:
        print_groups()
        return 0

    # The constituent scrapes (inside resolve_tickers) share the ticker cache's
    # directory and --no-cache flag, so a rerun skips the Wikipedia/ASX round-trips.
    universe.CACHE_DIR = None if args.no_cache else Path(args.cache_dir)

    tickers = resolve_tickers(args)
    if not tickers:
        print("No tickers to review.", file=sys.stderr)
        return 2

    run_day = date.today().isoformat()
    workers = max(1, args.workers)
    use_anim = (ascii_art is not None and not args.no_animation
                and sys.stderr.isatty() and ascii_art.enable_ansi())
    cache_dir = None if args.no_cache else Path(args.cache_dir)
    cache_note = "" if cache_dir is None else f"  (cache: {cache_dir}/{run_day})"
    print(f"Fetching {len(tickers)} tickers with {workers} worker(s){cache_note}...",
          file=sys.stderr)
    rows, failures, n_cached = _fetch_batch(tickers, workers, cache_dir=cache_dir,
                                            run_day=run_day, jitter=args.jitter,
                                            animate=use_anim, anim_seconds=args.anim_seconds,
                                            animator=progress, cancel_event=cancel_event)

    # Gentle retry rounds: once a burst of 429s eases, slower passes recover
    # stragglers. Successes are cached as we go, so each round only retries what's
    # still missing and progress accumulates on disk. Skipped entirely if the FIRST
    # pass got nothing (that's a network/IP block, not transient throttling).
    rnd = 0
    while failures and rnd < max(0, args.retry_rounds) and rows and not _cancelled():
        rnd += 1
        retry_workers = max(1, min(3, workers))
        pause = 5 * rnd
        print(f"\n[retry {rnd}/{args.retry_rounds}] {len(failures)} still missing; "
              f"pausing {pause}s then retrying with {retry_workers} worker(s)...",
              file=sys.stderr)
        _note(f"retry round {rnd}/{args.retry_rounds}: {len(failures)} ticker(s) "
              f"still missing - pausing {pause}s")
        # Cancellable pause: bail the instant the user cancels rather than sleeping
        # out the (growing) backoff first.
        if cancel_event is not None:
            if cancel_event.wait(pause):
                break
        else:
            time.sleep(pause)
        _note(f"retry round {rnd}/{args.retry_rounds}: re-attempting "
              f"{len(failures)} ticker(s)")
        recovered, failures, _ = _fetch_batch(
            [f["ticker"] for f in failures], retry_workers, cache_dir=cache_dir,
            run_day=run_day, jitter=max(args.jitter, 0.5),
            animate=use_anim, anim_seconds=args.anim_seconds, animator=progress,
            cancel_event=cancel_event)
        rows += recovered
    if rnd:
        _note(None)   # retry rounds over -> give the caption line back

    # A cancelled run still writes a report from whatever it managed to fetch —
    # an aborted 20-minute run shouldn't leave an empty file. Only a cancel with
    # NOTHING fetched writes nothing.
    cancelled = _cancelled()
    if cancelled:
        if not rows:
            print("\nCancelled before anything was fetched - nothing to write.",
                  file=sys.stderr)
            return 130
        print(f"\nCancelled - writing a partial report with the {len(rows)} "
              f"ticker(s) fetched so far...", file=sys.stderr)

    if not rows:
        failure_text = ("Could not fetch ANY tickers (network/yfinance issue or IP "
                        "rate-limited - wait a few minutes, lower --workers, then "
                        "rerun: cached tickers are skipped so you resume where you "
                        "left off).")
        print(failure_text, file=sys.stderr)
        _note(failure_text)  # GUI worker keeps this as the terminal note (see _finish)
        return 1

    # Sector precision pass: the ASX table has no sub-industry column, so AU names
    # enter mining/telecom via the coarse parent-sector fallback. Now that we have
    # each name's yfinance industry, drop the ones that clearly aren't in the group
    # (e.g. media/internet names that sit in 'Communication Services'). US names and
    # plain GICS-sector groups are already precise, so they're never touched.
    sector_key = resolve_sector_key(args.sector) if args.sector else None
    region = (args.source or "global") if sector_key else args.source
    excluded: list[dict] = []
    if sector_key:
        loose = loose_sector_tickers(sector_key, region)
        rows, excluded = verify_loose_industry(rows, loose, SECTORS[sector_key])
        if excluded:
            print(f"Sector precision pass: excluded {len(excluded)} off-sector name(s): "
                  f"{', '.join(e['ticker'] for e in excluded)}", file=sys.stderr)
        if not rows:
            print(f"No '{sector_key}' names left after the precision pass.", file=sys.stderr)
            return 1

    # Normalise market caps to USD (so Size buckets are comparable across markets).
    currencies = {r.get("currency", "USD") for r in rows}
    if currencies - {"USD"}:
        print(f"Fetching FX rates for {sorted(currencies - {'USD'})}...", file=sys.stderr)
    fx = fetch_fx_rates(currencies, workers=workers)
    for r in rows:
        r["usd_market_cap"] = to_usd_market_cap(r, fx)
        r["size"] = size_bucket(r["usd_market_cap"])

    group_label = describe_group(args, sector_key, region)
    if args.format == "csv":
        content = stocks_csv(rows, lean=not args.rich, drop_names=args.no_names)
    elif args.format == "json":
        content = stocks_json(rows, failures, run_day,
                              lean=not args.rich, drop_names=args.no_names)
    else:
        content = build_markdown(rows, failures, run_day, lean=not args.rich,
                                 group_label=group_label, excluded=excluded,
                                 drop_names=args.no_names)
        if cancelled:   # csv/json stay clean data; the banner is md-only
            content = (f"> **Partial report:** the run was cancelled after "
                       f"{len(rows)} of {len(tickers)} tickers. Rerun the same "
                       f"setup to fill in the rest (fetched tickers are cached "
                       f"for the day).\n\n" + content)

    out_path = (Path(args.out) if args.out
                else Path("reports") / f"stock_review_{run_day}.{args.format}")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
    except OSError as e:  # bad/locked/read-only path: say so; fetched tickers stay cached
        print(f"Could not write '{out_path}': {e}. Fetched tickers are cached for "
              "today - fix the path (--out) and rerun to finish instantly.", file=sys.stderr)
        _note(f"Could not write report: {e}")
        return 1

    print(f"\nWrote {out_path}  ({len(rows)} stocks"
          + (", partial - run cancelled" if cancelled else "")
          + (f", {n_cached} from cache" if n_cached else "")
          + (f", {len(failures)} still failed" if failures else "") + ")", file=sys.stderr)
    if failures and cache_dir is not None and not cancelled:
        print(f"  {len(failures)} tickers were rate-limited. Rerun the SAME command in a "
              "few minutes - cached tickers are skipped, so it resumes and fills the gaps.",
              file=sys.stderr)
    print(str(out_path))
    return 130 if cancelled else 0
