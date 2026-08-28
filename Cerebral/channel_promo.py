"""
channel_promo.py -- store x channel x day-of-week promo simulator.

Promo Lab's Brand Discount ROI tab answers "should we discount this brand".
This one answers a different question with a different failure mode:

    "20% off Delivery at Fifth Ave, Saturday and Sunday" -- does it pay?

WHY THE BRAND MODEL DOES NOT TRANSFER

The brand simulator's most important input is REACH: what share of existing
sales get marked down. A targeted offer has low reach and survivable maths.
A channel promo has reach = 1.0 by construction -- you cannot offer 20% off
delivery to only some delivery customers.

And a term appears that brand promos do not have: SWITCHING. In-store
customers who were coming anyway place a delivery order instead. Those
baskets arrive discounted. Because the promoted channel is the small one,
the switching risk is leveraged by the ratio of the two channels' size.

THE MODEL

In scope = one store, a chosen set of days, split into the promoted
channel(s) and everything else at that store on those days.

    N_p, m_p   net sales and margin rate in the promoted channel
    N_a, m_a   net sales and margin rate in the at-risk channel(s)
    d          discount depth
    sigma      share of N_a that switches into the promoted channel
    k          basket-size ratio, switched basket vs the one it replaced.
               MEASURED from dash_channel_pair, not assumed.
    L          genuinely incremental volume, as a share of N_p

    d margin = -N_p*d + L*N_p*(m_p - d) + sigma*N_a*[k*(m_p - d) - m_a]
    d net    = -N_p*d + L*N_p*(1 - d)   + sigma*N_a*[k*(1 - d) - 1]
    spend    = d * (N_p + L*N_p + sigma*N_a*k)

    BREAKEVEN LIFT
    L* = d/(m_p-d)  +  (N_a/N_p) * sigma * (m_a - k*(m_p-d)) / (m_p-d)

    SWITCH-NEUTRAL DEPTH
    d0 = m_p - m_a/k

Below d0 a switched basket earns MORE than the one it replaced, because the
bigger basket more than covers the discount, and switching is accretive.
Above it you start paying for traffic you already had. At the chain's
measured k of about 1.20 and roughly 49% margins, d0 lands near 9% -- which
is the single most useful number on the page, because it says the depth
matters far more than the targeting.

If d >= m_p the channel sells below cost and no volume rescues it.

DATA

  dash_channel_dow    baseline. store x channel x weekday.
  dash_channel_pair   within-customer basket-size ratio -> k.
  dash_channel_stick  channel adoption retention.

All three come from publish_channel.py, which must run AFTER publish.py.

Wire into render_promo_lab() as a fifth sub-tab:

    from channel_promo import render_channel_promo
    ...
    with tab5:
        render_channel_promo(q=q, keys=keys, stores=STORES,
                             heading=None, table_exists=table_exists,
                             accent=ACCENT, series=SERIES)
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
WEEKDAYS = [1, 2, 3, 4, 5]

DEPTH_MIN, DEPTH_MAX = 5, 30

# Below this many paired customers a per-store k is noise, and the tab
# falls back to the chain-wide figure rather than pretending to precision.
MIN_PAIR_CUSTOMERS = 150


# --------------------------------------------------------------- helpers

def _money(x) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return ("-" if x < 0 else "") + f"${abs(x):,.0f}"


def _signed(x) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return ("+" if x >= 0 else "-") + f"${abs(x):,.0f}"


def _pct(x, places: int = 1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    if np.isinf(x):
        return "never"
    return f"{x * 100:.{places}f}%"


def _note(html: str) -> None:
    st.markdown(f'<p class="note">{html}</p>', unsafe_allow_html=True)


# ------------------------------------------------------------ data loads

def _load_dow(q) -> pd.DataFrame:
    df = q("SELECT * FROM dash_channel_dow")
    if df.empty:
        return df
    for c in ("baskets", "net", "gm", "units", "discount", "days"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def _load_k(q, store: int, promoted: list[str],
            at_risk: list[str]) -> tuple[float, int, bool]:
    """Measured basket-size ratio for at-risk -> promoted.

    Returns (k, customers behind it, whether it fell back to chain-wide).
    Weighted by customer count across every relevant channel pair.
    """
    try:
        pair = q("SELECT * FROM dash_channel_pair")
    except Exception:
        return 1.0, 0, False
    if pair.empty:
        return 1.0, 0, False

    pair["customers"] = pd.to_numeric(pair["customers"], errors="coerce")
    pair["median_ratio"] = pd.to_numeric(pair["median_ratio"], errors="coerce")

    def blend(scope: pd.DataFrame) -> tuple[float, int]:
        sel = scope[scope["ch_from"].isin(at_risk)
                    & scope["ch_to"].isin(promoted)].dropna(
                        subset=["median_ratio", "customers"])
        n = float(sel["customers"].sum())
        if n <= 0:
            return np.nan, 0
        k = float((sel["median_ratio"] * sel["customers"]).sum() / n)
        return k, int(n)

    k, n = blend(pair[pair["store_key"] == store])
    if n >= MIN_PAIR_CUSTOMERS and not np.isnan(k):
        return k, n, False

    k0, n0 = blend(pair[pair["store_key"] == 0])
    if n0 > 0 and not np.isnan(k0):
        return k0, n0, True
    if not np.isnan(k):
        return k, n, False
    return 1.0, 0, False


def _load_stick(q, store: int) -> pd.DataFrame:
    try:
        df = q("SELECT * FROM dash_channel_stick")
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    sub = df[df["store_key"] == store]
    return sub if not sub.empty else df[df["store_key"] == 0]


# ------------------------------------------------------------ the model

def simulate(net_p: float, m_p: float, net_a: float, m_a: float,
             depth: float, sigma: float, k: float, lift: float) -> dict:
    """Full before/after picture. See the module docstring."""
    d, s, L = depth, sigma, lift

    base_margin = net_p * m_p + net_a * m_a
    base_net = net_p + net_a

    d_margin = (-net_p * d
                + L * net_p * (m_p - d)
                + s * net_a * (k * (m_p - d) - m_a))
    d_net = (-net_p * d
             + L * net_p * (1 - d)
             + s * net_a * (k * (1 - d) - 1))
    spend = d * (net_p + L * net_p + s * net_a * k)

    if m_p > d:
        breakeven = (d / (m_p - d)
                     + (net_a / net_p) * s * (m_a - k * (m_p - d)) / (m_p - d)
                     if net_p > 0 else np.inf)
    else:
        breakeven = np.inf

    # Depth at which a switched basket exactly replaces the margin of the
    # one it displaced. Negative means switching never pays at this k.
    d_neutral = m_p - (m_a / k) if k > 0 else np.nan

    return {
        "base_net": base_net,
        "base_margin": base_margin,
        "new_net": base_net + d_net,
        "new_margin": base_margin + d_margin,
        "d_net": d_net,
        "d_margin": d_margin,
        "spend": spend,
        "breakeven": max(breakeven, 0.0) if np.isfinite(breakeven)
                     else breakeven,
        "d_neutral": d_neutral,
        "below_cost": d >= m_p,
    }


# ------------------------------------------------------------- rendering

def _trough_chart(dow: pd.DataFrame, stores: dict, store: int,
                  scope_days: list[int], accent: str | None) -> go.Figure:
    """Every store's weekday shape, with the promoted days marked.

    The point of this chart is to answer a question that comes before any
    of the maths: are you filling a trough or paying for a peak?
    """
    fig = go.Figure()
    palette = ["#8AA6A3", "#B4472F", "#8AA6A3", "#8AA6A3"]
    for i, sk in enumerate(sorted(dow["store_key"].unique())):
        if sk == 0:
            continue
        s = dow[dow["store_key"] == sk]
        daily = (s.groupby("dow")
                  .apply(lambda g: g["net"].sum() / max(g["days"].max(), 1))
                  .reindex(DOW_ORDER))
        idx = daily / daily.mean() * 100
        focus = (sk == store)
        fig.add_trace(go.Scatter(
            x=[DOW_SHORT[d] for d in DOW_ORDER], y=idx.values,
            mode="lines+markers", name=stores.get(sk, str(sk)),
            line=dict(width=3.5 if focus else 1.5,
                      color=(accent or "#B4472F") if focus
                            else "rgba(0,0,0,0.22)"),
            marker=dict(size=7 if focus else 4),
            hovertemplate="%{fullData.name} %{x}: %{y:.0f}<extra></extra>"))

    for d in scope_days:
        fig.add_vrect(x0=DOW_SHORT[d], x1=DOW_SHORT[d],
                      fillcolor="rgba(180,71,47,0.10)", line_width=14,
                      layer="below")

    fig.add_hline(y=100, line=dict(color="rgba(0,0,0,0.3)", dash="dot"))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.16, x=0),
        yaxis=dict(title="Index vs store average",
                   gridcolor="rgba(0,0,0,0.07)"),
        xaxis=dict(gridcolor="rgba(0,0,0,0)"))
    return fig


def _breakeven_curve(m_p, m_a, ratio, sigma, k, depth, lift,
                     accent: str | None) -> go.Figure:
    depths = np.arange(DEPTH_MIN, DEPTH_MAX + 0.5, 0.5) / 100.0
    req = np.where(
        depths < m_p,
        depths / (m_p - depths)
        + ratio * sigma * (m_a - k * (m_p - depths)) / (m_p - depths),
        np.nan)
    req = np.where(req < 0, 0, req)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=depths * 100, y=req * 100, mode="lines",
        line=dict(color=accent or "#2E7D74", width=3),
        name="Volume needed to break even",
        hovertemplate="%{x:.0f}% off needs %{y:,.0f}% more<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=[depth * 100], y=[lift * 100], mode="markers",
        marker=dict(color="#B4472F", size=13, symbol="diamond"),
        name="What you are assuming",
        hovertemplate="Your plan: %{x:.0f}% off, "
                      "%{y:,.0f}% more<extra></extra>"))
    fig.update_layout(
        height=330, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.15, x=0),
        xaxis=dict(title="Discount depth", ticksuffix="%",
                   gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Extra volume needed", ticksuffix="%",
                   gridcolor="rgba(0,0,0,0.07)"))
    return fig


def render_channel_promo(q, keys, stores, heading=None, table_exists=None,
                         accent=None, series=None) -> None:
    title = "Channel & day promos: does the scope pay for itself?"
    if heading:
        try:
            heading(title)
        except TypeError:
            st.markdown(f"#### {title}")
    else:
        st.markdown(f"#### {title}")

    if table_exists and not table_exists("dash_channel_dow"):
        st.info("dash_channel_dow is not in the published database. Run "
                "publish.py, then publish_channel.py, then reload.")
        return

    dow = _load_dow(q)
    if dow.empty:
        st.info("No channel data published.")
        return

    window = int(pd.to_numeric(dow["window_days"], errors="coerce").max() or 0)

    # ------------------------------------------------------------ inputs
    real = sorted(k for k in dow["store_key"].unique() if k != 0)
    default_store = next((k for k in keys if k in real), real[0])

    c1, c2 = st.columns([1, 2])
    with c1:
        store = st.selectbox(
            "Store", real, index=real.index(default_store),
            format_func=lambda k: stores.get(k, str(k)),
            key="chp_store",
            help="A channel promo is scoped to one store. The chart below "
                 "still shows all four for comparison.")
    sdf = dow[dow["store_key"] == store]
    channels = (sdf.groupby("channel")["net"].sum()
                   .sort_values(ascending=False).index.tolist())
    with c2:
        promoted = st.multiselect(
            "Channel getting the discount", channels,
            default=[c for c in channels if c != "In-Store"][:1],
            key="chp_channels")

    if not promoted:
        st.info("Pick at least one channel to discount.")
        return
    at_risk = [c for c in channels if c not in promoted]
    if not at_risk:
        st.warning("Every channel is discounted, so there is nothing left to "
                   "switch from. This is a store-wide markdown, not a "
                   "channel promo.")

    d1, d2 = st.columns([1, 2])
    with d1:
        preset = st.radio(
            "Days", ["Weekend (Sat–Sun)", "Weekdays (Mon–Fri)",
                     "All days", "Custom"],
            key="chp_preset")
    if preset == "Custom":
        with d2:
            scope_days = st.multiselect(
                "Which days", DOW_ORDER, default=WEEKEND,
                format_func=lambda d: DOW_NAME[d], key="chp_days")
    else:
        scope_days = {"Weekend (Sat–Sun)": WEEKEND,
                      "Weekdays (Mon–Fri)": WEEKDAYS,
                      "All days": DOW_ORDER}[preset]
    if not scope_days:
        st.info("Pick at least one day.")
        return

    # --------------------------------------------------- trough or peak
    st.plotly_chart(_trough_chart(dow, stores, store, scope_days, accent),
                    use_container_width=True)
    _note(
        f"Average net per day by weekday over the last {window} days, each "
        f"store indexed to its own average. <b>Ask this before the maths:</b> "
        f"are the shaded days a trough you are trying to fill, or a peak you "
        f"are about to pay for? Discounting a day that already runs above "
        f"100 buys demand you were getting free.")

    # -------------------------------------------------------- the scope
    scope = sdf[sdf["dow"].isin(scope_days)]
    p = scope[scope["channel"].isin(promoted)]
    a = scope[~scope["channel"].isin(promoted)]

    net_p, gm_p, bask_p = p["net"].sum(), p["gm"].sum(), p["baskets"].sum()
    net_a, gm_a = a["net"].sum(), a["gm"].sum()
    if net_p <= 0:
        st.warning("No sales in that channel on those days.")
        return

    m_p = gm_p / net_p
    m_a = (gm_a / net_a) if net_a > 0 else m_p
    ratio = (net_a / net_p) if net_p > 0 else 0.0
    scope_day_count = float(p.groupby("dow")["days"].max().sum())
    aov_p = net_p / bask_p if bask_p else np.nan

    k, k_n, k_fallback = _load_k(q, store, promoted, at_risk or promoted)

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Promoted channel net", _money(net_p),
              help=f"{bask_p:,.0f} baskets over {scope_day_count:,.0f} days.")
    s2.metric("At-risk channel net", _money(net_a),
              help="Same store, same days, everything not discounted.")
    s3.metric("Size ratio", f"{ratio:,.1f}×",
              help="At-risk net divided by promoted net. This multiplies "
                   "the switching cost.")
    s4.metric("Basket-size ratio k", f"{k:,.2f}",
              help=f"Measured within-customer across {k_n:,} people"
                   + (" chain-wide." if k_fallback else " at this store."))

    if k_fallback:
        _note(
            f"<b>k is the chain-wide figure.</b> Fewer than "
            f"{MIN_PAIR_CUSTOMERS} customers at {stores.get(store, store)} "
            f"have enough baskets in both channels to measure it locally, so "
            f"a store-specific number would be noise.")

    # ------------------------------------------------------ the sliders
    x1, x2, x3 = st.columns(3)
    with x1:
        depth = st.slider("Discount depth", DEPTH_MIN, DEPTH_MAX, 20, 1,
                          format="%d%%", key="chp_depth") / 100.0
    with x2:
        sigma = st.slider(
            "Switching", 0, 25, 5, 1, format="%d%%", key="chp_sigma",
            help="Share of at-risk sales that move into the discounted "
                 "channel. These customers were coming anyway; they now "
                 "arrive cheaper.") / 100.0
    with x3:
        lift = st.slider("Genuinely new volume", 0, 250, 50, 5,
                         format="+%d%%", key="chp_lift") / 100.0

    sim = simulate(net_p, m_p, net_a, m_a, depth, sigma, k, lift)

    # --------------------------------------------------------- verdict
    st.markdown("### The number that decides it")

    if sim["below_cost"]:
        st.error(
            f"At {_pct(depth, 0)} off, this channel sells below cost — its "
            f"margin is {_pct(m_p)}. No volume fixes that.")
    else:
        be = sim["breakeven"]
        gap = lift - be
        (st.success if gap >= 0 else st.warning)(
            f"**Needs {_pct(be, 0)} more volume to break even** at "
            f"{_pct(depth, 0)} off with {_pct(sigma, 0)} switching. "
            f"You are assuming {_pct(lift, 0)}, which is {_pct(abs(gap), 0)} "
            f"{'above' if gap >= 0 else 'short of'} the line.")

        extra_baskets = be * bask_p
        per_day = extra_baskets / scope_day_count if scope_day_count else np.nan
        now_day = bask_p / scope_day_count if scope_day_count else np.nan
        if np.isfinite(per_day):
            _note(
                f"In orders rather than percentages: from about "
                f"<b>{now_day:,.0f}</b> to <b>{now_day + per_day:,.0f}</b> "
                f"per promoted day, at roughly {_money(aov_p)} each. That is "
                f"the operational question — can the channel physically take "
                f"that many more orders on those days?")

    dn = sim["d_neutral"]
    if np.isfinite(dn) and dn > 0:
        st.info(
            f"**Switching is free below {_pct(dn, 1)} off.** At a measured "
            f"k of {k:,.2f}, a switched basket is big enough that below that "
            f"depth it earns more than the one it replaced. Above it, every "
            f"customer you pull across costs you. Depth is doing more work "
            f"here than targeting is.")
    else:
        st.info(
            f"**Switching costs money at any depth.** The measured basket-"
            f"size ratio of {k:,.2f} is not enough to cover a discount, so "
            f"every customer pulled across is a loss. Keep the offer off "
            f"in-store signage.")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Margin change", _signed(sim["d_margin"]))
    m2.metric("Net sales change", _signed(sim["d_net"]))
    m3.metric("Discount spend", _money(sim["spend"]))
    m4.metric("Margin rate after",
              "—" if sim["new_net"] <= 0
              else _pct(sim["new_margin"] / sim["new_net"]))

    _note(
        "Figures cover the whole store on the promoted days, not just the "
        "discounted channel — a switched basket leaves one column and "
        "arrives in another, so scoring the promoted channel alone would "
        "count cannibalised sales as a win.")

    # ------------------------------------------------------------- curve
    st.markdown("### How the bar moves with depth")
    st.plotly_chart(
        _breakeven_curve(m_p, m_a, ratio, sigma, k, depth, lift, accent),
        use_container_width=True)

    # -------------------------------------------------- does it stick
    stick = _load_stick(q, store)
    if not stick.empty:
        sel = stick[stick["channel"].isin(promoted)]
        if not sel.empty:
            st.markdown("### Does the channel stick?")
            cols = st.columns(len(sel))
            for col, (_, r) in zip(cols, sel.iterrows()):
                col.metric(
                    f"{r['channel']} — came back",
                    _pct(float(r["repeat_rate"])),
                    help=f"{float(r['first_timers']):,.0f} first-time users; "
                         f"{float(r['avg_later_baskets']):,.2f} later baskets "
                         f"on average.")
            _note(
                "Of customers whose <i>first</i> basket in this channel "
                "landed in the lookback, the share who used it again. This "
                "is the real case for a channel promo: you are not buying a "
                "weekend, you are buying channel adoption, and only the part "
                "that persists was worth paying for. Note this is a raw "
                "repeat rate with no control group — some of these people "
                "would have adopted anyway.")

    # ------------------------------------------- what they already get
    st.markdown("### What these days already cost you")
    anchor = (scope.groupby("channel")
                   .agg(baskets=("baskets", "sum"), net=("net", "sum"),
                        discount=("discount", "sum")))
    anchor["rate"] = anchor["discount"] / (anchor["net"] + anchor["discount"])
    show = pd.DataFrame({
        "Channel": anchor.index,
        "Baskets": [f"{v:,.0f}" for v in anchor["baskets"]],
        "Net": [_money(v) for v in anchor["net"]],
        "Discount given": [_money(v) for v in anchor["discount"]],
        "Rate": [_pct(v, 2) for v in anchor["rate"]],
    })
    st.dataframe(show, hide_index=True, use_container_width=True)
    _note(
        "Discount is recorded against the whole basket, so this is a basket-"
        "level rate and it is honest at channel grain — unlike the brand "
        "view, nothing is being smeared across categories here.")
