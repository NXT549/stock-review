"""Offline tests for the storage layer (no network; temp-file SQLite DBs).

Run:  python tests/test_storage.py
"""
from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from stock_analyzer.storage.db import Storage, _safe_url
from stock_analyzer.storage.models import (
    CategoryScore, MetricScore, MetricValue, RawSnapshot, Report, Run, Score,
    Security,
)

CHILD_MODELS = (RawSnapshot, MetricValue, MetricScore, CategoryScore, Score, Report)


@contextmanager
def _tmp_storage():
    """Fresh temp-file SQLite Storage; engine disposed before cleanup
    (Windows can't delete a file the pooled connection still holds open)."""
    with tempfile.TemporaryDirectory() as tmp:
        st = Storage(f"sqlite:///{Path(tmp) / 't.db'}")
        st.init_db()
        try:
            yield st
        finally:
            st.engine.dispose()


def _fake_result(tickers, scores):
    """Duck-typed ScoringResult: just the four frames save_run reads."""
    idx = list(tickers)
    final = pd.DataFrame({"final_score": scores}, index=idx)
    final["rank"] = final["final_score"].rank(ascending=False, method="min").astype("Int64")
    return SimpleNamespace(
        metric_percentiles=pd.DataFrame({"pe": [50.0] * len(idx)}, index=idx),
        category_scores=pd.DataFrame({"fundamentals": scores}, index=idx),
        category_coverage=pd.DataFrame({"fundamentals": [1] * len(idx)}, index=idx),
        final=final,
    )


def _save(st, run_date, universe, cfg_hash, recs_or_scores):
    """save_run with minimal records built from {ticker: score}."""
    recs = [{"ticker": tk, "name": f"{tk} Inc", "sector": "Tech"}
            for tk in recs_or_scores]
    mf = pd.DataFrame({"pe": [10.0] * len(recs_or_scores)},
                      index=list(recs_or_scores))
    result = _fake_result(list(recs_or_scores), list(recs_or_scores.values()))
    return st.save_run(run_date, universe, cfg_hash, recs, mf, result)


def test_safe_url_masks_password():
    rendered = _safe_url("postgresql://user:secret@h/db")
    assert "secret" not in rendered, rendered
    assert "user" in rendered and "h/db" in rendered, rendered
    print(f"OK password masked: {rendered}")


def test_safe_url_passthrough_no_password():
    # URLs without credentials come through unchanged.
    assert _safe_url("sqlite:///database/stock_analyzer.db") == \
        "sqlite:///database/stock_analyzer.db"
    assert _safe_url("postgresql://user@h/db") == "postgresql://user@h/db"
    print("OK credential-free URLs unchanged")


def test_same_day_rerun_replaces_run():
    """A same-day rerun replaces the earlier run — zero orphans in ALL six
    child tables (the SQLite-cascade caveat this fix exists for)."""
    with _tmp_storage() as st:
        d = date(2026, 7, 15)
        id1 = _save(st, d, "uni", "hash1", {"AAA": 80.0, "BBB": 60.0})
        st.save_reports(id1, {"md": "reports/one.md"})
        # a second universe keeps the rowid sequence moving, so the rerun below
        # gets a genuinely new id (bare SQLite reuses a deleted max rowid) —
        # and doubles as the must-NOT-be-replaced case
        id_other = _save(st, d, "other", "hashO", {"CCC": 50.0})
        id2 = _save(st, d, "uni", "hash2", {"AAA": 70.0, "BBB": 90.0})
        assert id2 not in (id1, id_other), (id1, id_other, id2)
        with st.Session() as s:
            assert set(s.scalars(select(Run.id)).all()) == {id_other, id2}
            for model in CHILD_MODELS:
                orphans = s.scalars(
                    select(model.run_id).where(model.run_id == id1)).all()
                assert not orphans, (model.__tablename__, orphans)
        hist = st.load_score_history()
        assert set(hist["run_id"]) == {id_other, id2}
        assert float(hist.loc[hist["ticker"] == "BBB", "final_score"].iloc[0]) == 90.0
    print("OK same-day rerun replaces (no orphans in any child table); "
          "other universes untouched")


def test_run_uniqueness_enforced_on_new_dbs():
    with _tmp_storage() as st:
        d = date(2026, 7, 15)
        with st.Session() as s:
            s.add(Run(run_date=d, universe_name="u", config_hash="x"))
            s.commit()
            s.add(Run(run_date=d, universe_name="u", config_hash="y"))
            try:
                s.commit()
                raise AssertionError("expected IntegrityError on duplicate run")
            except IntegrityError:
                s.rollback()
    print("OK (run_date, universe_name) unique constraint enforced")


def test_sqlite_foreign_keys_enforced():
    """The connect-event pragma makes SQLite actually enforce FKs."""
    with _tmp_storage() as st:
        with st.Session() as s:
            s.add(Score(run_id=99999, run_date=date(2026, 7, 15), ticker="ZZZ"))
            try:
                s.commit()
                raise AssertionError("expected IntegrityError on bad run_id FK")
            except IntegrityError:
                s.rollback()
    print("OK PRAGMA foreign_keys=ON enforced per connection")


def test_upsert_securities_semantics():
    """Dialect upsert keeps stored values when incoming is NULL, updates when
    non-NULL, preserves first_seen and bumps last_seen."""
    with _tmp_storage() as st:
        d1, d2 = date(2026, 7, 14), date(2026, 7, 15)
        with st.Session() as s:
            st._upsert_securities(s, [{"ticker": "AAA", "name": "Aaa Inc",
                                       "sector": "Tech"}], d1)
            s.commit()
        with st.Session() as s:
            # name missing (must survive), sector updated, exchange newly set
            st._upsert_securities(s, [{"ticker": "AAA", "sector": "Fintech",
                                       "exchange": "NYSE"}], d2)
            s.commit()
        with st.Session() as s:
            sec = s.get(Security, "AAA")
            assert sec.name == "Aaa Inc", sec.name
            assert sec.sector == "Fintech" and sec.exchange == "NYSE"
            assert sec.first_seen == d1 and sec.last_seen == d2
    print("OK securities upsert: coalesce semantics + first/last_seen")


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL STORAGE TESTS PASSED")


if __name__ == "__main__":
    _run_all()
