"""Module 6 — Execution History & Recovery."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd
import streamlit as st

from trendpulse.dashboard import auth, trigger
from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import engineering_guard
from trendpulse.dashboard.figures import timeline_figure
from trendpulse.dashboard.ops import recovery_actions
from trendpulse.dashboard.runtime import operator_config, qo
from trendpulse.settings import get_settings

STATUSES = ["success", "warning", "partial", "dq_failed", "failed", "skipped", "abandoned", "running"]
PAGE = 25


@st.dialog("Confirm operator action")
def _confirm(action_key: str, label: str, run_id: str | None, cli: str | None) -> None:
    token, repo, _ = operator_config()
    st.markdown(f"**{label}**")
    st.markdown(f"This dispatches the `ingest.yml` GitHub Actions workflow on `{repo}` "
                f"({'action=replay, run_id=' + run_id if action_key in {'replay', 'reconcile'} else 'action=run'}). "
                "It runs with the pipeline's credentials on GitHub — not in this dashboard — and is subject to "
                "the workflow concurrency group and the database lock.")
    if cli:
        st.code(cli, language="bash")
    c1, c2 = st.columns(2)
    if c1.button("Dispatch", type="primary"):
        ok, msg = trigger.dispatch(token, repo, action="replay" if action_key in {"replay", "reconcile"} else "run",
                                   run_id=run_id)
        (st.success if ok else st.error)(msg)
    if c2.button("Cancel"):
        st.rerun()


def _operator_panel() -> bool:
    token, repo, phrase = operator_config()
    configured = trigger.enabled(token, repo, phrase)
    if not configured:
        st.caption("Operator dispatch is not configured on this deployment (no dispatch token / passphrase "
                   "secrets). Recovery actions are shown with their CLI equivalent.")
        return False
    if auth.is_operator(st.session_state):
        c1, c2 = st.columns([3, 1])
        c1.markdown(ui.badge("operator session active", ui.OK, "●"), unsafe_allow_html=True)
        if c2.button("Sign out", key="op_out"):
            auth.sign_out(st.session_state)
            st.rerun()
        return True
    with st.form("op_sign_in", border=True):
        given = st.text_input("Operator passphrase", type="password")
        if st.form_submit_button("Sign in"):
            ok, msg = auth.sign_in(st.session_state, given, phrase)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()
    return False


def _detail(run_id: str, tz: str) -> None:
    run = qo("run_row", run_id)
    if not run:
        st.warning("Execution not found.")
        return
    state = qo("dag_state", run_id)
    st.markdown(f"<div style='display:flex;gap:10px;align-items:center;margin:8px 0'><span class='tp-mono' "
                f"style='font-size:.95rem'>{run_id}</span>{ui.status_badge(run['status'])}"
                f"{ui.status_badge(run.get('dq_status'), ui.DQ_STATUS) if run.get('dq_status') else ''}</div>",
                unsafe_allow_html=True)
    tabs = st.tabs(["Timeline", "Stages", "Logs & errors", "Retry history", "Metrics", "Recovery"])
    with tabs[0]:
        fig = timeline_figure(state, tz)
        if fig.data:
            ui.plot(fig, key="ex_tl")
        else:
            st.info("No stage timings recorded.")
    with tabs[1]:
        st.dataframe(state[["order", "label", "status", "duration_ms", "records_in", "records_out", "rejected",
                            "attempts", "condition", "detail"]], hide_index=True, height=520,
                     column_config={"detail": st.column_config.TextColumn("Detail / error", width="large")})
    with tabs[2]:
        errs = qo("run_logs", run_id, ["ERROR", "WARNING"], None, None, None, None, 200)
        if run.get("error_message"):
            ui.banner(f"<b>Run error</b> · <span class='tp-mono'>{ui.esc(run['error_message'])[:1200]}</span>",
                      "critical", "✕")
        for r in errs[errs.exc_text.notna()].head(5).itertuples():
            with st.expander(f"{r.level} · {r.stage} · {r.message[:100]}", expanded=True):
                st.code(r.exc_text, language="text")
        logs = qo("run_logs", run_id, None, None, None, None, None, 500)
        if logs.empty:
            st.caption("No persisted logs for this run (runs before schema v2 only logged to stdout).")
        else:
            st.dataframe(logs.assign(logged_at=logs.logged_at.dt.tz_convert(tz))[
                ["logged_at", "level", "stage", "logger", "message"]], hide_index=True, height=360,
                column_config={"logged_at": st.column_config.DatetimeColumn("Time", format="HH:mm:ss.SSS"),
                               "message": st.column_config.TextColumn("Message", width="large")})
    with tabs[3]:
        rs = state[state.attempts.fillna(1) > 1]
        st.markdown("**Stage-level retries** (transient database errors are retried by the orchestrator)")
        st.dataframe(rs[["label", "attempts", "status", "detail"]], hide_index=True) if not rs.empty \
            else st.caption("No stage needed a retry.")
        api = qo("api_calls_for_run", run_id, True)
        st.markdown("**API-level retries and errors** (YouTube client: backoff with jitter, Retry-After)")
        st.dataframe(api.assign(requested_at=api.requested_at.dt.tz_convert(tz)), hide_index=True, height=300) \
            if not api.empty else st.caption("No API retries or error responses.")
    with tabs[4]:
        ui.kv([("Trigger", run["trigger"]), ("Data source", run["data_source"]),
               ("Started", ui.fmt_ts(run["started_at"], tz)), ("Finished", ui.fmt_ts(run.get("finished_at"), tz)),
               ("Duration", f"{(run.get('duration_seconds') or 0):.2f} s"),
               ("Bronze responses", ui.fmt_int(run.get("records_bronze"))),
               ("Silver valid rows", ui.fmt_int(run.get("records_silver"))),
               ("Rejected (quarantine)", ui.fmt_int(run.get("records_quarantined"))),
               ("Gold rows written", ui.fmt_int(run.get("records_gold"))),
               ("Videos · topics", f"{ui.fmt_int(run.get('videos_observed'))} · {ui.fmt_int(run.get('topics_observed'))}"),
               ("Quota units", ui.fmt_int(run.get("quota_used"))), ("Gold published", str(bool(run.get("gold_published")))),
               ("Git SHA", run.get("git_sha") or "—")])
    with tabs[5]:
        operator = _operator_panel()
        token, repo, phrase = operator_config()
        bronze = qo("bronze_available", run_id)
        acts = recovery_actions(run, bronze, operator, trigger.enabled(token, repo, phrase), get_settings())
        st.markdown(f"Bronze responses retained for this run: **{bronze if bronze >= 0 else 'not visible to this role'}**"
                    f" · replay window {get_settings().pipeline.retention.bronze_days} days")
        for a in acts:
            c1, c2, c3 = st.columns([2.2, 3.5, 1.1])
            c1.markdown(f"**{a.label}**")
            c2.markdown(f"<span class='tp-caption'>{ui.esc(a.reason)}</span>"
                        + (f"<br><code>{ui.esc(a.cli)}</code>" if a.cli else ""), unsafe_allow_html=True)
            if c3.button("Run…", key=f"act_{a.key}", disabled=not a.available,
                         help=None if a.available else a.reason):
                _confirm(a.key, a.label, run_id, a.cli)


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    if not engineering_guard("Execution History & Recovery", "Every pipeline execution with its timeline, logs, "
                                                             "retries and the recovery actions the backend supports"):
        return
    f1, f2, f3 = st.columns([2.4, 1, 1])
    statuses = f1.multiselect("Status", STATUSES, key="ex_status", placeholder="All statuses",
                              help="'warning' = DQ warnings or partial ingestion")
    today = datetime.now().date()
    since = f2.date_input("From (UTC)", today - timedelta(days=30), key="ex_from")
    until = f3.date_input("To (UTC, inclusive)", today, key="ex_to")
    s_dt = pd.Timestamp(datetime.combine(since, time.min), tz="UTC").to_pydatetime()
    u_dt = pd.Timestamp(datetime.combine(until + timedelta(days=1), time.min), tz="UTC").to_pydatetime()
    total = qo("run_count", statuses or None, s_dt, u_dt)
    offset = ui.pager(total, PAGE, "ex_page")
    runs = qo("runs", statuses or None, s_dt, u_dt, PAGE, offset)
    if runs.empty:
        ui.empty_state("No executions match", "Adjust the filters.")
        return
    show = runs.assign(started=runs.started_at.dt.tz_convert(tz), ended=runs.finished_at.dt.tz_convert(tz))
    ev = st.dataframe(show[["run_id", "trigger", "started", "ended", "duration_seconds", "status", "dq_status",
                            "failed_stage", "retries", "records_silver", "records_quarantined", "gold_published"]],
                      hide_index=True, height=360, on_select="rerun", selection_mode="single-row", key="ex_tbl",
                      column_config={
                          "run_id": st.column_config.TextColumn("Execution ID", width="medium"),
                          "started": st.column_config.DatetimeColumn(f"Start ({tz})", format="D MMM HH:mm:ss"),
                          "ended": st.column_config.DatetimeColumn("End", format="HH:mm:ss"),
                          "duration_seconds": st.column_config.NumberColumn("Duration s", format="%.1f"),
                          "dq_status": "DQ", "failed_stage": "Failed stage", "retries": "Retries",
                          "records_silver": "Records", "records_quarantined": "Rejected", "gold_published": "Gold"})
    sel = ev.selection.rows if ev is not None and hasattr(ev, "selection") else []
    qp = st.query_params.get("run")
    run_id = runs.iloc[sel[0]].run_id if sel else (qp if qp and qo("run_row", qp) else runs.iloc[0].run_id)
    _detail(run_id, tz)
