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

### Live board (`review/gui_board.py`) — second window, opt-in

Dark, table-first, stdlib Tk/ttk. It is a *presentation* layer only: it builds
the same argv (`gui.build_argv`), runs the same worker (`gui._run_review`) and
never changes report contents. Rules:

- One accent (muted teal) plus status colours used **only** for status (green
  fetched, amber retrying, red failed). No gradients, glow, emoji or confetti.
- Every number on screen is real: table rows come from the run's own events and
  the tape shows fetched tickers' 1-month return. Unknown -> omitted, never 0.
  There is no score and nothing reads as a recommendation.
- Counts are cumulative across the whole run (`BoardModel`); a retry round's
  smaller batch never shrinks the total, and failures between rounds read as
  "retrying". Failures end up named on the result card (project rule 5).
- The fun is small and contained: the ASCII rocket strip, one plain mood
  sentence, a pulse dot and a `>` marker on the progress bar.
- `BoardProgress.wants_rec = True` makes `fetch._fetch_batch` hand it each
  ticker's record; sinks without the flag keep the old `update()` signature.

## 7. Packaging

PyInstaller one-folder bundle with two exes from one `frozen_entry.py`:
`Stock Review.exe` (GUI) and `Stock Review Terminal.exe` (CLI; chosen by exe
name). Inno Setup builds a per-user `Stock Review Setup.exe` (no admin). Frozen
data dir: first writable `Documents\Stock Review`. See `packaging/PACKAGING.md`.

## 8. Website (`docs/`)

GitHub Pages landing page: static `docs/index.html`, no build step, deployed by
`.github/workflows/pages.yml` on pushes to `main` that touch `docs/`. It reuses
the Win98 tokens above so the site looks like the app. Keep it dependency-free.
