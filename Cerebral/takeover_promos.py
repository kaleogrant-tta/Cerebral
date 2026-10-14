"""
takeover_promos.py -- the promo view inside the Takeovers tab.

Two pieces, both wired from cerebral_public.py:

  render_current_promos   "Promos running now" -- a compact strip at the top of
                          the tab. One row per active promo (any program):
                          qualifying baskets, applied %, units, net, estimated
                          discount, days left, "vs run-up" (the clean days before
                          the window, from dash_promo_baseline.base_days / base_note)
                          with the dollar change beside the %, and a per-store mini bar.
                          Then one line of promos starting in the next 14 days
                          and a link into Promo Lab for the full picture.
  render_takeover_offers  "Offers inside this takeover" -- in the selected
                          takeover's detail: one card per promo attached to it
                          (promos.py "takeover" field), one stacked daily chart
                          of qualifying baskets per offer across the whole
                          takeover with the offer boundaries marked, a per-store
                          table and plain-English alerts.

Everything is read from the aggregate-only dash_promo_* tables that
publish_promo.py writes; the maths and the wording come from promo_lab_tab so
the two tabs never disagree about a number. "Qualifying" = the basket looks
like the offer; "applied" = the price, or the till discount on the basket, shows
the deal was given (an estimate -- see publish_promo.py). The store
filter (keys) applies everywhere. A published file that predates the promo
tables gets one st.info from the strip and nothing from the offers block.
"""
from __future__ import annotations

import datetime as dt
import html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import charts
from promo_lab_tab import (
    MECHANIC_LABEL, PROMO_TABLES,
    _alert, _base, _base_help, _card, _day, _days_word, _defs, _df, _int, _money, _note,
    _pct, summarise,
)

SOON_DAYS = 14          # "starting soon" horizon for the strip
PROMO_LAB_HREF = "?tab=promo-lab"


# ---------------------------------------------------------------- shared loads
def _have_tables(table_exists) -> bool:
    if table_exists is None:
        return True
    return all(table_exists(t) for t in PROMO_TABLES)


def _load(q, keys):
    """defs, day rows (selected stores), baselines (selected stores)."""
    defs = _defs(q)
    if defs.empty:
        return defs, pd.DataFrame(), pd.DataFrame()
    return defs, _day(q, keys), _base(q, keys)


def _esc(s) -> str:
    return html.escape(str(s))


def _applied_cell(s: dict) -> str:
    if s["applied"] is None:
        return "n/a · gift"
    return _pct(s["applied_pct"]) if s["qualifying"] else "—"


def _window(s: dict) -> str:
    return f"{s['start']:%b} {s['start'].day} – {s['end']:%b} {s['end'].day}"


def _vs_split(s: dict) -> tuple[str, str]:
    """(headline, small line) for the 'vs run-up' tile — the same cases
    promo_lab_tab._vs_text covers, split so the % reads big and the $ sits under it."""
    dollars = (f"{_money(s['vs_dollar'], signed=True)}/day in brand sales"
               if np.isfinite(s.get("vs_dollar", np.nan)) else "")
    if s["status"] == "upcoming":
        return "—", "not started"
    if not s["has_base"]:
        return "—", "no run-up days in the data"
    if not s["base_qual_pd"]:
        return "new", "offer did not exist before" + (f" · {dollars}" if dollars else "")
    if not np.isfinite(s["vs"]):
        return "—", dollars
    if s["vs"] > 3:
        return f"{s['base_qual_pd']:.1f} → {s['qual_pd']:.1f}/day", dollars
    return _pct(s["vs"], signed=True), dollars


def _store_bars(w: pd.DataFrame, keys, stores, accent: str) -> str:
    """Horizontal CSS bars: qualifying baskets per store for one promo."""
    by = w.groupby("store_key").qualifying_baskets.sum() if not w.empty else pd.Series(dtype=float)
    vals = [(k, float(by.get(k, 0.0))) for k in keys]
    top = max([v for _, v in vals] + [1.0])
    rows = []
    for k, v in vals:
        pct = max(2.0, v / top * 100) if v > 0 else 0.0
        rows.append(
            f'<div style="display:grid;grid-template-columns:5.6rem 1fr 2.6rem;align-items:center;'
            f'gap:.4rem;font-size:.78rem;line-height:1.5;">'
            f'<span style="color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
            f'{_esc(stores.get(k, k))}</span>'
            f'<span style="background:var(--rule);border-radius:3px;height:.55rem;overflow:hidden;">'
            f'<span style="display:block;width:{pct:.0f}%;height:100%;background:{accent};"></span></span>'
            f'<span style="color:var(--ink);text-align:right;font-variant-numeric:tabular-nums;">{_int(v)}</span>'
            f'</div>')
    return "".join(rows)


def _tile(label: str, value: str, sub: str | None = None, title: str | None = None) -> str:
    t = f' title="{html.escape(title, quote=True)}"' if title else ""
    return (
        f'<div style="min-width:0;"{t}>'
        f'<div style="font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);'
        f'line-height:1.25;white-space:nowrap;">{label}</div>'
        f'<div style="font-size:1.08rem;font-weight:700;color:var(--ink);line-height:1.2;'
        f'font-variant-numeric:tabular-nums;white-space:nowrap;">{value}</div>'
        + (f'<div style="font-size:.74rem;color:var(--body);line-height:1.3;">{sub}</div>' if sub else "")
        + "</div>")


def _strip_row(s: dict, w: pd.DataFrame, keys, stores, accent: str) -> str:
    """One active promo as a three-part row: who, the numbers, the stores."""
    takeover = s.get("takeover")
    tag = f"{_esc(s['brand'])} · {_esc(MECHANIC_LABEL.get(s['mechanic'], s['mechanic']))}"
    if takeover:
        tag += f" · part of the {_esc(takeover)} takeover"
    vs_head, vs_sub = _vs_split(s)
    tiles = "".join([
        _tile("Qualifying", _int(s["qualifying"]), f"{_int(s['qual_pd'])} a day" if s["days_in"] else None),
        _tile("Applied", _applied_cell(s), "of qualifying" if s["applied"] is not None else "tracked below"),
        _tile("Brand units", _int(s["units"])),
        _tile("Brand net", _money(s["net"])),
        _tile("Est. discount", _money(s["est"])),
        _tile(f"vs run-up ({s['base_days']}d)" if s.get("base_days") else "vs run-up",
              _esc(vs_head), _esc(vs_sub) if vs_sub else None, title=_base_help(s)),
    ])
    return (
        '<div class="tp-row" style="display:grid;grid-template-columns:minmax(12rem,1.3fr) minmax(0,4fr) '
        'minmax(11rem,1.4fr);gap:1.1rem;align-items:start;background:var(--tint);border:1px solid var(--rule);'
        f'border-left:4px solid {accent};border-radius:10px;padding:.75rem 1rem;margin:.3rem 0 .55rem 0;">'
        f'<div style="min-width:0;">'
        f'<div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);">'
        f'Running · {_esc(_days_word(s))}</div>'
        f'<div style="font-size:1.02rem;font-weight:700;color:var(--ink);line-height:1.25;margin:.1rem 0;">'
        f'{_esc(s["name"])}</div>'
        f'<div style="font-size:.78rem;color:var(--body);">{_window(s)}<br>{tag}</div>'
        f'</div>'
        f'<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) minmax(0,1.15fr) minmax(0,1.3fr) minmax(0,2fr);gap:.7rem;min-width:0;">{tiles}</div>'
        f'<div style="min-width:0;"><div style="font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;'
        f'color:var(--muted);margin-bottom:.15rem;">Qualifying by store</div>{_store_bars(w, keys, stores, accent)}</div>'
        '</div>')


_STRIP_CSS = """
<style>
@media (max-width: 1000px) {
  .tp-row { grid-template-columns: 1fr !important; }
  .tp-row > div:nth-child(2) { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }
}
</style>
"""


# ---------------------------------------------------------------- the strip
def render_current_promos(q, keys, stores, table_exists=None, accent="#2F6F4F",
                          warn="#B4632B", bad="#9E3B32", series=None) -> None:
    """Compact 'Promos running now' strip for the top of the Takeovers tab."""
    keys = [int(k) for k in (keys or list(stores))]
    st.markdown("##### Promos running now")
    if not _have_tables(table_exists):
        st.info("Promo tracking arrives with the next data refresh — the published file does not "
                "carry the promo tables (dash_promo_*) yet. Run publish_promo.py after publish.py.")
        return
    defs, d, b = _load(q, keys)
    if defs.empty:
        st.info("No promos in the calendar yet — add one to promos.py.")
        return
    last = defs.last_day.max()
    active = defs[defs.status == "active"].sort_values("start", ascending=False)
    soon = defs[(defs.status == "upcoming")
                & (defs.start <= last + dt.timedelta(days=SOON_DAYS))].sort_values("start")

    _note("Every offer in the calendar that is open at the selected stores, whichever programme it "
          "belongs to. <b>Qualifying</b> = the basket looks like the offer; <b>applied</b> = the price, "
          "or the till discount on the basket, shows the deal was given (an estimate); <b>vs run-up</b> = "
          "qualifying baskets a day against the clean days before the window — days under another offer "
          "on the same brand are left out (hover the tile for what was used) — with the brand's daily "
          f"sales change beside it. Data through "
          f"{last:%b} {last.day}. "
          f'<a href="{PROMO_LAB_HREF}" target="_self">Open Promo Lab</a> for the day-by-day view, '
          "goals and the designer.")
    st.markdown(_STRIP_CSS, unsafe_allow_html=True)

    if active.empty:
        st.caption("Nothing is running right now.")
    for _, defn in active.iterrows():
        pid = defn.promo_id
        s = summarise(pid, d, b, defn)
        s["takeover"] = defn.takeover if isinstance(defn.takeover, str) and defn.takeover else None
        w = d[(d.promo_id == pid) & d.in_window]
        st.markdown(_strip_row(s, w, keys, stores, accent), unsafe_allow_html=True)

    if not soon.empty:
        bits = []
        for _, defn in soon.iterrows():
            n = (defn.start - last).days
            when = "tomorrow" if n == 1 else f"in {n} days"
            bits.append(f"<b>{_esc(defn['name'])}</b> ({defn.start:%b} {defn.start.day}, {when})")
        _note("Starting soon: " + " · ".join(bits) + ".")
    elif active.empty:
        _note(f"Nothing in the calendar starts in the next {SOON_DAYS} days either.")


# ---------------------------------------------------------------- the offers
def _short(name: str, n: int = 28) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def _stacked_chart(offers: pd.DataFrame, d: pd.DataFrame, tk: dict, last: dt.date,
                   series: list[str], key: str) -> None:
    """Qualifying baskets per day, one stacked trace per offer, across the takeover."""
    tk_start, tk_end = pd.Timestamp(tk["start"]).date(), pd.Timestamp(tk["end"]).date()
    eff_end = min(tk_end, last)
    fig = go.Figure()
    drew = False
    for i, (_, o) in enumerate(offers.iterrows()):
        w = d[(d.promo_id == o.promo_id) & d.in_window]
        rows = w.groupby("day", as_index=False).agg(qualifying=("qualifying_baskets", "sum"),
                                                     store_baskets=("store_baskets", "sum"))
        rows = rows[rows.store_baskets > 0]
        colour = series[i % len(series)]
        if not rows.empty:
            drew = True
            fig.add_trace(go.Bar(
                x=pd.to_datetime(rows.day), y=rows.qualifying, name=_short(o["name"]),
                marker_color=colour,
                hovertemplate="%{x|%a %b %e}<br>%{y:,} qualifying<extra>" + _short(o["name"], 40) + "</extra>"))
        lo, hi = max(o.start, tk_start), min(o.end, tk_end)
        if lo <= hi:
            fig.add_vrect(x0=pd.Timestamp(lo) - pd.Timedelta(hours=12), x1=pd.Timestamp(hi) + pd.Timedelta(hours=12),
                          fillcolor=charts._rgba(colour, 0.07), line_width=0)
            if lo > tk_start:
                fig.add_vline(x=pd.Timestamp(lo) - pd.Timedelta(hours=12),
                              line=dict(color="#8a8a8a", dash="dot", width=1))
            fig.add_annotation(x=pd.Timestamp(lo) + (pd.Timestamp(hi) - pd.Timestamp(lo)) / 2, y=1.02,
                               yref="paper", text=f"<b>{_esc(_short(o['name'], 34))}</b>", showarrow=False,
                               font=dict(size=11, color=colour), xanchor="center", yanchor="bottom")
    if not drew:
        st.caption("No qualifying baskets to chart for these offers at the selected stores yet.")
        return
    fig.update_layout(barmode="stack", xaxis_title="", yaxis_title="Qualifying baskets per day",
                      legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="left", x=0,
                                  traceorder="normal"),
                      margin=dict(l=0, r=0, t=34, b=0))
    fig.update_xaxes(range=[pd.Timestamp(tk_start) - pd.Timedelta(hours=18),
                            pd.Timestamp(eff_end) + pd.Timedelta(hours=18)])
    charts.show(fig, key=key, kind="bar", margin=dict(l=0, r=0, t=34, b=0))


def _store_table(offers: pd.DataFrame, d: pd.DataFrame, keys, stores) -> pd.DataFrame:
    rows = []
    for _, o in offers.iterrows():
        w = d[(d.promo_id == o.promo_id) & d.in_window]
        g = w.groupby("store_key").agg(
            qualifying=("qualifying_baskets", "sum"), applied=("applied_baskets", "sum"),
            units=("matched_units", "sum"), net=("matched_net", "sum"), est=("est_discount", "sum"),
            nearmiss=("nearmiss_baskets", "sum"))
        for k in keys:
            r = g.loc[k] if k in g.index else None
            qual = float(r.qualifying) if r is not None else 0.0
            rows.append({
                "Offer": o["name"],
                "Store": stores.get(k, str(k)),
                "Qualifying": qual,
                "Applied %": (float(r.applied) / qual * 100) if (r is not None and qual and o.mechanic != "gwp"
                                                                 and pd.notna(r.applied)) else np.nan,
                "Brand units": float(r.units) if r is not None else 0.0,
                "Brand net $": round(float(r.net)) if r is not None else 0.0,
                "Est. discount $": round(float(r.est)) if r is not None else 0.0,
                "Near misses": float(r.nearmiss) if r is not None else 0.0,
            })
    return pd.DataFrame(rows)


def _offer_alerts(summaries: list[dict]) -> list[tuple[str, str]]:
    out = []
    for s in summaries:
        if s["status"] == "upcoming":
            continue
        if s["applied"] is not None and s["qualifying"] >= 10 and s["applied_pct"] < 0.7:
            verb = "show" if s["status"] == "active" else "showed"
            tail = (" Either the offer is not being rung every time, or these baskets bought the brand "
                    "without asking for it — worth a check at the till." if s["status"] == "active"
                    else " Worth a word with the team before the next offer on this brand.")
            out.append((f"<b>{_esc(s['name'])}</b>: only {_pct(s['applied_pct'])} of the "
                        f"{_int(s['qualifying'])} qualifying baskets {verb} the deal in the price or the "
                        f"till discount.{tail}", "a-bad"))
        if s["status"] == "active" and s["days_in"] >= 3 and s["last3_days"] >= 3 and s["last3"] == 0:
            out.append((f"<b>{_esc(s['name'])}</b>: no qualifying baskets in the last 3 days of a running "
                        "offer. Either it has stopped landing or the brand pattern in promos.py is wrong — "
                        "check both.", "a-bad"))
    return out


def render_takeover_offers(q, keys, stores, tk: dict, table_exists=None, accent="#2F6F4F",
                           warn="#B4632B", bad="#9E3B32", series=None) -> None:
    """'Offers inside this takeover' for the selected takeover's detail view."""
    if not _have_tables(table_exists):
        return                                  # the strip above already said so
    keys = [int(k) for k in (keys or list(stores))]
    series = series or [accent, warn, "#3B6E8F", "#8A5A9E", "#A8913C", "#6E7F8D"]
    defs, d, b = _load(q, keys)
    if defs.empty:
        return
    offers = defs[defs.takeover.fillna("") == tk["name"]].sort_values("start")
    st.markdown("##### Offers inside this takeover")
    if offers.empty:
        st.caption("No basket-detected offers are attached to this takeover in promos.py.")
        return
    last = defs.last_day.max()
    n = len(offers)
    _note(f"{n} offer{'s' if n != 1 else ''} ran inside the {_esc(tk['name'])} window, read from the baskets "
          "(there are no promo codes in the sales data). <b>Qualifying</b> = the basket looks like the "
          "offer; <b>applied</b> = the price, or the till discount on the basket, shows the deal was "
          "given (an estimate). \"vs the N clean days before\" compares qualifying baskets a day with the "
          "run-up to each offer; days under another offer on the same brand are left out, so when the "
          "offers follow each other the comparison reaches back to before the first one (hover the label "
          "on a card for what was used).")

    summaries = []
    cols = st.columns(min(n, 3))
    for i, (_, defn) in enumerate(offers.iterrows()):
        s = summarise(defn.promo_id, d, b, defn)
        summaries.append(s)
        with cols[i % len(cols)]:
            st.markdown(_card(s, accent, warn), unsafe_allow_html=True)

    for html_, tone in _offer_alerts(summaries):
        _alert(html_, tone)

    st.markdown("**Qualifying baskets a day, offer by offer**")
    _stacked_chart(offers, d, tk, last, series, key=f"tko_stack_{tk['name']}")
    _note("One colour per offer, stacked, across the whole takeover; the dotted lines are where one "
          "offer hands over to the next. Days before the takeover are not shown. Gift offers count "
          "baskets holding the brand — the free item itself is tracked in the GWP section below.")

    st.markdown("**Store by store**")
    tbl = _store_table(offers, d, keys, stores)
    _df(tbl, {
        "Qualifying": st.column_config.NumberColumn(format="localized"),
        "Applied %": st.column_config.NumberColumn(format="%d%%"),
        "Brand units": st.column_config.NumberColumn(format="localized"),
        "Brand net $": st.column_config.NumberColumn(format="dollar"),
        "Est. discount $": st.column_config.NumberColumn(format="dollar"),
        "Near misses": st.column_config.NumberColumn(
            format="%d", help="Baskets one step short of the offer (one unit short, or under the spend bar)."),
    })
    _note("Applied % is blank for gift-with-purchase offers. "
          f'<a href="{PROMO_LAB_HREF}" target="_self">Promo Lab</a> has each offer day by day, who bought '
          "it and what else was in the basket.")
