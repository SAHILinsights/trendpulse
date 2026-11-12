"""Configuration loading and validation.

Configuration has two sources:

* YAML files in ``config/`` (non-secret, version-controlled behaviour).
* Environment variables (secrets and per-environment overrides).

Every key in ``pipeline.yaml`` can be overridden with
``TRENDPULSE__<SECTION>__<KEY>`` so GitHub Actions or a local shell can tune
behaviour without editing files. Secrets are only ever read from the
environment (or Streamlit secrets in the dashboard) and never logged.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_DATA_DIR = REPO_ROOT / "data"

REGION_CODE_RE = re.compile(r"^[A-Z]{2}$")
WINDOW_RE = re.compile(r"^\d+(m|h|d)$")


# --------------------------------------------------------------------------- models
class Country(BaseModel):
    code: str
    name: str
    enabled: bool = True
    chart_categories: list[int] | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip().upper()
        if not REGION_CODE_RE.match(v):
            raise ValueError(f"invalid ISO-3166 alpha-2 region code: {v!r}")
        return v


class CountriesConfig(BaseModel):
    default_chart_categories: list[int] = Field(default_factory=list)
    countries: list[Country]

    @model_validator(mode="after")
    def _unique(self) -> CountriesConfig:
        codes = [c.code for c in self.countries]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate region codes in countries.yaml")
        if not any(c.enabled for c in self.countries):
            raise ValueError("at least one country must be enabled")
        return self

    @property
    def enabled(self) -> list[Country]:
        return [c for c in self.countries if c.enabled]

    def categories_for(self, country: Country) -> list[int]:
        return country.chart_categories if country.chart_categories is not None else self.default_chart_categories


class SearchSeed(BaseModel):
    id: str
    category: str
    query: str
    regions: list[str] | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9_]+$", v):
            raise ValueError(f"seed id must be snake_case: {v!r}")
        return v


class SearchSeedsConfig(BaseModel):
    seeds: list[SearchSeed]

    @model_validator(mode="after")
    def _unique(self) -> SearchSeedsConfig:
        ids = [s.id for s in self.seeds]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate seed ids in search_seeds.yaml")
        return self


class TaxonomyTopic(BaseModel):
    id: str
    name: str
    keywords: list[str]
    youtube_categories: list[int] = Field(default_factory=list)
    active: bool = True


class TaxonomyCategory(BaseModel):
    id: str
    name: str
    topics: list[TaxonomyTopic]


class TaxonomyConfig(BaseModel):
    categories: list[TaxonomyCategory]

    @model_validator(mode="after")
    def _unique(self) -> TaxonomyConfig:
        seen: set[str] = set()
        for cat in self.categories:
            for t in cat.topics:
                key = f"{cat.id}.{t.id}"
                if key in seen:
                    raise ValueError(f"duplicate taxonomy topic {key}")
                seen.add(key)
                if not t.keywords:
                    raise ValueError(f"taxonomy topic {key} has no keywords")
        return self


class ScheduleCfg(BaseModel):
    interval_minutes: int = 30


class QuotaCfg(BaseModel):
    daily_limit: int = 10_000
    daily_budget: int = 9_000
    refresh_reserve: int = 600
    timezone: str = "America/Los_Angeles"

    @model_validator(mode="after")
    def _check(self) -> QuotaCfg:
        if self.daily_budget > self.daily_limit:
            raise ValueError("quota.daily_budget cannot exceed quota.daily_limit")
        if self.refresh_reserve >= self.daily_budget:
            raise ValueError("quota.refresh_reserve must be smaller than daily_budget")
        return self


class IngestionCfg(BaseModel):
    chart_max_results: int = Field(50, ge=1, le=50)
    chart_pages: int = Field(1, ge=1, le=4)
    search_enabled: bool = True
    search_interval_minutes: int = 180
    searches_per_cycle: int = Field(6, ge=0)
    search_max_results: int = Field(25, ge=1, le=50)
    search_published_within_hours: int = 72
    track_search_videos_hours: int = 72
    max_tracked_videos: int = 1500
    request_timeout_seconds: float = 20
    max_retries: int = 4
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    min_seconds_between_requests: float = 0.05


class DiscoveryCfg(BaseModel):
    enabled: bool = True
    ngram_min: int = 1
    ngram_max: int = 3
    min_videos: int = 3
    min_channels: int = 2
    max_new_topics_per_run: int = 40
    similarity_threshold: float = 0.88
    max_doc_fraction: float = 0.25


class TopicsCfg(BaseModel):
    min_confidence: float = 0.5
    review_confidence: float = 0.25
    max_topics_per_video: int = 3
    confidence_saturation: float = 4.0
    discovery: DiscoveryCfg = Field(default_factory=DiscoveryCfg)


class ScoringCfg(BaseModel):
    w_videos: float = 20.0
    w_channels: float = 10.0
    w_vph: float = 8.0
    w_engagement: float = 1.0


class LifecycleCfg(BaseModel):
    min_platform_history_hours: float = 6
    emerging_hours: float = 24
    lookback_window: str = "24h"
    growth_threshold_pct: float = 15.0
    decline_threshold_pct: float = 15.0
    peak_ratio: float = 0.9
    decline_from_peak_ratio: float = 0.6
    dormant_hours: float = 24


class QualityCfg(BaseModel):
    critical_failure_ratio: float = 0.2
    volume_lookback_runs: int = 12
    volume_warning_change: float = 0.5
    volume_failure_drop: float = 0.9
    stale_after_minutes: int = 120
    max_future_skew_minutes: int = 60
    # per-check overrides: {check_name: {enabled: bool, severity: critical|warning}}
    checks: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RetentionCfg(BaseModel):
    bronze_days: int = 2
    silver_days: int = 1
    api_log_days: int = 30
    api_metadata_max_days: int = 30
    quarantine_days: int = 30
    video_obs_full_days: int = 1
    video_obs_max_days: int = 30
    topic_obs_full_days: int = 3
    topic_obs_hourly_days: int = 14
    topic_obs_max_days: int = 400
    pipeline_history_days: int = 90


class LockCfg(BaseModel):
    ttl_minutes: int = 50


class YouTubeSourceCfg(BaseModel):
    """Live-ingestion switch. Off by default until the API use case is approved
    (see docs/data_source_compliance.md)."""

    live_ingestion_enabled: bool = False
    compliance_approval_reference: str = ""


class SourcesCfg(BaseModel):
    youtube: YouTubeSourceCfg = Field(default_factory=YouTubeSourceCfg)


class AlertsCfg(BaseModel):
    on_failure: bool = True
    on_dq_warning: bool = False
    on_recovery: bool = True


class PipelineConfig(BaseModel):
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    quota: QuotaCfg = Field(default_factory=QuotaCfg)
    ingestion: IngestionCfg = Field(default_factory=IngestionCfg)
    topics: TopicsCfg = Field(default_factory=TopicsCfg)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    lifecycle: LifecycleCfg = Field(default_factory=LifecycleCfg)
    windows: dict[str, float] = Field(default_factory=dict)
    quality: QualityCfg = Field(default_factory=QualityCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    lock: LockCfg = Field(default_factory=LockCfg)
    alerts: AlertsCfg = Field(default_factory=AlertsCfg)
    sources: SourcesCfg = Field(default_factory=SourcesCfg)

    @property
    def comparison_windows(self) -> dict[str, int]:
        """Window label -> minutes (excludes tolerance_fraction)."""
        return {k: int(v) for k, v in self.windows.items() if WINDOW_RE.match(k)}

    @property
    def window_tolerance(self) -> float:
        return float(self.windows.get("tolerance_fraction", 0.5))

    @model_validator(mode="after")
    def _check_windows(self) -> PipelineConfig:
        if not self.windows:
            self.windows = {"30m": 30, "1h": 60, "6h": 360, "24h": 1440, "7d": 10080, "30d": 43200,
                            "tolerance_fraction": 0.5}
        if self.lifecycle.lookback_window not in self.comparison_windows:
            raise ValueError("lifecycle.lookback_window must be one of the configured windows")
        return self


class Secrets(BaseModel):
    """Values that must come from the environment. Repr hides them."""

    youtube_api_key: str | None = None
    database_url: str | None = None
    dashboard_database_url: str | None = None
    alert_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    alert_email_to: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        flags = {k: bool(v) for k, v in self.model_dump().items()}
        return f"Secrets(set={flags})"

    __str__ = __repr__


class Settings(BaseModel):
    config_dir: Path
    data_dir: Path
    countries: CountriesConfig
    seeds: SearchSeedsConfig
    taxonomy: TaxonomyConfig
    pipeline: PipelineConfig
    secrets: Secrets

    model_config = {"arbitrary_types_allowed": True}

    @property
    def region_codes(self) -> list[str]:
        return [c.code for c in self.countries.enabled]

    @property
    def all_region_codes(self) -> list[str]:
        return [c.code for c in self.countries.countries]


# --------------------------------------------------------------------------- loading
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def apply_env_overrides(raw: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Apply ``TRENDPULSE__SECTION__KEY[__SUBKEY]`` overrides onto a raw config mapping."""
    env = os.environ if environ is None else environ
    for name, value in env.items():
        if not name.startswith("TRENDPULSE__"):
            continue
        parts = [p.lower() for p in name.split("__")[1:] if p]
        if not parts:
            continue
        node = raw
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = _coerce(value)
    return raw


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def load_secrets() -> Secrets:
    return Secrets(
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        database_url=_env("DATABASE_URL"),
        dashboard_database_url=_env("DASHBOARD_DATABASE_URL"),
        alert_webhook_url=_env("TRENDPULSE_ALERT_WEBHOOK_URL"),
        smtp_host=_env("TRENDPULSE_SMTP_HOST"),
        smtp_port=int(_env("TRENDPULSE_SMTP_PORT") or 587),
        smtp_user=_env("TRENDPULSE_SMTP_USER"),
        smtp_password=_env("TRENDPULSE_SMTP_PASSWORD"),
        alert_email_to=_env("TRENDPULSE_ALERT_EMAIL_TO"),
    )


def load_settings(config_dir: Path | str | None = None, data_dir: Path | str | None = None,
                  load_dotenv_file: bool = True) -> Settings:
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(REPO_ROOT / ".env", override=False)
        except ImportError:  # pragma: no cover
            pass
    cdir = Path(config_dir or os.environ.get("TRENDPULSE_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
    ddir = Path(data_dir or os.environ.get("TRENDPULSE_DATA_DIR") or DEFAULT_DATA_DIR)
    pipeline_raw = apply_env_overrides(_read_yaml(cdir / "pipeline.yaml"))
    return Settings(
        config_dir=cdir,
        data_dir=ddir,
        countries=CountriesConfig(**_read_yaml(cdir / "countries.yaml")),
        seeds=SearchSeedsConfig(**_read_yaml(cdir / "search_seeds.yaml")),
        taxonomy=TaxonomyConfig(**_read_yaml(cdir / "taxonomy.yaml")),
        pipeline=PipelineConfig(**pipeline_raw),
        secrets=load_secrets(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
