"""
retention_tab.py -- the Retention tab for cerebral_public.py.

Reads only dash_retention_* through the app's q() helper.

Wire in:

    from retention_tab import render_retention
    ...
    with t_retention:
        render_retention(q=q, keys=keys, stores=STORES,
                         heading=heading, table_exists=table_exists)

NOTE ON FILTERS
---------------
Retention deliberately ignores the weeks slider. A customer's repeat rate is
a property of their whole history, not of a 26-week window: slice it and a
customer whose second order falls outside the window reads as never having
returned. The store filter DOES apply, on the store of the FIRST order.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from heat import show_heat, matrix_heat

DAYS_PER_MONTH = 30.44

CHANNEL_ORDER = ["In-Store", "Delivery", "Non-Stop"]
CHANNEL_COLORS = {
    "In-Store": "#5C3E34",
    "Delivery": "#00FFD4",
    "Non-Stop": "#F73400",
}
GAP_ORDER = ["0-7 days", "8-14 days", "15-30 days",
             "31-60 days", "61-90 days", "90+ days"]


def _money(x):
    return "-" if pd.isna(x) else f"${x:,.0f}"


def _money2(x):
    return "-" if pd.isna(x) else f"${x:,.2f}"


def _pct(x):
    return "-" if pd.isna(x) else f"{x * 100:.1f}%"


def _num(x, d=1):
    return "-" if pd.isna(x) else f"{x:,.{d}f}"


def _wide(fn, obj, **kw):
    try:
        return fn(obj, width="stretch", **kw)
    except TypeError:
        return fn(obj, use_container_width=True, **kw)


def _order(df, col="first_channel"):
    if df.empty:
        return df
    present = [c for c in CHANNEL_ORDER if c in set(df[col])]
    extra = [c for c in df[col].dropna().unique() if c not in CHANNEL_ORDER]
    return df.set_index(col).loc[present + extra].reset_index()


def _derive(df):
    if df.empty:
        return df
    d = df.copy()
    cu = pd.to_numeric(d.customers, errors="coerce").replace(0, np.nan)
    d["ltv"] = d.revenue / cu
    d["aov"] = d.revenue / pd.to_numeric(d.orders,
                                         errors="coerce").replace(0, np.nan)
    d["orders_per_cust"] = d.orders / cu
    d["repeat_rate"] = d.repeaters / cu
    d["sticky_rate"] = d.sticky / cu
    d["cust_share"] = d.customers / d.customers.sum()
    d["rev_share"] = d.revenue / d.revenue.sum()
    if "avg_lifespan_days" in d:
        d["lifespan_months"] = d.avg_lifespan_days / DAYS_PER_MONTH
    if "avg_gap_days" in d:
        d["freq_months"] = d.avg_gap_days / DAYS_PER_MONTH
    return d


def render_retention(q, keys, stores, heading=None, table_exists=None,
                     howto=None):
    """Render the Retention tab. See module docstring for wiring."""
    H = heading or (lambda t, term=None: st.markdown(f"##### {t}"))

    if table_exists and not table_exists("dash_retention_summary"):
        st.info("Retention tables have not been published yet. Run "
                "`publish.py` after adding `build_retention(con)`.")
        return

    all_stores = not keys or len(keys) == len(stores)
    if howto:
        howto("retention")

    # ---- scope banner --------------------------------------------------
    meta = q("SELECT * FROM dash_retention_meta")
    if not meta.empty:
        m = meta.iloc[0]
        share = m.customers / max(m.all_customers, 1)
        st.caption(
            f"Based on {int(m.customers):,} customers with a confirmed "
            f"identity ({share * 100:.0f}% of all transacting customers), "
            f"{m.cov_start} to {m.cov_end}. Customers matched only on name "
            f"are excluded — common names collapse several people into one "
            f"record and would invent repeat visits."
        )

    if all_stores:
        summ = _derive(_order(q("SELECT * FROM dash_retention_summary")))
    else:
        # Rebuild the headline from the cohort table for the chosen stores.
        ks = ",".join(str(k) for k in keys)
        coh = q(f"SELECT * FROM dash_retention_cohort "
                f"WHERE first_store IN ({ks})")
        if coh.empty:
            st.info("No retention data for the selected stores.")
            return
        summ = _derive(_order(
            coh.groupby("first_channel", as_index=False)
            .agg(customers=("customers", "sum"), orders=("orders", "sum"),
                 revenue=("revenue", "sum"), repeaters=("repeaters", "sum"),
                 sticky=("sticky", "sum"),
                 lifespan_days=("lifespan_days", "sum"))))
        summ["avg_lifespan_days"] = summ.lifespan_days / summ.customers
        summ["lifespan_months"] = summ.avg_lifespan_days / DAYS_PER_MONTH
        st.caption("Store view: customers are attributed to the store of "
                   "their **first** order.")

    if summ.empty:
        st.info("No retention data published.")
        return

    # ---- headline ------------------------------------------------------
    cols = st.columns(len(summ))
    for c, (_, r) in zip(cols, summ.iterrows()):
        c.metric(r.first_channel, _pct(r.repeat_rate), "place a 2nd order",
                 help="Share of customers acquired on this channel who ever "
                      "ordered again.")
        c.caption(f"{r.customers:,.0f} customers · {_money(r.ltv)} each")

    st.divider()

    # ---- the CRO table -------------------------------------------------
    H("Retention by first-order channel")
    disp = pd.DataFrame({
        "First channel": summ.first_channel,
        "Customers": summ.customers,
        "% of customers": summ.cust_share,
        "Revenue": summ.revenue,
        "% of revenue": summ.rev_share,
        "Historical LTV": summ.ltv,
        "Average order": summ.aov,
        "Avg lifespan (mo)": summ.get("lifespan_months",
                                      pd.Series(dtype=float)),
        "Order freq (mo)": summ.get("freq_months", pd.Series(dtype=float)),
        "Orders per customer": summ.orders_per_cust,
        "Repeat rate": summ.repeat_rate,
        "Sticky rate (5+)": summ.sticky_rate,
    })
    show_heat(
        st, disp,
        shading={"% of customers": "slate", "% of revenue": "slate",
                 "Historical LTV": "green", "Average order": "slate",
                 "Avg lifespan (mo)": "slate", "Order freq (mo)": "slate",
                 "Orders per customer": "slate",
                 "Repeat rate": "blue", "Sticky rate (5+)": "blue"},
        fmt={"Customers": "{:,.0f}", "% of customers": "{:.0%}",
             "Revenue": "${:,.0f}", "% of revenue": "{:.0%}",
             "Historical LTV": "${:,.0f}", "Average order": "${:,.2f}",
             "Avg lifespan (mo)": "{:.1f}", "Order freq (mo)": "{:.1f}",
             "Orders per customer": "{:.1f}",
             "Repeat rate": "{:.0%}", "Sticky rate (5+)": "{:.0%}"},
        # a shorter gap between orders is the better outcome
        reverse=("Order freq (mo)",))

    if {"gap_p25", "gap_p75"} <= set(summ.columns) and all_stores:
        g = pd.DataFrame({
            "First channel": summ.first_channel,
            "25th percentile": summ.gap_p25,
            "Median": summ.gap_p50,
            "75th percentile": summ.gap_p75,
        })
        st.caption("Days until the next order")
        show_heat(st, g,
                  shading={"25th percentile": "slate", "Median": "slate",
                           "75th percentile": "slate"},
                  fmt={c: "{:,.0f} days" for c in
                       ("25th percentile", "Median", "75th percentile")},
                  reverse=("25th percentile", "Median", "75th percentile"),
                  axis="table")

    st.caption(
        "**Repeat rate** is the share who ever placed a second order. "
        "**Sticky rate** is the share reaching five or more. **Lifespan** is "
        "first order to last, so it is bounded by how long we have observed "
        "each customer — see the cohort view below for a like-for-like "
        "comparison."
    )

    st.divider()

    # ---- first three baskets -------------------------------------------
    if not table_exists or table_exists("dash_retention_sequence"):
        seq = q("SELECT * FROM dash_retention_sequence ORDER BY seq")
        if not seq.empty:
            H("The first three baskets")
            base = (seq[seq.seq == 1].set_index("first_channel")
                    .customers.to_dict())
            seq["survival"] = seq.apply(
                lambda r: r.customers / base.get(r.first_channel, np.nan),
                axis=1)

            L, R = st.columns(2)
            with L:
                piv = seq.pivot(index="first_channel", columns="seq_label",
                                values="survival")
                piv = piv.reindex([c for c in CHANNEL_ORDER
                                   if c in piv.index])
                st.caption("Share of customers reaching each basket")
                matrix_heat(st, piv, fmt="{:.1%}", palette="blue")
            with R:
                piv2 = seq.pivot(index="first_channel", columns="seq_label",
                                 values="avg_value")
                piv2 = piv2.reindex([c for c in CHANNEL_ORDER
                                     if c in piv2.index])
                st.caption("Average basket value")
                matrix_heat(st, piv2, fmt="${:,.2f}", palette="green")

            gaps = seq[seq.seq > 1]
            if not gaps.empty:
                piv3 = gaps.pivot(index="first_channel", columns="seq_label",
                                  values="median_days_since_prev")
                piv3 = piv3.reindex([c for c in CHANNEL_ORDER
                                     if c in piv3.index])
                st.caption("Median days since the previous basket")
                matrix_heat(st, piv3, fmt="{:,.0f}", palette="slate",
                            reverse=True)

            fig = px.bar(seq, x="seq_label", y="survival",
                         color="first_channel", barmode="group",
                         color_discrete_map=CHANNEL_COLORS,
                         labels={"survival": "Share reaching this basket",
                                 "seq_label": ""},
                         category_orders={"first_channel": CHANNEL_ORDER})
            fig.update_layout(height=320, margin=dict(t=10, b=10, l=0, r=0),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              legend_title_text="")
            fig.update_yaxes(tickformat=".0%")
            _wide(st.plotly_chart, fig, key="ret_seq")

            st.caption(
                "The drop from basket 1 to basket 2 is the single biggest "
                "leak in the funnel. Everything after it is comparatively "
                "gentle — a customer who returns once tends to keep "
                "returning."
            )

    st.divider()

    # ---- rolling six-month visit frequency ------------------------------
    if not table_exists or table_exists("dash_retention_rolling"):
        roll = q("SELECT * FROM dash_retention_rolling ORDER BY month")
        if not roll.empty:
            H("Visits per customer, rolling six months")
            roll["month"] = pd.to_datetime(roll.month)
            roll["visits_per_cust"] = roll.visits / \
                roll.customers.replace(0, np.nan)
            roll["repeat_rate"] = roll.repeat_customers / \
                roll.customers.replace(0, np.nan)

            full = st.checkbox(
                "Only months with a full six-month lookback", value=True,
                key="ret_roll_full",
                help="Early months cannot look back six months, so their "
                     "visit counts are mechanically low.")
            r = roll[roll.window_months >= 6] if full else roll

            if r.empty:
                st.info("No months yet have a full six-month lookback.")
            else:
                metric = st.radio(
                    "Show", ["Visits per customer", "Active customers",
                             "Share visiting twice or more"],
                    horizontal=True, key="ret_roll_metric")
                ycol = {"Visits per customer": "visits_per_cust",
                        "Active customers": "customers",
                        "Share visiting twice or more": "repeat_rate"}[metric]

                fig = px.line(r.sort_values("month"), x="month", y=ycol,
                              color="first_channel",
                              color_discrete_map=CHANNEL_COLORS,
                              labels={ycol: metric, "month": ""},
                              category_orders={"first_channel":
                                               CHANNEL_ORDER})
                fig.update_layout(height=340,
                                  margin=dict(t=10, b=10, l=0, r=0),
                                  paper_bgcolor="rgba(0,0,0,0)",
                                  plot_bgcolor="rgba(0,0,0,0)",
                                  legend_title_text="")
                if ycol == "repeat_rate":
                    fig.update_yaxes(tickformat=".0%")
                _wide(st.plotly_chart, fig, key="ret_rolling")

                latest = r[r.month == r.month.max()]
                if not latest.empty:
                    st.caption(
                        f"Six months to {r.month.max():%b %Y}, by the channel "
                        f"each customer was acquired on.")
                    show_heat(st, pd.DataFrame({
                        "First channel": latest.first_channel,
                        "Active customers": latest.customers,
                        "Visits": latest.visits,
                        "Visits per customer": latest.visits_per_cust,
                        "Visited 2+ times": latest.repeat_rate,
                        "Visited 5+ times": latest.heavy_customers /
                        latest.customers.replace(0, np.nan),
                    }), shading={"Visits per customer": "blue",
                                 "Visited 2+ times": "blue",
                                 "Visited 5+ times": "green"},
                        fmt={"Active customers": "{:,.0f}",
                             "Visits": "{:,.0f}",
                             "Visits per customer": "{:.2f}",
                             "Visited 2+ times": "{:.0%}",
                             "Visited 5+ times": "{:.0%}"})

            st.caption(
                "Each point counts customers with at least one visit in the "
                "preceding six months and their visits in that window. "
                "Unlike the tables above this moves over time, so it shows "
                "whether frequency is improving or decaying. Chain-wide: a "
                "customer can visit several stores in six months, so the "
                "store filter does not apply here."
            )

    st.divider()

    # ---- gap distribution ----------------------------------------------
    if not table_exists or table_exists("dash_retention_gaps"):
        gp = q("SELECT * FROM dash_retention_gaps ORDER BY bucket_order")
        if not gp.empty:
            H("When customers come back")
            gp["share"] = gp.gaps / gp.groupby("first_channel").gaps \
                                     .transform("sum")
            fig = px.bar(gp, x="gap_bucket", y="share", color="first_channel",
                         barmode="group", color_discrete_map=CHANNEL_COLORS,
                         labels={"share": "Share of return visits",
                                 "gap_bucket": ""},
                         category_orders={"gap_bucket": GAP_ORDER,
                                          "first_channel": CHANNEL_ORDER})
            fig.update_layout(height=320, margin=dict(t=10, b=10, l=0, r=0),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              legend_title_text="")
            fig.update_yaxes(tickformat=".0%")
            _wide(st.plotly_chart, fig, key="ret_gaps")

    # ---- cohorts --------------------------------------------------------
    if not table_exists or table_exists("dash_retention_cohort"):
        where = "first_store = 0" if all_stores else \
            "first_store IN (%s)" % ",".join(str(k) for k in keys)
        coh = q(f"SELECT * FROM dash_retention_cohort WHERE {where}")
        if not coh.empty:
            H("By acquisition cohort")
            st.caption(
                "Customers grouped by the month of their first order. Recent "
                "cohorts have had less time to return, so read down a column "
                "rather than across — comparing a two-month-old cohort to a "
                "year-old one measures elapsed time, not quality."
            )
            g = (coh.groupby(["cohort_month", "first_channel"],
                             as_index=False)
                 .agg(customers=("customers", "sum"),
                      repeaters=("repeaters", "sum"),
                      revenue=("revenue", "sum"),
                      days_observed=("days_observed", "mean")))
            g["repeat_rate"] = g.repeaters / g.customers.replace(0, np.nan)
            g["cohort_month"] = pd.to_datetime(g.cohort_month)

            mature = st.checkbox(
                "Only cohorts observed at least 90 days", value=True,
                help="Excludes cohorts too recent to have had a fair chance "
                     "to return.")
            gg = g[g.days_observed >= 90] if mature else g

            if not gg.empty:
                fig = px.line(gg.sort_values("cohort_month"),
                              x="cohort_month", y="repeat_rate",
                              color="first_channel",
                              color_discrete_map=CHANNEL_COLORS,
                              labels={"repeat_rate": "Repeat rate",
                                      "cohort_month": ""},
                              category_orders={"first_channel":
                                               CHANNEL_ORDER})
                fig.update_layout(height=340,
                                  margin=dict(t=10, b=10, l=0, r=0),
                                  paper_bgcolor="rgba(0,0,0,0)",
                                  plot_bgcolor="rgba(0,0,0,0)",
                                  legend_title_text="")
                fig.update_yaxes(tickformat=".0%")
                _wide(st.plotly_chart, fig, key="ret_cohort")

                with st.expander("Cohort detail"):
                    t = gg.copy()
                    t["cohort"] = t.cohort_month.dt.strftime("%b %Y")
                    show_heat(st, pd.DataFrame({
                        "Cohort": t.cohort,
                        "First channel": t.first_channel,
                        "Customers": t.customers,
                        "Repeat rate": t.repeat_rate,
                        "Revenue": t.revenue,
                        "Days observed": t.days_observed,
                    }), shading={"Repeat rate": "blue", "Revenue": "green"},
                        fmt={"Customers": "{:,.0f}", "Repeat rate": "{:.0%}",
                             "Revenue": "${:,.0f}",
                             "Days observed": "{:,.0f}"})

    # ---- method ---------------------------------------------------------
    with st.expander("Method and caveats"):
        st.markdown(
            """
**Who is counted.** Only customers whose identity resolves to a real
POS or loyalty ID. Customers matched on name alone are excluded: common
names collapse several people into one record, which would show as one
person returning. This is the ETL's own warning about name-based identity,
applied here because retention is exactly the cross-period question it
warns against.

**First-order channel** is the channel of the earliest order *we can see*.
Coverage begins at the start of the window shown above, so a customer who
first shopped before then is attributed to their first observed order, not
their true first. Cohorts near the start of coverage are affected most.

**Repeat rate** — ever placed a second order. **Sticky rate** — reached five
or more. **Lifespan** — days from first to last order, which is censored by
how long we have observed each customer. **Order frequency** — average days
between consecutive orders, defined only for customers with two or more.

**The weeks slider does not apply.** Retention is a property of a
customer's whole history; windowing it would count a customer as lost
whenever their return fell outside the window. The store filter does apply,
on the store of the first order.

**Aggregates only.** No customer-level data reaches this dashboard, and
groups smaller than 25 customers are withheld.
            """)
