# IMPROVEMENTS.md — full review, findings and the changes I would make

Review of `main` at `920ac01` (Stock Review 2.3), done 2026-10-10. Covers both tools
(the scored pipeline and the "Stock Review" watchlist app), the GUI, packaging/CI,
the website and the docs.

Finding IDs (`F-xx`) are referenced from the roadmap in section 5. **Confirmed**
means I reproduced it (the scripts are in the appendix). **By inspection** means I
traced it in the code but did not run it.

---

## 1. TL;DR

- **The fundamentals are solid.** The code is careful, well commented and honest
  about missing data. All 95 tests pass (once dependencies are installed). The
  offline synthetic slice runs end to end, a live 3-market review run produced a
  correct report, and the GUI opens and looks right.
- **Several real correctness bugs affect the numbers.** The four that matter most:
  1. The backtest pools *partial-horizon* returns from recent score dates into its
     statistics (F-01).
  2. The FCF-yield and cash/market-cap metrics divide figures in different
     currencies for foreign reporters: TSM shows a 31% FCF yield and cash worth
     150% of its market cap (F-02).
  3. Two different CSV universes share one run identity, so they overwrite each
     other in the DB. The documented per-market cron recipe triggers this (F-03).
  4. Percentile scores rise with peer-group *size*: the median name in a 5-stock
     sector scores 60, but 50 in a big sector (F-04).
- **The desktop app's main rough edges.** On a 1366×768 screen the Run button and
  status bar sit below the bottom edge. Some universe hints describe a different
  universe from the one that gets fetched (e.g. `asx` claims to be the ASX 200 but
  fetches about 2,000 names). Reports from the same day overwrite each other.
- **Engineering gaps.** No CI runs the pipeline tests. `lxml` is missing from
  `requirements.txt`. The supported-Python story is inconsistent (3.11 in the
  README, 3.11–3.12 in `requirements.txt`, 3.13 for the release build), and the
  docs have drifted in a dozen small places.

---

## 2. How this review was done

- **Read all of it:** every module in `src/stock_analyzer/` (pipeline,
  `storage/`, `review/`, GUI, theme, animation engine), the scripts, all 10 test
  files, packaging (spec, `frozen_entry.py`, Inno Setup, build script), both
  workflows, `docs/index.html`, scheduling examples, the universe lists and every
  doc (README/README.txt/INSTALL.txt/CHANGES/AGENTS/DESIGN/PACKAGING).
- **Ran the test suite:** Python 3.13.16. Out of the box only 4 of 10 test files
  pass, because yfinance, SQLAlchemy and scipy are missing and
  `pip install -r requirements.txt` fails on 3.13 (as AGENTS.md warns). I then
  installed unpinned deps without `ta`: pandas 3.0.5, numpy 2.5.3, yfinance 1.7.0,
  SQLAlchemy 2.1.4, scipy 1.18.1. Result: **95 passed** under pytest, and every
  file exits 0 as a plain script. `ta` failed to build, so `indicators.py` was
  not exercised.
- **Ran the programs:**
  - `scripts/run_minimal_slice.py --synthetic`, from a different working
    directory, to test path handling.
  - A live `stock_review.py AAPL BHP.AX 7203.T` run, which hit Yahoo and fetched
    AUD/JPY FX.
  - The real Tk window under Xvfb, screenshotted at 1600×1000 and 1366×768. This
    used Python 3.12 + Tk 8.6 with a stub `yfinance` (see F-18 for why the stub
    was needed). Linux font fallback differs from Windows Consolas/MS Sans Serif.
- **Targeted probes:** one script per suspected bug (appendix).

---

## 3. What works (keep it)

**Design and honesty**
- **The rules are real and enforced in code.**
  - Missing values stay `None`, and unavailable metrics are excluded with their
    weights renormalised, never zero-filled. There's a test proving the reweight
    differs from a zero-fill.
  - Negative P/E counts as missing, and the `Unknown` sector is never a peer group.
  - Failures and sector-precision exclusions are listed in reports.
  - Lean-grid column compaction is announced in a note, never done silently.
- **The scoring is deterministic and tested:** direction, ties (`min` rank),
  small-sector fallback, metric drop rule, NaN sector, determinism.
- **There's a strong "no AI inside" boundary:** no SDKs or keys, and a test
  checks reports for leakage.

**Robustness that already works**
- **Atomic writes** with unique temp names (pipeline cache, GUI settings).
- **The pipeline cache defends against odd tickers:** Windows reserved names
  (`PRN`), path separators and dot-only tickers.
- **`save_run` is idempotent per (date, universe):** explicit child deletes,
  SQLite FKs on, a unique constraint for new DBs, and read-time dedupe in the
  backtest for old DBs.
- **The review fetcher survives Yahoo:**
  - Per-ticker backoff, with longer waits on 429s.
  - An empty pull counts as a failure.
  - Retry rounds, and a daily resume cache.
  - Cancel is threaded through every wait, and a cancelled run still writes a
    partial report (exit 130).
  - Retries are surfaced to the GUI and the CLI.
- **CSV-injection guard** on text cells only, so negative numbers are untouched.
- **DB URLs are password-masked** before reaching logs.

**Output**
- **The pipeline report is compact and self-describing:** one CSV block, a column
  dictionary with nominal weights, a sparse-row note and an unscored list.
- **Review lean/rich grids are driven by one `ColSpec` table**, with golden files
  pinning the grid.
- **The live run produced a correct, tidy report:**
  - AUD/JPY market caps were USD-normalised.
  - `Target` was dropped with a "reconstruct as Price × (1 + Upside/100)" note.
  - Rows were sorted by size.
  - The FX fetch ran in parallel.

**App and packaging**
- **The GUI is coherent:**
  - The Win98 design is clean and readable.
  - Tk stays on the main thread, with the worker only posting to a queue.
  - Run/Cancel are guarded against double-fire, and closing mid-run takes the
    graceful cancel path.
  - Settings persist atomically.
  - Dropdowns clamp to the window.
  - Contrast was deliberately tuned to WCAG AA.
- **One engine, two exes** (`frozen_entry.py` picks GUI or CLI by exe name). A
  headless self-test hook and a smoke-test hook make the frozen build verifiable.
- **`database/schema.sql` matches the ORM** (checked by regenerating it).
- **The docs for agents** (AGENTS.md / DESIGN.md) are better than most
  professional repos.

---

## 4. What doesn't work — findings

Severity: **High** = wrong numbers or silent data loss. **Medium** = a real bug or
a robustness/UX gap a user will hit. **Low** = polish, consistency or hygiene.

### 4.1 Scored pipeline — data and methodology

**F-01 (High) The backtest counts partial-horizon returns from recent dates. Confirmed.**
`backtest.py:74-101`. If a score date's horizon hasn't elapsed yet, the series
"ends early" and `_fwd_return` returns the return to the last bar with
`truncated=True`. That path was meant for delistings. A date scored 2 sessions
ago therefore contributes a 2-session return to the "1m" (21-session) statistics.
With daily runs, the most recent ~21 dates of *every* backtest are contaminated.
This hits decile means, top-N and IC, which is exactly the evidence the README
says decides whether the score means anything. The summary only says "N
truncated".

**F-02 (High) FCF yield and cash/market-cap mix currencies. Confirmed live.**
`metrics.py:189-190` divides `freeCashflow`/`totalCash` (in Yahoo's
`financialCurrency`) by `marketCap` (in the trading `currency`):

| Ticker | Trading currency | Financial currency | Result |
|---|---|---|---|
| TSM | USD | TWD | FCF/mcap **0.311**, cash/mcap **1.50** |
| BHP.AX | AUD | USD | understated ~1.5× |

The default `au_heavy` universe is full of ASX names that report in USD (BHP,
RIO, S32, WDS, …). These two fundamentals metrics are wrong for them, and the
within-sector ranks of their peers are distorted too.

**F-03 (High) Run identity collides, so universes overwrite each other. By inspection.**
`pipeline.py:57` uses `universe_name = cfg.universe.source`, which is `"csv"` for
*every* CSV watchlist. `save_run` replaces same-date runs with the same
`universe_name`, so:
- **The per-market cron recipe deletes data.** `crontab.example`'s *recommended*
  Option A uses two configs, US and ASX, both `source: csv`. When they fire on the
  same calendar date, the second run deletes the first. The backtest also pools
  them as one universe.
- **The live minimal slice clobbers the daily run.** `run_minimal_slice.py`
  without `--synthetic` writes to the main DB as `"csv"`, replacing that day's
  `run_daily.py` results.

**F-04 (High) The percentile scale depends on peer-group size. Confirmed.**
`scoring.py:72` uses `rank(pct=True)` → `rank/n`. The lowest name in a group gets
`100/n` (20 in a 5-name sector, 1 in a 100-name sector), and the group mean is
`50·(n+1)/n`. Measured: the median name in a 5-stock sector scores **60** on a
sector-relative metric, versus **50** in a 100-stock sector. Every
sector-relative metric is therefore inflated by up to ~10 points for small
sectors. That's systematic, not noise, and it flows straight into the
fundamentals category and the final score.

**F-06 (Medium) Negative debt/equity ranks as the *best* leverage. Confirmed.**
`metrics.py:188` keeps negative D/E (negative shareholder equity: buyback-heavy or
distressed names). With `lower_better` it gets the 100th percentile. P/E already
gets the right treatment (non-positive → missing); D/E needs the same.

**F-07 (Medium) Raw price is compared against dividend-adjusted history, in both tools. Confirmed live.**
History is fetched with `auto_adjust=True`, but "price" is Yahoo's raw
`currentPrice`:
- **Pipeline:** `price_vs_sma50/200` and `proximity_52w_high` (whose 52-week high
  comes from adjusted history, `data_collection.py:238`). Proximity can exceed 1.0.
- **Review grid:** `vs50d` and `vs200d` (`fetch.py:226-227`).

Measured vs-200d bias:

| Ticker | Adjusted | Raw |
|---|---|---|
| VZ | −7.5% | −10.5% |
| MO | +9.0% | +6.0% |
| BHP.AX | +9.2% | +6.8% |

That's a systematic ~2–3 point tilt toward dividend payers in the technicals
category.

**F-08 (Medium) The methodology hash misses methodology changes. By inspection.**
`util.py:114` hashes only weights and winsor/group knobs. It ignores the metric
registry (keys, directions, `sector_relative`) and the derivation/scoring code.
The 2.3 "Unknown sector" fix changed scores, but runs before and after it carry
the same `config_hash`, so the backtest can't tell them apart.

**F-09 (Medium) Config weights aren't validated, and the report prints nominal weights.**
- **A typo silently zeroes a category.** `scoring.py:165` reads
  `weights.get(c, 0.0)`, so `fundamental:` instead of `fundamentals:` drops that
  category without any error.
- **Unknown YAML keys give a bare traceback.** They surface as a dataclass
  `TypeError`.
- **The printed blend isn't what was applied.** The report's "score = …" line
  (`reporting.py:318`) shows `sentiment 10%`, but sentiment is never available,
  so the effective weights were 38.9/33.3/16.7/11.1.

**F-10 (Medium) `data_ok` is almost always true, and there's no minimum coverage to be ranked. By inspection.**
- **Empty info counts as success.** `data_collection.py:189` always adds
  `_net_upgrades_90d` to the info dict. Even an empty Yahoo response is therefore
  truthy, gets cached for the day (so reruns won't retry it) and makes
  `data_ok=True` (`:279`).
- **The log overstates "usable data".**
- **A price-only stock can top the ranking.** A stock whose info call failed but
  whose history loaded is scored on technicals and momentum alone. Category
  weights renormalise, so it can rank #1. Only `cov` hints at it.

**F-11 (Medium) The backtest's price window is fixed at 2 years. By inspection.**
`backtest.py:201`. Once score history is older than ~2 years, old score dates
fall before the first price bar. `_fwd_return` returns `(None, False)`, which is
neither counted as dropped nor reported, so those dates silently disappear.

### 4.2 Storage and operations

**F-12 (Medium) The SQLite path is relative to the working directory. Confirmed.**
Reports and logs resolve against the project root via `cfg.resolve`. The DB URL
`sqlite:///database/…` does not (`storage/db.py:54`). Running from another
directory created a *second* DB under that directory. Cron cds first so it works
there, but Task Scheduler without "Start in", ad-hoc runs and IDE runs split the
history.

**F-13 (Low) The long-lived scheduler writes forever to the first day's log. By inspection.**
`logging_setup.py:52` names the files once. `run_daily.py --schedule` (and the
Docker default) therefore logs every later day into
`stock_analyzer_<startup-date>.log`.

**F-14 (Medium) `lxml` is missing from `requirements.txt`. By inspection.**
`pd.read_html` needs lxml (or bs4 + html5lib, which is also absent). On a clean
install from `requirements.txt` the following fail with an ImportError traceback:
- The pipeline's `universe.source: sp500` (`universe.py:84`).
- The review app's `--sector`, `global`, `global-smallcap` and
  `australia-smallcap` (`review/universe.py:267`).

The README and release workflow install lxml; `requirements.txt` doesn't.

**F-30 (Low) The caches are never pruned.** By inspection. `data/cache/<ticker>/<date>_*`
and `data/review_cache/<date>/` grow by one set of files per ticker per day.
For `au_heavy` run on weekdays, that's ~170k files a year.

**F-31 (Low) It's slow by construction.** By inspection.
- **Collection is serial:** pipeline `collect()` (`data_collection.py:293`) does
  one ticker at a time with a 1 s pause and two requests each. That's ~15 min
  for 329 names and ~45 min for 1,000.
- **`batch_size` does nothing:** `data.batch_size` is documented as batched
  downloads but is never read.
- **The backtest also downloads one ticker at a time.**

### 4.3 Review app — CLI and report

**F-15 (Medium) Constituent scrapes have no retry and no fallback, and crash with a traceback. Confirmed.**
`review/universe.py:139-149, 264, 338`. If Wikipedia is unreachable, `--source
sp500` dies with a raw `requests.ConnectionError` traceback. There's no retry
(the pipeline's `universe._get` has one) and no fallback to yesterday's cached
constituent list, which is sitting on disk under `_universe/<date>/`.

**F-16 (Medium) Cache files hit Windows reserved names, so resume breaks for those tickers. By inspection.**
`fetch.py:110` only replaces slashes. `CON.DE` (Continental, a DAX member) and
`PRN.AX` are both in the bundled `config/universe/global.txt`, the README's
headline "big run". On Windows, `CON.DE.json` and `PRN.json` can't be created.
The write error is swallowed, so those tickers are never cached and refetch on
every resume. The pipeline already solved this (`_safe_cache_name`); the review
tool doesn't reuse it.

**F-17 (Medium) Same-day reports overwrite each other. By inspection.**
The default name is `stock_review_<date>.<fmt>` (`cli.py:286`, mirrored in the
GUI). Reviewing the ASX 200 and then the S&P 500 on the same day silently
replaces the first report. Recent reports dedupes by path, so the first is simply
gone.

**F-22 (Low) Row order isn't deterministic for rows with no market cap. Confirmed.**
`report.py:15` sorts only by USD market cap. Ties (e.g. missing caps or failed FX)
keep thread-completion order, so two runs over the same data can order rows
differently. That breaks the determinism principle.

**F-23 (Low) A missing currency is guessed as USD.** By inspection. `fetch.py:258`
uses `info.get("currency") or "USD"`, which then converts the cap at rate 1.0.
That's a guess, which the honesty rule forbids.

**F-24 (Low) Ugly names for ASX listings. Confirmed.** `fetch.py:252` prefers
`shortName`, giving names like `BHP GROUP FPO [BHP]`. The pipeline prefers
`longName` ("BHP Group Limited").

**F-25 (Low) The rich legend is incomplete.** `report.py:117`. It omits 6m,
vs 50d, Beta, Analyst and # An. It also says "52w" where the headers are
"From 52w hi/lo".

**F-26 (Low) Token waste and a misleading default.**
- **Irrelevant text for the AI.** Both instruction blocks spend tokens telling the
  reviewing AI about `run_daily.py` (`report.py:104-108, 138-139`), which is
  irrelevant to it and to app users.
- **Wrong default.** `build_markdown(lean=False)` is the default although its
  docstring says lean is.

**F-27 (Low) The sector taxonomy is mislabelled.** The code, report and docs say
"GICS sector". But yfinance's `sector` is Yahoo/Morningstar's taxonomy
("Consumer Cyclical", "Basic Materials", "Financial Services"). In the review
app, `--sector` filters by *real* GICS (from Wikipedia), while the grid shows
Yahoo sectors.

**F-32 (Low) Dead or unused code and config:**
- `data.batch_size` and `optional_providers.institutional_flow` (config).
- `Storage.latest_run_id`.
- `util.fmt_num` / `fmt_pct` (only tests use them).
- The review app fetches `earnings_growth` and caches it, but no column shows it.

### 4.4 Desktop app (GUI) and packaging

**F-18 (Medium) The GUI can't open without yfinance, and doesn't import lazily as claimed. Confirmed.**
- **The lazy import is defeated.** `gui.py` imports the CLI lazily "so the window
  opens instantly". But `review/__init__.py:7` eagerly does
  `from .cli import main`, which imports `fetch`, which calls
  `sys.exit("Missing dependency: yfinance")` at import (`fetch.py:18`). Every
  launch pays the pandas + yfinance import before the window appears; without
  yfinance the window never opens.
- **`--list-groups` also needs yfinance.** It's documented as "no network", but
  the same import chain runs.

**F-19 (Medium) GUI hints describe the wrong universe. Confirmed.** `gui.py:290-323`.

| Pick | Hint says | Actually fetched |
|---|---|---|
| `asx` | "ASX 200 - Australia's top 200" | every ASX listing, ~2,000 names (a long run) |
| `global` | "US + Australia + **major markets**" | S&P 1500 + ASX 200 only |
| `japan` | "Nikkei 225" | a 40-name curated list |

The website says "all of Japan". The alias map duplicates `universe.SOURCES` by
hand, which is why it drifted.

**F-20 (Medium) The window doesn't fit common laptop screens. Confirmed (screenshot).**
- **Controls fall off-screen.** `MIN_W, MIN_H = 720, 860` (`gui.py:45`), and
  `minsize` stops the window shrinking further. On a 1366×768 display, "Where it
  lands", the Run/Cancel bar and the status bar are below the screen edge.
- **The art may not show at the default size.** At 1600×1000 the default window
  showed `[window too small to show the art]` instead of the animation, because
  the CRT needs 36 rows at ≥ 8 pt. Fonts differ on Windows, so verify there,
  especially at 125–150% scaling.

**F-28 (Low) The GUI can't reach some CLI sector regions.** `gui.py:560`
hard-codes the sector "in region" list. `asx-all`, `america-smallcap`,
`australia-smallcap` and `global-smallcap` are missing. The README advertises
`--source asx-all --sector telecom`, but the GUI can't do it.

**F-29 (Low) The Terminal exe ignores the app's data folder.** By inspection.
`frozen_entry.py` routes the Terminal exe straight to `cli.main`. Its defaults
(`reports/`, `data/review_cache`) are relative to the working directory, not
`Documents\Stock Review` as the docs say. Launched from the Start-menu shortcut,
output can land in the install folder, which the uninstaller sweeps without
asking.

**F-36 (Low) Enter starts a run from anywhere in the main window**
(`gui.py:1615`), including while typing in the Custom tickers box. Combined with
`everything` or `asx-all`, that's an easy accidental 2,000-ticker run.

**F-21 (Medium) Builds aren't reproducible, and the supported-Python story is split.**
- **Three different "supported" versions.** README: 3.11. `requirements.txt`:
  3.11–3.12, and its pins don't install on 3.13. `release.yml`: builds the
  shipped exe on **3.13**.
- **The shipped build is unpinned.** `release.yml` pins only `yfinance==1.4.1`,
  with pandas/requests/bs4/lxml/pyinstaller unpinned. Two builds of the same tag
  can differ, and nothing ties the shipped exe to a tested dependency set.
- **Locally the build uses an untracked venv.** `build_exe.ps1` hard-requires a
  local `.venv313`.

**F-33 (Low) Versions are scattered.**
- **The app version lives in six places:** `APP_VERSION` (gui.py),
  `installer.iss`, README.txt, INSTALL.txt, the `docs/index.html` title and
  CHANGES.
- **The package says something else:** `stock_analyzer.__version__` is still
  `2.0.0`.
- **Nothing checks consistency:** no check that a pushed `vX.Y` tag matches
  `APP_VERSION`.

### 4.5 CI and tests

**F-05 (Medium) No CI runs most of the tests.** `release.yml:48-50` runs 3 of the
10 test files, on Windows, only when review/packaging paths change. A change to
`scoring.py`, `metrics.py`, `backtest.py`, `storage/` or `reporting.py` triggers
**no tests at all**. No lint step either.

**F-35 (Low) Test gaps.** No tests for:
- `indicators.compute_indicators` (needs `ta`).
- `derive_metric_inputs` beyond negative P/E.
- The review sector matching (`_sector_match`, `verify_loose_industry`,
  `resolve_sector_key`).
- `resolve_tickers` / `read_tickers_file`.
- The `fetch_one` success path with a fake `yf.Ticker`.
- ASCII frame invariants (width ≤ 100, `_GLOBAL_H`).
- GUI hint/alias consistency with `SOURCES` (would have caught F-19).
- Backtest with an unelapsed horizon (would have caught F-01).
- Config validation.

### 4.6 Documentation drift

**F-34 (Low) Docs that no longer match the code or the release:**
- **Report shape.** README (3×), `config.py` and `config.yaml` say the pipeline
  report is "two compact tables". It's one CSV block, as DESIGN.md correctly says.
- **Live minimal slice.** README and `run_minimal_slice.py` say the live slice
  runs "the 20 tickers in sample20.csv". `config.yaml` points at `au_heavy.csv`
  (329 names).
- **Stale README structure.** The project-structure section lists only two test
  files and says `config/` holds "sample20, custom_example".
- **Machine-specific setup.** README tells everyone the "bundled `.venv` is
  broken; use `.venv313`", which describes the author's machine, not the repo.
- **Dangling references.** CHANGES.md links `IMPLEMENTATION_PLAN.md` and
  `CODE_REVIEW.md`, and mentions `readfirst.txt`. None are in the repo.
- **Stale install steps.** INSTALL.txt step 1 says to unzip "Stock Review
  2.3.zip" containing "Stock Review Setup.exe". Releases now ship
  `Stock-Review-Setup.exe` directly, plus a portable zip of the app folder.
- **Missing-value wording.** README.txt says missing cells show "n/a"; the default
  (lean) grid shows blanks.
- **Website.** Its sample grid uses sector codes `Mat` / `ConsDisc`; the app emits
  `Matl` / `ConsCyc`. It also says "all of Japan".
- **Scheduling example.** `crontab.example` (Option A) references
  `config/config.us.yaml` / `config.asx.yaml`, which don't exist.

---

## 5. Improvements and changes I would implement

Ordered by priority. Each item lists **where**, **what** and **done when**.
Items that change scores or report output need a CHANGES.md entry saying "runs
before/after are not comparable", plus golden/test updates per AGENTS.md rule 6.

### P0 — make the numbers right (do first)

1. **Backtest: don't count unelapsed horizons** (F-01). `backtest.py`.
   - Pass an `as_of` date (the latest bar across the loaded price panel, or
     today) into `_fwd_return`.
   - If `pos + horizon` is beyond the series end *and* the series end is within a
     few sessions of `as_of`, return `(None, False)` and count it as `pending`.
   - Keep `truncated` only for series that stop well before `as_of` (real
     delistings).
   - Report the pending counts per horizon.
   - *Done when:* a test with a score date 2 sessions before the end of data
     shows that date excluded from the 1m stats, and the delisting test still
     passes.

2. **Currency-safe fundamentals** (F-02). `data_collection.py` and `metrics.py`.
   - Record `financialCurrency` in the raw record.
   - Compute `fcf_yield` / `cash_to_marketcap` only when the two currencies match.
     Otherwise convert with an FX rate: reuse `review.fetch.fetch_fx_rates`,
     moved to a shared module. If FX is unavailable, leave the value `None`,
     never guess. Handle GBp/GBX explicitly.
   - *Done when:* a TSM-like record (USD price, TWD financials) yields a sane
     FCF yield or `None`, never 31%.

3. **A unique run identity per universe** (F-03). `config.py` and `pipeline.py`.
   - Add an optional `universe.name`. Default it to `source` for network sources
     and to `csv:<file stem>` for CSV.
   - Give `run_minimal_slice.py` (live) its own name, e.g. `slice:<stem>`.
   - Fix the cron example to name each config.
   - *Done when:* two CSV universes saved on the same date both survive, and a
     test covers it.

4. **A size-independent percentile scale** (F-04). `scoring.py:_winsorized_percentile`.
   - Use mid-rank percentiles `(rank − 0.5)/n × 100`, so a group's median is 50
     at any size and the extremes are symmetric.
   - Update `test_scoring.py` expectations (they currently pin 20…100), README
     and DESIGN §3.
   - *Done when:* the median-name percentile is 50 for group sizes 5 and 100.

5. **Treat negative D/E as missing** (F-06). `metrics.py:188` → `_pos`-style,
   with the registry description updated. *Done when:* a test with D/E −150
   gives NaN, not 100.

6. **One price basis for price-relative metrics** (F-07).
   - Compute `price_vs_sma*`, `proximity_52w_high` (pipeline) and
     `vs_ma50/200` (review) against the **last close of the same adjusted
     series**.
   - Alternatively, fetch unadjusted history for MAs and the 52-week range. In
     either case, keep the displayed price as Yahoo's current price.
   - *Done when:* a fixture with a dividend inside the window gives identical
     vs-MA values whichever way it's computed.

7. **An honest `data_ok`, and a minimum coverage to be ranked** (F-10).
   - `data_ok` requires real info fields (a name, cap or price) or history.
   - Don't cache an info dict that only has `_net_upgrades_90d`.
   - Add `scoring.min_categories` (default 2) or `min_coverage`. Names below it go
     to the "Unscored (insufficient data)" line instead of being ranked.

8. **Validate the config and report the weights actually applied** (F-09). `config.py`.
   - `category_weights` keys must equal `CATEGORIES`, with non-negative values
     and a sum > 0.
   - Unknown `metric_weights` keys raise an error.
   - Unknown YAML keys give one clear error line.
   - In the report, print the *effective* blend: weights renormalised over the
     categories that had any data that day. Optionally set the default sentiment
     weight to 0 until a provider exists.

9. **Version the methodology** (F-08). Add a `SCORING_VERSION` constant and a
   registry signature (key/category/direction/sector_relative/weight) to
   `config_hash`. Bump the constant whenever derivation or scoring code changes
   meaning.

10. **Fix the dependency list** (F-14). Add `lxml` (pinned) to `requirements.txt`.
    *Done when:* a clean venv from `requirements.txt` runs `--sector energy` and
    the pipeline's `sp500` source.

### P1 — robustness and UX

11. **Scrapes retry, then fall back to the last good list** (F-15).
    `review/universe.py`.
    - Wrap `requests.get` in a short retry/backoff.
    - On failure, load the newest `_universe/<date>/<key>.json` and say so in
      stderr and in the report header ("constituents from 2026-10-08 cache").
    - Otherwise exit with a one-line message, no traceback.

12. **Share one cache-name sanitiser** (F-16). Move `_safe_cache_name` to a small
    shared module and use it in `review/fetch._cache_file`. Add a test for
    `CON.DE` and `PRN`.

13. **Stop same-day report collisions** (F-17).
    - Default to `stock_review_<date>_<group-slug>.<fmt>` (`sp500`,
      `mining-australia`, `custom-3`), and add `-2`, `-3`… if the file exists.
    - Mirror this in `gui.expected_output_path`, and have the CLI print the final
      path for the GUI to read.
    - Update `test_gui.py`.

14. **Real lazy imports** (F-18).
    - Make `review/__init__.py` expose `main` lazily.
    - Move `import yfinance` inside `fetch_one` / `_fetch_fx_one`, with a clear
      error at fetch time.
    - *Done when:* the window and `--list-groups` work without yfinance
      installed, and window start-up skips the pandas/yfinance import.

15. **Derive GUI hints from the universe definitions** (F-19, F-28).
    - Build `_SOURCE_ALIAS` by grouping `SOURCES` keys that map to the same
      callable, instead of a hand-written map.
    - Fix the `japan` / `global` / `asx` copy.
    - Build the sector "in region" list from `REGION_RECORDS`.
    - *Done when:* a test asserts every `SOURCES` key has a hint and every alias
      points at an identical fetcher.

16. **Make the window fit smaller screens** (F-20).
    - Clamp `minsize` to the screen (e.g. `min(860, screen_h − 80)`) and let the
      CRT panel absorb the squeeze. The bottom-up packing already supports this.
    - Consider dropping `_READABLE_FLOOR` to 7 pt, or trimming the HUD rows.
    - Verify on Windows at 100% and 150% scaling that the default window shows the
      art.

17. **Put the Terminal exe's output in the app's data folder** (F-29).
    - Move `_base_dir()` into a stdlib-only `review/paths.py`.
    - When frozen, default `--out` and `--cache-dir` under it for both exes.
    - Document this in PACKAGING.md.

18. **Resolve paths against the project root, and rotate logs daily** (F-12, F-13).
    - Resolve relative `sqlite:///` paths against `PROJECT_ROOT` in `load_config`.
    - Use a per-day file handler (e.g. `TimedRotatingFileHandler(when="midnight")`
      plus the error handler), or re-target the handlers in `run_once`.

19. **Small review-app correctness fixes** (F-22, F-23, F-24).
    - Use a secondary sort key `ticker` in `_sorted_ok`.
    - Make a missing currency `None`: the cap shows blank, the Curr column says so.
    - Prefer `longName` for names.

20. **Backtest robustness and reporting** (F-11, F-31).
    - Choose the price window from `min(run_date)` minus a buffer, not a fixed 2y.
    - Batch-download prices.
    - Add IC standard deviation, IC t-stat / IR, hit rate, and per-horizon
      observation counts, so "positive, stable" can be judged.

21. **Cache housekeeping** (F-30). Keep the last N days (default 7) of
    `data/cache` and `data/review_cache`, pruned at start-up, with a
    `--keep-cache-days` / config knob.

22. **Faster collection** (F-31). Bounded concurrency in `DataCollector.collect`
    (reuse the review app's batching), plus batched `yf.download` for history
    using `data.batch_size`. Otherwise delete that knob.

### P2 — engineering hygiene

23. **CI for everything** (F-05). Add `.github/workflows/tests.yml` running all
    of `tests/` on ubuntu across Python 3.11/3.12/3.13, on every push and PR.
    Install `ta` where it builds, so indicators get covered. Keep the Windows
    build workflow as is.

24. **Packaging metadata.** Add a `pyproject.toml`:
    - Project metadata and extras `[pipeline]`, `[app]`, `[dev]`.
    - Console scripts `stock-review` and `stock-analyzer-daily`.

    Then remove the `sys.path.insert` shims from scripts, tests and shims, keeping
    root `stock_review.py` for back-compat.

25. **Reproducible builds and one supported range** (F-21).
    - Commit a lock for the frozen app (`packaging/requirements-app.txt`, ideally
      with hashes) and use it in `release.yml` and `build_exe.ps1`. Drop the
      hard `.venv313` requirement in favour of any venv.
    - State one supported range everywhere (3.11–3.13). Pin a `requirements.txt`
      that installs on all of them, or split the 3.13 pins.

26. **One version source** (F-33).
    - Set `stock_analyzer.__version__` = the app version, and have
      `gui.APP_VERSION` import it.
    - Have Inno Setup read it (`#define` passed from the workflow).
    - Add a release-job step that fails if the tag ≠ `__version__`.

27. **Lint.** A minimal `ruff` config matching current style (line length,
    `BLE001` allowed where annotated), run in CI.

28. **Remove dead code or wire it up** (F-32). Delete or use: `batch_size`,
    `institutional_flow`, `latest_run_id`, `fmt_num` / `fmt_pct`. Either add an
    `EarnGr` column (golden update) or stop fetching `earnings_growth`.

29. **Drop scipy.** Compute Spearman IC as a Pearson correlation of ranks in
    pandas. scipy is used only for `spearmanr`.

30. **Generate the schema.** Add `scripts/gen_schema.py` and a test that
    regenerates `database/schema.sql` and asserts no diff.

31. **Close the test gaps** (F-35). Highest value first:
    - Backtest pending horizons.
    - GUI hint/alias consistency.
    - `derive_metric_inputs` edge cases (negative D/E, currency mismatch).
    - Review sector matching.
    - `read_tickers_file` / `resolve_tickers`.
    - `fetch_one` with a fake Ticker.
    - ASCII frame invariants.
    - Config validation.

### P3 — docs and features

32. **Docs sweep** (F-34). Fix every item in §4.6 in one commit:
    - Remove the `.venv313` notes from README.
    - Fix or delete the dangling CHANGES links.
    - Rewrite INSTALL.txt step 1 for the current release assets.
    - Align the website sample row and its "Japan" copy.
    - Either ship `config.us.yaml` / `config.asx.yaml` examples or change the cron
      text.
    - Call the sector taxonomy "Yahoo sector" where that's what it is (F-27).

33. **Review report polish** (F-25, F-26):
    - Complete the rich legend, and make `lean=True` the `build_markdown`
      default.
    - Drop the `run_daily.py` paragraph from the AI instructions.
    - Add a header line with the quote time and market-session state from
      `regularMarketTime`. A single report mixes closed and open markets, and the
      reviewing AI should know.
    - Add a `FinCurr` note when `financialCurrency` ≠ `currency`, so the AI can
      discount Yahoo's cross-currency ratios.

34. **Pipeline market caps in USD.** Reuse the FX module from P0-2 to normalise
    market cap. The report can then show size buckets for mixed-currency
    universes (it currently skips them).

35. **GUI niceties.**
    - Make Enter run only when focus isn't in a text field, or ask for
      confirmation above ~1,000 tickers (F-36).
    - Merge the duplicate "Curated" paths (the default form opens on
      Region/source = `curated`, which duplicates the first radio).
    - Put a real screenshot on the website.

---

## 6. Suggested order of work

| Step | Items | Why first |
|---|---|---|
| 1 | P0-1, P0-3, P0-10, P2-23 | Stop corrupting the evidence (backtest, DB) and get CI watching. Small diffs. |
| 2 | P0-2, P0-4, P0-5, P0-6, P0-9 | Scoring correctness. One release, one "not comparable" note. |
| 3 | P0-7, P0-8 | Ranking hygiene and honest reporting of the applied weights. |
| 4 | P1-11 … P1-16 | What app users hit: scrape failures, cache resume, overwritten reports, hints, small screens. |
| 5 | rest of P1, P2 | Robustness, reproducible builds, packaging. |
| 6 | P3 | Docs sweep and features. |

---

## Appendix — reproductions

All offline unless noted. Run from the repo root with dependencies installed.

<details><summary>F-01 backtest counts a 2-session return in the 1m horizon</summary>

```python
import sys; sys.path.insert(0, "src")
import numpy as np, pandas as pd
from stock_analyzer import backtest as bt
dates = pd.bdate_range(end="2026-10-09", periods=60)
prices = pd.Series(100 * np.power(1.001, np.arange(60)), index=dates)
print(bt._fwd_return(prices, dates[-3], 21))   # (0.0020..., True) -> counted in 1m stats
```
</details>

<details><summary>F-02 currency mismatch (live, needs network)</summary>

```python
import yfinance as yf
i = yf.Ticker("TSM").info
print(i["currency"], i["financialCurrency"], i["freeCashflow"] / i["marketCap"])  # USD TWD ~0.31
```
</details>

<details><summary>F-04 / F-06 percentile scale and negative D/E</summary>

```python
import sys; sys.path.insert(0, "src")
import numpy as np, pandas as pd
from stock_analyzer.config import ScoringConfig
from stock_analyzer.scoring import _rank_one_metric
from stock_analyzer.metrics import REGISTRY_BY_KEY as R
v = pd.Series(list(np.linspace(.05,.25,5)) + list(np.linspace(.05,.25,100)),
              index=[f"A{i}" for i in range(5)] + [f"B{i}" for i in range(100)])
s = pd.Series(["A"]*5 + ["B"]*100, index=v.index)
p = _rank_one_metric(v, s, R["roe"], ScoringConfig())
print(p[:5].mean(), p[5:].mean())             # 60.0 vs 50.5
de = pd.Series([-150., 20, 80, 150, 300], index=list("NABCD"))
print(_rank_one_metric(de, pd.Series(["X"]*5, index=de.index), R["debt_to_equity"], ScoringConfig())["N"])  # 100.0
```
</details>

<details><summary>F-12 DB lands in the working directory</summary>

```bash
mkdir -p /tmp/elsewhere && cd /tmp/elsewhere
python /path/to/repo/scripts/run_minimal_slice.py --synthetic
ls /tmp/elsewhere/database/        # stock_analyzer_synthetic.db created here; report went to repo/reports
```
</details>

<details><summary>F-15 scrape failure is a raw traceback</summary>

```python
import sys; sys.path.insert(0, "src")
import requests
requests.get = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("offline"))
from stock_analyzer.review import cli
cli.main(["--source", "sp500", "--no-cache", "--no-animation"])   # Traceback ... ConnectionError
```
</details>

<details><summary>F-16 / F-19 / F-22 review-app probes</summary>

```python
import sys; sys.path.insert(0, "src")
from pathlib import Path
from stock_analyzer.review import gui, fetch, report
print(gui.universe_hint("source", "asx"))                          # "ASX 200 ..." but fetches ~2,000
print(fetch._cache_file(Path("c"), "CON.DE", "d").name)            # CON.DE.json (reserved on Windows)
rows = [{"ticker": t, "usd_market_cap": None} for t in ("ZZZ", "AAA", "MMM")]
print([r["ticker"] for r in report._sorted_ok(rows)],
      [r["ticker"] for r in report._sorted_ok(rows[::-1])])        # order follows input order
```
</details>

<details><summary>F-18 / F-20 GUI checks</summary>

- Run `python stock_review_gui.py` in an environment without yfinance. It exits
  with "Missing dependency: yfinance" before any window appears.
- Run the window on a 1366×768 display (e.g. `Xvfb :1 -screen 0 1366x768x24`
  plus `STOCK_REVIEW_SMOKE_MS=9000`). The status bar and action buttons sit
  below the bottom edge.
</details>
