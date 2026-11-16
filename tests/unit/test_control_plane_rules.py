"""Pure control-plane logic: DAG declaration, health rules, recovery availability, operator auth."""

from datetime import UTC, datetime, timedelta

import pytest
import responses

from trendpulse.dashboard import auth, trigger
from trendpulse.dashboard.ops import health_status, recovery_actions, valid_run_id
from trendpulse.orchestration import dag
from trendpulse.orchestration.pipeline import STAGES
from trendpulse.storage import schema as s

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
RID = "0f8fad5b-d9cb-469f-a165-70867728950e"


# ------------------------------------------------------------------ DAG declaration
def test_dag_declares_exactly_the_orchestrator_stages_in_order():
    assert [s_.name for s_ in dag.STAGE_SPECS] == STAGES
    pos = {n: i for i, n in enumerate(STAGES)}
    for spec in dag.STAGE_SPECS:
        assert spec.group in dag.GROUPS
        for up in spec.upstream:
            assert pos[up] < pos[spec.name], (up, spec.name)      # acyclic, consistent with execution order
    assert dag.neighbours("data_quality") == (["extract_topics"], ["quarantine_records"])
    assert ("data_quality", "update_execution_metadata", "failure") in dag.edges()


def test_dataset_lineage_only_references_real_tables_or_declared_sources():
    tables = set(s.metadata.tables)
    for src, tgt, stage in dag.dataset_edges():
        for d in (src, tgt):
            assert d in tables or d.startswith("source:"), d
        assert stage.split("+")[-1] in STAGES
    targets = {t for _, t, _ in dag.dataset_edges()}
    assert {"bronze_api_responses", "silver_video_observations", "quarantine_records",
            "fact_topic_observations", "gold_trending_topics"} <= targets


# ------------------------------------------------------------------ health rules
class _S:  # minimal settings stand-in
    class pipeline:  # noqa: N801
        class sources:  # noqa: N801
            class youtube:  # noqa: N801
                live_ingestion_enabled = False

        class retention:  # noqa: N801
            bronze_days = 2


def _run(status, minutes_ago=10, rid=RID, err=None):
    return {"run_id": rid, "status": status, "started_at": NOW - timedelta(minutes=minutes_ago),
            "error_message": err}


def _health(last, ok, freshness="fresh", rate=1.0, quota=100):
    return {"last_attempt": last, "last_success": ok, "freshness": freshness, "freshness_minutes": 20,
            "success_rate_7d": rate, "quota_used_today": quota, "quota_budget": 9000, "interval_minutes": 30}


@pytest.mark.parametrize("health,dq,expected", [
    (None, None, "not_initialized"),
    ({"last_attempt": None}, None, "not_initialized"),
    (_health(_run("dq_failed", 5, err="gate"), _run("success", 40)), "FAIL", "failed"),
    (_health(_run("success", 5), _run("success", 5), freshness="stale"), "PASS", "stale"),
    (_health(_run("partial", 5), _run("partial", 5)), "PASS", "degraded"),
    (_health(_run("success", 5), _run("success", 5)), "WARNING", "degraded"),
    (_health(_run("success", 5), _run("success", 5), rate=0.5), "PASS", "degraded"),
    (_health(_run("success", 5), _run("success", 5), quota=8500), "PASS", "degraded"),
    (_health(_run("success", 5), _run("success", 5)), "PASS", "healthy"),
])
def test_health_rules(health, dq, expected):
    assert health_status(health, dq, _S, NOW).status == expected


def test_failed_then_recovered_is_not_failed():
    h = _health(_run("failed", 60), _run("success", 5))
    h["last_attempt"] = _run("success", 5)
    assert health_status(h, "PASS", _S, NOW).status == "healthy"


# ------------------------------------------------------------------ recovery availability
def _acts(status, bronze, operator, configured):
    return {a.key: a for a in recovery_actions({"run_id": RID, "status": status}, bronze, operator, configured, _S)}


def test_recovery_actions_are_only_offered_when_backed_and_authorised():
    a = _acts("dq_failed", 12, operator=True, configured=True)
    assert a["replay"].available and a["replay"].cli == f"trendpulse replay --run-id {RID}"
    assert not a["retry_stage"].available and not a["backfill"].available     # unsupported, never faked
    assert not a["reconcile"].available                                       # not abandoned
    assert _acts("abandoned", 3, True, True)["reconcile"].available
    no_bronze = _acts("failed", 0, True, True)["replay"]
    assert not no_bronze.available and "Bronze" in no_bronze.reason
    assert not _acts("success", 10, True, True)["replay"].available
    public = _acts("dq_failed", 12, operator=False, configured=True)
    assert not any(x.available for x in public.values()) and "sign-in" in public["replay"].reason
    unconfigured = _acts("dq_failed", 12, operator=False, configured=False)
    assert not unconfigured["run"].available and "CLI" in unconfigured["run"].reason


def test_run_id_validation():
    assert valid_run_id(RID)
    for bad in ("", "abc", RID + ";rm -rf /", RID.upper(), "$(whoami)"):
        assert not valid_run_id(bad)


# ------------------------------------------------------------------ operator auth
def test_operator_sign_in_lockout_and_expiry():
    store: dict = {}
    assert not auth.sign_in(store, "x", None)[0]                 # not configured
    for i in range(auth.MAX_ATTEMPTS - 1):
        ok, msg = auth.sign_in(store, "wrong", "s3cret", now=100.0 + i)
        assert not ok and "attempts left" in msg
    ok, msg = auth.sign_in(store, "wrong", "s3cret", now=110.0)
    assert not ok and "locked" in msg.lower()
    assert not auth.sign_in(store, "s3cret", "s3cret", now=120.0)[0]        # correct but locked
    assert auth.sign_in(store, "s3cret", "s3cret", now=110.0 + auth.LOCKOUT_SECONDS + 1)[0]
    t = 110.0 + auth.LOCKOUT_SECONDS + 1
    assert auth.is_operator(store, now=t + 10)
    assert not auth.is_operator(store, now=t + auth.SESSION_TTL_SECONDS + 1)
    auth.sign_out(store)
    assert not auth.is_operator(store, now=t)


@responses.activate
def test_dispatch_validates_inputs_and_sends_expected_payload():
    url = "https://api.github.com/repos/me/tp/actions/workflows/ingest.yml/dispatches"
    responses.post(url, status=204)
    assert not trigger.dispatch("tok", "me/tp", action="delete")[0]
    assert not trigger.dispatch("tok", "me/tp", action="replay", run_id="$(id)")[0]
    assert not trigger.dispatch("tok", "not a repo", action="run")[0]
    assert len(responses.calls) == 0
    ok, _ = trigger.dispatch("tok", "me/tp", action="replay", run_id=RID)
    import json
    body = json.loads(responses.calls[0].request.body)
    assert ok and body["inputs"] == {"reason": "dashboard operator: replay", "action": "replay", "run_id": RID,
                                     "force_search": "false"}
