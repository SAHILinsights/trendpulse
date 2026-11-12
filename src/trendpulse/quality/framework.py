"""Configurable data-quality framework.

Two kinds of checks
-------------------
* **Record-level** checks evaluate every Silver candidate row. Rows that
  fail a ``quarantine`` check are removed from the batch and written to
  ``quarantine_records`` with the reason — never silently discarded.
* **Dataset-level** checks evaluate the batch as a whole (schema drift,
  staleness, volume anomalies, topic coverage).

Status rules
------------
* ``PASS``     — nothing failed.
* ``WARNING``  — failures exist but are within tolerance (or the check is
  configured with ``severity: warning``).
* ``FAIL``     — a ``critical`` check whose failed fraction exceeds
  ``quality.critical_failure_ratio`` (record checks) or whose dataset
  condition is violated. Any FAIL closes the gate: Gold is not published.

Every check writes one row per run to ``dq_results`` (the audit table).
Checks can be disabled or re-graded in ``pipeline.yaml`` under
``quality.checks.<name>: {enabled: bool, severity: critical|warning}``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from trendpulse.settings import Settings
from trendpulse.transform.silver import REQUIRED_PATHS, SchemaProfile
from trendpulse.youtube_categories import CATEGORY_NAMES

PASS, WARNING, FAIL = "PASS", "WARNING", "FAIL"


@dataclass
class CheckResult:
    check_name: str
    severity: str
    status: str
    total_checked: int
    passed: int
    failed: int
    warnings: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_row(self, run_id: str, executed_at: datetime) -> dict[str, Any]:
        return {"run_id": run_id, "check_name": self.check_name, "severity": self.severity, "status": self.status,
                "total_checked": self.total_checked, "passed": self.passed, "failed": self.failed,
                "warnings": self.warnings, "details": json.dumps(self.details, default=str)[:4000],
                "executed_at": executed_at}


@dataclass
class RecordCheck:
    name: str
    severity: str
    action: str  # quarantine | flag
    description: str
    fn: Callable[[pd.DataFrame, Settings, datetime], pd.Series]  # True = failing row


def _issue(df: pd.DataFrame, prefix: str) -> pd.Series:
    return df["_issues"].map(lambda xs: any(x.startswith(prefix) for x in xs))


def _negative(col: str):
    return lambda df, s, now: df[col].fillna(0).astype("int64") < 0


def _invalid_ts(df: pd.DataFrame, s: Settings, now: datetime) -> pd.Series:
    skew = timedelta(minutes=s.pipeline.quality.max_future_skew_minutes)
    pub = pd.to_datetime(df["published_at"], utc=True)
    obs = pd.to_datetime(df["observed_at"], utc=True)
    return (_issue(df, "unparseable:published_at") | pub.isna() | obs.isna()
            | (pub > pd.Timestamp(now + skew)) | (obs > pd.Timestamp(now + skew)))


def _duplicates(df: pd.DataFrame, s: Settings, now: datetime) -> pd.Series:
    keys = ["run_id", "video_id", "region_code", "discovery_method", "discovery_query"]
    ranked = df.assign(_r=df["chart_rank"].fillna(10**9)).sort_values("_r", kind="stable")
    dup = ranked.duplicated(keys, keep="first")
    return dup.reindex(df.index)


RECORD_CHECKS: list[RecordCheck] = [
    RecordCheck("null_video_id", "critical", "quarantine", "video id is missing or blank",
                lambda df, s, now: df["video_id"].isna() | (df["video_id"].astype(str).str.strip() == "")),
    RecordCheck("duplicate_observations", "warning", "quarantine",
                "same (run, video, region, discovery context) seen more than once; first/best-ranked kept",
                _duplicates),
    RecordCheck("invalid_region_code", "critical", "quarantine", "region not in countries.yaml",
                lambda df, s, now: ~df["region_code"].isin(s.all_region_codes)),
    RecordCheck("negative_view_count", "critical", "quarantine", "viewCount < 0", _negative("view_count")),
    RecordCheck("negative_like_count", "critical", "quarantine", "likeCount < 0", _negative("like_count")),
    RecordCheck("negative_comment_count", "critical", "quarantine", "commentCount < 0", _negative("comment_count")),
    RecordCheck("invalid_timestamps", "critical", "quarantine",
                "publishedAt/observedAt missing, unparseable or in the future", _invalid_ts),
    RecordCheck("invalid_metric_types", "critical", "quarantine",
                "statistics value is not an integer (or viewCount missing)",
                lambda df, s, now: _issue(df, "invalid_type:view_count") | _issue(df, "invalid_type:like_count")
                | _issue(df, "invalid_type:comment_count") | df["view_count"].isna()),
    RecordCheck("missing_channel_id", "critical", "quarantine", "snippet.channelId missing",
                lambda df, s, now: df["channel_id"].isna() | (df["channel_id"].astype(str).str.strip() == "")),
    RecordCheck("invalid_category", "warning", "flag", "categoryId missing or not a known YouTube category",
                lambda df, s, now: ~df["category_id"].isin(list(CATEGORY_NAMES))),
]


def _check_cfg(settings: Settings, name: str, default_sev: str) -> tuple[bool, str]:
    c = settings.pipeline.quality.checks.get(name, {}) or {}
    return bool(c.get("enabled", True)), c.get("severity", default_sev)


@dataclass
class RecordValidation:
    valid: pd.DataFrame
    quarantined: list[dict[str, Any]]
    results: list[CheckResult]


def _record_key(row: pd.Series) -> str:
    return "|".join(str(row.get(k)) for k in ("video_id", "region_code", "discovery_method", "discovery_query"))


def validate_records(df: pd.DataFrame, settings: Settings, run_id: str, now: datetime) -> RecordValidation:
    total = len(df)
    ratio_limit = settings.pipeline.quality.critical_failure_ratio
    quarantine_mask = pd.Series(False, index=df.index)
    reasons: dict[int, list[str]] = {}
    results: list[CheckResult] = []
    for chk in RECORD_CHECKS:
        enabled, severity = _check_cfg(settings, chk.name, chk.severity)
        if not enabled:
            continue
        failing = chk.fn(df, settings, now).fillna(True).astype(bool) if total else pd.Series(dtype=bool)
        n_fail = int(failing.sum())
        if n_fail == 0:
            status = PASS
        elif severity == "critical" and total and n_fail / total > ratio_limit:
            status = FAIL
        else:
            status = WARNING
        examples = df.loc[failing, "video_id"].astype(str).head(5).tolist() if n_fail else []
        results.append(CheckResult(chk.name, severity, status, total, total - n_fail,
                                   n_fail if chk.action == "quarantine" else 0,
                                   n_fail if chk.action == "flag" else 0,
                                   {"description": chk.description, "action": chk.action,
                                    "failed_ratio": round(n_fail / total, 4) if total else 0, "examples": examples}))
        if chk.action == "quarantine" and n_fail:
            quarantine_mask |= failing
            for idx in df.index[failing]:
                reasons.setdefault(idx, []).append(chk.name)

    quarantined = []
    for idx, names in reasons.items():
        row = df.loc[idx]
        quarantined.append({
            "run_id": run_id, "stage": "silver", "check_name": names[0], "record_key": _record_key(row)[:200],
            "reason": ", ".join(names),
            "payload": json.dumps(row.get("_raw"), default=str, ensure_ascii=False)[:8000],
            "quarantined_at": now,
        })
    valid = df.loc[~quarantine_mask].copy()
    return RecordValidation(valid, quarantined, results)


# ---------------------------------------------------------------- dataset-level checks
def check_schema(profile: SchemaProfile, settings: Settings) -> CheckResult:
    items = profile.items
    missing = sum(profile.missing_required.values())
    worst = max(profile.missing_required.values(), default=0)
    ratio = worst / items if items else 0
    if items == 0:
        status = WARNING
    elif ratio > settings.pipeline.quality.critical_failure_ratio:
        status = FAIL
    elif missing or profile.unexpected_keys:
        status = WARNING
    else:
        status = PASS
    return CheckResult("schema_drift", "critical", status, items, items - worst, worst,
                       len(profile.unexpected_keys),
                       {"required_paths": REQUIRED_PATHS, "missing_required": profile.missing_required,
                        "unexpected_top_level_keys": profile.unexpected_keys})


def check_stale(valid: pd.DataFrame, now: datetime, settings: Settings,
                last_success_at: datetime | None) -> CheckResult:
    q = settings.pipeline.quality
    if valid.empty:
        return CheckResult("stale_data", "critical", FAIL, 0, 0, 0, 0, {"reason": "no valid observations in batch"})
    newest = pd.to_datetime(valid["observed_at"], utc=True).max()
    age_min = (pd.Timestamp(now) - newest).total_seconds() / 60
    gap_min = (now - last_success_at).total_seconds() / 60 if last_success_at else None
    if age_min > q.stale_after_minutes:
        status = FAIL
    elif gap_min is not None and gap_min > q.stale_after_minutes:
        status = WARNING
    else:
        status = PASS
    return CheckResult("stale_data", "critical", status, len(valid), len(valid) if status != FAIL else 0,
                       0 if status != FAIL else len(valid), 1 if status == WARNING else 0,
                       {"newest_observation_age_minutes": round(age_min, 1),
                        "minutes_since_previous_success": round(gap_min, 1) if gap_min is not None else None,
                        "threshold_minutes": q.stale_after_minutes})


def check_volume(count: int, history: list[int], settings: Settings) -> CheckResult:
    q = settings.pipeline.quality
    if len(history) < 3:
        return CheckResult("volume_anomaly", "critical", PASS, count, count, 0, 0,
                           {"reason": "insufficient history (<3 successful runs)", "history": history})
    median = float(pd.Series(history).median())
    change = (count - median) / median if median else 0.0
    if median and change <= -q.volume_failure_drop:
        status = FAIL
    elif abs(change) >= q.volume_warning_change:
        status = WARNING
    else:
        status = PASS
    return CheckResult("volume_anomaly", "critical", status, count, count if status != FAIL else 0,
                       count if status == FAIL else 0, 1 if status == WARNING else 0,
                       {"records": count, "median_recent": median, "relative_change": round(change, 4)})


def check_topic_coverage(valid: pd.DataFrame, assignments: pd.DataFrame) -> CheckResult:
    vids = set(valid["video_id"])
    countable = set(assignments.loc[assignments.review_status.isin(["auto", "unclassified"]), "video_id"])
    missing = vids - countable
    status = PASS if not missing else FAIL
    return CheckResult("missing_topic_ids", "critical", status, len(vids), len(vids) - len(missing), len(missing), 0,
                       {"examples": sorted(missing)[:5]})


def gate_passed(results: list[CheckResult]) -> bool:
    return not any(r.status == FAIL for r in results)


def overall_status(results: list[CheckResult]) -> str:
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status == WARNING for r in results):
        return WARNING
    return PASS
