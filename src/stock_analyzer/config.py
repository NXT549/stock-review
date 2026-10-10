"""Configuration loading.

Reads config/config.yaml plus environment variables (.env) and exposes typed
dataclasses. Environment variables override the YAML where noted, so secrets
never live in the YAML file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    # python-dotenv is optional at import time; if absent we just rely on the
    # ambient environment.
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv missing
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


# Repository root = two levels up from this file (src/stock_analyzer/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


@dataclass
class UniverseConfig:
    source: str = "csv"
    csv_path: str = "config/universe/sample20.csv"
    limit: int | None = None


@dataclass
class MarketsConfig:
    enabled: list[str] = field(default_factory=lambda: ["US"])
    close_times: dict[str, str] = field(default_factory=dict)


@dataclass
class DataConfig:
    cache_dir: str = "data/cache"
    cache_ttl_hours: float = 20.0
    batch_size: int = 50
    history_period: str = "1y"
    request_pause_seconds: float = 1.0
    max_retries: int = 4
    backoff_base_seconds: float = 2.0
    yfinance_min_version: str = "0.2.40"


@dataclass
class ScoringConfig:
    winsor_lower: float = 0.01
    winsor_upper: float = 0.99
    min_group_size: int = 5
    min_rankable: int = 3
    category_weights: dict[str, float] = field(
        default_factory=lambda: {
            "fundamentals": 0.35,
            "technicals": 0.30,
            "momentum": 0.15,
            "analyst": 0.10,
            "sentiment": 0.10,
        }
    )
    metric_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class ReportingConfig:
    output_dir: str = "reports"
    # The AI-review file is CONDENSED by default: two compact tables (a scores
    # table + a per-metric percentile matrix) covering EVERY scored stock at one
    # row each, so even a large universe stays small enough to feed to an AI.
    # deep_dive_n optionally appends a verbose raw-value breakdown for the top N
    # names (0 = none, fully condensed).
    deep_dive_n: int = 0
    # Used by the backtest harness (top-N vs universe forward-return comparison).
    top_n: int = 5


@dataclass
class StorageConfig:
    db_url: str = "sqlite:///database/stock_analyzer.db"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "logs"


@dataclass
class Config:
    universe: UniverseConfig
    markets: MarketsConfig
    data: DataConfig
    scoring: ScoringConfig
    reporting: ReportingConfig
    storage: StorageConfig
    logging: LoggingConfig
    optional_providers: dict[str, Any]
    project_root: Path = PROJECT_ROOT
    raw: dict[str, Any] = field(default_factory=dict)

    def resolve(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else (self.project_root / p)


def _section(raw: dict, key: str) -> dict:
    val = raw.get(key) or {}
    if not isinstance(val, dict):
        raise ValueError(f"Config section '{key}' must be a mapping, got {type(val)}")
    return val


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load YAML config + .env and return a typed Config.

    Environment overrides:
      STOCK_ANALYZER_DB_URL    -> storage.db_url (only if non-empty)
    """
    load_dotenv(PROJECT_ROOT / ".env")

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    storage = StorageConfig(**_section(raw, "storage"))
    db_env = os.getenv("STOCK_ANALYZER_DB_URL", "").strip()
    if db_env:
        storage.db_url = db_env

    return Config(
        universe=UniverseConfig(**_section(raw, "universe")),
        markets=MarketsConfig(**_section(raw, "markets")),
        data=DataConfig(**_section(raw, "data")),
        scoring=ScoringConfig(**_section(raw, "scoring")),
        reporting=ReportingConfig(**_section(raw, "reporting")),
        storage=storage,
        logging=LoggingConfig(**_section(raw, "logging")),
        optional_providers=_section(raw, "optional_providers"),
        raw=raw,
    )
