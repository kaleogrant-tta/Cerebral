"""
audiences_tab.py -- Audiences x Events tab for Cerebral.

Reads only the published dash_audience_* tables, so it works identically
against the local database and the deployed cerebral_dash.duckdb.

Entry point matches the other tabs in cerebral_public.py:

    from audiences_tab import render_audiences
    render_audiences(q=q, keys=keys, stores=STORES,
                     heading=heading, table_exists=table_exists)

`q` runs a query and returns an empty frame when the table is absent, so this
tab degrades gracefully if the code deploys before the data file carries the
dash_audience_* tables.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# Cerebral palette
CREAM = "#F5F0E6"
BROWN = "#4A3728"
BROWN_MID = "#7A5C43"
TEAL = "#1F7A6F"
CLAY = "#B5654A"
MUTED = "#9A8C7A"

MONO = "'IBM Plex Mono', ui-monospace, monospace"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def _load(q):
    cohorts = q("SELECT * FROM dash_audience_cohorts ORDER BY event_date")
    campaigns = q("SELECT * FROM dash_audience_campaigns ORDER BY campaign")
    meta_df = q("SELECT * FROM dash_audience_meta")
    returns = q("SELECT * FROM dash_audience_returns")
    meta = meta_df.iloc[0] if not meta_df.empty else None
    return cohorts, campaigns, meta, returns


# --------------------------------------------------------------------------
# pieces
# --------------------------------------------------------------------------
def _stat(label: str, value: str, sub: str = "", accent: str = BROWN) -> str:
    return f"""
    <div style="padding:14px 18px;background:{CREAM};border-left:3px solid {accent};
                border-radius:2px;height:100%;">
      <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;
                  color:{MUTED};margin-bottom:6px;">{label}</div>
      <div style="font-family:{MONO};font-size:27px;font-weight:600;color:{accent};
                  line-height:1.1;">{value}</div>
      <div style="font-size:12px;color:{BROWN_MID};margin-top:4px;">{sub}</div>
    </div>"""


def _headline(meta) -> None:
    pct = meta["pct_never_transacted"]
    st.markdown(
        f"""
        <div style="background:{BROWN};color:{CREAM};padding:22px 26px;border-radius:3px;
                    margin-bottom:20px;">
          <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;
                      opacity:.7;margin-bottom:10px;">The headline</div>
          <div style="font-size:23px;line-height:1.4;font-weight:500;">
            <span style="font-family:{MONO};color:#E8C07A;">{pct:.0f}%</span>
            of the people who sign up at our activations have never bought
            anything at The Travel Agency.
          </div>
          <div style="font-size:14px;opacity:.75;margin-top:10px;line-height:1.5;">
            That holds for in-store activations, not just off-site festivals. It is a
            measurement, not a data gap: Alpine only issues a POS id once a POS record
            exists for that person.
          </div>
        </div>""",
        unsafe_allow_html=True,
    )


def _kpis(meta) -> None:
    cols = st.columns(4)
    items = [
        ("Attendees measured", f"{int(meta['attendees']):,}",
         f"{int(meta['events_mapped'])} events", BROWN),
        ("Have ever transacted", f"{int(meta['attendees_with_pos_record']):,}",
         f"{100 - meta['pct_never_transacted']:.0f}% of attendees", TEAL),
        ("New customers acquired", f"{int(meta['new_customers']):,}",
         "first purchase on or after the event", TEAL),
        ("Revenue, 90 days", f"${meta['revenue_90d']:,.0f}",
         "attendees only, post-event", BROWN),
    ]
    for col, (lab, val, sub, acc) in zip(cols, items):
        with col:
            st.markdown(_stat(lab, val, sub, acc), unsafe_allow_html=True)


def _event_table(cohorts: pd.DataFrame) -> None:
    df = cohorts.copy()
    df["Transacted"] = (df["pos_record_rate"] * 100).round(0).astype(int).astype(str) + "%"
    for c in ("new_revenue_90d", "existing_revenue_90d"):
        df[c] = df[c].map(lambda v: f"${v:,.0f}")
    show = df[[
        "event_date", "event_name", "store", "roster", "Transacted",
        "new_customers", "new_returned_90d", "new_revenue_90d",
        "existing_customers", "existing_returned_90d", "existing_revenue_90d",
    ]].rename(columns={
        "event_date": "Date", "event_name": "Event", "store": "Store",
        "roster": "Attendees",
        "new_customers": "New",
        "new_returned_90d": "New returned 90d",
        "new_revenue_90d": "New rev 90d",
        "existing_customers": "Existing",
        "existing_returned_90d": "Existing returned 90d",
        "existing_revenue_90d": "Existing rev 90d",
    })
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.caption(
        "**New** = first-ever purchase on or after the event day. **Existing** = had "
        "already bought from us before it. Attendees who have never transacted appear "
        "in neither column, which is why New + Existing is well below Attendees. "
        "The 90-day figures cover the window after the event only."
    )

    n_new = int(cohorts["new_customers"].sum())
    n_ex = int(cohorts["existing_customers"].sum())
    r_new = float(cohorts["new_revenue_90d"].sum())
    r_ex = float(cohorts["existing_revenue_90d"].sum())
    ret_new = int(cohorts["new_returned_90d"].sum())
    ret_ex = int(cohorts["existing_returned_90d"].sum())

    st.markdown(f"""
    <div style="display:flex;gap:14px;margin-top:14px;">
      <div style="flex:1;padding:14px 18px;background:{CREAM};border-left:3px solid {TEAL};">
        <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;
                    color:{MUTED};margin-bottom:6px;">New customers</div>
        <div style="font-family:{MONO};font-size:25px;font-weight:600;color:{TEAL};">
          {n_new:,}</div>
        <div style="font-size:12px;color:{BROWN_MID};margin-top:4px;">
          {ret_new:,} returned within 90 days &middot; ${r_new:,.0f}</div>
      </div>
      <div style="flex:1;padding:14px 18px;background:{CREAM};border-left:3px solid {BROWN};">
        <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;
                    color:{MUTED};margin-bottom:6px;">Existing customers</div>
        <div style="font-family:{MONO};font-size:25px;font-weight:600;color:{BROWN};">
          {n_ex:,}</div>
        <div style="font-size:12px;color:{BROWN_MID};margin-top:4px;">
          {ret_ex:,} returned within 90 days &middot; ${r_ex:,.0f}</div>
      </div>
    </div>""", unsafe_allow_html=True)

    if r_new + r_ex:
        share = 100 * r_ex / (r_new + r_ex)
        st.caption(
            f"Existing customers account for {share:.0f}% of the 90-day revenue that "
            "follows an activation. Acquisition and revenue are not the same story."
        )


def _returns_chart(returns: pd.DataFrame, cohorts: pd.DataFrame) -> None:
    if returns.empty:
        st.caption("No return data published.")
        return
    names = (
        cohorts.sort_values("roster", ascending=False)["event_name"].head(8).tolist()
    )
    pick = st.multiselect(
        "Events", options=cohorts["event_name"].tolist(), default=names[:4],
        key="aud_returns_pick",
    )
    if not pick:
        st.caption("Choose at least one event.")
        return
    sub = returns[returns["event_name"].isin(pick)]
    wide = sub.pivot_table(
        index="day_offset", columns="event_name", values="cum_returners", aggfunc="max"
    )
    st.line_chart(wide, height=300)
    st.caption(
        "Cumulative distinct attendees who transacted, by days since the event. "
        "A curve that flattens early means the event produced a spike, not a habit."
    )


def _campaigns(campaigns: pd.DataFrame) -> None:
    costed = campaigns[campaigns["net_cost_to_tta"].notna()].copy()
    uncosted = campaigns[campaigns["net_cost_to_tta"].isna()]

    if len(costed):
        costed["Cost"] = costed["net_cost_to_tta"].map(lambda v: f"${v:,.0f}")
        costed["Rev 90d"] = costed["revenue_90d"].map(lambda v: f"${v:,.0f}")
        costed["$ / new customer"] = costed["cost_per_new_customer"].map(
            lambda v: f"${v:,.0f}" if pd.notna(v) else "--")
        costed["Revenue per $"] = costed["revenue_per_dollar"].map(
            lambda v: f"{v:.2f}" if pd.notna(v) else "--")
        st.dataframe(
            costed[["campaign", "events", "attendees", "new_customers",
                    "Cost", "Rev 90d", "$ / new customer", "Revenue per $"]]
            .rename(columns={"campaign": "Campaign", "events": "Events",
                             "attendees": "Attendees", "new_customers": "New"}),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info(
            "No campaign costs entered yet. Fill `cost_year_to_use` on the Campaigns "
            "tab of audience_event_mapping.xlsx, save in Excel, and re-publish."
        )

    if len(uncosted):
        st.caption(
            "Awaiting cost from marketing: "
            + ", ".join(uncosted["campaign"].astype(str).tolist())
        )


def _caveats(meta, cohorts: pd.DataFrame) -> None:
    with st.expander("How to read this"):
        st.markdown(f"""
**What an attendee is.** Someone who gave us their details at an event and landed in
an Alpine audience. That is not the same as everyone who showed up -- it is the subset
engaged enough to sign up, which biases these figures *upward* against the general
population.

**Event dates come from the events calendar, not Alpine.** Alpine's audience-created
timestamp is when the list was uploaded and lags the event by 1 to 47 days in this data.
Where a cohort has enough purchasers, the `Date check` column independently confirms the
date from the purchase spike.

**"New" means first-ever purchase on or after the event day.** Not new to loyalty --
most of these audiences enrol everyone at signup, so the loyalty flag is useless here.

**{int(meta['audiences_total']) - int(meta['events_mapped'])} audiences are not shown.**
Some could not be tied to a calendar event; one is excluded because its membership is
defined by having purchased, which would make same-day conversion true by construction.

**Comparisons are within-cohort, not against a control group.** These numbers describe
what attendees did. They do not prove the event caused it. A proper control needs
non-attendees matched on first-purchase week and prior spend.

**Costs are per campaign, not per event.** Several activations share one budget line,
so cost per acquired customer is computed at campaign level. Every budget figure
supplied excludes creative and brand ambassadors, so all costs are understated.
        """)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def render_audiences(q, keys=None, stores=None, heading=None,
                     table_exists=None, **kwargs) -> None:
    """Render the Audiences x Events tab.

    Signature mirrors render_events / render_loyalty. `keys` and `stores` are
    accepted for consistency but not used: these cohorts are defined by event
    attendance, not by the store/date window the rest of the dashboard filters
    on. Narrowing a 13-person dinner roster to a 13-week window would leave
    nothing to show.
    """
    if heading:
        heading("Audiences &times; Events",
                "What activations do for customer acquisition")
    else:
        st.markdown(
            f"<h2 style='color:{BROWN};margin-bottom:2px;'>Audiences &times; Events</h2>"
            f"<div style='color:{MUTED};font-size:13px;margin-bottom:18px;'>"
            f"What activations do for customer acquisition</div>",
            unsafe_allow_html=True,
        )

    if table_exists and not table_exists("dash_audience_cohorts"):
        st.info("The published data file predates this tab. It will populate "
                "after the next refresh rebuilds it.")
        return

    cohorts, campaigns, meta, returns = _load(q)
    if cohorts.empty or meta is None:
        st.info("The published data file predates this tab. It will populate "
                "after the next refresh rebuilds it.")
        return

    _headline(meta)
    _kpis(meta)

    st.caption(
        f"{meta['date_min']} to {meta['date_max']} \u00b7 "
        f"{int(meta['events_mapped'])} of {int(meta['audiences_total'])} audiences mapped \u00b7 "
        f"{int(meta['campaigns_costed'])} campaigns costed"
    )

    st.divider()
    tab1, tab2, tab3 = st.tabs(["By event", "Return behaviour", "Cost per customer"])
    with tab1:
        _event_table(cohorts)
    with tab2:
        _returns_chart(returns, cohorts)
    with tab3:
        _campaigns(campaigns)

    _caveats(meta, cohorts)
