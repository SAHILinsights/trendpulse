from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.runtime import q

SEQ = [[0, "#16213a"], [0.25, "#184f95"], [0.5, "#256abf"], [0.75, "#5598e7"], [1, "#b7d3f6"]]  # one hue, dark->light
DIV = [[0, "#e66767"], [0.5, "#383835"], [1, "#3987e5"]]  # red <-> grey <-> blue


def render(embedded: bool = False) -> None:
    if not embedded:
        ui.hero("Regional Intelligence", "Compare what each country is watching")
        if not guard():
            return
    reg = q("regional_comparison")
    if reg.empty:
        ui.empty_state("No regional data", "")
        return
    regions = sorted(reg.region_code.unique())
    sel = st.multiselect("Countries to compare", regions, default=[r for r in ("IN", "US", "GB") if r in regions]
                         or regions[:3], key="reg_sel")
    if not sel:
        st.info("Select at least one country.")
        return

    ui.section("Top topics per country", "Top 8 by popularity score in the latest snapshot")
    cols = st.columns(min(len(sel), 4))
    for i, r in enumerate(sel[:8]):
        with cols[i % len(cols)]:
            top = reg[reg.region_code == r].sort_values("popularity_rank").head(8)
            st.markdown(f"**{r}**")
            st.dataframe(top[["popularity_rank", "topic_name", "popularity_score", "regions_present"]],
                         hide_index=True, height=320, column_config={
                             "popularity_rank": st.column_config.NumberColumn("#", width="small"),
                             "topic_name": "Topic", "popularity_score": st.column_config.NumberColumn("Score",
                                                                                                     format="%.0f"),
                             "regions_present": st.column_config.NumberColumn("Regions", help="Regions where the topic is ranked")})

    a, b = st.columns([1.4, 1])
    with a:
        ui.section("Shared global topics", "Ranked in ≥ 3 regions — popularity score by region")
        shared = reg[reg.is_shared]
        top_shared = (shared.groupby(["topic_id", "topic_name"]).popularity_score.mean().sort_values(ascending=False)
                      .head(15).reset_index())
        if top_shared.empty:
            st.info("No topic is ranked in 3+ regions yet.")
        else:
            m = (shared[shared.topic_id.isin(top_shared.topic_id)]
                 .pivot_table(index="topic_name", columns="region_code", values="popularity_score")
                 .reindex(top_shared.topic_name))
            fig = go.Figure(go.Heatmap(z=m.values, x=list(m.columns), y=list(m.index), colorscale=SEQ,
                                       xgap=2, ygap=2, colorbar=dict(title="Score", thickness=10),
                                       hovertemplate="%{y} · %{x}<br>score %{z:.1f}<extra></extra>"))
            ui.plot(ui.style_fig(fig, 30 * len(m) + 90, legend=False), key="r1")
    with b:
        ui.section("Country-specific topics", "Ranked in exactly one region")
        spec = reg[reg.is_country_specific & reg.region_code.isin(sel)].sort_values("popularity_score",
                                                                                     ascending=False)
        st.dataframe(spec[["region_code", "topic_name", "category", "popularity_score"]], hide_index=True,
                     height=30 * min(len(spec), 14) + 40, column_config={
                         "region_code": "Region", "topic_name": "Topic", "category": "Category",
                         "popularity_score": st.column_config.NumberColumn("Score", format="%.1f")})

    ui.section("Popular here, absent there", "Topics ranked in country A's top 25 that are not ranked at all in B")
    c1, c2, _ = st.columns([1, 1, 2])
    a_ = c1.selectbox("Country A", regions, index=regions.index("IN") if "IN" in regions else 0, key="ra")
    b_ = c2.selectbox("Country B", regions, index=regions.index("US") if "US" in regions else min(1, len(regions) - 1),
                      key="rb")
    in_a = reg[(reg.region_code == a_) & (reg.popularity_rank <= 25)]
    in_b = set(reg[reg.region_code == b_].topic_id)
    diff = in_a[~in_a.topic_id.isin(in_b)].sort_values("popularity_rank")
    st.dataframe(diff[["popularity_rank", "topic_name", "category", "popularity_score", "regions_present"]],
                 hide_index=True, column_config={"popularity_rank": f"Rank in {a_}", "topic_name": "Topic",
                                                 "category": "Category", "popularity_score": st.column_config.NumberColumn(
                                                     "Score", format="%.1f"), "regions_present": "Regions"})

    a, b = st.columns(2)
    with a:
        ui.section("Topic growth by region (24h)", "Popularity-score change; blue = up, red = down")
        g = q("growth_all_regions", "24h")
        g = g[g.region_code.isin(sel) & g.score_growth_pct.notna()]
        if g.empty:
            st.info("24h growth needs at least 24 hours of history.")
        else:
            top = g.groupby("topic_name").score_growth_pct.apply(lambda x: x.abs().max()).sort_values(
                ascending=False).head(12).index
            m = g[g.topic_name.isin(top)].pivot_table(index="topic_name", columns="region_code",
                                                     values="score_growth_pct")
            lim = float(max(abs(m.min().min()), abs(m.max().max()), 1))
            fig = go.Figure(go.Heatmap(z=m.values, x=list(m.columns), y=list(m.index), colorscale=DIV, zmin=-lim,
                                       zmax=lim, xgap=2, ygap=2, colorbar=dict(title="%", thickness=10),
                                       hovertemplate="%{y} · %{x}<br>%{z:+.1f}%<extra></extra>"))
            ui.plot(ui.style_fig(fig, 30 * len(m) + 90, legend=False), key="r2")
    with b:
        ui.section("Category distribution by country", "Share of each country's observed videos")
        cat = q("category_summary")
        cat = cat[cat.region_code.isin(sel)]
        if not cat.empty:
            cat = cat.assign(share=cat.video_count / cat.groupby("region_code").video_count.transform("sum") * 100)
            m = cat.pivot_table(index="category", columns="region_code", values="share").fillna(0)
            fig = go.Figure(go.Heatmap(z=m.values, x=list(m.columns), y=list(m.index), colorscale=SEQ, xgap=2,
                                       ygap=2, colorbar=dict(title="%", thickness=10),
                                       hovertemplate="%{y} · %{x}<br>%{z:.1f}% of videos<extra></extra>"))
            ui.plot(ui.style_fig(fig, 30 * len(m) + 90, legend=False), key="r3")
