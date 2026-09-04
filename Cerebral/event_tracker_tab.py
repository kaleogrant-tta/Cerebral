"""
event_tracker_tab.py -- Event Performance & Cost (phase one) for the Events tab.

    from event_tracker_tab import render_event_tracker
    ...
    try:
        render_event_tracker(q, H, table_exists)
    except Exception as exc:
        st.caption(f"Event tracker unavailable: {exc}")

Reads dash_event_tracker and dash_event_tracker_meta. Renders only.

Every rate names its denominator in the header. Nothing here is per
attendee: signups are people who registered, and check-in data is not
reliable enough to say who came.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from heat import heat_table
from glossary import tip

BUCKET_LABEL = {"new": "New to TTA", "active": "Active",
                "lapsed": "Lapsed"}
BUCKET_ORDER = ["new", "active", "lapsed"]
MIN_NEW_FOR_COST = 5      # cost per target-bucket buyer needs this many
LOYALTY_TYPES = {"Loyalty"}   # judged on the Active bucket, not New
EARLY_MIN_SIGNUPS = 20    # early-signal flag needs this many target signups


def _money(x):
    return "-" if pd.isna(x) else f"${x:,.0f}"


def _wide(fn, obj, **kw):
    try:
        return fn(obj, width="stretch", **kw)
    except TypeError:
        return fn(obj, use_container_width=True, **kw)


def _colcfg(columns):
    """Hover definitions on column headers, from the glossary. Silent where
    the Streamlit build has no column_config."""
    cc = getattr(st, "column_config", None)
    if cc is None:
        return None
    out = {}
    for c in columns:
        t = tip(c)
        if t:
            out[c] = cc.Column(help=t)
    return out or None


def _table(df, shading, fmt, reverse=()):
    cfg = _colcfg(df.columns)
    styled = heat_table(df, shading=shading, fmt=fmt, reverse=reverse)
    try:
        return st.dataframe(styled, width="stretch", hide_index=True,
                            column_config=cfg)
    except TypeError:
        return st.dataframe(styled, use_container_width=True, hide_index=True,
                            column_config=cfg)


def _rate(n, d):
    d = pd.to_numeric(d, errors="coerce").replace(0, np.nan)
    return pd.to_numeric(n, errors="coerce") / d


def render_event_tracker(q, H, table_exists=None, howto=None):
    if table_exists and not table_exists("dash_event_tracker"):
        st.info("Event tracker has not been published yet. Map rosters in "
                "`event_audience_map.csv`, then run `publish.py`.")
        return

    df = q("SELECT * FROM dash_event_tracker")
    if df.empty:
        st.info("No mapped events with a roster yet.")
        return
    meta = q("SELECT * FROM dash_event_tracker_meta")
    lapse = int(meta.iloc[0].lapse_days) if not meta.empty else 90
    df["event_date"] = pd.to_datetime(df["event_date"])
    df["bucket"] = pd.Categorical(df.bucket, BUCKET_ORDER, ordered=True)

    st.divider()
    H("Event performance & cost - by signup roster", "signups")
    if howto:
        howto("event_tracker")
    st.caption(
        f"**Phase one: keyed on signups, attendance left out.** Signups are "
        f"the Alpine IQ roster for the event. There is no reliable check-in "
        f"capture, so no figure below is per attendee and there is no "
        f"showed / didn't-show split. Buckets are by purchase history "
        f"before the event: **New to TTA** never bought before; **Active** "
        f"bought within the prior {lapse} days; **Lapsed** bought before "
        f"but not in the prior {lapse} days. Windows count from the event "
        f"date and are cumulative.")

    n_ev = df.airtable_record_id.nunique()
    n_cost = df[df.cost_recorded].airtable_record_id.nunique()
    n_mat = df[df.mature_d90].airtable_record_id.nunique()
    st.caption(f"{n_ev} events with a mapped roster - {n_cost} with recorded "
               f"cost, {n_mat} old enough for a 90-day read.")

    # ---- all mature events, by bucket --------------------------------------
    mat = df[df.mature_d90]
    if not mat.empty:
        g = (mat.groupby("bucket", observed=True)
                .agg(signups=("signups", "sum"),
                     buyers_d0=("buyers_d0", "sum"),
                     revenue_d0=("revenue_d0", "sum"),
                     buyers_d30=("buyers_d30", "sum"),
                     revenue_d30=("revenue_d30", "sum"),
                     buyers_d90=("buyers_d90", "sum"),
                     revenue_d90=("revenue_d90", "sum"))
                .reset_index())
        st.markdown(f"**All events 90+ days old, by bucket** "
                    f"({mat.airtable_record_id.nunique()} events)")
        _table(pd.DataFrame({
            "Bucket": g.bucket.map(BUCKET_LABEL),
            "Signups": g.signups,
            "Bought day-of": g.buyers_d0,
            "% of signups (day-of)": _rate(g.buyers_d0, g.signups),
            "Bought by +30d": g.buyers_d30,
            "% of signups (+30d)": _rate(g.buyers_d30, g.signups),
            "Bought by +90d": g.buyers_d90,
            "% of signups (+90d)": _rate(g.buyers_d90, g.signups),
            "Revenue by +90d": g.revenue_d90,
        }), shading={"% of signups (+90d)": "blue", "Revenue by +90d": "green"},
            fmt={"Signups": "{:,.0f}", "Bought day-of": "{:,.0f}",
                 "Bought by +30d": "{:,.0f}", "Bought by +90d": "{:,.0f}",
                 "% of signups (day-of)": "{:.1%}",
                 "% of signups (+30d)": "{:.1%}",
                 "% of signups (+90d)": "{:.1%}",
                 "Revenue by +90d": "${:,.0f}"})
        st.caption(
            "A New-to-TTA signup who buys is a customer the event created. "
            "Active signups would mostly have bought anyway; their revenue "
            "is shown for completeness, not credited to the event.")

    # ---- per event -----------------------------------------------------------
    st.markdown("**By event**")
    piv = (df.pivot_table(index=["airtable_record_id", "event_name",
                                 "event_date", "event_type", "cost_recorded",
                                 "net_tta_cost", "mature_d30", "mature_d90"],
                          columns="bucket", values=["signups", "buyers_d30",
                                                    "buyers_d90",
                                                    "revenue_d90"],
                          aggfunc="sum", observed=True)
             .fillna(0).reset_index())
    piv.columns = ["_".join(c).strip("_") if isinstance(c, tuple) else c
                   for c in piv.columns]
    for b in BUCKET_ORDER:
        for m in ("signups", "buyers_d30", "buyers_d90", "revenue_d90"):
            if f"{m}_{b}" not in piv.columns:
                piv[f"{m}_{b}"] = 0
    piv["signups_all"] = sum(piv[f"signups_{b}"] for b in BUCKET_ORDER)
    piv["revenue_all"] = sum(piv[f"revenue_d90_{b}"] for b in BUCKET_ORDER)
    piv = piv.sort_values("event_date", ascending=False)

    # A loyalty event exists for existing customers; judging it on
    # net-new would always read as a failure and answer the wrong question.
    loyal = piv.event_type.isin(LOYALTY_TYPES)
    piv["target"] = np.where(loyal, "active", "new")
    piv["target_signups"] = np.where(loyal, piv.signups_active, piv.signups_new)
    piv["target_d30"] = np.where(loyal, piv.buyers_d30_active, piv.buyers_d30_new)
    piv["target_d90"] = np.where(loyal, piv.buyers_d90_active, piv.buyers_d90_new)

    cost_ok = (piv.cost_recorded & piv.mature_d90
               & (piv.target_d90 >= MIN_NEW_FOR_COST))
    cpnn = np.where(cost_ok,
                    piv.net_tta_cost / piv.target_d90.replace(0, np.nan),
                    np.nan)
    cps = np.where(piv.cost_recorded & (piv.signups_all > 0),
                   piv.net_tta_cost / piv.signups_all.replace(0, np.nan), np.nan)

    def _why(r, ok):
        if ok:
            return ""
        if not r.cost_recorded:
            return "cost unrecorded"
        if not r.mature_d90:
            # Early signal: 30 days in, plenty of target signups, none bought.
            if (r.mature_d30 and r.target_signups >= EARLY_MIN_SIGNUPS
                    and r.target_d30 == 0):
                return "under 90d - no target buyers at +30d yet"
            return "under 90 days old"
        return f"fewer than {MIN_NEW_FOR_COST} {r.target} buyers"
    why = [_why(r, ok) for r, ok in zip(piv.itertuples(), cost_ok)]

    _table(pd.DataFrame({
        "Date": piv.event_date.dt.strftime("%Y-%m-%d"),
        "Event": piv.event_name.str.slice(0, 50),
        "Net cost": piv.net_tta_cost,
        "Signups": piv.signups_all,
        "New signups": piv.signups_new,
        "Active signups": piv.signups_active,
        "Lapsed signups": piv.signups_lapsed,
        "Judged on": piv.target.map(BUCKET_LABEL),
        "Target bought by +90d": piv.target_d90,
        "% of target signups": _rate(piv.target_d90, piv.target_signups),
        "$ / target customer (signups, 90d)": cpnn,
        "$ / signup": cps,
        "Revenue +90d (all signups)": piv.revenue_all,
        "Why blank": why,
    }), shading={"$ / target customer (signups, 90d)": "blue",
                 "% of target signups": "aqua"},
        fmt={"Net cost": "${:,.0f}", "Signups": "{:,.0f}",
             "New signups": "{:,.0f}", "Active signups": "{:,.0f}",
             "Lapsed signups": "{:,.0f}", "Target bought by +90d": "{:,.0f}",
             "% of target signups": "{:.1%}",
             "$ / target customer (signups, 90d)": "${:,.0f}",
             "$ / signup": "${:,.0f}",
             "Revenue +90d (all signups)": "${:,.0f}"},
        reverse=("$ / target customer (signups, 90d)",))
    st.caption(
        "**Judged on** - which bucket the event is for. Acquisition events "
        "are judged on New to TTA: net cost over new signups who bought "
        "within 90 days is the figure to compare them on. Loyalty events "
        "exist for existing customers and are judged on Active signups "
        "who bought; cost per net-new would answer the wrong question. "
        "**$ / signup** is a real number but a weaker one: it rewards a "
        "long list, not a good event. Events under 90 days old show "
        "signups only; a row flagged *no target buyers at +30d yet* has "
        "had a month and produced nothing from its target bucket - worth "
        "watching, not yet a verdict.")

    # ---- one event, full grid ------------------------------------------------
    names = (piv.assign(label=lambda x: x.event_date.dt.strftime("%Y-%m-%d")
                        + "  " + x.event_name.str.slice(0, 60))
                [["airtable_record_id", "label"]])
    pick = st.selectbox("Full breakdown for one event", names.label,
                        key="ev_tracker_pick")
    rid = names.loc[names.label == pick, "airtable_record_id"].iloc[0]
    one = df[df.airtable_record_id == rid].sort_values("bucket")
    r0 = one.iloc[0]
    st.caption(f"Net cost {_money(r0.net_tta_cost)}"
               + ("" if r0.cost_recorded else " (unrecorded)")
               + f" - windows mature: day-of {'yes' if r0.mature_d0 else 'no'}, "
                 f"+30d {'yes' if r0.mature_d30 else 'no'}, "
                 f"+90d {'yes' if r0.mature_d90 else 'no'}")
    _table(pd.DataFrame({
        "Bucket": one.bucket.map(BUCKET_LABEL),
        "Signups": one.signups,
        "Resolvable to POS": one.resolvable,
        "Bought day-of": one.buyers_d0,
        "Revenue day-of": one.revenue_d0,
        "Bought by +30d": one.buyers_d30,
        "Revenue by +30d": one.revenue_d30,
        "Bought by +90d": one.buyers_d90,
        "Revenue by +90d": one.revenue_d90,
        "% of signups (+90d)": _rate(one.buyers_d90, one.signups),
    }), shading={"% of signups (+90d)": "blue"},
        fmt={"Signups": "{:,.0f}", "Resolvable to POS": "{:,.0f}",
             "Bought day-of": "{:,.0f}", "Bought by +30d": "{:,.0f}",
             "Bought by +90d": "{:,.0f}", "Revenue day-of": "${:,.0f}",
             "Revenue by +30d": "${:,.0f}", "Revenue by +90d": "${:,.0f}",
             "% of signups (+90d)": "{:.1%}"})
    st.caption(
        "**Resolvable to POS** - signups who have ever had a TTA POS "
        "record. For New-to-TTA signups this is people who bought on or "
        "after the event; the rest have still never transacted, which is a "
        "result about the event, not a gap in the data. "
        "**Attendance is not shown** - the roster is who registered. A "
        "showed / didn't-show split arrives when check-in capture is "
        "reliable.")
