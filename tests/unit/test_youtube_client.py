import pytest
import requests
import responses

from trendpulse.ingestion.quota import QuotaBudgetExceeded, QuotaTracker
from trendpulse.ingestion.youtube_client import (
    BASE_URL,
    NonRetryableError,
    QuotaExhaustedError,
    YouTubeApiError,
    YouTubeClient,
)
from trendpulse.settings import IngestionCfg, QuotaCfg

URL = f"{BASE_URL}/videos"


def make_client(budget=9000, used=0, retries=3):
    logs, sleeps = [], []
    cfg = IngestionCfg(max_retries=retries, backoff_base_seconds=0.5, min_seconds_between_requests=0)
    q = QuotaTracker(QuotaCfg(daily_budget=budget, refresh_reserve=min(600, budget - 1)), used_before_run=used)
    c = YouTubeClient("SECRET-KEY-123456", cfg, q, on_request=logs.append, sleep=sleeps.append)
    return c, q, logs, sleeps


@responses.activate
def test_success_logs_and_bills_without_exposing_key():
    responses.get(URL, json={"items": [{"id": "a"}, {"id": "b"}]})
    c, q, logs, _ = make_client()
    r = c.request("videos.list", {"part": "snippet", "id": "a,b"})
    assert r.status == 200 and len(r.payload["items"]) == 2
    assert q.used_this_run == 1 and logs[0]["items_returned"] == 2
    assert "key" not in logs[0]["params"]
    assert "SECRET-KEY" not in str(logs)


@responses.activate
def test_retries_5xx_with_backoff_then_succeeds():
    responses.get(URL, status=503, json={"error": {"errors": [{"reason": "backendError"}]}})
    responses.get(URL, status=500, json={})
    responses.get(URL, json={"items": []})
    c, q, logs, sleeps = make_client()
    r = c.request("videos.list", {"part": "id"})
    assert r.attempts == 3 and len(sleeps) == 2
    assert all(0 <= s <= 30 for s in sleeps)
    assert q.used_this_run == 3  # Google bills answered requests, including errors


@responses.activate
def test_retry_after_header_is_honoured():
    responses.get(URL, status=429, headers={"Retry-After": "7"}, json={})
    responses.get(URL, json={"items": []})
    c, _, _, sleeps = make_client()
    c.request("videos.list", {})
    assert sleeps == [7.0]


@responses.activate
def test_rate_limit_403_is_retried_but_quota_403_is_not():
    responses.get(URL, status=403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
    responses.get(URL, json={"items": []})
    c, _, _, sleeps = make_client()
    assert c.request("videos.list", {}).attempts == 2
    responses.reset()
    responses.get(URL, status=403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    with pytest.raises(QuotaExhaustedError):
        c.request("videos.list", {})
    assert len(responses.calls) == 1


@responses.activate
def test_client_errors_fail_fast():
    responses.get(URL, status=404, json={"error": {"errors": [{"reason": "videoChartNotFound"}]}})
    c, _, logs, _ = make_client()
    with pytest.raises(NonRetryableError) as ei:
        c.request("videos.list", {"chart": "mostPopular", "regionCode": "JP"})
    assert ei.value.reason == "videoChartNotFound" and len(logs) == 1


@responses.activate
def test_transport_errors_retry_and_are_not_billed():
    responses.get(URL, body=requests.ConnectionError("boom"))
    responses.get(URL, body=requests.Timeout("slow"))
    responses.get(URL, json={"items": []})
    c, q, logs, _ = make_client()
    c.request("videos.list", {})
    assert q.used_this_run == 1 and logs[0]["quota_cost"] == 0 and "ConnectionError" in logs[0]["error"]


@responses.activate
def test_gives_up_after_max_retries():
    for _ in range(3):
        responses.get(URL, status=500, json={})
    c, _, _, _ = make_client(retries=2)
    with pytest.raises(YouTubeApiError):
        c.request("videos.list", {})


def test_budget_checked_before_sending():
    c, _, logs, _ = make_client(budget=100, used=100)
    with pytest.raises(QuotaBudgetExceeded):
        c.request("videos.list", {})
    assert logs == []


@responses.activate
def test_pagination_follows_tokens_up_to_limit():
    responses.get(URL, json={"items": [{"id": "1"}], "nextPageToken": "p2"})
    responses.get(URL, json={"items": [{"id": "2"}], "nextPageToken": "p3"})
    responses.get(URL, json={"items": [{"id": "3"}]})
    c, _, _, _ = make_client()
    pages = list(c.paginate("videos.list", {"chart": "mostPopular"}, max_pages=2))
    assert [p for p, _ in pages] == [0, 1]
    assert "pageToken=p2" in responses.calls[1].request.url
    responses.reset()
    responses.get(URL, json={"items": [{"id": "1"}]})
    assert len(list(c.paginate("videos.list", {}, max_pages=5))) == 1


def test_requires_api_key():
    with pytest.raises(ValueError):
        YouTubeClient("", IngestionCfg(), QuotaTracker(QuotaCfg()))
