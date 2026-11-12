from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard import trigger
from trendpulse.dashboard.runtime import q, secret

ARCH_DOT = """
digraph G {
  rankdir=LR; bgcolor="transparent"; nodesep=0.35; ranksep=0.45;
  node [shape=box, style="rounded,filled", fontname="Inter", fontsize=11, color="#2b3a5e", fillcolor="#16213a",
        fontcolor="#e6eaf2", penwidth=1.2];
  edge [color="#6b7a93", arrowsize=0.6];
  gha [label="GitHub Actions\\ncron */30 + dispatch\\nconcurrency + DB lock", fillcolor="#1b2540"];
  yt [label="YouTube Data API v3\\nmostPopular charts\\nsearch.list (rotating)\\nvideos.list refresh"];
  q [label="Quota guard\\nretry / backoff"];
  b [label="BRONZE\\nraw JSON (gzip)\\nreplayable", fillcolor="#3a2a12", color="#c98500"];
  s [label="SILVER\\nconformed obs\\nschema + DQ checks", fillcolor="#1d2a44", color="#a8b3c7"];
  t [label="Topic engine\\ntaxonomy + TF-IDF\\nstable IDs"];
  dq [label="Quality gate\\n14 checks\\nquarantine"];
  g [label="GOLD\\nfacts (history)\\ndims + serving", fillcolor="#3a3212", color="#fab219"];
  pg [label="PostgreSQL\\n(Supabase)", shape=cylinder];
  ui [label="Streamlit dashboard\\nread-only role", fillcolor="#1b2540"];
  gha -> q -> yt -> b -> s -> t -> dq -> g -> pg -> ui;
}
"""

STAGE_COLORS = {"success": ui.STATUS["good"], "warning": ui.STATUS["warning"], "partial": ui.STATUS["warning"],
                "skipped": ui.MUTED, "failed": ui.STATUS["critical"]}


def _run_card(title: str, run: dict | None, tz: str) -> str:
    if not run:
        return ui.kpi(title, "—", "no run recorded")
    when = ui.fmt_ts(run["started_at"], tz)
    sub = (f"{ui.status_badge(run['status'])}&nbsp; {ui.esc(when.split(',')[0])} · {ui.esc(run['trigger'])} · "
           f"{(run.get('duration_seconds') or 0):.0f}s · {int(run.get('records_silver') or 0):,} records")
    return ui.kpi(title, when.split(", ")[-1], sub)


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    health = st.session_state.get("health")
    mode = st.session_state.get("data_mode")
    ui.hero("Data Engineering Control Center", "Pipeline health, lineage, data quality and API quota",
            ui.badge(mode.label if mode else "—", ui.STATUS["serious" if mode and mode.key == "sample" else "good"], "●"))
    if mode is None or mode.key in {"error"}:
        ui.banner(ui.esc(mode.detail if mode else "unavailable"), "critical", "✕")
        return
    if mode.key == "empty":
        ui.empty_state("No pipeline runs recorded", "Run <code>trendpulse run</code> to initialise the platform.")
        return
    if mode.key == "sample":
        ui.banner("Data source: <b>SAMPLE (offline API simulator)</b>. Execution metadata below is real — these "
                  "runs really executed — but the observations they processed are synthetic.", "serious", "!")

    h = health or {}
    quota_pct = h["quota_used_today"] / h["quota_budget"] * 100 if h.get("quota_budget") else 0
    c = st.columns(4)
    c[0].markdown(_run_card("Last attempted execution", h.get("last_attempt"), tz), unsafe_allow_html=True)
    c[1].markdown(_run_card("Last successful execution", h.get("last_success"), tz), unsafe_allow_html=True)
    c[2].markdown(ui.kpi("Data freshness", ui.fmt_age(h.get("freshness_minutes")),
                         ui.badge(h.get("freshness", "—"), ui.STATUS["good" if h.get("freshness") == "fresh"
                                                                     else "warning"], "●")
                         + f" target {h.get('interval_minutes')} min"), unsafe_allow_html=True)
    rate = h.get("success_rate_7d")
    c[3].markdown(ui.kpi("Success rate (7d)", f"{rate * 100:.1f}%" if rate is not None else "—",
                         f"{h.get('success_7d', 0)} of {h.get('runs_7d', 0)} executions"), unsafe_allow_html=True)
    st.write("")
    c = st.columns(4)
    c[0].markdown(ui.kpi("API quota today", f"{h.get('quota_used_today', 0):,} units",
                         f"{quota_pct:.0f}% of {h.get('quota_budget', 0):,} budget · resets "
                         f"{ui.fmt_ts(h.get('quota_day_resets_at'), tz).split(', ')[-1]}"), unsafe_allow_html=True)
    counts = q("warehouse_counts")
    c[1].markdown(ui.kpi("Bronze responses", ui.fmt_int(counts["bronze_api_responses"]),
                         "raw, within replay window" if counts["bronze_api_responses"] is not None
                         else "not readable by the dashboard role (by design)"),
                  unsafe_allow_html=True)
    c[2].markdown(ui.kpi("Gold history", ui.fmt_int(counts["fact_video_observations"]),
                         f"video obs · {ui.fmt_int(counts['fact_topic_observations'])} topic obs"),
                  unsafe_allow_html=True)
    c[3].markdown(ui.kpi("Quarantined records", ui.fmt_int(counts["quarantine_records"]), "never silently dropped"),
                  unsafe_allow_html=True)

    tabs = st.tabs(["Executions", "Stages", "Data quality", "Quarantine & review", "API quota", "Architecture",
                    "Failures & logs"])
    runs = q("recent_runs", 200)

    with tabs[0]:
        ui.section("Execution history", "Every run, including failed and skipped ones")
        if not runs.empty:
            r = runs.iloc[::-1]
            colors = [STAGE_COLORS.get("success" if s in {"success"} else "partial" if s in {"partial", "skipped",
                                                                                            "skipped_locked"}
                                       else "failed", ui.MUTED) for s in r.status]
            fig = go.Figure(go.Bar(x=r.started_at.dt.tz_convert(tz), y=r.duration_seconds, marker=dict(color=colors),
                                   customdata=r[["status", "records_silver", "quota_used", "trigger"]],
                                   hovertemplate="%{x|%d %b %H:%M}<br><b>%{customdata[0]}</b> · %{y:.1f}s"
                                                 "<br>%{customdata[1]} records · %{customdata[2]} quota units"
                                                 "<br>trigger: %{customdata[3]}<extra></extra>"))
            ui.plot(ui.style_fig(fig, 260, legend=False, ytitle="Duration (s)"), key="cc1")
            st.markdown(" ".join(f"{ui.status_badge(s)} {n}" for s, n in runs.status.value_counts().items()),
                        unsafe_allow_html=True)
            show = runs.assign(started=runs.started_at.dt.tz_convert(tz))
            st.dataframe(show[["started", "run_id", "status", "trigger", "data_source", "duration_seconds",
                               "records_bronze", "records_silver", "records_quarantined", "records_gold",
                               "quota_used", "dq_status", "gold_published", "error_message"]], hide_index=True,
                         height=360, column_config={
                             "started": st.column_config.DatetimeColumn(f"Started ({tz})", format="D MMM HH:mm"),
                             "run_id": st.column_config.TextColumn("Run ID", width="small"),
                             "duration_seconds": st.column_config.NumberColumn("Duration (s)", format="%.1f"),
                             "records_bronze": "Bronze", "records_silver": "Silver",
                             "records_quarantined": "Quarantined", "records_gold": "Gold rows",
                             "quota_used": "Quota", "gold_published": "Published", "error_message": "Error"})
        ui.section("Processing volume & throughput")
        tp = q("throughput", 7)
        if not tp.empty:
            fig = go.Figure()
            for i, (col, name) in enumerate([("records_silver", "Silver records"), ("records_quarantined",
                                                                                     "Quarantined")]):
                fig.add_trace(go.Scatter(x=tp.started_at.dt.tz_convert(tz), y=tp[col], name=name, mode="lines",
                                         line=dict(width=2, color=ui.SERIES[i])))
            ui.plot(ui.style_fig(fig, 240, ytitle="Records per run"), key="cc2")
            st.caption(f"Median throughput: {tp.records_per_second.median():,.0f} Silver records/second of run time.")

    with tabs[1]:
        latest = runs.iloc[0] if not runs.empty else None
        if latest is not None:
            ui.section(f"Latest execution — {latest.run_id[:8]}",
                       f"{ui.fmt_ts(latest.started_at, tz)} · status {latest.status}")
            st_df = q("stage_runs", latest.run_id)
            if not st_df.empty:
                d = st_df.iloc[::-1]
                fig = go.Figure(go.Bar(x=d.duration_ms, y=d.stage, orientation="h",
                                       marker=dict(color=[STAGE_COLORS.get(s, ui.MUTED) for s in d.status],
                                                   cornerradius=4),
                                       customdata=d[["status", "records_in", "records_out", "attempts", "error"]],
                                       hovertemplate="<b>%{y}</b> — %{customdata[0]}<br>%{x} ms · attempts "
                                                     "%{customdata[3]}<br>in %{customdata[1]} → out %{customdata[2]}"
                                                     "<br>%{customdata[4]}<extra></extra>"))
                ui.plot(ui.style_fig(fig, 480, legend=False, xtitle="Duration (ms)"), key="cc3")
                st.dataframe(st_df[["stage_order", "stage", "status", "duration_ms", "records_in", "records_out",
                                    "attempts", "error"]], hide_index=True,
                             column_config={"stage_order": "#", "error": st.column_config.TextColumn("Note / error",
                                                                                                    width="large")})
        ui.section("Bronze → Silver → Gold status", "Medallion layer row counts in the warehouse")
        layer = pd.DataFrame([
            ("Bronze", "bronze_api_responses", counts["bronze_api_responses"]),
            ("Silver", "silver_video_observations", counts["silver_video_observations"]),
            ("Silver", "quarantine_records", counts["quarantine_records"]),
            ("Gold", "fact_video_observations", counts["fact_video_observations"]),
            ("Gold", "fact_topic_observations", counts["fact_topic_observations"]),
            ("Gold", "dim_videos", counts["dim_videos"]), ("Gold", "dim_channels", counts["dim_channels"]),
            ("Gold", "dim_topics", counts["dim_topics"])], columns=["Layer", "Table", "Rows"])
        st.dataframe(layer, hide_index=True)
        ui.section("Stage duration statistics", "Last 48 executions")
        sd = q("stage_duration_stats", 48)
        if not sd.empty:
            st.dataframe(sd, hide_index=True, column_config={
                "stage_order": "#", "avg_ms": st.column_config.NumberColumn("Avg ms", format="%.0f"),
                "p95_ms": st.column_config.NumberColumn("p95 ms", format="%.0f")})

    with tabs[2]:
        dq = q("dq_results", None, 2)
        if dq.empty:
            st.info("No data-quality results yet.")
        else:
            latest_run = dq.sort_values("executed_at").run_id.iloc[-1]
            cur = dq[dq.run_id == latest_run].sort_values("check_name")
            ui.section("Latest data-quality audit", f"Run {latest_run[:8]} · {len(cur)} checks")
            st.markdown(" ".join(f"{ui.status_badge(s, ui.DQ_STATUS)} {n}" for s, n in cur.status.value_counts().items()),
                        unsafe_allow_html=True)
            cur = cur.assign(description=cur.details.map(lambda d: (json.loads(d) or {}).get("description", "")
                                                         if d else ""))
            st.dataframe(cur[["check_name", "severity", "status", "total_checked", "passed", "failed", "warnings",
                              "description", "details"]], hide_index=True, height=520,
                         column_config={"details": st.column_config.TextColumn("Details (JSON)", width="medium")})
            ui.section("Quality trend", "Checks in WARNING / FAIL per run, last 48h")
            tr = dq.groupby(["executed_at", "status"]).size().unstack(fill_value=0).reset_index()
            fig = go.Figure()
            for s_ in ("WARNING", "FAIL"):
                if s_ in tr:
                    fig.add_trace(go.Scatter(x=tr.executed_at.dt.tz_convert(tz), y=tr[s_], name=s_, mode="lines",
                                             line=dict(width=2, color=ui.STATUS["warning" if s_ == "WARNING"
                                                                                else "critical"])))
            ui.plot(ui.style_fig(fig, 220, ytitle="Checks"), key="cc4")

    with tabs[3]:
        ui.section("Quarantined records", "Rejected rows with their reasons and raw payload")
        qr = q("quarantine", 200)
        st.dataframe(qr, hide_index=True, height=320, column_config={
            "payload": st.column_config.TextColumn("Raw payload", width="large")}) if not qr.empty \
            else st.info("No quarantined records.")
        ui.section("Topic review queue", "Low-confidence or unclassified assignments awaiting taxonomy tuning")
        rv = q("review_queue", 200)
        st.dataframe(rv, hide_index=True, height=320) if not rv.empty else st.info("Review queue is empty.")

    with tabs[4]:
        qu = q("quota_usage", 14)
        ui.section("Daily API quota consumption", "Units per Pacific-time quota day, by endpoint")
        st.progress(min(1.0, quota_pct / 100), text=f"Today: {h.get('quota_used_today', 0):,} / "
                                                     f"{h.get('quota_budget', 0):,} budget "
                                                     f"(hard limit {h.get('quota_limit', 0):,})")
        if not qu.empty:
            fig = go.Figure()
            for i, (ep, grp) in enumerate(qu.groupby("endpoint")):
                fig.add_trace(go.Bar(x=grp.quota_day.astype(str), y=grp.units, name=ep,
                                     marker=dict(color=ui.SERIES[i], line=dict(width=2, color=ui.SURFACE)),
                                     hovertemplate="%{x}<br>" + ep + ": %{y} units<extra></extra>"))
            fig.add_hline(y=h.get("quota_budget", 0), line=dict(color=ui.STATUS["warning"], width=1, dash="dot"),
                          annotation_text="budget", annotation_font_color=ui.TEXT_2)
            fig.update_layout(barmode="stack")
            ui.plot(ui.style_fig(fig, 300, ytitle="Quota units"), key="cc5")
            st.dataframe(qu, hide_index=True)

    with tabs[5]:
        ui.section("Pipeline architecture")
        st.graphviz_chart(ARCH_DOT, width="stretch")
        st.markdown("""
| Stage | What happens |
|---|---|
| 1–3 | start run, load + validate config, migrations, seed the quota tracker from today's API log |
| 4–6 | regional charts (1 unit/page) → rotating topic search (100/units, every 3h) → `videos.list` refresh of tracked videos |
| 7 | Bronze verified (each response was persisted on arrival, gzip JSON) |
| 8–9 | schema-drift check, Bronze → Silver parse, record-level DQ, dedupe |
| 10 | topic extraction: taxonomy classifier + TF-IDF keyphrase discovery, stable IDs |
| 11–12 | dataset-level DQ + audit table; failed rows to quarantine; **gate** |
| 13–14 | one transaction: dims, append-only facts, serving tables, publication record |
| 15–17 | run metadata + retention, status file / job summary, optional alerts |
""")

    with tabs[6]:
        ui.section("Failed / degraded executions")
        bad = runs[runs.status.isin(["failed", "dq_failed", "abandoned", "partial"])] if not runs.empty else runs
        st.dataframe(bad[["started_at", "run_id", "status", "error_message"]], hide_index=True) if not bad.empty \
            else st.success("No failed executions in recent history.")
        ui.section("API errors", "Non-2xx responses and transport failures (retries included)")
        err = q("api_errors", 50)
        st.dataframe(err, hide_index=True) if not err.empty else st.info("No API errors logged.")

    token, repo, phrase = (secret("TRENDPULSE_GITHUB_DISPATCH_TOKEN"), secret("TRENDPULSE_GITHUB_REPO"),
                           secret("TRENDPULSE_OPERATOR_PASSPHRASE"))
    ui.section("Manual ingestion")
    if trigger.enabled(token, repo, phrase):
        with st.form("dispatch"):
            given = st.text_input("Operator passphrase", type="password")
            if st.form_submit_button("Trigger ingestion workflow"):
                if trigger.check_passphrase(given, phrase):
                    ok, msg = trigger.dispatch(token, repo)
                    (st.success if ok else st.error)(msg)
                else:
                    st.error("Invalid passphrase.")
    else:
        st.caption("Disabled on this deployment. Public visitors cannot start ingestion; operators trigger the "
                   "`Scheduled ingestion` workflow from GitHub Actions (workflow_dispatch).")
