"""Plotly figure builders for the control plane (pure functions, unit-tested)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from trendpulse.dashboard import components as ui
from trendpulse.orchestration import dag

NODE_W, NODE_H = 1.0, 0.62
COL_GAP = 1.32


def _wrap(label: str, width: int = 16) -> str:
    words, lines, cur = label.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return "<br>".join(lines[:2])


def dag_layout() -> dict[str, tuple[float, float]]:
    """Columns = stage groups (left → right), rows = order within group (top → bottom)."""
    pos: dict[str, tuple[float, float]] = {}
    for gi, g in enumerate(dag.GROUPS):
        members = [s.name for s in dag.STAGE_SPECS if s.group == g]
        for ri, name in enumerate(members):
            pos[name] = (gi * COL_GAP, -ri * 1.0)
    return pos


def dag_figure(state: pd.DataFrame, selected: str | None = None, height: int = 400) -> go.Figure:
    """Pipeline DAG coloured by each stage's recorded status for one execution."""
    pos = dag_layout()
    fig = go.Figure()
    st = state.set_index("stage")
    # edges
    for up, down, kind in dag.edges():
        (x0, y0), (x1, y1) = pos[up], pos[down]
        if x0 == x1:
            xs, ys = [x0, x1], [y0 - NODE_H / 2, y1 + NODE_H / 2]
        else:
            xs, ys = [x0 + NODE_W / 2, (x0 + x1) / 2, (x0 + x1) / 2, x1 - NODE_W / 2], [y0, y0, y1, y1]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                                 line=dict(color=ui.FAIL_TEXT if kind == "failure" else "#4B5A73", width=1.5,
                                           dash="dot" if kind == "failure" else "solid")))
        fig.add_annotation(x=xs[-1], y=ys[-1], ax=xs[-2], ay=ys[-2], xref="x", yref="y", axref="x", ayref="y",
                           showarrow=True, arrowhead=2, arrowsize=1, arrowwidth=1.5, text="",
                           arrowcolor=ui.FAIL_TEXT if kind == "failure" else "#4B5A73")
    # group headers
    for gi, g in enumerate(dag.GROUPS):
        fig.add_annotation(x=gi * COL_GAP, y=0.55, text=g.upper(), showarrow=False,
                           font=dict(size=10, color=ui.MUTED, family="Inter"))
    # nodes
    for name, (x, y) in pos.items():
        row = st.loc[name] if name in st.index else None
        status = row.status if row is not None else "not_recorded"
        color = ui.NODE_FILL.get(status, ui.IDLE)
        sel = name == selected
        fig.add_shape(type="rect", x0=x - NODE_W / 2, x1=x + NODE_W / 2, y0=y - NODE_H / 2, y1=y + NODE_H / 2,
                      line=dict(color=ui.ACCENT if sel else color, width=3 if sel else 1.5),
                      fillcolor=ui.PANEL, layer="below")
        fig.add_shape(type="rect", x0=x - NODE_W / 2, x1=x - NODE_W / 2 + 0.05, y0=y - NODE_H / 2,
                      y1=y + NODE_H / 2, line=dict(width=0), fillcolor=color, layer="below")
        label = dag.SPEC_BY_NAME[name].label
        sub = status.replace("_", " ")
        if row is not None and pd.notna(row.duration_ms) and status not in {"pending", "not_recorded"}:
            sub += f" · {ui.fmt_ms(row.duration_ms)}"
        fig.add_annotation(x=x + 0.03, y=y + 0.08, text=f"<b>{_wrap(label)}</b>", showarrow=False,
                           font=dict(size=11, color=ui.TEXT, family="Inter"))
        fig.add_annotation(x=x + 0.03, y=y - 0.2, text=sub, showarrow=False,
                           font=dict(size=10, color=color if status != "not_recorded" else ui.MUTED,
                                     family="JetBrains Mono, ui-monospace, monospace"))
    # invisible click targets carrying the stage name
    names = list(pos)
    custom = []
    for n in names:
        r = st.loc[n] if n in st.index else None
        custom.append([n, dag.SPEC_BY_NAME[n].label, r.status if r is not None else "not_recorded",
                       ui.fmt_ms(r.duration_ms) if r is not None else "—",
                       "" if r is None or pd.isna(r.records_in) else int(r.records_in),
                       "" if r is None or pd.isna(r.records_out) else int(r.records_out),
                       "" if r is None or pd.isna(r.attempts) else int(r.attempts)])
    fig.add_trace(go.Scatter(
        x=[pos[n][0] for n in names], y=[pos[n][1] for n in names], mode="markers",
        marker=dict(size=48, symbol="square", color="rgba(0,0,0,0)", line=dict(width=0)),
        customdata=custom, showlegend=False,
        hovertemplate="<b>%{customdata[1]}</b><br>%{customdata[2]} · %{customdata[3]}<br>in %{customdata[4]} → "
                      "out %{customdata[5]} · attempts %{customdata[6]}<extra>click to inspect</extra>"))
    fig.update_xaxes(visible=False, range=[-0.6, (len(dag.GROUPS) - 1) * COL_GAP + 0.6], fixedrange=True)
    fig.update_yaxes(visible=False, range=[-2.4, 0.75], fixedrange=True)
    fig.update_layout(height=height, margin=dict(l=4, r=4, t=4, b=4), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", dragmode=False, clickmode="event+select",
                      hoverlabel=dict(bgcolor=ui.BG_2, bordercolor=ui.BORDER, font=dict(color=ui.TEXT)))
    return fig


LAYER_COL = {"Source": 0, "Operational": 1, "Bronze": 2, "Silver": 3, "Gold": 4}
LAYER_COLOR = {"Source": "#9085e9", "Operational": "#64748B", "Bronze": "#c98500", "Silver": "#94A3B8",
               "Gold": "#F59E0B"}
L_GAP, L_W, L_H, L_ROW = 2.35, 2.0, 0.4, 0.55


def lineage_layout(nodes: pd.DataFrame) -> dict[str, tuple[float, float]]:
    pos: dict[str, tuple[float, float]] = {}
    cols: dict[int, list[str]] = {}
    for r in nodes.sort_values(["layer", "dataset"]).itertuples(index=False):
        cols.setdefault(LAYER_COL.get(r.layer, 2), []).append(r.dataset)
    for c, names in cols.items():
        n = len(names)
        for i, name in enumerate(names):
            pos[name] = (c * L_GAP, -(i - (n - 1) / 2) * L_ROW)
    return pos


def lineage_figure(nodes: pd.DataFrame, edges: pd.DataFrame, selected: str | None = None,
                   related: set[str] | None = None, height: int = 560) -> go.Figure:
    related = related or set()
    pos = lineage_layout(nodes)
    layer_of = dict(zip(nodes.dataset, nodes.layer, strict=True))
    fig = go.Figure()
    for r in edges.itertuples(index=False):
        (x0, y0), (x1, y1) = pos[r.source], pos[r.target]
        on = selected is not None and {r.source, r.target} <= (related | {selected})
        xs = [x0 + L_W / 2, x0 + L_W / 2 + 0.12, x1 - L_W / 2 - 0.12, x1 - L_W / 2]
        fig.add_trace(go.Scatter(x=xs, y=[y0, y0, y1, y1], mode="lines", hoverinfo="skip", showlegend=False,
                                 line=dict(color=ui.ACCENT if on else "#2A3850", width=1.8 if on else 1)))
    for name, (x, y) in pos.items():
        dim = selected is not None and name not in related and name != selected
        lay = layer_of[name]
        col = LAYER_COLOR.get(lay, ui.MUTED)
        fig.add_shape(type="rect", x0=x - L_W / 2, x1=x + L_W / 2, y0=y - L_H / 2, y1=y + L_H / 2,
                      fillcolor=ui.PANEL, layer="below",
                      line=dict(color=ui.ACCENT if name == selected else ("#2A3850" if dim else col),
                                width=2.5 if name == selected else 1))
        label = name.replace("source:youtube.", "YouTube ").replace("(chart=mostPopular)", "(chart)")
        fig.add_annotation(x=x, y=y, text=label, showarrow=False,
                           font=dict(size=10, family="JetBrains Mono, ui-monospace, monospace", color=ui.MUTED if dim else ui.TEXT))
    fig.add_trace(go.Scatter(x=[p[0] for p in pos.values()], y=[p[1] for p in pos.values()], mode="markers",
                             marker=dict(size=30, symbol="square", color="rgba(0,0,0,0)"),
                             customdata=[[n, layer_of[n]] for n in pos], showlegend=False,
                             hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}<extra>click to trace</extra>"))
    top = max(p[1] for p in pos.values()) + 0.55
    for label, c in (("SOURCE", 0), ("INGESTION STATE", 1), ("BRONZE", 2), ("SILVER", 3), ("GOLD", 4)):
        fig.add_annotation(x=c * L_GAP, y=top, text=label, showarrow=False, font=dict(size=10, color=ui.MUTED))
    fig.update_xaxes(visible=False, range=[-L_W / 2 - 0.1, 4 * L_GAP + L_W / 2 + 0.1], fixedrange=True)
    fig.update_yaxes(visible=False, fixedrange=True)
    fig.update_layout(height=height, margin=dict(l=4, r=4, t=4, b=4), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", dragmode=False, clickmode="event+select",
                      hoverlabel=dict(bgcolor=ui.BG_2, bordercolor=ui.BORDER, font=dict(color=ui.TEXT)))
    return fig


def timeline_figure(state: pd.DataFrame, tz: str = "UTC", height: int = 420) -> go.Figure:
    """Gantt of recorded stage executions for one run."""
    d = state.dropna(subset=["started_at", "finished_at"]).copy()
    fig = go.Figure()
    if d.empty:
        return fig
    d["start"] = pd.to_datetime(d.started_at, utc=True).dt.tz_convert(tz)
    d["end"] = pd.to_datetime(d.finished_at, utc=True).dt.tz_convert(tz)
    d["dur_ms"] = (d.end - d.start).dt.total_seconds() * 1000
    d = d.iloc[::-1]
    fig.add_trace(go.Bar(
        y=d.label, x=d.dur_ms.clip(lower=1), base=d.start, orientation="h",
        marker=dict(color=[ui.NODE_FILL.get(s, ui.IDLE) for s in d.status], cornerradius=3),
        customdata=d[["status", "attempts", "records_in", "records_out"]].fillna(""),
        hovertemplate="<b>%{y}</b> — %{customdata[0]}<br>%{base|%H:%M:%S.%L} · %{x:.0f} ms<br>attempts "
                      "%{customdata[1]} · in %{customdata[2]} → out %{customdata[3]}<extra></extra>"))
    fig.update_xaxes(type="date")
    return ui.style_fig(fig, height, legend=False, xtitle=f"Wall-clock time ({tz})")
