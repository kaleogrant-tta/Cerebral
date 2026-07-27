"""
Cerebral — Category Analytics dashboard

    streamlit run cerebral_app.py

Three tabs:
  Charts    trends, mix, control charts, channel index
  Insights  alerts, scorecard, substitution, inventory efficiency
  Ops       upload and ingest new exports, load history, database health

Every metric is a rate (per 100 baskets, penetration) rather than a raw
total, so traffic swings do not read as category swings.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from tta_config import CATEGORY_ORDER, CONFIG_VERSION, STORES as STORE_MAP  # noqa: E402

DB = Path(__file__).parent / "tta.duckdb"
STORES = {v["store_key"]: v["code"] for v in STORE_MAP.values()}
NAMES = {v["store_key"]: v["name"] for v in STORE_MAP.values()}
BASELINE_WEEKS = 13

INK = "#101418"
MUTED = "#7A8590"
ACCENT = "#2F6F4F"
WARN = "#B4632B"
BAD = "#9E3B32"

st.set_page_config(page_title="Cerebral", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; max-width: 1500px;}
  h1, h2, h3 {letter-spacing: -0.02em;}
  [data-testid="stMetricValue"] {font-size: 1.55rem;}
  [data-testid="stMetricLabel"] {font-size: .74rem; text-transform: uppercase;
      letter-spacing: .07em; color: #7A8590;}
  .stTabs [data-baseweb="tab"] {padding: 0 1.1rem; font-weight: 500;}
  .alert {border-left: 3px solid; padding: .55rem .9rem; margin: .35rem 0;
      background: rgba(128,128,128,.06); font-size: .9rem;}
  .a-bad {border-color: #9E3B32;}
  .a-warn {border-color: #B4632B;}
  .a-ok {border-color: #2F6F4F;}
  .note {color: #7A8590; font-size: .82rem; line-height: 1.45;}
</style>
""", unsafe_allow_html=True)


# ===========================================================================
# Data access
# ===========================================================================

@st.cache_resource
def connect():
    if not DB.exists():
        return None
    return duckdb.connect(str(DB), read_only=True)


@st.cache_data(ttl=120)
def q(sql: str) -> pd.DataFrame:
    con = connect()
    if con is None:
        return pd.DataFrame()
    return con.execute(sql).df()


def store_filter(keys: list[int]) -> str:
    if not keys or len(keys) == len(STORES):
        return ""
    return f" AND store_key IN ({','.join(map(str, keys))})"


def has_data() -> bool:
    if not DB.exists():
        return False
    try:
        return q("SELECT COUNT(*) n FROM fact_line").n.iloc[0] > 0
    except Exception:
        return False


# ===========================================================================
# Shared queries
# ===========================================================================

def weekly(sf: str) -> pd.DataFrame:
    return q(f"""
        WITH bw AS (
            SELECT iso_year, iso_week, COUNT(*) baskets,
                   COUNT(DISTINCT date_key) AS days_open,
                   SUM(basket_net) net_all,
                   AVG(basket_lines) avg_lines
            FROM fact_basket WHERE NOT is_return {sf} GROUP BY 1,2
        ),
        cw AS (
            SELECT iso_year, iso_week, category,
                   SUM(net_sales) net, SUM(gross_margin) gm, SUM(units) units,
                   COUNT(DISTINCT basket_id) baskets_with
            FROM fact_line WHERE NOT is_return {sf} GROUP BY 1,2,3
        )
        SELECT cw.*, bw.baskets, bw.days_open, bw.net_all, bw.avg_lines,
               cw.baskets_with::DOUBLE / bw.baskets AS penetration,
               cw.net / bw.baskets * 100            AS per100,
               cw.gm / NULLIF(cw.net,0)             AS margin_pct,
               make_date(cw.iso_year, 1, 4) + (cw.iso_week - 1) * 7 AS wk_date
        FROM cw JOIN bw USING (iso_year, iso_week)
        ORDER BY 1,2,3
    """)


def cats_by_size(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    tot = df.groupby("category").net.sum().sort_values(ascending=False)
    return tot.index.tolist()


def limits(hist: pd.Series, n: float):
    if len(hist) < 4 or n <= 0:
        return None, None, None
    p = hist.mean()
    se = (p * (1 - p) / n) ** 0.5
    return p, p - 2 * se, p + 2 * se


def run_rule(s: pd.Series) -> str | None:
    if len(s) < 7:
        return None
    base = s.iloc[:-1].mean()
    last7 = s.tail(7)
    if (last7 > base).all():
        return "7 consecutive weeks above baseline"
    if (last7 < base).all():
        return "7 consecutive weeks below baseline"
    d = s.tail(6).diff().dropna()
    if len(d) >= 5 and (d > 0).all():
        return "6 consecutive weeks rising"
    if len(d) >= 5 and (d < 0).all():
        return "6 consecutive weeks falling"
    return None


def build_alerts(df: pd.DataFrame) -> list[dict]:
    """Control-limit breaches and run-rule signals for the latest week."""
    if df.empty:
        return []
    wks = (df[["iso_year", "iso_week"]].drop_duplicates()
             .sort_values(["iso_year", "iso_week"]).reset_index(drop=True))
    if len(wks) < 5:
        return []
    cur = wks.iloc[-1]
    hist = wks.iloc[max(0, len(wks) - 1 - BASELINE_WEEKS):len(wks) - 1]
    hkeys = set(zip(hist.iso_year, hist.iso_week))

    out = []
    latest = df[(df.iso_year == cur.iso_year) & (df.iso_week == cur.iso_week)]
    total_net = latest.net.sum()

    for _, r in latest.iterrows():
        # Skip categories too small for control limits to mean anything.
        if total_net and r.net / total_net < 0.01:
            continue
        h = df[(df.category == r.category)
               & df.apply(lambda x: (x.iso_year, x.iso_week) in hkeys, axis=1)]
        h = h.sort_values(["iso_year", "iso_week"])
        if len(h) < 4:
            continue
        base, lcl, ucl = limits(h.penetration, r.baskets)
        if base is not None:
            if r.penetration < lcl:
                out.append(dict(sev="bad", cat=r.category,
                                msg=f"penetration {r.penetration*100:.1f}% is below the "
                                    f"lower control limit {lcl*100:.1f}% "
                                    f"(13-week baseline {base*100:.1f}%)"))
            elif r.penetration > ucl:
                out.append(dict(sev="ok", cat=r.category,
                                msg=f"penetration {r.penetration*100:.1f}% is above the "
                                    f"upper control limit {ucl*100:.1f}% "
                                    f"(13-week baseline {base*100:.1f}%)"))
        rr = run_rule(pd.concat([h.penetration, pd.Series([r.penetration])]))
        if rr:
            sev = "bad" if "below" in rr or "falling" in rr else "ok"
            out.append(dict(sev=sev, cat=r.category, msg=f"{rr} on penetration"))
    order = {"bad": 0, "warn": 1, "ok": 2}
    return sorted(out, key=lambda x: order[x["sev"]])


# ===========================================================================
# Sidebar
# ===========================================================================

st.sidebar.markdown("### Cerebral")
st.sidebar.caption(f"config {CONFIG_VERSION}")

if not has_data():
    st.sidebar.warning("No data loaded")
    st.title("Cerebral")
    st.info("No database found. Open the **Ops** tab to load exports.")
    ops_only = True
else:
    ops_only = False

sel_keys: list[int] = list(STORES)
sf = ""
dfw = pd.DataFrame()

if not ops_only:
    picked = st.sidebar.multiselect(
        "Stores", options=list(STORES.values()),
        default=list(STORES.values()),
        help="Filters every chart and metric except Ops.")
    sel_keys = [k for k, v in STORES.items() if v in picked] or list(STORES)
    sf = store_filter(sel_keys)

    dfw = weekly(sf)
    if not dfw.empty:
        wk_opts = (dfw[["iso_year", "iso_week"]].drop_duplicates()
                     .sort_values(["iso_year", "iso_week"]))
        labels = [f"{int(y)}-W{int(w):02d}" for y, w in
                  zip(wk_opts.iso_year, wk_opts.iso_week)]
        n_weeks = st.sidebar.slider("Weeks shown", 4, max(len(labels), 4),
                                    min(26, len(labels)))
        keep = set(zip(wk_opts.iso_year.tail(n_weeks), wk_opts.iso_week.tail(n_weeks)))
        dfw = dfw[dfw.apply(lambda r: (r.iso_year, r.iso_week) in keep, axis=1)]

    st.sidebar.divider()
    rows = q(f"SELECT COUNT(*) n FROM fact_line WHERE TRUE {sf}").n.iloc[0]
    bask = q(f"SELECT COUNT(*) n FROM fact_basket WHERE TRUE {sf}").n.iloc[0]
    st.sidebar.caption(f"{rows:,} lines · {bask:,} baskets")


# ===========================================================================
# Layout
# ===========================================================================

st.title("Cerebral")
label = "All stores" if len(sel_keys) == len(STORES) else \
        ", ".join(NAMES[k] for k in sel_keys)
st.caption(f"Category analytics · The Travel Agency · {label}")

tab_charts, tab_insights, tab_ops = st.tabs(["Charts", "Insights", "Ops"])


# ---------------------------------------------------------------------------
# CHARTS
# ---------------------------------------------------------------------------
with tab_charts:
    if ops_only or dfw.empty:
        st.info("Load data in the Ops tab to see charts.")
    else:
        wk = dfw.groupby(["iso_year", "iso_week", "wk_date"]).agg(
            net=("net", "sum"), baskets=("baskets", "first")).reset_index()
        wk["atv"] = wk.net / wk.baskets
        cur, prev = wk.iloc[-1], (wk.iloc[-2] if len(wk) > 1 else None)

        c = st.columns(4)
        c[0].metric("Net sales, latest week", f"${cur.net:,.0f}",
                    f"{(cur.net/prev.net-1)*100:+.1f}%" if prev is not None else None)
        c[1].metric("Baskets", f"{int(cur.baskets):,}",
                    f"{(cur.baskets/prev.baskets-1)*100:+.1f}%" if prev is not None else None)
        c[2].metric("Average basket", f"${cur.atv:,.2f}",
                    f"{(cur.atv/prev.atv-1)*100:+.1f}%" if prev is not None else None)
        c[3].metric("Weeks in view", f"{len(wk)}")

        st.divider()

        left, right = st.columns([3, 2])

        with left:
            st.markdown("##### Net sales and basket count by week")
            fig = go.Figure()
            fig.add_bar(x=wk.wk_date, y=wk.net, name="Net sales",
                        marker_color=ACCENT, opacity=.75)
            fig.add_scatter(x=wk.wk_date, y=wk.baskets, name="Baskets",
                            yaxis="y2", line=dict(color=MUTED, width=2))
            fig.update_layout(
                height=330, margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(title="Net $"),
                yaxis2=dict(title="Baskets", overlaying="y", side="right",
                            showgrid=False),
                legend=dict(orientation="h", y=1.12, x=0),
                plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)
            st.markdown('<p class="note">Revenue and traffic together. When '
                        'they diverge, the gap is average basket — which is a '
                        'different problem from a footfall decline.</p>',
                        unsafe_allow_html=True)

        with right:
            st.markdown("##### Category share of revenue")
            mix = dfw.groupby("category").net.sum().reset_index()
            mix = mix.sort_values("net", ascending=False)
            fig = px.pie(mix, values="net", names="category", hole=.55,
                         color_discrete_sequence=px.colors.sequential.Greens_r)
            fig.update_traces(textposition="outside", textinfo="percent+label")
            fig.update_layout(height=330, margin=dict(l=0, r=0, t=10, b=0),
                              showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.markdown("##### Dollars per 100 baskets, by category")
        st.markdown('<p class="note">The primary trend metric. Normalising by '
                    'basket count separates category health from traffic.</p>',
                    unsafe_allow_html=True)

        top = cats_by_size(dfw)[:6]
        pick = st.multiselect("Categories", cats_by_size(dfw), default=top,
                              key="per100_cats")
        if pick:
            d = dfw[dfw.category.isin(pick)]
            fig = px.line(d, x="wk_date", y="per100", color="category", markers=True)
            fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis_title="$ per 100 baskets", xaxis_title="",
                              legend=dict(orientation="h", y=1.1, x=0),
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.markdown("##### Penetration with control limits")
        st.markdown('<p class="note">Share of baskets containing the category, '
                    'against a rolling 13-week mean ±2 standard errors. Points '
                    'outside the band are worth investigating; a run of seven '
                    'on one side is a trend even inside it.</p>',
                    unsafe_allow_html=True)

        cat = st.selectbox("Category", cats_by_size(dfw), key="ctrl_cat")
        d = dfw[dfw.category == cat].sort_values(["iso_year", "iso_week"]).copy()
        if len(d) >= 5:
            d["base"] = d.penetration.rolling(BASELINE_WEEKS, min_periods=4).mean()
            d["se"] = ((d.base * (1 - d.base)) / d.baskets) ** .5
            d["ucl"], d["lcl"] = d.base + 2 * d.se, d.base - 2 * d.se
            fig = go.Figure()
            fig.add_scatter(x=d.wk_date, y=d.ucl * 100, line=dict(width=0),
                            showlegend=False, hoverinfo="skip")
            fig.add_scatter(x=d.wk_date, y=d.lcl * 100, line=dict(width=0),
                            fill="tonexty", fillcolor="rgba(47,111,79,.10)",
                            name="control band", hoverinfo="skip")
            fig.add_scatter(x=d.wk_date, y=d.base * 100, name="baseline",
                            line=dict(color=MUTED, dash="dot", width=1.5))
            breach = d[(d.penetration > d.ucl) | (d.penetration < d.lcl)]
            fig.add_scatter(x=d.wk_date, y=d.penetration * 100, name="penetration",
                            mode="lines+markers", line=dict(color=ACCENT, width=2.5))
            if len(breach):
                fig.add_scatter(x=breach.wk_date, y=breach.penetration * 100,
                                mode="markers", name="outside limits",
                                marker=dict(color=BAD, size=11, symbol="circle-open",
                                            line=dict(width=2.5)))
            fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis_title="% of baskets", xaxis_title="",
                              legend=dict(orientation="h", y=1.1, x=0),
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Need at least 5 weeks for control limits.")

        st.divider()
        cc1, cc2 = st.columns(2)

        with cc1:
            st.markdown("##### Channel mix by week")
            ch = q(f"""
                SELECT iso_year, iso_week, channel, COUNT(*) baskets,
                       make_date(iso_year,1,4) + (iso_week-1)*7 AS wk_date
                FROM fact_basket WHERE NOT is_return {sf}
                GROUP BY 1,2,3 ORDER BY 1,2
            """)
            if not ch.empty:
                ch = ch.merge(ch.groupby(["iso_year", "iso_week"]).baskets.sum()
                                .rename("tot"), on=["iso_year", "iso_week"])
                ch["share"] = ch.baskets / ch.tot * 100
                keep = set(zip(dfw.iso_year, dfw.iso_week))
                ch = ch[ch.apply(lambda r: (r.iso_year, r.iso_week) in keep, axis=1)]
                fig = px.area(ch, x="wk_date", y="share", color="channel",
                              color_discrete_map={"In-Store": "#2F6F4F",
                                                  "Non-Stop": "#7FA98F",
                                                  "Delivery": "#B4632B"})
                fig.update_layout(height=330, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis_title="% of baskets", xaxis_title="",
                                  legend=dict(orientation="h", y=1.12, x=0),
                                  plot_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig, use_container_width=True)

        with cc2:
            st.markdown("##### Category by channel — index")
            idx = q(f"""
                WITH t AS (
                    SELECT channel, category, SUM(net_sales) net
                    FROM fact_line WHERE NOT is_return {sf} GROUP BY 1,2
                )
                SELECT category, channel,
                       net / SUM(net) OVER (PARTITION BY channel)
                       / (SUM(net) OVER (PARTITION BY category) / SUM(net) OVER ())
                       * 100 AS idx
                FROM t
            """)
            if not idx.empty:
                piv = idx.pivot(index="category", columns="channel", values="idx")
                piv = piv.reindex([c for c in cats_by_size(dfw) if c in piv.index])
                cols = [c for c in ["In-Store", "Non-Stop", "Delivery"]
                        if c in piv.columns]
                fig = px.imshow(piv[cols], text_auto=".0f", aspect="auto",
                                color_continuous_scale="RdYlGn", origin="upper",
                                zmin=40, zmax=160)
                fig.update_layout(height=330, margin=dict(l=0, r=0, t=10, b=0),
                                  coloraxis_showscale=False,
                                  xaxis_title="", yaxis_title="")
                st.plotly_chart(fig, use_container_width=True)
                st.markdown('<p class="note">100 is neutral. Above 115 means the '
                            'category over-indexes in that channel.</p>',
                            unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# INSIGHTS
# ---------------------------------------------------------------------------
with tab_insights:
    if ops_only or dfw.empty:
        st.info("Load data in the Ops tab to see insights.")
    else:
        st.markdown("#### Alerts")
        alerts = build_alerts(dfw)
        if not alerts:
            st.markdown('<div class="alert a-ok">No control-limit breaches or '
                        'run-rule signals in the latest week.</div>',
                        unsafe_allow_html=True)
        else:
            for a in alerts:
                cls = {"bad": "a-bad", "warn": "a-warn", "ok": "a-ok"}[a["sev"]]
                st.markdown(f'<div class="alert {cls}"><b>{a["cat"]}</b> — '
                            f'{a["msg"]}</div>', unsafe_allow_html=True)
        st.markdown('<p class="note">A single week outside the limits is a '
                    'prompt to look. A run of seven weeks on one side is the '
                    'signal that matters — slow erosion never trips a '
                    'threshold but compounds over a quarter.</p>',
                    unsafe_allow_html=True)

        st.divider()
        st.markdown("#### Category scorecard — latest week")
        wks = (dfw[["iso_year", "iso_week"]].drop_duplicates()
                 .sort_values(["iso_year", "iso_week"]))
        cy, cw = wks.iloc[-1]
        cur = dfw[(dfw.iso_year == cy) & (dfw.iso_week == cw)].copy()
        if len(wks) > 1:
            py, pw = wks.iloc[-2]
            pv = dfw[(dfw.iso_year == py) & (dfw.iso_week == pw)][
                ["category", "per100", "penetration"]].rename(
                columns={"per100": "p_prev", "penetration": "pen_prev"})
            cur = cur.merge(pv, on="category", how="left")
            cur["d_per100"] = (cur.per100 / cur.p_prev - 1) * 100
            cur["d_pen"] = (cur.penetration - cur.pen_prev) * 100
        cur = cur.sort_values("net", ascending=False)
        show = pd.DataFrame({
            "Category": cur.category,
            "Net $": cur.net.round(0),
            "% total": (cur.net / cur.net.sum() * 100).round(1),
            "$/100 bkt": cur.per100.round(0),
            "Δ WoW %": cur.get("d_per100", pd.Series(dtype=float)).round(1),
            "Penetration %": (cur.penetration * 100).round(1),
            "Δ pen pp": cur.get("d_pen", pd.Series(dtype=float)).round(2),
            "Margin %": (cur.margin_pct * 100).round(1),
        })
        st.dataframe(show, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("#### Substitution — co-purchase lift")
        st.markdown('<p class="note">Restricted to baskets containing two or '
                    'more categories. With most baskets holding a single item, '
                    'the unrestricted figure is suppressed for every pair and '
                    'reads as "everything substitutes everything".</p>',
                    unsafe_allow_html=True)
        lift = q(f"""
            WITH b AS (
                SELECT basket_id, category FROM fact_line
                WHERE NOT is_return {sf} GROUP BY 1,2
            ),
            m AS (SELECT basket_id FROM b GROUP BY 1 HAVING COUNT(*) >= 2),
            bm AS (SELECT b.* FROM b JOIN m USING (basket_id)),
            n AS (SELECT COUNT(DISTINCT basket_id)::DOUBLE t FROM bm),
            p AS (SELECT category, COUNT(DISTINCT basket_id)::DOUBLE/(SELECT t FROM n) pr
                  FROM bm GROUP BY 1),
            pr AS (
                SELECT x.category a, y.category b,
                       COUNT(*)::DOUBLE/(SELECT t FROM n) joint, COUNT(*) n
                FROM bm x JOIN bm y
                  ON x.basket_id=y.basket_id AND x.category<y.category
                GROUP BY 1,2
            )
            SELECT pr.a, pr.b, pr.n AS baskets,
                   pr.joint/(pa.pr*pb.pr) AS lift
            FROM pr JOIN p pa ON pa.category=pr.a JOIN p pb ON pb.category=pr.b
            WHERE pr.n >= 50 ORDER BY lift
        """)
        if not lift.empty:
            lift["Read"] = lift.lift.apply(
                lambda v: "substitutes" if v < .7 else
                          ("affinity" if v > 1.3 else "independent"))
            st.dataframe(pd.DataFrame({
                "Pair": lift.a + "  +  " + lift.b,
                "Lift": lift.lift.round(2),
                "Baskets": lift.baskets,
                "Read": lift.Read,
            }), use_container_width=True, hide_index=True)

        inv = q(f"""
            SELECT category, SUM(ext_cost) inv_cost, SUM(qty_on_hand) qoh,
                   COUNT(DISTINCT product) skus,
                   MAX(snapshot_date) snap
            FROM fact_inventory WHERE sellable {sf} GROUP BY 1
        """)
        if not inv.empty and inv.inv_cost.sum() > 0:
            st.divider()
            st.markdown("#### Inventory efficiency")
            sales = q(f"""
                SELECT category, SUM(net_sales) net, SUM(units) units
                FROM fact_line
                WHERE NOT is_return {sf}
                  AND txn_ts >= (SELECT MAX(txn_ts) - INTERVAL 30 DAY FROM fact_line)
                GROUP BY 1
            """)
            j = sales.merge(inv, on="category", how="inner")
            j["ssi"] = (j.net / j.net.sum()) / (j.inv_cost / j.inv_cost.sum())
            j["days"] = j.qoh / (j.units / 30)
            j = j.sort_values("net", ascending=False)
            st.dataframe(pd.DataFrame({
                "Category": j.category,
                "Net $ (30d)": j.net.round(0),
                "Inventory @ cost": j.inv_cost.round(0),
                "SKUs": j.skus,
                "SSI": j.ssi.round(2),
                "Days supply": j.days.round(0),
            }), use_container_width=True, hide_index=True)
            st.markdown('<p class="note">SSI above 1.2 means the category earns '
                        'more revenue than its share of your capital — a case '
                        'for expansion. Below 0.8 means capital is tied up '
                        'without pulling its weight. Snapshot dated '
                        f'{inv.snap.max()}.</p>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# OPS
# ---------------------------------------------------------------------------
with tab_ops:
    st.markdown("#### Load new exports")
    st.markdown('<p class="note">Drop a period\'s exports here — three per '
                'store (Dispensations, POS by Register) plus two chain-wide '
                '(Detailed Sales Breakdown, Alpine IQ). Files are identified '
                'by their contents, so filenames do not matter. Always export '
                'closed days only.</p>', unsafe_allow_html=True)

    up = st.file_uploader("Exports", type=["xlsx", "xls"],
                          accept_multiple_files=True, key="uploader")
    period = st.text_input("Period label", placeholder="2026-07",
                           help="Used in the load log. Usually YYYY-MM.")

    if up and period:
        if st.button("Validate and load", type="primary"):
            tmp = Path(tempfile.mkdtemp())
            for f in up:
                (tmp / f.name).write_bytes(f.getbuffer())
            st.caption(f"{len(up)} file(s) staged")

            import subprocess
            with st.spinner("Running ETL…"):
                r = subprocess.run(
                    [sys.executable, "tta_etl.py", "--inbox", str(tmp),
                     "--db", str(DB), "--period", period],
                    capture_output=True, text=True,
                    cwd=str(Path(__file__).parent))
            out = r.stdout + r.stderr
            if "FAILED" in out:
                st.error("One or more stores failed validation. Nothing written "
                         "for those stores.")
            elif r.returncode == 0:
                st.success("Loaded.")
                st.cache_data.clear()
            else:
                st.error("ETL returned an error.")
            st.code(out, language="text")
            shutil.rmtree(tmp, ignore_errors=True)

    st.divider()
    st.markdown("#### Inventory snapshot")
    st.markdown('<p class="note">The inventory export has no date column, so '
                'the snapshot date is read from its Export Date header. Load '
                'this daily — inventory position at a point in time cannot be '
                'reconstructed later.</p>', unsafe_allow_html=True)

    inv_up = st.file_uploader("Current Inventory export", type=["xlsx", "xls"],
                              key="inv_uploader")
    if inv_up and st.button("Load snapshot"):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / inv_up.name
        p.write_bytes(inv_up.getbuffer())
        try:
            from tta_etl import Pipeline, read_export
            from tta_refresh import _export_date
            stamp = _export_date(p)
            pipe = Pipeline(str(DB))
            counts = pipe.load_inventory(read_export(p, "inventory"), stamp)
            pipe.close()
            st.success(f"Snapshot {stamp} loaded: {counts}")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    st.divider()
    st.markdown("#### Database")

    if DB.exists():
        c = st.columns(4)
        stats = q("""
            SELECT COUNT(*) lines,
                   COUNT(DISTINCT basket_id) baskets,
                   MIN(txn_ts) first_txn, MAX(txn_ts) last_txn
            FROM fact_line
        """)
        if not stats.empty:
            s = stats.iloc[0]
            c[0].metric("Lines", f"{int(s.lines):,}")
            c[1].metric("Baskets", f"{int(s.baskets):,}")
            c[2].metric("From", str(s.first_txn)[:10])
            c[3].metric("To", str(s.last_txn)[:10])
        st.caption(f"{DB.stat().st_size/1e6:,.0f} MB · {DB}")

        log = q("""
            SELECT period, COUNT(*) stores, SUM(lines) lines,
                   SUM(baskets) baskets, SUM(warnings) warnings,
                   MAX(config_version) config, MAX(loaded_at) loaded
            FROM load_log GROUP BY period ORDER BY period DESC
        """)
        if not log.empty:
            st.markdown("##### Load history")
            bad = log[log.stores != 4]
            if not bad.empty:
                st.warning(f"{len(bad)} period(s) do not have all four stores: "
                           f"{', '.join(bad.period)}")
            st.dataframe(log, use_container_width=True, hide_index=True)
    else:
        st.info("No database yet. Load a period above to create one.")

    st.divider()
    st.markdown("#### Cloud sync")
    st.markdown('<p class="note">Push the database to the Drive state folder so '
                'the scheduled Monday refresh starts from current data.</p>',
                unsafe_allow_html=True)
    if st.button("Upload database to Drive"):
        try:
            import os
            from tta_env import bootstrap
            from tta_drive import DriveClient
            bootstrap()
            with st.spinner("Uploading…"):
                DriveClient().upload(DB, os.environ["TTA_DRIVE_STATE"])
            st.success("Uploaded to TTA/state.")
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")
            st.caption("Check .env and run tta_preflight.py.")
