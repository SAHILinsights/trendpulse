"""Module 3 — Data Lakehouse Explorer: catalog of Bronze / Silver / Gold datasets."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import engineering_guard
from trendpulse.dashboard.ops import table_schema
from trendpulse.dashboard.runtime import qo

LAYERS = ["Bronze", "Silver", "Gold", "Operational"]
PAGE = 25


def render() -> None:
    tz = st.session_state.get("tz", "UTC")
    if not engineering_guard("Data Lakehouse", "Catalog of every warehouse dataset — schema, volume, freshness, "
                                               "partitioning and bounded sample previews"):
        return
    cat = qo("dataset_catalog")
    if cat.empty:
        ui.empty_state("Catalog empty", "Run migrations first.")
        return
    by = cat.groupby("layer_key").agg(tables=("table", "count"), rows=("rows", "sum"), size=("size_bytes", "sum"))
    ui.strip([(k, ui.fmt_int(by.rows.get(k, 0)) + " rows",
               f"{int(by.tables.get(k, 0))} tables · {ui.fmt_bytes(by['size'].get(k)) if by['size'].get(k) else 'size n/a'}")
              for k in LAYERS if k in by.index])
    f1, f2 = st.columns([2, 1])
    search = f1.text_input("Search datasets", placeholder="table name or purpose…", key="lh_q")
    layers = f2.multiselect("Layer", LAYERS, default=["Bronze", "Silver", "Gold"], key="lh_layers")
    view = cat[cat.layer_key.isin(layers or LAYERS)]
    if search:
        s = search.lower()
        view = view[view.table.str.contains(s, regex=False) | view.purpose.str.lower().str.contains(s, regex=False)]
    show = view.assign(size=view.size_bytes.map(ui.fmt_bytes),
                       rows_label=[f"{ui.fmt_int(r)}{'' if e else ' (est.)'}" if r is not None else "n/a"
                                   for r, e in zip(view.rows, view.rows_exact, strict=True)],
                       last_updated=pd.to_datetime(view.last_updated, utc=True).dt.tz_convert(tz))
    ev = st.dataframe(show[["layer", "table", "rows_label", "size", "columns", "last_updated", "partitioning",
                            "purpose"]], hide_index=True, height=380, on_select="rerun",
                      selection_mode="single-row", key="lh_tbl", column_config={
                          "layer": "Layer", "table": st.column_config.TextColumn("Dataset"), "rows_label": "Rows",
                          "size": "Size", "columns": "Cols",
                          "last_updated": st.column_config.DatetimeColumn(f"Last updated ({tz})",
                                                                           format="D MMM HH:mm"),
                          "partitioning": st.column_config.TextColumn("Partitioning", width="medium"),
                          "purpose": st.column_config.TextColumn("Purpose", width="large")})
    rows = ev.selection.rows if ev is not None and hasattr(ev, "selection") else []
    names = list(show.table)
    if not names:
        return
    if rows:
        st.session_state["lh_sel"] = names[rows[0]]
    if st.session_state.get("lh_sel") not in names:
        st.session_state["lh_sel"] = names[0]
    current = st.selectbox("Dataset", names, key="lh_sel")
    meta = cat.set_index("table").loc[current]
    sch = table_schema(current)
    st.markdown(f"<div style='display:flex;gap:10px;align-items:center;margin-top:6px'>"
                f"<span style='font-weight:650;font-size:1.02rem' class='tp-mono'>{ui.esc(current)}</span>"
                f"{ui.badge(meta.layer, ui.ACCENT)}<span class='tp-caption'>{ui.esc(meta.purpose)}</span></div>",
                unsafe_allow_html=True)
    tabs = st.tabs(["Schema", "Sample records", "Partitions", "Lineage"])
    with tabs[0]:
        ui.kv([("Primary key", ", ".join(sch["primary_key"])), ("Unique", "; ".join(sch["unique"]) or "—"),
               ("Indexes", "; ".join(sch["indexes"]) or "—"), ("Partitioning", meta.partitioning),
               ("Rows", f"{ui.fmt_int(meta.rows)}{'' if meta.rows_exact else ' (planner estimate)'}"),
               ("Size", ui.fmt_bytes(meta.size_bytes))])
        st.dataframe(sch["columns"], hide_index=True, height=min(38 * len(sch["columns"]) + 40, 560),
                     column_config={"exposed": st.column_config.CheckboxColumn(
                         "Exposed", help="Columns hidden from the control plane (e.g. raw payloads)")})
    with tabs[1]:
        c1, c2 = st.columns([1.5, 1])
        run_filter = c1.text_input("Filter by run_id (optional)", key="lh_run",
                                   placeholder="full run UUID") if "run_id" in set(sch["columns"].column) else None
        total = meta.rows or 0
        offset = ui.pager(int(total if not run_filter else PAGE * 40), PAGE, key=f"lh_page_{current}")
        sample = qo("table_sample", current, PAGE, offset, run_filter or None)
        st.caption(f"Newest first · {PAGE} rows per page · hidden columns never selected · long text truncated")
        st.dataframe(sample, hide_index=True, height=420)
    with tabs[2]:
        if current == "silver_video_observations":
            st.dataframe(qo("silver_partitions", 48), hide_index=True, height=360)
        elif current == "bronze_api_responses":
            st.caption("Responses by source endpoint and discovery method (metadata only; payload not exposed)")
            st.dataframe(qo("bronze_by_source"), hide_index=True)
        rp = qo("rows_per_run", current, 24)
        if rp is not None and not rp.empty:
            st.caption("Rows per run (latest 24 runs present in this table)")
            st.dataframe(rp, hide_index=True, height=300)
        else:
            st.caption(f"Logical partitioning: {meta.partitioning}")
    with tabs[3]:
        from trendpulse.dashboard.ops import lineage_for
        lin = lineage_for(current)
        c1, c2 = st.columns(2)
        c1.markdown("**Upstream**<br>" + ("<br>".join(ui.mono(x) for x in lin["upstream"]) or "—"),
                    unsafe_allow_html=True)
        c2.markdown("**Downstream**<br>" + ("<br>".join(ui.mono(x) for x in lin["downstream"]) or "—"),
                    unsafe_allow_html=True)
        st.caption("Derived from the declared stage inputs/outputs (orchestration/dag.py). Operational tables have "
                   "no data lineage edges.")
