"""TrendPulse control plane (entry: ``streamlit run streamlit_app.py``).

Information architecture — the pipeline is the product, the dashboard is its control room:

  Platform        Overview · Pipeline Orchestrator · Data Lakehouse · Data Quality & Governance ·
                  Lineage & Observability · Execution History & Recovery
  Consumption     Trend Intelligence (Gold data products)
"""

from __future__ import annotations

import streamlit as st

from trendpulse.dashboard import components as ui
from trendpulse.dashboard.runtime import data_mode, get_wh, q

TIMEZONES = ["UTC", "Asia/Kolkata", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin",
             "Asia/Tokyo", "Asia/Seoul", "America/Sao_Paulo", "Australia/Sydney"]
PRODUCT = "TrendPulse"
SUBTITLE = "Real-Time Data Engineering & Intelligence Platform"


def _environment() -> dict:
    from trendpulse.settings import get_settings
    from trendpulse.storage.migrations import current_version

    env = {"warehouse": "—", "schema": "—", "live": get_settings().pipeline.sources.youtube.live_ingestion_enabled}
    wh = get_wh()
    if wh is not None:
        env["warehouse"] = "PostgreSQL" if wh.is_postgres else "SQLite (local)"
        try:
            env["schema"] = f"v{current_version(wh)}"
        except Exception:  # noqa: BLE001
            env["schema"] = "?"
    return env


def _sidebar(health: dict | None, mode) -> None:
    with st.sidebar:
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:10px;margin:2px 0 4px 0'>"
            f"<div style='width:30px;height:30px;border-radius:6px;background:{ui.ACCENT};display:flex;"
            f"align-items:center;justify-content:center;font-weight:800;color:{ui.BG};font-size:.8rem'>TP</div>"
            f"<div><div style='font-weight:700;font-size:1rem;color:{ui.TEXT}'>{PRODUCT}</div>"
            f"<div style='font-size:.68rem;color:{ui.MUTED};line-height:1.2'>{SUBTITLE}</div></div></div>",
            unsafe_allow_html=True)


def _sidebar_footer(health: dict | None, mode) -> None:
    env = _environment()
    with st.sidebar:
        st.markdown("<hr style='margin:8px 0;border-color:#273449'>", unsafe_allow_html=True)
        st.session_state.setdefault("tz", "UTC")
        st.selectbox("Display timezone", TIMEZONES, key="tz")
        role = {"live": "good", "historical": "warning", "sample": "serious", "empty": "idle",
                "error": "critical"}[mode.key]
        st.markdown(
            f"<div class='tp-caption' style='line-height:1.9'>Data mode&nbsp; {ui.badge(mode.label, ui.STATUS[role])}"
            f"<br>Warehouse&nbsp; <span class='tp-mono'>{env['warehouse']} · schema {env['schema']}</span>"
            f"<br>Live YouTube ingestion&nbsp; "
            f"{ui.badge('enabled', ui.OK) if env['live'] else ui.badge('disabled · compliance', ui.WARN)}"
            + (f"<br>Snapshot&nbsp; <span class='tp-mono'>{ui.fmt_ts(health.get('snapshot_at'), st.session_state.tz)}"
               f"</span>" if health else "") + "</div>", unsafe_allow_html=True)
        st.markdown(_legal_footer(), unsafe_allow_html=True)


REPO_URL = "https://github.com/SAHILinsights/trendpulse"


def _legal_footer() -> str:
    """Attribution and policy links required for YouTube API Services clients."""
    link = f"color:{ui.ACCENT};text-decoration:none"
    return (
        f"<div class='tp-caption' style='margin-top:10px;padding-top:8px;border-top:1px solid {ui.BORDER};"
        f"line-height:1.8'>"
        f"<span style='color:{ui.TEXT};font-weight:600'>Uses YouTube API Services</span>"
        f"<br>Third-party analysis, not affiliated with or endorsed by YouTube."
        f"<br><a style='{link}' href='{REPO_URL}/blob/main/PRIVACY.md' target='_blank'>Privacy policy</a>"
        f" · <a style='{link}' href='{REPO_URL}/blob/main/TERMS.md' target='_blank'>Terms of service</a>"
        f"<br><a style='{link}' href='https://www.youtube.com/t/terms' target='_blank'>YouTube Terms of Service</a>"
        f" · <a style='{link}' href='https://policies.google.com/privacy' target='_blank'>Google Privacy Policy</a>"
        f"</div>")


def main() -> None:
    st.set_page_config(page_title=f"{PRODUCT} — Data Engineering Control Plane", page_icon="◆", layout="wide",
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
        control_center,
        executions,
        intelligence,
        lakehouse,
        lineage,
        orchestrator,
        platform,
        quality,
    )

    pages = {
        "Platform": [
            st.Page(platform.render, title="Platform Overview", icon=":material/monitor_heart:", url_path="overview",
                    default=True),
            st.Page(orchestrator.render, title="Pipeline Orchestrator", icon=":material/account_tree:",
                    url_path="orchestrator"),
            st.Page(lakehouse.render, title="Data Lakehouse", icon=":material/database:", url_path="lakehouse"),
            st.Page(quality.render, title="Data Quality & Governance", icon=":material/verified:",
                    url_path="quality"),
            st.Page(lineage.render, title="Lineage & Observability", icon=":material/hub:", url_path="lineage"),
            st.Page(executions.render, title="Execution History & Recovery", icon=":material/history:",
                    url_path="executions"),
        ],
        "Consumption": [
            st.Page(intelligence.render, title="Trend Intelligence", icon=":material/trending_up:",
                    url_path="intelligence"),
            # previous single-page control center, kept reachable for comparison (not in navigation)
            st.Page(control_center.render, title="Legacy control center", url_path="legacy-control-center",
                    visibility="hidden"),
        ],
    }
    nav = st.navigation(pages)
    _sidebar_footer(health, mode)
    nav.run()


def guard() -> bool:
    """Preamble for data-product pages: banners for sample/empty/error states."""
    mode = st.session_state.get("data_mode")
    if mode is None:
        return False
    if mode.key == "error":
        ui.banner(f"Cannot reach the warehouse. {ui.esc(mode.detail)}", "critical", "✕")
        return False
    if mode.key == "empty":
        ui.empty_state("No published data yet", INIT_HELP)
        return False
    mode_banner(mode)
    return True


INIT_HELP = ("Initialise the platform: <code>trendpulse migrate</code>, then <code>trendpulse run</code> "
             "(live ingestion must be approved — see docs/data_source_compliance.md) or, for an offline demo, "
             "<code>trendpulse simulate --cycles 96 --chaos</code>.")


def mode_banner(mode) -> None:
    if mode.key == "sample":
        ui.banner("<b>SAMPLE DATA</b> — observations come from TrendPulse's offline API simulator (fictional "
                  "videos/channels; some runs include deliberate fault injection). Execution metadata, quality "
                  "audits and logs are real records of those runs.", "serious", "!")
    elif mode.key == "historical":
        ui.banner("No successful run recently — figures are historical, not current.", "warning", "!")


def engineering_guard(title: str, subtitle: str, crumb: str = "Platform", right_html: str = "") -> bool:
    """Preamble for engineering modules (renders the header; returns False on empty/error state)."""
    ui.hero(title, subtitle, right_html, crumb)
    mode = st.session_state.get("data_mode")
    if mode is None:
        return False
    if mode.key == "error":
        ui.banner(f"Cannot reach the warehouse. {ui.esc(mode.detail)}", "critical", "✕")
        return False
    if mode.key == "empty" and not (st.session_state.get("health") or {}).get("last_attempt"):
        ui.empty_state("Platform not initialized", "No pipeline execution has been recorded. " + INIT_HELP)
        return False
    mode_banner(mode)
    return True
