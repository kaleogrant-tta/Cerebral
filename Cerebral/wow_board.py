"""
wow_board.py -- the Week-over-Week view on the Charts tab.

Compares ONE week with the week before it: the latest complete week by
default (the week the Monday call is about), and nothing wider. Everything
on the Charts tab proper still follows the "Weeks shown" slider; this board
ignores the slider on purpose and only follows the store filter.

Tables used: dash_basket_week (per store x channel), dash_category_week
(per store x channel x category), dash_brand_week (per store x brand).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _wk_id(y, w) -> int:
    return int(y) * 100 + int(w)


def _label(y: int, w: int) -> str:
    start = pd.Timestamp.fromisocalendar(int(y), int(w), 1)
    end = start + pd.Timedelta(days=6)
    return f"W{int(w):02d} · {start:%b %d} – {end:%b %d}"


def _pct(cur, prev):
    cur = pd.to_numeric(cur, errors="coerce").astype(float)
    prev = pd.to_numeric(prev, errors="coerce").astype(float)
    return np.where(prev > 0, (cur / prev.replace(0, np.nan) - 1) * 100, np.nan)


def _pct1(cur, prev):
    try:
        return (float(cur) / float(prev) - 1) * 100 if prev else None
    except (TypeError, ValueError):
        return None


def _money_delta(d) -> str:
    return f"{'+' if d >= 0 else '−'}${abs(d):,.0f}"


def render(q, keys: list[int], stores: dict, weeks: pd.DataFrame,
           partial_week: bool, charts, accent: str, bad: str, muted: str,
           cat_color, tip) -> None:
    store_sql = ("" if len(keys) == len(stores)
                 else f" AND store_key IN ({','.join(map(str, keys))})")

    # ---------------------------------------------------------- week picker
    wk_all = weeks.sort_values(["iso_year", "iso_week"]).reset_index(drop=True)
    if len(wk_all) < 2:
        st.info("Week-over-week needs at least two weeks of data.")
        return
    # Default to the latest COMPLETE week: a week still in progress compared
    # against a full one reads as a collapse.
    default_pos = len(wk_all) - (2 if partial_week else 1)
    default_pos = max(1, default_pos)
    choices = list(range(len(wk_all) - 1, 0, -1))[:12]      # last 12 weeks
    labels = {i: _label(wk_all.iloc[i].iso_year, wk_all.iloc[i].iso_week)
              for i in choices}

    pc1, pc2 = st.columns([2, 3])
    with pc1:
        pos = st.selectbox(
            "Week", choices, index=choices.index(default_pos)
            if default_pos in choices else 0,
            format_func=lambda i: labels[i], key="wow_week",
            help="The week being reported on. It is compared with the week "
                 "immediately before it. Defaults to the latest complete week.")
    cur_y, cur_w = int(wk_all.iloc[pos].iso_year), int(wk_all.iloc[pos].iso_week)
    prv_y, prv_w = int(wk_all.iloc[pos - 1].iso_year), int(wk_all.iloc[pos - 1].iso_week)
    cur_id, prv_id = _wk_id(cur_y, cur_w), _wk_id(prv_y, prv_w)
    is_latest_partial = partial_week and pos == len(wk_all) - 1
    with pc2:
        st.markdown(
            f'<p class="note" style="margin-top:1.9rem"><b>{labels[pos]}</b> '
            f'compared with <b>{_label(prv_y, prv_w)}</b>'
            + (" — the week you picked is still in progress, so its totals "
               "are not a fair comparison." if is_latest_partial else "")
            + "</p>", unsafe_allow_html=True)

    # ------------------------------------------------------------- queries
    bw = q(f"""
        SELECT store_key, channel, iso_year * 100 + iso_week AS wk,
               SUM(baskets) AS baskets, SUM(net) AS net,
               SUM(redeem_baskets) AS redeem_baskets,
               SUM(redeem_value) AS redeem_value, MAX(days_open) AS days_open
        FROM dash_basket_week
        WHERE (iso_year * 100 + iso_week) IN ({cur_id},{prv_id}) {store_sql}
        GROUP BY 1,2,3
    """)
    cw = q(f"""
        SELECT store_key, category, iso_year * 100 + iso_week AS wk,
               SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
               SUM(baskets_with) AS baskets_with,
               MAX(skus_out) AS skus_out, MAX(skus_in_stock) AS skus_in_stock
        FROM dash_category_week
        WHERE (iso_year * 100 + iso_week) IN ({cur_id},{prv_id}) {store_sql}
        GROUP BY 1,2,3
    """)
    try:
        brw = q(f"""
            SELECT brand, iso_year * 100 + iso_week AS wk,
                   SUM(net) AS net, SUM(units) AS units
            FROM dash_brand_week
            WHERE (iso_year * 100 + iso_week) IN ({cur_id},{prv_id}) {store_sql}
            GROUP BY 1,2
        """)
    except Exception:                        # table missing on an old build
        brw = pd.DataFrame(columns=["brand", "wk", "net", "units"])

    if bw.empty or bw[bw.wk == cur_id].empty:
        st.warning("No baskets for the selected week and stores.")
        return

    def two(frame: pd.DataFrame, by: list[str], cols: list[str]) -> pd.DataFrame:
        """Pivot cur/prev side by side on `by`, columns cols -> cols_cur/_prev."""
        g = frame.groupby(by + ["wk"])[cols].sum().reset_index()
        c = g[g.wk == cur_id].drop(columns="wk").set_index(by)
        p = g[g.wk == prv_id].drop(columns="wk").set_index(by)
        out = c.join(p, lsuffix="_cur", rsuffix="_prev", how="outer").fillna(0)
        return out.reset_index()

    # ------------------------------------------------------------ headline
    cur_b, prv_b = bw[bw.wk == cur_id], bw[bw.wk == prv_id]
    net_c, net_p = cur_b.net.sum(), prv_b.net.sum()
    bk_c, bk_p = cur_b.baskets.sum(), prv_b.baskets.sum()
    atv_c = net_c / bk_c if bk_c else np.nan
    atv_p = net_p / bk_p if bk_p else np.nan
    cur_c, prv_c = cw[cw.wk == cur_id], cw[cw.wk == prv_id]
    un_c, un_p = cur_c.units.sum(), prv_c.units.sum()
    gm_c = cur_c.gm.sum() / cur_c.net.sum() * 100 if cur_c.net.sum() else np.nan
    gm_p = prv_c.gm.sum() / prv_c.net.sum() * 100 if prv_c.net.sum() else np.nan
    rd_c, rd_p = cur_b.redeem_value.sum(), prv_b.redeem_value.sum()

    def m(col, label, cur, prev, fmt, pct=True, help_=None, inverse=False):
        d = _pct1(cur, prev) if pct else (cur - prev if prev == prev else None)
        if d is None or d != d:
            col.metric(label, fmt(cur), help=help_)
        elif pct:
            col.metric(label, fmt(cur), f"{d:+.1f}% WoW",
                       delta_color="inverse" if inverse else "normal", help=help_)
        else:
            col.metric(label, fmt(cur), f"{d:+.1f} pp WoW", help=help_)

    c = st.columns(6)
    m(c[0], "Net sales", net_c, net_p, lambda v: f"${v:,.0f}", help_=tip("net sales"))
    m(c[1], "Baskets", bk_c, bk_p, lambda v: f"{int(v):,}", help_=tip("basket"))
    m(c[2], "Average basket", atv_c, atv_p, lambda v: f"${v:,.2f}",
      help_=tip("average basket"))
    m(c[3], "Units", un_c, un_p, lambda v: f"{int(v):,}")
    m(c[4], "Gross margin", gm_c, gm_p, lambda v: f"{v:,.1f}%", pct=False)
    m(c[5], "Loyalty redeemed", rd_c, rd_p, lambda v: f"${v:,.0f}",
      help_="Points value redeemed at the till this week versus last. A rise "
            "usually means a bigger discount load, so it is not automatically "
            "good news.")

    # --------------------------------------------------------- the readout
    cat2 = two(cw, ["category"], ["net", "units", "baskets_with"])
    cat2["d_net"] = cat2.net_cur - cat2.net_prev
    cat2["d_pct"] = _pct(cat2.net_cur, cat2.net_prev)
    cat2 = cat2.sort_values("net_cur", ascending=False)
    up = cat2.sort_values("d_net", ascending=False).iloc[0] if len(cat2) else None
    dn = cat2.sort_values("d_net").iloc[0] if len(cat2) else None
    st2 = two(bw, ["store_key"], ["net", "baskets"])
    st2["store"] = st2.store_key.map(stores)
    st2["d_net"] = st2.net_cur - st2.net_prev
    st2["d_pct"] = _pct(st2.net_cur, st2.net_prev)

    d_net = _pct1(net_c, net_p)
    d_bk = _pct1(bk_c, bk_p)
    d_atv = _pct1(atv_c, atv_p)
    parts = []
    if d_net is not None:
        parts.append(f"Net sales came in at <b>${net_c:,.0f}</b>, "
                     f"<b>{d_net:+.1f}%</b> ({_money_delta(net_c - net_p)}) "
                     f"against the week before.")
    if d_bk is not None and d_atv is not None:
        if abs(d_bk) >= abs(d_atv):
            parts.append(f"That was mostly traffic: baskets {d_bk:+.1f}% "
                         f"while the average basket moved {d_atv:+.1f}%.")
        else:
            parts.append(f"That was mostly basket size: the average basket "
                         f"moved {d_atv:+.1f}% while baskets went {d_bk:+.1f}%.")
    if up is not None and dn is not None and up.category != dn.category:
        parts.append(f"Biggest gain was <b>{up.category}</b> "
                     f"({_money_delta(up.d_net)}); biggest drop was "
                     f"<b>{dn.category}</b> ({_money_delta(dn.d_net)}).")
    if len(st2) > 1:
        best = st2.sort_values("d_pct", ascending=False).iloc[0]
        worst = st2.sort_values("d_pct").iloc[0]
        if best.store != worst.store and pd.notna(best.d_pct) and pd.notna(worst.d_pct):
            parts.append(f"By store, <b>{best.store}</b> led ({best.d_pct:+.1f}%) "
                         f"and <b>{worst.store}</b> trailed ({worst.d_pct:+.1f}%).")
    if parts:
        st.markdown('<div class="howto"><b>In one breath.</b> '
                    + " ".join(parts) + "</div>", unsafe_allow_html=True)

    st.divider()

    # ------------------------------------------------------- store + chart
    L, R = st.columns([3, 2])
    with L:
        st.markdown("##### By store")
        if len(st2) > 1:
            st2["atv_cur"] = st2.net_cur / st2.baskets_cur.replace(0, np.nan)
            st2["atv_prev"] = st2.net_prev / st2.baskets_prev.replace(0, np.nan)
            st2["d_bk"] = _pct(st2.baskets_cur, st2.baskets_prev)
            st2["d_atv"] = _pct(st2.atv_cur, st2.atv_prev)
            show = st2.sort_values("net_cur", ascending=False)[
                ["store", "net_cur", "net_prev", "d_net", "d_pct", "d_bk", "d_atv"]]
            st.dataframe(
                show, hide_index=True, width="stretch",
                column_config={
                    "store": "Store",
                    "net_cur": st.column_config.NumberColumn("This week", format="$%d"),
                    "net_prev": st.column_config.NumberColumn("Last week", format="$%d"),
                    "d_net": st.column_config.NumberColumn("Δ $", format="$%d"),
                    "d_pct": st.column_config.NumberColumn("Δ net %", format="%+.1f%%"),
                    "d_bk": st.column_config.NumberColumn("Δ baskets %", format="%+.1f%%"),
                    "d_atv": st.column_config.NumberColumn("Δ avg basket %", format="%+.1f%%"),
                })
        else:
            st.markdown('<p class="note">One store selected — the headline '
                        'cards above are that store.</p>', unsafe_allow_html=True)

        st.markdown("##### By channel")
        ch2 = two(bw, ["channel"], ["net", "baskets"])
        ch2["d_pct"] = _pct(ch2.net_cur, ch2.net_prev)
        ch2["d_bk"] = _pct(ch2.baskets_cur, ch2.baskets_prev)
        ch2["share"] = ch2.net_cur / ch2.net_cur.sum() * 100 if ch2.net_cur.sum() else np.nan
        st.dataframe(
            ch2.sort_values("net_cur", ascending=False)[
                ["channel", "net_cur", "net_prev", "d_pct", "baskets_cur", "d_bk", "share"]],
            hide_index=True, width="stretch",
            column_config={
                "channel": "Channel",
                "net_cur": st.column_config.NumberColumn("This week", format="$%d"),
                "net_prev": st.column_config.NumberColumn("Last week", format="$%d"),
                "d_pct": st.column_config.NumberColumn("Δ net %", format="%+.1f%%"),
                "baskets_cur": st.column_config.NumberColumn("Baskets", format="%d"),
                "d_bk": st.column_config.NumberColumn("Δ baskets %", format="%+.1f%%"),
                "share": st.column_config.NumberColumn("Share of net", format="%.1f%%"),
            })

    with R:
        st.markdown("##### Where the change came from")
        st.markdown('<p class="note">Each bar is a category\'s net sales change '
                    'versus last week, in dollars. They add up to the headline '
                    'change.</p>', unsafe_allow_html=True)
        bars = cat2.sort_values("d_net")
        fig = go.Figure(go.Bar(
            x=bars.d_net, y=bars.category, orientation="h",
            marker_color=[accent if v >= 0 else bad for v in bars.d_net],
            text=[_money_delta(v) for v in bars.d_net], textposition="outside",
            hovertemplate="%{y}<br>%{x:$+,.0f} vs last week<extra></extra>"))
        fig.update_layout(xaxis=dict(title="Δ net $", tickformat="$~s",
                                     zeroline=True, zerolinecolor=muted),
                          yaxis=dict(title=""), showlegend=False,
                          margin=dict(l=10, r=60, t=10, b=30))
        charts.show(fig, key="wow_cat_bridge", kind="bar",
                    height=max(260, 28 * len(bars) + 60))

    st.divider()

    # ------------------------------------------------------------ category
    st.markdown("##### By category")
    bk_tot_c = bk_c if bk_c else np.nan
    bk_tot_p = bk_p if bk_p else np.nan
    cat2["pen_cur"] = cat2.baskets_with_cur / bk_tot_c * 100
    cat2["pen_prev"] = cat2.baskets_with_prev / bk_tot_p * 100
    cat2["d_pen"] = cat2.pen_cur - cat2.pen_prev
    cat2["d_units"] = _pct(cat2.units_cur, cat2.units_prev)
    stock = (cw[cw.wk == cur_id].groupby("category")
               .agg(skus_out=("skus_out", "sum"))
               .reset_index())
    cat2 = cat2.merge(stock, on="category", how="left")
    has_stock = cat2.skus_out.notna().any()
    cols = ["category", "net_cur", "net_prev", "d_net", "d_pct", "d_units",
            "pen_cur", "d_pen"] + (["skus_out"] if has_stock else [])
    st.dataframe(
        cat2[cols], hide_index=True, width="stretch",
        column_config={
            "category": "Category",
            "net_cur": st.column_config.NumberColumn("This week", format="$%d"),
            "net_prev": st.column_config.NumberColumn("Last week", format="$%d"),
            "d_net": st.column_config.NumberColumn("Δ $", format="$%d"),
            "d_pct": st.column_config.NumberColumn("Δ net %", format="%+.1f%%"),
            "d_units": st.column_config.NumberColumn("Δ units %", format="%+.1f%%"),
            "pen_cur": st.column_config.NumberColumn(
                "Penetration", format="%.1f%%",
                help="Share of baskets this week holding the category. "
                     + (tip("penetration") or "")),
            "d_pen": st.column_config.NumberColumn("Δ pen (pp)", format="%+.1f"),
            "skus_out": st.column_config.NumberColumn(
                "SKUs out", format="%d",
                help="Products of this category that were out of stock on the "
                     "floor at some point this week (summed over the stores "
                     "selected). A drop with a high number here is a supply "
                     "problem, not a demand one."),
        })

    # -------------------------------------------------------------- brands
    if not brw.empty:
        st.divider()
        st.markdown("##### Brands that moved the most")
        b2 = two(brw, ["brand"], ["net", "units"])
        b2["d_net"] = b2.net_cur - b2.net_prev
        b2["d_pct"] = _pct(b2.net_cur, b2.net_prev)
        b2 = b2[(b2.net_cur + b2.net_prev) > 0]
        cfg = {
            "brand": "Brand",
            "net_cur": st.column_config.NumberColumn("This week", format="$%d"),
            "net_prev": st.column_config.NumberColumn("Last week", format="$%d"),
            "d_net": st.column_config.NumberColumn("Δ $", format="$%d"),
            "d_pct": st.column_config.NumberColumn("Δ %", format="%+.1f%%"),
        }
        bl, br = st.columns(2)
        keep = ["brand", "net_cur", "net_prev", "d_net", "d_pct"]
        with bl:
            st.caption("Up")
            st.dataframe(b2.sort_values("d_net", ascending=False).head(8)[keep],
                         hide_index=True, width="stretch", column_config=cfg)
        with br:
            st.caption("Down")
            st.dataframe(b2.sort_values("d_net").head(8)[keep],
                         hide_index=True, width="stretch", column_config=cfg)
        st.markdown('<p class="note">Ranked by dollar change, so a big brand '
                    'with a small swing can outrank a small brand that halved. '
                    'Brands with no sales in either week are left out.</p>',
                    unsafe_allow_html=True)
