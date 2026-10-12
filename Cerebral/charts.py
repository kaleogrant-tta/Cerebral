"""Cerebral chart system.

One Plotly template plus a handful of helpers so every chart in the app
shares fonts, colours, grid, hover and spacing.  This module is standalone:
it never imports cerebral_public.  Hand it the palette (the dict parsed from
theme.css :root) or let from_css() read theme.css directly.

Typical use:

    import charts
    charts.apply_charts_template(PALETTE)        # once, at startup
    fig = px.bar(...)                            # picks the template up
    charts.show(fig, key="unique_key")           # tidy + render
"""
from __future__ import annotations

import re
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio

try:                                   # Streamlit is only needed by show()
    import streamlit as st
except Exception:                      # pragma: no cover
    st = None

TEMPLATE_NAME = "cerebral"
CSS_FILE = Path(__file__).parent / "theme.css"

BODY_FONT = "Inter, -apple-system, 'Segoe UI', Roboto, sans-serif"
MONO_FONT = "'IBM Plex Mono', ui-monospace, Menlo, Consolas, monospace"

# Consistent heights per chart type (pixels).
HEIGHTS = {"line": 340, "bar": 320, "scatter": 440, "pie": 280,
           "curve": 330, "small": 280, "heat": 360}
# Margins: tight; automargin on the axes grows them only when labels need it.
MARGIN = dict(l=0, r=8, t=16, b=0, pad=4)
MARGIN_TITLED = dict(l=0, r=8, t=44, b=0, pad=4)

_FALLBACK = {
    "accent": "#2F6F4F", "accent-soft": "#7FA98F", "warn": "#B4632B",
    "bad": "#9E3B32", "muted": "#7A8590", "ink": "#241812",
    "body": "#4A3B34", "rule": "#CFC3B4",
}
_FALLBACK_SERIES = ["#2F6F4F", "#B4632B", "#3B6E8F", "#8A5A9E", "#A8913C",
                    "#6E7F8D", "#9E3B32", "#4C8C7A", "#7A6A55"]


# --------------------------------------------------------------- theme parse
def _parse_root(css: str) -> dict:
    """Same logic as cerebral_public._parse_root: first :root block."""
    m = re.search(r":root\s*\{(.*?)\}", css, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        line = line.split("/*")[0]
        hit = re.match(r"\s*--([\w-]+)\s*:\s*([^;]+);", line)
        if hit:
            out[hit.group(1).strip()] = hit.group(2).strip()
    return out


def from_css(path=None) -> dict:
    """Parse the :root variables of theme.css into a palette dict."""
    p = Path(path) if path else CSS_FILE
    return _parse_root(p.read_text(encoding="utf-8"))


def _series(palette: dict) -> list:
    s = [palette[f"series-{i}"] for i in range(1, 10)
         if f"series-{i}" in palette]
    return s or list(_FALLBACK_SERIES)


def _rgba(color: str, alpha: float) -> str:
    c = (color or "").strip()
    if c.startswith("#") and len(c) in (4, 7):
        if len(c) == 4:
            c = "#" + "".join(ch * 2 for ch in c[1:])
        r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
        return f"rgba({r},{g},{b},{alpha})"
    return "rgba(0,0,0,%s)" % alpha


# ----------------------------------------------------------------- template
def make_template(palette: dict | None = None) -> go.layout.Template:
    """Build the Cerebral Plotly template from a palette dict."""
    pal = {**_FALLBACK, **(palette or {})}
    ink, body, muted, rule = pal["ink"], pal["body"], pal["muted"], pal["rule"]
    grid = _rgba(rule, 0.45)
    axis_common = dict(
        showline=False, zeroline=False, ticks="",
        gridcolor=grid, gridwidth=1, zerolinecolor=rule, linecolor=rule,
        tickfont=dict(family=MONO_FONT, size=11, color=muted),
        title=dict(font=dict(family=BODY_FONT, size=12, color=muted),
                   standoff=8),
        automargin=True,
    )
    layout = go.Layout(
        font=dict(family=BODY_FONT, size=12, color=body),
        colorway=_series(pal),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=MARGIN,
        title=dict(font=dict(family=BODY_FONT, size=14, color=ink),
                   x=0, xanchor="left", y=0.98, yanchor="top"),
        xaxis=dict(**axis_common, showgrid=False),
        yaxis=dict(**axis_common, showgrid=True),
        legend=dict(orientation="h", x=0, xanchor="left", y=1.02,
                    yanchor="bottom", title=dict(text=""),
                    font=dict(family=BODY_FONT, size=12, color=body),
                    bgcolor="rgba(0,0,0,0)", itemsizing="constant"),
        hovermode="closest",
        hoverlabel=dict(bgcolor="#FFFFFF", bordercolor=rule,
                        font=dict(family=BODY_FONT, size=12, color=ink),
                        align="left", namelength=-1),
        transition=dict(duration=250, easing="cubic-in-out"),
        uirevision="cerebral",
        bargap=0.28, bargroupgap=0.06,
        colorscale=dict(sequential=[[0, _rgba(pal["accent"], 0.12)],
                                    [1, pal["accent"]]]),
    )
    tmpl = go.layout.Template(layout=layout)
    tmpl.data.bar = [go.Bar(marker=dict(cornerradius=3, line=dict(width=0)))]
    tmpl.data.scatter = [go.Scatter(line=dict(width=2.25),
                                    marker=dict(size=7,
                                                line=dict(width=1.5,
                                                          color="#FFFFFF")))]
    tmpl.data.pie = [go.Pie(marker=dict(line=dict(color="#FFFFFF", width=2)),
                            textfont=dict(family=BODY_FONT, size=12))]
    return tmpl


def apply_charts_template(palette: dict | None = None) -> go.layout.Template:
    """Register the template as 'cerebral' and make it the default.  Call once
    at startup, before any figure is built."""
    global _PALETTE
    tmpl = make_template(palette)
    _PALETTE = {**_FALLBACK, **(palette or {})}
    pio.templates[TEMPLATE_NAME] = tmpl
    pio.templates.default = TEMPLATE_NAME
    return tmpl


_PALETTE: dict = dict(_FALLBACK)


def color(name: str = "accent") -> str:
    """A palette colour by theme.css variable name (accent, warn, series-2...)."""
    return _PALETTE.get(name) or _FALLBACK.get(name) or _FALLBACK_SERIES[0]


def _ensure_template(palette=None):
    if TEMPLATE_NAME not in pio.templates:
        apply_charts_template(palette if palette else _safe_css())


def _safe_css() -> dict:
    try:
        return from_css()
    except Exception:
        return {}


# ------------------------------------------------------------------ helpers
def fmt_money(compact: bool = True, decimals: int = 0) -> str:
    """Plotly tickformat for dollars.  compact -> $1.2k / $3M."""
    return "$~s" if compact else f"$,.{decimals}f"


def fmt_pct(decimals: int = 0, signed: bool = False) -> str:
    """Plotly tickformat for a 0-1 fraction shown as a percent."""
    return f"{'+' if signed else ''}.{decimals}%"


def _kind(fig: go.Figure) -> str:
    types = {getattr(t, "type", "") for t in fig.data}
    if types & {"pie", "sunburst", "treemap"}:
        return "pie"
    if types & {"heatmap"}:
        return "heat"
    if types == {"bar"} or types <= {"bar", "scatter"}:
        for t in fig.data:
            if t.type == "bar" and getattr(t, "orientation", None) == "h":
                return "hbar"
    if "scatter" in types:
        modes = {(getattr(t, "mode", None) or "lines") for t in fig.data
                 if t.type == "scatter"}
        if all("lines" not in m for m in modes):
            return "scatter"
        return "line"
    if "bar" in types:
        return "bar"
    return "line"


def apply(fig: go.Figure, height: int | None = None, kind: str | None = None,
          title: str | None = None, margin: dict | None = None) -> go.Figure:
    """Retrofit an existing figure: template, spacing, hover, uirevision.
    Never touches traces, so data, colours and hover text are unchanged.
    Height is only set when the figure has none (or when passed)."""
    _ensure_template()
    kind = kind or _kind(fig)
    fig.update_layout(template=TEMPLATE_NAME, uirevision="cerebral")
    fig.update_layout(margin=MARGIN_TITLED if title or fig.layout.title.text
                      else MARGIN)
    if margin:
        fig.update_layout(margin=margin)
    if title:
        fig.update_layout(title_text=title)
    if height is not None:
        fig.update_layout(height=height)
    elif fig.layout.height is None:
        fig.update_layout(height=HEIGHTS.get(kind, HEIGHTS["line"]))
    if kind in ("line", "bar") and len(fig.data) > 1 and \
            fig.layout.hovermode in (None, "closest") and \
            not any(getattr(t, "orientation", None) == "h" for t in fig.data):
        fig.update_layout(hovermode="x unified")
    if kind == "pie":
        fig.update_layout(margin=dict(l=0, r=0, t=8, b=0))
    if kind == "scatter":
        fig.update_xaxes(showgrid=True)
    if kind == "hbar":
        fig.update_xaxes(showgrid=True)
        fig.update_yaxes(showgrid=False)
    # px's default "legend title" and facet clutter
    if fig.layout.legend.title.text is None:
        fig.update_layout(legend_title_text="")
    return fig


def fig_base(title: str | None = None, height: int | None = None,
             kind: str = "line") -> go.Figure:
    """An empty figure carrying the template, ready for add_trace()."""
    _ensure_template()
    fig = go.Figure(layout=dict(template=TEMPLATE_NAME))
    return apply(fig, height=height or HEIGHTS.get(kind, HEIGHTS["line"]),
                 kind=kind, title=title)


plotly_config = {
    "displaylogo": False,
    "displayModeBar": "hover",
    "scrollZoom": False,
    "responsive": True,
    "modeBarButtonsToRemove": [
        "select2d", "lasso2d", "autoScale2d", "zoomIn2d", "zoomOut2d",
        "toggleSpikelines", "hoverClosestCartesian",
        "hoverCompareCartesian", "resetScale2d",
    ],
    "toImageButtonOptions": {"format": "png", "scale": 2},
}


# For charts whose axes are fixed (fixedrange): drop every range-changing button.
plotly_config_static = {**plotly_config, "modeBarButtonsToRemove":
                        plotly_config["modeBarButtonsToRemove"] +
                        ["zoom2d", "pan2d"]}


def show(fig: go.Figure, key: str, height: int | None = None,
         kind: str | None = None, margin: dict | None = None, **kw):
    """Tidy the figure and render it full-width with the shared config."""
    apply(fig, height=height, kind=kind, margin=margin)
    kw.setdefault("config", plotly_config)
    try:
        return st.plotly_chart(fig, width="stretch", key=key, **kw)
    except TypeError:                       # older Streamlit
        return st.plotly_chart(fig, use_container_width=True, key=key, **kw)
