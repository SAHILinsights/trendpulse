from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.runtime import q


def _line(df, x, ys: list[tuple[str, str]], tz: str, ytitle: str, height: int = 280, fmt: str = ".1f"):
    fig = go.Figure()
    for i, (col, name) in enumerate(ys):
        fig.add_trace(go.Scatter(x=df[x].dt.tz_convert(tz), y=df[col], name=name, mode="lines",
                                 line=dict(width=2, color=ui.SERIES[i]),
                                 hovertemplate="%{x|%d %b %H:%M}<br>%{y:" + fmt + "}<extra>" + name + "</extra>"))
    return ui.style_fig(fig, height, legend=len(ys) > 1, ytitle=ytitle)


def render(embedded: bool = False) -> None:
    tz = st.session_state.get("tz", "UTC")
    if not embedded:
        ui.hero("Topic Intelligence", "Deep dive into one topic's history, reach and drivers")
        if not guard():
            return
    glob = q("trending", "GLOBAL")
    topics = q("topics")
    if topics.empty:
        ui.empty_state("No topics yet", "")
        return
    ranked = list(glob.topic_id)
    others = [t for t in topics.topic_id if t not in set(ranked) and t != "tx.unclassified"]
    names = dict(zip(topics.topic_id, topics.canonical_name, strict=True))
    options = ranked + others
    c1, c2, c3 = st.columns([2.5, 1, 1])
    tid = c1.selectbox("Topic", options, format_func=lambda t: f"{names.get(t, t)}  ·  {t}", key="topic_sel")
    region = c2.selectbox("Region", ["GLOBAL"] + list(q("regions").region_code), key="topic_region")
    days = c3.segmented_control("History", [1, 7, 30], default=7, format_func=lambda d: f"{d}d", key="topic_days") or 7

    meta = topics[topics.topic_id == tid].iloc[0]
    cur = q("trending", region)
    row = cur[cur.topic_id == tid]
    stage = row.lifecycle_stage.iloc[0] if not row.empty else None
    badges = (ui.lifecycle_badge(stage) if stage else ui.badge("not ranked in this region now", ui.MUTED)) + " " + \
        ui.badge(meta.category, ui.TEXT_2) + " " + ui.badge(meta.topic_kind, ui.TEXT_2)
    st.markdown(f"<div style='margin:4px 0 12px 0'><span style='font-size:1.4rem;font-weight:700;color:#e6eaf2'>"
                f"{ui.esc(meta.canonical_name)}</span>&nbsp;&nbsp;{badges}</div>", unsafe_allow_html=True)
    hist = q("topic_history", tid, region, days)
    ui.kpi_row([
        ("Popularity rank", f"#{int(row.popularity_rank.iloc[0])}" if not row.empty else "—", f"in {region}"),
        ("Popularity score", f"{row.popularity_score.iloc[0]:.1f}" if not row.empty else "—",
         f"24h growth {ui.fmt_pct(row.growth_24h_pct.iloc[0])}" if not row.empty else ""),
        ("First observed", ui.fmt_ts(meta.first_observed_at, tz).split(",")[0],
         ui.fmt_ts(meta.first_observed_at, tz).split(",")[-1]),
        ("Latest observed", ui.fmt_ts(meta.last_observed_at, tz).split(",")[0],
         ui.fmt_ts(meta.last_observed_at, tz).split(",")[-1]),
    ])
    if hist.empty:
        ui.empty_state("No observations in this window", "Pick a longer history or another region.")
        return
    a, b = st.columns(2)
    with a:
        ui.section("Historical popularity", "Popularity score per pipeline snapshot")
        ui.plot(_line(hist, "observed_at", [("popularity_score", "Popularity score")], tz, "Score"), key="t1")
    with b:
        ui.section("Video count & unique channels over time")
        ui.plot(_line(hist, "observed_at", [("video_count", "Videos"), ("channel_count", "Channels")], tz, "Count",
                      fmt=".0f"), key="t2")
    a, b = st.columns(2)
    with a:
        ui.section("Engagement trend", "Mean (likes + comments) / views of the topic's videos")
        ui.plot(_line(hist.assign(er=hist.avg_engagement_rate * 100), "observed_at", [("er", "Engagement %")], tz,
                      "Engagement (%)", fmt=".2f"), key="t3")
    with b:
        ui.section("View velocity", "Views gained per hour since each video's previous observation (summed)")
        ui.plot(_line(hist, "observed_at", [("views_per_hour", "Views / hour")], tz, "Views per hour", fmt=",.0f"),
                key="t4")

    a, b = st.columns([1.2, 1])
    with a:
        ui.section("Regional distribution", "Popularity score by region, latest snapshot")
        reg = q("topic_regions", tid).sort_values("popularity_score", ascending=False)
        if reg.empty:
            st.info("Not currently ranked in any individual region.")
        else:
            fig = go.Figure(go.Bar(x=reg.region_code, y=reg.popularity_score, marker=dict(color=ui.SERIES[0],
                                                                                          cornerradius=4),
                                   customdata=reg[["popularity_rank", "lifecycle_stage", "video_count"]],
                                   hovertemplate="<b>%{x}</b><br>score %{y:.1f} · rank #%{customdata[0]}"
                                                 "<br>%{customdata[1]} · %{customdata[2]} videos<extra></extra>"))
            ui.plot(ui.style_fig(fig, 280, legend=False, ytitle="Score"), key="t5")
    with b:
        ui.section("Related topics", "Topics sharing the most videos with this one")
        rel = q("related_topics", tid)
        st.dataframe(rel, hide_index=True, height=280, column_config={
            "topic_id": None, "canonical_name": "Topic", "category": "Category", "shared_videos": "Shared videos"})

    ui.section("Top contributing videos")
    vids = q("topic_videos", tid, region)
    if not vids.empty:
        vids = vids.assign(url="https://www.youtube.com/watch?v=" + vids.video_id)
        st.dataframe(vids[["thumbnail_url", "title", "channel_title", "latest_view_count", "latest_like_count",
                           "latest_comment_count", "first_observed_at", "last_observed_at", "confidence", "url"]],
                     hide_index=True, row_height=60, column_config={
                         "thumbnail_url": st.column_config.ImageColumn("", width="small"),
                         "title": st.column_config.TextColumn("Title", width="large"), "channel_title": "Channel",
                         "latest_view_count": st.column_config.NumberColumn("Views", format="compact"),
                         "latest_like_count": st.column_config.NumberColumn("Likes", format="compact"),
                         "latest_comment_count": st.column_config.NumberColumn("Comments", format="compact"),
                         "first_observed_at": st.column_config.DatetimeColumn("First seen", format="D MMM HH:mm"),
                         "last_observed_at": st.column_config.DatetimeColumn("Last seen", format="D MMM HH:mm"),
                         "confidence": st.column_config.NumberColumn("Match conf.", format="%.2f"),
                         "url": st.column_config.LinkColumn("Open", display_text="▶ YouTube")})
    if meta.topic_kind == "taxonomy":
        st.markdown(f"<div class='tp-caption'>Taxonomy keywords: {ui.esc(meta.keywords)[:600]}</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown(f"<div class='tp-caption'>Discovered phrase variants: {ui.esc(meta.keywords)}</div>",
                    unsafe_allow_html=True)
