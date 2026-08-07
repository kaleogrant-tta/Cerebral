"""
heat.py -- heat-shaded tables for Cerebral, in the style of the Tableau
decks (blue/green/slate gradients with contrast-flipped text).

Deliberately does NOT use Styler.background_gradient, which needs matplotlib.
Colours are interpolated here in pure Python, so this works on the Streamlit
Cloud image without adding a dependency.

    from heat import heat_table, PALETTES

    st.dataframe(heat_table(df, {"Repeat rate": "blue",
                                 "LTV": "green"},
                            fmt={"LTV": "${:,.0f}",
                                 "Repeat rate": "{:.1%}"}))

Scaling is per column by default (each column normalised over its own
range), matching how the decks shade. Pass axis="table" to scale a block of
columns against a shared range -- right when the columns are comparable
(e.g. basket 1/2/3 values) and wrong when they are not.
"""

from __future__ import annotations

import pandas as pd

# (light, dark) endpoints. Light end is near-white so low values stay legible.
PALETTES = {
    "blue":   ((222, 235, 247), (33, 85, 140)),
    "green":  ((229, 245, 224), (35, 110, 60)),
    "slate":  ((233, 236, 239), (55, 65, 81)),
    "aqua":   ((224, 255, 250), (0, 150, 130)),     # brand Aqua #00FFD4
    "red":    ((255, 232, 224), (190, 45, 10)),     # brand Red #F73400
    "brown":  ((240, 234, 231), (92, 62, 52)),      # brand Brown #5C3E34
    "grey":   ((242, 242, 242), (120, 120, 120)),
}

# Below this relative luminance the cell gets white text.
_DARK_CUTOFF = 0.55


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _luminance(rgb):
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _css(rgb):
    fg = "#ffffff" if _luminance(rgb) < _DARK_CUTOFF else "#111111"
    return "background-color: rgb(%d,%d,%d); color: %s;" % (*rgb, fg)


def _norm(series, lo=None, hi=None, reverse=False):
    s = pd.to_numeric(series, errors="coerce")
    lo = s.min() if lo is None else lo
    hi = s.max() if hi is None else hi
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return pd.Series([0.5] * len(s), index=s.index).where(s.notna())
    t = (s - lo) / (hi - lo)
    return (1 - t) if reverse else t


def heat_table(df: pd.DataFrame,
               shading: dict,
               fmt: dict | None = None,
               reverse: tuple = (),
               axis: str = "column",
               na_rep: str = "-"):
    """Return a Styler with per-cell background shading.

    df       -- values must be numeric in the shaded columns; format via fmt
    shading  -- {column: palette_name}; columns absent from df are ignored
    fmt      -- {column: format string}, e.g. "${:,.0f}" or "{:.1%}"
    reverse  -- columns where LOW values should read as "good" (dark)
    axis     -- "column" scales each column separately;
                "table" scales every shaded column against one shared range
    """
    shading = {k: v for k, v in (shading or {}).items() if k in df.columns}
    styler = df.style

    shared_lo = shared_hi = None
    if axis == "table" and shading:
        block = pd.concat([pd.to_numeric(df[c], errors="coerce")
                           for c in shading], axis=0)
        shared_lo, shared_hi = block.min(), block.max()

    for col, pal_name in shading.items():
        lo, hi = PALETTES.get(pal_name, PALETTES["slate"])
        t = _norm(df[col],
                  shared_lo if axis == "table" else None,
                  shared_hi if axis == "table" else None,
                  reverse=col in reverse)

        def _style(_s, _t=t, _lo=lo, _hi=hi):
            return [_css(_lerp(_lo, _hi, v)) if pd.notna(v) else ""
                    for v in _t]

        styler = styler.apply(_style, subset=[col])

    if fmt:
        styler = styler.format({k: v for k, v in fmt.items()
                                if k in df.columns}, na_rep=na_rep)

    styler = styler.set_properties(**{"text-align": "right"})
    return styler


def show_heat(st, df, shading, fmt=None, reverse=(), axis="column",
              hide_index=True, height=None):
    """st.dataframe wrapper that tolerates both width APIs."""
    sty = heat_table(df, shading, fmt, reverse, axis)
    kw = {"hide_index": hide_index}
    if height:
        kw["height"] = height
    try:
        return st.dataframe(sty, width="stretch", **kw)
    except TypeError:
        return st.dataframe(sty, use_container_width=True, **kw)


def matrix_heat(st, df, fmt="{:.1%}", palette="blue", reverse=False,
                axis="table"):
    """Shade a whole pivot table (index labels kept, all columns numeric)."""
    shading = {c: palette for c in df.columns}
    rev = tuple(df.columns) if reverse else ()
    sty = heat_table(df.reset_index(), shading,
                     {c: fmt for c in df.columns}, rev, axis)
    try:
        return st.dataframe(sty, hide_index=True, width="stretch")
    except TypeError:
        return st.dataframe(sty, hide_index=True, use_container_width=True)
