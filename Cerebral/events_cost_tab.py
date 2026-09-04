"""
events_cost_tab.py -- "What events cost" section for the Events tab.

    from events_cost_tab import render_events_cost
    ...
    # inside render_events(), after the individual-events table:
    try:
        render_events_cost(q, H, table_exists)
    except Exception as exc:
        st.caption(f"Cost section unavailable: {exc}")

Reads dash_events_cost (built by publish_events.py) and, for the roster
method, dash_event_return. Renders only; every number is in the published
file.

TWO WAYS TO GET A COST PER NEW CUSTOMER, DELIBERATELY KEPT APART
----------------------------------------------------------------
LIFT method -- single-store events only. Incremental new customers is what
the difference-in-differences model attributes to the event day at the
hosting store. Per-event figures are shown only above a minimum evidence
floor (MIN_INCREMENTAL, MIN_CONTROLS); below it the row shows a dash.
Pooled by series or type is the figure to quote: summing cost and
incremental customers across a series cancels most of the per-night noise.

ROSTER method -- any event with an Alpine IQ roster mapped in
event_audience_map.csv. Net cost divided by attendees whose FIRST purchase
was on or after the event. This is the only defensible figure for off-site
events: the chain-wide lift for an off-site event is day-to-day variance in
first-time buyers (a few hundred a day) attributed to whatever was on the
calendar, and a four-seat dinner cannot bring in a hundred new customers.
Off-site rows therefore never show a lift-based figure.

COST CONVENTIONS (marketing's)
------------------------------
* net_tta_cost = direct + allocated share of shared budget lines - brand
  offset. Shared lines are split evenly across the events they cover.
* UNRECORDED means no budget line is linked. It is not free. Those events
  are counted and shown, never divided.
* Airtable's own "Total Cost for Event" rollup is not used anywhere: it
  charges every shared line in full to every event it touches.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from heat import heat_table
from glossary import tip

MIN_INCREMENTAL = 5     # lift-based $/new customer needs this many
MIN_CONTROLS = 5        # ...and this many baseline days
MIN_POOL_EVENTS = 3     # a series/type pool needs this many costed events
MIN_ROSTER_NEW = 5      # roster-based $/new customer needs this many
LOYALTY_TYPES = {"Loyalty"}   # judged on retention; no $/net-new here

SCOPE_SINGLE = "On-site (single store)"


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


def _roster_new(q, table_exists):
    """airtable_record_id -> new-customer buyers from the roster method."""
    if table_exists and not table_exists("dash_event_return"):
        return pd.DataFrame(columns=["airtable_record_id", "roster_new",
                                     "roster_members", "roster_mature"])
    r = q("SELECT * FROM dash_event_return")
    if r.empty:
        return pd.DataFrame(columns=["airtable_record_id", "roster_new",
                                     "roster_members", "roster_mature"])
    r["airtable_record_id"] = r.event_id.astype(str).str.rsplit("-", n=1).str[0]
    new = r[r.segment == "New"]
    out = (r.groupby("airtable_record_id")
             .agg(roster_members=("roster_members", "max"),
                  roster_mature=("mature", "min"))
             .reset_index())
    n = (new.groupby("airtable_record_id")["buyers"].sum()
            .rename("roster_new").reset_index())
    return out.merge(n, on="airtable_record_id", how="left").fillna(
        {"roster_new": 0})


def _pool(df, key, label):
    """Pooled cost per incremental new customer for controlled events."""
    g = df[(df.scope == SCOPE_SINGLE) & df.cost_recorded & df.measured
           & ~df.event_type.isin(LOYALTY_TYPES)]
    if g.empty:
        return pd.DataFrame()
    p = (g.groupby(key)
           .agg(events=("airtable_record_id", "nunique"),
                net_cost=("net_tta_cost", "sum"),
                incremental=("incremental_new", "sum"))
           .reset_index())
    p = p[p.events >= MIN_POOL_EVENTS].copy()
    p["cpnc"] = np.where(p.incremental > 0,
                         p.net_cost / p.incremental.replace(0, np.nan), np.nan)
    return p.rename(columns={key: label}).sort_values("cpnc")


def _pool_table(p, label):
    if p.empty:
        return
    _table(pd.DataFrame({
        label: p[label],
        "Events": p.events,
        "Net cost": p.net_cost,
        "Incremental new": p.incremental,
        "$ / net-new customer (lift)": p.cpnc,
    }), shading={"$ / net-new customer (lift)": "blue"},
        fmt={"Events": "{:,.0f}", "Net cost": "${:,.0f}",
             "Incremental new": "{:,.0f}",
             "$ / net-new customer (lift)": "${:,.0f}"},
        reverse=("$ / net-new customer (lift)",))


def render_events_cost(q, H, table_exists=None, howto=None):
    if table_exists and not table_exists("dash_events_cost"):
        st.info("Cost tables have not been published yet. Run "
                "`events_ingest.py` against the marketing cost export, "
                "then `publish.py`.")
        return

    d = q("SELECT * FROM dash_events_cost")
    if d.empty:
        return
    d["event_date"] = pd.to_datetime(d["event_date"])
    d = d.merge(_roster_new(q, table_exists), on="airtable_record_id",
                how="left")

    st.divider()
    H("What events cost", "net cost")
    if howto:
        howto("events_cost")

    rec = d[d.cost_recorded]
    unrec = d[~d.cost_recorded]
    ctrl = rec[(rec.scope == SCOPE_SINGLE) & rec.measured
               & ~rec.event_type.isin(LOYALTY_TYPES)
               & (rec.incremental_new >= MIN_INCREMENTAL)
               & (rec.min_controls >= MIN_CONTROLS)]
    pooled = (ctrl.net_tta_cost.sum() / ctrl.incremental_new.sum()
              if ctrl.incremental_new.sum() > 0 else np.nan)

    c = st.columns(4)
    c[0].metric("Net cost, recorded events", _money(rec.net_tta_cost.sum()),
                f"{len(rec)} events",
                help="Marketing's corrected figure: direct cost plus an even "
                     "share of any shared budget line, minus brand "
                     "contributions. Airtable's own rollup is not used - it "
                     "charges every shared line in full to every event it "
                     "touches and overstates by roughly half.")
    c[1].metric("Cost unrecorded", f"{len(unrec)}", "events",
                help=tip("unrecorded"))
    c[2].metric("Pooled $ / net-new customer (lift)", _money(pooled),
                f"{len(ctrl)} controlled events",
                help="Total net cost over total incremental new customers, "
                     "single-store events with enough evidence only. The "
                     "one number to quote if you quote one.")
    c[3].metric("Median $ / net-new customer (lift)",
                _money(ctrl.cost_per_new_customer.median()),
                "same events",
                help="Half of controlled events did better than this.")

    st.caption(
        "**$ / new customer** here is net cost divided by the new customers "
        "the lift model attributes to the event day - people whose first "
        "ever purchase was that day, over what the same weekday would have "
        "brought in anyway, after subtracting the other stores. It is only "
        "computed for single-store events, where that subtraction is "
        "possible. Off-site events get a roster-based figure further down, "
        "or nothing.")

    # ---- pooled ----------------------------------------------------------
    st.markdown("**By series** - the figures to quote")
    st.caption(
        f"Summing across a series cancels most of the night-to-night noise "
        f"that makes single events swing. Needs {MIN_POOL_EVENTS}+ costed, "
        f"measurable events at a single store.")
    _pool_table(_pool(d, "series", "Series"), "Series")

    st.markdown("**By event type**")
    _pool_table(_pool(d, "event_type", "Type"), "Type")

    # ---- per event ---------------------------------------------------------
    st.markdown("**By event**")
    st.caption(
        f"Lift-based $ / new customer shows only where the event had at "
        f"least {MIN_INCREMENTAL} incremental new customers and "
        f"{MIN_CONTROLS} baseline days; below that a per-event dollar figure "
        f"is more luck than measurement. **Signups** is the Alpine IQ "
        f"roster - people who signed up, not people who walked in; there is "
        f"no reliable check-in data yet, so no rate here is per attendee. "
        f"**$ / net-new (signups)** divides cost by signups whose first TTA "
        f"purchase came on or after the event - the only usable figure for "
        f"off-site events, and only where a roster is mapped.")

    show_unrec = st.checkbox("Include events with unrecorded cost",
                             value=False, key="ev_cost_unrec")
    only_series = st.multiselect(
        "Series", sorted(d.series.dropna().unique()), default=[],
        key="ev_cost_series")

    t = d if show_unrec else rec
    if only_series:
        t = t[t.series.isin(only_series)]
    t = t.sort_values("event_date", ascending=False)

    loyal = t.event_type.isin(LOYALTY_TYPES)
    lift_ok = ((t.scope == SCOPE_SINGLE) & t.cost_recorded & t.measured
               & ~loyal
               & (t.incremental_new >= MIN_INCREMENTAL)
               & (t.min_controls >= MIN_CONTROLS)
               & (t.incremental_new > 0))
    lift_cpnc = np.where(lift_ok, t.net_tta_cost / t.incremental_new, np.nan)

    roster_ok = (t.cost_recorded & t.roster_new.notna() & ~loyal
                 & (t.roster_new >= MIN_ROSTER_NEW)
                 & t.roster_mature.fillna(False).astype(bool))
    roster_cpnc = np.where(roster_ok, t.net_tta_cost / t.roster_new, np.nan)
    signup_ok = t.cost_recorded & t.roster_members.notna() & (t.roster_members > 0)
    cost_per_signup = np.where(signup_ok,
                               t.net_tta_cost / t.roster_members.replace(0, np.nan),
                               np.nan)

    def _note(r, lo, ro):
        if not r.cost_recorded:
            return "unrecorded"
        if r.event_type in LOYALTY_TYPES:
            return "loyalty event - judged on Active bucket in tracker"
        if r.scope != SCOPE_SINGLE and not ro:
            if pd.notna(r.roster_members):
                return "roster too small / immature"
            return "off-site, no roster"
        if r.scope == SCOPE_SINGLE and not lo and not ro:
            if not r.measured:
                return "not measurable"
            if r.incremental_new < MIN_INCREMENTAL:
                return "too few incremental"
            return "too few controls"
        return ""

    notes = [_note(r, lo, ro) for r, lo, ro
             in zip(t.itertuples(), lift_ok, roster_ok)]


    _table(pd.DataFrame({
        "Date": t.event_date.dt.strftime("%Y-%m-%d"),
        "Event": t.event_name.str.slice(0, 55),
        "Scope": t.scope.str.replace("On-site ", ""),
        "Net cost": t.net_tta_cost,
        "Incremental new": np.where(t.scope == SCOPE_SINGLE,
                                    t.incremental_new, np.nan),
        "$ / net-new (lift)": lift_cpnc,
        "Signups": t.roster_members,
        "$ / signup": cost_per_signup,
        "Net-new of signups": t.roster_new,
        "$ / net-new (signups)": roster_cpnc,
        "Why blank": notes,
    }), shading={"$ / net-new (lift)": "blue", "$ / net-new (signups)": "aqua"},
        fmt={"Net cost": "${:,.0f}", "Incremental new": "{:,.0f}",
             "$ / net-new (lift)": "${:,.0f}", "Signups": "{:,.0f}",
             "$ / signup": "${:,.0f}", "Net-new of signups": "{:,.0f}",
             "$ / net-new (signups)": "${:,.0f}"},
        reverse=("$ / net-new (lift)", "$ / net-new (signups)"))

    with st.expander("How cost is worked out"):
        st.markdown(f"""
**Net cost** comes from marketing's corrected export, not from Airtable's
"Total Cost for Event" rollup. The rollup charges every shared budget line
- an agency retainer, a print order, a partnership fee - in full to every
event it touches, so a retainer attached to three events reads as three
retainers. Across the calendar that was about $477K of phantom cost. The
corrected figure splits each shared line evenly across the events it
covers, adds the event's own direct costs, and subtracts anything a brand
partner reimbursed.

**Even split is a convention, not a fact.** A monthly retainer split
across four dinners charges each dinner the same whether it was a
four-seat tasting or a fifty-person launch. Where that is wrong, the
allocation is corrected in the export, not here.

**Unrecorded is not free.** {len(unrec)} completed events have no budget
line linked. They are listed so the gap is visible and never divided into
a per-customer figure.

**Every rate names its denominator in the header.** Per signup and per
net-new customer are real numbers today; per attendee is not, because
check-in capture is unreliable (one event: 160+ RSVPs, 23 checked in).

**Two denominators.** The lift figure asks how many more first-time buyers
the hosting store saw that day than it should have, after subtracting the
other stores. It needs a single hosting store and enough comparable days;
one-off nights with a couple of incremental customers are suppressed. The
roster figure asks, of the people on the event's sign-up or ticket list,
how many made their first TTA purchase after it. It needs a roster mapped
in `event_audience_map.csv` and 90 days of data after the event. They
answer different questions and are not averaged together.

**No return on same-day sales, anywhere.** Same-day sales lift on-site
is +1.7% with a range that crosses zero, so it is not a claim that holds.
Net-new customers is: +7.4%, range +3.0% to +11.9%, with a clean spike
on the day and the day after and nothing before. Every dollar figure on
this tab is cost per net-new customer for that reason.

**Off-site events never get the lift figure.** Chain-wide first-time
buyers move by a hundred or more day to day. Attributing that swing to
whichever off-site dinner was on the calendar produced "$1.47 per new
customer" for a four-seat dinner. The roster is the only honest read.
        """)
