"""
loyalty_tab.py -- the Loyalty tab for cerebral_public.py.

Reads only dash_loyalty_* tables through the app's own q() helper, so it
inherits the read-only connection, the 30-minute cache, and the
missing-table tolerance. No customer-level access, no source files, no PII.

Wire into cerebral_public.py:

  1. Import, near the glossary import:

        from loyalty_tab import render_loyalty

  2. Add a tab:

        t_charts, t_insights, t_brands, t_redeem, t_loyalty, t_takeover, \
            t_projections, t_promo, t_gloss = st.tabs(
            ["Charts", "Insights", "Brands", "Redemptions", "Loyalty",
             "Takeovers", "Projections", "Promo Lab", "What the terms mean"])

  3. Render it, anywhere after the tabs are declared:

        with t_loyalty:
            render_loyalty(q=q, keys=keys, keep=keep, stores=STORES,
                           heading=heading, table_exists=table_exists,
                           partial_week=PARTIAL_WEEK)

The tab respects the sidebar store multiselect and the weeks slider by
taking `keys` and `keep` from the app rather than adding its own controls.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from heat import show_heat, matrix_heat

TIER_ORDER = ["Frequent Flyer", "Travel Club", "Non-Loyalty"]

# Aqua / red / brown from the house palette, in descending tier value.
TIER_COLORS = {
    "Frequent Flyer": "#00FFD4",
    "Travel Club":    "#F73400",
    "Non-Loyalty":    "#5C3E34",
}

# The Non-Stop register went live in ISO week 40 of 2025. Anything earlier is
# missing instrumentation, not absent demand.
NON_STOP_FIRST = (2025, 40)


# ---------------------------------------------------------------- helpers

def _money(x):
    return "-" if pd.isna(x) else f"${x:,.0f}"


def _money2(x):
    return "-" if pd.isna(x) else f"${x:,.2f}"


def _pct(x):
    return "-" if pd.isna(x) else f"{x * 100:.1f}%"


def _fmt_df(df, fn):
    """DataFrame.map arrived in pandas 2.1; applymap covers older builds."""
    try:
        return df.map(fn)
    except AttributeError:
        return df.applymap(fn)


def _wide(fn, obj, **kw):
    """st.dataframe/plotly_chart width API changed; support both builds."""
    try:
        return fn(obj, width="stretch", **kw)
    except TypeError:
        return fn(obj, use_container_width=True, **kw)


def _order(df, col="tier"):
    if df.empty:
        return df
    present = [t for t in TIER_ORDER if t in set(df[col])]
    extra = [t for t in df[col].unique() if t not in TIER_ORDER]
    return df.set_index(col).loc[present + extra].reset_index()


def _store_clause(keys, stores):
    """Chain rollup when everything is selected, explicit list otherwise.

    dash_loyalty_* holds both per-store rows and a store_key = 0 rollup, so
    the two must never be summed together.
    """
    if not keys or len(keys) == len(stores):
        return "store_key = 0", True
    return f"store_key IN ({','.join(map(str, keys))})", False


def _clip(df, keep):
    if df.empty or not keep:
        return df
    mask = [(int(y), int(w)) in keep
            for y, w in zip(df.iso_year, df.iso_week)]
    return df[mask]


def _wk_date(df):
    return pd.to_datetime(
        df.iso_year.astype(int).astype(str) + "-W"
        + df.iso_week.astype(int).astype(str).str.zfill(2) + "-1",
        format="%G-W%V-%u", errors="coerce")


def _derive(df):
    if df.empty:
        return df
    df = df.copy()
    net = pd.to_numeric(df.net, errors="coerce")
    bk = pd.to_numeric(df.baskets, errors="coerce").replace(0, np.nan)
    cu = pd.to_numeric(df.customers, errors="coerce").replace(0, np.nan)
    df["atv"] = net / bk
    df["baskets_per_cust"] = bk / cu
    df["net_per_cust"] = net / cu
    if "redeem_baskets" in df.columns:
        df["redeem_rate"] = pd.to_numeric(df.redeem_baskets,
                                          errors="coerce") / bk
    if "redeem_value" in df.columns:
        df["redeem_pct_net"] = pd.to_numeric(df.redeem_value,
                                             errors="coerce") / net
    return df


# ---------------------------------------------------------------- render

def render_loyalty(q, keys, keep, stores, heading=None, table_exists=None,
                   partial_week=False, howto=None):
    """Render the Loyalty tab.

    q             -- the app's cached query helper
    keys          -- selected store_key list from the sidebar
    keep          -- set of (iso_year, iso_week) tuples from the weeks slider
    stores        -- the STORES dict, for the all-selected test
    heading       -- the app's heading() helper (optional)
    table_exists  -- the app's table_exists() helper (optional)
    partial_week  -- PARTIAL_WEEK flag from the app (optional)
    howto         -- the app's howto() helper (optional)
    """
    H = heading or (lambda t, term=None: st.markdown(f"##### {t}"))

    if table_exists and not table_exists("dash_loyalty_week"):
        st.info(
            "Loyalty tables have not been published yet. Run "
            "`loyalty_ingest.py` against the source database, then "
            "`publish.py`.")
        return

    where, is_chain = _store_clause(keys, stores)

    wk = q(f"SELECT * FROM dash_loyalty_week WHERE {where}")
    if wk.empty:
        st.info("No loyalty data published for the selected stores.")
        return

    if howto:
        howto("loyalty")

    if not is_chain:
        st.caption(
            "Store-level view. Cells with fewer than 25 customers in a week "
            "are withheld to protect individual privacy, so per-store totals "
            "can fall slightly short of the all-stores figure.")

    wkv = _clip(wk, keep)
    if wkv.empty:
        st.info("No loyalty data in the selected week range.")
        return

    agg = {"customers": "sum", "baskets": "sum", "net": "sum",
           "redeem_value": "sum", "redeem_baskets": "sum"}
    summ = _derive(_order(wkv.groupby("tier", as_index=False).agg(agg)))

    # ---- headline ---------------------------------------------------------
    cols = st.columns(len(summ))
    for c, (_, r) in zip(cols, summ.iterrows()):
        c.metric(r.tier, _money2(r.atv), "average basket",
                 help="Net sales divided by baskets for this tier over the "
                      "weeks shown. Unaffected by a partial trailing week, "
                      "since it is already a per-basket figure.")
        c.caption(f"{r.baskets:,.0f} baskets · {_money(r.net)}")

    if partial_week:
        st.caption(
            "The latest week is still in progress. Average basket is "
            "unaffected; totals below are correspondingly lower.")

    st.divider()

    # ---- how much they shop ----------------------------------------------
    H("How much they shop")
    show = summ.copy()
    show["share"] = show.net / show.net.sum()
    show_heat(st, pd.DataFrame({
        "Tier": show.tier,
        "Baskets": show.baskets,
        "Net sales": show.net,
        "Share of net": show["share"],
        "Average basket": show.atv,
        "Baskets per customer-week": show.baskets_per_cust,
        "Net per customer-week": show.net_per_cust,
    }), shading={"Share of net": "slate", "Average basket": "green",
                 "Baskets per customer-week": "blue",
                 "Net per customer-week": "green"},
        fmt={"Baskets": "{:,.0f}", "Net sales": "${:,.0f}",
             "Share of net": "{:.1%}", "Average basket": "${:,.2f}",
             "Baskets per customer-week": "{:.2f}",
             "Net per customer-week": "${:,.2f}"})

    st.caption(
        "Customer counts are summed across weeks, so someone active in six "
        "weeks counts six times. Per-customer figures are therefore a weekly "
        "intensity measure, not lifetime value.")

    # ---- roster -----------------------------------------------------------
    if not table_exists or table_exists("dash_loyalty_roster"):
        ros = q("SELECT * FROM dash_loyalty_roster")
        if not ros.empty:
            ros = _order(ros)
            ros["dormant"] = 1 - (pd.to_numeric(ros.transacted, errors="coerce")
                                  / pd.to_numeric(ros.roster, errors="coerce")
                                  .replace(0, np.nan))
            H("Roster")
            show_heat(st, pd.DataFrame({
                "Tier": ros.tier,
                "On roster": ros.roster,
                "Has ever transacted": ros.transacted,
                "Dormant": ros.dormant,
            }), shading={"Dormant": "red"},
                fmt={"On roster": "{:,.0f}",
                     "Has ever transacted": "{:,.0f}",
                     "Dormant": "{:.1%}"})
            st.caption(
                "Roster counts are chain-wide and do not follow the store "
                "filter — membership belongs to a person, not a shop.")

    st.divider()

    # ---- channel ----------------------------------------------------------
    if not table_exists or table_exists("dash_loyalty_channel_week"):
        cw = _clip(q(f"SELECT * FROM dash_loyalty_channel_week WHERE {where}"),
                   keep)
        if not cw.empty:
            H("By channel")
            g = _derive(cw.groupby(["tier", "channel"], as_index=False)
                        .agg({"customers": "sum", "baskets": "sum",
                              "net": "sum", "redeem_baskets": "sum"}))

            L, R = st.columns(2)
            with L:
                piv = g.pivot(index="tier", columns="channel", values="atv")
                piv = piv.reindex([t for t in TIER_ORDER if t in piv.index])
                st.caption("Average basket")
                _wide(st.dataframe, _fmt_df(piv, _money2))
            with R:
                mix = g.copy()
                mix["share"] = mix.net / mix.groupby("tier").net \
                                           .transform("sum")
                pm = mix.pivot(index="tier", columns="channel", values="share")
                pm = pm.reindex([t for t in TIER_ORDER if t in pm.index])
                st.caption("Share of the tier's net sales")
                _wide(st.dataframe, _fmt_df(pm, _pct))

            fig = px.bar(g, x="channel", y="atv", color="tier",
                         barmode="group",
                         color_discrete_map=TIER_COLORS,
                         labels={"atv": "Average basket", "channel": ""},
                         category_orders={"tier": TIER_ORDER})
            fig.update_layout(height=320, margin=dict(t=10, b=10, l=0, r=0),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              legend_title_text="")
            fig.update_yaxes(tickprefix="$")
            _wide(st.plotly_chart, fig, key="loy_ch")

            if keep and min(keep) < NON_STOP_FIRST:
                st.warning(
                    "The window reaches back before the Non-Stop register "
                    "went live in 2025-W40. Non-Stop figures before that "
                    "point reflect missing instrumentation, not absent "
                    "demand.")

    st.divider()

    # ---- redemptions ------------------------------------------------------
    H("Redemptions")
    red = summ.copy()
    red["share"] = red.redeem_value / red.redeem_value.sum()
    show_heat(st, pd.DataFrame({
        "Tier": red.tier,
        "Baskets with a redemption": red.redeem_rate,
        "Redemption value": red.redeem_value,
        "Share of all redemption value": red["share"],
        "As % of net sales": red.redeem_pct_net,
    }), shading={"Baskets with a redemption": "blue",
                 "Share of all redemption value": "slate",
                 "As % of net sales": "red"},
        fmt={"Baskets with a redemption": "{:.1%}",
             "Redemption value": "${:,.0f}",
             "Share of all redemption value": "{:.1%}",
             "As % of net sales": "{:.2%}"})

    st.caption(
        "The Frequent Flyer audience carries a 2x points multiplier, so FF "
        "members accrue at double rate and shop far more often. Much of the "
        "redemption gap is mechanical rather than behavioural — the programme "
        "working as designed, not overspend. Judge the rate by comparing "
        "redemption value against net sales within each tier, not by "
        "comparing tiers to each other.")

    if not table_exists or table_exists("dash_loyalty_offer"):
        off = _clip(q(f"SELECT * FROM dash_loyalty_offer WHERE {where}"), keep)
        if not off.empty:
            with st.expander("Top offers by redemption value"):
                g = (off.groupby(["tier", "offer_name"], as_index=False)
                     .agg({"redemptions": "sum", "redeem_value": "sum",
                           "avg_basket": "mean"})
                     .sort_values("redeem_value", ascending=False).head(25))
                _wide(st.dataframe, pd.DataFrame({
                    "Tier": g.tier,
                    "Offer": g.offer_name,
                    "Redemptions": g.redemptions.map("{:,.0f}".format),
                    "Redemption value": g.redeem_value.map(_money),
                    "Average basket": g.avg_basket.map(_money2),
                }), hide_index=True)

    # ---- order-value bins -------------------------------------------------
    if not table_exists or table_exists("dash_loyalty_bins"):
        bins = _clip(q(f"SELECT * FROM dash_loyalty_bins WHERE {where}"), keep)
        if not bins.empty:
            st.divider()
            H("Order value distribution")
            b = (bins.groupby(["tier", "bin_label", "bin_order"],
                              as_index=False)
                 .agg(baskets=("baskets", "sum"), net=("net", "sum"))
                 .sort_values("bin_order"))
            b["share"] = b.baskets / b.groupby("tier").baskets \
                                      .transform("sum")
            b["aov"] = b.net / b.baskets.replace(0, np.nan)

            order = [x for x in ["At or above $125", "Between $100 and $124",
                                 "Below $100"] if x in set(b.bin_label)]
            tiers = [t for t in TIER_ORDER if t in set(b.tier)]

            st.caption("Share of each tier's baskets")
            sh = b.pivot(index="bin_label", columns="tier", values="share")
            sh = sh.reindex(order)[[t for t in tiers if t in sh.columns]]
            matrix_heat(st, sh, fmt="{:.0%}", palette="blue", axis="table")

            st.caption("Average basket within each band")
            av = b.pivot(index="bin_label", columns="tier", values="aov")
            av = av.reindex(order)[[t for t in tiers if t in av.columns]]
            matrix_heat(st, av, fmt="${:,.0f}", palette="green",
                        axis="table")

            with st.expander("Basket counts"):
                ct = b.pivot(index="bin_label", columns="tier",
                             values="baskets")
                ct = ct.reindex(order)[[t for t in tiers if t in ct.columns]]
                matrix_heat(st, ct, fmt="{:,.0f}", palette="slate",
                            axis="table")

            st.caption(
                "Bands match the discount thresholds, so the middle row is "
                "the actionable one: baskets close to a threshold but not "
                "over it are the cheapest incremental revenue available.")

    st.divider()

    # ---- trend ------------------------------------------------------------
    H("Average basket by week")
    t = wkv.copy()
    t["wk_date"] = _wk_date(t)
    t["atv"] = pd.to_numeric(t.net, errors="coerce") / \
        pd.to_numeric(t.baskets, errors="coerce").replace(0, np.nan)
    t = t.groupby(["wk_date", "tier"], as_index=False)["atv"].mean()
    t = t.sort_values(["tier", "wk_date"])
    fig = px.line(t, x="wk_date", y="atv", color="tier",
                  color_discrete_map=TIER_COLORS,
                  labels={"atv": "Average basket", "wk_date": ""},
                  category_orders={"tier": TIER_ORDER})
    fig.update_layout(height=340, margin=dict(t=10, b=10, l=0, r=0),
                      paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", legend_title_text="")
    fig.update_yaxes(tickprefix="$")
    _wide(st.plotly_chart, fig, key="loy_trend")

    # ---- enrolment --------------------------------------------------------
    if not table_exists or table_exists("dash_loyalty_enrollment"):
        en = q("SELECT * FROM dash_loyalty_enrollment ORDER BY month")
        if not en.empty:
            H("Frequent Flyer enrolments per month")
            fig = px.bar(en, x="month", y="enrollments",
                         labels={"enrollments": "Enrolments", "month": ""})
            fig.update_traces(marker_color=TIER_COLORS["Frequent Flyer"])
            fig.update_layout(height=280, margin=dict(t=10, b=10, l=0, r=0),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)")
            _wide(st.plotly_chart, fig, key="loy_enrol")
            st.caption(
                "Chain-wide, from the Dutchie discount-group audit. Covers "
                "the full audit history rather than the weeks slider.")

    # ---- category ---------------------------------------------------------
    if not table_exists or table_exists("dash_loyalty_category_week"):
        cat = _clip(q(f"SELECT * FROM dash_loyalty_category_week "
                      f"WHERE {where}"), keep)
        if not cat.empty:
            with st.expander("Category mix by tier"):
                g = cat.groupby(["tier", "category"], as_index=False) \
                       .agg({"net": "sum"})
                g["share"] = g.net / g.groupby("tier").net.transform("sum")
                piv = g.pivot(index="category", columns="tier", values="share")
                piv = piv[[c for c in TIER_ORDER if c in piv.columns]]
                piv = piv.sort_values(piv.columns[0], ascending=False)
                matrix_heat(st, piv, fmt="{:.1%}", palette="blue",
                            axis="column")

    # ---- method -----------------------------------------------------------
    with st.expander("Method and caveats"):
        cov = ""
        if not table_exists or table_exists("dash_loyalty_meta"):
            m = q("SELECT * FROM dash_loyalty_meta")
            if not m.empty:
                m = m.iloc[0]
                rate = m.matched_customers / max(m.transacting_customers, 1)
                cov = (f"\n\n**Match rate.** {rate * 100:.1f}% of transacting "
                       f"customers resolve to a tier. Unmatched customers "
                       f"fall to Non-Loyalty, which slightly overstates that "
                       f"tier. Tier assignments last rebuilt "
                       f"{str(m.tiers_built_at)[:16]}.")
        st.markdown(
            """
**Tiers.** *Frequent Flyer* is the Dutchie `Travel Club Frequent Flyer`
discount group, reconciled against the Alpine IQ audience of the same name —
the two agree to within about 1%. *Travel Club* is an Alpine IQ loyalty
member who is not a Frequent Flyer. *Non-Loyalty* is everyone else.

**Selection, not lift.** Frequent Flyer is assigned by staff at the register,
to customers who already spend heavily. The gap between Frequent Flyer and
the other tiers is descriptive and must not be read as the effect of
membership. The Travel Club against Non-Loyalty comparison is far less
affected, because Travel Club is opt-in rather than hand-picked — that is the
more honest measure of what the loyalty programme is worth.

**Aggregates only.** This dashboard reads pre-aggregated tables. Cells
covering fewer than 25 customers are withheld at store level; the all-stores
view is computed before suppression and is therefore exact.
            """ + cov)
