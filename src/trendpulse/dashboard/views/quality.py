"""Module 4 — Data Quality & Governance: the 14 checks, gate decisions, quarantine, schema, freshness."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import engineering_guard
from trendpulse.dashboard.runtime import qo
from trendpulse.quality.framework import GATE_POLICY, check_catalog

STATUS_Z = {"PASS": 0, "WARNING": 1, "FAIL": 2}
PAGE = 25


def _gate(status: str | None) -> None:
    if not status:
        return
    role = {"PASS": "good", "WARNING": "warning", "FAIL": "critical"}[status]
    ui.banner(f"<b>Quality gate: {status}</b> — {ui.esc(GATE_POLICY[status])}", role,
              {"PASS": "✓", "WARNING": "!", "FAIL": "✕"}[status])


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    if not engineering_guard("Data Quality & Governance", "Fourteen validation checks, gate decisions and every "
                                                          "rejected record — read from dq_results and "
                                                          "quarantine_records"):
        return
    catalog = pd.DataFrame(check_catalog())
    latest = qo("latest_dq_results")
    tabs = st.tabs(["Overview", "Validation history", "Failed checks", "Rejected records & quarantine",
                    "Schema validation", "Data freshness", "Audit history", "Check catalog"])

    with tabs[0]:
        if latest.empty:
            ui.empty_state("No quality audit yet", "Audit rows appear after the first execution reaches the "
                                                   "data_quality stage.")
        else:
            overall = "FAIL" if (latest.status == "FAIL").any() else \
                "WARNING" if (latest.status == "WARNING").any() else "PASS"
            rid = latest.run_id.iloc[0]
            run = qo("run_row", rid) or {}
            _gate(overall)
            ui.strip([
                ("Audited run", f"<span class='tp-mono'>{rid[:8]}</span>",
                 ui.fmt_ts(latest.executed_at.iloc[0], tz)),
                ("Checks", f"{len(latest)}", " · ".join(f"{s} {int((latest.status == s).sum())}"
                                                       for s in ("PASS", "WARNING", "FAIL"))),
                ("Records evaluated", ui.fmt_int(latest.total_checked.max()), "largest check population"),
                ("Rejected (quarantined)", ui.fmt_int(run.get("records_quarantined")), "this run"),
                ("Gold publication", "published" if run.get("gold_published") else "blocked / not reached",
                 ui.esc(run.get("status", ""))),
            ])
            m = catalog.merge(latest, left_on="name", right_on="check_name", how="left", suffixes=("", "_run"))
            m["executed_at"] = pd.to_datetime(m.executed_at, utc=True).dt.tz_convert(tz)
            st.dataframe(m[["name", "level", "severity_run", "status", "total_checked", "passed", "failed",
                            "warnings", "action", "stage", "executed_at", "description"]], hide_index=True,
                         height=545, column_config={
                             "name": "Check", "level": "Level", "severity_run": "Severity", "status": "Status",
                             "total_checked": "Evaluated", "passed": "Passed", "failed": "Failed",
                             "warnings": "Warnings", "action": "On failure", "stage": "Runs in",
                             "executed_at": st.column_config.DatetimeColumn("Executed", format="D MMM HH:mm:ss"),
                             "description": st.column_config.TextColumn("Rule", width="large")})

    with tabs[1]:
        hist = qo("dq_history", 7)
        if hist.empty:
            st.info("No audit history in the last 7 days.")
        else:
            runs_order = hist.drop_duplicates("run_id").sort_values("executed_at").tail(60)
            h = hist[hist.run_id.isin(runs_order.run_id)]
            piv = h.pivot_table(index="check_name", columns="run_id", values="status", aggfunc="first")
            piv = piv[list(runs_order.run_id)]
            z = piv.map(lambda v: STATUS_Z.get(v))
            xs = [pd.Timestamp(t).tz_convert(tz).strftime("%d %b %H:%M") for t in runs_order.executed_at]
            fig = go.Figure(go.Heatmap(
                z=z.values, x=xs, y=list(piv.index), zmin=0, zmax=2, xgap=1, ygap=1, showscale=False,
                colorscale=[[0, ui.OK], [0.33, ui.OK], [0.34, ui.WARN], [0.66, ui.WARN], [0.67, ui.FAIL], [1, ui.FAIL]],
                customdata=piv.values, hovertemplate="%{y}<br>%{x}: <b>%{customdata}</b><extra></extra>"))
            ui.plot(ui.style_fig(fig, 30 * len(piv) + 90, legend=False), key="q_hist")
            st.markdown(f"<div class='tp-caption'>{ui.badge('PASS', ui.OK)} {ui.badge('WARNING', ui.WARN)} "
                        f"{ui.badge('FAIL', ui.FAIL_TEXT)} — one column per audited run (latest 60, 7 days)</div>",
                        unsafe_allow_html=True)
            by = qo("dq_status_by_run", 7)
            gate = by.groupby("run_id").status.agg(lambda s: "FAIL" if "FAIL" in set(s) else
                                                   "WARNING" if "WARNING" in set(s) else "PASS")
            st.markdown("Gate decisions (7 d): " + " ".join(f"{ui.status_badge(s, ui.DQ_STATUS)} {n}"
                                                          for s, n in gate.value_counts().items()),
                        unsafe_allow_html=True)

    with tabs[2]:
        fc = qo("failed_checks", 7, 300)
        if fc.empty:
            st.success("No WARNING or FAIL results in the last 7 days.")
        else:
            fc = fc.assign(executed_at=fc.executed_at.dt.tz_convert(tz), run=fc.run_id.str[:8])
            st.dataframe(fc[["executed_at", "run", "check_name", "severity", "status", "total_checked", "failed",
                             "warnings", "details"]], hide_index=True, height=420, column_config={
                "executed_at": st.column_config.DatetimeColumn("Executed", format="D MMM HH:mm"),
                "details": st.column_config.TextColumn("Details (JSON)", width="large")})

    with tabs[3]:
        c1, c2 = st.columns(2)
        run_f = c1.text_input("Run ID", key="qq_run", placeholder="filter by full run UUID")
        check_f = c2.selectbox("Rule", ["(all)"] + [c for c in catalog.name if catalog.set_index("name")
                                                    .loc[c, "action"] == "quarantine"], key="qq_check")
        chk = None if check_f == "(all)" else check_f
        total = qo("quarantine_count", run_f or None, chk)
        offset = ui.pager(total, PAGE, "qq_page")
        page = qo("quarantine_page", run_f or None, chk, PAGE, offset)
        if page.empty:
            st.info("No quarantined records match.")
        else:
            page = page.assign(quarantined_at=page.quarantined_at.dt.tz_convert(tz))
            ev = st.dataframe(page, hide_index=True, height=330, on_select="rerun", selection_mode="single-row",
                              key="qq_tbl", column_config={
                                  "quarantined_at": st.column_config.DatetimeColumn("Quarantined", format="D MMM HH:mm"),
                                  "reason": "Failed rule(s)", "record_key": st.column_config.TextColumn(
                                      "Record key (video|region|method|query)", width="large")})
            sel = ev.selection.rows if ev is not None and hasattr(ev, "selection") else []
            rid = int(page.iloc[sel[0]].id) if sel else int(page.iloc[0].id)
            rec = qo("quarantine_record", rid)
            if rec:
                ui.section(f"Record #{rid}", "Why it was rejected")
                rules = [r.strip() for r in rec["reason"].split(",")]
                info = catalog.set_index("name")
                c1, c2 = st.columns([1, 1.2])
                with c1:
                    ui.kv([("Run", ui.Raw(ui.mono(rec["run_id"]))), ("Stage", rec["stage"]),
                           ("Record key", ui.Raw(ui.mono(rec["record_key"]))),
                           ("Quarantined", ui.fmt_ts(rec["quarantined_at"], tz))])
                    for r in rules:
                        if r in info.index:
                            ui.banner(f"<b>{ui.esc(r)}</b> · {ui.esc(info.loc[r, 'severity'])} — "
                                      f"{ui.esc(info.loc[r, 'description'])}", "critical", "✕")
                with c2:
                    st.caption("Raw item as received from the API (truncated at 8 KB)")
                    if rec.get("payload_json") is not None:
                        st.json(rec["payload_json"], expanded=2)
                    else:
                        st.code(rec.get("payload") or "", language="json")

    with tabs[4]:
        sd = qo("schema_drift_history", 48)
        st.caption("schema_drift: required paths id, snippet.title, snippet.channelId, snippet.publishedAt, "
                   "statistics.viewCount; any new top-level field is reported.")
        if sd.empty:
            st.info("No schema validation results yet.")
        else:
            st.dataframe(sd.assign(executed_at=sd.executed_at.dt.tz_convert(tz)), hide_index=True, height=400,
                         column_config={"executed_at": st.column_config.DatetimeColumn("Executed", format="D MMM HH:mm"),
                                        "total_checked": "Items profiled"})

    with tabs[5]:
        fr = qo("freshness_by_region")
        st.dataframe(fr, hide_index=True, column_config={
            "age_minutes": st.column_config.NumberColumn("Age (min)", format="%.0f")})
        hist = qo("dq_history", 2)
        stale = hist[hist.check_name == "stale_data"] if not hist.empty else hist
        if not stale.empty:
            st.caption("stale_data check over the last 48 h")
            st.dataframe(stale.sort_values("executed_at", ascending=False).head(48), hide_index=True, height=300)

    with tabs[6]:
        hist = qo("dq_history", 30)
        if hist.empty:
            st.info("No audit records.")
        else:
            st.caption(f"{len(hist):,} audit rows in the last 30 days (newest first, first 500 shown)")
            st.dataframe(hist.sort_values("executed_at", ascending=False).head(500), hide_index=True, height=420)

    with tabs[7]:
        st.dataframe(catalog, hide_index=True, column_config={
            "description": st.column_config.TextColumn("Rule", width="large")})
        st.markdown("\n".join(f"- **{k}** — {v}" for k, v in GATE_POLICY.items()))
        st.caption("Thresholds and per-check overrides live in config/pipeline.yaml → quality (changed via git "
                   "review, never from the UI).")
