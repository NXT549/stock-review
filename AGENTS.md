# AGENTS.md — orientation for coding agents

Read this first. It is a map of the repo so you can go straight to the files a
task needs instead of re-reading all ~15k lines. Deeper "why" lives in
[DESIGN.md](DESIGN.md); user-facing docs are [README.md](README.md) (developer),
[README.txt](README.txt) / [INSTALL.txt](INSTALL.txt) (end users of the Windows
app), and [CHANGES.md](CHANGES.md) (release notes).

## What this project is

Two tools sharing one package, `src/stock_analyzer/`:

| | **Scored pipeline** | **Watchlist review ("Stock Review" app)** |
|---|---|---|
| Entry points | `scripts/run_daily.py`, `scripts/run_minimal_slice.py`, `scripts/run_backtest.py`, `scripts/init_db.py` | `stock_review.py` (CLI shim), `stock_review_gui.py` (Tk GUI), `python -m stock_analyzer.review` |
| Code | `src/stock_analyzer/*.py` + `storage/` | `src/stock_analyzer/review/` |
| Config | `config/config.yaml` + `.env` | CLI flags only (GUI builds an argv) |
| Output | `reports/ai_review_<date>.md` — 0–100 scores + percentile matrix | `reports/stock_review_<date>.{md,csv,json}` — raw sourced numbers, **no score** |
| Persistence | SQLite/Postgres via SQLAlchemy (feeds the backtest) | Per-day JSON resume cache only |

Both end in **one self-contained file meant to be handed to an AI by the user**.

## Non-negotiable rules

1. **No AI/LLM anywhere in the code.** No API keys, no SDKs, no network calls to
   model providers. Scoring is deterministic Python. `tests/test_reporting.py`
   checks reports for AI leakage.
2. **Never fabricate data.** Missing → `None` in code, blank cell (lean) or
   `n/a` (rich) in output. Never `0`, never a guess. Missing metrics are
   *excluded and weights renormalised*, not zero-filled.
3. **Determinism.** Same input → same percentiles, scores, ranks. No randomness
   or wall-clock dependence in `scoring.py` / `metrics.py` / report builders
   (the ASCII animation's RNG is decorative and seeded).
4. **Not financial advice** — the disclaimer stays in reports and docs.
5. **Failures are visible.** Failed/excluded tickers are listed in the report,
   never silently dropped. Rate limits are expected; retry/backoff/cache.
6. **Output contracts are tested.** Changing a report column/format means
   updating `tests/golden/*.csv` and the relevant test deliberately.

## Repo map

```
config/config.yaml          pipeline config (universe, data, scoring weights, reporting, storage)
config/universe/*.csv|txt   ticker lists (sample20, au_heavy = default, global ~1,000, custom_example)
scripts/                    pipeline entry points; _bootstrap.py puts src/ on sys.path
src/stock_analyzer/
  config.py                 typed dataclasses; load_config(); .env overrides (STOCK_ANALYZER_DB_URL)
  logging_setup.py          setup_logging() → stderr + logs/stock_analyzer_<date>.log + errors_<date>.log
  universe.py               pipeline universe: csv | sp500 | nasdaq | asx
  data_collection.py        DataCollector: yfinance + per-ticker daily cache, throttle, retry/backoff
  indicators.py             compute_indicators() using `ta` (NOT pandas-ta)
  metrics.py                ★ METRIC REGISTRY (category, direction, sector-relative?) + derive_metric_inputs()
  scoring.py                ★ deterministic 0–100 engine (winsorize → percentile → category → final)
  sentiment_providers.py    optional paid providers; STUB that refuses to invent numbers
  storage/models.py, db.py  SQLAlchemy schema + Storage (save_run replaces same-day run)
  pipeline.py               run_pipeline(): collect → derive → score → store → report; synthetic_records()
  reporting.py              render_ai_review(): the single condensed CSV-in-Markdown report
  backtest.py               forward returns, decile table, top-N vs universe, rank IC
  util.py                   df_to_markdown/csv, fmt helpers, config_hash()
  review/                   the watchlist tool ("Stock Review")
    cli.py                  argparse + main(argv, progress=, cancel_event=); resolve_tickers()
    universe.py             --source lists, Wikipedia/ASX scrapers, --sector GICS groups, SOURCES dict
    fetch.py                fetch_one() per ticker, _fetch_batch() concurrency + retry rounds, FX → USD
    format.py               ★ COLUMNS (ColSpec list) = single source of truth for lean & rich grids
    report.py               build_markdown(), stocks_csv(), stocks_json(); AI instructions + legends
    symbols.py              clean_symbol(): exchange suffixes, BRK.B → BRK-B, etc.
    gui.py                  Tk app (Win98 style): run(), build_argv(), settings, dialogs, menus
    gui_theme.py            Win98 palette + widget factories (button98, group98, Dropdown98, …)
    gui_anim.py             TkAnimator: renders ascii_art scenes into a Tk Text widget
    ascii_art.py            13 ~100×30 ANSI scenes + HUD; Animator for real terminals
stock_review.py             CLI shim; re-exports the whole review namespace (tests import it)
stock_review_gui.py         GUI launcher (what PyInstaller freezes)
packaging/                  PyInstaller spec, frozen_entry.py (GUI vs Terminal by exe name), Inno Setup .iss
scheduling/                 cron, Windows Task Scheduler, Docker examples for run_daily.py
database/schema.sql         generated SQL mirror of storage/models.py
tests/                      plain-script tests (+ golden/ CSVs for the review grid)
example_report/             committed synthetic example of the pipeline report
docs/                       GitHub Pages site (static; deployed by .github/workflows/pages.yml)
```

Runtime-only (gitignored): `data/cache/`, `data/review_cache/<date>/`, `logs/`,
`reports/*`, `database/*.db`, `gui_settings.json`, `build/`, `dist/`.

## Commands

```bash
pip install -r requirements.txt          # pins target Python 3.11–3.12
python scripts/run_minimal_slice.py --synthetic   # offline end-to-end pipeline check
python stock_review.py --list-groups     # no network
python stock_review.py AAPL MSFT --out reports/t.md   # needs network (yfinance)
python stock_review_gui.py               # Tk window (needs a display)
```

**Tests** are plain scripts (pytest optional, not required). Run all:

```bash
for t in tests/test_*.py; do python "$t" || echo "FAIL $t"; done
# or: python -m pytest tests -q
```

All tests are offline (network is monkeypatched). `test_gui.py` never opens a
window. On Python 3.13 the exact pins in `requirements.txt` fail to build
(`pandas==2.2.2`, `ta`); installing unpinned `yfinance scipy SQLAlchemy requests
beautifulsoup4 lxml schedule python-dotenv pandas numpy PyYAML` is enough for
the whole suite to pass. `ta` is only needed for live technical indicators.
pytest is not installed by default.

There is no linter/formatter config in the repo; match surrounding style
(type hints, `from __future__ import annotations`, docstrings that explain *why*,
`# noqa: BLE001` on deliberate broad excepts).

## Where to make common changes

| Task | Touch |
|---|---|
| Add/change a scored metric | `metrics.py` registry + `derive_metric_inputs()`; maybe `indicators.py`/`data_collection.py`; `tests/test_scoring.py` |
| Change scoring weights | `config/config.yaml` → `scoring.*` (then backtest) — not code |
| Change pipeline report | `reporting.py`; `tests/test_reporting.py` |
| Add a column to the review grid | `review/fetch.py` (populate key) → `review/format.py` `COLUMNS` → legends in `review/report.py` → regenerate `tests/golden/*.csv` |
| Add a `--source` / region | `review/universe.py` (`SOURCES`, `REGION_RECORDS`, scrapers) + `gui.py` `_SOURCE_HINTS` / quick picks |
| Add a `--sector` | `review/universe.py` `SECTORS` + `SECTOR_ALIASES` |
| New CLI flag | `review/cli.py` + `gui.py` `build_argv()` (if exposed) + `tests/test_cli.py` / `test_gui.py` |
| GUI look | `review/gui_theme.py` tokens/factories — see DESIGN.md "GUI design system" |
| New ASCII scene | `review/ascii_art.py` → append to `ANIMATIONS` (same dict shape); GUI picks it up automatically |
| DB schema | `storage/models.py` → regenerate `database/schema.sql`; `tests/test_storage.py` |
| Release | bump `APP_VERSION` in `review/gui.py`, `AppVersion` in `packaging/installer.iss`, headers of `README.txt`/`INSTALL.txt`; add a CHANGES.md section |

## Gotchas

- `stock_review.py` flattens every submodule's names into its namespace;
  tests do `import stock_review as sr` and monkeypatch `cli.*` at module level.
  Patch where the name is *looked up* (`cli._fetch_batch`, `cli.fetch_fx_rates`).
- Tk is not thread-safe: the worker thread only puts messages on a queue
  (`QueueProgress`, `_StreamToQueue`); all widget work happens in `root.after` loops.
- `cli.main` returns **130** when cancelled and still writes a partial report.
- Market caps are USD-normalised (`usd_market_cap`); prices/targets stay in
  listing currency (`GBp` = pence).
- `review` and the pipeline each have their own `universe.py` — different modules.
- Pipeline `save_run` replaces an earlier same-day run for the same universe;
  backtest also dedupes at read time. SQLite FKs are enabled via PRAGMA.
- "Unknown" sector is never a peer group — always ranks universe-wide.
- GUI data dir: source checkout → repo root; frozen → `Documents\Stock Review`.
- Env switches: `STOCK_REVIEW_SELFTEST=1` (headless GUI worker test),
  `STOCK_REVIEW_SMOKE_MS=N` (auto-close the window). Parsed by `gui.env_flag`.
- README.md references `IMPLEMENTATION_PLAN.md`, which is not in the repo.
- README.md commands use Windows paths (`.venv313\Scripts\python.exe`); on
  Linux/macOS just use `python`.

## Working agreements

- Keep changes minimal and in the existing style; don't add dependencies
  lightly (the GUI must stay stdlib-only so PyInstaller freezes cleanly).
- Run the full test suite before committing.
- User-visible behaviour changes get a CHANGES.md entry.
- Update this file and DESIGN.md when you change structure or contracts, so the
  next agent doesn't have to rediscover it.
