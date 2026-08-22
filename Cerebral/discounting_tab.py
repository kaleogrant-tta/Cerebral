"""
discounting_tab.py -- the Discounting tab.

Whole-period discount and offer analysis. This is the deliberate home for
every discount view whose source table carries no dates: dash_offer_performance
has first_seen / last_seen per offer but no per-day grain, and the promo cohort
tables are lifetime aggregates. A takeover page must only show numbers scoped
to its window, so the whole-period views live here instead, where a
58-week baseline is the expected frame and says so on the page.

Four sections, each a different KIND of discounting:

  1. Discount type      -- the mix. Loyalty brand offers vs Travel Club tier
                           rewards vs Secret Drops vs point substitutions.
  2. Offers and SKUs    -- offer-level detail, what the customer received.
  3. Brand cohorts      -- who redeemed per brand and whether they churned.
  4. Category cohorts   -- the same by category.

Sections 3 and 4 read dash_promo_brand / dash_promo_category, which are
REDEEMER COHORT tables, not discount-volume tables: customers who took an
offer, their spend, and churn at 30/45/60/90 days. They answer "did
discounting buy us a lasting customer", not "what did we discount".

Wire into cerebral_public.py:

  1. Import, near the other tab imports:

        from discounting_tab import render_discounting

  2. Add the tab, next to Redemptions:

        t_charts, t_insights, t_brands, t_acc, t_redeem, t_discount, \
            t_loyalty, t_retention, t_events, t_audiences, \
            t_takeover, t_projections, t_promo, t_gloss = st.tabs(
            ["Charts", "Insights", "Brands", "Accessories", "Redemptions",
             "Discounting", "Loyalty", "Retention", "Events", "Audiences",
             "Takeovers", "Projections", "Promo Lab",
             "What the terms mean"])

  3. Render it, anywhere after the tabs are declared:

        with t_discount:
            render_discounting(q=q, keys=keys, keep=keep, stores=STORES,
                               heading=heading, table_exists=table_exists,
                               accent=ACCENT, series=SERIES)

The tab respects both the store multiselect and the weeks slider, but only
where the data supports it. dash_discount_day has a real date grain, so the
cost sections honour the slider. The offer and cohort tables below are
whole-period by construction and say so on the page rather than pretending
to filter.
"""

from __future__ import annotations

import glob
import os
import re

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Offer-name taxonomy. Order matters: substitutions are matched before the
# generic "Travel Club" prefix, since a substitution offer is named
# "Travel Club 1000 Points Substitution" and belongs in its own bucket.
#
# These four cover every offer name in the published data. Anything new falls
# to "Other", which is surfaced rather than hidden so an unclassified family
# shows up as a row instead of silently disappearing into a bucket.
TYPE_ORDER = [
    "Point substitution",
    "Loyalty reward",
    "Secret Drop",
    "Other",
]

TYPE_NOTE = {
    "Loyalty reward":
        "The loyalty menu. Customers spend points they earned by shopping on "
        "a set list of SKUs, and the brands supplying those SKUs rebate "
        "them. One point is one dollar of reward value; Frequent Flyers earn "
        "two points per dollar spent, so they reach a redemption twice as "
        "fast. Offer names carried a \"Loyalty \" prefix until March 2026 "
        "and a \"Travel Club \" prefix after -- a rename, not a change of "
        "programme, so both sit in this row.",
    "Point substitution":
        "Same programme, stock workaround: the customer's chosen reward was "
        "out, so staff swapped in something of similar value and wrote the "
        "discount down to it.",
    "Secret Drop":
        "The April 2026 mystery-promo series. No brand of its own, tracked "
        "as its own family.",
    "Other":
        "Offers matching none of the naming conventions above. A growing row "
        "here means a new campaign type needs classifying.",
}

# What it costs TTA to ISSUE a point, not to honour one. 1 point = $1 of
# redemption value (Frequent Flyers earn 2 points per dollar spent, so they
# accrue twice as fast, but a point is still worth a dollar when spent).
# This rate applies to points handed out rather than earned -- event
# giveaways, boarding passes, service recovery, comps.
#
# It does NOT apply to redemptions. Those are points customers earned by
# spending, and what they cost is the product, which the supplying brands
# rebate. Rebate terms are not in the published data, so redemption cost
# cannot be computed here. Multiplying redemption value by this rate mixes
# the two sides of the programme and is wrong.
POINT_COST_RATE = 0.05

_SUB_RE = re.compile(r"[0-9]+\s*points?\s+substitution", re.I)

# Store labels for the discount tables. The app passes store_key
# integers; this keeps the tab readable without importing STORES.
STORE_LABEL = {1: "DTBK", 2: "Fifth Avenue", 3: "SoHo",
               4: "Union Square"}


def _classify(name) -> str:
    """Bucket an offer name into a discount type.

    Mirrors _family_sql() in publish.py for Secret Drops and substitutions,
    then splits the remainder on the "Loyalty " / "Travel Club " prefixes that
    the offer names actually use.
    """
    s = str(name or "")
    low = s.lower()
    if "secret drop" in low:
        return "Secret Drop"
    if _SUB_RE.search(low):
        return "Point substitution"
    # One type, not two. The prefix changed on 2026-03-16 -- "Loyalty
    # TTA Lighter" became "Travel Club TTA Lighter" mid-run -- so splitting
    # on it reads a rename as a programme ending and another starting.
    # Which tier funded the redemption comes from dash_redemption_tier.
    if low.startswith(("travel club", "loyalty")):
        return "Loyalty reward"
    return "Other"


def _store_clause(keys, stores):
    """Chain rollup when everything is selected, explicit list otherwise.

    The promo and offer tables carry per-store rows only -- no store_key = 0
    rollup -- so an all-selected view sums them. Kept as a helper so the
    behaviour is stated once rather than repeated per query.
    """
    if not keys:
        return "1 = 0"
    if stores and len(keys) == len(stores):
        return "1 = 1"
    return "store_key IN (" + ",".join(str(int(k)) for k in keys) + ")"


def _money(x):
    return "-" if pd.isna(x) else f"${x:,.0f}"


def _pct(x):
    return "-" if pd.isna(x) else f"{x:.1f}%"


def _order_types(df, col="Discount type"):
    if df.empty:
        return df
    present = [t for t in TYPE_ORDER if t in set(df[col])]
    extra = [t for t in df[col].unique() if t not in TYPE_ORDER]
    return df.set_index(col).loc[present + extra].reset_index()



# --------------------------------------------------- what discounting costs

def _render_cost(q, where, keep, H, accent, pal, table_exists):
    """Window-scoped discount cost, from dash_discount_day.

    This is the money actually taken off at the till: every discount the POS
    applied, of which loyalty offers are one part. The loyalty split matters
    because the two are NOT additive -- loyalty_redeem is a subset of
    discount, not a sibling of it. Showing the remainder is the only way to
    see what group, employee and manual discounting costs, since nothing
    else in the published data isolates it.
    """
    if table_exists and not table_exists("dash_discount_day"):
        st.info(
            "Discount tables have not been published yet. The POS export "
            "carries DiscountAmt at basket level; run `backfill_discount.py` "
            "then `publish.py` to populate them.")
        return

    day = q(f"""
        SELECT store_key, day, channel, baskets, discounted_baskets,
               gross, discount, net, margin, loyalty_redeem
        FROM dash_discount_day
        WHERE {where}
    """)
    if day.empty:
        st.info("No discount data for the selected stores.")
        return

    day["day"] = pd.to_datetime(day["day"])
    iso = day["day"].dt.isocalendar()
    day["iso_year"] = iso.year.astype(int)
    day["iso_week"] = iso.week.astype(int)
    if keep:
        day = day[[(y, w) in keep
                   for y, w in zip(day.iso_year, day.iso_week)]]
    if day.empty:
        st.info("No discount data in the selected weeks.")
        return

    disc = day.discount.sum()
    loy = day.loyalty_redeem.sum()
    other = disc - loy
    gross = day.gross.sum()
    net = day.net.sum()

    H("What discounting costs")
    st.markdown(
        '<p class="note">Money taken off at the till, scoped to the weeks '
        'slider and store filter. <b>Loyalty</b> is the part attributable to '
        'loyalty offers; <b>everything else</b> is group and employee '
        'discounts, first-responder and retail-worker rates, promo codes and '
        'manual write-downs. The two are not additive — loyalty is a '
        '<i>subset</i> of total discount, so the remainder is what the rest '
        'of the discount programme costs.</p>', unsafe_allow_html=True)

    m = st.columns(4)
    m[0].metric("Total discount", _money(disc),
                help="Every discount the till applied, in the window.")
    m[1].metric("Loyalty portion", _money(loy),
                help="The part attributable to loyalty offers.")
    m[2].metric("Everything else", _money(other),
                help="Group, employee, first-responder, promo and manual "
                     "discounts — total minus loyalty.")
    m[3].metric("Discount rate", _pct(disc / gross * 100 if gross else 0),
                help="Discount as a share of gross (net + discount).")

    m2 = st.columns(4)
    m2[0].metric("Discounted baskets", f"{int(day.discounted_baskets.sum()):,}")
    m2[1].metric("All baskets", f"{int(day.baskets.sum()):,}")
    m2[2].metric("% of baskets discounted",
                 _pct(day.discounted_baskets.sum()
                      / max(day.baskets.sum(), 1) * 100))
    m2[3].metric("Avg discount per discounted basket",
                 f"${disc / max(day.discounted_baskets.sum(), 1):,.2f}")

    # --- weekly trend, loyalty vs the rest --------------------------------
    wk = (day.groupby(["iso_year", "iso_week"])
             .agg(discount=("discount", "sum"),
                  loyalty=("loyalty_redeem", "sum"),
                  gross=("gross", "sum"),
                  day=("day", "min"))
             .reset_index()
             .sort_values("day"))
    wk["other"] = wk.discount - wk.loyalty
    wk["rate"] = wk.discount / wk.gross.replace(0, np.nan) * 100

    fig = go.Figure()
    fig.add_bar(x=wk.day, y=wk.loyalty, name="Loyalty",
                marker_color=accent, opacity=.85)
    fig.add_bar(x=wk.day, y=wk.other, name="Everything else",
                marker_color=pal[1] if len(pal) > 1 else "#B4632B",
                opacity=.85)
    fig.add_scatter(x=wk.day, y=wk.rate, name="Discount rate %",
                    yaxis="y2", mode="lines",
                    line=dict(color="#7A8590", width=2))
    fig.update_layout(
        barmode="stack", height=340,
        margin=dict(l=0, r=0, t=10, b=0),
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(title="Discount $", tickformat="$~s",
                   gridcolor="rgba(0,0,0,.07)"),
        yaxis2=dict(title="Rate %", overlaying="y", side="right",
                    showgrid=False),
        legend=dict(orientation="h", y=-.15))
    st.plotly_chart(fig, use_container_width=True, key="disc_cost_trend")

    # --- by store and channel ---------------------------------------------
    cL, cR = st.columns(2)
    with cL:
        st.markdown("**By store**")
        bs = (day.groupby("store_key")
                 .agg(discount=("discount", "sum"),
                      loyalty=("loyalty_redeem", "sum"),
                      gross=("gross", "sum"),
                      baskets=("baskets", "sum"),
                      dbaskets=("discounted_baskets", "sum"))
                 .reset_index())
        bs["other"] = bs.discount - bs.loyalty
        bs["rate"] = bs.discount / bs.gross.replace(0, np.nan) * 100
        bs["pct_baskets"] = bs.dbaskets / bs.baskets.replace(0, np.nan) * 100
        bs = bs.sort_values("rate", ascending=False)
        st.dataframe(pd.DataFrame({
            "Store": bs.store_key.map(lambda k: STORE_LABEL.get(k, str(k))),
            "Discount": bs.discount.round(0),
            "Loyalty": bs.loyalty.round(0),
            "Other": bs.other.round(0),
            "Rate %": bs.rate.round(2),
            "% baskets": bs.pct_baskets.round(1),
        }), use_container_width=True, hide_index=True, column_config={
            "Discount": st.column_config.NumberColumn(format="$%d"),
            "Loyalty": st.column_config.NumberColumn(format="$%d"),
            "Other": st.column_config.NumberColumn(format="$%d"),
            "Rate %": st.column_config.NumberColumn(format="%.2f%%"),
            "% baskets": st.column_config.NumberColumn(format="%.1f%%"),
        })
        if len(bs) > 1:
            hi, lo = bs.iloc[0], bs.iloc[-1]
            gap = (hi.rate - lo.rate) / 100 * hi.gross
            st.markdown(
                f'<p class="note">{STORE_LABEL.get(hi.store_key, hi.store_key)} '
                f'runs {hi.rate:.2f}% against '
                f'{STORE_LABEL.get(lo.store_key, lo.store_key)} at '
                f'{lo.rate:.2f}%. Closing that gap on the same gross would be '
                f'about {_money(gap)}.</p>', unsafe_allow_html=True)

    with cR:
        st.markdown("**By channel**")
        bc = (day.groupby("channel")
                 .agg(discount=("discount", "sum"),
                      loyalty=("loyalty_redeem", "sum"),
                      gross=("gross", "sum"),
                      dbaskets=("discounted_baskets", "sum"))
                 .reset_index())
        bc["rate"] = bc.discount / bc.gross.replace(0, np.nan) * 100
        bc = bc.sort_values("discount", ascending=False)
        st.dataframe(pd.DataFrame({
            "Channel": bc.channel,
            "Discount": bc.discount.round(0),
            "Loyalty": bc.loyalty.round(0),
            "Rate %": bc.rate.round(2),
            "Baskets": bc.dbaskets,
        }), use_container_width=True, hide_index=True, column_config={
            "Discount": st.column_config.NumberColumn(format="$%d"),
            "Loyalty": st.column_config.NumberColumn(format="$%d"),
            "Rate %": st.column_config.NumberColumn(format="%.2f%%"),
            "Baskets": st.column_config.NumberColumn(format="%d"),
        })

    # --- by brand, allocated ----------------------------------------------
    if table_exists and table_exists("dash_discount_brand"):
        st.markdown("**By brand**")
        st.markdown(
            '<div class="alert a-warn"><b>Allocated, not measured.</b> The '
            'till records a discount against the <i>basket</i>, not the line. '
            'These figures spread each basket\'s discount across its lines by '
            'net-sales share, so a single-brand discount on a mixed basket is '
            'smeared across every brand in it. Good for ranking which brands '
            'sit in discounted baskets; not a per-brand cost figure. The '
            'store and channel numbers above have no such caveat. This table '
            'is also whole-period — it carries no dates.</div>',
            unsafe_allow_html=True)
        br = q(f"""
            SELECT brand, category,
                   SUM(baskets)  AS baskets,
                   SUM(units)    AS units,
                   SUM(net)      AS net,
                   SUM(discount) AS discount,
                   SUM(margin)   AS margin
            FROM dash_discount_brand
            WHERE {where}
            GROUP BY 1,2
        """)
        if not br.empty:
            agg = (br.groupby("brand")
                     .agg(baskets=("baskets", "sum"), units=("units", "sum"),
                          net=("net", "sum"), discount=("discount", "sum"),
                          margin=("margin", "sum"))
                     .reset_index())
            agg["depth"] = (agg.discount
                            / (agg.net + agg.discount).replace(0, np.nan) * 100)
            agg["margin_rate"] = agg.margin / agg.net.replace(0, np.nan) * 100
            agg = agg.sort_values("discount", ascending=False)
            st.dataframe(pd.DataFrame({
                "Brand": agg.brand,
                "Discount (alloc.)": agg.discount.round(0),
                "Net in those baskets": agg.net.round(0),
                "Depth %": agg.depth.round(1),
                "Margin %": agg.margin_rate.round(1),
                "Baskets": agg.baskets,
            }), use_container_width=True, hide_index=True, height=380,
                column_config={
                    "Discount (alloc.)": st.column_config.NumberColumn(
                        format="$%d"),
                    "Net in those baskets": st.column_config.NumberColumn(
                        format="$%d"),
                    "Depth %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Baskets": st.column_config.NumberColumn(format="%d"),
            })

            top = agg.head(15).sort_values("discount")
            fig = px.bar(top, x="discount", y="brand", orientation="h",
                         color_discrete_sequence=[accent],
                         labels={"discount": "Allocated discount $",
                                 "brand": ""})
            fig.update_layout(height=max(300, 32 * len(top)),
                              margin=dict(l=0, r=0, t=10, b=0),
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_xaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
            st.plotly_chart(fig, use_container_width=True, key="disc_brand")



# ------------------------------------------------------- who got the discount

def _render_groups(q, where, H, accent, pal, table_exists):
    """Discount attributed to named customer groups.

    The only place a discount has a NAME. Everything else in the published
    data records an amount; this records who was entitled to it and why.

    Ever-member by default, since 90% of the audit's memberships never close
    and the file is left-censored at 2025-07-01 anyway. The windowed figure --
    baskets falling between first_added and last_removed -- is shown alongside
    as the conservative floor, not as the headline.
    """
    if table_exists and not table_exists("dash_discount_group"):
        return

    g = q(f"""
        SELECT group_name, group_kind,
               SUM(members)          AS members,
               SUM(baskets)          AS baskets,
               SUM(net)              AS net,
               SUM(discount)         AS discount,
               SUM(loyalty)          AS loyalty,
               SUM(other_discount)   AS other_discount,
               SUM(windowed_baskets) AS w_baskets,
               SUM(windowed_other)   AS w_other
        FROM dash_discount_group
        WHERE {where}
        GROUP BY 1,2
    """)
    if g.empty:
        return

    st.divider()
    H("Who got the discount")
    st.markdown(
        '<p class="note">Discount attributed to named customer groups, from '
        'the Customer Discount Group Audit. This is the only source that '
        'names a discount — the POS export records an amount and no reason. '
        'Figures are <b>ever-member</b>: any discounted basket by someone who '
        'was ever in the group.</p>', unsafe_allow_html=True)

    tiers = g[g.group_kind == "Loyalty tier"]
    real = g[g.group_kind != "Loyalty tier"]

    m = st.columns(4)
    m[0].metric("Groups with spend", f"{len(real):,}")
    m[1].metric("Members", f"{int(real.members.sum()):,}")
    m[2].metric("Non-loyalty discount", _money(real.other_discount.sum()),
                help="Total discount minus the loyalty-offer portion, for "
                     "members of non-tier groups.")
    m[3].metric("Conservative floor", _money(real.w_other.sum()),
                help="Same figure counting only baskets between the member's "
                     "add and remove dates. Lower because most memberships "
                     "predate or outlast the audit window.")

    if not tiers.empty:
        st.markdown(
            f'<p class="note">Loyalty tiers ({", ".join(tiers.group_name)}) '
            f'are excluded from the figures above — they account for '
            f'{_money(tiers.other_discount.sum())} and are a tier benefit '
            f'rather than a group discount. Tier behaviour lives on the '
            f'Loyalty tab.</p>', unsafe_allow_html=True)

    # --- by kind ----------------------------------------------------------
    kind = (real.groupby("group_kind")
                .agg(groups=("group_name", "nunique"),
                     members=("members", "sum"),
                     baskets=("baskets", "sum"),
                     net=("net", "sum"),
                     other=("other_discount", "sum"))
                .reset_index()
                .sort_values("other", ascending=False))
    kind["depth"] = (kind.other
                     / (kind.net + kind.other).replace(0, np.nan) * 100)

    cL, cR = st.columns([3, 2])
    with cL:
        st.dataframe(pd.DataFrame({
            "Kind": kind.group_kind,
            "Groups": kind.groups,
            "Members": kind.members,
            "Baskets": kind.baskets,
            "Discount": kind.other.round(0),
            "Depth %": kind.depth.round(1),
        }), use_container_width=True, hide_index=True, column_config={
            "Groups": st.column_config.NumberColumn(format="%d"),
            "Members": st.column_config.NumberColumn(format="%d"),
            "Baskets": st.column_config.NumberColumn(format="%d"),
            "Discount": st.column_config.NumberColumn(format="$%d"),
            "Depth %": st.column_config.NumberColumn(format="%.1f%%"),
        })
    with cR:
        fig = px.pie(kind, values="other", names="group_kind", hole=.55,
                     color_discrete_sequence=pal)
        fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0),
                          showlegend=False)
        fig.update_traces(textposition="inside", textinfo="percent")
        st.plotly_chart(fig, use_container_width=True, key="disc_grp_kind")

    # --- per group --------------------------------------------------------
    st.markdown("**Every group**")
    pick = st.multiselect("Kind", options=list(kind.group_kind),
                          default=list(kind.group_kind), key="disc_grp_kind_pick")
    sub = real[real.group_kind.isin(pick)] if pick else real.iloc[0:0]
    if not sub.empty:
        sub = sub.sort_values("other_discount", ascending=False).copy()
        sub["depth"] = (sub.other_discount
                        / (sub.net + sub.other_discount).replace(0, np.nan) * 100)
        sub["per_member"] = sub.other_discount / sub.members.replace(0, np.nan)
        sub["baskets_per_member"] = sub.baskets / sub.members.replace(0, np.nan)
        st.dataframe(pd.DataFrame({
            "Group": sub.group_name,
            "Kind": sub.group_kind,
            "Members": sub.members,
            "Baskets": sub.baskets,
            "Baskets/member": sub.baskets_per_member.round(1),
            "Discount": sub.other_discount.round(0),
            "$/member": sub.per_member.round(0),
            "Depth %": sub.depth.round(1),
            "Net": sub.net.round(0),
        }), use_container_width=True, hide_index=True, height=440,
            column_config={
                "Members": st.column_config.NumberColumn(format="%d"),
                "Baskets": st.column_config.NumberColumn(format="%d"),
                "Baskets/member": st.column_config.NumberColumn(format="%.1f"),
                "Discount": st.column_config.NumberColumn(format="$%d"),
                "$/member": st.column_config.NumberColumn(format="$%d"),
                "Depth %": st.column_config.NumberColumn(format="%.1f%%"),
                "Net": st.column_config.NumberColumn(format="$%d"),
        })

        # Heavy individual use: a neighbour discount meant for a shop's staff
        # being run by one person dozens of times is worth seeing by name of
        # group, without naming the customer.
        heavy = sub[(sub.members <= 3) & (sub.baskets >= 20)]
        if not heavy.empty:
            st.markdown(
                '<div class="alert a-warn"><b>Concentrated use.</b> These '
                'groups have three or fewer members but 20+ discounted '
                'baskets — a rate intended for a business\'s staff being used '
                'as one person\'s standing discount. Worth confirming the '
                'membership is still current.</div>', unsafe_allow_html=True)
            st.dataframe(pd.DataFrame({
                "Group": heavy.group_name,
                "Members": heavy.members,
                "Baskets": heavy.baskets,
                "Discount": heavy.other_discount.round(0),
            }), use_container_width=True, hide_index=True, column_config={
                "Members": st.column_config.NumberColumn(format="%d"),
                "Baskets": st.column_config.NumberColumn(format="%d"),
                "Discount": st.column_config.NumberColumn(format="$%d"),
            })

    with st.expander("What this does and does not cover"):
        st.markdown(
            "- **Left-censored.** The audit starts 2025-07-01. Anyone added "
            "before that has no Added event and is missing unless they were "
            "later removed.\n"
            "- **Partial match.** Roughly 73% of audit customer IDs join to "
            "basket data. The rest never transacted, or transacted under a "
            "different customer key.\n"
            "- **A minority of the total.** Group members account for around "
            "15% of non-loyalty discount chain-wide. The remainder is promo "
            "codes, manual write-downs, and pre-window members.\n"
            "- **Whole-period.** This table carries no dates, so the weeks "
            "slider does not apply. The store filter does.\n"
            "- **Refresh** by re-exporting the audit and re-running "
            "`ingest_discount_groups.py` then `publish.py`.")

    _render_group_lift(q, H, table_exists)



def _render_group_lift(q, H, table_exists=None):
    """Spend per member either side of enrolment.

    Replaces basket size as the way to judge a discount group. Basket size
    is not a behavioural measure here: a group whose members carry offers on
    most of their baskets will show a large basket whether or not membership
    changed anything. This measures the member against their own prior
    quarter instead.

    No store filter. The comparison is per member around that member's own
    enrolment date, so faceting by store would split one member's window.
    """
    if table_exists and not table_exists("dash_group_lift"):
        return
    d = q("SELECT * FROM dash_group_lift ORDER BY members DESC")
    if d.empty:
        return
    meta = q("SELECT * FROM dash_group_lift_meta")
    win = int(meta.iloc[0].window_days) if not meta.empty else 90
    minm = int(meta.iloc[0].min_members) if not meta.empty else 30

    st.divider()
    H(f"Did membership change spending? ({win} days either side)")
    st.markdown(
        f'<p class="note"><b>Why not basket size.</b> Groups with generous '
        f'offers carry a discount on most of their baskets, and a basket '
        f'with an offer on it looks bigger. That makes basket size a '
        f'statement about offer attachment rather than about spending. This '
        f'section compares each member against <i>themselves</i>: total '
        f'spend in the {win} days before they joined against the {win} days '
        f'after. Members who had not been customers for at least {win} days '
        f'before joining are excluded — their "before" window would be '
        f'partly pre-customer and would invent a lift.</p>',
        unsafe_allow_html=True)

    if "interpretable" in d.columns:
        skip = d[~d.interpretable.astype(bool)]
        d = d[d.interpretable.astype(bool)]
    else:
        skip = d.iloc[0:0]
    if d.empty:
        return

    d["extra_disc"] = d.discount_post - d.discount_pre
    d["ci"] = [
        "--" if pd.isna(r.ci_lo) else
        f"[{r.ci_lo:+,.0f}, {r.ci_hi:+,.0f}]"
        + ("  \u2713" if r.excludes_zero else "")
        for _, r in d.iterrows()]

    st.dataframe(pd.DataFrame({
        "Group": d.group_name,
        "Members": d.members,
        f"Spend, prior {win}d": d.spend_pre.round(0),
        f"Spend, next {win}d": d.spend_post.round(0),
        "Median change": d.median_change.round(0),
        "95% interval": d.ci,
        "% who spent more": d.pct_increased.round(0),
        "Visits before": d.visits_pre.round(1),
        "Visits after": d.visits_post.round(1),
        "Extra discount": d.extra_disc.round(0),
        "Median margin change": d.median_margin_change.round(0),
    }), hide_index=True, use_container_width=True)

    st.caption(
        f"Per member, averaged. **Median change** is the typical member's "
        f"change in net spend, with a bootstrap interval beside it — a check "
        f"mark means the interval excludes zero. Use the median rather than "
        f"the difference of the two averages: a handful of large members "
        f"move an average a long way.  \n"
        f"**Extra discount** is what the group cost per member over the same "
        f"window. Read it against **median margin change**, not against the "
        f"spend change — spend is already net of discount, so comparing the "
        f"two would count the discount twice.  \n"
        f"Groups with fewer than {minm} measurable members are not shown. "
        f"Members who predate the audit window are excluded throughout.  \n"
        f"**Travel Club Frequent Flyer** appears here despite being "
        f"classified as a loyalty tier: it is a paid membership rather than "
        f"an earned tier, so before-and-after enrolment is a meaningful "
        f"comparison for it in a way it is not for a tier someone reaches "
        f"by spending.")

    if not skip.empty:
        with st.expander("Staff and friends-and-family groups "
                         f"({len(skip)} not shown)"):
            st.caption(
                "Membership in these groups tracks employment, so a member "
                "who leaves the group is usually someone who left the job. "
                "A spend decline reads as attrition rather than as a change "
                "in shopping, and the before-and-after comparison cannot "
                "separate the two. Shown for completeness only.")
            st.dataframe(pd.DataFrame({
                "Group": skip.group_name,
                "Members": skip.members,
                f"Spend, prior {win}d": skip.spend_pre.round(0),
                f"Spend, next {win}d": skip.spend_post.round(0),
                "Median change": skip.median_change.round(0),
                "Visits before": skip.visits_pre.round(1),
                "Visits after": skip.visits_post.round(1),
            }), hide_index=True, use_container_width=True)


# ------------------------------------------------- local-only: policing view

# This section reads the audit export and the ETL database straight off disk.
# Neither exists on Streamlit Cloud, so it renders locally and silently
# disappears when deployed. That is deliberate: it shows customer names, and
# publish.py strips every identifier before cerebral_dash.duckdb leaves the
# machine. Keeping the lookup out of the published file means names cannot
# travel even by accident.

LOCAL_DB = os.path.expanduser("~/cerebral/tta.duckdb")

PAID_MEMBERSHIP = {"travel club frequent flyer", "travel club",
                   "frequent flyer"}


def _kind(name: str) -> str:
    low = str(name or "").strip().lower()
    if low in PAID_MEMBERSHIP:
        return "Paid membership"
    if "employee" in low:
        return "Employee"
    if "first responder" in low or "veteran" in low:
        return "First responder / veteran"
    if "retail worker" in low or "friends and family" in low:
        return "Staff / friends & family"
    if ("drinks on us" in low or "drinksonus" in low
            or "drink on us" in low):
        return "Neighbour business"
    if low.startswith(("soho -", "soho-", "5th ave", "5thave", "usq ",
                       "dtbk ", "fifth ave")):
        return "Neighbour business"
    return "Other"


def _find_audit():
    pats = ["*Discount Group Audit*.xls*", "*Discount_Group_Audit*.xls*"]
    for root in (".", os.path.expanduser("~/cerebral"),
                 os.path.expanduser("~/cerebral/Cerebral")):
        for pat in pats:
            hits = glob.glob(os.path.join(root, pat))
            if hits:
                return sorted(hits)[-1]
    return None


@st.cache_data(show_spinner=False)
def _load_multi(audit_path: str, mtime: float):
    """Open memberships per customer, joined to local transaction context.

    Cached on the file's mtime so re-exporting the audit invalidates it.
    """
    import duckdb

    a = pd.read_excel(audit_path, header=3)
    a["ts"] = pd.to_datetime(a["Time"], errors="coerce")
    a = a.dropna(subset=["ts", "Customer ID", "Discount Description"])
    a["cid"] = a["Customer ID"].astype("Int64").astype(str)
    a["grp"] = a["Discount Description"].astype(str).str.strip()
    a["nm"] = a["Customer Name"].astype(str).str.strip()

    rows = []
    for (cid, grp), d in a.sort_values("ts").groupby(["cid", "grp"]):
        last = d.iloc[-1]
        if str(last.Action).strip() != "Added":
            continue
        rows.append({"cid": cid, "nm": d["nm"].iloc[-1], "grp": grp,
                     "kind": _kind(grp), "added": last.ts,
                     "by": str(last.get("Performed By", "")).strip()})
    mem = pd.DataFrame(rows)
    if mem.empty:
        return mem, mem

    per = (mem.groupby("cid")
              .agg(nm=("nm", "last"),
                   groups=("grp", lambda s: " | ".join(sorted(set(s)))),
                   kinds=("kind", lambda s: " | ".join(sorted(set(s)))),
                   n_groups=("grp", "nunique"),
                   granted=("kind", lambda s: sum(
                       1 for x in set(s) if x != "Paid membership")),
                   by=("by", lambda s: " | ".join(sorted(
                       {x for x in s if x}))[:120]))
              .reset_index())

    con = duckdb.connect(LOCAL_DB, read_only=True)
    con.register("ids", per[["cid"]])
    tx = con.execute("""
        SELECT b.customer_key AS cid,
               COUNT(*) FILTER (WHERE COALESCE(b.discount_amt,0) > 0)
                                                      AS disc_baskets,
               SUM(COALESCE(b.discount_amt,0))        AS discount,
               SUM(COALESCE(b.discount_amt,0)
                   - COALESCE(b.loyalty_redeem,0))    AS other_disc,
               SUM(b.basket_net)                      AS net,
               MODE(b.store_key)                      AS home_store,
               MAX(CAST(b.txn_ts AS DATE))            AS last_seen
        FROM fact_basket b JOIN ids i ON b.customer_key = i.cid
        WHERE NOT b.is_return
        GROUP BY 1
    """).df()
    con.close()

    per = per.merge(tx, on="cid", how="left")
    for c in ("disc_baskets", "discount", "other_disc", "net"):
        per[c] = per[c].fillna(0)
    per["store"] = per.home_store.map(
        lambda k: STORE_LABEL.get(int(k), str(k)) if pd.notna(k)
        else "never transacted")
    per["depth"] = (per.other_disc
                    / (per.net + per.discount).replace(0, np.nan) * 100)
    return per, mem


def _render_policing(H):
    """Accounts holding more than one discount entitlement, by name."""
    audit = _find_audit()
    if not audit or not os.path.exists(LOCAL_DB):
        return

    try:
        per, mem = _load_multi(audit, os.path.getmtime(audit))
    except Exception as exc:                                   # noqa: BLE001
        st.warning(f"Could not read the discount group audit: {exc}")
        return
    if per.empty:
        return

    multi = per[per.n_groups > 1].sort_values(
        ["granted", "other_disc"], ascending=[False, False])
    gg = multi[multi.granted > 1]

    st.divider()
    H("Accounts with more than one entitlement")
    st.markdown(
        '<div class="alert a-warn"><b>Local only — names shown.</b> This '
        'section reads the audit export and the ETL database from disk, '
        'neither of which is deployed. It does not appear in the published '
        'dashboard, and no identifier here reaches the published file. '
        'Treat what follows as an internal document.</div>',
        unsafe_allow_html=True)

    m = st.columns(4)
    m[0].metric("Multi-entitlement accounts", f"{len(multi):,}")
    m[1].metric("Two or more granted", f"{len(gg):,}",
                help="Excludes a paid Frequent Flyer membership held "
                     "alongside a single granted rate.")
    m[2].metric("Discount taken", _money(multi.other_disc.sum()),
                help="Non-loyalty portion, whole history.")
    m[3].metric("Of which, granted+granted", _money(gg.other_disc.sum()))

    st.markdown(
        '<p class="note">Frequent Flyer is a <b>purchased</b> $100 '
        'membership, not an earned tier, so holding it alongside one granted '
        'rate is a policy question rather than an error. Two <i>granted</i> '
        'rates on one account is the thing to chase. This shows who holds '
        'multiple entitlements — not that both were applied to the same '
        'basket.</p>', unsafe_allow_html=True)

    only_gg = st.checkbox("Show only two-or-more granted", value=True,
                          key="disc_pol_gg")
    view = gg if only_gg else multi
    if view.empty:
        st.info("Nothing to show for that filter.")
    else:
        st.dataframe(pd.DataFrame({
            "Customer": view.nm,
            "ID": view.cid,
            "Groups held": view.groups,
            "#": view.n_groups,
            "Granted": view.granted,
            "Store": view.store,
            "Disc. baskets": view.disc_baskets.astype(int),
            "Discount": view.other_disc.round(0),
            "Net": view.net.round(0),
            "Depth %": view.depth.round(1),
            "Granted by": view.by,
        }), use_container_width=True, hide_index=True, height=460,
            column_config={
                "#": st.column_config.NumberColumn(format="%d"),
                "Granted": st.column_config.NumberColumn(format="%d"),
                "Disc. baskets": st.column_config.NumberColumn(format="%d"),
                "Discount": st.column_config.NumberColumn(format="$%d"),
                "Net": st.column_config.NumberColumn(format="$%d"),
                "Depth %": st.column_config.NumberColumn(format="%.1f%%"),
        })

    # Who granted them. A single name across many rows is one conversation,
    # not forty.
    if not gg.empty:
        who = (gg.assign(g=gg.by.str.split(" | ", regex=False))
                 .explode("g"))
        who = who[who.g.astype(bool)]
        if not who.empty:
            tally = (who.groupby("g")
                        .agg(accounts=("cid", "nunique"),
                             discount=("other_disc", "sum"))
                        .reset_index()
                        .sort_values("accounts", ascending=False)
                        .head(15))
            st.markdown("**Who granted the overlapping entitlements**")
            st.dataframe(pd.DataFrame({
                "Granted by": tally.g,
                "Accounts": tally.accounts,
                "Discount on those accounts": tally.discount.round(0),
            }), use_container_width=True, hide_index=True, column_config={
                "Accounts": st.column_config.NumberColumn(format="%d"),
                "Discount on those accounts":
                    st.column_config.NumberColumn(format="$%d"),
            })
            st.markdown(
                '<p class="note">An account can appear under more than one '
                'name when its entitlements were granted separately. Counts '
                'are of accounts touched, not of errors made.</p>',
                unsafe_allow_html=True)

    with st.expander("Limits of this view"):
        st.markdown(
            "- **Open memberships only.** A customer x group pair counts "
            "when its last recorded action is an Added.\n"
            "- **Left-censored.** The audit begins 2025-07-01; anyone added "
            "before then is missing unless later removed and re-added.\n"
            "- **Partial match.** About 73% of audit IDs join to basket "
            "data. 'never transacted' means the entitlement exists but no "
            "purchase history does.\n"
            "- **Whole history.** Discount figures cover the customer's "
            "entire record, not only the period they held the "
            "entitlement.\n"
            "- **Store** is where they shop most, not an assignment.\n"
            "- Re-export the audit to refresh; this reads the newest file "
            "matching *Discount Group Audit* in the project or cerebral "
            "folder.")


# ------------------------------------------------------------------ render

def render_discounting(q, keys, keep, stores, heading=None, table_exists=None,
                       accent="#2F6F4F", series=None):
    """Render the Discounting tab.

    q             -- the app's cached query helper
    keys          -- selected store_key list from the sidebar
    keep          -- set of (iso_year, iso_week) tuples from the weeks slider
    stores        -- the STORES dict, for the all-selected test
    heading       -- the app's heading() helper (optional)
    table_exists  -- the app's table_exists() helper (optional)
    accent        -- ACCENT from the app's palette (optional)
    series        -- SERIES list from the app's palette (optional)
    """
    H = heading or (lambda t, term=None: st.markdown(f"##### {t}"))
    pal = series or [accent]
    where = _store_clause(keys, stores)

    # --- what discounting costs -------------------------------------------
    # dash_discount_day is the only table on this tab with a real date grain,
    # so these sections honour the weeks slider. Everything below the divider
    # marked "whole loaded period" does not, and says so.
    _render_cost(q, where, keep, H, accent, pal, table_exists)
    _render_groups(q, where, H, accent, pal, table_exists)
    _render_policing(H)

    if table_exists and not table_exists("dash_offer_performance"):
        return

    # --- 1. discount type mix ---------------------------------------------
    st.divider()
    st.markdown(
        '<div class="howto"><b>Everything below covers the whole loaded '
        'period</b>, not the weeks slider. These tables carry no per-day '
        'grain, so a window filter would change the heading without changing '
        'the numbers. The store filter is still respected. For '
        'window-scoped discount figures, use the section above.</div>',
        unsafe_allow_html=True)
    H("The mix — what kind of discounting is this?")

    off = q(f"""
        SELECT offer_name, brand, category,
               SUM(redemptions)  AS units,
               SUM(redeem_value) AS spend,
               MIN(first_seen)   AS first_seen,
               MAX(last_seen)    AS last_seen
        FROM dash_offer_performance
        WHERE {where}
        GROUP BY 1,2,3
    """)

    if off.empty:
        st.info("No offer data for the selected stores.")
        return

    off["Discount type"] = off.offer_name.map(_classify)

    mix = (off.groupby("Discount type")
              .agg(offers=("offer_name", "nunique"),
                   units=("units", "sum"),
                   spend=("spend", "sum"))
              .reset_index())
    mix["share"] = mix.spend / mix.spend.sum() * 100
    mix["avg_unit"] = mix.spend / mix.units.replace(0, np.nan)
    mix = _order_types(mix)

    m = st.columns(4)
    m[0].metric("Redeemed units", f"{int(mix.units.sum()):,}")
    m[1].metric("Redemption value", _money(mix.spend.sum()),
                help="Face value of the points customers spent, at one "
                     "dollar per point. What the reward was worth to them, "
                     "not what it cost us.")
    m[2].metric("Distinct offers", f"{int(mix.offers.sum()):,}")
    m[3].metric("Discount types", f"{len(mix):,}")

    st.markdown(
        '<p class="note"><b>Redemption value</b> is the face value of the '
        'points customers spent on the loyalty menu, at one dollar per '
        'point. It is not revenue, and it is not our cost — these are points '
        'people earned by shopping, and the SKUs they redeem against are '
        'rebated by the brands supplying them. Rebate terms are not in this '
        'data, so what the programme actually costs cannot be read off this '
        'table. Gift-with-purchase from takeovers is not here; it carries '
        'its own "(GWP)" SKU and is reconciled on the Takeovers tab.</p>',
        unsafe_allow_html=True)

    cL, cR = st.columns([3, 2])
    with cL:
        st.dataframe(pd.DataFrame({
            "Discount type": mix["Discount type"],
            "Offers": mix.offers,
            "Units": mix.units,
            "Redemption value": mix.spend.round(0),
            "Share of value": mix.share.round(1),
            "Avg value / unit": mix.avg_unit.round(2),
        }), use_container_width=True, hide_index=True, column_config={
            "Offers": st.column_config.NumberColumn(format="%d"),
            "Units": st.column_config.NumberColumn(format="%d"),
            "Redemption value": st.column_config.NumberColumn(format="$%d"),
            "Share of value": st.column_config.NumberColumn(format="%.1f%%"),
            "Avg value / unit": st.column_config.NumberColumn(format="$%.2f"),
        })
    with cR:
        fig = px.pie(mix, values="spend", names="Discount type", hole=.55,
                     color_discrete_sequence=pal)
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                          showlegend=False)
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True, key="disc_mix")

    with st.expander("What each discount type means"):
        for t in mix["Discount type"]:
            st.markdown(f"**{t}** — {TYPE_NOTE.get(t, 'Unclassified.')}")

    # --- 2. offers and SKUs -----------------------------------------------
    st.divider()
    H("Offers and SKUs")
    st.markdown(
        '<p class="note">Offer-level detail: the specific campaign and the '
        'product the customer actually received. Nearly all of this is the '
        'loyalty menu; a Secret Drop is a different instrument and is worth '
        'filtering out before reading the rest.</p>',
        unsafe_allow_html=True)

    pick = st.multiselect(
        "Discount type", options=list(mix["Discount type"]),
        default=list(mix["Discount type"]), key="disc_type_pick")
    sub = off[off["Discount type"].isin(pick)] if pick else off.iloc[0:0]

    if sub.empty:
        st.info("No offers match that selection.")
    else:
        det = (sub.groupby(["offer_name", "Discount type"])
                  .agg(units=("units", "sum"),
                       spend=("spend", "sum"),
                       skus=("offer_name", "size"),
                       first_seen=("first_seen", "min"),
                       last_seen=("last_seen", "max"))
                  .reset_index()
                  .sort_values("spend", ascending=False))
        det["avg_unit"] = det.spend / det.units.replace(0, np.nan)

        st.dataframe(pd.DataFrame({
            "Offer": det.offer_name,
            "Type": det["Discount type"],
            "SKUs": det.skus,
            "Units": det.units,
            "Redemption value": det.spend.round(0),
            "Avg value / unit": det.avg_unit.round(2),
            "First seen": pd.to_datetime(det.first_seen).dt.date,
            "Last seen": pd.to_datetime(det.last_seen).dt.date,
        }), use_container_width=True, hide_index=True, height=420,
            column_config={
                "SKUs": st.column_config.NumberColumn(format="%d"),
                "Units": st.column_config.NumberColumn(format="%d"),
                "Redemption value": st.column_config.NumberColumn(
                    format="$%d"),
                "Avg value / unit": st.column_config.NumberColumn(
                    format="$%.2f"),
        })

        top = det.head(15).sort_values("spend")
        fig = px.bar(top, x="spend", y="offer_name", orientation="h",
                     color="Discount type", color_discrete_sequence=pal,
                     labels={"spend": "Net sales $", "offer_name": ""})
        fig.update_layout(height=max(300, 34 * len(top)),
                          margin=dict(l=0, r=0, t=10, b=0),
                          plot_bgcolor="rgba(0,0,0,0)",
                          legend=dict(orientation="h", y=-.12))
        fig.update_xaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
        st.plotly_chart(fig, use_container_width=True, key="disc_offers")

        # SKU drill-down for one offer, so a campaign can be opened up.
        one = st.selectbox("Open an offer", options=["—"] + list(det.offer_name),
                           key="disc_offer_one")
        if one and one != "—":
            rows = (off[off.offer_name == one]
                    .sort_values("spend", ascending=False))
            st.dataframe(pd.DataFrame({
                "Product": rows["product"] if "product" in rows else rows.brand,
                "Brand": rows.brand,
                "Category": rows.category,
                "Units": rows.units,
                "Net sales": rows.spend.round(0),
            }), use_container_width=True, hide_index=True, column_config={
                "Units": st.column_config.NumberColumn(format="%d"),
                "Net sales": st.column_config.NumberColumn(format="$%d"),
            })

    # --- 3. brand redeemer cohorts ----------------------------------------
    if table_exists and table_exists("dash_promo_brand"):
        st.divider()
        H("Did the discount buy a lasting customer? — by brand")
        st.markdown(
            '<div class="howto"><b>Different question, different table.</b> '
            'The sections above measure discount <i>volume</i>. This one '
            'measures <i>consequence</i>: of the customers who redeemed an '
            "offer on this brand, how many came back, and how many had "
            'lapsed by 30, 60 and 90 days. A high churn figure next to high '
            'spend is a brand whose discounting bought transactions rather '
            'than customers.</div>', unsafe_allow_html=True)

        pb = q(f"""
            SELECT brand,
                   SUM(customers)       AS customers,
                   SUM(repeat_buyers)   AS repeat_buyers,
                   SUM(spend_sum)       AS spend,
                   SUM(gm_sum)          AS margin,
                   SUM(churned_30)      AS churned_30,
                   SUM(churned_60)      AS churned_60,
                   SUM(churned_90)      AS churned_90
            FROM dash_promo_brand
            WHERE {where}
            GROUP BY 1
            ORDER BY spend DESC
        """)
        if pb.empty:
            st.info("No brand cohort data for the selected stores.")
        else:
            pb["repeat_rate"] = pb.repeat_buyers / pb.customers.replace(0, np.nan) * 100
            pb["churn_90"] = pb.churned_90 / pb.customers.replace(0, np.nan) * 100
            pb["margin_rate"] = pb.margin / pb.spend.replace(0, np.nan) * 100

            k = st.columns(4)
            k[0].metric("Redeemers", f"{int(pb.customers.sum()):,}")
            k[1].metric("Repeat buyers", f"{int(pb.repeat_buyers.sum()):,}")
            k[2].metric("Spend", _money(pb.spend.sum()))
            k[3].metric("Lapsed by 90d",
                        _pct(pb.churned_90.sum() / max(pb.customers.sum(), 1) * 100))

            st.dataframe(pd.DataFrame({
                "Brand": pb.brand,
                "Redeemers": pb.customers,
                "Repeat": pb.repeat_buyers,
                "Repeat %": pb.repeat_rate.round(1),
                "Spend": pb.spend.round(0),
                "Margin %": pb.margin_rate.round(1),
                "Lapsed 30d": pb.churned_30,
                "Lapsed 90d": pb.churned_90,
                "Lapsed 90d %": pb.churn_90.round(1),
            }), use_container_width=True, hide_index=True, height=420,
                column_config={
                    "Redeemers": st.column_config.NumberColumn(format="%d"),
                    "Repeat": st.column_config.NumberColumn(format="%d"),
                    "Repeat %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Spend": st.column_config.NumberColumn(format="$%d"),
                    "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Lapsed 30d": st.column_config.NumberColumn(format="%d"),
                    "Lapsed 90d": st.column_config.NumberColumn(format="%d"),
                    "Lapsed 90d %": st.column_config.NumberColumn(
                        format="%.1f%%"),
            })

            big = pb[pb.customers >= 25].head(25)
            if not big.empty:
                fig = px.scatter(
                    big, x="repeat_rate", y="churn_90", size="spend",
                    hover_name="brand", color_discrete_sequence=[accent],
                    labels={"repeat_rate": "Repeat rate %",
                            "churn_90": "Lapsed by 90 days %"})
                fig.update_layout(height=380,
                                  margin=dict(l=0, r=0, t=10, b=0),
                                  plot_bgcolor="rgba(0,0,0,0)")
                fig.update_xaxes(gridcolor="rgba(0,0,0,.07)")
                fig.update_yaxes(gridcolor="rgba(0,0,0,.07)")
                st.plotly_chart(fig, use_container_width=True,
                                key="disc_brand_scatter")
                st.markdown(
                    '<p class="note">Bubble size is spend. Bottom-right is '
                    'where you want a brand: customers came back and few '
                    'lapsed. Top-left bought a transaction. Brands with '
                    'fewer than 25 redeemers are omitted — the rates are too '
                    'noisy to plot.</p>', unsafe_allow_html=True)

    # --- 4. category redeemer cohorts -------------------------------------
    if table_exists and table_exists("dash_promo_category"):
        st.divider()
        H("Did the discount buy a lasting customer? — by category")

        pc = q(f"""
            SELECT category,
                   SUM(customers)     AS customers,
                   SUM(repeat_buyers) AS repeat_buyers,
                   SUM(spend_sum)     AS spend,
                   SUM(gm_sum)        AS margin,
                   SUM(churned_30)    AS churned_30,
                   SUM(churned_90)    AS churned_90
            FROM dash_promo_category
            WHERE {where}
            GROUP BY 1
            ORDER BY spend DESC
        """)
        if pc.empty:
            st.info("No category cohort data for the selected stores.")
        else:
            pc["repeat_rate"] = pc.repeat_buyers / pc.customers.replace(0, np.nan) * 100
            pc["churn_90"] = pc.churned_90 / pc.customers.replace(0, np.nan) * 100
            pc["margin_rate"] = pc.margin / pc.spend.replace(0, np.nan) * 100

            st.dataframe(pd.DataFrame({
                "Category": pc.category,
                "Redeemers": pc.customers,
                "Repeat %": pc.repeat_rate.round(1),
                "Spend": pc.spend.round(0),
                "Margin %": pc.margin_rate.round(1),
                "Lapsed 90d %": pc.churn_90.round(1),
            }), use_container_width=True, hide_index=True, column_config={
                "Redeemers": st.column_config.NumberColumn(format="%d"),
                "Repeat %": st.column_config.NumberColumn(format="%.1f%%"),
                "Spend": st.column_config.NumberColumn(format="$%d"),
                "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
                "Lapsed 90d %": st.column_config.NumberColumn(format="%.1f%%"),
            })

            fig = px.bar(pc.sort_values("spend"), x="spend", y="category",
                         orientation="h", color_discrete_sequence=[accent],
                         labels={"spend": "Spend $", "category": ""})
            fig.update_layout(height=max(260, 34 * len(pc)),
                              margin=dict(l=0, r=0, t=10, b=0),
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_xaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
            st.plotly_chart(fig, use_container_width=True, key="disc_cat")

    # --- 5. loyalty offer x tier ------------------------------------------
    if table_exists and table_exists("dash_loyalty_offer"):
        st.divider()
        H("Which tier redeems what")
        st.markdown(
            '<p class="note">Loyalty offers split by customer tier. This is '
            'the only table on this tab with a week grain, but it is shown '
            'whole-period for consistency with the sections above.</p>',
            unsafe_allow_html=True)

        lo = q(f"""
            SELECT offer_name, tier,
                   SUM(customers)    AS customers,
                   SUM(redemptions)  AS units,
                   SUM(redeem_value) AS spend
            FROM dash_loyalty_offer
            WHERE {where}
            GROUP BY 1,2
        """)
        if lo.empty:
            st.info("No loyalty offer data for the selected stores.")
        else:
            lo["Discount type"] = lo.offer_name.map(_classify)
            piv = (lo.pivot_table(index="Discount type", columns="tier",
                                  values="spend", aggfunc="sum", fill_value=0)
                     .reset_index())
            piv = _order_types(piv)
            cfg = {c: st.column_config.NumberColumn(format="$%d")
                   for c in piv.columns if c != "Discount type"}
            st.dataframe(piv, use_container_width=True, hide_index=True,
                         column_config=cfg)

            bytier = (lo.groupby("tier")
                        .agg(customers=("customers", "sum"),
                             units=("units", "sum"),
                             spend=("spend", "sum"))
                        .reset_index()
                        .sort_values("spend", ascending=False))
            fig = px.bar(bytier, x="tier", y="spend",
                         color_discrete_sequence=[accent],
                         labels={"spend": "Net sales $", "tier": ""})
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
            st.plotly_chart(fig, use_container_width=True, key="disc_tier")

