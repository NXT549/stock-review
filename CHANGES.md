# Changes — Unreleased: GUI layout, contrast and focus fixes

From the UI review (visual design, usability, accessibility). GUI only — no
scoring, fetch or report changes.

- **The window now fits laptop screens.** Minimum height 860 → 600, the default
  and any remembered size are clamped to the screen, and the Details log no
  longer grows the window off-screen. The ASCII panel is what shrinks, so Run
  and the status bar stay visible at 1366×768.
- **Options no longer clips on the right** (Rich / Drop company names / Format).
- **Disabled buttons look disabled**: flat silver face with a grey label
  instead of a dimmed coloured one (contrast was 1.8–3.3:1).
- **A run with failed tickers no longer says "All wrapped up".** The failure
  count now also comes from the CLI's closing "N still failed" line, so retry
  rounds can't hide it. Partial/cancelled text is dark amber (was 1.8:1) and
  status messages carry a `(ok)` / `(!)` / `(x)` glyph.
- Focus rings: every entry, button and the log keep a visible 2px ring; the
  log is reachable with Tab and click. Ctrl+A in entry fields selects all.
  7pt captions are now 8pt.

# Changes — 2.3: review.md fixes, correctness + design (2026-07-16)

The full [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) sweep against the
external review — code correctness (Phases 0–2) and GUI/report design
(Phases 3–6). Behavior-visible changes are listed here; pure internal fixes
live in the git history (`git log`).

- **Backtest: same-day reruns no longer double-count, and dying tickers no
  longer vanish (CODE-04/05). Backtest numbers change.**
  Re-running the pipeline on the same day used to stack a second full set of
  scores; every duplicated ticker-date then counted twice in deciles, top-N
  and IC. `save_run` now REPLACES the earlier same-day run for the same
  universe (child rows deleted explicitly — SQLite ignores the schema's
  cascades unless `PRAGMA foreign_keys=ON`, which Storage now sets), new DBs
  get a `(run_date, universe_name)` unique constraint, and the backtest
  additionally keeps only the latest run per (date, ticker) at read time,
  which repairs already-polluted DBs with no migration. Separately, a price
  series that ends mid-horizon (delisting) now contributes its realized
  return to the last real trade — flagged `truncated`, never fabricated —
  instead of silently dropping out (survivorship bias), and tickers with no
  loadable prices at all are now reported as a per-date dropped count in the
  summary and report instead of vanishing. Backtests before and after this
  fix are not comparable.
- **Scoring: "Unknown" sector is no longer a peer group (CODE-02).**
  Sector-relative metrics used to rank tickers with a missing sector against
  each other once ≥ `min_group_size` of them shared the "Unknown" label — a
  mediocre stock could score 100 for merely leading the unknowns. Unknown
  tickers now always rank universe-wide, and tickers whose sector is literally
  NaN (previously left with no percentile at all) get the universe percentile
  too. **Scores and ranks change** for any universe with ≥ 5 Unknown-sector
  tickers; runs before and after this fix are not comparable there.


# Changes — 2.2: more fun, more friendly (2026-07-11)

A features release in the same Win98 spirit: more toys, more polish, and a
friendlier form. (`AppVersion` bumped to 2.2 in the app and the installer.)

**New fun**
- **Splash screen** — every good 90s program made you wait a moment: a brief
  CRT-style splash (mini chart, green title, version) before the window opens.
  Click it to skip. Skipped entirely in the headless smoke test.
- **Crystal Ball** (Extras menu, or **Ctrl+8**) — a crystal-ball fortune teller
  with 18 rotating market fortunes ("Hold. Also, hydrate."). Entertainment
  only; not financial advice.
- **Four new ASCII channels** (13 total): **The exchange** — a golden stepped
  pediment, clock, colonnade and brass doors under a *live scrolling quote
  marquee* (green up-moves, red down-moves), with flags, birds and traders
  shuffling in; **After hours** — the city at night, a skyline of flickering
  lit windows, a cratered moon, a blinking rooftop beacon, a shooting star and
  late traffic; **The vault** — a giant riveted vault door with a spinning
  handwheel, racks of glinting gold bars, a sweeping red security beam and a
  patrolling guard mouse; **The harbour** — a gantry crane lowering one more
  container onto the S.S. DIVIDEND, smoke drifting, radar sweeping, swell
  rolling through. The idle HUD counts the channels dynamically.
- **Odometer** — the About box now shows lifetime runs and tickers reviewed,
  persisted across launches like a proper 90s odometer.
- **More of everything** — 9 new Tips of the Day (covering the new features)
  and 4 new completion banners.

**New friendly**
- **Quick picks menu** — one click fills the whole form: ASX 200, S&P 500,
  US tech, Aussie miners, global small caps, a 10-ticker quick taste, or the
  curated shortlist. Presets that no longer exist in a build hide themselves.
- **Run summary** — the finish banner now says what happened: "Done.
  (198 ok, 2 failed, 3:42)" — and cancelled runs
  report the same counts for their partial report.
- **Sized to your screen** — on first launch the window now fits itself to the
  display (the CRT font scales with it, so the art starts big); your own size
  is still remembered and wins from then on.
- **Tooltips everywhere** — the classic hover-yellow Win98 tooltip on nearly
  every control: what a source is, what Workers does, what csv/json are for,
  what Cancel leaves behind.
- **Universe decoder** — a grey plain-English hint under "What to review"
  translates whatever is selected ("a200" → "ASX 200 - Australia's top 200";
  "sp1500" warns it's a long run). Covers every mode, not just sources.
- **Recent reports** — File > Recent reports lists your last five written
  reports (partial ones included) for one-click reopening, persisted across
  launches, with a Clear list entry.
- **Live validation** — Limit and Workers turn pink the instant they hold
  anything that isn't a positive whole number, instead of complaining at Run.


# Changes — 2.1.1: testing-feedback fixes (2026-07-11)

Four fixes straight from hands-on testing notes (`readfirst.txt`).

- **Dropdowns now stay inside the window.** The region/sector/format pickers
  were native `tk.OptionMenu` menus, and a native menu ignores the window it
  belongs to — on a small window the 40-entry source list sprawled across the
  screen. They're now Win98-combobox popups (sunken white list, navy selection)
  that are measured and clamped to the app window: they scroll when the list is
  taller than the space and flip to open upward when there's more room above.
- **The ASCII art now fills the screen when maximised.** The MS-DOS panel was
  stuck at 8pt Consolas, so fullscreen left the scene huddled in a corner. The
  font now rescales to the largest size where the whole 100×30 scene + HUD fits
  the panel, centred both ways, and scales back down when the window shrinks.
  Debounced on resize; purely cosmetic, never raises.
- **Cancelling a run now writes a partial report.** Cancel used to abort before
  writing anything — a 20-minute run cancelled at 90% left an empty file. The
  run now stops fetching promptly, then writes everything already fetched to
  the chosen output (markdown gets a `> **Partial report:** cancelled after N
  of M tickers` banner; csv/json stay clean data). Results that had already
  finished when Cancel landed are harvested instead of dropped. The GUI's
  Open report / Open folder / Copy path buttons light up for partial reports,
  and only "cancelled before anything was fetched" writes nothing.
- **Re-attempts are now visible in the app.** Per-ticker backoff retries used
  to happen invisibly inside the fetch. Each one now emits an event: the
  details log gets `[retry] BHP.AX: waiting 8s, then attempt 2/3`, the HUD
  ticker tape marks the symbol with a yellow `»`, the progress line grows a
  yellow `retries N` counter, and the between-round messages (`retry round
  2/4: 12 ticker(s) still missing - pausing 10s`) now show on the animation's
  caption line, not just the log. Plain CLI runs (no animation) print the same
  retry lines.


# Changes — GUI 2.1: full Win98 treatment + QoL (2026-07-10)

The windowed app (`stock_review_gui.py` → `stock_analyzer.review.gui`) now reads
as a complete little Windows 98 program, and picked up the most-asked-for
quality-of-life features. The CLI is untouched.

**New features**
- **"Where it lands" output-folder picker** — choose any folder for reports
  (wired to the CLI's existing `--out`); a live grey line under it shows exactly
  where the next report will be written. Blank = the app's own `reports\` folder.
  `Open folder` now opens the *actual* report's folder, wherever it landed.
- **Settings persistence** — the whole form, the output folder, the window
  size/position, and the Extras toggles are remembered across launches in
  `gui_settings.json` (next to the reports; delete it to reset). Corrupt/missing
  files fall back to defaults silently.
- **Menu bar** (File / Extras / Help) styled silver-and-navy like the rest.
- **Keyboard shortcuts** — F5/Enter run, Esc cancel, Ctrl+O open report,
  Ctrl+L copy its path, Ctrl+D toggle the details log.
- **Copy path button** — puts the finished report's full path on the clipboard.
- **Completion sounds** — the classic system ding on success / chord on error
  (Extras menu toggle, Windows-only, off never crashes anything).
- **Status bar upgrades** — tray-style clock, an elapsed-run timer cell, and the
  window title shows live progress (`Stock Review — 42% (210/503)`).
- **Tip of the Day** — the classic Win98 startup dialog (lightbulb and all),
  rotating one tip per launch; toggleable at startup or from Help.
- **About dialog** — mini ASCII chart on a CRT chip + version/paths.
- **Easter egg** — double-click the MS-DOS title bar to flip ASCII channels.

**Follow-up polish (same day)**
- **New icon**: a Windows 98-style stock chart — four chunky black-outlined
  bars climbing in the classic flag colours (red/blue/yellow/green) with a bold
  trend arrow, on the iconic desktop teal — `packaging/make_icon.py` redrawn,
  still zero-dependency. Shows in the title bar, taskbar, and both exes.
  (First cut was a waving Win98 flag; redrawn as a chart to say "stocks" too.)
- **Credits**: "Created by NXT" in the About dialog, `by NXT` in the app title
  strip, `AppPublisher=NXT` in the installer, and a sign-off in INSTALL.txt.
- **Code cleanup**: module-level `APP_AUTHOR` / `MIN_W`/`MIN_H` / `DONE_MSGS`
  constants (the min-size numbers were duplicated in two places); one `_anim()`
  guard helper replaces five scattered try/except blocks around the animator;
  `_title_progress` extracted from `_drain`; `_s` renamed `_remembered`; dead
  locals and lambda wrappers dropped.

**Clean uninstall (2026-07-10, later)**
- The uninstaller now removes **everything**: `[UninstallDelete]` sweeps the
  install dir (incl. files the app created there after install), and a new
  `[Code]` section deletes `Documents\Stock Review` (reports/cache/settings) —
  mirroring the GUI's Documents-candidate resolution (Known Folder / OneDrive /
  USERPROFILE) so it finds the data wherever the app put it.
- Interactive uninstalls ask first ("Also delete your saved reports...?");
  silent uninstalls (`/SILENT`) delete without asking.
- Verified live: silent install → planted reports/cache/settings → silent
  uninstall → app dir, data dir and Start-menu group all gone; then the machine
  was restored (app reinstalled, real data put back from backup).
- INSTALL.txt / PACKAGING.md updated (they used to promise reports survive).

**Thanks-for-installing + "just for fun" framing (2026-07-10, later still)**
- The installer's finish page now says a proper thank-you ("Yay - you made
  it!...") via custom `[Messages]`.
- New one-time **welcome dialog** on the app's very first launch (mini chart,
  "Thanks for installing Stock Review!", the vibe-coded-for-fun blurb, a green
  "Let's go!" button). Tracked via `welcomed` in `gui_settings.json`; the Tip
  of the Day takes over from the second launch onward.
- The About dialog and INSTALL.txt now both say what this is: "a silly lil
  vibe-coded project, made for fun. Not financial advice."

**Fixes / plumbing**
- The main column now packs **bottom-up**, so a too-short window squeezes the
  DOS screen instead of clipping the Run buttons or the status bar; showing the
  details log grows the window to make room instead of stealing it.
- Run/Cancel are guarded against double-fire (shortcuts and menu items could
  previously bypass the disabled buttons).
- Restored window geometry is validated against the layout's minimum first.
- New `tests/test_gui.py` (5 tests): `build_argv` modes/validation/`--out`
  derivation, `expected_output_path` mirroring, settings round-trip. Runs like
  the others: `.venv313/Scripts/python.exe tests/test_gui.py`.

---

# Changes — `stock_review` refactor (2026-07-06)

Implementation of the [CODE_REVIEW.md](CODE_REVIEW.md) recommendations. Work was done
in phases, each verified against a test suite in the working `.venv313`
(the committed `.venv` is broken — see project memory).

**Headline:** the standalone 1,543-line `stock_review.py` is now a **thin shim** over a
tested, split package at `src/stock_analyzer/review/`. The default (lean) report is
more compact and more honest; the human-readable (`--rich`) report is byte-for-byte
unchanged. Everything the tool used to do, invoked the same way, still works:

```
py stock_review.py --source japan
py stock_review.py AAPL MSFT NVDA
python -m stock_analyzer.review --list-groups   # (needs src/ on PYTHONPATH)
```

---

## What changed, by phase

### Phase 0 — Test safety net (was: no tests at all)
- Added `tests/test_stock_review.py` (**23 tests**) covering the pure, honesty-critical
  helpers (symbol normalisation, dividend-yield branches, size buckets, every formatter)
  plus **golden-file tests** on the report grid.
- Added `tests/golden/lean_stocks.csv` and `tests/golden/rich_stocks.csv` — the exact CSV
  grids, so any future header/row drift is caught instantly.
- Run with: `.venv313/Scripts/python.exe tests/test_stock_review.py`.

### Phase 1 — One column spec (kills the header/row-drift risk)
- Replaced the **four hand-aligned column lists** (lean header, lean row, rich header,
  rich row) with a single `ColSpec` table + `_active_columns()`. The header row and the
  data cells are now generated from the same source and cannot drift out of sync.
- Output byte-identical (verified by the golden tests).

### Phase 2 — Compact the lean output (no data loss)
All applied to the **lean** format only; **rich stays verbose/stable**. Every dropped
column is announced in a self-documenting note above the grid, so nothing is silent.
- **2a** Drop the `Curr` column when the whole set is one currency (stated once instead).
- **2b** Drop any column blank for every row (a blank already means "unavailable").
- **2c** Drop the derivable `Target` (reconstruct as `Price × (1 + Upside/100)`), keeping
  the currency-neutral `Upside`. Only dropped when it's genuinely reconstructable.
- **2d** Trim trailing zeros on lean prices (`3306.00` → `3306`, `12.50` → `12.5`), which
  removes the wasted `.00` on high-nominal markets (JPY/KRW) with zero loss of value.

### Phase 3 — Correctness / honesty
- `dividend_yield_fraction` no longer **scale-guesses** the unreliable raw `dividendYield`
  field; with no unambiguous source it returns `None` (blank). A handful of names now show
  a blank `Div` instead of a possibly-100×-wrong number — the intended honesty tradeoff.
- The previously-swallowed price-history exception now logs at **DEBUG** (module logger
  `stock_review`), so a systemic yfinance API-shape change is visible on demand without
  breaking a normal run.

### Phase 4 — Fold into the package + split into modules
- Moved the code from the standalone `stock_review.py` into `src/stock_analyzer/review/`,
  split into six focused modules:

  | Module | Concern |
  |---|---|
  | `symbols.py` | ticker normalisation (`clean_symbol`, `_intl_symbol`) |
  | `format.py` | formatters + `ColSpec`/`COLUMNS` + compaction |
  | `fetch.py` | yfinance fetch, resume cache, FX, concurrent batching |
  | `universe.py` | `--source`/`--sector` universes + index scrapers |
  | `report.py` | `build_markdown` + the CSV/JSON builders + prose |
  | `cli.py` | argparse + orchestration |

- `ascii_art.py` moved into the package too.
- Root `stock_review.py` is now a **thin shim** (39 lines) that re-exports the flat
  namespace, so `import stock_review` and the CLI keep working unchanged.
- The `review` subpackage is intentionally **light-deps** (yfinance/pandas/requests/bs4/lxml
  only) and does not import the package's PyYAML/SQLAlchemy parts, so it still runs in
  `.venv313`.
- Definitions were extracted with their **exact source** (via `ast`) — byte-identical to
  the tested original.

### Phase 5 — Performance / rate-limiting
- **Parallel FX** (`fetch_fx_rates`): the 6–7 currency rates fetch concurrently instead of
  serially; GBp/GBX still dedup to one GBP fetch; unpriceable currencies still reported.
- **Cached constituent scrapes** (`@_disk_cached` in `universe.py`): the Wikipedia/ASX
  constituent lists are cached to disk per calendar day (same dir as the ticker cache,
  honouring `--cache-dir`/`--no-cache`), so a rerun skips the network round-trips.

### Phase 6 — Hygiene
- Added `data/review_cache/` to `.gitignore`.
- Added `--format {md,csv,json}`: `md` (default, unchanged); `csv` (raw pipeable grid, no
  prose); `json` (sorted records + failures). The raw CSV is exactly the report's fenced
  grid (shared `_grid_csv`).

---

## Deliberate deferrals (not oversights)

- **`fast_info` (review P1 #3) — skipped.** This tool always renders the full fundamental
  grid (PE, ROE, margins, analyst targets, sector …), none of which `fast_info` provides —
  only `.info` does. Adding `fast_info` on top of the still-required `.info` would mean two
  calls per ticker and make throttling **worse**, not better. It would only pay off in a
  price-only mode this tool doesn't have.
- **Merging the two formatter families into `util.py` (review P2 #5 / §4).** The review
  formatters are now folded into the package, but a hard merge with `stock_analyzer/util.py`'s
  `fmt_num`/`fmt_pct` was deferred: their semantics genuinely differ (`$`/magnitude, blank vs
  `n/a`, sign options), and the package's own tests can't be run in `.venv313` (missing
  PyYAML), so a `util.py` change couldn't be verified against `reporting.py` here.

---

## Verification

- **23 unit + golden tests pass** in `.venv313`.
- Live runs confirmed end-to-end: 2-ticker fetch → report; `--format csv`/`--format json`;
  and the constituent cache write-then-serve (503 sp500 symbols served from disk on rerun).
- The `--rich` report and both golden CSV grids are byte-identical across the entire
  refactor.
