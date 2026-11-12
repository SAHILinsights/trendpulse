"""Reusable UI building blocks: theme, CSS, KPI tiles, badges, chart styling."""

from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---- palette (validated with the dataviz validator against the #121a2e surface: all checks pass)
SURFACE = "#121a2e"
BG = "#0b1120"
TEXT = "#e6eaf2"
TEXT_2 = "#a8b3c7"
MUTED = "#6b7a93"
GRID = "rgba(168,179,199,0.10)"
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
POS, NEG, NEUTRAL = "#3987e5", "#e66767", "#4b5563"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

LIFECYCLE = {
    "emerging": ("#9085e9", "✦"), "growing": ("#199e70", "▲"), "peaking": ("#c98500", "◆"),
    "stable": ("#3987e5", "●"), "declining": ("#d95926", "▼"), "dormant": ("#6b7a93", "○"),
    "baseline": ("#64748b", "◌"),
}
RUN_STATUS = {
    "success": ("good", "✓"), "partial": ("warning", "◐"), "skipped": ("warning", "⤼"),
    "skipped_locked": ("warning", "⤼"), "running": ("warning", "…"), "dq_failed": ("critical", "✕"),
    "failed": ("critical", "✕"), "abandoned": ("serious", "!"),
}
DQ_STATUS = {"PASS": ("good", "✓"), "WARNING": ("warning", "!"), "FAIL": ("critical", "✕")}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
html, body, [class*="css"], .stMarkdown, .stText {{ font-family: 'Inter', system-ui, sans-serif; }}
.stApp {{ background: radial-gradient(1200px 600px at 10% -10%, rgba(57,135,229,0.10), transparent 60%),
          radial-gradient(900px 500px at 100% 0%, rgba(144,133,233,0.08), transparent 55%), {BG}; }}
section[data-testid="stSidebar"] {{ background: linear-gradient(180deg, #0e1528 0%, #0b1120 100%);
          border-right: 1px solid rgba(168,179,199,0.08); }}
.block-container {{ padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1400px; }}
h1, h2, h3 {{ letter-spacing: -0.01em; }}
.tp-hero {{ display:flex; justify-content:space-between; align-items:flex-end; gap:1rem; flex-wrap:wrap;
           margin-bottom: 1.1rem; }}
.tp-hero h1 {{ font-size: 1.75rem; font-weight: 700; margin: 0; color: {TEXT}; }}
.tp-hero p {{ color: {TEXT_2}; margin: .25rem 0 0 0; font-size: .95rem; }}
.tp-card {{ background: linear-gradient(180deg, rgba(25,35,60,0.85), rgba(18,26,46,0.85));
           border: 1px solid rgba(168,179,199,0.10); border-radius: 14px; padding: 16px 18px; height: 100%; }}
.tp-kpi-label {{ color: {TEXT_2}; font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; }}
.tp-kpi-value {{ color: {TEXT}; font-size: 1.65rem; font-weight: 700; margin-top: 4px;
                font-variant-numeric: tabular-nums; }}
.tp-kpi-sub {{ color: {MUTED}; font-size: .8rem; margin-top: 2px; }}
.tp-badge {{ display:inline-flex; align-items:center; gap:5px; padding: 2px 9px; border-radius: 999px;
            font-size: .76rem; font-weight: 600; border: 1px solid; white-space: nowrap; }}
.tp-banner {{ border-radius: 12px; padding: 10px 14px; margin-bottom: 14px; font-size: .9rem;
             border: 1px solid; display:flex; gap:10px; align-items:center; }}
.tp-section {{ color: {TEXT}; font-weight: 600; font-size: 1.05rem; margin: 1.2rem 0 .5rem 0; }}
.tp-caption {{ color: {MUTED}; font-size: .8rem; }}
.tp-mono {{ font-family: 'JetBrains Mono', monospace; font-size: .8rem; color: {TEXT_2}; }}
.tp-flow {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
.tp-node {{ background: rgba(57,135,229,0.10); border:1px solid rgba(57,135,229,0.35); color:{TEXT};
           padding:6px 10px; border-radius:8px; font-size:.8rem; }}
.tp-arrow {{ color:{MUTED}; }}
div[data-testid="stMetric"] {{ background: rgba(18,26,46,0.7); border: 1px solid rgba(168,179,199,0.10);
           border-radius: 12px; padding: 10px 14px; }}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def hero(title: str, subtitle: str, right_html: str = "") -> None:
    st.markdown(f"<div class='tp-hero'><div><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>"
                f"<div>{right_html}</div></div>", unsafe_allow_html=True)


def badge(text: str, color: str, icon: str = "") -> str:
    return (f"<span class='tp-badge' style='color:{color};border-color:{color}55;background:{color}1a'>"
            f"{esc(icon)} {esc(text)}</span>")


def status_badge(status: str | None, mapping: dict = RUN_STATUS) -> str:
    if not status:
        return badge("unknown", MUTED)
    role, icon = mapping.get(status, ("warning", "?"))
    return badge(status.replace("_", " "), STATUS[role], icon)


def lifecycle_badge(stage: str) -> str:
    color, icon = LIFECYCLE.get(stage, (MUTED, "•"))
    return badge(stage, color, icon)


def kpi(label: str, value, sub: str = "", help_text: str = "") -> str:
    title = f" title='{esc(help_text)}'" if help_text else ""
    return (f"<div class='tp-card'{title}><div class='tp-kpi-label'>{esc(label)}</div>"
            f"<div class='tp-kpi-value'>{esc(value)}</div><div class='tp-kpi-sub'>{sub}</div></div>")


def kpi_row(items: list[tuple]) -> None:
    cols = st.columns(len(items))
    for col, item in zip(cols, items, strict=True):
        col.markdown(kpi(*item), unsafe_allow_html=True)


def section(title: str, caption: str = "") -> None:
    st.markdown(f"<div class='tp-section'>{esc(title)}</div>"
                + (f"<div class='tp-caption' style='margin-top:-6px;margin-bottom:8px'>{esc(caption)}</div>"
                   if caption else ""), unsafe_allow_html=True)


def banner(text: str, role: str = "warning", icon: str = "!") -> None:
    c = STATUS.get(role, TEXT_2) if role != "info" else "#3987e5"
    st.markdown(f"<div class='tp-banner' style='color:{TEXT};border-color:{c}66;background:{c}14'>"
                f"<b style='color:{c}'>{esc(icon)}</b><span>{text}</span></div>", unsafe_allow_html=True)


def empty_state(title: str, body: str) -> None:
    st.markdown(f"<div class='tp-card' style='text-align:center;padding:36px'><div style='font-size:1.1rem;"
                f"font-weight:600;color:{TEXT}'>{esc(title)}</div><div class='tp-caption' style='margin-top:6px;"
                f"font-size:.9rem'>{body}</div></div>", unsafe_allow_html=True)


# ---- formatting
def fmt_int(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NA:
        return "—"
    x = float(x)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:,.0f}"


def fmt_pct(x, digits: int = 1) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:+.{digits}f}%"


def fmt_ts(x, tz: str = "UTC") -> str:
    if x is None or (not isinstance(x, datetime) and pd.isna(x)):
        return "—"
    ts = pd.Timestamp(x)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return ts.tz_convert(tz).strftime("%d %b %Y, %H:%M ") + ("UTC" if tz == "UTC" else ts.tz_convert(tz).tzname())


def fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h ago"
    return f"{minutes / 1440:.1f} days ago"


# ---- plotly
def style_fig(fig: go.Figure, height: int = 360, legend: bool = True, ytitle: str | None = None,
              xtitle: str | None = None) -> go.Figure:
    fig.update_layout(
        height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", color=TEXT_2, size=12),
        margin=dict(l=8, r=8, t=28, b=8), colorway=SERIES, showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(color=TEXT_2),
                    bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor="#1b2540", bordercolor="#2b3a5e", font=dict(color=TEXT, family="Inter")),
        bargap=0.25,
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor="rgba(168,179,199,0.2)", title=xtitle,
                     tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor="rgba(168,179,199,0.2)", title=ytitle,
                     tickfont=dict(color=MUTED))
    return fig


def color_map(keys: list[str]) -> dict[str, str]:
    """Stable entity -> color assignment (fixed order, never cycled past 8: extra keys get muted grey)."""
    out = {}
    for i, k in enumerate(keys):
        out[k] = SERIES[i] if i < len(SERIES) else MUTED
    return out


def plot(fig: go.Figure, key: str | None = None) -> None:
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key)
