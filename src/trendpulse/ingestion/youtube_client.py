"""Resilient YouTube Data API v3 client.

Responsibilities
----------------
* Quota: every call is checked against :class:`QuotaTracker` *before* it is
  sent and recorded after it is answered (Google bills failed requests too).
* Retries: exponential backoff with full jitter for 429, 5xx, transport
  errors and per-user rate-limit 403s. ``Retry-After`` is honoured.
* Fail fast: 400/404 (bad request / chart not available in region) and
  quota-exhausted 403s are never retried.
* Rate limiting: a minimum interval between consecutive requests.
* Logging: every attempt is reported to ``on_request`` (persisted to
  ``api_request_log``); the API key is never part of logged params.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from trendpulse.ingestion.quota import COSTS, QuotaTracker
from trendpulse.logging_utils import get_logger
from trendpulse.settings import IngestionCfg

log = get_logger("youtube")

BASE_URL = "https://www.googleapis.com/youtube/v3"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


class YouTubeApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class QuotaExhaustedError(YouTubeApiError):
    """Google reported the project's daily quota as exhausted."""


class NonRetryableError(YouTubeApiError):
    """4xx error that retrying cannot fix (bad params, chart unavailable...)."""


@dataclass
class ApiResponse:
    endpoint: str
    params: dict[str, Any]
    status: int
    payload: dict[str, Any]
    attempts: int
    duration_ms: int


RequestLogger = Callable[[dict[str, Any]], None]


def _error_reason(payload: Any) -> str | None:
    try:
        errs = payload["error"].get("errors") or []
        if errs:
            return errs[0].get("reason")
        return payload["error"].get("status")
    except (KeyError, TypeError, AttributeError):
        return None


class YouTubeClient:
    def __init__(self, api_key: str, cfg: IngestionCfg, quota: QuotaTracker, *,
                 session: requests.Session | Any | None = None,
                 on_request: RequestLogger | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 base_url: str = BASE_URL):
        if not api_key:
            raise ValueError("YouTube API key is required")
        self._key = api_key
        self.cfg = cfg
        self.quota = quota
        self.session = session or requests.Session()
        self.on_request = on_request or (lambda _: None)
        self.sleep = sleep
        self.base_url = base_url
        self._last_request_at = 0.0

    # ------------------------------------------------------------------ core
    def _throttle(self) -> None:
        wait = self.cfg.min_seconds_between_requests - (time.monotonic() - self._last_request_at)
        if wait > 0:
            self.sleep(wait)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.cfg.backoff_max_seconds)
            except ValueError:
                pass
        cap = min(self.cfg.backoff_max_seconds, self.cfg.backoff_base_seconds * (2 ** attempt))
        return random.uniform(0, cap)  # full jitter

    def request(self, endpoint: str, params: dict[str, Any], context: dict[str, Any] | None = None) -> ApiResponse:
        """Call ``endpoint`` (e.g. ``videos.list``) with retries and quota control."""
        if endpoint not in COSTS:
            raise ValueError(f"unknown endpoint {endpoint}")
        context = context or {}
        path = endpoint.split(".")[0]
        url = f"{self.base_url}/{path}"
        max_attempts = self.cfg.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            cost = self.quota.reserve(endpoint)  # raises QuotaBudgetExceeded
            self._throttle()
            started = time.monotonic()
            status: int | None = None
            payload: Any = None
            error: str | None = None
            retry_after = None
            try:
                resp = self.session.get(url, params={**params, "key": self._key},
                                        timeout=self.cfg.request_timeout_seconds)
                status = resp.status_code
                retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {"_raw": getattr(resp, "text", "")[:2000]}
            except requests.RequestException as exc:  # transport error: not billed
                error = f"{type(exc).__name__}: {exc}"
                last_error = exc
            duration_ms = int((time.monotonic() - started) * 1000)
            billed = cost if status is not None else 0
            self.quota.record(endpoint, billed)
            items = len(payload.get("items", [])) if isinstance(payload, dict) else None
            reason = _error_reason(payload) if status and status >= 400 else None
            self.on_request({
                "endpoint": endpoint,
                "params": {k: v for k, v in params.items() if k != "key"},
                "http_status": status,
                "quota_cost": billed,
                "items_returned": items,
                "duration_ms": duration_ms,
                "attempt": attempt,
                "error": error or (f"{status} {reason}" if reason else None),
                **{k: context.get(k) for k in ("region_code", "discovery_method")},
            })

            if status is not None and status < 400:
                return ApiResponse(endpoint, params, status, payload, attempt, duration_ms)

            if status == 403 and reason in QUOTA_REASONS:
                raise QuotaExhaustedError(f"{endpoint}: daily quota exhausted", status, reason)
            retryable = (
                status is None
                or status in RETRYABLE_STATUS
                or (status == 403 and reason in RATE_LIMIT_REASONS)
            )
            if not retryable:
                raise NonRetryableError(f"{endpoint} {params.get('regionCode', '')} -> {status} {reason}",
                                        status, reason)
            if attempt < max_attempts:
                delay = self._backoff(attempt, retry_after)
                log.warning("%s attempt %d/%d failed (%s); retrying in %.1fs", endpoint, attempt, max_attempts,
                            error or f"HTTP {status} {reason or ''}".strip(), delay)
                self.sleep(delay)
            last_error = last_error or YouTubeApiError(f"HTTP {status} {reason}", status, reason)

        raise YouTubeApiError(f"{endpoint} failed after {max_attempts} attempts: {last_error}")

    # ------------------------------------------------------------------ helpers
    def paginate(self, endpoint: str, params: dict[str, Any], max_pages: int,
                 context: dict[str, Any] | None = None):
        """Yield successive pages, following ``nextPageToken`` up to ``max_pages``."""
        token = None
        for page in range(max_pages):
            p = dict(params)
            if token:
                p["pageToken"] = token
            resp = self.request(endpoint, p, context)
            yield page, resp
            token = resp.payload.get("nextPageToken")
            if not token:
                break
