"""
Brand Efficiency Index tab.

BEI = a brand's share of category sales divided by its share of category
inventory, within a store. Above 1.0 the brand sells faster than its shelf
presence implies; below 1.0 it occupies more space or capital than its sales
justify.

The scatter is the whole index in one picture: sales share on one axis,
capital share on the other, break-even on the diagonal. Distance from the
line IS the BEI, so the chart needs no separate legend for it.

Reads dash_bei and dash_bei_coverage, both built by publish.py. Categories
whose brand attribution is too poor to trust are hidden rather than shown
with numbers nobody should act on — accessories in particular often reach
the sales fact without a consistent brand.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

MIN_COVERAGE = 0.90


def collapse(d: pd.DataFrame) -> pd.DataFrame:
    """Roll several stores up to one row per brand within a category.

    dash_bei computes shares WITHIN a store, so rows from different stores
    have different denominators. Plotting them together would put four
    incompatible scales on one pair of axes and show the same brand four
    times. Summing the components and recomputing the shares gives one
    honest chain-level view.

    Slight understatement is inherited here: the published table already
    dropped rows below its floors, so a category total rebuilt from what
    survived is marginally low. It moves every brand in a category by the
    same factor, so the ranking is unaffected.
    """
    g = (d.groupby(["category", "brand"], as_index=False)
          .agg(net=("net", "sum"), gm=("gm", "sum"), units=("units", "sum"),
               inv_cost=("inv_cost", "sum"), qoh=("qoh", "sum"),
               skus=("skus", "sum"), stores=("store_key", "nunique"),
               window_days=("window_days", "max")))

    for src, dst in (("net", "cat_net"), ("gm", "cat_gm"),
                     ("units", "cat_units"), ("inv_cost", "cat_inv_cost"),
                     ("qoh", "cat_qoh"), ("skus", "cat_skus")):
        g[dst] = g.groupby("category")[src].transform("sum")

    def share(n, d_):
        return (g[n] / g[d_]).where(g[d_] > 0)

    g["sales_share"] = share("net", "cat_net")
    g["gm_share"] = share("gm", "cat_gm")
    g["capital_share"] = share("inv_cost", "cat_inv_cost")
    g["unit_share"] = share("qoh", "cat_qoh")
    g["assort_share"] = share("skus", "cat_skus")

    thick = g.inv_cost >= 200
    g["bei_capital"] = (g.sales_share / g.capital_share).where(thick)
    g["bei_margin"] = (g.gm_share / g.capital_share).where(thick)
    g["bei_assort"] = (g.sales_share / g.assort_share).where(g.skus >= 1)
    rate = g.units / g.window_days
    g["dos"] = (g.qoh / rate).where(rate > 0)
    g["stocked_out"] = (g.net > 0) & (g.inv_cost < 200)
    return g


def render_bei(*, q, keys, stores, heading, table_exists,
               accent="#2F6F4F", series=None) -> None:
    if not table_exists("dash_bei"):
        st.info("Brand efficiency needs a newer data file than the one "
                "currently published. It will appear after the next refresh.")
        return

    where = (f" WHERE store_key IN ({','.join(map(str, keys))})"
             if keys and len(keys) < len(stores) else "")
    bei = q(f"SELECT * FROM dash_bei{where}")
    if bei.empty:
        st.info("No inventory snapshot in the published file yet.")
        return

    snap = pd.to_datetime(bei.snapshot_date.iloc[0]).date()
    days = int(bei.window_days.iloc[0])

    # --- which categories are trustworthy --------------------------------
    trusted = None
    if table_exists("dash_bei_coverage"):
        cov = q("SELECT * FROM dash_bei_coverage")
        trusted = set(cov[cov.cost_coverage >= MIN_COVERAGE].category)
        hidden = sorted(set(cov.category) - trusted)
        bei = bei[bei.category.isin(trusted)]
    else:
        hidden = []

    st.caption(
        f"Inventory as of {snap:%b %d, %Y} · sales from the {days} days "
        f"ending then. Brands are ranked within their own category and "
        f"store, so a big category cannot outrank a small one.")

    cats = (bei.groupby("category").net.sum()
               .sort_values(ascending=False).index.tolist())
    if not cats:
        st.warning("No category has reliable brand attribution.")
        return

    # Defaulting to one category rather than all: plotting every category at
    # once is several hundred points and reads as noise.
    cat = st.selectbox("Category", cats, index=0, key="bei_cat")

    # Shares in dash_bei are within-store. With more than one store selected
    # they must be rebuilt chain-wide, or the same brand appears once per
    # store against four different denominators.
    multi = len([k for k in keys if k in stores]) > 1 if keys else True
    top = collapse(bei[bei.category == cat]) if multi \
        else bei[bei.category == cat].copy()
    if multi:
        st.caption("Showing all selected stores combined. Pick a single "
                   "store in the sidebar to see it on its own.")
    d = top

    left, right = st.columns([3, 2])

    # ------------------------------------------------------------ scatter
    with left:
        heading("Sales share vs shelf share")
        st.markdown(
            "> **How to read this.** Each dot is a brand. The diagonal is "
            "break-even — sales share exactly matching inventory share. "
            "Above the line a brand earns more than its space costs; below "
            "it, the shelf is working harder than the brand is. Distance "
            "from the line is the index.")

        p = d[(d.capital_share > 0) & d.bei_capital.notna()].copy()
        if p.empty:
            st.caption("No brand in this category has enough stock on hand "
                       "to compute a ratio.")
        else:
            hi = float(max(p.sales_share.max(), p.capital_share.max())) * 1.1
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=[0, hi], y=[0, hi], mode="lines",
                line=dict(color="#999", width=1, dash="dash"),
                hoverinfo="skip", showlegend=False))
            fig.add_trace(go.Scatter(
                x=p.capital_share, y=p.sales_share, mode="markers+text",
                text=p.brand, textposition="top center",
                textfont=dict(size=9),
                marker=dict(
                    size=(p.net / p.net.max() * 26 + 7),
                    color=p.bei_capital, colorscale="RdYlGn",
                    cmid=1.0, line=dict(width=1, color="white"),
                    colorbar=dict(title="BEI", thickness=12)),
                customdata=p[["net", "inv_cost", "bei_capital",
                              "dos"]].to_numpy(),
                hovertemplate=(
                    "<b>%{text}</b><br>"
                    "sales share %{y:.1%}<br>"
                    "shelf share %{x:.1%}<br>"
                    "net $%{customdata[0]:,.0f}<br>"
                    "stock $%{customdata[1]:,.0f} at cost<br>"
                    "BEI %{customdata[2]:.2f}<br>"
                    "%{customdata[3]:.0f} days of supply"
                    "<extra></extra>"),
                showlegend=False))
            fig.update_layout(
                height=460, margin=dict(l=10, r=10, t=10, b=10),
                xaxis=dict(title="share of category inventory (at cost)",
                           tickformat=".0%", range=[0, hi]),
                yaxis=dict(title="share of category sales",
                           tickformat=".0%", range=[0, hi]),
                plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Dot size is net sales. Colour is the index itself — "
                       "red below break-even, green above.")

    # -------------------------------------------------------- worst offenders
    with right:
        heading("Costing you the most space")
        over = d[(d.inv_cost >= 1000) & d.bei_capital.notna()] \
            .nsmallest(10, "bei_capital")
        if over.empty:
            st.caption("Nothing in this category is carrying enough stock to "
                       "flag.")
        else:
            show = over[["brand", "net", "inv_cost", "bei_capital"]].copy()
            show.columns = ["Brand", "Net", "Stock $", "BEI"]
            st.dataframe(
                show.style.format({"Net": "${:,.0f}", "Stock $": "${:,.0f}",
                                   "BEI": "{:.2f}"})
                    .background_gradient(subset=["BEI"], cmap="RdYlGn",
                                         vmin=0, vmax=2),
                hide_index=True, use_container_width=True)
            st.caption(
                f"Ten lowest-index brands in {cat} holding $1,000+ of stock. "
                f"An index of 0.15 means the brand takes roughly seven times "
                f"the shelf its sales support.")

    st.divider()

    # ------------------------------------------------------------- detail
    heading("What to act on")
    view = st.radio(
        "View", ["Stocked out", "Running out", "Dead stock", "Range bloat"],
        horizontal=True, label_visibility="collapsed", key="bei_view")

    # Deliberately per-store, unlike the panels above: running out of a
    # brand is something you fix at one store, not on average.
    base = bei if st.checkbox("All categories", value=False,
                              key="bei_all") else bei[bei.category == cat]
    scope = base.copy()
    scope["Store"] = scope.store_key.map(stores)

    def show(df, cols, names, fmt, note):
        if df.empty:
            st.caption("Nothing to show here — which is the good outcome.")
            return
        t = df[cols].copy()
        t.columns = names
        st.dataframe(t.style.format(fmt), hide_index=True,
                     use_container_width=True)
        st.caption(note)

    if view == "Stocked out":
        show(scope[scope.stocked_out & (scope.net >= 500)]
             .nlargest(20, "net"),
             ["Store", "category", "brand", "net", "inv_cost", "qoh"],
             ["Store", "Category", "Brand", "Net", "Stock $", "Units"],
             {"Net": "${:,.0f}", "Stock $": "${:,.0f}", "Units": "{:,.0f}"},
             "Sold well, almost nothing left. No index is shown because "
             "dividing by a near-zero denominator produces a huge number "
             "that means 'empty shelf', not 'efficient'.")

    elif view == "Running out":
        show(scope[scope.dos.notna() & (scope.dos < 21)
                   & (scope.net >= 1000) & ~scope.stocked_out]
             .nsmallest(20, "dos"),
             ["Store", "category", "brand", "net", "qoh", "dos",
              "bei_capital"],
             ["Store", "Category", "Brand", "Net", "Units", "Days left",
              "BEI"],
             {"Net": "${:,.0f}", "Units": "{:,.0f}", "Days left": "{:.0f}",
              "BEI": "{:.2f}"},
             "Days left is stock on hand at the selling rate of the window. "
             "A high index here is a reorder, not a congratulation.")

    elif view == "Dead stock":
        show(scope[(scope.net == 0) & (scope.inv_cost > 0)]
             .nlargest(20, "inv_cost"),
             ["Store", "category", "brand", "inv_cost", "qoh", "skus"],
             ["Store", "Category", "Brand", "Stock $", "Units", "SKUs"],
             {"Stock $": "${:,.0f}", "Units": "{:,.0f}", "SKUs": "{:,.0f}"},
             "On the shelf, nothing sold in the window. Capital sitting "
             "still.")

    else:
        show(scope[(scope.skus >= 3) & scope.bei_assort.notna()
                   & (scope.net > 0)].nsmallest(20, "bei_assort"),
             ["Store", "category", "brand", "skus", "assort_share",
              "sales_share", "bei_assort"],
             ["Store", "Category", "Brand", "SKUs", "Range share",
              "Sales share", "Index"],
             {"SKUs": "{:,.0f}", "Range share": "{:.1%}",
              "Sales share": "{:.1%}", "Index": "{:.2f}"},
             "Many SKUs, little of the category's sales. Breadth costs "
             "working capital and shelf whether or not it earns.")

    if hidden:
        st.caption(
            f"Hidden: {', '.join(hidden)}. Less than {MIN_COVERAGE:.0%} of "
            f"the stock in these categories could be matched to a brand, so "
            f"their shares would be wrong in ways that look plausible.")
