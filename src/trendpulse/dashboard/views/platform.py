"""Module 1 — Platform Overview: is the platform healthy right now?"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import engineering_guard
from trendpulse.dashboard.ops import health_status
from trendpulse.dashboard.runtime import qo
from trendpulse.settings import get_settings


def _run_cell(run: dict | None, tz: str) -> tuple[str, str]:
    if not run:
        return "—", "none recorded"
    when = ui.fmt_ts(run["started_at"], tz)
    return when.split(", ")[-1], f"{ui.status_badge(run['status'])} {ui.esc(when.split(',')[0])} · " \
                                 f"<span class='tp-mono'>{run['run_id'][:8]}</span>"


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    health = st.session_state.get("health")
    dq = qo("latest_dq_results")
    dq_overall = None if dq.empty else ("FAIL" if (dq.status == "FAIL").any() else
                                        "WARNING" if (dq.status == "WARNING").any() else "PASS")
    hs = health_status(health, dq_overall, get_settings())
    if not engineering_guard("Platform Overview", "Operational health of ingestion, processing and publication",
                             right_html=ui.health_badge(hs.status)):
        return
    h = health or {}
    last_ok_v, last_ok_s = _run_cell(h.get("last_success"), tz)
    last_v, last_s = _run_cell(h.get("last_attempt"), tz)
    tot = qo("totals")
    rate = h.get("success_rate_7d")
    quota_pct = h.get("quota_used_today", 0) / h["quota_budget"] * 100 if h.get("quota_budget") else 0
    ui.strip([
        ("Platform health", ui.health_badge(hs.status), ui.esc(hs.reasons[0])[:150]),
        ("Last successful run", last_ok_v, last_ok_s),
        ("Last attempted run", last_v, last_s),
        ("Success rate · 7 d", f"{rate:.0%}" if rate is not None else "—",
         f"{h.get('success_7d', 0)} of {h.get('runs_7d', 0)} executions"),
    ])
    st.write("")
    ui.strip([
        ("Source freshness", ui.fmt_age(h.get("freshness_minutes")),
         f"{ui.esc(h.get('freshness', '—'))} · target {h.get('interval_minutes')} min"),
        ("Records processed", ui.fmt_int(tot["silver"]), f"valid Silver rows · {tot['runs']} runs retained"),
        ("Rejected records", ui.fmt_int(tot["rejected"]),
         f"{tot['rejected'] / max(1, tot['silver'] + tot['rejected']):.2%} of candidates · quarantined"),
        ("Data quality", ui.status_badge(dq_overall, ui.DQ_STATUS) if dq_overall else "—",
         f"latest audit · {len(dq)} checks" if not dq.empty else "no audit yet"),
        ("API quota today", f"{h.get('quota_used_today', 0):,}",
         f"{quota_pct:.0f}% of {h.get('quota_budget', 0):,} budget"),
    ])

    left, right = st.columns([1.55, 1])
    with left:
        ui.section("Recent execution activity", "Last 48 executions — bar height = wall-clock duration")
        runs = qo("runs", None, None, None, 48)
        if runs.empty:
            ui.empty_state("No executions", "")
        else:
            r = runs.iloc[::-1]
            color = {"success": ui.OK, "partial": ui.WARN, "skipped": ui.IDLE, "dq_failed": ui.FAIL,
                     "failed": ui.FAIL, "abandoned": "#FB923C", "running": ui.RUNNING}
            fig = go.Figure(go.Bar(
                x=r.started_at.dt.tz_convert(tz), y=r.duration_seconds.fillna(0),
                marker=dict(color=[color.get(s, ui.IDLE) for s in r.status]),
                customdata=r[["run_id", "status", "trigger", "records_silver", "records_quarantined"]],
                hovertemplate="%{x|%d %b %H:%M}<br><b>%{customdata[1]}</b> · %{y:.1f}s · %{customdata[2]}"
                              "<br>%{customdata[3]} valid · %{customdata[4]} rejected<br>%{customdata[0]}<extra></extra>"))
            ui.plot(ui.style_fig(fig, 200, legend=False, ytitle="Duration (s)"), key="pf_runs")
            st.markdown(" ".join(f"{ui.status_badge(s)} {n}" for s, n in runs.status.value_counts().items()),
                        unsafe_allow_html=True)
            show = runs.head(10).assign(started=runs.started_at.head(10).dt.tz_convert(tz),
                                        run=runs.run_id.head(10).str[:8])
            st.dataframe(show[["started", "run", "status", "trigger", "duration_seconds", "records_silver",
                               "records_quarantined", "dq_status", "failed_stage"]], hide_index=True, height=250,
                         column_config={
                             "started": st.column_config.DatetimeColumn(f"Started ({tz})", format="D MMM HH:mm:ss"),
                             "duration_seconds": st.column_config.NumberColumn("Duration s", format="%.1f"),
                             "records_silver": "Valid", "records_quarantined": "Rejected", "dq_status": "DQ",
                             "failed_stage": "Failed stage"})
    with right:
        ui.section("Active conditions", "Derived from run, audit, quota and lock metadata")
        conds = qo("active_conditions", h)
        if not conds:
            st.markdown(f"<div class='tp-card'>{ui.badge('none', ui.OK, '✓')} No active conditions.</div>",
                        unsafe_allow_html=True)
        else:
            html = "".join(f"<div class='tp-cond'>{ui.severity_badge(c['severity'])}<div><b>{ui.esc(c['title'])}"
                           f"</b><div class='tp-caption'>{ui.esc(c['detail'])}</div></div></div>" for c in conds)
            st.markdown(f"<div class='tp-card' style='padding:2px 6px'>{html}</div>", unsafe_allow_html=True)
        ui.section("Medallion state")
        cat = qo("dataset_catalog")
        if not cat.empty:
            agg = cat.groupby("layer_key").agg(tables=("table", "count"), rows=("rows", "sum"),
                                               size=("size_bytes", "sum"), updated=("last_updated", "max"))
            agg = agg.reindex([k for k in ["Bronze", "Silver", "Gold", "Operational"] if k in agg.index])
            agg = agg.reset_index().assign(size=lambda d: d["size"].map(ui.fmt_bytes))
            st.dataframe(agg, hide_index=True, column_config={
                "layer_key": "Layer", "tables": "Tables", "rows": st.column_config.NumberColumn("Rows", format="%d"),
                "size": "Size", "updated": st.column_config.DatetimeColumn("Last write", format="D MMM HH:mm")})
        ui.section("Source freshness by region")
        fr = qo("freshness_by_region")
        if not fr.empty:
            st.dataframe(fr[["region_code", "country_name", "freshness", "age_minutes", "is_active"]],
                         hide_index=True, height=230, column_config={
                             "region_code": "Region", "country_name": "Country", "freshness": "State",
                             "age_minutes": st.column_config.NumberColumn("Age (min)", format="%.0f"),
                             "is_active": "Enabled"})
