"""
events_tab.py -- the Events tab for cerebral_public.py.

    from events_tab import render_events
    ...
    with t_events:
        render_events(q=q, keys=keys, stores=STORES,
                      heading=heading, table_exists=table_exists)

Reads only dash_events_*. All statistics are computed in publish_events.py;
this file renders them.

FILTERS
-------
The store filter applies, on the store hosting the event. The weeks slider
does NOT: each event's baseline is drawn from a 42-day window around it, so
slicing to the last N weeks would leave most events without a baseline.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from heat import show_heat

METRIC_LABEL = {"net": "Net sales", "baskets": "Baskets",
                "new_customers": "New customers"}
SCOPE_SINGLE = "On-site (single store)"
SCOPE_CHAIN = "On-site (chain-wide)"
SCOPE_OFF = "Off-site"

STORE_COLORS = {"DTBK": "#F73400", "5th Avenue": "#0e7c66",
                "Soho": "#d9a514", "Union Square": "#7b4fd8",
                "Chain": "#5C3E34"}


def _pct(x):
    return "-" if pd.isna(x) else f"{x * 100:+.1f}%"


def _wide(fn, obj, **kw):
    try:
        return fn(obj, width="stretch", **kw)
    except TypeError:
        return fn(obj, use_container_width=True, **kw)


def _ci_text(r):
    if pd.isna(r.ci_lo):
        return "--"
    s = f"[{r.ci_lo * 100:+.1f}%, {r.ci_hi * 100:+.1f}%]"
    if not r.reliable:
        s += "  n small"
    elif r.excludes_zero:
        s += "  ✓"
    return s


def _summary_table(df, title, note=None):
    if df.empty:
        return
    st.caption(title)
    d = df.sort_values("n", ascending=False)
    show_heat(st, pd.DataFrame({
        "Group": d.group_value,
        "Events": d.n,
        "Mean": d["mean"],
        "Median": d["median"],
        "95% interval": [_ci_text(r) for _, r in d.iterrows()],
    }), shading={"Median": "blue"},
        fmt={"Events": "{:,.0f}", "Mean": "{:+.1%}", "Median": "{:+.1%}"})
    if note:
        st.caption(note)


def render_events(q, keys, stores, heading=None, table_exists=None,
                  howto=None):
    H = heading or (lambda t, term=None: st.markdown(f"##### {t}"))

    if table_exists and not table_exists("dash_events_summary"):
        st.info("Event tables have not been published yet. Run "
                "`events_ingest.py`, then `publish.py`.")
        return

    if howto:
        howto("events")

    meta = q("SELECT * FROM dash_events_meta")
    if not meta.empty:
        m = meta.iloc[0]
        st.caption(
            f"{int(m.events):,} events inside {m.cov_start} to {m.cov_end}. "
            f"{int(m.single_store):,} ran at a single store and carry a "
            f"control group; {int(m.offsite):,} were off-site. Baselines use "
            f"the same weekday within ±{int(m.control_window_days)} days, "
            f"minimum {int(m.min_controls)} control days.")

    metric = st.radio("Measure", list(METRIC_LABEL),
                      format_func=lambda k: METRIC_LABEL[k],
                      horizontal=True, key="ev_metric")

    summ = q(f"SELECT * FROM dash_events_summary WHERE metric = '{metric}'")
    detail = q(f"SELECT * FROM dash_events_detail WHERE metric = '{metric}'")

    st.caption({
        "net": "**Net sales** — revenue. Answers whether an event made money "
               "that day.",
        "baskets": "**Baskets** — number of transactions, ignoring size. "
                   "Answers whether more people bought, which is a cleaner "
                   "footfall signal than revenue because one large purchase "
                   "cannot skew it.",
        "new_customers": "**New customers** — people whose first ever "
                         "purchase was that day. This is what most events "
                         "are actually for: an event that brings in "
                         "first-timers has done its job even if same-day "
                         "revenue barely moves, because those customers "
                         "return later. Counted only for customers whose "
                         "identity is confirmed, so the true figure is "
                         "somewhat higher.",
    }.get(metric, ""))
    if summ.empty:
        st.info("Nothing measurable for this metric.")
        return
    detail["event_date"] = pd.to_datetime(detail["event_date"])

    all_stores = not keys or len(keys) == len(stores)
    picked_names = {stores[k] for k in keys} if not all_stores else None

    # ---- headline ------------------------------------------------------
    hi = summ[(summ.scope == SCOPE_SINGLE) & (summ.group_kind == "all")]
    off = summ[(summ.scope == SCOPE_OFF) & (summ.group_kind == "all")]
    c1, c2 = st.columns(2)
    if not hi.empty:
        r = hi.iloc[0]
        c1.metric("On-site, single store", _pct(r["median"]),
                  "median lift vs control stores",
                  help="How much better the hosting store did than its own "
                       "normal for that weekday, AFTER subtracting whatever "
                       "the other three stores did that day. Subtracting the "
                       "others removes weather, holidays and citywide events. "
                       "A typical event moved sales by this much.")
        c1.caption(f"{int(r.n)} events · {_ci_text(r)}")
    if not off.empty:
        r = off.iloc[0]
        c2.metric("Off-site", _pct(r["median"]), "median chain-wide lift",
                  help="Off-site events are not tied to one store, so there "
                       "is nothing to compare against. This is just whether "
                       "chain sales beat their weekday normal — which could "
                       "equally be the weather or the season. Weaker "
                       "evidence: treat it as a ceiling, not a measurement.")
        c2.caption(f"{int(r.n)} events · {_ci_text(r)}")

    with st.expander("How to read this tab", expanded=False):
        st.markdown("""
### The question

Did an event make people shop more than they otherwise would have?

The hard part is *otherwise*. Sales rise on Fridays, in December, in good
weather. If an event ran on a busy Friday, sales were high — but they would
have been anyway. So every number here is a **comparison against what that
day should have looked like**, never a raw sales figure.

### How "should have looked like" is worked out

For each event we find the **same weekday** within six weeks either side —
if the event was a Thursday, we collect nearby Thursdays. We skip any that
sit next to another event. Those are the **control days**, and their average
is the baseline.

> Soho event on a Thursday. Nearby clean Thursdays averaged $18,000. Soho
> did $21,700 that day. That's **+20.7%** — the *own lift*.

### Why that alone isn't enough

Suppose it rained all day. Every store would be down, and the event would
look like a failure that wasn't its fault. Or it was the first warm Thursday
of spring and every store was up — the event would take credit it didn't
earn.

So we check what the **other three stores** did against *their* own
baselines that same day. If they were down 2.9%, something citywide was
dragging on everyone.

> Own lift **+20.7%**, other stores **−2.9%** → effect **+23.6%**

That subtraction is the **effect** column. It's the part that can't be
explained by anything affecting the whole city — weather, holidays, a
transit strike, a news cycle. That's why single-store events are the
strongest evidence here.

### Why off-site events are weaker

An off-site event — a film festival, a gallery night — has no "other
stores" to compare against, because it isn't tied to one shop. All we can
measure is whether chain sales rose, and we can't separate that from
whatever else was happening in New York. **Treat off-site numbers as an
upper bound**, not a measured effect.

### Reading the numbers

**Median, not mean.** One extraordinary day can drag an average a long way.
If mean is +5.7% and median +1.7%, the typical event did about +1.7% and a
couple of outliers pulled the rest. Quote the median.

**The range in brackets** is how confident we are. `[+2.3%, +9.4%]` means
the true effect is very likely somewhere in there. **If the range includes
zero, we have not detected an effect** — the data can't tell a real change
from normal day-to-day noise.

**"n small"** means fewer than ten events. With that few, the range gets
narrow by luck rather than evidence — on test data with nothing real to
find, a five-event group still produced a range that excluded zero. Read
small-n rows as a description of what happened, not proof of a pattern.

### The one thing this cannot rule out

If events get scheduled onto days already expected to be strong — a product
drop, a holiday weekend, a partner promotion — the effect will look bigger
than it is. Nothing in the data distinguishes "the event caused a good day"
from "a good day was chosen for the event."
        """)

    st.divider()

    # ---- on-site -------------------------------------------------------
    H("On-site events, single store")
    st.caption(
        "Events held at one store, where the other three act as a control "
        "group. The figure is how much better the hosting store did than its "
        "own weekday normal, minus whatever the other stores did that day. "
        "Anything affecting the whole city cancels out, which makes this the "
        "most trustworthy section on the tab. A range that excludes zero "
        "means a real effect; a range containing zero means none was "
        "detected.")

    s1 = summ[summ.scope == SCOPE_SINGLE]
    _summary_table(s1[s1.group_kind == "store"], "By store")
    _summary_table(s1[s1.group_kind == "type"], "By event type")
    ser = s1[s1.group_kind == "series"]
    if not ser.empty:
        _summary_table(ser, "By series")

    off_curve = s1[s1.group_kind == "offset"].copy()
    if not off_curve.empty:
        off_curve["off"] = off_curve.group_value.astype(int)
        off_curve = off_curve.sort_values("off")
        fig = px.bar(off_curve, x="off", y="median",
                     error_y=off_curve.ci_hi - off_curve["median"],
                     error_y_minus=off_curve["median"] - off_curve.ci_lo,
                     labels={"median": "Median lift", "off": "Days from event"})
        fig.update_traces(marker_color="#00FFD4")
        fig.update_layout(height=300, margin=dict(t=10, b=10, l=0, r=0),
                          paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(tickformat=".0%")
        st.caption(
            "**Why this chart matters most.** A real effect should appear on "
            "the day and not before it. An event on Thursday cannot raise "
            "Tuesday's sales — so if the bars are high on every offset, the "
            "events are being scheduled into already-busy periods and the "
            "headline number is inflated. A clear peak at 0 with flat "
            "shoulders either side is the signature of a genuine effect. Day "
            "+1 staying somewhat raised is normal: people who discovered you "
            "at an event come back.")
        _wide(st.plotly_chart, fig, key="ev_offset")

    st.divider()

    # ---- off-site ------------------------------------------------------
    H("Off-site events")
    s2 = summ[summ.scope == SCOPE_OFF]
    oc = s2[s2.group_kind == "offset"].copy()
    if not oc.empty:
        oc["off"] = oc.group_value.astype(int)
        oc = oc.sort_values("off")
        flat = (oc["median"] > 0).all()
        if flat:
            st.warning(
                "**Read this before quoting the off-site numbers.** The lift "
                "is positive at every offset from −2 to +2 days. An event "
                "cannot raise sales two days before it happens, so this is a "
                "plateau rather than a spike — off-site events are scheduled "
                "into already-busy weeks (festival season, holidays, the "
                "cultural calendar). Treat the figures below as an upper "
                "bound that is mostly scheduling, not effect.")
        _summary_table(oc.assign(group_value="day " + oc.group_value),
                       "By day offset")

    _summary_table(s2[s2.group_kind == "type"], "By event type")
    _summary_table(s2[s2.group_kind == "series"], "By series")

    st.divider()

    # ---- chain-wide ----------------------------------------------------
    s3 = summ[summ.scope == SCOPE_CHAIN]
    if not s3.empty:
        with st.expander("Chain-wide on-site events (no control group)"):
            st.caption(
                "Events running at every store at once. With no store left "
                "out there is nothing to compare against, so these use the "
                "weaker within-store measure and are not comparable with the "
                "difference-in-differences figures above.")
            _summary_table(s3[s3.group_kind == "store"], "By store")
            _summary_table(s3[s3.group_kind == "type"], "By event type")

    # ---- individual events ---------------------------------------------
    H("Individual events")
    d0 = detail[detail.offset == 0].copy()
    d0["effect"] = np.where(d0.has_control, d0.did, d0.own_lift)
    d0 = d0.dropna(subset=["effect"])
    if d0.empty:
        st.info("No individual events to show for this selection.")
        return

    if picked_names:
        d0 = d0[d0.store_name.isin(picked_names | {"Chain"})]
        st.caption(
            "Filtered to the selected stores, on the store hosting the "
            "event. The summary tables above are chain-level and are not "
            "affected by the store filter.")
    scope_pick = st.multiselect(
        "Scope", [SCOPE_SINGLE, SCOPE_CHAIN, SCOPE_OFF],
        default=[SCOPE_SINGLE], key="ev_scope",
        help="Single-store events are the only ones with a control group, so "
             "they are the only ones where the effect is properly isolated. "
             "Chain-wide and off-site events show an uncontrolled lift, which "
             "still includes weather, season and everything else that day.")
    d0 = d0[d0.scope.isin(scope_pick)] if scope_pick else d0
    if d0.empty:
        st.info("Nothing in the selected scope.")
        return

    d0 = d0.sort_values("effect", ascending=False)

    # Only single-store events have a control group. For everything else
    # "Other stores" is empty and "Effect" is identical to "Own lift", so
    # showing all three columns invites exactly the question "why is Other
    # stores always None?". Show the columns that carry information.
    any_controlled = bool(d0.has_control.any())
    all_controlled = bool(d0.has_control.all())

    cols = {
        "Date": d0.event_date.dt.strftime("%Y-%m-%d"),
        "Event": d0.event_name.str.slice(0, 58),
        "Store": d0.store_name,
        "Type": d0.event_type,
    }
    fmt = {"Controls": "{:,.0f}"}

    if not any_controlled:
        # No control group anywhere in this selection -- one honest column.
        cols["Lift vs normal"] = d0.own_lift
        fmt["Lift vs normal"] = "{:+.1%}"
        shading = {"Lift vs normal": "blue"}
    else:
        cols["Effect"] = d0.effect
        cols["Own lift"] = d0.own_lift
        cols["Other stores"] = d0.control_drift
        fmt.update({"Effect": "{:+.1%}", "Own lift": "{:+.1%}",
                    "Other stores": "{:+.1%}"})
        shading = {"Effect": "blue"}
    cols["Controls"] = d0.n_controls

    show_heat(st, pd.DataFrame(cols), shading=shading, fmt=fmt)

    if not any_controlled:
        st.caption(
            "**Lift vs normal** — how chain sales compared with the same "
            "weekday nearby. These events are not held at a single store, so "
            "there is no control group and nothing can be subtracted: the "
            "figure includes whatever else was happening in the city that "
            "day. Read it as an upper bound.  \n"
            "**Controls** — how many comparable days built the baseline. "
            "Three is the minimum and one odd day can swing it; eight or more "
            "is solid.")
    else:
        st.caption(
            "**Own lift** — how the hosting store did against its own normal "
            "for that weekday.  \n"
            "**Other stores** — how the non-hosting stores did against "
            "theirs, the same day. This is the citywide background: weather, "
            "holidays, everything that had nothing to do with the event. "
            "*Blank where there is no control group* — off-site events are "
            "not held at a store, and chain-wide events occupy all four, so "
            "in both cases there is nothing left to compare against and "
            "Effect simply equals Own lift.  \n"
            "**Effect** — own lift minus other stores. What survives once "
            "the background is removed, and the number to judge an event by.  "
            "\n"
            "**Controls** — how many comparable days built the baseline. "
            "Three is the minimum and one odd day can swing it; eight or "
            "more is solid.")
        if not all_controlled:
            st.info(
                "This selection mixes single-store events (which have a "
                "control group) with off-site or chain-wide ones (which do "
                "not). Rows with a blank **Other stores** are the second "
                "kind — their Effect is an uncontrolled lift and is not "
                "comparable with the rest. Filter Scope to one kind at a "
                "time to compare like with like.")

    # Attendee-level 90-day return. Wrapped so a failure here degrades to
    # a caption instead of taking down the whole Events tab.
    try:
        render_event_return(q, keys, stores, H, table_exists)
    except Exception as exc:
        st.caption(f"Attendee return section unavailable: {exc}")

    with st.expander("Method, in detail"):
        st.markdown("""
**Picking the baseline.** For every event day we take the same weekday
within 42 days either side, drop any that fall within one day of another
event at that store (or of an off-site event, which touches everywhere),
and average what remains. At least three such days are required or the
event is left out entirely.

That buffer matters more than it sounds. An earlier version excluded days
near *any* event anywhere, which knocked out 71% of the calendar and left a
median of two comparison days per event — a baseline of two Tuesdays. Fixing
it to block per store took the median to six.

**Why weekday matching.** Friday and Saturday run far ahead of Monday, so
comparing an event Friday against "an average day" would credit the event
with the weekend. Staying inside 42 days also holds the season and the
underlying sales trend roughly constant.

**The confidence range** comes from resampling the events thousands of times
and seeing how much the average moves. A wide range means the events
disagree with each other; a narrow one means they tell a consistent story.
It is not a guarantee — one time in twenty the truth sits outside it.

**Why some rows say n small.** Ranges built on fewer than ten events are
unreliable in a specific way: they can look convincing by accident. On test
data containing no real effect at all, a five-event group produced a range
that excluded zero. Anything marked *n small* is there to describe what
happened, not to support a decision.

**Timing is day-level only.** The event times in the calendar are
data-entry artefacts — sequential minutes typed while filling the sheet —
so there is no way to look at the hours around an event.

**Scheduling bias is the real limit.** Difference-in-differences removes
anything hitting all four stores. It cannot tell whether a day was good
because of the event, or whether the event was placed on a day already
expected to be good. If you want to settle that, the only clean answer is
to schedule a few events at random and compare.

**Filters.** The weeks slider does not apply — each event needs its own
42-day window to build a baseline, so shortening the view would break the
comparison. The store filter does apply, on the hosting store.
        """)


def render_event_return(q, keys, stores, H, table_exists=None):
    """90-day return by attendee, new versus regular.

    Deliberately leads with how little of the roster can be measured. The
    return rate is computed on attendees who bought at all, and most did
    not -- reporting the rate without that context would overstate what
    events achieve by a wide margin.
    """
    if table_exists and not table_exists("dash_event_return"):
        st.info(
            "Attendee return analysis has not been published yet. Fill in "
            "`event_audience_map.csv`, then run `publish.py`.")
        return

    df = q("SELECT * FROM dash_event_return")
    if df.empty:
        st.info("No mapped events with measurable attendees yet.")
        return
    meta = q("SELECT * FROM dash_event_return_meta")
    win = int(meta.iloc[0].window_days) if not meta.empty else 90
    min_n = int(meta.iloc[0].min_measurable) if not meta.empty else 10

    st.divider()
    H(f"Did attendees come back? ({win}-day return)")

    all_stores = not keys or len(keys) == len(stores)
    if not all_stores:
        df = df[df.store_key.isin(list(keys)) | (df.store_key == 0)]
        if df.empty:
            st.info("No mapped events at the selected stores.")
            return

    mature = df[df.mature]
    immature = df[~df.mature]

    # --- the funnel, before any rate ------------------------------------
    per_ev = (df.groupby("event_id")
                .agg(roster=("roster_members", "max"),
                     matched=("pos_matched", "max"))
                .reset_index())
    roster_tot = int(per_ev.roster.sum())
    matched_tot = int(per_ev.matched.sum())
    buyers_tot = int(mature.buyers.sum())
    ret_tot = int(mature.returned.sum())

    st.markdown(
        f'<p class="note">Most attendees cannot be followed into '
        f'transactions. A roster member with no POS record has never bought '
        f'anything at TTA — that is a result, not a data gap. The return '
        f'rate below is computed only on those who did buy, so read it '
        f'alongside the funnel, never on its own.</p>',
        unsafe_allow_html=True)

    f = st.columns(4)
    f[0].metric("On the rosters", f"{roster_tot:,}",
                help="Everyone captured across all mapped events.")
    f[1].metric("Have a POS record", f"{matched_tot:,}",
                f"{matched_tot / max(roster_tot, 1) * 100:.0f}% of roster",
                help="Have ever transacted at TTA and can be resolved to a "
                     "customer identity.")
    f[2].metric("Bought after the event", f"{buyers_tot:,}",
                f"{buyers_tot / max(roster_tot, 1) * 100:.0f}% of roster",
                help="Made at least one purchase on or after the event "
                     "date. This is the denominator of the return rate.")
    f[3].metric(f"Returned within {win}d", f"{ret_tot:,}",
                f"{ret_tot / max(buyers_tot, 1) * 100:.0f}% of buyers",
                help="Bought again within the window, measured from their "
                     "first purchase on or after the event.")

    if not immature.empty:
        n_imm = immature.event_id.nunique()
        st.caption(
            f"{n_imm} event{'s' if n_imm > 1 else ''} held within the last "
            f"{win} days {'are' if n_imm > 1 else 'is'} excluded — there has "
            f"not been time for a return to happen yet.")

    if mature.empty:
        st.info("No mapped event is old enough to measure yet.")
        return

    # --- new versus regular ----------------------------------------------
    seg = (mature.groupby("segment")
                 .agg(buyers=("buyers", "sum"), returned=("returned", "sum"))
                 .reindex(["New", "Regular"]).fillna(0).reset_index())
    seg["rate"] = seg.returned / seg.buyers.replace(0, np.nan) * 100

    st.markdown("**New attendees against regulars**")
    st.caption(
        "**New** — the attendee's first ever purchase was on or after the "
        "event, so they met TTA there. **Regular** — they were already a "
        "customer and happened to attend. Both are measured from their "
        f"first purchase on or after the event, so neither gets a head "
        f"start on the {win}-day window.")

    sc = st.columns(2)
    for i, row in seg.iterrows():
        if pd.isna(row.rate):
            continue
        sc[i].metric(f"{row.segment} attendees",
                     f"{row.rate:.0f}% returned",
                     f"{int(row.returned):,} of {int(row.buyers):,}")

    if seg.rate.notna().all() and len(seg) == 2:
        gap = seg.loc[seg.segment == "New", "rate"].iloc[0] - \
              seg.loc[seg.segment == "Regular", "rate"].iloc[0]
        st.markdown(
            f'<p class="note">First-timers return at '
            f'{abs(gap):.0f}pp {"above" if gap > 0 else "below"} the rate of '
            f'existing customers who attended. A gap below zero is the '
            f'normal case and not a failure — established customers return '
            f'more readily than anyone meeting the brand for the first '
            f'time. What matters is the size of the gap and whether it '
            f'moves.</p>', unsafe_allow_html=True)

    # --- per event ---------------------------------------------------------
    piv = (mature.pivot_table(index=["event_id", "event_name", "event_date",
                                     "roster_members", "pos_matched"],
                              columns="segment",
                              values=["buyers", "returned"],
                              aggfunc="sum")
                 .fillna(0).reset_index())
    piv.columns = ["_".join(c).strip("_") if isinstance(c, tuple) else c
                   for c in piv.columns]
    for s in ("New", "Regular"):
        for m in ("buyers", "returned"):
            if f"{m}_{s}" not in piv.columns:
                piv[f"{m}_{s}"] = 0
    piv["measurable"] = piv.buyers_New + piv.buyers_Regular
    piv["rate_all"] = ((piv.returned_New + piv.returned_Regular)
                       / piv.measurable.replace(0, np.nan) * 100)
    piv["rate_new"] = (piv.returned_New
                       / piv.buyers_New.replace(0, np.nan) * 100)
    piv = piv.sort_values("event_date", ascending=False)

    # A rate on three people is not a rate. Suppressing it is more useful
    # than printing a number nobody should act on.
    def _fmt(rate, n):
        if pd.isna(rate) or n < min_n:
            return "—"
        return f"{rate:.0f}%"

    st.markdown("**By event**")
    _wide(st.dataframe, pd.DataFrame({
        "Event": piv.event_name,
        "Date": pd.to_datetime(piv.event_date).dt.strftime("%b %d, %Y"),
        "Roster": piv.roster_members.astype(int),
        "Bought": piv.measurable.astype(int),
        "New": piv.buyers_New.astype(int),
        "Returned": (piv.returned_New + piv.returned_Regular).astype(int),
        f"{win}d return": [_fmt(r, n) for r, n
                           in zip(piv.rate_all, piv.measurable)],
        "New return": [_fmt(r, n) for r, n
                       in zip(piv.rate_new, piv.buyers_New)],
    }), hide_index=True)
    st.caption(
        f"A dash means fewer than {min_n} measurable attendees — too few for "
        f"a rate to mean anything. Those events still count towards the "
        f"totals above.")
