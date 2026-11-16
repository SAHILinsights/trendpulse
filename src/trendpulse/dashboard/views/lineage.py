"""Module 5 — Data Lineage & Observability."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import engineering_guard
from trendpulse.dashboard.figures import lineage_figure
from trendpulse.dashboard.ops import lineage_for, lineage_graph
from trendpulse.dashboard.runtime import q, qo
from trendpulse.orchestration import dag

LEVELS = ["ERROR", "WARNING", "INFO"]
PAGE = 100


def _lineage(tz: str) -> None:
    nodes, edges = lineage_graph()
    c1, c2 = st.columns([2.3, 1])
    with c2:
        st.session_state.setdefault("ln_ds", "silver_video_observations")
        sel = st.selectbox("Dataset", list(nodes.dataset), key="ln_ds")
    lin = lineage_for(sel)
    related = set(lin["upstream"]) | set(lin["downstream"])
    with c1:
        ev = st.plotly_chart(lineage_figure(nodes, edges, sel, related), width="stretch", key="ln_fig",
                             on_select="rerun", selection_mode="points", config={"displayModeBar": False})
        pts = getattr(getattr(ev, "selection", None), "points", None) or []
        for p in pts:
            cd = p.get("customdata") if isinstance(p, dict) else None
            if cd and cd[0] != sel:
                st.session_state["ln_ds"] = cd[0]
                st.rerun()
    with c2:
        st.markdown("**Upstream**<br>" + ("<br>".join(ui.mono(x) for x in lin["upstream"]) or "—")
                    + "<br><br>**Downstream**<br>" + ("<br>".join(ui.mono(x) for x in lin["downstream"]) or "—"),
                    unsafe_allow_html=True)
        st.caption("Edges are declared per stage in orchestration/dag.py (inputs → outputs) and tested against "
                   "the orchestrator's stage list; nothing is inferred.")
    ui.section("Transformations touching this dataset", "Per execution: the stage that produced each edge")
    runs = qo("runs", None, None, None, 100)
    if runs.empty:
        return
    labels = {r.run_id: f"{pd.Timestamp(r.started_at).tz_convert(tz).strftime('%d %b %H:%M')} · {r.status} · "
                        f"{r.run_id[:8]}" for r in runs.itertuples()}
    rid = st.selectbox("Execution", list(runs.run_id), format_func=lambda i: labels[i], key="ln_run")
    m = qo("lineage_run_metrics", rid)
    m = m[(m.source == sel) | (m.target == sel)]
    m = m.assign(processed_at=pd.to_datetime(m.processed_at, utc=True).dt.tz_convert(tz))
    st.dataframe(m, hide_index=True, column_config={
        "processed_at": st.column_config.DatetimeColumn("Processed", format="D MMM HH:mm:ss"),
        "duration_ms": st.column_config.NumberColumn("Duration ms", format="%d"), "run_id": None})


def _observability(tz: str) -> None:
    days = st.segmented_control("Window", [1, 2, 7], default=2, format_func=lambda d: f"{d} d", key="ob_days") or 2
    ob = qo("observability", days)
    if ob.empty:
        st.info("No executions in this window.")
        return
    ob = ob.assign(t=ob.started_at.dt.tz_convert(tz))
    api = qo("api_retry_stats", days)
    ui.strip([
        ("Executions", f"{len(ob)}", f"{len(ob) / (days * 24):.1f} per hour"),
        ("Median throughput", f"{ob.throughput_rps.median():,.0f}/s", "valid Silver rows per second"),
        ("Stage failures", f"{int(ob.failed_stages.sum())}", f"{int((ob.failed_stages > 0).sum())} runs affected"),
        ("Stage retries", f"{int(ob.retries.sum())}", "DB-level stage retries"),
        ("API retries", f"{int(api.retries.sum()) if not api.empty else 0}",
         f"{int(api.errors.sum()) if not api.empty else 0} error responses"),
        ("Rejection rate", f"{ob.rejection_rate.mean():.2%}" if ob.rejection_rate.notna().any() else "—",
         "mean of quarantined / candidates"),
    ])

    def line(df, x, y, title, ytitle, fmt=".1f", color=0):
        fig = go.Figure(go.Scatter(x=df[x], y=df[y], mode="lines", line=dict(width=2, color=ui.SERIES[color]),
                                   hovertemplate="%{x|%d %b %H:%M}<br>%{y:" + fmt + "}<extra></extra>"))
        ui.section(title)
        ui.plot(ui.style_fig(fig, 200, legend=False, ytitle=ytitle), key=f"ob_{y}")

    a, b = st.columns(2)
    with a:
        line(ob, "t", "throughput_rps", "Pipeline throughput", "rows / s")
    with b:
        line(ob, "t", "duration_seconds", "Execution duration", "seconds", color=2)
    a, b = st.columns(2)
    with a:
        sd = qo("stage_duration_trend", days)
        if not sd.empty:
            top = sd.groupby("stage").duration_ms.median().sort_values(ascending=False).head(5).index
            fig = go.Figure()
            for i, stg in enumerate(top):
                g = sd[sd.stage == stg]
                fig.add_trace(go.Scatter(x=g.started_at.dt.tz_convert(tz), y=g.duration_ms, mode="lines",
                                         name=dag.SPEC_BY_NAME[stg].label, line=dict(width=1.5, color=ui.SERIES[i])))
            ui.section("Stage duration (5 slowest stages)")
            ui.plot(ui.style_fig(fig, 230, ytitle="ms"), key="ob_stage")
    with b:
        if not api.empty:
            api = api.assign(t=api.requested_at.dt.tz_convert(tz))
            fig = go.Figure([go.Bar(x=api.t, y=api.retries, name="API retries", marker_color=ui.SERIES[3]),
                             go.Bar(x=api.t, y=api.errors, name="API error responses", marker_color=ui.SERIES[7])])
            fig.update_layout(barmode="group")
            ui.section("Retry & error frequency (YouTube client)")
            ui.plot(ui.style_fig(fig, 230, ytitle="per run"), key="ob_api")
    a, b = st.columns(2)
    with a:
        line(ob.assign(rr=ob.rejection_rate * 100), "t", "rr", "Record rejection rate", "%", ".2f", color=4)
    with b:
        qu = q("quota_usage", 14)
        if not qu.empty:
            fig = go.Figure()
            for i, (ep, g) in enumerate(qu.groupby("endpoint")):
                fig.add_trace(go.Bar(x=g.quota_day.astype(str), y=g.units, name=ep, marker_color=ui.SERIES[i]))
            fig.update_layout(barmode="stack")
            ui.section("API quota consumption (Pacific quota day)")
            ui.plot(ui.style_fig(fig, 230, ytitle="units"), key="ob_quota")
    lv = qo("log_level_counts", days)
    if not lv.empty:
        st.markdown("Log volume: " + " ".join(f"{ui.badge(r.level, ui.FAIL_TEXT if r.level == 'ERROR' else ui.WARN if r.level == 'WARNING' else ui.TEXT_2)} {r.n:,}"
                                              for r in lv.itertuples()), unsafe_allow_html=True)


def _logs(tz: str) -> None:
    c1, c2, c3, c4 = st.columns([2, 1.4, 1.6, 1.4])
    search = c1.text_input("Search message / stack trace", key="lg_q")
    levels = c2.multiselect("Severity", LEVELS, default=LEVELS, key="lg_lv")
    run = c3.text_input("Execution ID", key="lg_run", placeholder="full run UUID")
    stage = c4.selectbox("Stage", ["(all)"] + list(dag.SPEC_BY_NAME), key="lg_stage")
    d1, d2 = st.columns(2)
    today = datetime.now().date()
    since = d1.date_input("From (UTC)", today - timedelta(days=7), key="lg_from")
    until = d2.date_input("To (UTC, inclusive)", today, key="lg_to")
    s_dt = pd.Timestamp(datetime.combine(since, time.min), tz="UTC")
    u_dt = pd.Timestamp(datetime.combine(until + timedelta(days=1), time.min), tz="UTC")
    page = st.number_input("Page", 1, 1000, 1, key="lg_page")
    logs = qo("run_logs", run or None, levels or None, search or None, None if stage == "(all)" else stage,
              s_dt.to_pydatetime(), u_dt.to_pydatetime(), PAGE, (int(page) - 1) * PAGE)
    st.caption(f"{len(logs)} records on this page (max {PAGE}) · newest first · persisted per run in "
               "pipeline_log_records (INFO and above)")
    if logs.empty:
        st.info("No log records match.")
        return
    show = logs.assign(logged_at=logs.logged_at.dt.tz_convert(tz), run=logs.run_id.str[:8],
                       has_trace=logs.exc_text.notna())
    ev = st.dataframe(show[["logged_at", "level", "run", "stage", "logger", "message", "has_trace"]],
                      hide_index=True, height=420, on_select="rerun", selection_mode="single-row", key="lg_tbl",
                      column_config={"logged_at": st.column_config.DatetimeColumn(f"Time ({tz})",
                                                                                  format="D MMM HH:mm:ss.SSS"),
                                     "message": st.column_config.TextColumn("Message", width="large"),
                                     "has_trace": st.column_config.CheckboxColumn("Trace")})
    sel = ev.selection.rows if ev is not None and hasattr(ev, "selection") else []
    if sel:
        r = logs.iloc[sel[0]]
        ui.kv([("Execution", ui.Raw(ui.mono(r.run_id))), ("Stage", r.stage), ("Level", r.level),
               ("Logger", r.logger)])
        st.code(r.message + (("\n\n" + r.exc_text) if isinstance(r.exc_text, str) else ""), language="text")


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    if not engineering_guard("Lineage & Observability", "Dataset lineage from Source to Gold, operational metrics "
                                                        "and structured run logs"):
        return
    tabs = st.tabs(["Data lineage", "Observability", "Log explorer"])
    with tabs[0]:
        _lineage(tz)
    with tabs[1]:
        _observability(tz)
    with tabs[2]:
        _logs(tz)
