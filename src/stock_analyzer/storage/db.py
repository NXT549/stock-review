"""Database helpers: engine creation, schema init, persistence, history reads."""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import create_engine, delete, event, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from ..logging_setup import get_logger
from .models import (
    Base, CategoryScore, MetricScore, MetricValue, RawSnapshot,
    Report, Run, Score, Security,
)

if TYPE_CHECKING:
    from ..scoring import ScoringResult

log = get_logger("db")


def _safe_url(db_url: str) -> str:
    """DB URL with the password masked — the only form that may reach logs/console."""
    return make_url(db_url).render_as_string(hide_password=True)


def _f(x) -> float | None:
    """NaN/inf/None -> None; else float. Keeps NULLs honest in the DB."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _i(x) -> int | None:
    v = _f(x)
    return int(v) if v is not None else None


class Storage:
    def __init__(self, db_url: str):
        self.db_url = db_url
        if db_url.startswith("sqlite"):
            # Ensure the sqlite directory exists (sqlite won't create dirs).
            path = db_url.split("sqlite:///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.engine: Engine = create_engine(
                db_url, connect_args={"check_same_thread": False}
            )
            # SQLite ignores the schema's ondelete=CASCADE unless foreign-key
            # enforcement is switched on per-connection.
            event.listen(self.engine, "connect",
                         lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        else:
            self.engine = create_engine(db_url, pool_pre_ping=True)
        self.Session = sessionmaker(bind=self.engine)

    def init_db(self) -> None:
        Base.metadata.create_all(self.engine)
        log.info("schema ready at %s", _safe_url(self.db_url))

    # --------------------------- write a full run ---------------------------
    def save_run(self, run_date: date, universe_name: str, config_hash: str,
                 records: list[dict], metric_frame: pd.DataFrame,
                 result: "ScoringResult") -> int:
        """Persist one run's raw data, metric values, scores and ranks. Returns run_id.

        Idempotent per (run_date, universe_name): a same-day rerun REPLACES the
        earlier run and all its child rows instead of stacking a duplicate that
        would double-count every ticker in the backtest.
        """
        with self.Session() as s:
            self._replace_same_day_runs(s, run_date, universe_name)
            run = Run(run_date=run_date, universe_name=universe_name,
                      config_hash=config_hash, n_tickers=len(records),
                      status="scored")
            s.add(run)
            s.flush()  # get run.id
            run_id = run.id

            self._upsert_securities(s, records, run_date)
            self._bulk_raw(s, run_id, records)
            self._bulk_metric_values(s, run_id, metric_frame)
            self._bulk_metric_scores(s, run_id, result.metric_percentiles)
            self._bulk_category_scores(s, run_id, result.category_scores, result.category_coverage)
            self._bulk_scores(s, run_id, run_date, result.final)

            s.commit()
            log.info("saved run %d (%s, %d tickers)", run_id, run_date, len(records))
            return run_id

    def _replace_same_day_runs(self, s: Session, run_date: date, universe_name: str):
        """Delete earlier runs for this (run_date, universe) plus ALL child rows.

        Children are deleted explicitly by run_id: only Run.scores has an ORM
        cascade, and the schema's DB-level ondelete=CASCADE fires on SQLite
        only when PRAGMA foreign_keys is on — explicit deletes keep the replace
        orphan-free on any engine or pre-existing connection.
        """
        stale = s.scalars(select(Run.id).where(
            Run.run_date == run_date, Run.universe_name == universe_name)).all()
        if not stale:
            return
        log.info("replacing %d earlier run(s) for %s / %s",
                 len(stale), run_date, universe_name)
        for model in (RawSnapshot, MetricValue, MetricScore,
                      CategoryScore, Score, Report):
            s.execute(delete(model).where(model.run_id.in_(stale)))
        s.execute(delete(Run).where(Run.id.in_(stale)))

    def _upsert_securities(self, s: Session, records: list[dict], run_date: date):
        """One dialect-level upsert statement (was: select-all-then-insert).

        COALESCE keeps the stored value when the incoming one is NULL — the
        same keep-if-missing semantics as the old ORM loop — and first_seen
        survives conflicts untouched.
        """
        by_ticker = {}  # dedup within the batch (upserts reject dup keys); last wins
        for r in records:
            by_ticker[r["ticker"]] = {
                "ticker": r["ticker"], "name": r.get("name"),
                "sector": r.get("sector"), "industry": r.get("industry"),
                "exchange": r.get("exchange"), "currency": r.get("currency"),
                "first_seen": run_date, "last_seen": run_date,
            }
        rows = list(by_ticker.values())
        if not rows:
            return
        insert_fn = {"sqlite": sqlite_insert, "postgresql": pg_insert}.get(
            s.get_bind().dialect.name)
        if insert_fn is None:  # exotic dialect: keep the portable ORM path
            self._upsert_securities_orm(s, records, run_date)
            return
        sec = Security.__table__
        stmt = insert_fn(sec).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[sec.c.ticker],
            set_={
                **{col: func.coalesce(getattr(stmt.excluded, col), sec.c[col])
                   for col in ("name", "sector", "industry", "exchange", "currency")},
                "last_seen": stmt.excluded.last_seen,
            },
        )
        s.execute(stmt)

    def _upsert_securities_orm(self, s: Session, records: list[dict], run_date: date):
        existing = {sec.ticker: sec for sec in s.scalars(select(Security)).all()}
        for r in records:
            tk = r["ticker"]
            sec = existing.get(tk)
            if sec is None:
                s.add(Security(
                    ticker=tk, name=r.get("name"), sector=r.get("sector"),
                    industry=r.get("industry"), exchange=r.get("exchange"),
                    currency=r.get("currency"), first_seen=run_date, last_seen=run_date,
                ))
            else:
                sec.name = r.get("name") or sec.name
                sec.sector = r.get("sector") or sec.sector
                sec.industry = r.get("industry") or sec.industry
                sec.exchange = r.get("exchange") or sec.exchange
                sec.currency = r.get("currency") or sec.currency
                sec.last_seen = run_date

    def _bulk_raw(self, s: Session, run_id: int, records: list[dict]):
        rows = [{"run_id": run_id, "ticker": r["ticker"], "payload": _json_safe(r)}
                for r in records]
        if rows:
            s.execute(insert(RawSnapshot), rows)

    def _bulk_metric_values(self, s: Session, run_id: int, metric_frame: pd.DataFrame):
        rows = []
        for ticker, row in metric_frame.iterrows():
            for key, val in row.items():
                v = _f(val)
                rows.append({"run_id": run_id, "ticker": ticker, "metric_key": key,
                             "raw_value": v, "is_available": v is not None})
        if rows:
            s.execute(insert(MetricValue), rows)

    def _bulk_metric_scores(self, s: Session, run_id: int, pct: pd.DataFrame):
        rows = []
        for ticker, row in pct.iterrows():
            for key, val in row.items():
                rows.append({"run_id": run_id, "ticker": ticker, "metric_key": key,
                             "percentile": _f(val)})
        if rows:
            s.execute(insert(MetricScore), rows)

    def _bulk_category_scores(self, s: Session, run_id: int,
                              cat: pd.DataFrame, cov: pd.DataFrame):
        rows = []
        for ticker, row in cat.iterrows():
            for category, val in row.items():
                rows.append({"run_id": run_id, "ticker": ticker, "category": category,
                             "score": _f(val),
                             "coverage": int(cov.loc[ticker, category]) if ticker in cov.index else 0})
        if rows:
            s.execute(insert(CategoryScore), rows)

    def _bulk_scores(self, s: Session, run_id: int, run_date: date, final: pd.DataFrame):
        rows = []
        for ticker, row in final.iterrows():
            rows.append({"run_id": run_id, "run_date": run_date, "ticker": ticker,
                         "final_score": _f(row.get("final_score")),
                         "rank": _i(row.get("rank"))})
        if rows:
            s.execute(insert(Score), rows)

    # ------------------------------ reports ---------------------------------
    def save_reports(self, run_id: int, paths: dict[str, str]):
        if not paths:
            return
        with self.Session() as s:
            for fmt, path in paths.items():
                s.add(Report(run_id=run_id, fmt=fmt, path=str(path)))
            s.commit()

    # ------------------------------ reads -----------------------------------
    def load_score_history(self) -> pd.DataFrame:
        """All historical final scores: columns run_date, ticker, final_score, rank, run_id."""
        sql = select(Score.run_date, Score.ticker, Score.final_score, Score.rank, Score.run_id)
        df = pd.read_sql(sql, self.engine)
        if not df.empty:
            df["run_date"] = pd.to_datetime(df["run_date"])
        return df

    def latest_run_id(self) -> int | None:
        with self.Session() as s:
            return s.scalar(select(Run.id).order_by(Run.id.desc()).limit(1))


def _json_safe(d: dict) -> dict:
    """Make a record dict JSON/SQLite-JSON safe (NaN -> None)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and not math.isfinite(v):
            out[k] = None
        else:
            out[k] = v
    return out
