from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.runtime import q

WINDOWS = ["30m", "1h", "6h", "24h", "7d", "30d"]
STAGES = ["emerging", "growing", "peaking", "stable", "declining", "baseline"]
GROWTH = ["rising", "stable", "falling", "new", "vanished", "no_history"]


def _growth_bar(df: pd.DataFrame, title_col: str = "topic_name") -> go.Figure:
    d = df.iloc[::-1]
    colors = [ui.POS if v >= 0 else ui.NEG for v in d.score_growth_pct]
    fig = go.Figure(go.Bar(
        x=d.score_growth_pct, y=d[title_col], orientation="h", marker=dict(color=colors, cornerradius=4),
        customdata=d[["score_prev", "score_now", "velocity"]],
        hovertemplate="<b>%{y}</b><br>%{x:+.1f}%  (score %{customdata[0]:.1f} → %{customdata[1]:.1f})"
                      "<br>velocity %{customdata[2]:+.2f} pts/h<extra></extra>"))
    return ui.style_fig(fig, max(260, 26 * len(d) + 60), legend=False, xtitle="Popularity-score growth (%)")


def render() -> None:
    ui.hero("Trend Discovery", "Which topics are emerging, accelerating, peaking or fading")
    if not guard():
        return
    regions = ["GLOBAL"] + list(q("regions").region_code)
    f1, f2, f3, f4, f5 = st.columns([1.1, 1.6, 1.4, 1.6, 1.6])
    region = f1.selectbox("Country", regions, key="disc_region")
    trending = q("trending", region)
    cats = sorted(trending.category.dropna().unique()) if not trending.empty else []
    sel_cats = f2.multiselect("Category", cats, key="disc_cats", placeholder="All categories")
    window = f3.segmented_control("Time window", WINDOWS, default="24h", key="disc_window") or "24h"
    sel_stages = f4.multiselect("Lifecycle", STAGES, key="disc_stage", placeholder="All stages")
    sel_growth = f5.multiselect("Growth status", GROWTH, key="disc_growth", placeholder="All")

    growth = q("growth", region, window)
    if trending.empty:
        ui.empty_state("No ranked topics for this region", "Try another country or wait for the next run.")
        return
    cur = trending.merge(growth[["topic_id", "score_growth_pct", "video_count_growth_pct", "vph_growth_pct",
                                 "velocity", "acceleration", "growth_status", "score_prev", "score_now",
                                 "compare_observed_at"]], on="topic_id", how="left")
    if sel_cats:
        cur = cur[cur.category.isin(sel_cats)]
        growth = growth[growth.category.isin(sel_cats)]
    if sel_stages:
        cur = cur[cur.lifecycle_stage.isin(sel_stages)]
    if sel_growth:
        cur = cur[cur.growth_status.isin(sel_growth)]
        growth = growth[growth.growth_status.isin(sel_growth)]

    compare = growth.compare_observed_at.dropna()
    if compare.empty:
        ui.banner(f"No snapshot ~{window} old exists yet, so growth for this window can't be computed. "
                  "It becomes available once the platform has collected that much history.", "warning", "i")
    else:
        st.markdown(f"<div class='tp-caption'>Comparing the latest snapshot with the one closest to {window} "
                    f"earlier ({ui.fmt_ts(compare.iloc[0], st.session_state.get('tz', 'UTC'))}).</div>",
                    unsafe_allow_html=True)

    stage_counts = cur.lifecycle_stage.value_counts()
    ui.kpi_row([(s.capitalize(), int(stage_counts.get(s, 0)), ui.lifecycle_badge(s)) for s in
                ["emerging", "growing", "peaking", "stable", "declining"]])

    has = cur.dropna(subset=["score_growth_pct"])
    left, right = st.columns(2)
    with left:
        ui.section("Fastest-growing topics", f"Largest popularity-score increase over {window}")
        top = has[has.score_growth_pct > 0].sort_values("score_growth_pct", ascending=False).head(12)
        ui.plot(_growth_bar(top), key="grow") if not top.empty else ui.empty_state("Nothing rising", "")
    with right:
        ui.section("Topics losing popularity", f"Largest decrease over {window} (incl. topics no longer observed)")
        g2 = growth.dropna(subset=["score_growth_pct"])
        g2 = g2[g2.score_growth_pct < 0].sort_values("score_growth_pct").head(12)
        g2 = g2.assign(score_now=g2.score_now.fillna(0))
        ui.plot(_growth_bar(g2), key="fall") if not g2.empty else ui.empty_state("Nothing falling", "")

    ui.section("Emerging & recently peaked")
    e1, e2 = st.columns(2)
    cols = {"topic_name": "Topic", "category": "Category", "popularity_score": st.column_config.NumberColumn(
        "Score", format="%.1f"), "video_count": "Videos", "channel_count": "Channels",
            "first_observed_at": st.column_config.DatetimeColumn("First seen", format="D MMM, HH:mm")}
    with e1:
        st.caption("Emerging — first observed within the emerging window")
        em = cur[cur.lifecycle_stage == "emerging"].sort_values("popularity_score", ascending=False)
        st.dataframe(em[list(cols)], column_config=cols, hide_index=True, height=260) if not em.empty \
            else st.info("No emerging topics in this view.")
    with e2:
        st.caption("Peaking — near their 7-day high after a real rise, growth flattening")
        pk = cur[cur.lifecycle_stage == "peaking"].sort_values("popularity_score", ascending=False)
        st.dataframe(pk[list(cols)], column_config=cols, hide_index=True, height=260) if not pk.empty \
            else st.info("No peaking topics in this view.")

    ui.section("Popularity rankings", "Rank change: positive = moved up. Velocity in score points per hour; "
                                      "acceleration in points per hour².")
    table = cur.sort_values("popularity_rank")[[
        "popularity_rank", "rank_change_1h", "rank_change_24h", "topic_name", "category", "lifecycle_stage",
        "popularity_score", "score_growth_pct", "video_count_growth_pct", "velocity", "acceleration",
        "video_count", "channel_count", "views_per_hour", "consecutive_observations", "topic_kind"]]
    st.dataframe(table, hide_index=True, height=520, column_config={
        "popularity_rank": st.column_config.NumberColumn("#", width="small"),
        "rank_change_1h": st.column_config.NumberColumn("Δ rank 1h", format="%+d"),
        "rank_change_24h": st.column_config.NumberColumn("Δ rank 24h", format="%+d"),
        "topic_name": "Topic", "category": "Category", "lifecycle_stage": "Lifecycle",
        "popularity_score": st.column_config.ProgressColumn("Score", format="%.1f", min_value=0,
                                                            max_value=float(cur.popularity_score.max() or 1)),
        "score_growth_pct": st.column_config.NumberColumn(f"Score growth {window}", format="%+.1f%%"),
        "video_count_growth_pct": st.column_config.NumberColumn(f"Video-count growth {window}", format="%+.1f%%"),
        "velocity": st.column_config.NumberColumn("Velocity", format="%+.2f"),
        "acceleration": st.column_config.NumberColumn("Acceleration", format="%+.3f"),
        "video_count": "Videos", "channel_count": "Channels",
        "views_per_hour": st.column_config.NumberColumn("Views/hour", format="compact"),
        "consecutive_observations": st.column_config.NumberColumn("Consecutive obs."),
        "topic_kind": "Kind",
    })
