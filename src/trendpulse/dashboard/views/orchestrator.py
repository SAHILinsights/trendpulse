"""Module 2 — Pipeline Orchestrator: the DAG of one execution, driven by recorded stage metadata."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import engineering_guard
from trendpulse.dashboard.figures import dag_figure
from trendpulse.dashboard.runtime import qo
from trendpulse.orchestration import dag


def _run_label(r) -> str:
    return f"{pd.Timestamp(r.started_at).strftime('%d %b %H:%M:%S')} · {r.status} · {r.run_id[:8]}"


def select_run(key: str, runs: pd.DataFrame) -> str | None:
    """Run picker shared by modules; honours ?run=<id> deep links."""
    if runs.empty:
        return None
    ids = list(runs.run_id)
    qp = st.query_params.get("run")
    if qp and qp not in ids:
        extra = qo("run_row", qp)
        if extra:
            runs = pd.concat([pd.DataFrame([extra]), runs], ignore_index=True)
            ids = [qp, *ids]
    labels = {r.run_id: _run_label(r) for r in runs.itertuples()}
    processed = [i for i, s in zip(ids, runs.status, strict=False) if s not in {"skipped", "skipped_locked"}]
    default = ids.index(qp) if qp in ids else (ids.index(processed[0]) if processed else 0)
    rid = st.selectbox("Execution", ids, index=default, format_func=lambda i: labels.get(i, i), key=key)
    return rid


def stage_inspector(state: pd.DataFrame, stage: str, run_id: str, tz: str, logs: bool = True) -> None:
    row = state.set_index("stage").loc[stage]
    spec = dag.SPEC_BY_NAME[stage]
    st.markdown(f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div style='font-weight:650;font-size:1rem'>{ui.esc(spec.label)}</div>"
                f"{ui.status_badge(row.status)}</div><div class='tp-caption' style='margin:2px 0 8px'>"
                f"{ui.esc(spec.description)}</div>", unsafe_allow_html=True)
    ui.kv([
        ("Stage", ui.Raw(ui.mono(stage))), ("Execution ID", ui.Raw(ui.mono(run_id))),
        ("Group · order", f"{spec.group} · {int(row.order) + 1} of {len(dag.STAGE_SPECS)}"),
        ("Previous stage", ", ".join(row.upstream) or "—"), ("Next stage", ", ".join(row.downstream) or "—"),
        ("Runs when", spec.condition),
        ("Input datasets", ", ".join(spec.inputs) or "—"), ("Output datasets", ", ".join(spec.outputs) or "—"),
        ("Started", ui.fmt_ts(row.started_at, tz) if pd.notna(row.started_at) else "—"),
        ("Finished", ui.fmt_ts(row.finished_at, tz) if pd.notna(row.finished_at) else "—"),
        ("Duration", ui.fmt_ms(row.duration_ms)),
        ("Records in → out", f"{ui.fmt_int(row.records_in)} → {ui.fmt_int(row.records_out)}"),
        ("Rejected", ui.fmt_int(row.rejected) if row.rejected is not None and pd.notna(row.rejected) else "—"),
        ("Attempts / retries", "—" if pd.isna(row.attempts) else f"{int(row.attempts)} / {int(row.retries)}"),
    ])
    if row.detail:
        role = "critical" if row.status == "failed" else "info"
        st.markdown("<div class='tp-section' style='font-size:.85rem'>Detail / error</div>", unsafe_allow_html=True)
        ui.banner(f"<span class='tp-mono'>{ui.esc(row.detail)[:1500]}</span>", role, "›")
    if not logs:
        return
    logs = qo("run_logs", run_id, None, None, stage, None, None, 100)
    st.markdown(f"<div class='tp-section' style='font-size:.85rem'>Execution logs ({len(logs)})</div>",
                unsafe_allow_html=True)
    if logs.empty:
        st.caption("No log records for this stage (runs before schema v2 did not persist logs).")
    else:
        st.dataframe(logs[["logged_at", "level", "logger", "message"]].assign(
            logged_at=logs.logged_at.dt.tz_convert(tz)), hide_index=True, height=200, column_config={
            "logged_at": st.column_config.DatetimeColumn("Time", format="HH:mm:ss.SSS"),
            "message": st.column_config.TextColumn("Message", width="large")})
        for r in logs[logs.exc_text.notna()].head(3).itertuples():
            with st.expander(f"Stack trace · {r.level} · {r.message[:80]}"):
                st.code(r.exc_text, language="text")


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    if not engineering_guard("Pipeline Orchestrator", "17-stage DAG of a single execution, coloured by recorded "
                                                      "stage status — select a node to inspect it"):
        return
    c1, c2 = st.columns([2.2, 1])
    with c2:
        statuses = st.multiselect("Filter history", ["success", "partial", "dq_failed", "failed", "skipped",
                                                     "abandoned", "running"], key="orc_status",
                                  placeholder="All statuses")
    runs = qo("runs", statuses or None, None, None, 200)
    with c1:
        run_id = select_run("orc_run", runs)
    if not run_id:
        ui.empty_state("No execution matches", "Change the filter.")
        return
    run = qo("run_row", run_id)
    state = qo("dag_state", run_id)
    failed = state[state.status == "failed"]
    ui.strip([
        ("Status", ui.status_badge(run["status"]), ui.esc((run.get("error_message") or "")[:80])),
        ("Trigger · source", ui.esc(run["trigger"]), ui.esc(run["data_source"])),
        ("Started", ui.fmt_ts(run["started_at"], tz).split(", ")[-1], ui.fmt_ts(run["started_at"], tz).split(",")[0]),
        ("Duration", f"{(run.get('duration_seconds') or 0):.1f} s",
         f"{int((state.status == 'success').sum())} ok · {int((state.status == 'skipped').sum())} skipped · "
         f"{len(failed)} failed"),
        ("Records", ui.fmt_int(run.get("records_silver")),
         f"bronze {ui.fmt_int(run.get('records_bronze'))} · rejected {ui.fmt_int(run.get('records_quarantined'))}"),
        ("Quality gate", ui.status_badge(run.get("dq_status"), ui.DQ_STATUS) if run.get("dq_status") else "—",
         "Gold published" if run.get("gold_published") else "Gold not published"),
    ])
    sel = st.session_state.get("orc_node")
    event = st.plotly_chart(dag_figure(state, selected=sel), width="stretch", key="orc_dag",
                            on_select="rerun", selection_mode="points",
                            config={"displayModeBar": False, "scrollZoom": False})
    pts = getattr(getattr(event, "selection", None), "points", None) or []
    for p in pts:
        cd = p.get("customdata") if isinstance(p, dict) else None
        if cd and cd[0] != st.session_state.get("orc_node"):
            st.session_state["orc_node"] = cd[0]
            st.session_state["orc_inspect"] = cd[0]
    legend = " ".join(ui.badge(k.replace("_", " "), ui.NODE_FILL[k]) for k in
                      ["success", "running", "warning", "failed", "skipped", "pending"])
    st.markdown(f"<div class='tp-caption'>{legend} &nbsp; solid arrows = dependency · dotted red = failure path "
                f"(gate FAIL → quarantine still persisted → Gold skipped → metadata + alert) · click a node to "
                f"inspect it</div>", unsafe_allow_html=True)

    default = st.session_state.get("orc_node") or (failed.stage.iloc[0] if not failed.empty else "data_quality")
    if "orc_inspect" not in st.session_state:
        st.session_state["orc_inspect"] = default
    left, right = st.columns([1, 1.25])
    with left:
        stage = st.selectbox("Inspect stage", list(dag.SPEC_BY_NAME),
                             format_func=lambda s: dag.SPEC_BY_NAME[s].label, key="orc_inspect")
        st.session_state["orc_node"] = stage
        with st.container(border=True):
            stage_inspector(state, stage, run_id, tz, logs=False)
    with right:
        logs = qo("run_logs", run_id, None, None, stage, None, None, 100)
        st.markdown(f"<div class='tp-section' style='margin-top:.3rem'>Execution logs · "
                    f"{ui.esc(dag.SPEC_BY_NAME[stage].label)} ({len(logs)})</div>", unsafe_allow_html=True)
        if logs.empty:
            st.caption("No log records for this stage in this run (runs before schema v2 did not persist logs; "
                       "skipped stages log nothing).")
        else:
            st.dataframe(logs[["logged_at", "level", "message"]].assign(logged_at=logs.logged_at.dt.tz_convert(tz)),
                         hide_index=True, height=300, column_config={
                             "logged_at": st.column_config.DatetimeColumn("Time", format="HH:mm:ss.SSS"),
                             "message": st.column_config.TextColumn("Message", width="large")})
            for r in logs[logs.exc_text.notna()].head(3).itertuples():
                with st.expander(f"Stack trace · {r.level} · {r.message[:80]}"):
                    st.code(r.exc_text, language="text")

    ui.section("Stage execution table", "Exactly as recorded in pipeline_stage_runs")
    show = state.assign(started_at=pd.to_datetime(state.started_at, utc=True).dt.tz_convert(tz))
    st.dataframe(show[["order", "label", "group", "status", "started_at", "duration_ms", "records_in", "records_out",
                       "rejected", "attempts", "detail"]], hide_index=True, height=420, column_config={
        "order": st.column_config.NumberColumn("#", width="small"), "label": "Stage", "group": "Group",
        "started_at": st.column_config.DatetimeColumn(f"Started ({tz})", format="HH:mm:ss.SSS"),
        "duration_ms": st.column_config.NumberColumn("Duration ms", format="%d"),
        "records_in": "In", "records_out": "Out", "rejected": "Rejected", "attempts": "Attempts",
        "detail": st.column_config.TextColumn("Detail / error", width="large")})
    st.caption(f"Recovery for this run (replay from Bronze, reconcile): Execution History & Recovery · run "
               f"{run_id}")
