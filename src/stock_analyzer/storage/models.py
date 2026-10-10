"""SQLAlchemy ORM models == the database schema.

Everything is date-stamped and tied to a `run` so the backtest has history to
work with. Raw collected data is stored verbatim (with its nulls) AND in a
normalised per-metric form, so you can audit exactly what fed each score.

Tables
  runs            one row per pipeline execution (date, universe, config hash)
  securities      slowly-changing dimension of tickers (name/sector/...)
  raw_snapshots   full raw collected dict per ticker per run (JSON, nulls kept)
  metric_values   normalised raw scoring inputs (raw_value + is_available)
  metric_scores   0-100 percentile per metric per ticker per run
  category_scores per-category 0-100 score + coverage count
  scores          final 0-100 composite + rank
  reports         generated report artifacts (path + format)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String,
    Index, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, index=True)
    universe_name: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    n_tickers: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="started")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    scores: Mapped[list["Score"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    # One run per (date, universe): same-day reruns REPLACE (see Storage.save_run).
    # SQLite won't retro-alter existing DBs — those are repaired at read time by
    # the backtest's latest-run_id dedup instead.
    __table_args__ = (
        UniqueConstraint("run_date", "universe_name", name="uq_run_date_universe"),
    )


class Security(Base):
    __tablename__ = "securities"
    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_seen: Mapped[date] = mapped_column(Date, default=date.today)
    last_seen: Mapped[date] = mapped_column(Date, default=date.today)


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict] = mapped_column(JSON)  # raw collected dict, nulls preserved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class MetricValue(Base):
    __tablename__ = "metric_values"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(24), index=True)
    metric_key: Mapped[str] = mapped_column(String(48))
    raw_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_metricvalue_run_ticker", "run_id", "ticker"),)


class MetricScore(Base):
    __tablename__ = "metric_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(24), index=True)
    metric_key: Mapped[str] = mapped_column(String(48))
    percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_metricscore_run_ticker", "run_id", "ticker"),)


class CategoryScore(Base):
    __tablename__ = "category_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(24), index=True)
    category: Mapped[str] = mapped_column(String(32))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (Index("ix_categoryscore_run_ticker", "run_id", "ticker"),)


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    run_date: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str] = mapped_column(String(24), index=True)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    run: Mapped["Run"] = relationship(back_populates="scores")
    __table_args__ = (
        UniqueConstraint("run_id", "ticker", name="uq_score_run_ticker"),
        Index("ix_score_date_ticker", "run_date", "ticker"),
    )


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    fmt: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
