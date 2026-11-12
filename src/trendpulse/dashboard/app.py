"""TrendPulse Streamlit application (entry: ``streamlit run streamlit_app.py``)."""

from __future__ import annotations

import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.runtime import data_mode, get_wh, q

TIMEZONES = ["UTC", "Asia/Kolkata", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin",
             "Asia/Tokyo", "Asia/Seoul", "America/Sao_Paulo", "Australia/Sydney"]


def _sidebar(health: dict | None, mode) -> None:
    with st.sidebar:
        st.markdown(
            "<div style='display:flex;align-items:center;gap:10px;margin:4px 0 14px 0'>"
            "<div style='width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#3987e5,#9085e9);"
            "display:flex;align-items:center;justify-content:center;font-weight:800;color:white'>TP</div>"
            "<div><div style='font-weight:700;font-size:1.05rem;color:#e6eaf2'>TrendPulse</div>"
            "<div style='font-size:.72rem;color:#6b7a93'>YouTube Trending Intelligence</div></div></div>",
            unsafe_allow_html=True)
        st.session_state.setdefault("tz", "UTC")
        st.selectbox("Display timezone", TIMEZONES, key="tz")
        role = {"live": "good", "historical": "warning", "sample": "serious", "empty": "warning",
                "error": "critical"}[mode.key]
        st.markdown(ui.badge(mode.label, ui.STATUS[role], "●"), unsafe_allow_html=True)
        if health:
            st.markdown(
                f"<div class='tp-caption' style='margin-top:8px'>Snapshot: {ui.fmt_ts(health.get('snapshot_at'), st.session_state.tz)}"
                f"<br>Freshness: <b>{health.get('freshness')}</b> ({ui.fmt_age(health.get('freshness_minutes'))})"
                f"<br>Target cadence: every {health.get('interval_minutes')} min</div>", unsafe_allow_html=True)
        st.markdown("<div class='tp-caption' style='margin-top:18px'>Data: YouTube Data API v3 regional "
                    "popular charts + topic search. Topics are inferred by TrendPulse, not provided by YouTube."
                    "</div>", unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="TrendPulse — YouTube Trending Intelligence", page_icon="📈", layout="wide",
                       initial_sidebar_state="expanded")
    ui.inject_css()
    error = None
    health = None
    try:
        wh = get_wh()
        if wh is not None:
            health = q("platform_health")
    except Exception as exc:  # noqa: BLE001 - shown as an error state
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    mode = data_mode(health, error)
    st.session_state["data_mode"] = mode
    st.session_state["health"] = health
    _sidebar(health, mode)

    from trendpulse.dashboard.views import (
        channels,
        control_center,
        discovery,
        overview,
        regional,
        topic,
        videos,
    )

    pages = {
        "Intelligence": [
            st.Page(overview.render, title="Global Overview", icon=":material/public:", url_path="overview",
                    default=True),
            st.Page(discovery.render, title="Trend Discovery", icon=":material/trending_up:", url_path="discovery"),
            st.Page(topic.render, title="Topic Intelligence", icon=":material/hub:", url_path="topic"),
            st.Page(regional.render, title="Regional Intelligence", icon=":material/language:", url_path="regional"),
            st.Page(channels.render, title="Channel Intelligence", icon=":material/subscriptions:",
                    url_path="channels"),
            st.Page(videos.render, title="Video Explorer", icon=":material/smart_display:", url_path="videos"),
        ],
        "Platform": [
            st.Page(control_center.render, title="Data Engineering Control Center", icon=":material/monitoring:",
                    url_path="control-center"),
        ],
    }
    nav = st.navigation(pages)
    nav.run()


def guard() -> bool:
    """Shared page preamble: banners for sample/empty/error states. Returns False if the page can't render."""
    mode = st.session_state.get("data_mode")
    if mode is None:
        return False
    if mode.key == "error":
        ui.banner(f"Cannot reach the warehouse. {ui.esc(mode.detail)}", "critical", "✕")
        return False
    if mode.key == "empty":
        ui.empty_state("No published data yet",
                       "Initialise the platform: set <code>YOUTUBE_API_KEY</code> and <code>DATABASE_URL</code>, "
                       "then run <code>trendpulse run</code> (or trigger the <b>Scheduled ingestion</b> GitHub "
                       "workflow). For an offline demo: <code>trendpulse simulate --cycles 96</code>.")
        return False
    if mode.key == "sample":
        ui.banner("<b>SAMPLE DATA.</b> Everything on this page comes from TrendPulse's offline API simulator "
                  "(fictional videos, channels and topics) — it demonstrates the pipeline, not real YouTube trends.",
                  "serious", "!")
    elif mode.key == "historical":
        ui.banner("The latest successful pipeline run is older than expected — figures below are historical, "
                  "not current. See the Control Center for run status.", "warning", "!")
    return True
