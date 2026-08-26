"""
brand_roi.py -- brand-level discount ROI simulator for the Promo Lab tab.

Promo Lab's existing sections answer "which categories leak customers and
would a win-back offer pay for itself". This one answers a different
question: if we discount a SPECIFIC BRAND by some depth, how much more
volume do we need to sell before margin dollars are back where they started?

THE MODEL

One formula covers both a shelf-wide markdown and a targeted offer. For a
brand with baseline net sales N and margin rate m:

    d  discount depth        the markdown, 5-20%
    r  reach                 share of BASELINE units that get the discount.
                             1.0 = shelf-wide (existing volume is marked
                             down too). Below 1.0 = a targeted offer only
                             some buyers see.
    L  lift                  incremental units as a share of baseline. Every
                             incremental unit is assumed to be discounted.

    change in margin $  =  N * [ L*(m - d) - r*d ]
    change in net sales =  N * [ L*(1 - d) - r*d ]
    discount spend      =  N * (r + L) * d

Setting the margin change to zero gives the headline number:

    BREAKEVEN LIFT  L* = r*d / (m - d)

At r = 1 that collapses to d/(m-d): a 46%-margin brand marked down 15%
needs +48% units just to stand still. At r = 0 it is zero, because a
discount that only ever reaches genuinely incremental buyers is accretive
on any unit sold above cost. Reach is therefore the single most important
input on the page and it is deliberately a slider, not an assumption.

If d >= m the brand sells below cost at that depth and no volume rescues
it. The tab says so instead of printing a nonsense number.

DATA

  dash_bei             spine. net, gm, units, inv_cost, qoh, dos, window_days.
                       Gives margin rate, average unit price, cost base and
                       an inventory feasibility check over a defined window.
  dash_discount_brand  realized discount depth, shown as an anchor only.
                       Its discount column is basket discount smeared across
                       brands by net-sales share, so it is directional for
                       "is this brand already being discounted" and is NOT a
                       per-brand cost figure. The page says this out loud.
  dash_promo_brand     redeemer cohort churn, for whether past discounting on
                       this brand bought a lasting customer.

None of these carry a date grain except dash_bei's own window, so this
section is whole-period and honours the store filter only, never the weeks
slider. Same contract as the Discounting tab.

Wire into render_promo_lab() as a fourth sub-tab:

    from brand_roi import render_brand_roi
    ...
    with tab4:
        render_brand_roi(q=q, keys=keys, stores=STORES,
                         heading=heading, table_exists=table_exists,
                         accent=ACCENT, series=SERIES)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Brands below this in windowed net sales are noise: a handful of units,
# a margin rate that swings on rounding, and no promo would ever be built
# on them. Keeping them in the picker just makes the list unusable.
MIN_NET = 2_000.0

DEPTH_MIN, DEPTH_MAX = 5, 20


# --------------------------------------------------------------- helpers

def _money(x: float) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


def _signed(x: float) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return ("+" if x >= 0 else "-") + f"${abs(x):,.0f}"


def _pct(x: float, places: int = 1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x * 100:.{places}f}%"


def _note(html: str) -> None:
    st.markdown(f'<p class="note">{html}</p>', unsafe_allow_html=True)


def _real_keys(keys: list[int]) -> list[int]:
    """Drop the chain rollup unless it is the only thing selected.

    store_key 0 is the chain aggregate. Summing it alongside 1-4 would
    double every figure on the page.
    """
    real = [k for k in keys if k != 0]
    return real if real else list(keys)


# ------------------------------------------------------------ data loads

def _derive(g: pd.DataFrame, window: float) -> pd.DataFrame:
    """Rates and per-unit figures. Never average a rate across source rows —
    sum the additive columns first, divide afterwards."""
    g = g.copy()
    g["margin_rate"] = np.where(g["net"] > 0, g["gm"] / g["net"], np.nan)
    g["unit_price"] = np.where(g["units"] > 0, g["net"] / g["units"], np.nan)
    g["cogs"] = g["net"] - g["gm"]
    g["window_days"] = window
    # Days of supply at the current rate, recomputed rather than averaged
    # from the source rows, which are per store x category.
    daily_units = np.where(window > 0, g["units"] / window, np.nan)
    g["dos"] = np.where(daily_units > 0, g["qoh"] / daily_units, np.nan)
    return g


def _load_detail(q, keys: list[int], brand: str, window: float) -> pd.DataFrame:
    """One row per category for a single brand.

    A brand's blended margin can be a fiction. Rythm runs vape near 47% and
    flower near 27%; the blend describes no promo anyone would actually run.
    Scope the simulation to the category being discounted.
    """
    ks = ",".join(str(k) for k in _real_keys(keys))
    safe = brand.replace("'", "''")
    df = q(f"""
        SELECT category, SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
               SUM(inv_cost) AS inv_cost, SUM(qoh) AS qoh
        FROM dash_bei
        WHERE store_key IN ({ks}) AND brand = '{safe}'
        GROUP BY 1 ORDER BY 2 DESC
    """)
    if df.empty:
        return df
    return _derive(df, window)


def _load_bei(q, keys: list[int]) -> pd.DataFrame:
    """Brand spine, collapsed across the selected stores.

    dash_bei is one row per store x category x brand. A brand that sells in
    two categories at three stores is six rows. Sum the additive columns and
    recompute the rates afterwards; never average a rate across rows.
    """
    ks = ",".join(str(k) for k in _real_keys(keys))
    df = q(f"""
        SELECT brand, category, net, gm, units, inv_cost, qoh, dos,
               stocked_out, window_days
        FROM dash_bei
        WHERE store_key IN ({ks})
    """)
    if df.empty:
        return df

    window = float(pd.to_numeric(df["window_days"], errors="coerce").max() or 0)

    g = (df.groupby("brand", as_index=False)
           .agg(net=("net", "sum"),
                gm=("gm", "sum"),
                units=("units", "sum"),
                inv_cost=("inv_cost", "sum"),
                qoh=("qoh", "sum"),
                categories=("category", "nunique"),
                top_category=("category", "first"),
                stocked_out=("stocked_out", "sum")))

    g = g[g["net"] >= MIN_NET].copy()
    g = _derive(g, window)
    return g.sort_values("net", ascending=False)


def _load_realized(q, keys: list[int], brand: str) -> dict | None:
    ks = ",".join(str(k) for k in _real_keys(keys))
    safe = brand.replace("'", "''")
    df = q(f"""
        SELECT SUM(net) AS net, SUM(discount) AS discount,
               SUM(baskets) AS baskets, SUM(units) AS units
        FROM dash_discount_brand
        WHERE store_key IN ({ks}) AND brand = '{safe}'
    """)
    if df.empty or pd.isna(df.iloc[0]["net"]):
        return None
    r = df.iloc[0]
    gross = float(r["net"] or 0) + float(r["discount"] or 0)
    return {
        "net": float(r["net"] or 0),
        "discount": float(r["discount"] or 0),
        "rate": (float(r["discount"] or 0) / gross) if gross > 0 else np.nan,
        "baskets": float(r["baskets"] or 0),
    }


def _load_cohort(q, keys: list[int], brand: str) -> pd.Series | None:
    ks = ",".join(str(k) for k in _real_keys(keys))
    safe = brand.replace("'", "''")
    df = q(f"""
        SELECT SUM(customers) AS customers,
               SUM(repeat_buyers) AS repeat_buyers,
               SUM(spend_sum) AS spend_sum,
               SUM(gm_sum) AS gm_sum,
               SUM(churned_60) AS churned_60,
               SUM(churned_90) AS churned_90
        FROM dash_promo_brand
        WHERE store_key IN ({ks}) AND brand = '{safe}'
    """)
    if df.empty or pd.isna(df.iloc[0]["customers"]):
        return None
    return df.iloc[0]


# ------------------------------------------------------------ the model

def simulate(net: float, margin_rate: float, depth: float,
             reach: float, lift: float) -> dict:
    """Return the full before/after picture. See the module docstring."""
    m, d, r, L = margin_rate, depth, reach, lift

    base_margin = net * m
    base_cogs = net - base_margin

    d_margin = net * (L * (m - d) - r * d)
    d_net = net * (L * (1 - d) - r * d)
    spend = net * (r + L) * d
    d_cogs = base_cogs * L

    breakeven = (r * d) / (m - d) if m > d else np.inf

    return {
        "base_net": net,
        "base_cogs": base_cogs,
        "base_margin": base_margin,
        "new_net": net + d_net,
        "new_cogs": base_cogs + d_cogs,
        "new_margin": base_margin + d_margin,
        "d_net": d_net,
        "d_cogs": d_cogs,
        "d_margin": d_margin,
        "spend": spend,
        "breakeven": breakeven,
        "sells_below_cost": d >= m,
    }


# ------------------------------------------------------------- rendering

def _breakeven_curve(row: pd.Series, reach: float, depth: float,
                     lift: float, accent: str | None) -> go.Figure:
    m = float(row["margin_rate"])
    depths = np.arange(DEPTH_MIN, DEPTH_MAX + 0.5, 0.5) / 100.0
    req = np.where(depths < m, (reach * depths) / (m - depths), np.nan)

    line = accent or "#2E7D74"
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=depths * 100, y=req * 100, mode="lines",
        line=dict(color=line, width=3), name="Volume needed to break even",
        hovertemplate="Discount %{x:.0f}%<br>Needs %{y:,.0f}% more units<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=[depth * 100], y=[lift * 100], mode="markers",
        marker=dict(color="#B4472F", size=13, symbol="diamond"),
        name="What you are assuming",
        hovertemplate="Your plan: %{x:.0f}% off, %{y:,.0f}% more units<extra></extra>"))

    fig.update_layout(
        height=340, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.14, x=0),
        xaxis=dict(title="Discount depth", ticksuffix="%",
                   gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Extra units needed", ticksuffix="%",
                   gridcolor="rgba(0,0,0,0.07)"))
    return fig


def render_brand_roi(q, keys, stores, heading=None, table_exists=None,
                     accent=None, series=None) -> None:
    title = "Does discounting this brand pay for itself?"
    if heading:
        try:
            heading(title)
        except TypeError:
            st.markdown(f"#### {title}")
    else:
        st.markdown(f"#### {title}")

    if table_exists and not table_exists("dash_bei"):
        st.info("dash_bei is not in the published database. Run publish.py "
                "to build it, then reload.")
        return

    bei = _load_bei(q, keys)
    if bei.empty:
        st.info("No brands clear the reporting threshold for the selected "
                "stores.")
        return

    window = int(bei["window_days"].iloc[0] or 0)
    scope = ", ".join(stores.get(k, str(k)) for k in _real_keys(keys))
    _note(
        f"<b>Whole-period.</b> Baselines come from the Brand Efficiency "
        f"window ({window} days) for {scope}. This section ignores the weeks "
        f"slider — the tables behind it carry no dates, and a filtered "
        f"heading over lifetime numbers is exactly the confusion worth "
        f"avoiding.")

    # ------------------------------------------------------------ inputs
    left, mid, right = st.columns([2, 2, 3])
    with left:
        brand = st.selectbox(
            "Brand", bei["brand"].tolist(), key="broi_brand",
            help="Ranked by net sales in the window.")

    detail = _load_detail(q, keys, brand, float(window))
    with mid:
        choices = ["Whole brand"] + detail["category"].tolist()
        scope_cat = st.selectbox(
            "Scope", choices, key="broi_cat",
            help="A brand's blended margin can be a fiction. Scope this to "
                 "the category you actually plan to discount.")

    if scope_cat == "Whole brand":
        row = bei[bei["brand"] == brand].iloc[0]
    else:
        row = detail[detail["category"] == scope_cat].iloc[0]
    m = float(row["margin_rate"])

    with right:
        c1, c2 = st.columns(2)
        with c1:
            depth = st.slider(
                "Discount depth", DEPTH_MIN, DEPTH_MAX, 10, 1,
                format="%d%%", key="broi_depth") / 100.0
        with c2:
            reach = st.slider(
                "Share of current sales that get it", 0, 100, 100, 5,
                format="%d%%", key="broi_reach",
                help="100% is a shelf-wide markdown: everything already "
                     "selling gets marked down too. Lower it for a targeted "
                     "offer that only some buyers see.") / 100.0

    lift = st.slider(
        "Extra units you expect it to sell", 0, 200, 25, 5, format="+%d%%",
        key="broi_lift",
        help="Incremental volume on top of the baseline, as a percentage of "
             "it. Every extra unit is assumed to carry the discount.") / 100.0

    sim = simulate(float(row["net"]), m, depth, reach, lift)

    # --------------------------------------------------------- the verdict
    st.markdown("### The number that decides it")

    if sim["sells_below_cost"]:
        st.error(
            f"At {_pct(depth, 0)} off, {brand} sells below cost — its margin "
            f"is {_pct(m)}. No amount of volume fixes that. Cap the depth "
            f"below {_pct(m, 0)}.")
    else:
        be = sim["breakeven"]
        verdict = st.success if lift >= be else st.warning
        gap = lift - be
        verdict(
            f"**{brand} needs {_pct(be, 0)} more units to break even** at "
            f"{_pct(depth, 0)} off with {_pct(reach, 0)} reach. "
            f"You are assuming {_pct(lift, 0)}, which is "
            f"{_pct(abs(gap), 0)} "
            f"{'above' if gap >= 0 else 'short of'} the line.")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Margin change", _signed(sim["d_margin"]))
    k2.metric("Net sales change", _signed(sim["d_net"]))
    k3.metric("Discount spend", _money(sim["spend"]))
    k4.metric("Margin rate after",
              "—" if sim["new_net"] <= 0
              else _pct(sim["new_margin"] / sim["new_net"]))

    _note(
        "<b>Margin change</b> is the only line that matters. Net sales "
        "almost always rise on a discount — that is what a discount is for "
        "— so a growing top line is not evidence the promo worked.")

    # -------------------------------------------------------- the mix
    if len(detail) > 1:
        st.markdown("### The blend hides the answer")
        mix = detail.copy()
        mix["breakeven"] = np.where(
            mix["margin_rate"] > depth,
            (reach * depth) / (mix["margin_rate"] - depth), np.nan)
        show = pd.DataFrame({
            "Category": mix["category"],
            "Net sales": mix["net"].map(_money),
            "Margin": mix["margin_rate"].map(lambda v: _pct(v)),
            "Units": mix["units"].map(lambda v: f"{v:,.0f}"),
            f"Needs at {_pct(depth, 0)} off": [
                "impossible" if np.isnan(b) else f"+{b * 100:,.0f}%"
                for b in mix["breakeven"]],
        })
        st.dataframe(show, hide_index=True, use_container_width=True)

        worst = mix.loc[mix["margin_rate"].idxmin()]
        best = mix.loc[mix["margin_rate"].idxmax()]
        spread = float(best["margin_rate"]) - float(worst["margin_rate"])
        if spread >= 0.10:
            _note(
                f"<b>{brand} spans a {spread * 100:.0f}-point margin "
                f"spread</b> — {best['category']} at "
                f"{_pct(float(best['margin_rate']))} against "
                f"{worst['category']} at "
                f"{_pct(float(worst['margin_rate']))}. The whole-brand number "
                f"averages promos you would never run together. Scope this to "
                f"one category before quoting a figure.")

        neg = mix[mix["margin_rate"] <= 0]
        if not neg.empty:
            st.error(
                "Selling below cost before any new discount: "
                + ", ".join(f"{r.category} ({_pct(r.margin_rate)}, "
                            f"{_money(r.net)})" for r in neg.itertuples())
                + ". Fix that before modelling a markdown on it.")

    # ------------------------------------------------------------- the P&L
    st.markdown("### Where the money moves")
    pnl = pd.DataFrame({
        "": ["Net sales", "Cost of goods", "Margin dollars",
             "Margin rate", "Discount given"],
        "Now": [_money(sim["base_net"]), _money(sim["base_cogs"]),
                _money(sim["base_margin"]), _pct(m), _money(0)],
        "After": [_money(sim["new_net"]), _money(sim["new_cogs"]),
                  _money(sim["new_margin"]),
                  "—" if sim["new_net"] <= 0
                  else _pct(sim["new_margin"] / sim["new_net"]),
                  _money(sim["spend"])],
        "Change": [_signed(sim["d_net"]), _signed(sim["d_cogs"]),
                   _signed(sim["d_margin"]),
                   "—" if sim["new_net"] <= 0
                   else f"{(sim['new_margin'] / sim['new_net'] - m) * 100:+.1f} pts",
                   _signed(sim["spend"])],
    })
    st.dataframe(pnl, hide_index=True, use_container_width=True)

    # ------------------------------------------------------------- curve
    st.markdown("### How the bar moves with depth")
    st.plotly_chart(_breakeven_curve(row, reach, depth, lift, accent),
                    use_container_width=True)
    _note(
        "The curve climbs faster than the discount does. Every point of "
        "markdown comes out of margin, so the volume needed to replace it "
        "grows against a shrinking base — which is why deep discounts on "
        "thin-margin brands rarely recover.")

    # -------------------------------------------------------- feasibility
    st.markdown("### Can the shelf even supply it?")
    base_units = float(row["units"])
    extra_units = base_units * lift
    qoh = float(row["qoh"] or 0)
    dos = float(row["dos"]) if pd.notna(row["dos"]) else np.nan

    f1, f2, f3 = st.columns(3)
    f1.metric("Extra units needed", f"{extra_units:,.0f}",
              help=f"On top of {base_units:,.0f} in the window.")
    f2.metric("On hand now", f"{qoh:,.0f}")
    f3.metric("Days of supply", "—" if np.isnan(dos) else f"{dos:,.0f}")

    if qoh > 0 and extra_units > qoh:
        st.warning(
            f"The plan needs {extra_units:,.0f} extra units but only "
            f"{qoh:,.0f} are on hand. Either the promo runs short or "
            f"procurement has to commit before the lift is proven.")

    _note(
        f"Inventory capital tied up in {brand} is "
        f"<b>{_money(float(row['inv_cost'] or 0))}</b>. A markdown that "
        f"clears aging stock can be worth negative margin — that trade is "
        f"real, but it is a working-capital decision, not a margin one, and "
        f"this page only scores the margin.")

    # ---------------------------------------------------- realized anchor
    realized = _load_realized(q, keys, brand)
    if realized and realized["rate"] == realized["rate"]:
        st.markdown("### What this brand already gets")
        a1, a2 = st.columns(2)
        a1.metric("Discount rate today", _pct(realized["rate"]))
        a2.metric("Baskets containing it", f"{realized['baskets']:,.0f}")
        _note(
            "<b>Directional only.</b> The till records a discount against "
            "the whole basket, not the line, so a single-brand offer inside "
            "a mixed basket gets smeared across every brand in it. Read this "
            "as \"is this brand already living on discount\", never as a "
            "per-brand cost.")

    # -------------------------------------------------------- did it stick
    cohort = _load_cohort(q, keys, brand)
    if cohort is not None and float(cohort["customers"] or 0) > 0:
        st.markdown("### Did past discounting buy a lasting customer?")
        customers = float(cohort["customers"])
        churn60 = float(cohort["churned_60"] or 0) / customers
        repeat = float(cohort["repeat_buyers"] or 0) / customers
        c1, c2, c3 = st.columns(3)
        c1.metric("Redeemers", f"{customers:,.0f}")
        c2.metric("Came back", _pct(repeat))
        c3.metric("Gone at 60 days", _pct(churn60))
        _note(
            "These are people who took an offer on this brand at some point, "
            "not the customers the simulation above is about. If most of "
            "them churned inside 60 days, the discount bought units rather "
            "than a relationship, and the margin math is the whole story.")
