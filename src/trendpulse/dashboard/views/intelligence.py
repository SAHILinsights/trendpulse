"""Module 7 — Trend Intelligence: the consumption layer over Gold serving tables."""

from __future__ import annotations

import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.app import guard
from trendpulse.dashboard.views import channels, discovery, overview, regional, topic, videos


def render() -> None:
    ui.hero("Trend Intelligence", "Gold data products: topic trends, lifecycle, regional comparison — every metric "
                                  "is a documented TrendPulse calculation over observed API data",
            crumb="Consumption")
    if not guard():
        return
    st.markdown("<div class='tp-caption' style='margin-bottom:6px'>Source tables: gold_trending_topics · "
                "gold_topic_growth · gold_regional_comparison · gold_channel_performance · gold_category_summary · "
                "fact_topic_observations. Definitions: docs/topic_intelligence.md.</div>", unsafe_allow_html=True)
    tabs = st.tabs(["Summary", "Emerging & growth", "Topic detail", "Regional comparison", "Channels", "Videos"])
    for tab, view in zip(tabs, (overview, discovery, topic, regional, channels, videos), strict=True):
        with tab:
            view.render(embedded=True)
