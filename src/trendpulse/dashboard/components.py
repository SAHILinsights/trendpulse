"""Engineering design system: tokens, CSS, status badges, metric strips, panels, chart styling."""

from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---- design tokens --------------------------------------------------------------------------
# Chrome: engineering palette (contrast checked against PANEL: text 15.5:1, secondary 6.3:1, accent
# 6.4:1, success 7.1:1, warning 7.6:1, failure text uses FAIL_TEXT 5.6:1).
# Chart series: the validated categorical order (dataviz validator, dark mode, surface #172033:
# lightness, chroma, CVD ΔE ≥ 8.4, normal-vision ΔE ≥ 19.3, contrast ≥ 3:1 — all PASS).
BG = "#0B1120"
BG_2 = "#111827"
PANEL = "#172033"
SURFACE = PANEL
BORDER = "#273449"
ACCENT = "#60A5FA"
TEXT = "#F8FAFC"
TEXT_2 = "#94A3B8"
MUTED = "#8796AC"
GRID = "rgba(148,163,184,0.10)"
OK, WARN, FAIL, RUNNING, IDLE = "#22C55E", "#F59E0B", "#EF4444", "#60A5FA", "#64748B"
FAIL_TEXT = "#F87171"
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
POS, NEG, NEUTRAL = "#3987e5", "#e66767", "#4b5563"
STATUS = {"good": OK, "warning": WARN, "serious": "#FB923C", "critical": FAIL_TEXT, "info": ACCENT,
          "idle": IDLE}

LIFECYCLE = {
    "emerging": ("#9085e9", "✦"), "growing": ("#199e70", "▲"), "peaking": ("#c98500", "◆"),
    "stable": ("#3987e5", "●"), "declining": ("#d95926", "▼"), "dormant": ("#64748B", "○"),
    "baseline": ("#64748B", "◌"),
}
RUN_STATUS = {
    "success": ("good", "✓"), "partial": ("warning", "◐"), "warning": ("warning", "!"), "skipped": ("idle", "⤼"),
    "skipped_locked": ("idle", "⤼"), "running": ("info", "▶"), "pending": ("idle", "·"),
    "not_recorded": ("idle", "·"), "dq_failed": ("critical", "✕"), "failed": ("critical", "✕"),
    "abandoned": ("serious", "!"), "interrupted": ("serious", "↯"),
}
DQ_STATUS = {"PASS": ("good", "✓"), "WARNING": ("warning", "!"), "FAIL": ("critical", "✕")}
HEALTH = {"healthy": ("good", "●", "Healthy"), "degraded": ("warning", "◐", "Degraded"),
          "failed": ("critical", "✕", "Failed"), "stale": ("serious", "◷", "Stale"),
          "not_initialized": ("idle", "○", "Not initialized")}
NODE_FILL = {"success": OK, "warning": WARN, "partial": WARN, "failed": FAIL, "running": RUNNING,
             "interrupted": "#FB923C",
             "skipped": IDLE, "pending": IDLE, "not_recorded": "#3B475C"}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
:root {{ --bg:{BG}; --bg2:{BG_2}; --panel:{PANEL}; --border:{BORDER}; --accent:{ACCENT}; --text:{TEXT};
        --text2:{TEXT_2}; --muted:{MUTED}; }}
html, body, [class*="css"], .stMarkdown, .stText {{ font-family: 'Inter', system-ui, sans-serif; }}
.stApp {{ background: {BG}; }}
section[data-testid="stSidebar"] {{ background: {BG_2}; border-right: 1px solid {BORDER}; }}
.block-container {{ padding-top: 1.1rem; padding-bottom: 2.5rem; max-width: 1480px; }}
h1, h2, h3 {{ letter-spacing: -0.01em; }}
.tp-hero {{ display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
           padding-bottom:.6rem; margin-bottom:.8rem; border-bottom:1px solid {BORDER}; }}
.tp-hero h1 {{ font-size: 1.28rem; font-weight: 650; margin: 0; color: {TEXT}; }}
.tp-hero p {{ color: {TEXT_2}; margin: .15rem 0 0 0; font-size: .85rem; }}
.tp-crumb {{ color:{MUTED}; font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; }}
.tp-card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 12px 14px; height: 100%; }}
.tp-kpi-label {{ color: {TEXT_2}; font-size: .7rem; text-transform: uppercase; letter-spacing: .07em; }}
.tp-kpi-value {{ color: {TEXT}; font-size: 1.25rem; font-weight: 650; margin-top: 3px;
                font-variant-numeric: tabular-nums; }}
.tp-kpi-sub {{ color: {MUTED}; font-size: .75rem; margin-top: 2px; }}
.tp-strip {{ display:flex; flex-wrap:wrap; background:{PANEL}; border:1px solid {BORDER}; border-radius:8px; }}
.tp-strip > div {{ flex:1 1 140px; padding:9px 14px; border-right:1px solid {BORDER}; min-width:130px; }}
.tp-strip > div:last-child {{ border-right:none; }}
.tp-badge {{ display:inline-flex; align-items:center; gap:4px; padding: 1px 8px; border-radius: 4px;
            font-size: .72rem; font-weight: 600; border: 1px solid; white-space: nowrap; line-height:1.5; }}
.tp-banner {{ border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; font-size: .84rem;
             border: 1px solid; display:flex; gap:10px; align-items:center; }}
.tp-section {{ color: {TEXT}; font-weight: 600; font-size: .95rem; margin: 1rem 0 .35rem 0; }}
.tp-caption {{ color: {MUTED}; font-size: .76rem; }}
.tp-mono, code {{ font-family: 'JetBrains Mono', monospace !important; font-size: .78rem; }}
.tp-kv {{ display:grid; grid-template-columns: 140px minmax(0,1fr); gap:4px 12px; font-size:.82rem; }}
.tp-kv div:nth-child(odd) {{ color:{TEXT_2}; }}
.tp-kv div:nth-child(even) {{ color:{TEXT}; font-family:'JetBrains Mono', monospace; font-size:.78rem;
                             word-break: break-all; }}
.tp-cond {{ display:flex; gap:10px; align-items:flex-start; padding:7px 10px; border-bottom:1px solid {BORDER}; font-size:.82rem; }}
.tp-cond:last-child {{ border-bottom:none; }}
div[data-testid="stMetric"] {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 12px; }}
div[data-testid="stTabs"] button p {{ font-size: .85rem; }}
div[data-testid="stExpander"] {{ border-color: {BORDER}; }}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def hero(title: str, subtitle: str, right_html: str = "", crumb: str = "") -> None:
    c = f"<div class='tp-crumb'>{esc(crumb)}</div>" if crumb else ""
    st.markdown(f"<div class='tp-hero'><div>{c}<h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>"
                f"<div>{right_html}</div></div>", unsafe_allow_html=True)


def badge(text: str, color: str, icon: str = "", title: str = "") -> str:
    t = f" title='{esc(title)}'" if title else ""
    return (f"<span class='tp-badge'{t} style='color:{color};border-color:{color}66;background:{color}1f'>"
            f"{esc(icon)} {esc(text)}</span>")


def status_badge(status: str | None, mapping: dict = RUN_STATUS) -> str:
    if not status:
        return badge("unknown", MUTED)
    role, icon = mapping.get(status, ("warning", "?"))
    return badge(status.replace("_", " "), STATUS[role], icon)


def health_badge(status: str) -> str:
    role, icon, label = HEALTH.get(status, ("idle", "?", status))
    return badge(label, STATUS[role], icon)


def severity_badge(sev: str) -> str:
    role = {"critical": "critical", "serious": "serious", "warning": "warning", "info": "info"}.get(sev, "idle")
    return badge(sev, STATUS[role])


def strip(items: list[tuple[str, str, str]]) -> None:
    """Compact horizontal metric strip: (label, value, sub)."""
    cells = "".join(f"<div><div class='tp-kpi-label'>{esc(a)}</div><div class='tp-kpi-value' style='font-size:1.05rem'>"
                    f"{b}</div><div class='tp-kpi-sub'>{c}</div></div>" for a, b, c in items)
    st.markdown(f"<div class='tp-strip'>{cells}</div>", unsafe_allow_html=True)


def kv(pairs: list[tuple[str, object]]) -> None:
    cells = "".join(f"<div>{esc(k)}</div><div>{v if isinstance(v, Raw) else esc('—' if v is None else v)}</div>"
                    for k, v in pairs)
    st.markdown(f"<div class='tp-kv'>{cells}</div>", unsafe_allow_html=True)


class Raw(str):
    """Marks pre-escaped HTML for kv()."""


def mono(x) -> str:
    return f"<span class='tp-mono'>{esc(x)}</span>"


def fmt_ms(ms) -> str:
    if ms is None or (isinstance(ms, float) and pd.isna(ms)):
        return "—"
    ms = float(ms)
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s" if ms < 120_000 else f"{ms / 60000:.1f} min"


def fmt_bytes(n) -> str:
    if n is None or (isinstance(n, float) and pd.isna(n)):
        return "n/a"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pager(total: int, page_size: int, key: str) -> int:
    """Simple pager; returns the offset."""
    pages = max(1, -(-total // page_size))
    c1, c2 = st.columns([1, 5])
    page = c1.number_input("Page", 1, pages, 1, key=key, label_visibility="collapsed")
    c2.markdown(f"<div class='tp-caption' style='padding-top:8px'>page {page} of {pages} · {total:,} rows</div>",
                unsafe_allow_html=True)
    return (int(page) - 1) * page_size


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
        hoverlabel=dict(bgcolor=BG_2, bordercolor=BORDER, font=dict(color=TEXT, family="Inter")),
        bargap=0.25,
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=BORDER, title=xtitle,
                     tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=BORDER, title=ytitle,
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
