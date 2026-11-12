# DESIGN.md — architecture, contracts and the visual design system

The "why" behind the code. [AGENTS.md](AGENTS.md) says *where* things are; this
file says *how they fit and what must not change by accident*.

---

## 1. Principles

| Principle | What it means in code |
|---|---|
| **No AI inside** | The program produces a file; the user hands it to any AI. No model calls, keys or SDKs. Reports include written instructions *for* the reviewing AI. |
| **Honest data** | `None` for missing, never `0`/guessed. Missing metrics are excluded and weights renormalised. Failed and excluded tickers are listed in the output with the reason. |
| **Deterministic** | Pure pandas ranking; no randomness or time dependence in scoring/report math. Tie rules are fixed (see §3). |
| **Token-lean output** | The payload is CSV inside Markdown (≈half the tokens of a pipe table), one row per stock, units stated once in a legend. |
| **Survive yfinance** | yfinance is unofficial. Everything is cached per day, throttled, retried with exponential backoff + jitter, and resumable by re-running the same command. |
| **Research screen, not advice** | Disclaimers in every report. The score means nothing until the backtest says otherwise. |

---

## 2. Two pipelines

```
SCORED PIPELINE (scripts/run_daily.py → pipeline.run_pipeline)

  config.yaml ─► universe.build_universe ─► DataCollector.collect (yfinance, cache)
                                               │  raw record per ticker (flat dict, None = missing)
                                               ▼
              metrics.build_metric_frame  (ticker × metric inputs + sector series)
                                               ▼
              scoring.score_universe ─► percentiles ─► category scores ─► final 0–100 + rank
                                               ▼
              storage.Storage.save_run (SQLite/Postgres, date-stamped)
                                               ▼
              reporting.generate_reports ─► reports/ai_review_<date>.md
              backtest (later, scripts/run_backtest.py) ◄── stored score history + real prices


WATCHLIST REVIEW (stock_review.py / GUI → review.cli.main)

  argv ─► cli.resolve_tickers (positional | --file | --sector[+--source] | --source | curated)
            └─ universe.UniverseLoader (memo → day cache → concurrent scrape → stale cache)
       ─► fetch._fetch_batch (thread pool, per-day cache, retry rounds, cancel_event)
       ─► fetch.fetch_fx_rates + to_usd_market_cap
       ─► universe.verify_loose_industry (sector precision pass)
       ─► report.build_markdown | stocks_csv | stocks_json ─► reports/stock_review_<date>.<fmt>
```

The GUI (`review/gui.py`) is a thin front-end: it turns its form into an argv
(`build_argv`) and calls `cli.main(argv, progress=QueueProgress, cancel_event=…)`
on a worker thread. There is exactly one engine; the GUI and the Terminal exe
can't drift.

---

## 3. Scoring engine (`scoring.py`, `metrics.py`)

- **Registry** (`metrics.REGISTRY`, 28 metrics) is the single source of truth:
  key, category, direction (`higher_better`/`lower_better`), `sector_relative`,
  label, default weight.
  - fundamentals (sector-relative): revenue/earnings growth, P/E, fwd P/E, PEG,
    gross/operating/net margin, ROE, ROA, D/E, FCF yield, cash/mkt-cap
  - technicals: price vs SMA50/200, trend alignment, RSI health, MACD hist
  - momentum: 1/3/6m return, proximity to 52w high, relative volume
  - analyst: recommendation mean (lower better), target upside, net upgrades 90d
  - sentiment: news/social (stubbed → normally unavailable)
- **Per metric:** winsorize to `[winsor_lower, winsor_upper]` → percentile rank
  0–100 (`method='average'`), flipped for lower-better. Non-positive P/E = missing.
- **Sector logic:** sector-relative metrics rank within GICS sector if the sector
  has ≥ `min_group_size` non-null members, else universe-wide. `"Unknown"`/NaN
  sector **always** ranks universe-wide (never a peer group).
- **Drop rule:** a metric with < `min_rankable` non-null values universe-wide is
  dropped for the day (listed in the report).
- **Aggregation:** category = weighted mean of available metric percentiles;
  final = weighted sum of available categories with weights renormalised.
- **Rank:** final rank uses `method='min'` (1, 1, 3).
- Risk stats (beta, vol, drawdown, liquidity) are collected and stored but **not**
  in the composite.
- `util.config_hash(cfg.scoring)` is stored per run so backtests can tell
  methodologies apart.

## 4. Storage & backtest

Tables (`storage/models.py`): `runs`, `securities`, `raw_snapshots`,
`metric_values`, `metric_scores`, `category_scores`, `scores`, `reports`.
`save_run` replaces an earlier same-day run for the same universe (explicit
child deletes; SQLite FKs enabled by PRAGMA). Backtest keeps only the latest run
per (date, ticker), computes forward returns over 5 and 21 sessions, and reports
decile means, top-N vs universe, and mean Spearman rank IC. Delisted series
contribute a realised `truncated` return; unpriceable tickers are counted, not
hidden.

---

## 5. Output contracts

### Pipeline report — `reports/ai_review_<date>.md` (`reporting.py`)
Header line (`universe=… | scored=n/N | generated=…` + overview), disclaimer,
instructions for the reviewing AI, **one CSV block**:
`rank, ticker, sector, score, <category scores>, cov, <per-metric percentiles>`,
a column legend, sparse-row note, dropped metrics, backtest status; optional
raw-value deep dive for top `deep_dive_n`. Guarded by `tests/test_reporting.py`.

### Review report — `reports/stock_review_<date>.<md|csv|json>` (`review/`)
- `format.COLUMNS` (list of `ColSpec`) is the **only** column definition; each
  has a lean header/formatter and a rich one (`None` = absent in that format).
- **Lean (default):** blank = missing; bare numbers (`16.6` = +16.6%); short
  sector/analyst codes (`SECTOR_CODE`, `ANALYST_CODE`); `MktCapB` in USD
  billions; rows sorted mega → micro. `_compact_columns` drops columns that lose
  nothing (single currency, all-blank, derivable Target) and `_compaction_note`
  says so in the file.
- **Rich (`--rich`):** `n/a`, per-cell units, spelled-out values, Size column.
- `--no-names` drops Name. CSV text cells starting with `= + - @` are
  apostrophe-prefixed (formula-injection safety).
- Returns windows are shown only with enough price history.
- Failures and sector-precision exclusions are listed with reasons; a cancelled
  run writes a **partial report** with a banner and exits 130.
- Golden files `tests/golden/{lean,rich}_stocks.csv` pin the exact grid.

### Universe lists (`review/universe.py`)
- **Definitions are data.** `INDEXES` (one page each), `UNIVERSES` (named
  `--source`s built from index parts, with aliases) and `SECTORS` are tables;
  `SOURCES` and `REGION_RECORDS` are generated from them and their key sets are
  a contract with the CLI and both GUIs (pinned in `tests/test_universe.py`).
- **One record model.** Every page is parsed once into `IndexRecord`s (symbol +
  normalised GICS labels + local market cap or `None`). Plain ticker lists and
  sector groups read the same records, so a page is downloaded at most once a
  day whichever is asked for.
- **Loading order:** in-run memo → `<cache>/_universe/<date>/<index>.json` →
  live scrape (all missing pages concurrently, ≤ 4 at a time, one HTTP session,
  3 attempts on 429/5xx/timeouts) → the newest copy from the last 7 days, used
  with a warning and never re-saved as fresh. Day folders older than that are
  pruned. `--no-cache` disables all of it.
- **Failure rules.** A table must have its symbol column and a minimum row
  count or the scrape fails (never a short list). A missing sector or
  market-cap column still yields tickers, but a `--sector` or small-cap request
  on it stops with the reason. In a multi-page universe every failed page is
  named and the pages that loaded are cached before the error is raised.

---

## 6. GUI design system (`review/gui_theme.py`)

**Concept:** a genuine-feeling **Windows 98** program "with the colour turned
up", whose hero is an MS-DOS Prompt panel playing ASCII-art scenes. Classic `tk`
widgets (not `ttk`) because they honour `relief`/`bd`/`bg`. Stdlib only.

### Tokens

| Token | Value | Use |
|---|---|---|
| `FACE` | `#c0c0c0` | window, buttons, groups (silver) |
| `HILIGHT` / `LIGHT` | `#ffffff` / `#dfdfdf` | bevel highlights |
| `SHADOW` / `DARKEDGE` | `#808080` / `#000000` | bevel shadows |
| `NAVY` → `TITLE_BLUE` | `#000080` → `#1084d0` | gradient title bars, selection |
| `FIELD` / `INK` | `#ffffff` / `#000000` | sunken inputs / text |
| `GRAYTEXT` | `#4d4d4d` | hints, disabled (4.65:1 on FACE) |
| `STATUS_RED` | `#981f1f` | status/caption warnings |
| `TIP_BG` | `#ffffe1` | tooltips |
| `CRT_BG` / `CRT_FG` | `#000000` / `#d6dde6` | DOS panel |
| `BTN_GREEN` | `#098828` | Run |
| `BTN_RED` | `#c62828` | Cancel |
| `BTN_BLUE` | `#1668c8` | Open report |
| `BTN_TEAL` | `#108282` | Open folder / Browse |
| `BTN_PURPLE` | `#7a1fa2` | Copy path / Tip of the Day |
| `GOLD` | `#b8860b` | lightbulb accent (decoration only, fails text contrast) |
| `STATUS_AMBER` | `#664200` | "done but partial" status text (4.9:1 on FACE) |
| `TITLE_PURPLE` / `TITLE_GREEN` | `#7a1fa2` / `#075c22` | group-box titles |

Fonts: `MS Sans Serif 9` (UI; bold only for primary actions and group titles),
`MS Sans Serif 10 bold` (dialog headings), `Consolas` (CRT; size rescales so the
100×30 scene fits the panel).

**Accessibility rule:** every text/background pair must clear **WCAG AA 4.5:1**.
Several tokens were darkened for this; comments in `gui_theme.py` record the
ratios. Check contrast before introducing a colour.

### Components (factories — always use these, never raw widgets)
`TitleBar` (gradient caption), `button98(fill=…)` (raised bevel, coloured face
brightens on hover; **disabled → flat silver face + `GRAYTEXT` label** so it can't
look clickable), `group98` (groove box + coloured bold title), `label98`,
`entry98` / `field98` (sunken white), `check98`, `radio98`, `option98` /
`Dropdown98` (combobox popup clamped to the window, scrolls, flips upward),
`tooltip98` (pale yellow), `menu98` (silver + navy highlight), `dialog98`
(silver dialog with gradient caption), `center_over`.

### Layout (top → bottom)
App title bar → **MS-DOS Prompt panel** (always animating; double-click its title
bar to change channel; can be hidden) → two group boxes side by side
(**What to review** navy / **Options** green) with a grey plain-English
universe hint → **Where it lands** (purple) with live output-path preview →
action bar (▶ Run review · ■ Cancel · Open report · Open folder · Copy path) →
collapsible details log → sunken status bar (message · run timer · clock).
Window sizing: `MIN_H` 600; the default and any remembered size are clamped to the
screen (height − 80), and the DOS panel is the part that shrinks. Options is packed
first so it keeps its natural width. Status messages carry a glyph — `(ok)` `(!)`
`(x)` — so state never relies on colour alone.
Menus: File (Recent reports), Quick picks, Extras (Crystal Ball Ctrl+8, Tip of the
Day), Help (About with odometer). Splash on launch. Keys: F5/Enter run, Esc
cancel, Ctrl+O open report, Ctrl+Shift+O open folder, Ctrl+L copy path.

### Behaviour rules
- Worker thread never touches Tk; it posts to a queue drained by `root.after`.
- Everything cosmetic is wrapped so it can never crash a run.
- Form state, window size, recent reports and odometer persist in
  `gui_settings.json` (atomic write; bad file → defaults).
- Live validation tints numeric fields pink rather than erroring at Run.

### ASCII animation (`ascii_art.py`, `gui_anim.py`)
13 scenes (`ANIMATIONS`): Candles, To the moon, The bull, The whale, Money
printer, Trading desk, Bear nap, Diamond hands, Disco break, The exchange, After
hours, The vault, The harbour. Each is a dict `{title, color, frames, captions, accents}` on a
~100×30 canvas. The terminal `Animator` renders with ANSI; `TkAnimator` reuses
the same frames with Tk colour tags. HUD: progress bar, rate, ETA, ticker tape
(retries marked with yellow `»`), sparkline, caption line.

---

### Live board (`review/gui_board*.py`) — the main window ("the ledger")

What `Stock Review.exe` opens. Table-first, dark and light, stdlib Tk only. It
is a *presentation* layer: it builds the same argv (`gui.build_argv`), runs the
same worker (`gui._run_review`) and never changes report contents.

| Module | Holds |
|---|---|
| `gui_board.py` | pure model + helpers (`BoardModel`, `filter_rows`, `sort_rows`, `finish_summary`, `failure_line`, …) and `BoardApp`, the window |
| `gui_board_theme.py` | `Theme` tokens (`DARK`, `LIGHT`), `contrast()`, `heat()`, `detect_system_theme()`, font choice. No Tk |
| `gui_board_table.py` | pure geometry + `BoardTable` (virtualised canvas grid), `Mosaic` and `ScenePane` (the loading scene's canvas) |
| `gui_board_fx.py` | motion, all pure: `ease`, `bar_cells`, `Pops`, `Milestones`, `scene_frame`, `liftoff`, `RunFx`. No Tk |

**Layout (one frame that does not jump).** Header (brand, tape of latest
1-month returns, Log / theme / Options) → sidebar (what to review, recent
reports) → main pane: command header (title, plain-English hint, output summary
and path, **Run** beside it; Run swaps to Cancel in place) → per-source input →
**status band** → filter chips + find box → table → optional log → footer (key
hints, status). Options are a drawer over the right edge, not a modal, and save
as they change; a bad Limit/Workers value tints its field and disables Run with
the reason on the meta line.

**The status band** is one fixed slot with three states of identical height
(idle: what a run produces + latest report; running: headline, mood, clock,
progress bar with the real percentage beside it; done: result, failed tickers
by name, Copy for AI / Open report / Open folder / Retry). When the window is wide enough its right side holds the
**mosaic**: one square per ticker, laid out for the whole run so it fills in
rather than reflows.

**Tokens** (`gui_board_theme.py`; the website copies them):

| Role | Dark | Light |
|---|---|---|
| `bg` / `surface` / `raised` | `#0e1114` / `#151a1f` / `#1b2128` | `#f1f3f1` / `#ffffff` / `#f6f8f6` |
| `ink` / `muted` | `#e6eaef` / `#9ca7b4` | `#161b1e` / `#566067` |
| `accent` (teal of the app icon, tuned) | `#35b8c6` | `#0b7480` |
| `ok` / `warn` / `bad` | `#6cc777` / `#e2ad52` / `#f2858e` | `#1c7a3e` / `#855600` / `#b3261e` |

Fonts: Segoe UI (UI), Cascadia Mono / Consolas (tickers, numbers, ASCII), with
fallbacks. Pixel sizes are written for 96 dpi and multiplied by Tk's scaling
(`BoardApp.px`), and the process is DPI-aware on Windows.

Rules:

- **One accent** for actions, focus and selection. Status colours are used
  **only** for status and for the sign of a return, and always beside a word or
  a shape (dot = fetched, ring = retrying, cross = failed, pip = queued; a
  failed square in the mosaic is hollow), so nothing depends on hue alone. No
  gradients, glow, emoji or confetti.
- **Contrast is tested.** Every text/background pair the window draws is listed
  in `TEXT_PAIRS` and must clear WCAG AA 4.5:1 in both themes (3:1 for
  `INDICATOR_PAIRS`); `tests/test_gui_board.py` walks the lists. Add a pair
  when you introduce a combination. `faint` is decoration only, never text.
- **Every number on screen is real**, decoration included: rows come from the
  run's own events (name, sector, price in listing currency, 1-month return). Unknown → blank,
  never 0; a missing value draws no bar, gets a neutral grey square, and sorts
  last in both directions. Market cap is not shown because it is not
  USD-normalised until after the fetch. No score; nothing reads as advice.
- Counts are cumulative across the whole run (`BoardModel`); a retry round's
  smaller batch never shrinks the total, and failures between rounds read as
  "retrying" (in the counts and in the Retrying filter). No ETA is shown while
  the run is deliberately waiting. Failed tickers are named on the band and
  listed first in the table (project rule 5).
- The view is rebuilt from state (`BoardApp.build`), which is how a theme
  switch works: nothing a run produced may live only in a widget. Theme
  switching is disabled during a run. Border-only containers go through
  `BoardApp.panel` (Tk paints a frame's border black when a child has focus).
- **A run is fun to watch** (`gui_board_fx.py`; pure and tested, the view only
  draws what it returns). Still ASCII and tokens only: no emoji, gradients or
  images. What moves, and the limits it keeps:
  - *Loading scene.* While a looked-up list has produced no row yet (and only
    after 0.3 s, so a cached list never flashes one), a scene from
    `ascii_art.ANIMATIONS` plays on a `ScenePane` **placed** over the chips
    and the table, at the largest font that fits (the caption goes first; below
    7 px it is not shown at all). It is destroyed when the first ticker lands.
    Lists known offline (starter, typed, file) keep showing their queued rows.
    Only 8 of the Classic window's 13 scenes are used (`SCENE_QUIPS`): the
    others draw invented prices, claim to score, or carry a market view, and
    the captions are written for this window for the same reason. Line art is
    `muted`, the scene's accent characters `accent_text`.
  - *The bar* is segmented: fetched share solid `accent`, failed share hollow
    `warn` (a shape, not only a hue). The fill eases toward the real fraction
    and **never passes it** (`ease`); the percentage is always the real one.
    While the size of the run is unknown a short block sweeps instead. The
    `~=>` rocket rides the leading edge.
  - *Pops.* The latest fetched tickers show beside the rocket for about a
    second with their own 1-month return (no return known: no figure). At most
    three, at most one new one per 0.3 s; the rest are in the table and tape.
    A pop stays at full contrast and then leaves, it does not fade. A just
    landed mosaic square is outlined in `ink` for a moment.
  - *Quips* at 25 / 50 / 75 % replace the mood line for 3 s, and only while
    the run is calm (`BoardModel.calm`): a retry, a slow ticker or a failure
    always gets the line.
  - *Lift-off.* A clean run (every ticker fetched, not cancelled) ends with
    the rocket leaving and ASCII fireworks for under 2 s, on a canvas placed
    over the lower rows of the already-built done band, so nothing changes
    size and the shortcuts work throughout; any key or click ends it. A
    partial, cancelled or failed run gets none.
  - **One switch** (Options, "Animations while a review runs"; default follows
    Windows' "Animation effects" via `detect_reduced_motion`) turns all of it
    off: no scene, no fast loop, the bar steps.
  - The fast loop (`BoardApp.fx_tick`, 66 ms) runs only while something moves.
    No randomness: every frame is a function of its index. Decoration can
    never break a run: a failure in it is caught, kept in `BoardApp.fx_error`
    (the window test asserts it stays `None`) and the run carries on still.
- `BoardProgress.wants_rec = True` makes `fetch._fetch_batch` hand it each
  ticker's record; sinks without the flag keep the old `update()` signature.
- Tk 8.6 (the frozen build) and Tk 9 both work; avoid APIs only one has.

## 7. Packaging

PyInstaller one-folder bundle with three exes from one `frozen_entry.py`,
chosen by exe name: `Stock Review.exe` (the Live board), `Stock Review
Classic.exe` (the Win98 window) and `Stock Review Terminal.exe` (CLI). Inno Setup builds a per-user `Stock Review Setup.exe` (no admin). Frozen
data dir: first writable `Documents\Stock Review`. See `packaging/PACKAGING.md`.

## 8. Website (`docs/`)

GitHub Pages landing page: static `docs/index.html`, no build step, deployed by
`.github/workflows/pages.yml` on pushes to `main` that touch `docs/`. It copies
the Live board tokens (dark by default, light via `prefers-color-scheme` or the
Theme button) so the site looks like the app it ships, and its hero is an
HTML/CSS illustration of the window mid-run, captioned as example data. Only
the Classic edition card keeps the Win98 look. Keep it dependency-free (system
fonts, inline CSS, one small inline script for the theme button); motion is
off under `prefers-reduced-motion`. The download links must keep pointing at
`releases/latest/download/Stock-Review-Setup.exe` / `-Portable.zip`.
