"""
promo_calendar.py -- planning surface for building a promo calendar.

The Channel & Day Promo tab scores ONE proposal. This one works the other
direction: it scans every store x weekday cell for where the money actually
is, sets a defensible depth ceiling per channel, and then asks the question
that decides whether a calendar entry is worth running at all --

    can a test of this promo even tell success from failure?

THREE SECTIONS

1. TROUGH SCANNER
   Every store x weekday, indexed to that store's own average, with the
   gap to the store average expressed in dollars per day and annualised.
   Ranked by opportunity. A trough is worth filling; a peak is not worth
   discounting, and the scanner refuses to rank peaks as opportunities
   no matter how large the store is.

2. DEPTH CEILING
   The switch-neutral depth d0 = m_p - m_a/k for every channel pair, from
   the measured within-customer basket-size ratio. Below d0 a migrated
   customer earns more than the one they replaced; above it you pay. This
   is the number that should cap every channel offer on the calendar, and
   vendor funding raises it to d0/(1-v).

3. TEST DESIGNER  <- the section that matters most
   TTA has never run a controlled discount test, so every figure in Promo
   Lab is a BREAKEVEN, not a prediction: what you would have to believe,
   never what is true. An alternating on/off design fixes that, but only
   if it is powered.

   For a two-arm comparison of n promoted days against n control days,
   with day-level standard deviation sd and mean daily net mu, the
   minimum detectable effect is

       MDE = (z_alpha/2 + z_beta) * sd * sqrt(2/n) / mu

   The gate: if MDE exceeds the breakeven lift the promo needs, the test
   cannot distinguish a promo that paid from one that did not, however
   long it runs. That is a reason to change the design -- deeper offer,
   more days in scope, more stores, longer window -- BEFORE spending the
   quarter, not after.

DATA
  dash_channel_dow    store x channel x weekday baseline
  dash_channel_pair   within-customer basket-size ratio -> k
  dash_channel_daysd  day-level mean and sd -> statistical power

All from publish_channel.py, which runs AFTER publish.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DOW_ORDER = [1, 2, 3, 4, 5, 6, 7]
DOW_NAME = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
            5: "Friday", 6: "Saturday", 7: "Sunday"}
DOW_SHORT = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu",
             5: "Fri", 6: "Sat", 7: "Sun"}
WEEKEND = [6, 7]

# Two-sided alpha = 0.05, and the two conventional power levels.
Z_ALPHA = 1.959964
Z_BETA = {80: 0.841621, 90: 1.281552}

MIN_PAIR_CUSTOMERS = 150


def _money(x) -> str:
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return "—"
    return ("-" if x < 0 else "") + f"${abs(x):,.0f}"


def _pct(x, places: int = 1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    if np.isinf(x):
        return "never"
    return f"{x * 100:.{places}f}%"


def _note(html: str) -> None:
    st.markdown(f'<p class="note">{html}</p>', unsafe_allow_html=True)


# ------------------------------------------------------------ data loads

def _dow(q) -> pd.DataFrame:
    df = q("SELECT * FROM dash_channel_dow")
    for c in ("baskets", "net", "gm", "units", "discount", "days"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def _daysd(q) -> pd.DataFrame:
    try:
        df = q("SELECT * FROM dash_channel_daysd")
    except Exception:
        return pd.DataFrame()
    for c in ("days", "mean_net", "sd_net", "mean_baskets", "sd_baskets"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _pairs(q) -> pd.DataFrame:
    try:
        df = q("SELECT * FROM dash_channel_pair")
    except Exception:
        return pd.DataFrame()
    for c in ("customers", "median_ratio", "aov_from", "aov_to"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _k_for(pairs: pd.DataFrame, store: int, ch_from: str,
           ch_to: str) -> tuple[float, int, bool]:
    def pick(scope):
        r = scope[(scope["ch_from"] == ch_from) & (scope["ch_to"] == ch_to)]
        if r.empty or pd.isna(r.iloc[0]["median_ratio"]):
            return np.nan, 0
        return float(r.iloc[0]["median_ratio"]), int(r.iloc[0]["customers"])

    k, n = pick(pairs[pairs["store_key"] == store])
    if n >= MIN_PAIR_CUSTOMERS:
        return k, n, False
    k0, n0 = pick(pairs[pairs["store_key"] == 0])
    if n0 > 0:
        return k0, n0, True
    return (k, n, False) if n else (np.nan, 0, False)


# ------------------------------------------------------------- the maths

def mde(sd: float, mean: float, n_per_arm: float, power: int = 80) -> float:
    """Minimum detectable effect as a fraction of the mean."""
    if not (sd > 0 and mean > 0 and n_per_arm > 0):
        return np.inf
    return (Z_ALPHA + Z_BETA[power]) * sd * np.sqrt(2.0 / n_per_arm) / mean


def days_needed(sd: float, mean: float, target: float,
                power: int = 80) -> float:
    """Days per arm required to detect a lift of `target`."""
    if not (sd > 0 and mean > 0 and target > 0):
        return np.inf
    return 2.0 * ((Z_ALPHA + Z_BETA[power]) * sd / (mean * target)) ** 2


def breakeven(m_p: float, m_a: float, ratio: float, sigma: float,
              k: float, depth: float, vendor: float = 0.0) -> float:
    """Same formula the Channel & Day Promo tab uses."""
    d = depth * (1.0 - vendor)
    if d >= m_p or m_p <= 0:
        return np.inf
    return (d / (m_p - d)
            + ratio * sigma * (m_a - k * (m_p - d)) / (m_p - d))


# ------------------------------------------------------------- rendering

def render_promo_calendar(q, keys, stores, heading=None, table_exists=None,
                          accent=None, series=None) -> None:
    title = "Promo calendar: where to look, how deep, and can you measure it"
    if heading:
        try:
            heading(title)
        except TypeError:
            st.markdown(f"#### {title}")
    else:
        st.markdown(f"#### {title}")

    if table_exists and not table_exists("dash_channel_dow"):
        st.info("Run publish.py then publish_channel.py, and restart.")
        return

    dow = _dow(q)
    if dow.empty:
        st.info("No channel data published.")
        return
    pairs = _pairs(q)
    dsd = _daysd(q)
    window = int(pd.to_numeric(dow["window_days"], errors="coerce").max() or 0)

    real = sorted(k for k in dow["store_key"].unique() if k != 0)

    # =================================================== 1. trough scanner
    st.markdown("### 1. Where the holes are")

    rows = []
    for sk in real:
        s = dow[dow["store_key"] == sk]
        daily = (s.groupby("dow")
                  .apply(lambda g: g["net"].sum() / max(g["days"].max(), 1)))
        avg = daily.mean()
        for d in DOW_ORDER:
            if d not in daily.index:
                continue
            gap = avg - daily[d]
            rows.append({
                "store_key": sk,
                "Store": stores.get(sk, str(sk)),
                "Day": DOW_NAME[d],
                "dow": d,
                "Net per day": daily[d],
                "Index": daily[d] / avg * 100,
                "Gap to store average": gap,
                # 52 of each weekday a year. Recovering the entire gap is
                # not on the table -- this sizes the hole, not the prize.
                "Hole per year": gap * 52,
            })
    scan = pd.DataFrame(rows)
    troughs = scan[scan["Index"] < 100].sort_values(
        "Hole per year", ascending=False)

    show = troughs.head(12).copy()
    st.dataframe(pd.DataFrame({
        "Store": show["Store"],
        "Day": show["Day"],
        "Net per day": [_money(v) for v in show["Net per day"]],
        "Index": [f"{v:.0f}" for v in show["Index"]],
        "Below store avg": [_money(v) for v in show["Gap to store average"]],
        "Annualised hole": [_money(v) for v in show["Hole per year"]],
    }), hide_index=True, use_container_width=True)

    _note(
        f"Days running <b>below</b> their own store's average over the last "
        f"{window} days, worst first. Peaks are excluded on purpose — a busy "
        f"day is not a promo opportunity, it is demand you already have. "
        f"<b>The annualised figure sizes the hole, not the prize:</b> no "
        f"promotion recovers a full trough, and a day that is structurally "
        f"empty (an office district on a Sunday) may not be recoverable at "
        f"all through price.")

    # ================================================== 2. depth ceilings
    st.markdown("### 2. How deep you can afford to go")

    vendor_pl = st.slider(
        "Vendor-funded share", 0, 100, 0, 5, format="%d%%", key="pcal_vendor",
        help="Raises every ceiling below to d0/(1-v). The customer sees the "
             "full depth either way.") / 100.0

    crows = []
    for sk in real:
        s = dow[dow["store_key"] == sk]
        marg = (s.groupby("channel")
                 .apply(lambda g: g["gm"].sum() / max(g["net"].sum(), 1e-9)))
        for to_ch in marg.index:
            for from_ch in marg.index:
                if to_ch == from_ch:
                    continue
                k, n, fb = _k_for(pairs, sk, from_ch, to_ch) \
                    if not pairs.empty else (np.nan, 0, False)
                if not np.isfinite(k) or k <= 0:
                    continue
                d0 = marg[to_ch] - marg[from_ch] / k
                crows.append({
                    "Store": stores.get(sk, str(sk)),
                    "Promote": to_ch,
                    "Pulls from": from_ch,
                    "k": k,
                    "n": n,
                    "fb": fb,
                    "d0": d0,
                })
    ceil = pd.DataFrame(crows)
    if not ceil.empty:
        ceil = ceil[ceil["Promote"] != "In-Store"]
        ceil["ceiling"] = ceil["d0"] / max(1.0 - vendor_pl, 1e-9)
        ceil = ceil.sort_values(["Store", "ceiling"], ascending=[True, False])
        st.dataframe(pd.DataFrame({
            "Store": ceil["Store"],
            "Promote": ceil["Promote"],
            "Pulls from": ceil["Pulls from"],
            "Basket ratio k": [f"{v:,.2f}" for v in ceil["k"]],
            "Measured on": [f"{n:,}" + (" (chain)" if f else "")
                            for n, f in zip(ceil["n"], ceil["fb"])],
            "Depth ceiling": [_pct(v, 1) if v > 0 else "none"
                              for v in ceil["ceiling"]],
        }), hide_index=True, use_container_width=True)
        _note(
            "The depth at which a customer pulled into the promoted channel "
            "earns exactly what they earned in the channel they left. Below "
            "it, migration is free or better; above it you pay on every "
            "switched basket, multiplied by how much bigger the other "
            "channel is. <b>A channel whose ceiling reads 'none' should not "
            "be discounted at all</b> — promote it on convenience instead.")

    # =================================================== 3. test designer
    st.markdown("### 3. Could you actually measure it?")

    if dsd.empty:
        st.info("dash_channel_daysd is not published. Re-run "
                "publish_channel.py to enable the test designer.")
        return

    t1, t2, t3 = st.columns(3)
    with t1:
        tstore = st.selectbox("Store", real, key="pcal_store",
                              format_func=lambda k: stores.get(k, str(k)))
    sdf = dow[dow["store_key"] == tstore]
    chans = (sdf.groupby("channel")["net"].sum()
                .sort_values(ascending=False).index.tolist())
    with t2:
        tchan = st.selectbox(
            "Channel", chans, key="pcal_chan",
            index=min(1, len(chans) - 1))
    with t3:
        tdays = st.multiselect(
            "Promoted days", DOW_ORDER, default=WEEKEND,
            format_func=lambda d: DOW_NAME[d], key="pcal_days")

    if not tdays:
        st.info("Pick at least one day.")
        return

    cell = dsd[(dsd["store_key"] == tstore) & (dsd["channel"] == tchan)
               & (dsd["dow"].isin(tdays))]
    if cell.empty or cell["mean_net"].isna().all():
        st.warning("No daily history for that combination.")
        return

    # Pool the scoped weekdays: weight means by observed days, and combine
    # variances the same way rather than averaging standard deviations,
    # which would understate the spread.
    nd = cell["days"].sum()
    mu = float((cell["mean_net"] * cell["days"]).sum() / nd)
    var = float((cell["sd_net"].fillna(0) ** 2 * (cell["days"] - 1)).sum()
                / max(nd - len(cell), 1))
    sd = float(np.sqrt(var))
    cv = sd / mu if mu else np.nan

    e1, e2, e3 = st.columns(3)
    e1.metric("Net per promoted day", _money(mu))
    e2.metric("Day-to-day spread", _money(sd),
              help=f"Standard deviation across {nd:,.0f} observed days.")
    e3.metric("Noise ratio", f"{cv:,.0%}" if np.isfinite(cv) else "—",
              help="Spread as a share of the mean. Above roughly 30% a "
                   "short test struggles to see anything.")

    d1, d2, d3 = st.columns(3)
    with d1:
        weeks = st.slider("Test length (weeks)", 4, 52, 26, 2,
                          key="pcal_weeks",
                          help="Alternating on/off, so half the weeks are "
                               "promoted and half are control.")
    with d2:
        power = st.radio("Power", [80, 90], horizontal=True, key="pcal_power",
                         format_func=lambda p: f"{p}%")
    with d3:
        tdepth = st.slider("Planned depth", 5, 30, 10, 1, format="%d%%",
                           key="pcal_depth") / 100.0

    n_arm = (weeks / 2.0) * len(tdays)
    detect = mde(sd, mu, n_arm, power)

    # The breakeven this promo would have to clear, on the same
    # assumptions the Channel & Day Promo tab uses.
    scope = sdf[sdf["dow"].isin(tdays)]
    p = scope[scope["channel"] == tchan]
    a = scope[scope["channel"] != tchan]
    net_p, net_a = p["net"].sum(), a["net"].sum()
    m_p = p["gm"].sum() / max(net_p, 1e-9)
    m_a = a["gm"].sum() / max(net_a, 1e-9)
    ratio = net_a / max(net_p, 1e-9)
    from_ch = (a.groupby("channel")["net"].sum().idxmax()
               if not a.empty else tchan)
    k, k_n, k_fb = _k_for(pairs, tstore, from_ch, tchan) \
        if not pairs.empty else (1.0, 0, False)
    k = k if np.isfinite(k) and k > 0 else 1.0
    need = breakeven(m_p, m_a, ratio, 0.05, k, tdepth, vendor_pl)

    st.markdown("#### The gate")
    g1, g2 = st.columns(2)
    g1.metric("Smallest lift the test can see", _pct(detect, 0),
              help=f"{n_arm:,.0f} promoted days against {n_arm:,.0f} "
                   f"control days.")
    g2.metric("Lift the promo needs to break even", _pct(need, 0),
              help=f"At {_pct(tdepth, 0)} off with 5% switching.")

    if not np.isfinite(detect) or not np.isfinite(need):
        st.warning("Not enough history to judge this design.")
    elif detect <= need:
        st.success(
            f"**This design works.** Over {weeks} weeks it resolves down to "
            f"{_pct(detect, 0)}, comfortably inside the {_pct(need, 0)} the "
            f"promo needs to clear. A null result here means the promo "
            f"really did not pay, not that you could not see it.")
    else:
        wk = days_needed(sd, mu, need, power) * 2 / max(len(tdays), 1)
        st.error(
            f"**This design cannot answer the question.** It only resolves "
            f"to {_pct(detect, 0)}, but breakeven sits at {_pct(need, 0)} — "
            f"so a promo that lands anywhere between them reads as "
            f"inconclusive, and you will have spent the discount to learn "
            f"nothing. Reaching {_pct(need, 0)} at this depth needs about "
            f"**{wk:,.0f} weeks**."
            + ("" if wk <= 52 else
               " That is longer than a year — this cell is too small or too "
               "noisy to test on its own. Pool stores, widen the days, or "
               "test a deeper offer where the effect is larger."))

    # ------------------------------------------------ length sensitivity
    wks = np.arange(4, 105, 2)
    curve = [mde(sd, mu, (w / 2.0) * len(tdays), power) for w in wks]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=wks, y=np.array(curve) * 100, mode="lines",
        line=dict(color=accent or "#2E7D74", width=3),
        name="Smallest detectable lift",
        hovertemplate="%{x:.0f} weeks resolves %{y:,.0f}%<extra></extra>"))
    if np.isfinite(need):
        fig.add_hline(
            y=need * 100, line=dict(color="#B4472F", dash="dash", width=2),
            annotation_text=f"breakeven {_pct(need, 0)}",
            annotation_position="top right")
    fig.add_vline(x=weeks, line=dict(color="rgba(0,0,0,0.35)", dash="dot"))
    fig.update_layout(
        height=330, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(title="Test length (weeks, alternating on/off)",
                   gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Smallest lift detectable", ticksuffix="%",
                   gridcolor="rgba(0,0,0,0.07)"))
    st.plotly_chart(fig, use_container_width=True)

    _note(
        "Where the curve crosses the dashed line is the shortest honest "
        "test. Precision improves with the square root of days, so doubling "
        "the length buys only about 30% more resolution — which is why "
        "widening the scope or deepening the offer beats simply waiting. "
        "<b>One caveat this chart cannot show:</b> an alternating design "
        "assumes promoted and control days differ only in the promo. "
        "Seasonality, a takeover running concurrently, or customers learning "
        "the pattern and shifting purchases into promoted days will all "
        "break that assumption, and the last one biases the result upward.")
