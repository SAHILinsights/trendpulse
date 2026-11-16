from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.runtime import q


def render(embedded: bool = False) -> None:
    tz = st.session_state.get("tz", "UTC")
    health = st.session_state.get("health") or {}
    if not embedded:
        ui.hero("Global Overview", "What is trending across monitored YouTube regions right now",
                ui.badge(f"Freshness: {health.get('freshness', '—')}",
                         ui.STATUS["good" if health.get("freshness") == "fresh" else "warning"], "●"))
        if not guard():
            return
    c = q("overview_counts")
    growth = q("growth", "GLOBAL", "24h")
    fastest = "—"
    if not growth.empty:
        g = growth.dropna(subset=["score_growth_pct"]).sort_values("score_growth_pct", ascending=False)
        g = g[g.video_count_now.fillna(0) > 0]
        if not g.empty:
            fastest = f"{g.iloc[0].topic_name}"
            fastest_sub = f"{ui.fmt_pct(g.iloc[0].score_growth_pct)} popularity score, 24h"
    last_ok = health.get("last_success") or {}
    ui.kpi_row([
        ("Active trending topics", c["active_topics"], "ranked globally in the latest snapshot"),
        ("Emerging topics", c["emerging_topics"], f"{c['growing_topics']} more classified as growing"),
        ("Fastest-growing (24h)", fastest, fastest_sub if fastest != "—" else "needs ≥ 24h of history"),
        ("Observed videos", ui.fmt_int(c["videos_24h"]), f"last 24h · {ui.fmt_int(c['videos_total'])} all-time"),
    ])
    st.write("")
    ui.kpi_row([
        ("Regions monitored", c["regions_monitored"], "countries with data in this snapshot"),
        ("Unique channels", ui.fmt_int(c["channels_total"]), "seen since tracking began"),
        ("Latest successful ingestion", ui.fmt_ts(last_ok.get("started_at"), tz).rsplit(",", 1)[-1].strip()
         if last_ok else "—", ui.fmt_ts(last_ok.get("started_at"), tz).split(",")[0] if last_ok else ""),
        ("Data freshness", ui.fmt_age(health.get("freshness_minutes")),
         f"target every {health.get('interval_minutes')} min"),
    ])

    left, right = st.columns([3, 2])
    with left:
        ui.section("Topic popularity over time", "Popularity score of today's top global topics, last 48 hours")
        hist = q("top_topics_history", "GLOBAL", 6, 2)
        if hist.empty:
            ui.empty_state("No history yet", "Charts appear after a few pipeline runs.")
        else:
            cmap = ui.color_map(list(hist.drop_duplicates("topic_id").topic_name))
            fig = go.Figure()
            for name, grp in hist.groupby("topic_name", sort=False):
                x = grp.observed_at.dt.tz_convert(tz)
                fig.add_trace(go.Scatter(x=x, y=grp.popularity_score, name=name, mode="lines",
                                         line=dict(width=2, color=cmap[name]),
                                         hovertemplate="%{x|%d %b %H:%M}<br>score %{y:.1f}<extra>" + name + "</extra>"))
            ui.plot(ui.style_fig(fig, 360, ytitle="Popularity score", xtitle=f"Time ({tz})"))
    with right:
        ui.section("Global trending topic ranking", "Top 12 by popularity score")
        t = q("trending", "GLOBAL").head(12).iloc[::-1]
        if not t.empty:
            fig = go.Figure(go.Bar(
                x=t.popularity_score, y=t.topic_name, orientation="h", marker=dict(color=ui.SERIES[0],
                                                                                  cornerradius=4),
                customdata=t[["category", "lifecycle_stage", "video_count", "channel_count"]],
                hovertemplate="<b>%{y}</b><br>score %{x:.1f}<br>%{customdata[0]} · %{customdata[1]}"
                              "<br>%{customdata[2]} videos · %{customdata[3]} channels<extra></extra>"))
            ui.plot(ui.style_fig(fig, 360, legend=False, xtitle="Popularity score"))

    left, right = st.columns(2)
    with left:
        ui.section("Category distribution", "Distinct videos per TrendPulse category, latest snapshot (global)")
        cat = q("category_summary")
        cat = cat[cat.region_code == "GLOBAL"].sort_values("video_count")
        if not cat.empty:
            fig = go.Figure(go.Bar(x=cat.video_count, y=cat.category, orientation="h",
                                   marker=dict(color=ui.SERIES[2], cornerradius=4),
                                   customdata=cat[["topic_count", "total_views", "avg_engagement_rate"]],
                                   hovertemplate="<b>%{y}</b><br>%{x} videos · %{customdata[0]} topics"
                                                 "<br>avg engagement %{customdata[2]:.2%}<extra></extra>"))
            ui.plot(ui.style_fig(fig, 340, legend=False, xtitle="Videos"))
    with right:
        ui.section("Country-wise trending activity", "Active ranked topics per region, latest snapshot")
        ra = q("region_activity").sort_values("active_topics", ascending=False)
        if not ra.empty:
            fig = go.Figure(go.Bar(x=ra.region_code, y=ra.active_topics, marker=dict(color=ui.SERIES[0],
                                                                                     cornerradius=4),
                                   customdata=ra[["videos"]],
                                   hovertemplate="<b>%{x}</b><br>%{y} active topics<br>%{customdata[0]} videos"
                                                 "<extra></extra>"))
            ui.plot(ui.style_fig(fig, 340, legend=False, ytitle="Active topics"))
    st.markdown(f"<div class='tp-caption'>Times shown in {tz}. Popularity score = 20·ln(1+videos) + "
                "10·ln(1+channels) + 8·log10(1+views/hour) + engagement% — see docs/topic_intelligence.md.</div>",
                unsafe_allow_html=True)
