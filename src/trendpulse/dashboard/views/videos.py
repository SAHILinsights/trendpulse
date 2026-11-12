from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.runtime import q


def render(embedded: bool = False) -> None:
    tz = st.session_state.get("tz", "UTC")
    if not embedded:
        ui.hero("Video Explorer", "Search every video the pipeline has observed")
        if not guard():
            return
    c1, c2, c3, c4 = st.columns([2.2, 1, 1.4, 1])
    search = c1.text_input("Search title or channel", key="vx_q", placeholder="e.g. trailer, cricket, AI…")
    region = c2.selectbox("Country", ["GLOBAL"] + list(q("regions").region_code), key="vx_region")
    yt_cats = list(q("video_categories"))
    category = c3.selectbox("YouTube category", ["All"] + yt_cats, key="vx_cat")
    days = c4.selectbox("Observed within", [1, 7, 30], index=1, format_func=lambda d: f"{d} days", key="vx_days")
    df = q("videos", search or None, region, None if category == "All" else category, days, 500)
    if df.empty:
        ui.empty_state("No videos match", "Adjust the filters.")
        return
    st.caption(f"{len(df)} videos (max 500 shown) · timestamps in {tz}")
    df = df.assign(url="https://www.youtube.com/watch?v=" + df.video_id,
                   first_observed_at=df.first_observed_at.dt.tz_convert(tz),
                   last_observed_at=df.last_observed_at.dt.tz_convert(tz),
                   published_at=df.published_at.dt.tz_convert(tz))
    event = st.dataframe(
        df[["thumbnail_url", "title", "channel_title", "regions", "category_name", "latest_view_count",
            "latest_like_count", "latest_comment_count", "best_chart_rank", "published_at", "first_observed_at",
            "last_observed_at", "url"]],
        hide_index=True, height=520, row_height=56, on_select="rerun", selection_mode="single-row", key="vx_tbl",
        column_config={
            "thumbnail_url": st.column_config.ImageColumn("", width="small"),
            "title": st.column_config.TextColumn("Title", width="large"), "channel_title": "Channel",
            "regions": "Countries", "category_name": "Category",
            "latest_view_count": st.column_config.NumberColumn("Views", format="compact"),
            "latest_like_count": st.column_config.NumberColumn("Likes", format="compact"),
            "latest_comment_count": st.column_config.NumberColumn("Comments", format="compact"),
            "best_chart_rank": st.column_config.NumberColumn("Best chart rank"),
            "published_at": st.column_config.DatetimeColumn("Published", format="D MMM YYYY"),
            "first_observed_at": st.column_config.DatetimeColumn("First observed", format="D MMM HH:mm"),
            "last_observed_at": st.column_config.DatetimeColumn("Latest observed", format="D MMM HH:mm"),
            "url": st.column_config.LinkColumn("Open", display_text="▶ YouTube")})
    rows = event.selection.rows if event and hasattr(event, "selection") else []
    if rows:
        v = df.iloc[rows[0]]
        ui.section(f"Metric history — {v.title}", "Each point is one pipeline observation (lifetime counts)")
        h = q("video_history", v.video_id).drop_duplicates("observed_at")
        if not h.empty:
            fig = go.Figure(go.Scatter(x=h.observed_at.dt.tz_convert(tz), y=h.view_count, mode="lines+markers",
                                       line=dict(width=2, color=ui.SERIES[0]), marker=dict(size=8),
                                       hovertemplate="%{x|%d %b %H:%M}<br>%{y:,} views<extra></extra>"))
            ui.plot(ui.style_fig(fig, 300, legend=False, ytitle="Views (cumulative)"), key="vh")
    else:
        st.caption("Select a row to see that video's observation history.")
