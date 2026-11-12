from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.runtime import q


def render() -> None:
    ui.hero("Channel Intelligence", "Which channels are driving trending topics (last 24 hours)")
    if not guard():
        return
    region = st.selectbox("Region", ["GLOBAL"] + list(q("regions").region_code), key="ch_region")
    ch = q("channels", region, 100)
    if ch.empty:
        ui.empty_state("No channel data", "")
        return
    ui.kpi_row([
        ("Channels (24h)", ui.fmt_int(len(ch)), "top 100 shown"),
        ("Most frequent", ch.iloc[0].channel_title, f"{int(ch.iloc[0].appearances_24h)} chart/search appearances"),
        ("Highest views/hour", ch.sort_values("views_per_hour", ascending=False).iloc[0].channel_title,
         ui.fmt_int(ch.views_per_hour.max()) + " views/h"),
        ("Multi-region channels", int((ch.regions_count.fillna(0) >= 3).sum()), "present in ≥ 3 regions"),
    ])
    a, b = st.columns([1, 1.3])
    with a:
        ui.section("Appearance frequency", "Observations of the channel's videos in the last 24h")
        top = ch.head(15).iloc[::-1]
        fig = go.Figure(go.Bar(x=top.appearances_24h, y=top.channel_title, orientation="h",
                               marker=dict(color=ui.SERIES[0], cornerradius=4),
                               customdata=top[["videos_24h", "top_topic_name", "regions_count"]],
                               hovertemplate="<b>%{y}</b><br>%{x} appearances · %{customdata[0]} videos"
                                             "<br>top topic: %{customdata[1]}<br>%{customdata[2]} regions"
                                             "<extra></extra>"))
        ui.plot(ui.style_fig(fig, 460, legend=False, xtitle="Appearances (24h)"), key="c1")
    with b:
        ui.section("Video performance", "Views gained per hour vs. engagement — one dot per channel")
        d = ch.dropna(subset=["views_per_hour", "avg_engagement_rate"])
        fig = go.Figure(go.Scatter(x=d.views_per_hour, y=d.avg_engagement_rate * 100, mode="markers",
                                   marker=dict(size=10, color=ui.SERIES[2], line=dict(width=2, color=ui.SURFACE)),
                                   text=d.channel_title, customdata=d[["videos_24h", "top_topic_name"]],
                                   hovertemplate="<b>%{text}</b><br>%{x:,.0f} views/h · %{y:.2f}% engagement"
                                                 "<br>%{customdata[0]} videos · %{customdata[1]}<extra></extra>"))
        fig.update_xaxes(type="log")
        ui.plot(ui.style_fig(fig, 460, legend=False, xtitle="Views per hour (log)", ytitle="Engagement (%)"), key="c2")

    ui.section("Top trending channels")
    st.dataframe(ch[["channel_title", "appearances_24h", "videos_24h", "total_views", "views_per_hour",
                     "avg_engagement_rate", "top_topic_name", "topics_count", "regions_count"]], hide_index=True,
                 height=420, column_config={
                     "channel_title": "Channel", "appearances_24h": "Appearances 24h", "videos_24h": "Videos 24h",
                     "total_views": st.column_config.NumberColumn("Total views (lifetime)", format="compact"),
                     "views_per_hour": st.column_config.NumberColumn("Views/hour", format="compact"),
                     "avg_engagement_rate": st.column_config.NumberColumn("Engagement", format="percent"),
                     "top_topic_name": "Top topic", "topics_count": "Topics", "regions_count": "Regions"})

    ui.section("Channel detail")
    sel = st.selectbox("Channel", list(ch.channel_id), format_func=lambda c: ch.set_index("channel_id")
                       .channel_title.get(c, c), key="ch_sel")
    a, b = st.columns(2)
    with a:
        st.caption("Topic contribution — videos per topic")
        st.dataframe(q("channel_topics", sel), hide_index=True, height=260,
                     column_config={"canonical_name": "Topic", "category": "Category", "videos": "Videos"})
    with b:
        st.caption("Regional popularity — appearances per region (24h)")
        rr = q("channel_regions", [sel])
        if not rr.empty:
            rr = rr.sort_values("appearances_24h", ascending=False)
            fig = go.Figure(go.Bar(x=rr.region_code, y=rr.appearances_24h, marker=dict(color=ui.SERIES[0],
                                                                                       cornerradius=4),
                                   hovertemplate="%{x}: %{y} appearances<extra></extra>"))
            ui.plot(ui.style_fig(fig, 260, legend=False), key="c3")
