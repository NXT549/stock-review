"""End-to-end pipeline: collect -> derive -> score -> store -> report.

The whole pipeline is deterministic: same data in -> same scores and ranks out.
There is NO AI/LLM step. The final stage writes a single clean Markdown file
listing every stock with its 0-100 score and per-metric breakdown, ready to feed
to an AI of your choice for qualitative review.

`synthetic_records()` provides a fully offline, deterministic fixture (clearly
fake tickers SYNTH01..) so the whole pipeline (minus live data) can be exercised
and verified without the network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from .backtest import run_backtest, BacktestResult
from .config import Config
from .data_collection import DataCollector
from .logging_setup import get_logger
from .metrics import build_metric_frame
from .reporting import ReportContext, build_stock_views, generate_reports
from .scoring import score_universe, rankings_table, ScoringResult
from .storage.db import Storage
from .universe import build_universe
from .util import config_hash

log = get_logger("pipeline")


@dataclass
class PipelineResult:
    run_date: date
    universe_name: str
    run_id: Optional[int]
    rankings: pd.DataFrame
    result: ScoringResult
    views: list[dict]
    report_paths: dict[str, str] = field(default_factory=dict)
    n_tickers: int = 0


def run_pipeline(cfg: Config, *, records: list[dict] | None = None,
                 run_date: date | None = None, synthetic: bool = False,
                 with_reports: bool = True,
                 db_url_override: str | None = None,
                 backtest_summary: str | None = None) -> PipelineResult:
    run_date = run_date or date.today()

    # 1. DATA -------------------------------------------------------------
    if records is None:
        tickers = build_universe(cfg)
        records = DataCollector(cfg).collect(tickers)
        universe_name = cfg.universe.source
    else:
        universe_name = "SYNTHETIC" if synthetic else "custom"
    records = [r for r in records if r.get("ticker")]
    by_ticker = {r["ticker"]: r for r in records}
    log.info("scoring %d tickers (universe=%s, date=%s)", len(records), universe_name, run_date)

    # 2. DERIVE + SCORE (deterministic) -----------------------------------
    metric_frame, sector = build_metric_frame(records)
    result = score_universe(metric_frame, sector, cfg.scoring)
    rankings = rankings_table(result)

    # 3. STORE ------------------------------------------------------------
    run_id = None
    try:
        storage = Storage(db_url_override or cfg.storage.db_url)
        storage.init_db()
        run_id = storage.save_run(run_date, universe_name, config_hash(cfg.scoring),
                                  records, metric_frame, result)
    except Exception as exc:  # noqa: BLE001 - never let storage kill the report
        log.error("storage failed (continuing without persistence): %s", exc)
        storage = None

    # 4. VIEWS for every ranked stock (feed the condensed percentile matrix) ---
    views = build_stock_views(by_ticker, metric_frame, result, limit=None)

    # 5. REPORT (single AI-ready Markdown file) ---------------------------
    report_paths: dict[str, str] = {}
    if with_reports:
        ctx = ReportContext(
            run_date=run_date, universe_name=universe_name, rankings=rankings,
            views=views, dropped_metrics=result.dropped_metrics,
            backtest_summary=backtest_summary, synthetic=synthetic,
        )
        report_paths = generate_reports(ctx, cfg)
        if storage and run_id is not None:
            try:
                storage.save_reports(run_id, report_paths)
            except Exception as exc:  # noqa: BLE001
                log.error("could not persist report paths: %s", exc)

    log.info("pipeline complete: run_id=%s, reports=%s", run_id, list(report_paths))
    return PipelineResult(run_date=run_date, universe_name=universe_name, run_id=run_id,
                          rankings=rankings, result=result, views=views,
                          report_paths=report_paths, n_tickers=len(records))


def run_backtest_from_db(cfg: Config) -> BacktestResult:
    """Load score history from the DB and run the backtest (pulls real prices)."""
    storage = Storage(cfg.storage.db_url)
    storage.init_db()
    history = storage.load_score_history()
    return run_backtest(history, cfg)


# ---------------------------------------------------------------------------
# Deterministic SYNTHETIC fixture (clearly fake) for the offline slice.
# Tickers SYNTH01.. with obviously synthetic names. Built from a compact spec;
# derived price/MA fields are computed so technical metrics vary by `trend`.
# A few records intentionally omit fields (to exercise re-weighting) and one has
# a deliberate data anomaly (negative margins despite strong price action) for a
# downstream AI reviewer to catch in the generated file.
# ---------------------------------------------------------------------------
def synthetic_records() -> list[dict]:
    def mk(ticker, sector, trend=0.5, drop=(), **over):
        base = dict(
            ticker=ticker, name=f"Synthetic {ticker}", sector=sector,
            industry=f"{sector} (synthetic)", exchange="SYN", currency="USD",
            price=100.0, market_cap=5.0e10,
            revenue_growth=0.08, earnings_growth=0.10,
            trailing_pe=20.0, forward_pe=18.0, peg=2.0,
            gross_margin=0.42, operating_margin=0.20, net_margin=0.12,
            roe=0.18, roa=0.08, debt_to_equity=80.0,
            free_cash_flow=2.0e9, total_cash=1.0e10,
            eps=5.0, forward_eps=5.5,
            rsi14=55.0, macd_hist=0.5,
            ret_1m=0.02, ret_3m=0.06, ret_6m=0.12,
            recommendation_mean=2.2, target_mean_price=115.0, num_analysts=20,
            net_upgrades_90d=1.0,
            beta=1.1, week52_high=120.0, week52_low=80.0,
            volume=2.0e7, avg_volume_50d=1.8e7,
            volatility_annual=0.28, max_drawdown_1y=-0.22,
            data_ok=True, collected_at="synthetic",
        )
        base.update(over)
        price = base["price"]
        # Derive MAs from `trend`: trend>0 => price above its averages (uptrend).
        for key, g in [("sma20", 0.01), ("sma50", 0.03), ("sma100", 0.06), ("sma200", 0.10)]:
            base.setdefault(key, round(price * (1 - g * trend), 2))
        base.setdefault("dollar_liquidity", base["avg_volume_50d"] * price)
        for k in drop:
            base.pop(k, None)
        return base

    return [
        # --- Technology ---
        mk("SYNTH01", "Technology", trend=1.0, price=180, revenue_growth=0.28,
           earnings_growth=0.35, gross_margin=0.70, operating_margin=0.38,
           net_margin=0.30, roe=0.40, trailing_pe=32, forward_pe=26, peg=1.1,
           ret_3m=0.18, ret_6m=0.40, recommendation_mean=1.7, target_mean_price=215,
           net_upgrades_90d=4),
        mk("SYNTH02", "Technology", trend=0.8, price=95, revenue_growth=0.18,
           earnings_growth=0.22, gross_margin=0.62, net_margin=0.20, roe=0.28,
           trailing_pe=24, forward_pe=20, peg=1.4, ret_6m=0.25),
        mk("SYNTH03", "Technology", trend=-0.4, price=40, revenue_growth=0.05,
           earnings_growth=-0.10, gross_margin=0.55, net_margin=0.05, roe=0.10,
           trailing_pe=45, forward_pe=30, peg=3.5, rsi14=38, macd_hist=-0.6,
           ret_1m=-0.06, ret_3m=-0.12, ret_6m=-0.20, recommendation_mean=3.1,
           target_mean_price=42),
        mk("SYNTH04", "Technology", trend=0.6, price=120, revenue_growth=0.12,
           net_margin=0.16, roe=0.22, trailing_pe=22, forward_pe=19, drop=("peg",)),
        mk("SYNTH05", "Technology", trend=0.2, price=60, revenue_growth=0.09,
           earnings_growth=0.08, net_margin=0.10, roe=0.15, trailing_pe=28),

        # --- Financials ---
        mk("SYNTH06", "Financial Services", trend=0.7, price=150, revenue_growth=0.10,
           earnings_growth=0.14, gross_margin=float("nan"), operating_margin=0.45,
           net_margin=0.30, roe=0.16, roa=0.015, debt_to_equity=190, trailing_pe=12,
           forward_pe=11, peg=1.6, recommendation_mean=2.0, target_mean_price=175),
        mk("SYNTH07", "Financial Services", trend=0.3, price=45, revenue_growth=0.06,
           earnings_growth=0.07, net_margin=0.26, roe=0.12, roa=0.011,
           debt_to_equity=210, trailing_pe=10, forward_pe=9.5, drop=("peg", "free_cash_flow")),
        mk("SYNTH08", "Financial Services", trend=-0.2, price=30, revenue_growth=0.03,
           earnings_growth=-0.05, net_margin=0.18, roe=0.08, roa=0.007,
           debt_to_equity=240, trailing_pe=8, forward_pe=9, ret_3m=-0.05),

        # --- Energy ---
        mk("SYNTH09", "Energy", trend=0.5, price=110, revenue_growth=-0.04,
           earnings_growth=-0.15, gross_margin=0.30, operating_margin=0.18,
           net_margin=0.11, roe=0.20, debt_to_equity=45, trailing_pe=9,
           forward_pe=10, peg=float("nan"), free_cash_flow=6.0e9,
           recommendation_mean=2.6, target_mean_price=118, ret_6m=0.05),
        mk("SYNTH10", "Energy", trend=-0.6, price=55, revenue_growth=-0.08,
           earnings_growth=-0.25, net_margin=0.08, roe=0.12, debt_to_equity=60,
           trailing_pe=7, forward_pe=8, rsi14=33, macd_hist=-0.9, ret_1m=-0.09,
           ret_3m=-0.16, ret_6m=-0.28),

        # --- Healthcare ---
        mk("SYNTH11", "Healthcare", trend=0.6, price=140, revenue_growth=0.11,
           earnings_growth=0.13, gross_margin=0.66, net_margin=0.22, roe=0.24,
           trailing_pe=21, forward_pe=18, peg=1.7, recommendation_mean=1.9,
           target_mean_price=165),
        mk("SYNTH12", "Healthcare", trend=0.1, price=75, revenue_growth=0.04,
           earnings_growth=0.02, net_margin=0.14, roe=0.16, trailing_pe=17,
           forward_pe=16, drop=("target_mean_price", "recommendation_mean", "num_analysts")),
        # Deliberate ANOMALY: negative earnings & margin but strong price action.
        mk("SYNTH13", "Healthcare", trend=0.9, price=210, revenue_growth=0.40,
           earnings_growth=-0.60, net_margin=-0.18, operating_margin=-0.10,
           roe=-0.25, roa=-0.08, trailing_pe=float("nan"), forward_pe=120, peg=float("nan"),
           free_cash_flow=-8.0e8, ret_1m=0.15, ret_3m=0.55, ret_6m=1.20,
           rsi14=82, macd_hist=2.4, recommendation_mean=2.4, target_mean_price=240,
           net_upgrades_90d=3),

        # --- Consumer ---
        mk("SYNTH14", "Consumer Cyclical", trend=0.7, price=88, revenue_growth=0.15,
           earnings_growth=0.18, gross_margin=0.48, net_margin=0.13, roe=0.26,
           trailing_pe=23, forward_pe=19, peg=1.5, ret_3m=0.12, ret_6m=0.22),
        mk("SYNTH15", "Consumer Cyclical", trend=-0.3, price=33, revenue_growth=0.02,
           earnings_growth=-0.08, net_margin=0.06, roe=0.09, trailing_pe=26,
           forward_pe=22, rsi14=41, ret_3m=-0.07),
        mk("SYNTH16", "Consumer Defensive", trend=0.4, price=125, revenue_growth=0.05,
           earnings_growth=0.06, gross_margin=0.38, net_margin=0.11, roe=0.30,
           debt_to_equity=110, trailing_pe=24, forward_pe=22, peg=3.0,
           recommendation_mean=2.8, target_mean_price=128),
        mk("SYNTH17", "Consumer Defensive", trend=0.2, price=70, revenue_growth=0.03,
           earnings_growth=0.04, net_margin=0.09, roe=0.22, debt_to_equity=130,
           trailing_pe=22, forward_pe=21),

        # --- Industrials ---
        mk("SYNTH18", "Industrials", trend=0.6, price=160, revenue_growth=0.09,
           earnings_growth=0.12, gross_margin=0.35, operating_margin=0.17,
           net_margin=0.10, roe=0.19, debt_to_equity=95, trailing_pe=19, forward_pe=17,
           peg=1.8, ret_6m=0.16),
        mk("SYNTH19", "Industrials", trend=0.0, price=52, revenue_growth=0.04,
           earnings_growth=0.03, net_margin=0.08, roe=0.13, trailing_pe=18,
           forward_pe=17, ret_3m=0.0),
        mk("SYNTH20", "Industrials", trend=-0.5, price=28, revenue_growth=-0.02,
           earnings_growth=-0.12, net_margin=0.05, roe=0.07, debt_to_equity=140,
           trailing_pe=15, forward_pe=16, rsi14=36, macd_hist=-0.5, ret_3m=-0.10,
           ret_6m=-0.18),
    ]
