"""
promo_lab_tab.py -- the Promo Lab tab: what our promos are doing, and what to try next.

Reads the aggregate-only dash_promo_* tables that publish_promo.py writes
(see dev/PROMO_LAB_SPEC.md) plus the promo calendar in promos.py. There are
no discount names or promo codes in the sales data, so every figure here is
inferred from what was in the basket:

    qualifying = the basket looks like the offer (right brand, enough units)
    applied    = the prices in the basket, or the till discount on it, show the
                 deal was actually given (an estimate: line prices in the export
                 are period averages, see publish_promo.py)
    near miss  = one step short (one gummy short, a few dollars under the bar)

The "before" comparison is a CLEAN run-up: the days before the window with
any day under another offer on the same brand left out, grown further back
when that leaves too few days. dash_promo_baseline.base_days says how many
days were used and base_note says what was done; every caption here reads
those two columns rather than assuming 28 days.

Eight sub-tabs, in this order:

  1. Live board     What is running now. One card per promo (active first,
                    then upcoming within 30 days, then the last two finished),
                    a daily chart of qualifying baskets against the clean
                    run-up rate, a store leaderboard with goals from
                    config/promo_goals.json (editable for the session, with a
                    JSON download), and plain-English alerts.
  2. Anatomy        One promo in depth: who bought it (new / returning /
                    unknown, loyalty tiers), when (weekday x hour heat strip),
                    which channel, what else was in the basket (attach index),
                    price and volume against baseline with the break-even
                    volume lift, and basket size vs everyone else.
  3. Designer       What-if for a new offer: pick a mechanic, brand, category
                    and depth; see how many baskets already qualify (pure
                    cost), how many sit one step short (the stretch), the
                    weekly cost at current behaviour and the extra baskets
                    needed to break even. Threshold offers get the gross-basket
                    histogram with the bar marked. "Copy definition" prints
                    the promos.py block.
  4. History        Timeline of every promo coloured by program with takeover
                    windows behind, a results table for finished promos, and a
                    side-by-side compare of two promos per window day.
  5. Win-back model The original Promo Lab (churn map, store opportunities,
                    brand promos, brand discount ROI) -- passed in as `legacy`.
  6. Channel & Day  channel_promo.render_channel_promo, unchanged.
  7. Promo Calendar promo_calendar.render_promo_calendar, unchanged.
  8. Ideas          Rule-based suggestions from the published tables (no AI
                    calls): near-miss stretches, weekday troughs, lapsed
                    categories worth a win-back, attach-driven bundles, promos
                    worth repeating, new-customer magnets -- each with the
                    evidence in one sentence, an offer, stores, a rough weekly
                    cost and a confidence word. Ends with a "Request ideas"
                    brief Kaleo can paste into an AI chat or send to marketing.

Conventions: every table goes through st.column_config; charts go through
charts.show with a unique key; the store filter (`keys`) applies everywhere;
a published file that predates the promo tables gets one st.info and a clean
return. Nothing here reads customer identifiers -- the tables carry none.
"""
from __future__ import annotations

import datetime as dt
import html
import json

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import charts
import promos
import promo_calendar
from channel_promo import render_channel_promo
from promo_calendar import render_promo_calendar

try:
    from glossary import tip
except Exception:  # pragma: no cover
    def tip(term):
        return ""

DESIGNER_WEEKS = 13
DOW_SHORT = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
SEGMENT_LABEL = {"new": "New to TTA", "returning": "Returning", "unknown": "Unknown (no account)"}
MECHANIC_LABEL = {
    "bundle_price": "Bundle price (N for $X)",
    "threshold_addon": "Spend $X, get an add-on for $Y",
    "bogo": "Buy N, get M free",
    "multi_pct_off": "N items, X% off",
    "spotlight": "Spotlight prices",
    "gwp": "Gift with purchase",
    "ghost_sku": "Free item (ghost SKU)",
}
PROMO_TABLES = ["dash_promo_def", "dash_promo_day", "dash_promo_baseline"]


# ---------------------------------------------------------------- formatting
def _money(x, signed=False) -> str:
    if x is None or (isinstance(x, (float, np.floating)) and not np.isfinite(x)):
        return "—"
    s = f"${abs(float(x)):,.0f}"
    if x < 0:
        return "-" + s
    return ("+" + s) if signed and x > 0 else s


def _pct(x, places=0, signed=False) -> str:
    if x is None or (isinstance(x, (float, np.floating)) and not np.isfinite(x)):
        return "—"
    return f"{'+' if signed and x > 0 else ''}{x * 100:.{places}f}%"


def _int(x) -> str:
    if x is None or (isinstance(x, (float, np.floating)) and not np.isfinite(x)):
        return "—"
    return f"{int(round(x)):,}"


def _note(html: str) -> None:
    st.markdown(f'<p class="note">{html}</p>', unsafe_allow_html=True)


def _alert(html: str, tone: str = "a-warn") -> None:
    st.markdown(f'<div class="alert {tone}">{html}</div>', unsafe_allow_html=True)


def _fmt_cell(v, fmt: str) -> str:
    """Apply a NumberColumn format by hand (used only when a column holds blanks)."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or pd.isna(v):
        return "—"
    v = float(v)
    if fmt == "percent":
        return f"{v:.2%}"
    if fmt == "dollar":
        return f"${v:,.2f}"
    if fmt == "localized":
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    try:
        return (fmt % int(round(v))) if "%d" in fmt else (fmt % v)
    except (TypeError, ValueError):
        return f"{v:,.2f}"


def _df(frame: pd.DataFrame, cfg: dict | None = None, **kw) -> None:
    """st.dataframe with one fix: Streamlit prints a missing number as the word
    "None", which reads like a value. Any number column that has a blank is
    pre-formatted here so the blank shows as an em dash instead."""
    kw.setdefault("hide_index", True)
    cfg = dict(cfg or {})
    frame = frame.copy()
    for col, cc in list(cfg.items()):
        if col not in frame.columns or not isinstance(cc, dict):
            continue
        tc = cc.get("type_config") or {}
        if tc.get("type") != "number" or not frame[col].isna().any():
            continue
        fmt = tc.get("format") or "localized"
        frame[col] = frame[col].map(lambda v, f=fmt: _fmt_cell(v, f))
        new = st.column_config.TextColumn(help=cc.get("help"), width=cc.get("width"))
        if isinstance(new, dict) and "alignment" in new:
            new["alignment"] = "right"
        cfg[col] = new
    try:
        st.dataframe(frame, width="stretch", column_config=cfg, **kw)
    except TypeError:                      # older Streamlit
        st.dataframe(frame, use_container_width=True, column_config=cfg, **kw)


def _d(v) -> dt.date:
    return promos._d(v)


def _store_names(stores: dict, keys: list[int]) -> str:
    if not keys or len(keys) == len(stores):
        return "all stores"
    return ", ".join(stores.get(k, str(k)) for k in keys)


def _in(keys: list[int]) -> str:
    return "(" + ",".join(str(int(k)) for k in keys) + ")"


def _confidence(n: float) -> str:
    """Plain word for how much evidence sits behind a number."""
    if n >= 200:
        return "solid"
    if n >= 50:
        return "fair"
    return "thin"


# ---------------------------------------------------------------- data loads
def _defs(q) -> pd.DataFrame:
    df = q("SELECT * FROM dash_promo_def")
    if df.empty:
        return df
    df["start"] = pd.to_datetime(df["start"]).dt.date
    df["end"] = pd.to_datetime(df["end"]).dt.date
    df["last_day"] = pd.to_datetime(df["last_day"]).dt.date
    params = []
    for r in df.itertuples():
        p = promos.promo_by_id(r.promo_id)
        if p is not None:
            params.append(p.get("params") or {})
        else:
            try:
                params.append(json.loads(r.params_json or "{}"))
            except Exception:
                params.append({})
    df["params"] = params
    return df


DAY_COLS = ["promo_id", "store_key", "day", "in_window", "store_baskets", "store_net",
            "qualifying_baskets", "applied_baskets", "matched_units", "matched_net", "matched_gm",
            "qual_basket_net", "qual_basket_gross", "est_discount", "new_customers", "known_customers",
            "nearmiss_baskets"]
BASE_COLS = ["promo_id", "store_key", "base_days", "base_qualifying_pd", "base_matched_units_pd",
             "base_matched_net_pd", "base_store_baskets_pd", "base_unit_price", "base_gm_pct",
             "base_qual_share", "base_note"]


def _with_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """A frame that always has the contract columns, so a store filter that matches
    nothing (or a query that errored into an empty frame) still renders quietly."""
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    return df


def _day(q, keys) -> pd.DataFrame:
    df = _with_cols(q(f"SELECT * FROM dash_promo_day WHERE store_key IN {_in(keys)}"), DAY_COLS)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["in_window"] = df["in_window"].astype(bool)
    for c in ("qualifying_baskets", "applied_baskets", "store_baskets", "new_customers",
              "known_customers", "nearmiss_baskets"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _base(q, keys) -> pd.DataFrame:
    return _with_cols(q(f"SELECT * FROM dash_promo_baseline WHERE store_key IN {_in(keys)}"), BASE_COLS)


def _tbl(q, table, keys, table_exists=None, where="") -> pd.DataFrame:
    if table_exists and not table_exists(table):
        return pd.DataFrame()
    return q(f"SELECT * FROM {table} WHERE store_key IN {_in(keys)} {where}")


# ---------------------------------------------------------------- summaries
def summarise(pid: str, d: pd.DataFrame, b: pd.DataFrame, defn: pd.Series) -> dict:
    """Headline numbers for one promo across the selected stores."""
    w = d[(d.promo_id == pid) & d.in_window]
    pre = d[(d.promo_id == pid) & ~d.in_window]
    bb = b[b.promo_id == pid]
    gwp = defn.mechanic == "gwp"
    qual = float(w.qualifying_baskets.sum())
    applied = None if gwp else float(w.applied_baskets.fillna(0).sum())
    days_in = int(w[w.store_baskets > 0].day.nunique())
    qual_pd = qual / days_in if days_in else np.nan
    net_pd = float(w.matched_net.sum()) / days_in if days_in else np.nan
    base_qual_pd = float(bb.base_qualifying_pd.fillna(0).sum()) if not bb.empty else 0.0
    base_net_pd = float(bb.base_matched_net_pd.fillna(0).sum()) if not bb.empty else 0.0
    has_base = not bb.empty and bb.base_days.fillna(0).sum() > 0
    # clean run-up: how many days were used (the most any selected store had) and why
    base_days = int(bb.base_days.fillna(0).max()) if has_base else 0
    notes = [str(x) for x in bb.get("base_note", pd.Series(dtype=object)).dropna().unique()] if not bb.empty else []
    base_note = notes[0] if len(notes) == 1 else ("; ".join(notes) if notes else "")
    vs = (qual_pd / base_qual_pd - 1) if (has_base and base_qual_pd > 0 and days_in) else np.nan
    last3_days = sorted(w[w.store_baskets > 0].day.unique())[-3:]
    last3 = float(w[w.day.isin(last3_days)].qualifying_baskets.sum()) if last3_days else 0.0
    known = float(w.known_customers.sum())
    return dict(
        pid=pid, name=defn["name"], mechanic=defn.mechanic, brand=defn.brand_label,
        start=defn.start, end=defn.end, status=defn.status, last_day=defn.last_day,
        qualifying=qual, applied=applied,
        applied_pct=(applied / qual) if (applied is not None and qual) else np.nan,
        units=float(w.matched_units.sum()), net=float(w.matched_net.sum()),
        est=float(w.est_discount.sum()), nearmiss=float(w.nearmiss_baskets.sum()),
        new=float(w.new_customers.sum()), known=known,
        new_share=(float(w.new_customers.sum()) / known) if known else np.nan,
        days_in=days_in, qual_pd=qual_pd, base_qual_pd=base_qual_pd, has_base=has_base,
        base_days=base_days, base_note=base_note,
        vs=vs, vs_dollar=(net_pd - base_net_pd) if (has_base and days_in) else np.nan,
        last3=last3, last3_days=len(last3_days),
        qual_net=float(w.qual_basket_net.sum()), store_net=float(w.store_net.sum()),
        store_baskets=float(w.store_baskets.sum()),
        pre_units=float(pre.matched_units.sum()), pre_net=float(pre.matched_net.sum()),
    )


def _days_word(s: dict) -> str:
    last = s["last_day"]
    if s["status"] == "upcoming":
        n = (s["start"] - last).days
        return f"starts in {n} day{'s' if n != 1 else ''}"
    if s["status"] == "done":
        return f"finished {s['end']:%b} {s['end'].day}"
    n = (s["end"] - last).days
    return "last day today" if n == 0 else f"{n} day{'s' if n != 1 else ''} left"


def _vs_short(s: dict) -> str:
    """Table cell: the % when it means something, the two rates when it does not."""
    if s["status"] == "upcoming" or not s["has_base"]:
        return "—"
    if not s["base_qual_pd"]:
        return "none before"
    if not np.isfinite(s["vs"]):
        return "—"
    if s["vs"] > 3:
        return f"{s['base_qual_pd']:.1f} → {s['qual_pd']:.1f}/day"
    return _pct(s["vs"], signed=True)


def _base_label(s: dict, short: bool = False) -> str:
    """'vs the 14 clean days before' (or 'vs run-up' when short), driven by base_days."""
    n = s.get("base_days") or 0
    if short:
        return "vs run-up"
    if not n:
        return "vs the days before"
    return f"vs the {n} clean day{'s' if n != 1 else ''} before"


def _base_help(s: dict) -> str:
    """One sentence on what the run-up was, for help= and captions."""
    note = s.get("base_note") or ""
    n = s.get("base_days") or 0
    core = (f"The run-up is {n} clean day{'s' if n != 1 else ''} before the window: days under "
            "another offer on the same brand are left out, and the window reaches further back when "
            "that leaves too few days.") if n else "No run-up days in the data before this window."
    return core + (f" Used here: {note}." if note else "")


def _vs_text(s: dict) -> str:
    if s["status"] == "upcoming":
        return "not started"
    if not s["has_base"]:
        return "no run-up days in the data"
    if not s["base_qual_pd"]:
        return f"offer did not exist before ({_money(s['vs_dollar'], signed=True)}/day in brand sales)"
    if not np.isfinite(s["vs"]):
        return "—"
    if s["vs"] > 3:        # a percent of almost nothing misleads: show the rates instead
        return (f"{s['base_qual_pd']:.1f}/day → {s['qual_pd']:.1f}/day "
                f"({_money(s['vs_dollar'], signed=True)}/day in brand sales)")
    return f"{_pct(s['vs'], signed=True)} ({_money(s['vs_dollar'], signed=True)}/day in brand sales)"


# ---------------------------------------------------------------- live board
def _card(s: dict, accent: str, warn: str) -> str:
    status_word = {"active": "Running", "upcoming": "Coming up", "done": "Finished"}[s["status"]]
    tone = accent if s["status"] == "active" else ("var(--muted)" if s["status"] == "done" else warn)
    applied = ("n/a (gift tracked on Takeovers)" if s["applied"] is None
               else (_pct(s["applied_pct"]) if s["qualifying"] else "—"))
    rows = [
        ("Qualifying baskets", _int(s["qualifying"])),
        ("Applied", applied),
        ("Brand units / net", f"{_int(s['units'])} / {_money(s['net'])}"),
        ("Est. discount", _money(s["est"])),
        (f'<span title="{html.escape(_base_help(s), quote=True)}">{_base_label(s)}</span>', _vs_text(s)),
        ("New-customer share", f"{_pct(s['new_share'])} of known buyers" if np.isfinite(s["new_share"]) else "—"),
    ]
    if s["status"] == "upcoming":
        n = s.get("base_days") or 0
        rows = [("Qualifying so far", "nothing yet — window not open"),
                ("Run-up brand sales", f"{_int(s['pre_units'])} units / {_money(s['pre_net'])} in the "
                                       f"{n} day{'s' if n != 1 else ''} before" if n else "no run-up days in the data")]
    body = "".join(
        f'<div style="display:flex;justify-content:space-between;gap:.6rem;font-size:.84rem;'
        f'padding:.12rem 0;border-top:1px solid var(--rule);"><span style="color:var(--muted);">{k}</span>'
        f'<span style="color:var(--ink);text-align:right;">{v}</span></div>' for k, v in rows)
    return (
        f'<div style="background:var(--tint);border:1px solid var(--rule);border-left:4px solid {tone};'
        f'border-radius:10px;padding:.8rem 1rem;margin:.3rem 0 .8rem 0;">'
        f'<div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);">'
        f'{status_word} · {_days_word(s)}</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:var(--ink);line-height:1.25;margin:.15rem 0 .1rem 0;">'
        f'{s["name"]}</div>'
        f'<div style="font-size:.8rem;color:var(--body);margin-bottom:.4rem;">{s["brand"]} · '
        f'{s["start"]:%b} {s["start"].day} – {s["end"]:%b} {s["end"].day} · {MECHANIC_LABEL.get(s["mechanic"], s["mechanic"])}</div>'
        f'{body}</div>')


def _pick_for_board(defs: pd.DataFrame) -> list[str]:
    last = defs.last_day.max()
    active = defs[defs.status == "active"].sort_values("start", ascending=False)
    up = defs[(defs.status == "upcoming") & (defs.start <= last + dt.timedelta(days=30))].sort_values("start")
    done = defs[defs.status == "done"].sort_values("end", ascending=False).head(2)
    return list(active.promo_id) + list(up.promo_id) + list(done.promo_id)


def _daily_chart(pid: str, s: dict, d: pd.DataFrame, accent: str, warn: str, key: str) -> None:
    rows = d[d.promo_id == pid].groupby("day", as_index=False).agg(
        qualifying=("qualifying_baskets", "sum"), est=("est_discount", "sum"),
        store_baskets=("store_baskets", "sum"))
    rows = rows[rows.store_baskets > 0]
    if rows.empty:
        st.info("No day rows for this promo at the selected stores yet.")
        return
    fig = go.Figure()
    in_win = [(s["start"] <= dd <= s["end"]) for dd in rows.day]
    fig.add_trace(go.Bar(
        x=rows.day, y=rows.qualifying, name="Qualifying baskets",
        marker_color=[accent if w else charts._rgba(accent, 0.35) for w in in_win],
        hovertemplate="%{x|%a %b %e}: %{y:,} qualifying<extra></extra>"))
    if s["has_base"]:
        fig.add_hline(y=s["base_qual_pd"], line=dict(dash="dash", color=warn, width=2),
                      annotation_text=f"{s['base_days']} clean days before: {s['base_qual_pd']:.1f}/day",
                      annotation_position="top left")
    lo, hi = rows.day.min(), rows.day.max()
    if s["start"] <= hi:
        fig.add_vrect(x0=pd.Timestamp(max(s["start"], lo)) - pd.Timedelta(hours=12),
                      x1=pd.Timestamp(min(s["end"], hi)) + pd.Timedelta(hours=12),
                      fillcolor=charts._rgba(accent, 0.07), line_width=0,
                      annotation_text="promo window", annotation_position="top right")
    fig.update_layout(showlegend=False, yaxis_title="Qualifying baskets per day", xaxis_title="")
    charts.show(fig, key=f"{key}_qual", kind="bar")
    if s["mechanic"] not in ("gwp", "ghost_sku") and rows.est.sum() > 0:
        f2 = go.Figure(go.Bar(x=rows.day, y=rows.est, marker_color=warn, name="Est. discount",
                              hovertemplate="%{x|%a %b %e}: $%{y:,.0f} est. discount<extra></extra>"))
        f2.update_layout(showlegend=False, yaxis_title="Est. discount $ per day", xaxis_title="",
                         yaxis_tickformat="$,.0f")
        charts.show(f2, key=f"{key}_est", kind="small", height=220)


def _goal_editor(goals: dict, pid: str, keys: list[int], stores: dict) -> dict:
    """Session-only goal inputs for one promo; returns {store_key: goal or None}."""
    out = {}
    cols = st.columns(len(keys))
    for c, k in zip(cols, keys):
        current = (goals.get(pid, {}).get(str(k), {}) or {}).get("qualifying_baskets")
        skey = f"pl_goal_{pid}_{k}"
        if skey not in st.session_state:
            st.session_state[skey] = int(current) if current else 0
        with c:
            v = st.number_input(f"{stores.get(k, k)} goal", min_value=0, step=10, key=skey,
                                help="Qualifying baskets you want from this promo at this store, "
                                     "over the whole window. 0 = no goal. Session only — download "
                                     "the JSON below and commit it to keep it.")
        out[k] = int(v) if v else None
        goals.setdefault(pid, {})
        if v:
            goals[pid][str(k)] = {**goals[pid].get(str(k), {}), "qualifying_baskets": int(v)}
        elif str(k) in goals[pid] and "qualifying_baskets" in goals[pid][str(k)]:
            goals[pid][str(k)].pop("qualifying_baskets", None)
            if not goals[pid][str(k)]:
                goals[pid].pop(str(k), None)
        if not goals[pid]:
            goals.pop(pid, None)
    return out


def _leaderboard(pid: str, s: dict, d: pd.DataFrame, b: pd.DataFrame, keys, stores,
                 goals_for: dict) -> pd.DataFrame:
    w = d[(d.promo_id == pid) & d.in_window]
    g = w.groupby("store_key").agg(
        qualifying=("qualifying_baskets", "sum"), applied=("applied_baskets", "sum"),
        units=("matched_units", "sum"), net=("matched_net", "sum"), est=("est_discount", "sum"),
        days=("store_baskets", lambda x: int((x > 0).sum())), nearmiss=("nearmiss_baskets", "sum"))
    window_days = (s["end"] - s["start"]).days + 1
    rows = []
    for k in keys:
        r = g.loc[k] if k in g.index else None
        qual = float(r.qualifying) if r is not None else 0.0
        days = int(r.days) if r is not None else 0
        goal = goals_for.get(k)
        pace = (qual / days * window_days) if days else np.nan
        if s["status"] == "upcoming":
            status = "not started"
        elif goal:
            if s["status"] == "done":
                status = "hit goal" if qual >= goal else "missed goal"
            else:
                status = "on pace" if (np.isfinite(pace) and pace >= goal) else "behind"
        else:
            status = "no goal" if s["status"] == "active" else "done"
        rows.append({
            "Store": stores.get(k, str(k)),
            "Qualifying": qual,
            "Applied %": (float(r.applied) / qual) if (r is not None and qual and s["mechanic"] != "gwp") else np.nan,
            "Brand units": float(r.units) if r is not None else 0.0,
            "Brand net $": float(r.net) if r is not None else 0.0,
            "Est. discount $": float(r.est) if r is not None else 0.0,
            "Near misses": float(r.nearmiss) if r is not None else 0.0,
            "Goal": goal if goal else np.nan,
            "Pace (full window)": pace,
            "Status": status,
        })
    return pd.DataFrame(rows)


def _live_board(q, keys, stores, defs, d, b, accent, warn, bad, goals: dict, table_exists) -> None:
    st.markdown("##### What is running now")
    _note("One card per promo at the selected stores: running first, then anything starting in the "
          "next 30 days, then the last two that finished. <b>Qualifying</b> = the basket looks like the "
          "offer; <b>applied</b> = the price, or the till discount on the basket, shows the deal was "
          "given (an estimate — see the Anatomy tab). \"vs the N clean days before\" compares qualifying "
          "baskets per day with the same rate in the run-up to the window, where days under another "
          "offer on the same brand are left out (hover the label on a card for what was used), "
          "with the change in the brand's daily sales beside it. Dates are read against the last "
          f"sales day in the data ({defs.last_day.max():%b} {defs.last_day.max().day}).")
    order = _pick_for_board(defs)
    if not order:
        st.info("No promos in the calendar yet — add one to promos.py.")
        return
    summ = {pid: summarise(pid, d, b, defs[defs.promo_id == pid].iloc[0]) for pid in defs.promo_id}
    cols = st.columns(3)
    for i, pid in enumerate(order):
        with cols[i % 3]:
            st.markdown(_card(summ[pid], accent, warn), unsafe_allow_html=True)

    # ---- alerts ---------------------------------------------------------
    alerts = []
    for pid in order:
        s = summ[pid]
        if s["status"] != "active":
            continue
        if s["applied"] is not None and s["qualifying"] >= 10 and s["applied_pct"] < 0.7:
            alerts.append((f"<b>{s['name']}</b>: only {_pct(s['applied_pct'])} of the "
                           f"{_int(s['qualifying'])} qualifying baskets show the deal in the price or the "
                           "till discount. Either the offer is not being rung every time, or these baskets "
                           "bought the brand without asking for it — worth a check at the till.", "a-bad"))
        if s["nearmiss"] >= 10:
            step = {"bundle_price": "one unit short", "bogo": "one unit short", "multi_pct_off": "one eighth short",
                    "threshold_addon": "within $15 of the spend bar with no add-on"}.get(s["mechanic"], "one step short")
            alerts.append((f"<b>{s['name']}</b>: {_int(s['nearmiss'])} baskets were {step} "
                           f"({_int(s['nearmiss'] / max(s['days_in'], 1))} a day). A nudge at the counter "
                           "would reach them.", "a-warn"))
        if s["days_in"] >= 3 and s["last3_days"] >= 3 and s["last3"] == 0:
            alerts.append((f"<b>{s['name']}</b>: no qualifying baskets in the last 3 days of a running "
                           "promo. Either it has stopped landing or the brand pattern in promos.py is "
                           "wrong — check both.", "a-bad"))
        if s["mechanic"] == "gwp":
            alerts.append((f"<b>{s['name']}</b>: the free item itself is not visible here — the GWP "
                           "section on the Takeovers tab tracks the gift stock.", "a-ok"))
    if alerts:
        st.markdown("##### Worth a look")
        for html, tone in alerts:
            _alert(html, tone)

    # ---- pick a promo ---------------------------------------------------
    st.markdown("##### Day by day")
    names = {pid: f"{summ[pid]['name']} ({summ[pid]['status']})" for pid in order}
    pid = st.selectbox("Promo", order, format_func=lambda x: names.get(x, x), key="pl_live_pick")
    s = summ[pid]
    if s["status"] == "upcoming":
        st.info(f"**{s['name']}** {_days_word(s)}. The chart shows the run-up only — how the "
                "promoted items were selling before the offer — so the window can be judged "
                "against it once it opens.")
    _daily_chart(pid, s, d, accent, warn, key=f"pl_live_{pid}")
    run_up = (f"the {s['base_days']} clean days before" if s["has_base"] else "the days before")
    _note(f"Bars are qualifying baskets per day (lighter bars are the run-up before the window). "
          f"The dashed line is the per-day rate in {run_up} — days under another offer on the same brand "
          "are left out, so the Wyld weeks are not measured against each other"
          + (f" ({s['base_note']})" if s.get("base_note") else "") + ". The second chart is the estimated "
          "discount given on applied baskets: base unit price minus the price paid, or the till discount "
          "where the price does not show it.")

    # ---- leaderboard + goals -------------------------------------------
    st.markdown("##### Stores and goals")
    with st.expander("Set goals for this promo (session only)", expanded=False):
        goals_for = _goal_editor(goals, pid, keys, stores)
    lb = _leaderboard(pid, s, d, b, keys, stores, goals_for)
    _df(lb, {
        "Qualifying": st.column_config.NumberColumn(format="%d", help=tip("qualifying")),
        "Applied %": st.column_config.NumberColumn(format="percent", help=tip("applied")),
        "Brand units": st.column_config.NumberColumn(format="%d"),
        "Brand net $": st.column_config.NumberColumn(format="dollar"),
        "Est. discount $": st.column_config.NumberColumn(format="dollar", help=tip("est. discount")),
        "Near misses": st.column_config.NumberColumn(format="%d", help=tip("near miss")),
        "Goal": st.column_config.NumberColumn(format="%d"),
        "Pace (full window)": st.column_config.NumberColumn(
            format="%d", help="Qualifying baskets per day so far, carried across the whole window."),
    })
    _note("Pace = qualifying baskets per day so far × days in the window. \"On pace\" means that "
          "figure clears the goal. Applied % is blank for gift-with-purchase offers (the gift is "
          "tracked on the Takeovers tab).")
    c1, c2 = st.columns(2)
    c1.download_button("Download live board (CSV)", lb.to_csv(index=False),
                       f"promo_live_{pid}.csv", "text/csv", key=f"pl_live_csv_{pid}")
    c2.download_button("Download goals JSON", json.dumps(goals, indent=2, sort_keys=True),
                       "promo_goals.json", "application/json", key="pl_goals_json",
                       help="Paste over Cerebral/config/promo_goals.json and commit to keep the goals.")


# ---------------------------------------------------------------- anatomy
def _default_anatomy(defs: pd.DataFrame) -> str:
    act = defs[defs.status == "active"].sort_values("start", ascending=False)
    if not act.empty:
        return act.promo_id.iloc[0]
    done = defs[defs.status == "done"].sort_values("end", ascending=False)
    return (done if not done.empty else defs).promo_id.iloc[0]


def _anatomy(q, keys, stores, defs, d, b, accent, warn, bad, series, table_exists) -> None:
    order = list(defs.sort_values("start", ascending=False).promo_id)
    default = _default_anatomy(defs)
    names = {r.promo_id: f"{r['name']} ({r.status})" for _, r in defs.iterrows()}
    pid = st.selectbox("Promo", order, index=order.index(default), format_func=lambda x: names.get(x, x), key="pl_anat_pick")
    defn = defs[defs.promo_id == pid].iloc[0]
    s = summarise(pid, d, b, defn)
    st.markdown(f"##### {s['name']}")
    rules = promos.MECHANICS.get(s["mechanic"], {})
    try:
        qual_rule = rules.get("qualifying", "").format(**(defn["params"] or {}))
    except (KeyError, IndexError, ValueError):
        qual_rule = rules.get("qualifying", "")
    _note(f"{s['brand']} · {MECHANIC_LABEL.get(s['mechanic'], s['mechanic'])} · "
          f"{s['start']:%b} {s['start'].day} – {s['end']:%b} {s['end'].day} · {_days_word(s)} · "
          f"{_store_names(stores, keys)}. Qualifying rule: {qual_rule}. "
          f"Applied rule: {rules.get('applied', '')}. <b>Applied is an estimate</b>: the sales export "
          "carries each product's average price for the load period, not the price on the receipt, so "
          "the till discount on the basket is used as a second sign the deal was given.")
    if s["status"] == "upcoming":
        st.info(f"This promo {_days_word(s)}. Only the run-up is in the data, so the sections below "
                "fill in once the window opens.")
    m = st.columns(5)
    m[0].metric("Qualifying baskets", _int(s["qualifying"]), help=tip("qualifying"))
    m[1].metric("Applied", "n/a" if s["applied"] is None else _pct(s["applied_pct"]), help=tip("applied"))
    m[2].metric("Brand net in window", _money(s["net"]))
    m[3].metric("Est. discount", _money(s["est"]), help=tip("est. discount"))
    m[4].metric("Near misses", _int(s["nearmiss"]), help=tip("near miss"))
    downloads = {}

    # ---- who ------------------------------------------------------------
    cust = _tbl(q, "dash_promo_customer", keys, table_exists, f"AND promo_id = '{pid}'")
    ch = _tbl(q, "dash_promo_channel", keys, table_exists, f"AND promo_id = '{pid}'")
    # full width, one under the other: side by side the six-column tables were cropped
    left = right = st.container()
    with left:
        st.markdown("**Who bought it**")
        if cust.empty:
            st.caption("No qualifying baskets yet.")
        else:
            seg = cust[~cust.segment.str.startswith("tier:")].groupby("segment", as_index=False).agg(
                baskets=("qualifying_baskets", "sum"), customers=("customers", "sum"),
                net=("qual_basket_net", "sum"), repeat=("repeat_baskets", "sum"))
            tot = seg.baskets.sum()
            seg["Share"] = seg.baskets / tot if tot else np.nan
            seg["Avg basket $"] = seg.net / seg.baskets.replace(0, np.nan)
            seg["Segment"] = seg.segment.map(SEGMENT_LABEL).fillna(seg.segment)
            show = seg[["Segment", "baskets", "Share", "net", "Avg basket $", "repeat"]].rename(
                columns={"baskets": "Qualifying", "net": "Basket net $", "repeat": "Repeat baskets"})
            _df(show, {
                "Qualifying": st.column_config.NumberColumn(format="%d"),
                "Share": st.column_config.NumberColumn(format="percent"),
                "Basket net $": st.column_config.NumberColumn(format="dollar"),
                "Avg basket $": st.column_config.NumberColumn(format="dollar"),
                "Repeat baskets": st.column_config.NumberColumn(
                    format="%d", help="Qualifying baskets from someone who had already qualified earlier in the window."),
            })
            downloads["who"] = show
            tiers = cust[cust.segment.str.startswith("tier:")]
            if not tiers.empty:
                t = tiers.groupby("segment", as_index=False).agg(
                    baskets=("qualifying_baskets", "sum"), net=("qual_basket_net", "sum"))
                t["Tier"] = t.segment.str.replace("tier:", "", regex=False)
                t["Share of known"] = t.baskets / t.baskets.sum() if t.baskets.sum() else np.nan
                _df(t[["Tier", "baskets", "Share of known", "net"]].rename(
                    columns={"baskets": "Qualifying", "net": "Basket net $"}), {
                    "Qualifying": st.column_config.NumberColumn(format="%d"),
                    "Share of known": st.column_config.NumberColumn(format="percent"),
                    "Basket net $": st.column_config.NumberColumn(format="dollar"),
                })
            _note("New = this was the customer's first basket on record (the sales history starts in "
                  "mid-2025, so a long-lapsed customer can read as new). Unknown = no account on the "
                  "sale, so nothing is known about them. Tier rows cover known customers only.")
    with right:
        st.markdown("**Which channel**")
        if ch.empty:
            st.caption("No channel rows yet.")
        else:
            g = ch.groupby("channel", as_index=False).agg(
                qualifying=("qualifying_baskets", "sum"), store=("store_baskets", "sum"), net=("matched_net", "sum"))
            g["Share of qualifying"] = g.qualifying / g.qualifying.sum() if g.qualifying.sum() else np.nan
            g["Store's channel mix"] = g.store / g.store.sum() if g.store.sum() else np.nan
            g["Reach in channel"] = g.qualifying / g.store.replace(0, np.nan)
            show = g.rename(columns={"channel": "Channel", "qualifying": "Qualifying", "net": "Brand net $"})[
                ["Channel", "Qualifying", "Share of qualifying", "Store's channel mix", "Reach in channel", "Brand net $"]]
            _df(show, {
                "Qualifying": st.column_config.NumberColumn(format="%d"),
                "Share of qualifying": st.column_config.NumberColumn(format="percent"),
                "Store's channel mix": st.column_config.NumberColumn(format="percent"),
                "Reach in channel": st.column_config.NumberColumn(
                    format="percent", help="Qualifying baskets as a share of all baskets in that channel during the window."),
                "Brand net $": st.column_config.NumberColumn(format="dollar"),
            })
            downloads["channel"] = show
            _note("When the share of qualifying runs ahead of the store's channel mix, the offer is "
                  "landing better in that channel than the store's traffic would suggest.")

    # ---- when -----------------------------------------------------------
    hr = _tbl(q, "dash_promo_hour", keys, table_exists, f"AND promo_id = '{pid}'")
    st.markdown("**When people buy it**")
    if hr.empty or hr.qualifying_baskets.sum() == 0:
        st.caption("No qualifying baskets to place on the week yet.")
    else:
        g = hr.groupby(["day_of_week", "hour"], as_index=False).agg(
            q=("qualifying_baskets", "sum"), s=("store_baskets", "sum"))
        hours = sorted(h for h in g.hour.unique() if g[g.hour == h].s.sum() >= 20)
        z, text = [], []
        for dow in range(1, 8):
            row, trow = [], []
            for h in hours:
                c = g[(g.day_of_week == dow) & (g.hour == h)]
                sb = float(c.s.sum()) if not c.empty else 0.0
                qq = float(c.q.sum()) if not c.empty else 0.0
                share = qq / sb if sb >= 10 else np.nan
                row.append(share * 100 if np.isfinite(share) else None)
                trow.append(f"{DOW_SHORT[dow]} {h:02d}:00<br>{int(qq)} of {int(sb)} baskets")
            z.append(row)
            text.append(trow)
        fig = go.Figure(go.Heatmap(
            z=z, x=[f"{h:02d}" for h in hours], y=[DOW_SHORT[i] for i in range(1, 8)],
            text=text, hovertemplate="%{text}<br>%{z:.1f}% of baskets<extra></extra>",
            colorscale=[[0, charts._rgba(accent, 0.06)], [1, accent]], showscale=True,
            colorbar=dict(title="% of baskets", ticksuffix="%", thickness=10)))
        fig.update_layout(xaxis_title="Hour of day", yaxis_title="", yaxis_autorange="reversed")
        charts.show(fig, key=f"pl_anat_heat_{pid}", kind="heat", height=300)
        _note("Each cell is qualifying baskets as a share of all baskets in that hour, so a dark "
              "cell means the offer is a bigger part of what sells then — not just a busy hour. Cells "
              "with fewer than 10 baskets are left blank.")

    # ---- attach ---------------------------------------------------------
    att = _tbl(q, "dash_promo_attach", keys, table_exists, f"AND promo_id = '{pid}'")
    st.markdown("**What else is in the basket**")
    if att.empty:
        st.caption("No attach rows yet.")
    else:
        g = att.groupby("category", as_index=False).agg(
            qwith=("qual_baskets_with_cat", "sum"), qnet=("qual_cat_net", "sum"),
            owith=("other_baskets_with_cat", "sum"), other=("other_baskets", "sum"), qual=("qual_baskets", "sum"))
        g["Share of qualifying"] = g.qwith / g.qual.replace(0, np.nan)
        g["Share of other baskets"] = g.owith / g.other.replace(0, np.nan)
        g["Attach index"] = g["Share of qualifying"] / g["Share of other baskets"].replace(0, np.nan)
        g["Net $ in qualifying baskets"] = g.qnet
        g = g[g.qwith > 0].sort_values("Attach index", ascending=False)
        if (defn.category or "") in set(g.category):
            g["is_promo_cat"] = g.category == defn.category
        show = g[["category", "qwith", "Share of qualifying", "Share of other baskets", "Attach index",
                  "Net $ in qualifying baskets"]].rename(columns={"category": "Category", "qwith": "Qualifying baskets with it"})
        _df(show, {
            "Qualifying baskets with it": st.column_config.NumberColumn(format="%d"),
            "Share of qualifying": st.column_config.NumberColumn(format="percent"),
            "Share of other baskets": st.column_config.NumberColumn(format="percent"),
            "Attach index": st.column_config.NumberColumn(
                format="%.2f", help="Share among qualifying baskets ÷ share among the other baskets in the window. "
                                    "Above 1.3 means the category rides along with the offer; the promo's own category "
                                    "always sits at the top."),
            "Net $ in qualifying baskets": st.column_config.NumberColumn(format="dollar"),
        })
        downloads["attach"] = show
        _note("The promo's own category will read high by definition. Look at the rows under it: a "
              "category with an attach index over 1.3 and real dollars beside it is a bundle candidate.")

    # ---- price & volume -------------------------------------------------
    st.markdown(f"**Price and volume {_base_label(s)}**", help=_base_help(s))
    bb = b[b.promo_id == pid]
    w = d[(d.promo_id == pid) & d.in_window]
    units_pd = s["units"] / s["days_in"] if s["days_in"] else np.nan
    base_units_pd = float(bb.base_matched_units_pd.fillna(0).sum()) if not bb.empty else np.nan
    price_in = s["net"] / s["units"] if s["units"] else np.nan
    base_px = float((bb.base_unit_price * bb.base_matched_units_pd.fillna(0)).sum()
                    / max(bb.base_matched_units_pd.fillna(0).sum(), 1e-9)) if not bb.empty and bb.base_unit_price.notna().any() else np.nan
    if not np.isfinite(base_px) or base_px <= 0:
        base_px = float(bb.base_unit_price.dropna().mean()) if (not bb.empty and bb.base_unit_price.notna().any()) else np.nan
    gm = float((bb.base_gm_pct * bb.base_matched_net_pd.fillna(0)).sum()
               / max(bb.base_matched_net_pd.fillna(0).sum(), 1e-9)) if not bb.empty and bb.base_gm_pct.notna().any() else np.nan
    if (not np.isfinite(gm) or gm <= 0) and s["net"] > 0:
        gm = float(w.matched_gm.sum()) / s["net"]
    depth = (1 - price_in / base_px) if (np.isfinite(price_in) and np.isfinite(base_px) and base_px > 0) else np.nan
    vol_lift = (units_pd / base_units_pd - 1) if (np.isfinite(units_pd) and base_units_pd > 0) else np.nan
    be = (depth / (gm - depth)) if (np.isfinite(depth) and np.isfinite(gm) and gm > depth > 0) else np.nan
    c = st.columns(4)
    c[0].metric("Brand units per day", _int(units_pd),
                delta=(f"{_pct(vol_lift, signed=True)} vs {_int(base_units_pd)}/day" if np.isfinite(vol_lift) else None))
    c[1].metric("Unit price in window", f"${price_in:,.2f}" if np.isfinite(price_in) else "—",
                delta=(f"{_pct(-depth, signed=True)} vs ${base_px:,.2f}" if np.isfinite(depth) else None),
                delta_color="inverse" if np.isfinite(depth) and depth > 0 else "normal")
    c[2].metric("Run-up margin", _pct(gm),
                help=f"Gross margin on the promoted brand lines in the run-up. {_base_help(s)}")
    c[3].metric("Break-even volume lift", _pct(be) if np.isfinite(be) else ("none needed" if np.isfinite(depth) and depth <= 0 else "—"),
                help=tip("break-even volume lift"))
    if s["status"] == "upcoming" or s["days_in"] < 5:
        verdict = "Too early to call — fewer than 5 days in the window."
        tone = "a-warn"
    elif not np.isfinite(depth) or depth <= 0:
        verdict = (f"Units per day moved {_pct(vol_lift, signed=True)} and the unit price did not fall "
                   f"({_money((units_pd - base_units_pd) * (price_in if np.isfinite(price_in) else 0), signed=True)}/day in brand sales). "
                   "No discount to pay back, so any lift is a gain.") if np.isfinite(vol_lift) else \
                  "The unit price did not fall, so there is no discount to pay back."
        tone = "a-ok"
    elif not np.isfinite(be):
        verdict = (f"The discount depth ({_pct(depth)}) is at or above the brand's margin ({_pct(gm)}): "
                   "no amount of extra volume pays it back.")
        tone = "a-bad"
    elif np.isfinite(vol_lift) and vol_lift >= be:
        verdict = (f"Units per day rose {_pct(vol_lift, signed=True)} ({_money(s['vs_dollar'], signed=True)}/day in brand sales); "
                   f"the {_pct(depth)} discount needed {_pct(be)} just to break even. <b>It paid.</b>")
        tone = "a-ok"
    elif np.isfinite(vol_lift):
        verdict = (f"Units per day moved {_pct(vol_lift, signed=True)} ({_money(s['vs_dollar'], signed=True)}/day in brand sales); "
                   f"the {_pct(depth)} discount needed {_pct(be)} just to break even. <b>It did not pay</b> on margin, "
                   f"with {_money(s['est'])} of estimated discount given.")
        tone = "a-bad"
    else:
        verdict = "No pre-window volume to compare against."
        tone = "a-warn"
    _alert(verdict, tone)
    _note("Break-even volume lift = depth ÷ (margin − depth): how much more volume the brand must "
          "sell for the margin dollars to come back to where they were. It judges the brand's margin "
          "only — pull-through on the rest of the basket is the next block.")

    # ---- basket size ----------------------------------------------------
    st.markdown("**Basket size**")
    other_b = s["store_baskets"] - s["qualifying"]
    avg_q = s["qual_net"] / s["qualifying"] if s["qualifying"] else np.nan
    avg_o = (s["store_net"] - s["qual_net"]) / other_b if other_b > 0 else np.nan
    c = st.columns(3)
    c[0].metric("Avg qualifying basket", _money(avg_q))
    c[1].metric("Avg other basket (same window)", _money(avg_o))
    c[2].metric("Difference", _money(avg_q - avg_o, signed=True) if np.isfinite(avg_q) and np.isfinite(avg_o) else "—",
                delta=(_pct(avg_q / avg_o - 1, signed=True) if np.isfinite(avg_q) and np.isfinite(avg_o) and avg_o else None))
    _note("Net (after discounts) per basket. A qualifying basket will usually be bigger because it holds "
          "more units of the brand — the useful question is whether it is bigger by more than the offer itself.")

    # ---- spotlight items -------------------------------------------------
    if s["mechanic"] == "spotlight":
        items = _tbl(q, "dash_promo_spotlight_item", keys, table_exists, f"AND promo_id = '{pid}'")
        if not items.empty:
            st.markdown("**Spotlight items**")
            items["day"] = pd.to_datetime(items["day"]).dt.date
            win = items[items.in_window]
            pre = items[~items.in_window]
            g_pre = pre.groupby("item_label").agg(units=("units", "sum"), days=("day", "nunique"),
                                                 net=("net", "sum"))
            g_win = win.groupby("item_label").agg(units=("units", "sum"), days=("day", "nunique"),
                                                 net=("net", "sum"), baskets=("baskets", "sum"))
            rows = []
            prices = {it["label"]: it["price"] for it in (defn["params"].get("items") or [])}
            for lab in sorted(items.item_label.unique()):
                pu = g_pre.units.get(lab, 0.0); pdays = g_pre.days.get(lab, 0)
                wu = g_win.units.get(lab, 0.0); wdays = g_win.days.get(lab, 0)
                rows.append({
                    "Item": lab, "Spotlight price": prices.get(lab, np.nan),
                    "Run-up price": (g_pre.net.get(lab, 0.0) / pu) if pu else np.nan,
                    "Run-up units/day": (pu / pdays) if pdays else np.nan,
                    "Window units/day": (wu / wdays) if wdays else np.nan,
                    "Window price": (g_win.net.get(lab, 0.0) / wu) if wu else np.nan,
                    "Window baskets": g_win.baskets.get(lab, 0.0),
                })
            show = pd.DataFrame(rows)
            _df(show, {c: st.column_config.NumberColumn(format="dollar") for c in
                       ("Spotlight price", "Run-up price", "Window price")} | {
                       "Run-up units/day": st.column_config.NumberColumn(format="%.1f"),
                       "Window units/day": st.column_config.NumberColumn(format="%.1f"),
                       "Window baskets": st.column_config.NumberColumn(format="%d")})
            downloads["spotlight"] = show
            n = s.get("base_days") or 0
            _note("Window columns stay blank until the spotlight opens. Run-up = the "
                  f"{n} clean day{'s' if n != 1 else ''} before the window.")

    if downloads:
        frames = [df.assign(block=k) for k, df in downloads.items()]
        csv = pd.concat(frames, ignore_index=True).to_csv(index=False)
        st.download_button("Download anatomy tables (CSV)", csv, f"promo_anatomy_{pid}.csv", "text/csv",
                           key=f"pl_anat_csv_{pid}")


# ---------------------------------------------------------------- designer
def _brand_gm(bp: pd.DataFrame, brand: str, category: str | None) -> float:
    r = bp[bp.brand == brand]
    if category:
        r = r[r.category == category]
    net = float(r.net.sum()) if not r.empty else 0.0
    return float(r.gm.sum()) / net if net > 0 else np.nan


def _designer(q, keys, stores, defs, accent, warn, bad, table_exists, partial_week) -> None:
    st.markdown("##### Try an offer before you run it")
    _note(f"Everything here comes from the last {DESIGNER_WEEKS} complete weeks at {_store_names(stores, keys)}. "
          "Baskets that <b>already</b> buy enough to qualify get the discount for free — that is the cost. "
          "Baskets one step short are the <b>stretch</b>: the people the offer is really for. If the brand "
          "ran an offer inside those weeks, its baskets are in these counts too, so \"already qualify\" "
          "reads high for a brand that has just been on promotion.")
    qd = _tbl(q, "dash_promo_qty_dist", keys, table_exists)
    gb = _tbl(q, "dash_promo_gross_band", keys, table_exists)
    bp = _tbl(q, "dash_promo_brand_price", keys, table_exists)
    if qd.empty and gb.empty:
        st.info("The designer tables (dash_promo_qty_dist / dash_promo_gross_band) are not in the published "
                "file yet. They arrive with the next refresh.")
        return
    for c in ("baskets", "units", "net", "avg_unit_price"):
        if c in qd:
            qd[c] = pd.to_numeric(qd[c], errors="coerce").fillna(0.0)
    mech = st.selectbox("Mechanic", ["bundle_price", "bogo", "multi_pct_off", "threshold_addon"],
                        format_func=lambda m: MECHANIC_LABEL.get(m, m), key="pl_des_mech")
    brands = sorted(qd.brand.dropna().unique()) if not qd.empty else []
    c1, c2 = st.columns(2)
    with c1:
        default_brand = "Wyld" if "Wyld" in brands else (brands[0] if brands else None)
        brand = st.selectbox("Brand", brands, index=brands.index(default_brand) if default_brand else 0,
                             key="pl_des_brand") if brands else None
    cats = sorted(qd[qd.brand == brand].category.dropna().unique()) if brand else []
    with c2:
        category = st.selectbox("Category", cats, key="pl_des_cat") if cats else None
    sub = qd[(qd.brand == brand) & (qd.category == category)] if brand and category else pd.DataFrame()
    by_q = sub.groupby("units_in_basket").agg(baskets=("baskets", "sum"), units=("units", "sum"), net=("net", "sum")) \
        if not sub.empty else pd.DataFrame(columns=["baskets", "units", "net"])
    p = float(sub.net.sum() / sub.units.sum()) if (not sub.empty and sub.units.sum() > 0) else np.nan
    gm = _brand_gm(bp, brand, category) if (brand and not bp.empty) else np.nan
    if not np.isfinite(gm):
        gm = 0.5
    wk = DESIGNER_WEEKS

    def at_least(n):
        return float(by_q[by_q.index >= n].baskets.sum())

    def exactly(n):
        return float(by_q.loc[n].baskets) if n in by_q.index else 0.0

    params = {}
    if mech == "threshold_addon":
        if gb.empty:
            st.info("dash_promo_gross_band is not published yet.")
            return
        a, bcol, c3 = st.columns(3)
        thr = a.slider("Spend bar ($)", 30, 150, 65, 5, key="pl_des_thr")
        addon = bcol.slider("Add-on price ($)", 1, 40, 10, 1, key="pl_des_addon")
        take = c3.slider("Share who take the add-on", 5, 100, 25, 5, format="%d%%", key="pl_des_take") / 100
        params = {"threshold": float(thr), "addon_price": float(addon)}
        band = gb.groupby("band_lo", as_index=False).agg(baskets=("baskets", "sum"), net=("net", "sum"))
        total = float(band.baskets.sum())
        above = float(band[band.band_lo >= thr].baskets.sum())
        stretch = float(band[(band.band_lo >= thr - 15) & (band.band_lo < thr)].baskets.sum())
        disc = max(0.0, (p if np.isfinite(p) else addon) - addon)
        depth = disc / p if np.isfinite(p) and p > 0 else np.nan
        cost_wk = above / wk * take * disc
        margin_per = gm * thr          # rough: brand margin rate on a basket at the bar
        need = cost_wk / (margin_per - disc) if margin_per > disc else np.inf
        m = st.columns(4)
        m[0].metric("Baskets already at or above the bar", f"{above / wk:,.0f} / week",
                    delta=f"{_pct(above / total) if total else '—'} of baskets", delta_color="off")
        m[1].metric("In the stretch band ($15 below)", f"{stretch / wk:,.0f} / week",
                    delta=f"{_pct(stretch / total) if total else '—'} of baskets", delta_color="off")
        m[2].metric("Weekly cost at current behaviour", _money(cost_wk),
                    help=f"Baskets already over the bar × {take:.0%} taking the add-on × "
                         f"(${p:,.2f} regular price − ${addon} add-on price).")
        m[3].metric("Extra baskets/week to break even", "never on its own" if not np.isfinite(need) else f"{need:,.0f}",
                    help="Weekly cost ÷ (margin on a basket at the bar − the discount given).")
        fig = go.Figure(go.Bar(
            x=band.band_lo, y=band.baskets / wk,
            marker_color=[accent if lo >= thr else (warn if lo >= thr - 15 else charts._rgba(accent, 0.35)) for lo in band.band_lo],
            hovertemplate="$%{x}–$%{customdata}: %{y:,.0f} baskets a week<extra></extra>",
            customdata=[("+" if lo >= 200 else lo + 5) for lo in band.band_lo]))
        fig.add_vline(x=thr - 2.5, line=dict(color=bad, dash="dash", width=2),
                      annotation_text=f"${thr} bar · {_pct(above / total) if total else '—'} at/above",
                      annotation_position="top right")
        fig.update_layout(showlegend=False, xaxis_title="Gross basket ($5 bands; 200 = $200+)",
                          yaxis_title="Baskets per week", xaxis_tickprefix="$")
        charts.show(fig, key="pl_des_band", kind="bar")
        _note("Gross basket = what the basket came to before discounts. Dark bars already clear the bar; "
              "orange bars are within $15 of it — the ones a counter prompt can push over.")
        sentence = (f"At {_store_names(stores, keys)}, {above / wk:,.0f} baskets a week already spend ${thr}+ "
                    f"({_pct(above / total) if total else '—'} of all baskets) and {stretch / wk:,.0f} a week sit within $15 of it. "
                    f"If {take:.0%} of qualifiers take a {brand} {category} for ${addon} (regular ${p:,.2f}), the offer costs about "
                    f"{_money(cost_wk)} a week before it brings in anyone new"
                    + (f"; it needs about {need:,.0f} extra ${thr}+ baskets a week to pay for itself." if np.isfinite(need)
                       else "; the add-on discount is bigger than the margin on a basket at the bar, so it cannot pay for itself on volume."))
    else:
        if sub.empty:
            st.info("Pick a brand and category with enough baskets (brands with 200+ baskets in the period are listed).")
            return
        a, bcol = st.columns(2)
        if mech == "bundle_price":
            qty = a.slider("Units in the bundle", 2, 5, 3, key="pl_des_qty")
            reg = qty * p if np.isfinite(p) else 0.0
            bundle = bcol.slider("Bundle price ($)", int(max(1, reg * 0.5)), int(max(2, reg)),
                                 int(round(reg * 0.8)), 1, key="pl_des_bundle")
            params = {"qty": qty, "bundle_price": float(bundle)}
            disc = max(0.0, reg - bundle)
            already, stretch = at_least(qty), exactly(qty - 1)
            label = f"{qty} for ${bundle}"
        elif mech == "bogo":
            buy = a.slider("Buy", 1, 4, 2, key="pl_des_buy")
            get = bcol.slider("Get free", 1, 2, 1, key="pl_des_get")
            qty = buy + get
            params = {"buy": buy, "get": get}
            disc = get * p if np.isfinite(p) else 0.0
            already, stretch = at_least(qty), exactly(buy)
            label = f"buy {buy} get {get} free"
        else:
            qty = a.slider("Units needed", 2, 6, 4, key="pl_des_mqty")
            pct = bcol.slider("Percent off", 5, 50, 25, 5, format="%d%%", key="pl_des_pct")
            params = {"qty": qty, "pct": pct, "single_brand": True}
            disc = qty * p * pct / 100 if np.isfinite(p) else 0.0
            already, stretch = at_least(qty), exactly(qty - 1)
            label = f"{qty} for {pct}% off"
        reg = qty * p if np.isfinite(p) else np.nan
        depth = disc / reg if np.isfinite(reg) and reg > 0 else np.nan
        cost_wk = already / wk * disc
        margin_per = gm * reg if np.isfinite(reg) else np.nan
        need = cost_wk / (margin_per - disc) if (np.isfinite(margin_per) and margin_per > disc) else np.inf
        m = st.columns(4)
        m[0].metric("Baskets that already qualify", f"{already / wk:,.1f} / week",
                    help="Baskets already holding enough units — they get the discount without changing anything.")
        m[1].metric("One unit short (the stretch)", f"{stretch / wk:,.1f} / week",
                    help="Baskets one unit short of qualifying. The offer's real audience.")
        m[2].metric("Weekly cost at current behaviour", _money(cost_wk),
                    delta=f"{_pct(depth)} off ${reg:,.2f}" if np.isfinite(depth) else None, delta_color="off")
        m[3].metric("Extra qualifying baskets/week to break even",
                    "never on its own" if not np.isfinite(need) else f"{need:,.1f}",
                    help="Weekly cost ÷ (margin on a qualifying bundle − the discount on it).")
        dist = by_q.reset_index()
        dist["label"] = dist.units_in_basket.map(lambda u: "6+" if u >= 6 else str(int(u)))
        fig = go.Figure(go.Bar(
            x=dist.label, y=dist.baskets / wk,
            marker_color=[accent if u >= qty else (warn if u == qty - 1 else charts._rgba(accent, 0.35)) for u in dist.units_in_basket],
            hovertemplate="%{x} unit(s): %{y:,.1f} baskets a week<extra></extra>"))
        fig.update_layout(showlegend=False, xaxis_title=f"{brand} {category} units in the basket",
                          yaxis_title="Baskets per week", xaxis_type="category")
        charts.show(fig, key="pl_des_qty_chart", kind="bar", height=260)
        sentence = (f"At {_store_names(stores, keys)}, {already / wk:,.1f} {brand} {category} baskets a week already hold "
                    f"{qty}+ units and would get \"{label}\" for free (about {_money(cost_wk)} a week at "
                    f"{_pct(depth)} off ${reg:,.2f}). {stretch / wk:,.1f} baskets a week are one unit short. "
                    + (f"The offer needs about {need:,.1f} extra qualifying baskets a week — "
                       f"{_pct(need / max(already / wk, 1e-9))} more than today — to pay for itself on {brand}'s "
                       f"{_pct(gm)} margin." if np.isfinite(need)
                       else f"At {_pct(depth)} off, the discount is bigger than the {_pct(gm)} margin on the bundle, so it cannot pay for itself on volume."))
    _alert(sentence, "a-ok" if np.isfinite(need) and need < max(stretch / wk, 1) else "a-warn")
    _note("\"Pay for itself\" means margin dollars on this brand return to where they were — the basket "
          "around the offer is a bonus on top. Break-even counts extra baskets at the full bundle "
          "margin minus the discount; a stretch bigger than the break-even figure is a good sign.")

    # ---- depth ceiling from the channel model ----------------------------
    if table_exists and table_exists("dash_channel_dow") and table_exists("dash_channel_pair"):
        try:
            dow = promo_calendar._dow(q)
            pairs = promo_calendar._pairs(q)
        except Exception:
            dow, pairs = pd.DataFrame(), pd.DataFrame()
        if not dow.empty and not pairs.empty:
            lines = []
            for sk in keys:
                sdf = dow[dow.store_key == sk]
                if sdf.empty:
                    continue
                marg = sdf.groupby("channel").apply(lambda g: g["gm"].sum() / max(g["net"].sum(), 1e-9))
                for to_ch in marg.index:
                    if to_ch == "In-Store":
                        continue
                    best = None
                    for from_ch in marg.index:
                        if from_ch == to_ch:
                            continue
                        k, n, fb = promo_calendar._k_for(pairs, sk, from_ch, to_ch)
                        if np.isfinite(k) and k > 0:
                            d0 = marg[to_ch] - marg[from_ch] / k
                            best = d0 if best is None else min(best, d0)
                    if best is not None:
                        lines.append(f"{stores.get(sk, sk)} {to_ch}: {_pct(best, 1) if best > 0 else 'none'}")
            if lines:
                _note("<b>Depth ceiling if this runs as a channel offer</b> (from the Promo Calendar maths — "
                      "above it, a customer who switches channel earns less than before): " + " · ".join(lines) + ".")

    # ---- copy definition ------------------------------------------------
    if st.button("Copy definition", key="pl_des_copy", help="Prints the promos.py block for this offer."):
        pid = f"{(brand or 'brand').lower().replace(' ', '_').replace('.', '')}_{mech}"
        block = {
            "id": pid, "name": f"{brand} {category}: {MECHANIC_LABEL[mech]}", "mechanic": mech,
            "brand_label": brand, "brand_patterns": [str(brand).lower()],
            "category": category, "product_regex": None, "exclude_brand_patterns": [],
            "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "stores": None if len(keys) == len(stores) else list(keys),
            "takeover": None, "program": "New offer", "params": params, "notes": "Drafted in the Promo Lab designer",
        }
        body = ",\n".join(f'    "{k}": {json.dumps(v) if not isinstance(v, str) or k in ("start", "end") else repr(v)}'
                          for k, v in block.items())
        st.code("{\n" + body.replace("null", "None").replace("true", "True").replace("false", "False") + ",\n},",
                language="python")
        st.caption("Paste this at the end of PROMOS in promos.py, fix the dates and the id, then run "
                   "`python -c \"import promos; promos.validate_promos()\"`.")


# ---------------------------------------------------------------- history
def _history(q, keys, stores, defs, d, b, accent, warn, series, takeovers) -> None:
    st.markdown("##### Every promo on one timeline")
    tl = defs.copy()
    tl["Start"] = pd.to_datetime(tl.start)
    tl["Finish"] = pd.to_datetime(tl.end) + pd.Timedelta(days=1)
    tl["Program"] = tl.program.fillna("Other")
    last = pd.Timestamp(defs.last_day.max())
    fig = px.timeline(tl, x_start="Start", x_end="Finish", y="name", color="Program",
                      color_discrete_sequence=series or None, hover_data={"status": True, "mechanic": True})
    fig.update_yaxes(autorange="reversed", title="")
    lo, hi = tl.Start.min() - pd.Timedelta(days=14), max(tl.Finish.max(), last) + pd.Timedelta(days=14)
    for tk in (takeovers or []):
        try:
            ts, te = pd.Timestamp(tk["start"]), pd.Timestamp(tk["end"]) + pd.Timedelta(days=1)
        except Exception:
            continue
        if te < lo or ts > hi:
            continue
        kw = {}
        if (te - ts).days >= 25:          # short bands get hover-free, label-free shading
            kw = dict(annotation_text=tk["name"], annotation_position="top left",
                      annotation_font=dict(size=10, color="#7A8590"))
        fig.add_vrect(x0=ts, x1=te, fillcolor="rgba(0,0,0,0.045)", line_width=0, layer="below", **kw)
    fig.add_vline(x=last, line=dict(color=warn, dash="dot", width=1.5),
                  annotation_text=f"data through {last:%b} {last.day}", annotation_position="bottom right")
    fig.update_layout(xaxis=dict(range=[lo, hi], title=""), height=max(300, 40 * len(tl) + 130),
                      legend=dict(orientation="h", y=-0.12, yanchor="top", x=0, title_text=""),
                      margin=dict(t=30, b=0, l=0, r=8))
    charts.show(fig, key="pl_hist_timeline", kind="hbar")
    _note("Grey bands behind the bars are brand takeovers from the Takeovers tab. The dotted line is the "
          "last day of sales in the data.")

    st.markdown("##### How the finished ones did")
    done = defs[defs.status == "done"].sort_values("end", ascending=False)
    if done.empty:
        st.caption("No finished promos yet.")
    else:
        rows = []
        for _, r in done.iterrows():
            s = summarise(r.promo_id, d, b, r)
            rows.append({"Promo": s["name"], "Window": f"{s['start']:%b} {s['start'].day} – {s['end']:%b} {s['end'].day}",
                         "Qualifying": s["qualifying"],
                         "Applied %": s["applied_pct"] if s["applied"] is not None else np.nan,
                         "Brand net $": s["net"],
                         "Est. discount $": np.nan if s["mechanic"] in ("gwp", "ghost_sku") else s["est"],
                         "Run-up days": float(s["base_days"]) if s["has_base"] else np.nan,
                         "vs run-up": _vs_short(s), "Brand $/day change": s["vs_dollar"],
                         "New-buyer share": s["new_share"]})
        res = pd.DataFrame(rows)
        for c in ("Qualifying", "Applied %", "Brand net $", "Est. discount $", "Run-up days",
                  "Brand $/day change", "New-buyer share"):
            res[c] = pd.to_numeric(res[c], errors="coerce")       # NaN, never a stray None
        _df(res, {
            "Qualifying": st.column_config.NumberColumn(format="%d"),
            "Applied %": st.column_config.NumberColumn(format="percent", help="Blank for gift offers (not detectable here)."),
            "Brand net $": st.column_config.NumberColumn(format="dollar"),
            "Est. discount $": st.column_config.NumberColumn(
                format="dollar", help="Blank for gift and free-item offers: the cost of the free item is not in these tables."),
            "Run-up days": st.column_config.NumberColumn(
                format="%d", help="Clean days before the window used as the comparison: days under another "
                                  "offer on the same brand are left out, so the count differs between promos."),
            "vs run-up": st.column_config.TextColumn(
                help="Qualifying baskets per day in the window against the rate over the clean run-up days in the "
                     "column to the left. Shown as the two rates when the offer barely existed before."),
            "Brand $/day change": st.column_config.NumberColumn(
                format="dollar", help="Brand sales per day in the window minus brand sales per day in the run-up."),
            "New-buyer share": st.column_config.NumberColumn(
                format="percent", help="Share of qualifying baskets with an account whose first basket on record was this one."),
        })
        _note("\"none before\" means the offer could not qualify before it existed (a 3-for bundle has no "
              "natural baseline); \"0.1 → 25/day\" means it barely could. Read the brand $/day change beside it. "
              "Blank cells are things that cannot be measured for that offer, not zeros.")

    st.markdown("##### Compare two promos, day by day")
    opts = list(defs[defs.status != "upcoming"].sort_values("start").promo_id)
    if len(opts) < 2:
        st.caption("Needs two promos with a window in the data.")
        return
    names = {r.promo_id: r["name"] for _, r in defs.iterrows()}
    c1, c2 = st.columns(2)
    a = c1.selectbox("First", opts, index=len(opts) - 2, format_func=lambda x: names.get(x, x), key="pl_hist_a")
    bsel = c2.selectbox("Second", opts, index=len(opts) - 1, format_func=lambda x: names.get(x, x), key="pl_hist_b")
    fig = go.Figure()
    for i, pid in enumerate((a, bsel)):
        w = d[(d.promo_id == pid) & d.in_window].groupby("day", as_index=False).agg(
            q=("qualifying_baskets", "sum"), s=("store_baskets", "sum"))
        w = w[w.s > 0].sort_values("day")
        if w.empty:
            continue
        w["day_n"] = range(1, len(w) + 1)
        fig.add_trace(go.Scatter(x=w.day_n, y=w.q / w.s * 1000, mode="lines+markers", name=names[pid],
                                 line=dict(color=(series or [accent, warn])[i % 2]),
                                 hovertemplate="Day %{x}: %{y:.1f} per 1,000 baskets<extra>" + names[pid] + "</extra>"))
    fig.update_layout(xaxis_title="Day of the promo window", yaxis_title="Qualifying per 1,000 store baskets")
    charts.show(fig, key="pl_hist_compare", kind="line")
    _note("Each line starts on its promo's first day, and qualifying baskets are divided by the store's "
          "total baskets that day — so a quiet Tuesday and a busy Saturday compare fairly, and so do "
          "promos from different months.")


# ---------------------------------------------------------------- ideas
def _idea_nearmiss_bundles(qd: pd.DataFrame, bp: pd.DataFrame, keys, stores) -> list[dict]:
    out = []
    if qd.empty:
        return out
    store_txt = _store_names(stores, keys)
    g = qd.groupby(["brand", "category", "units_in_basket"], as_index=False).agg(
        baskets=("baskets", "sum"), units=("units", "sum"), net=("net", "sum"))
    for (brand, cat), grp in g.groupby(["brand", "category"]):
        tot_units = grp.units.sum()
        if tot_units <= 0:
            continue
        p = grp.net.sum() / tot_units
        for qty in (3, 4):
            stretch = float(grp[grp.units_in_basket == qty - 1].baskets.sum())
            already = float(grp[grp.units_in_basket >= qty].baskets.sum())
            if stretch < 20:
                continue
            bundle = int(round(qty * p * 0.85))
            disc = max(0.0, qty * p - bundle)
            out.append(dict(
                source="Near-miss stretch",
                idea=f"{qty} {brand} {cat.lower()} for ${bundle}",
                evidence=(f"At {store_txt}, {stretch / DESIGNER_WEEKS:,.0f} baskets a week hold exactly "
                          f"{qty - 1} {brand} {cat.lower()} ({_money(stretch / DESIGNER_WEEKS * (qty - 1) * p)}/week) "
                          f"and only {already / DESIGNER_WEEKS:,.0f} a week already hold {qty}+ — "
                          f"{_pct(already / max(stretch, 1))} as many."),
                offer=f"{qty} for ${bundle} (about 15% off ${qty * p:,.0f})", mechanic="bundle_price",
                stores=store_txt, cost_wk=already / DESIGNER_WEEKS * disc,
                reach_wk=stretch / DESIGNER_WEEKS * qty * p,
                confidence=_confidence(stretch), score=stretch / DESIGNER_WEEKS * p * qty - already / DESIGNER_WEEKS * disc,
                params={"qty": qty, "bundle_price": float(bundle)}, brand=brand, category=cat))
    return out


def _idea_threshold(gb: pd.DataFrame, keys, stores) -> list[dict]:
    out = []
    if gb.empty:
        return out
    store_txt = _store_names(stores, keys)
    band = gb.groupby("band_lo", as_index=False).agg(baskets=("baskets", "sum"), net=("net", "sum"))
    total = float(band.baskets.sum())
    if total <= 0:
        return out
    best = None
    for thr in (50, 65, 80, 100):
        above = float(band[band.band_lo >= thr].baskets.sum())
        stretch = float(band[(band.band_lo >= thr - 15) & (band.band_lo < thr)].baskets.sum())
        if stretch < 50:
            continue
        ratio = stretch / max(above, 1)
        if best is None or ratio > best[0]:
            best = (ratio, thr, above, stretch)
    if best:
        ratio, thr, above, stretch = best
        cost = above / DESIGNER_WEEKS * 0.25 * 12   # 25% take a $12-off add-on
        out.append(dict(
            source="Near-miss stretch",
            idea=f"Spend ${thr}, add an item for $10",
            evidence=(f"At {store_txt}, {stretch / DESIGNER_WEEKS:,.0f} baskets a week land within $15 under ${thr} "
                      f"({_pct(stretch / total)} of baskets, about {_money(stretch / DESIGNER_WEEKS * (thr - 7.5))}/week of sales in them) "
                      f"against {above / DESIGNER_WEEKS:,.0f} a week already at or above it ({_pct(above / total)})."),
            offer=f"Spend ${thr}, get a ~$22 item for $10", mechanic="threshold_addon", stores=store_txt,
            cost_wk=cost, reach_wk=stretch / DESIGNER_WEEKS * 15, confidence=_confidence(stretch),
            score=stretch / DESIGNER_WEEKS * 15 - cost, params={"threshold": float(thr), "addon_price": 10.0}))
    return out


def _idea_troughs(q, keys, stores, table_exists) -> list[dict]:
    out = []
    if table_exists and not table_exists("dash_channel_dow"):
        return out
    try:
        dow = promo_calendar._dow(q)
    except Exception:
        return out
    if dow.empty:
        return out
    scan = promo_calendar.trough_scan(dow, stores, [k for k in keys if k in set(dow.store_key)])
    if scan.empty:
        return out
    troughs = scan[scan["Index"] < 92].sort_values("Hole per year", ascending=False).head(3)
    for _, r in troughs.iterrows():
        gap = float(r["Gap to store average"])
        out.append(dict(
            source="Weekday trough",
            idea=f"{r['Day']} offer at {r['Store']}",
            evidence=(f"{r['Store']} takes {_money(r['Net per day'])} on a {r['Day']} — {_pct(1 - r['Index'] / 100)} "
                      f"({_money(gap)}) below its own average day, a {_money(r['Hole per year'])} hole a year."),
            offer=f"A {r['Day']}-only offer (double points or 10% off one category), scoped to {r['Store']}",
            mechanic="channel/day", stores=r["Store"], cost_wk=0.10 * float(r["Net per day"]),
            reach_wk=gap, confidence="fair", score=gap - 0.10 * float(r["Net per day"]),
            params=None))
    return out


def _idea_winback(q, keys, stores, table_exists) -> list[dict]:
    """Same maths as the Win-back model at its default assumptions
    (30-day lapse, 10% win-back, 20% depth, 30+ customers)."""
    out = []
    if table_exists and not table_exists("dash_promo_category"):
        return out
    pub = q(f"SELECT * FROM dash_promo_category WHERE store_key IN {_in(keys)}")
    if pub.empty or "churned_30" not in pub:
        return out
    g = pub.groupby("category").agg(customers=("customers", "sum"), spend=("spend_sum", "sum"),
                                     gm=("gm_sum", "sum"), churned=("churned_30", "sum"),
                                     lapsed_spend=("lapsed_spend_30", "sum")).reset_index()
    g = g[(g.customers >= 30) & (g.churned >= 5)]
    g["margin"] = np.where(g.spend > 0, g.gm / g.spend.replace(0, np.nan), 0.5)
    g["avg_lapsed"] = g.lapsed_spend / g.churned.clip(lower=1)
    g["winbacks"] = g.churned * 0.10
    g["revenue"] = g.winbacks * g.avg_lapsed
    g["cost"] = g.revenue * 0.20
    g["gain"] = g.revenue * g.margin - g.cost
    g = g[g.gain > 0].sort_values("gain", ascending=False).head(3)
    store_txt = _store_names(stores, keys)
    for _, r in g.iterrows():
        out.append(dict(
            source="Lapsed win-back",
            idea=f"Win back lapsed {r.category} buyers",
            evidence=(f"At {store_txt}, {int(r.churned):,} {r.category} buyers have not bought it in 30+ days "
                      f"({_pct(r.churned / r.customers)} of {int(r.customers):,}), worth {_money(r.avg_lapsed)} each in past spend. "
                      f"If 10% come back on 20% off, the margin clears the discount by {_money(r.gain)}."),
            offer=f"20% off {r.category} for lapsed buyers (targeted message, 2 weeks)", mechanic="targeted % off",
            stores=store_txt, cost_wk=float(r.cost) / 2, reach_wk=float(r.revenue) / 2,
            confidence=_confidence(r.churned), score=float(r.gain) / 2, params=None))
    return out


def _idea_attach(q, keys, stores, defs, table_exists) -> list[dict]:
    out = []
    att = _tbl(q, "dash_promo_attach", keys, table_exists)
    store_txt = _store_names(stores, keys)
    if not att.empty:
        for pid, grp in att.groupby("promo_id"):
            dfn = defs[defs.promo_id == pid]
            if dfn.empty:
                continue
            dfn = dfn.iloc[0]
            if dfn.mechanic in ("gwp", "ghost_sku"):   # the free item rides along by definition
                continue
            g = grp.groupby("category", as_index=False).agg(
                qwith=("qual_baskets_with_cat", "sum"), qnet=("qual_cat_net", "sum"),
                owith=("other_baskets_with_cat", "sum"), other=("other_baskets", "sum"), qual=("qual_baskets", "sum"))
            g = g[(g.category != (dfn.category or "")) & (g.qwith >= 15) & (g.other > 0) & (g.qual > 0)]
            if g.empty:
                continue
            g["idx"] = (g.qwith / g.qual) / (g.owith / g.other).replace(0, np.nan)
            g = g[g.idx >= 1.3].sort_values("idx", ascending=False).head(1)
            for _, r in g.iterrows():
                out.append(dict(
                    source="Attach pairing",
                    idea=f"Bundle {dfn.brand_label} with {r.category}",
                    evidence=(f"Baskets that bought \"{dfn['name']}\" also held {r.category} {r.idx:.1f}× as often as other "
                              f"baskets ({_pct(r.qwith / r.qual)} vs {_pct(r.owith / r.other)}), "
                              f"{_money(r.qnet)} of {r.category} across {int(r.qwith):,} baskets."),
                    offer=f"Add a {r.category.lower()} to the {dfn.brand_label} offer for $5 off",
                    mechanic="threshold_addon", stores=store_txt, cost_wk=float(r.qwith) * 5 / max((dfn.end - dfn.start).days / 7, 1),
                    reach_wk=float(r.qnet) / max((dfn.end - dfn.start).days / 7, 1), confidence=_confidence(r.qwith),
                    score=float(r.qnet) / max((dfn.end - dfn.start).days / 7, 1) * 0.3, params=None))
    if not out and table_exists and table_exists("dash_pairs"):
        pr = q(f"SELECT cat_a, cat_b, sum(joint_baskets) AS j FROM dash_pairs WHERE store_key IN {_in(keys)} "
               "GROUP BY 1,2 ORDER BY j DESC LIMIT 1")
        if not pr.empty:
            r = pr.iloc[0]
            out.append(dict(source="Attach pairing", idea=f"{r.cat_a} + {r.cat_b} bundle",
                            evidence=f"{r.cat_a} and {r.cat_b} share {int(r.j):,} multi-category baskets at {store_txt} — the most common pair.",
                            offer=f"$5 off when a {r.cat_a.lower()} and a {r.cat_b.lower()} are bought together",
                            mechanic="bundle", stores=store_txt, cost_wk=np.nan, reach_wk=np.nan, confidence="fair",
                            score=0.0, params=None))
    return out


def _idea_repeat(defs, d, b, keys, stores) -> list[dict]:
    out = []
    store_txt = _store_names(stores, keys)
    for _, r in defs[defs.status == "done"].iterrows():
        s = summarise(r.promo_id, d, b, r)
        if s["days_in"] < 5 or s["qualifying"] < 20:
            continue
        beat = (np.isfinite(s["vs"]) and s["vs"] > 0.25) or (not s["base_qual_pd"] and s["vs_dollar"] > 0)
        if not beat:
            continue
        weeks = max(s["days_in"] / 7, 1)
        if np.isfinite(s["vs"]) and s["vs"] <= 3:
            vs_txt = f"{_pct(s['vs'], signed=True)} more qualifying baskets a day"
        elif np.isfinite(s["vs"]):
            vs_txt = f"{s['qual_pd']:.1f} qualifying baskets a day against {s['base_qual_pd']:.1f} before"
        else:
            vs_txt = f"{s['qual_pd']:.1f} qualifying baskets a day where the offer had none before"
        gift = s["mechanic"] in ("gwp", "ghost_sku")        # the free item's cost is not in these tables
        cost_txt = ("; the cost of the free item is not measured here" if gift
                    else f" for {_money(s['est'])} of estimated discount")
        cost_wk = np.nan if gift else s["est"] / weeks
        out.append(dict(
            source="Worked before",
            idea=f"Repeat \"{s['name']}\"",
            evidence=(f"At {store_txt} it drew {_int(s['qualifying'])} qualifying baskets in {s['days_in']} days — "
                      f"{vs_txt} ({_money(s['vs_dollar'], signed=True)}/day in {s['brand']} sales){cost_txt}."),
            offer=f"Run it again: {MECHANIC_LABEL.get(s['mechanic'], s['mechanic'])}", mechanic=s["mechanic"],
            stores=store_txt, cost_wk=cost_wk, reach_wk=s["vs_dollar"] * 7 if np.isfinite(s["vs_dollar"]) else np.nan,
            confidence=_confidence(s["qualifying"]),
            score=(s["vs_dollar"] * 7 if np.isfinite(s["vs_dollar"]) else 0.0) - (0.0 if gift else s["est"] / weeks),
            params=r["params"]))
    return out


def _idea_new_magnets(defs, d, b, keys, stores) -> list[dict]:
    out = []
    store_txt = _store_names(stores, keys)
    rows = []
    for _, r in defs[defs.status != "upcoming"].iterrows():
        s = summarise(r.promo_id, d, b, r)
        if s["known"] >= 20 and np.isfinite(s["new_share"]):
            rows.append(s)
    if not rows:
        return out
    rows.sort(key=lambda s: s["new_share"], reverse=True)
    s = rows[0]
    out.append(dict(
        source="New-customer magnet",
        idea=f"Lead acquisition with \"{s['name']}\"",
        evidence=(f"{_pct(s['new_share'])} of its known buyers at {store_txt} were brand-new to TTA "
                  f"({_int(s['new'])} of {_int(s['known'])}), the highest share of any promo, with "
                  f"{_money(s['qual_net'] / max(s['qualifying'], 1))} average basket."),
        offer="Put this offer on the acquisition channels (Get Lost Guides, Boarding Pass, street team) rather than loyalty blasts",
        mechanic=s["mechanic"], stores=store_txt, cost_wk=s["est"] / max(s["days_in"] / 7, 1),
        reach_wk=s["new"] / max(s["days_in"] / 7, 1) * s["qual_net"] / max(s["qualifying"], 1),
        confidence=_confidence(s["known"]), score=s["new"] / max(s["days_in"] / 7, 1) * 40, params=None))
    return out


def _brief(q, keys, stores, defs, d, b, qd, gb, table_exists) -> str:
    """Plain-English summary of the store's behaviour, ending in a request for ideas."""
    store_txt = _store_names(stores, keys)
    lines = [f"PROMO IDEAS BRIEF — The Travel Agency (TTA), {store_txt}",
             f"Prepared from the Cerebral dashboard on {dt.date.today():%d %b %Y}. Aggregates only — no customer data.", ""]
    weeks_txt = ""
    if not gb.empty:
        wk = gb.assign(w=gb.iso_year * 100 + gb.iso_week)
        lo, hi = wk.sort_values("w").iloc[0], wk.sort_values("w").iloc[-1]
        weeks_txt = f"ISO weeks {int(lo.iso_year)}-W{int(lo.iso_week):02d} to {int(hi.iso_year)}-W{int(hi.iso_week):02d}"
        band = gb.groupby("band_lo").agg(baskets=("baskets", "sum"), net=("net", "sum"), disc=("discount", "sum"))
        tot = band.baskets.sum()
        avg_gross = (band.net.sum() + band.disc.sum()) / tot if tot else np.nan
        u40 = band[band.index < 40].baskets.sum() / tot if tot else np.nan
        b40_80 = band[(band.index >= 40) & (band.index < 80)].baskets.sum() / tot if tot else np.nan
        o80 = band[band.index >= 80].baskets.sum() / tot if tot else np.nan
        disc_rate = band.disc.sum() / (band.net.sum() + band.disc.sum()) if tot else np.nan
        lines += [f"COVERAGE: {store_txt}; {weeks_txt} ({DESIGNER_WEEKS} complete weeks); about {tot / DESIGNER_WEEKS:,.0f} baskets a week.",
                  "",
                  "BASKETS",
                  f"- Average gross basket (before discounts) ${avg_gross:,.0f}; {_pct(disc_rate)} of gross is given back as discounts.",
                  f"- {_pct(u40)} of baskets are under $40, {_pct(b40_80)} are $40–80, {_pct(o80)} are $80 or more."]
        ch = gb.groupby("channel").baskets.sum().sort_values(ascending=False)
        lines.append("- Channel mix: " + ", ".join(f"{c} {_pct(v / tot)}" for c, v in ch.items()) + ".")
        lines.append("")
    if not qd.empty:
        g = qd.groupby(["brand", "category"]).agg(baskets=("baskets", "sum"), net=("net", "sum")).reset_index()
        top = g.sort_values("net", ascending=False).head(6)
        lines += ["TOP BRANDS (by sales in the period)",
                  "- " + "; ".join(f"{r.brand} {r.category.lower()} {_money(r.net)}" for _, r in top.iterrows()) + "."]
        multi = qd.groupby("units_in_basket").baskets.sum()
        share2 = multi[multi.index >= 2].sum() / multi.sum() if multi.sum() else np.nan
        lines.append(f"- {_pct(share2)} of brand-baskets hold 2 or more units of the same brand; the rest are single units.")
        lines.append("")
    # dow / daypart
    try:
        dow = promo_calendar._dow(q) if (not table_exists or table_exists("dash_channel_dow")) else pd.DataFrame()
    except Exception:
        dow = pd.DataFrame()
    if not dow.empty:
        scan = promo_calendar.trough_scan(dow, stores, [k for k in keys if k in set(dow.store_key)])
        if not scan.empty:
            agg = scan.groupby("Day").agg(net=("Net per day", "sum"), idx=("Index", "mean"))
            peak, trough = agg.idx.idxmax(), agg.idx.idxmin()
            lines += ["WEEK SHAPE",
                      f"- Busiest day {peak} ({_money(agg.loc[peak, 'net'])}/day, index {agg.loc[peak, 'idx']:.0f}); "
                      f"quietest {trough} ({_money(agg.loc[trough, 'net'])}/day, index {agg.loc[trough, 'idx']:.0f}, 100 = an average day)."]
    hr = _tbl(q, "dash_promo_hour", keys, table_exists)
    if not hr.empty:
        h = hr.groupby("hour").store_baskets.sum()
        h = h[h > 0]
        if not h.empty:
            lines += [f"- Peak hour {int(h.idxmax()):02d}:00 ({_pct(h.max() / h.sum())} of baskets); the quietest open hour is "
                      f"{int(h.idxmin()):02d}:00 ({_pct(h.min() / h.sum())}). (Measured on promo windows.)"]
    if not dow.empty or not hr.empty:
        lines.append("")
    # attach
    att = _tbl(q, "dash_promo_attach", keys, table_exists)
    if not att.empty:
        g = att.groupby(["promo_id", "category"], as_index=False).agg(
            qwith=("qual_baskets_with_cat", "sum"), owith=("other_baskets_with_cat", "sum"),
            other=("other_baskets", "sum"), qual=("qual_baskets", "sum"))
        g = g.merge(defs[["promo_id", "brand_label", "category"]].rename(columns={"category": "pcat"}), on="promo_id")
        g = g[(g.category != g.pcat.fillna("")) & (g.qwith >= 15) & (g.other > 0) & (g.qual > 0)]
        if not g.empty:
            g["idx"] = (g.qwith / g.qual) / (g.owith / g.other).replace(0, np.nan)
            top = g.sort_values("idx", ascending=False).head(3)
            lines += ["WHAT GOES TOGETHER",
                      "- " + "; ".join(f"{r.brand_label} buyers also take {r.category} {r.idx:.1f}× as often as other baskets"
                                       for _, r in top.iterrows()) + ".", ""]
    # lapsed
    if not table_exists or table_exists("dash_promo_category"):
        pub = q(f"SELECT * FROM dash_promo_category WHERE store_key IN {_in(keys)}")
        if not pub.empty and "churned_30" in pub:
            g = pub.groupby("category").agg(c=("customers", "sum"), ch=("churned_30", "sum")).reset_index()
            g = g[g.c >= 30].assign(rate=lambda x: x.ch / x.c).sort_values("ch", ascending=False).head(3)
            lines += ["LAPSED BUYERS (no purchase of the category in 30+ days)",
                      "- " + "; ".join(f"{r.category}: {int(r.ch):,} of {int(r.c):,} ({_pct(r.rate)})" for _, r in g.iterrows()) + ".", ""]
    # promos
    lines.append("RECENT PROMOS (qualifying = basket looked like the offer; applied = the price or the till discount showed the deal was given, an estimate)")
    for _, r in defs.sort_values("start", ascending=False).iterrows():
        s = summarise(r.promo_id, d, b, r)
        if s["status"] == "upcoming":
            lines.append(f"- {s['name']} ({s['start']:%d %b}–{s['end']:%d %b}): {_days_word(s)}.")
            continue
        app = "" if s["applied"] is None else f", {_pct(s['applied_pct'])} applied"
        lines.append(f"- {s['name']} ({s['start']:%d %b}–{s['end']:%d %b}, {s['status']}): {_int(s['qualifying'])} qualifying baskets"
                     f"{app}, {_money(s['net'])} brand sales, {_money(s['est'])} est. discount, "
                     f"{_vs_text(s)}, new-customer share {_pct(s['new_share'])}.")
    lines += ["",
              "REQUEST",
              f"Using the picture above, suggest 5–8 promotional ideas for {store_txt} for the next 4–6 weeks. For each: "
              "the offer and mechanic (bundle price, spend-threshold add-on, buy-N-get-1, N-for-%-off, spotlight pricing, "
              "gift with purchase), which customers it is for, which day or daypart it should run, a rough weekly cost, "
              "and how we would tell within two weeks whether it paid for itself. Favour offers that stretch baskets "
              "one step further rather than discounting what already sells. Plain English please; we are a licensed "
              "New York cannabis dispensary, so no claims about effects and nothing aimed at under-21s."]
    return "\n".join(lines)


def _ideas(q, keys, stores, defs, d, b, accent, warn, table_exists) -> None:
    st.markdown("##### What the data suggests we should try next")
    _note("Rule-based, from the published tables — no guesswork and no AI. Each idea shows its evidence in one "
          "sentence, an offer to start from, a rough weekly cost if behaviour stayed as it is, and a confidence "
          "word (<b>solid</b> 200+ baskets behind it, <b>fair</b> 50–200, <b>thin</b> under 50). "
          f"Stores: {_store_names(stores, keys)}.")
    qd = _tbl(q, "dash_promo_qty_dist", keys, table_exists)
    gb = _tbl(q, "dash_promo_gross_band", keys, table_exists)
    bp = _tbl(q, "dash_promo_brand_price", keys, table_exists)
    for c in ("baskets", "units", "net"):
        if c in qd:
            qd[c] = pd.to_numeric(qd[c], errors="coerce").fillna(0.0)
    ideas: list[dict] = []
    for fn, args in ((_idea_nearmiss_bundles, (qd, bp, keys, stores)),
                     (_idea_threshold, (gb, keys, stores)),
                     (_idea_troughs, (q, keys, stores, table_exists)),
                     (_idea_winback, (q, keys, stores, table_exists)),
                     (_idea_attach, (q, keys, stores, defs, table_exists)),
                     (_idea_repeat, (defs, d, b, keys, stores)),
                     (_idea_new_magnets, (defs, d, b, keys, stores))):
        try:
            ideas += fn(*args)
        except Exception as exc:          # one bad source must not hide the others
            st.caption(f"({fn.__name__} skipped: {type(exc).__name__})")
    if not ideas:
        st.info("Not enough published data to suggest anything yet.")
    else:
        # keep the best near-miss bundles from crowding everything else out
        nm = [i for i in ideas if i["source"] == "Near-miss stretch" and i["mechanic"] == "bundle_price"]
        nm.sort(key=lambda i: i["score"], reverse=True)
        keep = set(id(i) for i in nm[:4])
        ideas = [i for i in ideas if not (i["source"] == "Near-miss stretch" and i["mechanic"] == "bundle_price") or id(i) in keep]
        ideas.sort(key=lambda i: (i["score"] if np.isfinite(i["score"]) else -1e9), reverse=True)
        ideas = ideas[:12]
        tbl = pd.DataFrame([{
            "#": n + 1, "Idea": i["idea"], "Why (the evidence)": i["evidence"], "Offer": i["offer"],
            "Mechanic": MECHANIC_LABEL.get(i["mechanic"], i["mechanic"]), "Stores": i["stores"],
            "Weekly cost at current behaviour": i["cost_wk"], "Weekly $ in reach": i["reach_wk"],
            "Confidence": i["confidence"], "Source": i["source"],
        } for n, i in enumerate(ideas)])
        tone = {"solid": "var(--accent)", "fair": "var(--warn)", "thin": "var(--muted)"}
        for n, i in enumerate(ideas):
            cost = _money(i["cost_wk"]) if np.isfinite(i["cost_wk"]) else "—"
            reach = _money(i["reach_wk"]) if np.isfinite(i["reach_wk"]) else "—"
            st.markdown(
                f'<div style="border:1px solid var(--rule);border-left:4px solid {tone.get(i["confidence"], "var(--muted)")};'
                f'border-radius:8px;padding:.6rem .9rem;margin:.35rem 0;background:var(--tint);">'
                f'<div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);">'
                f'#{n + 1} · {i["source"]} · confidence {i["confidence"]} · {i["stores"]}</div>'
                f'<div style="font-size:1rem;font-weight:700;color:var(--ink);margin:.1rem 0 .25rem 0;">{i["idea"]}</div>'
                f'<div style="font-size:.88rem;color:var(--body);">{i["evidence"]}</div>'
                f'<div style="font-size:.86rem;color:var(--ink);margin-top:.3rem;"><b>Try:</b> {i["offer"]} '
                f'<span style="color:var(--muted);">({MECHANIC_LABEL.get(i["mechanic"], i["mechanic"])})</span></div>'
                f'<div style="font-size:.82rem;color:var(--muted);margin-top:.2rem;">Weekly cost at current behaviour '
                f'<b style="color:var(--ink);">{cost}</b> · weekly $ in reach <b style="color:var(--ink);">{reach}</b></div>'
                f'</div>', unsafe_allow_html=True)
        _note("Ranked by weekly dollars in reach minus weekly cost. <b>Cost</b> is what the offer would give away "
              "each week to baskets that already behave that way; <b>reach</b> is the weekly sales sitting one step "
              "from the offer, or the weekly gap the idea addresses. Costs assume nobody changes behaviour; reach "
              "assumes everyone does — the truth sits between. Take the top one to the Designer tab to tune it.")
        st.download_button("Download ideas (CSV)", tbl.to_csv(index=False), "promo_ideas.csv", "text/csv",
                           key="pl_ideas_csv")

    st.markdown("##### Request ideas")
    _note("A plain-English brief of how these stores behave — basket sizes, channels, week shape, what sells "
          "together, who has lapsed and what recent promos did — ending in a request for ideas. Paste it into a "
          "chat with an AI assistant or send it to marketing. It holds no customer information.")
    brief = _brief(q, keys, stores, defs, d, b, qd, gb, table_exists)
    st.text_area("Brief", brief, height=420, key="pl_brief_text", label_visibility="collapsed")
    st.download_button("Download brief (.txt)", brief, "promo_ideas_brief.txt", "text/plain", key="pl_brief_dl")


# ---------------------------------------------------------------- entry point
def render_promo_lab(q, keys, stores, heading=None, table_exists=None,
                     accent="#2F6F4F", series=None, warn="#B4632B", bad="#9E3B32",
                     takeovers=None, partial_week=False, legacy=None) -> None:
    """The Promo Lab tab. See the module docstring for what each sub-tab shows."""
    title = "Promo Lab"
    st.subheader(title)
    st.caption("What our offers are doing, one promo at a time — and what the baskets say we should try next. "
               "Promos are detected from what is in the basket: qualifying (looks like the offer) and applied "
               "(the price, or the till discount on the basket, shows the deal was given — an estimate). "
               "Edit the calendar in promos.py.")
    keys = [int(k) for k in (keys or list(stores))]
    series = series or [accent, warn, "#3B6E8F", "#8A5A9E", "#A8913C", "#6E7F8D"]

    tabs = st.tabs(["Live board", "Anatomy", "Designer", "History", "Win-back model",
                    "Channel & Day", "Promo Calendar", "Ideas"])

    have = all(table_exists(t) for t in PROMO_TABLES) if table_exists else True
    defs = _defs(q) if have else pd.DataFrame()
    if defs.empty:
        for t in list(tabs[:4]) + [tabs[7]]:        # st.tabs returns a tuple
            with t:
                st.info("The published data file predates the promo tables (dash_promo_*). They arrive with the "
                        "next refresh — run publish_promo.py after publish.py.")
    else:
        d = _day(q, keys)
        b = _base(q, keys)
        goals = st.session_state.setdefault("pl_goals", promos.load_goals())
        with tabs[0]:
            _live_board(q, keys, stores, defs, d, b, accent, warn, bad, goals, table_exists)
        with tabs[1]:
            _anatomy(q, keys, stores, defs, d, b, accent, warn, bad, series, table_exists)
        with tabs[2]:
            _designer(q, keys, stores, defs, accent, warn, bad, table_exists, partial_week)
        with tabs[3]:
            _history(q, keys, stores, defs, d, b, accent, warn, series, takeovers)
        with tabs[7]:
            _ideas(q, keys, stores, defs, d, b, accent, warn, table_exists)

    with tabs[4]:
        if legacy is not None:
            legacy()
        else:
            st.info("The win-back model is not wired in.")
    with tabs[5]:
        render_channel_promo(q=q, keys=keys, stores=stores, heading=None,
                             table_exists=table_exists, accent=accent, series=series)
    with tabs[6]:
        render_promo_calendar(q=q, keys=keys, stores=stores, heading=None,
                              table_exists=table_exists, accent=accent, series=series)
