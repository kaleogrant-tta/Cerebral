"""
Cerebral — read-only dashboard.

Deliberately a separate file from cerebral_app.py rather than a flag on it.
A flag can be mis-set; a separate app that only knows how to read the slim
schema cannot expose Ops or customer data, because neither is reachable
from here.

Reads cerebral_dash.duckdb — pre-aggregated, no customer identifiers of any
kind. Built by publish.py at the end of each scheduled refresh and pulled
from Drive on startup.

Local:
    python -m streamlit run cerebral_public.py

Deployed (Streamlit Community Cloud): set these in the app's Secrets —

    APP_PASSWORD = "something-long"
    TTA_DRIVE_STATE = "17GL1j3sAO1fexQb4RG5LTANGPO_TBbnj"

    [gcp_service_account]
    ...contents of the service account JSON...
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from glossary import GLOSSARY, SECTIONS, marker, section_note, tip

DASH_FILE = "cerebral_dash.duckdb"
CACHE_MINUTES = 30
BASELINE_WEEKS = 13

STORES = {1: "DTBK", 2: "5th Avenue", 3: "Soho", 4: "Union Square"}

st.set_page_config(page_title="Cerebral", layout="wide",
                   initial_sidebar_state="expanded")

# ===========================================================================
# Theme
#
# The stylesheet below is written to theme.css on first run. Edit that file
# and reload — it takes precedence from then on, and this copy is only the
# fallback. The :root block is the single source of truth for colour: the
# variables are parsed out and handed to Plotly, so changing --accent
# restyles the page chrome and the charts together.
# ===========================================================================

DEFAULT_CSS = r"""/* ==========================================================================
   Cerebral — stylesheet
   ==========================================================================

   Everything visual lives here. Edit freely and reload the page.

   The :root block below is the single source of truth for colour: theme.py
   parses these variables and hands them to Plotly, so changing --accent here
   restyles both the page chrome and the charts.

   What this file CANNOT reach: page background, sidebar background, the base
   font, and Streamlit's own widget colours. Those come from Streamlit's
   theming system — see .streamlit/config.toml.
   ========================================================================== */

:root {
  /* Chart and accent colours. Read by theme.py for Plotly. */
  --accent:        #2F6F4F;   /* primary — bars, lines, positive states     */
  --accent-soft:   #7FA98F;   /* secondary series                           */
  --warn:          #B4632B;   /* third series, cautionary states            */
  --bad:           #9E3B32;   /* breaches, negative alerts                  */
  --ink:           #101418;   /* headings, emphasised text                  */
  --body:          #3C4650;   /* explanatory paragraphs                     */
  --muted:         #7A8590;   /* captions, secondary lines, labels          */
  --rule:          #9AA4AE;   /* hairlines, dotted underlines               */

  /* Multi-series chart palette. Ordered by how well they separate — the
     first four carry most charts, so they are the most distinct.          */
  --series-1:      #2F6F4F;   /* green   */
  --series-2:      #B4632B;   /* rust    */
  --series-3:      #3B6E8F;   /* blue    */
  --series-4:      #8A5A9E;   /* violet  */
  --series-5:      #A8913C;   /* ochre   */
  --series-6:      #6E7F8D;   /* slate   */
  --series-7:      #9E3B32;   /* red     */
  --series-8:      #4C8C7A;   /* teal    */
  --series-9:      #7A6A55;   /* taupe   */

  /* Surfaces */
  --tint:          rgba(47, 111, 79, .06);   /* how-to callout background   */
  --tint-band:     rgba(47, 111, 79, .10);   /* control-limit band fill     */
  --alert-bg:      rgba(128, 128, 128, .06);

  /* Layout */
  --max-width:     1500px;
  --pad-top:       2.2rem;
}

/* --- page ---------------------------------------------------------------- */

.block-container {
  padding-top: var(--pad-top);
  max-width: var(--max-width);
}

h1, h2, h3 {
  letter-spacing: -0.02em;
}

/* --- metrics ------------------------------------------------------------- */

[data-testid="stMetricValue"] {
  font-size: 1.55rem;
}

[data-testid="stMetricLabel"] {
  font-size: .74rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--muted);
}

/* --- tabs ---------------------------------------------------------------- */

.stTabs [data-baseweb="tab"] {
  padding: 0 1.1rem;
  font-weight: 500;
}

/* --- alerts -------------------------------------------------------------- */

.alert {
  border-left: 3px solid;
  padding: .55rem .9rem;
  margin: .35rem 0;
  background: var(--alert-bg);
  font-size: .9rem;
}

.a-bad  { border-color: var(--bad); }
.a-warn { border-color: var(--warn); }
.a-ok   { border-color: var(--accent); }

/* --- explanatory text ---------------------------------------------------- */

.note {
  color: var(--muted);
  font-size: .82rem;
  line-height: 1.45;
}

/* "How to read this" callouts */
.howto {
  background: var(--tint);
  border-left: 3px solid var(--accent);
  padding: .7rem 1rem;
  margin: .4rem 0 1rem 0;
  font-size: .87rem;
  line-height: 1.5;
  color: var(--body);
}

.howto b { color: var(--ink); }

/* --- glossary hover markers ---------------------------------------------- */

.gloss {
  border-bottom: 1px dotted var(--rule);
  cursor: help;
}

.gloss-mark {
  font-size: .62em;
  vertical-align: super;
  color: var(--muted);
  margin-left: 1px;
  font-weight: 600;
}

/* --- glossary entries ---------------------------------------------------- */

.gloss-entry {
  margin: .1rem 0 .9rem 0;
}

.gloss-entry b { color: var(--ink); }

.gloss-body {
  color: var(--body);
  font-size: .9rem;
  line-height: 1.55;
}
"""

CSS_FILE = Path(__file__).parent / "theme.css"


def _parse_root(css: str) -> dict:
    m = re.search(r":root\s*\{(.*?)\}", css, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        line = line.split("/*")[0]
        hit = re.match(r"\s*--([\w-]+)\s*:\s*([^;]+);", line)
        if hit:
            out[hit.group(1).strip()] = hit.group(2).strip()
    return out


def load_theme() -> dict:
    """Inject the stylesheet and return the colour palette."""
    if not CSS_FILE.exists():
        try:
            CSS_FILE.write_text(DEFAULT_CSS, encoding="utf-8")
        except Exception:
            pass                                  # read-only deploy: use inline
    try:
        css = CSS_FILE.read_text(encoding="utf-8")
    except Exception:
        css = DEFAULT_CSS
    st.markdown(f"<style>\n{css}\n</style>", unsafe_allow_html=True)
    return _parse_root(css) or _parse_root(DEFAULT_CSS)


PALETTE = load_theme()

ACCENT      = PALETTE.get("accent", "#2F6F4F")
ACCENT_SOFT = PALETTE.get("accent-soft", "#7FA98F")
WARN        = PALETTE.get("warn", "#B4632B")
MUTED       = PALETTE.get("muted", "#7A8590")
BAD         = PALETTE.get("bad", "#9E3B32")
BAND        = PALETTE.get("tint-band", "rgba(47,111,79,.10)")
SERIES      = [PALETTE[f"series-{i}"] for i in range(1, 10)
               if f"series-{i}" in PALETTE] or [ACCENT]


# ===========================================================================
# Access
# ===========================================================================

def secret(key: str, default=None):
    """Read a Streamlit secret without exploding when none are configured.

    st.secrets always exists as an attribute, so hasattr() is not a guard —
    it is the lookup that raises. Locally there is no secrets.toml and that
    is fine.
    """
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def gate() -> bool:
    pw = secret("APP_PASSWORD")
    if not pw:
        return True                              # no password configured
    if st.session_state.get("_ok"):
        return True
    st.markdown("### Cerebral")
    st.caption("Category analytics · The Travel Agency")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == pw:
            st.session_state["_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect.")
    return False


# ===========================================================================
# Data
# ===========================================================================

@st.cache_resource(ttl=CACHE_MINUTES * 60)
def load_db() -> str | None:
    """Local file if present, otherwise pull the published copy from Drive."""
    local = Path(DASH_FILE)
    if local.exists():
        return str(local)

    sa = secret("gcp_service_account")
    folder = secret("TTA_DRIVE_STATE") or os.environ.get("TTA_DRIVE_STATE")
    if not sa or not folder:
        return None

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    info = dict(sa) if not isinstance(sa, str) else json.loads(sa)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    res = svc.files().list(
        q=f"'{folder}' in parents and name = '{DASH_FILE}' and trashed = false",
        fields="files(id,name,size)").execute().get("files", [])
    if not res:
        return None

    dest = Path(tempfile.gettempdir()) / DASH_FILE
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, svc.files().get_media(fileId=res[0]["id"]),
                                 chunksize=4 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return str(dest)


@st.cache_data(ttl=CACHE_MINUTES * 60)
def q(sql: str) -> pd.DataFrame:
    path = load_db()
    if not path:
        return pd.DataFrame()
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def pct_change(late, early):
    """Percentage change, returning float NaN where there is no baseline.

    Products and brands that did not exist in the first half have zero early
    sales. Dividing by pd.NA yields NAType, which has no __round__ and breaks
    every downstream format call — numpy NaN behaves correctly and renders as
    an empty cell.
    """
    import numpy as np
    e = pd.to_numeric(early, errors="coerce").astype("float64")
    l = pd.to_numeric(late, errors="coerce").astype("float64")
    return np.where(e > 0, (l / e.replace(0, np.nan) - 1) * 100, np.nan)


def howto(key: str) -> None:
    """Render the plain-language note for a section."""
    txt = section_note(key)
    if txt:
        body = txt.replace("\n\n", "<br><br>")
        st.markdown(f'<div class="howto"><b>How to read this.</b> {body}</div>',
                    unsafe_allow_html=True)


def heading(text: str, term: str | None = None) -> None:
    """Section heading, with a hover definition on the heading itself rather
    than the term repeated after it."""
    if term and tip(term):
        st.markdown(f"##### {marker(term, text)}", unsafe_allow_html=True)
    else:
        st.markdown(f"##### {text}")


def sfilter(keys: list[int]) -> str:
    if not keys or len(keys) == len(STORES):
        return ""
    return f" WHERE store_key IN ({','.join(map(str, keys))})"


def and_filter(keys: list[int]) -> str:
    if not keys or len(keys) == len(STORES):
        return ""
    return f" AND store_key IN ({','.join(map(str, keys))})"


# ===========================================================================
# Analysis helpers
# ===========================================================================

def control_band(hist: pd.Series, n: float):
    if len(hist) < 4 or n <= 0:
        return None, None, None
    p = hist.mean()
    se = (p * (1 - p) / n) ** .5
    return p, p - 2 * se, p + 2 * se


def run_rule(s: pd.Series) -> str | None:
    if len(s) < 7:
        return None
    base = s.iloc[:-1].mean()
    l7 = s.tail(7)
    if (l7 > base).all():
        return "7 consecutive weeks above baseline"
    if (l7 < base).all():
        return "7 consecutive weeks below baseline"
    d = s.tail(6).diff().dropna()
    if len(d) >= 5 and (d > 0).all():
        return "6 consecutive weeks rising"
    if len(d) >= 5 and (d < 0).all():
        return "6 consecutive weeks falling"
    return None


# ===========================================================================
# App
# ===========================================================================

if not gate():
    st.stop()

if load_db() is None:
    st.title("Cerebral")
    st.error("No published data available.")
    st.markdown("""
The dashboard reads `cerebral_dash.duckdb`, built by the scheduled refresh.

If you are seeing this, either the refresh has not run since this app was
deployed, or the app's Drive credentials are not configured.
""")
    st.stop()

meta = q("SELECT * FROM dash_meta").iloc[0]

st.sidebar.markdown("### Cerebral")
picked = st.sidebar.multiselect("Stores", list(STORES.values()),
                                default=list(STORES.values()))
keys = [k for k, v in STORES.items() if v in picked] or list(STORES)
wf, af = sfilter(keys), and_filter(keys)

cw = q(f"""
    SELECT iso_year, iso_week, category,
           SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
           SUM(baskets_with) AS baskets_with
    FROM dash_category_week {wf}
    GROUP BY 1,2,3
""")
bw = q(f"""
    SELECT iso_year, iso_week, SUM(baskets) AS baskets,
           SUM(net) AS net, SUM(redeem_baskets) AS redeem_baskets,
           SUM(redeem_value) AS redeem_value, MAX(days_open) AS days_open
    FROM dash_basket_week {wf}
    GROUP BY 1,2
""")

if cw.empty or bw.empty:
    st.error("No data for the selected stores.")
    st.stop()

df = cw.merge(bw[["iso_year", "iso_week", "baskets"]], on=["iso_year", "iso_week"])
df["penetration"] = df.baskets_with / df.baskets
df["per100"] = df.net / df.baskets * 100
df["margin_pct"] = pd.to_numeric(df.gm, errors="coerce") / \
    pd.to_numeric(df.net, errors="coerce").replace(0, float("nan"))
df["wk_date"] = pd.to_datetime(
    df.iso_year.astype(str) + "-W" + df.iso_week.astype(str).str.zfill(2) + "-1",
    format="%G-W%V-%u", errors="coerce")
# Plotly draws points in row order, so unsorted data produces a scribble.
df = df.sort_values(["category", "wk_date"]).reset_index(drop=True)

weeks = (df[["iso_year", "iso_week"]].drop_duplicates()
           .sort_values(["iso_year", "iso_week"]).reset_index(drop=True))
n_wk = st.sidebar.slider("Weeks shown", 4, max(len(weeks), 4), min(26, len(weeks)))
keep = set(zip(weeks.iso_year.tail(n_wk), weeks.iso_week.tail(n_wk)))
dfv = df[df.apply(lambda r: (r.iso_year, r.iso_week) in keep, axis=1)]
dfv = dfv.sort_values(["category", "wk_date"])

st.sidebar.divider()
st.sidebar.caption(
    f"{int(meta.n_baskets):,} baskets\n\n"
    f"{str(meta.first_txn)[:10]} → {str(meta.last_txn)[:10]}\n\n"
    f"updated {str(meta.built_at)[:16]}")

st.title("Cerebral")
label = "All stores" if len(keys) == len(STORES) else ", ".join(STORES[k] for k in keys)
st.caption(f"Category analytics · The Travel Agency · {label}")

t_charts, t_insights, t_gloss = st.tabs(["Charts", "Insights", "What the terms mean"])

# ---------------------------------------------------------------- charts
with t_charts:
    wk = (dfv.groupby(["iso_year", "iso_week", "wk_date"])
             .agg(net=("net", "sum"), baskets=("baskets", "first"))
             .reset_index().sort_values("wk_date"))
    wk["atv"] = wk.net / wk.baskets
    cur = wk.iloc[-1]
    prev = wk.iloc[-2] if len(wk) > 1 else None

    c = st.columns(4)
    c[0].metric("Net sales, latest week", f"${cur.net:,.0f}",
                f"{(cur.net/prev.net-1)*100:+.1f}%" if prev is not None else None,
                help=tip("net sales"))
    c[1].metric("Baskets", f"{int(cur.baskets):,}",
                f"{(cur.baskets/prev.baskets-1)*100:+.1f}%" if prev is not None else None,
                help=tip("basket"))
    c[2].metric("Average basket", f"${cur.atv:,.2f}",
                f"{(cur.atv/prev.atv-1)*100:+.1f}%" if prev is not None else None,
                help=tip("average basket"))
    c[3].metric("Weeks shown", f"{len(wk)}",
                help="How many weeks of history the charts below cover. "
                     "Change it with the slider in the sidebar.")

    st.divider()
    L, R = st.columns([3, 2])

    with L:
        heading("Net sales and basket count by week")
        howto("revenue_traffic")
        fig = go.Figure()
        fig.add_bar(x=wk.wk_date, y=wk.net, name="Net sales",
                    marker_color=ACCENT, opacity=.75)
        fig.add_scatter(x=wk.wk_date, y=wk.baskets, name="Baskets", yaxis="y2",
                        line=dict(color=MUTED, width=2))
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis=dict(title="Net $", tickformat="$,.0s",
                                     gridcolor="rgba(0,0,0,.07)"),
                          yaxis2=dict(title="Baskets", overlaying="y",
                                      side="right", showgrid=False,
                                      tickformat=",.0f"),
                          hovermode="x unified",
                          legend=dict(orientation="h", y=1.12, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    with R:
        st.markdown("##### Category share of revenue")
        mix = dfv.groupby("category").net.sum().reset_index().sort_values(
            "net", ascending=False)
        fig = px.pie(mix, values="net", names="category", hole=.58,
                     color_discrete_sequence=SERIES)
        fig.update_traces(textposition="outside", textinfo="percent+label",
                          hovertemplate="%{label}<br>%{value:$,.0f}"
                                        "<extra></extra>")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    heading("Dollars per 100 baskets", "$/100 baskets")
    howto("per100")
    order = dfv.groupby("category").net.sum().sort_values(ascending=False).index.tolist()
    cc1, cc2 = st.columns([3, 1])
    with cc1:
        pick = st.multiselect("Categories", order, default=order[:4])
    with cc2:
        smooth = st.checkbox("4-week average", value=True,
                             help="Averages each point with the three weeks "
                                  "before it. Week-to-week noise is large "
                                  "enough to hide the trend underneath.")
    if pick:
        d = dfv[dfv.category.isin(pick)].sort_values(["category", "wk_date"]).copy()
        ycol, ylab = "per100", "$ per 100 baskets"
        if smooth:
            d["smoothed"] = (d.groupby("category")["per100"]
                              .transform(lambda x: x.rolling(4, min_periods=2).mean()))
            ycol, ylab = "smoothed", "$ per 100 baskets (4-week average)"
        fig = px.line(d, x="wk_date", y=ycol, color="category",
                      markers=not smooth, color_discrete_sequence=SERIES)
        fig.update_traces(line=dict(width=2.6),
                          marker=dict(size=6),
                          hovertemplate="%{y:$,.0f}<extra>%{fullData.name}</extra>")
        fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title=ylab, xaxis_title="",
                          hovermode="x unified",
                          legend=dict(orientation="h", y=1.1, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", zeroline=False,
                         tickformat="$,.0f")
        fig.update_xaxes(gridcolor="rgba(0,0,0,.04)", showgrid=True)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    heading("Penetration with control limits", "penetration")
    howto("control_chart")
    cat = st.selectbox("Category", order)
    d = dfv[dfv.category == cat].sort_values(["iso_year", "iso_week"]).copy()
    if len(d) >= 5:
        d["base"] = d.penetration.rolling(BASELINE_WEEKS, min_periods=4).mean()
        d["se"] = ((d.base * (1 - d.base)) / d.baskets) ** .5
        d["ucl"], d["lcl"] = d.base + 2 * d.se, d.base - 2 * d.se
        fig = go.Figure()
        fig.add_scatter(x=d.wk_date, y=d.ucl * 100, line=dict(width=0),
                        showlegend=False, hoverinfo="skip")
        fig.add_scatter(x=d.wk_date, y=d.lcl * 100, line=dict(width=0),
                        fill="tonexty", fillcolor=BAND,
                        name="control band", hoverinfo="skip")
        fig.add_scatter(x=d.wk_date, y=d.base * 100, name="baseline",
                        line=dict(color=MUTED, dash="dot", width=1.5))
        fig.add_scatter(x=d.wk_date, y=d.penetration * 100, name="penetration",
                        mode="lines+markers", line=dict(color=ACCENT, width=2.5))
        br = d[(d.penetration > d.ucl) | (d.penetration < d.lcl)]
        if len(br):
            fig.add_scatter(x=br.wk_date, y=br.penetration * 100, mode="markers",
                            name="outside limits",
                            marker=dict(color=BAD, size=11, symbol="circle-open",
                                        line=dict(width=2.5)))
        fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="% of baskets containing this category",
                          xaxis_title="", hovermode="x unified",
                          legend=dict(orientation="h", y=1.1, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", ticksuffix="%",
                         zeroline=False)
        fig.update_xaxes(gridcolor="rgba(0,0,0,.04)")
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    A, B = st.columns(2)
    with A:
        heading("Channel mix by week", "channel")
        ch = q(f"""
            SELECT iso_year, iso_week, channel, SUM(baskets) AS baskets
            FROM dash_basket_week {wf} GROUP BY 1,2,3
        """)
        if not ch.empty:
            ch = ch.merge(ch.groupby(["iso_year", "iso_week"]).baskets.sum()
                            .rename("tot"), on=["iso_year", "iso_week"])
            ch["share"] = ch.baskets / ch.tot * 100
            ch = ch[ch.apply(lambda r: (r.iso_year, r.iso_week) in keep, axis=1)]
            ch["wk_date"] = pd.to_datetime(
                ch.iso_year.astype(str) + "-W" +
                ch.iso_week.astype(str).str.zfill(2) + "-1",
                format="%G-W%V-%u", errors="coerce")
            ch = ch.sort_values(["channel", "wk_date"])
            fig = px.area(ch, x="wk_date", y="share", color="channel",
                          color_discrete_map={"In-Store": ACCENT,
                                              "Non-Stop": ACCENT_SOFT,
                                              "Delivery": WARN})
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis_title="% of baskets", xaxis_title="",
                              hovermode="x unified",
                              legend=dict(orientation="h", y=1.12, x=0,
                                          title_text=""),
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_yaxes(ticksuffix="%", gridcolor="rgba(0,0,0,.07)")
            st.plotly_chart(fig, use_container_width=True)

    with B:
        heading("Category by channel — index", "channel index")
        ci = q(f"""
            WITH t AS (SELECT channel, category, SUM(net) AS net
                       FROM dash_category_week {wf} GROUP BY 1,2)
            SELECT category, channel,
                   net / SUM(net) OVER (PARTITION BY channel)
                   / (SUM(net) OVER (PARTITION BY category) / SUM(net) OVER ())
                   * 100 AS idx
            FROM t
        """)
        if not ci.empty:
            piv = ci.pivot(index="category", columns="channel", values="idx")
            piv = piv.reindex([c for c in order if c in piv.index])
            cols = [c for c in ["In-Store", "Non-Stop", "Delivery"] if c in piv.columns]
            fig = px.imshow(piv[cols], text_auto=".0f", aspect="auto",
                            color_continuous_scale="RdYlGn", origin="upper",
                            zmin=40, zmax=160)
            fig.update_traces(textfont_size=13)
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                              coloraxis_showscale=False, xaxis_title="",
                              yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)
            howto("channel_grid")

# -------------------------------------------------------------- insights
with t_insights:
    st.markdown("#### Alerts")
    howto("alerts")
    alerts = []
    if len(weeks) >= 5:
        cy, cwk = weeks.iloc[-1]
        hist = weeks.iloc[max(0, len(weeks) - 1 - BASELINE_WEEKS):len(weeks) - 1]
        hk = set(zip(hist.iso_year, hist.iso_week))
        latest = df[(df.iso_year == cy) & (df.iso_week == cwk)]
        tot = latest.net.sum()
        for _, r in latest.iterrows():
            if tot and r.net / tot < 0.01:      # too small for limits to mean anything
                continue
            h = df[(df.category == r.category)
                   & df.apply(lambda x: (x.iso_year, x.iso_week) in hk, axis=1)]
            h = h.sort_values(["iso_year", "iso_week"])
            if len(h) < 4:
                continue
            base, lcl, ucl = control_band(h.penetration, r.baskets)
            if base is not None:
                if r.penetration < lcl:
                    alerts.append(("bad", r.category,
                        f"penetration {r.penetration*100:.1f}% is below the lower "
                        f"control limit {lcl*100:.1f}% (baseline {base*100:.1f}%)"))
                elif r.penetration > ucl:
                    alerts.append(("ok", r.category,
                        f"penetration {r.penetration*100:.1f}% is above the upper "
                        f"control limit {ucl*100:.1f}% (baseline {base*100:.1f}%)"))
            rr = run_rule(pd.concat([h.penetration, pd.Series([r.penetration])]))
            if rr:
                alerts.append(("bad" if ("below" in rr or "falling" in rr) else "ok",
                               r.category, rr + " on penetration"))
    alerts.sort(key=lambda a: 0 if a[0] == "bad" else 1)
    if not alerts:
        st.markdown('<div class="alert a-ok">No control-limit breaches or '
                    'run-rule signals in the latest week.</div>',
                    unsafe_allow_html=True)
    for sev, cat, msg in alerts:
        st.markdown(f'<div class="alert a-{sev}"><b>{cat}</b> — {msg}</div>',
                    unsafe_allow_html=True)


    st.divider()
    st.markdown("#### Category scorecard — latest week")
    howto("scorecard")
    cy, cwk = weeks.iloc[-1]
    cur = df[(df.iso_year == cy) & (df.iso_week == cwk)].copy()
    if len(weeks) > 1:
        py, pw = weeks.iloc[-2]
        pv = df[(df.iso_year == py) & (df.iso_week == pw)][
            ["category", "per100", "penetration"]].rename(
            columns={"per100": "pp", "penetration": "pn"})
        cur = cur.merge(pv, on="category", how="left")
        cur["d100"] = (cur.per100 / cur.pp - 1) * 100
        cur["dpen"] = (cur.penetration - cur.pn) * 100
    cur = cur.sort_values("net", ascending=False)
    st.dataframe(pd.DataFrame({
        "Category": cur.category,
        "Net $": cur.net.round(0),
        "% total": (cur.net / cur.net.sum() * 100).round(1),
        "$/100 bkt": cur.per100.round(0),
        "Δ WoW %": pd.to_numeric(cur.get("d100", pd.Series(dtype=float)),
                                 errors="coerce").round(1),
        "Penetration %": (cur.penetration * 100).round(1),
        "Δ pen pp": pd.to_numeric(cur.get("dpen", pd.Series(dtype=float)),
                                  errors="coerce").round(2),
        "Margin %": pd.to_numeric(cur.margin_pct * 100, errors="coerce").round(1),
    }), use_container_width=True, hide_index=True, column_config={
        "Net $": st.column_config.NumberColumn(
            help=tip("net sales"), format="$%d"),
        "% total": st.column_config.NumberColumn(
            help="This category's share of all revenue this week.",
            format="%.1f%%"),
        "$/100 bkt": st.column_config.NumberColumn(
            help=tip("$/100 baskets"), format="$%d"),
        "Δ WoW %": st.column_config.NumberColumn(
            help="Change in $/100 baskets versus last week. " + tip("Δ WoW"),
            format="%.1f%%"),
        "Penetration %": st.column_config.NumberColumn(
            help=tip("penetration"), format="%.1f%%"),
        "Δ pen pp": st.column_config.NumberColumn(
            help="Change in penetration versus last week. " + tip("pp"),
            format="%.2f"),
        "Margin %": st.column_config.NumberColumn(
            help=tip("gross margin"), format="%.1f%%"),
    })

    st.divider()
    st.markdown("#### Which categories get bought together")
    howto("lift")
    lift = q(f"""
        WITH p AS (SELECT cat_a, cat_b, SUM(joint_baskets) AS n
                   FROM dash_pairs {wf} GROUP BY 1,2),
        b AS (SELECT category, SUM(cat_baskets) AS cb,
                     MAX(multi_baskets) AS mb
              FROM dash_pair_base {wf} GROUP BY 1),
        t AS (SELECT SUM(mb) AS total FROM (
                SELECT DISTINCT store_key, multi_baskets AS mb
                FROM dash_pair_base {wf}))
        SELECT p.cat_a, p.cat_b, p.n AS baskets,
               (p.n::DOUBLE / (SELECT total FROM t))
               / ((ba.cb::DOUBLE / (SELECT total FROM t))
                  * (bb.cb::DOUBLE / (SELECT total FROM t))) AS lift
        FROM p JOIN b ba ON ba.category = p.cat_a
               JOIN b bb ON bb.category = p.cat_b
        WHERE p.n >= 50 ORDER BY lift
    """)
    if not lift.empty:
        lift["Read"] = lift.lift.apply(
            lambda v: "substitutes" if v < .7 else
                      ("affinity" if v > 1.3 else "independent"))
        st.dataframe(pd.DataFrame({
            "Pair": lift.cat_a + "  +  " + lift.cat_b,
            "Lift": lift.lift.round(2),
            "Baskets": lift.baskets,
            "What it means": lift.Read,
        }), use_container_width=True, hide_index=True, column_config={
            "Pair": st.column_config.TextColumn(
                help="The two categories being compared."),
            "Lift": st.column_config.NumberColumn(
                help=tip("co-purchase lift"), format="%.2f"),
            "Baskets": st.column_config.NumberColumn(
                help="How many transactions contained both. Bigger numbers "
                     "make the result more reliable.", format="%d"),
            "What it means": st.column_config.TextColumn(
                help="Substitutes — customers pick one or the other. "
                     "Affinity — often bought together. "
                     "Independent — no real relationship."),
        })


    # ---- drill-down: brands and products behind a category pair ---------
    if not lift.empty:
        st.markdown("##### Go deeper on a pair")
        st.markdown('<p class="note">Pick a pair to see which brands and '
                    'products sit behind the category-level read, and what it '
                    'suggests for buying.</p>', unsafe_allow_html=True)

        labels = [f"{r.cat_a} + {r.cat_b}  ({r.lift:.2f} — {r.Read})"
                  for _, r in lift.iterrows()]
        choice = st.selectbox("Category pair", labels, key="pairpick")
        row = lift.iloc[labels.index(choice)]
        ca, cb, lv, read = row.cat_a, row.cat_b, float(row.lift), row.Read

        bt = q(f"""
            SELECT category, brand,
                   SUM(net_early) AS net_early, SUM(net_late) AS net_late,
                   SUM(units_early) AS u_early, SUM(units_late) AS u_late,
                   SUM(net_total) AS net_total
            FROM dash_brand_trend
            WHERE category IN ('{ca}','{cb}') {af}
            GROUP BY 1,2 HAVING SUM(net_total) >= 2000
        """)

        if bt.empty:
            st.info("Not enough brand-level volume in this pair to break down.")
        else:
            bt["change"] = pct_change(bt.net_late, bt.net_early)
            bt["dir"] = bt.change.apply(
                lambda v: "growing" if pd.notna(v) and v > 10 else
                          ("declining" if pd.notna(v) and v < -10 else "flat"))

            # --- the recommendation ---------------------------------------
            a_side = bt[bt.category == ca].sort_values("net_total", ascending=False)
            b_side = bt[bt.category == cb].sort_values("net_total", ascending=False)
            a_chg = a_side.change.median()
            b_chg = b_side.change.median()

            lines = []
            if read == "substitutes":
                lines.append(
                    f"**Customers choose between {ca} and {cb}** rather than "
                    f"buying both (lift {lv:.2f}). Demand is likely to move "
                    f"between them rather than disappear.")
                if pd.notna(a_chg) and pd.notna(b_chg) and abs(a_chg - b_chg) > 15:
                    up, dn = (cb, ca) if b_chg > a_chg else (ca, cb)
                    lines.append(
                        f"Demand is moving toward **{up}**. Weight the buy that "
                        f"way rather than carrying full depth in both — you are "
                        f"funding two ranges to serve one decision.")
                else:
                    lines.append(
                        "Neither side is clearly winning, so both are earning "
                        "their place. Avoid promoting them against each other "
                        "in the same week — you would be discounting a sale "
                        "you were getting anyway.")
                lines.append(
                    f"Before cutting depth in either, check the brand table "
                    f"below: a category-level substitution often comes down to "
                    f"a handful of brands, and the rest are unaffected.")
            elif read == "affinity":
                lines.append(
                    f"**{ca} and {cb} get bought together** more than chance "
                    f"predicts (lift {lv:.2f}).")
                lines.append(
                    "Place them near each other and prompt the attachment at "
                    "the counter. A stockout in one now costs you sales in "
                    "both, so treat their par levels as linked rather than "
                    "independent.")
                lines.append(
                    "Bundle candidates are the top brand pairs below — they "
                    "already co-occur, so a bundle formalises existing "
                    "behaviour rather than trying to create it.")
            else:
                lines.append(
                    f"**No real relationship** between {ca} and {cb} "
                    f"(lift {lv:.2f}). Buying and merchandising decisions on "
                    f"one should be made without reference to the other.")

            st.markdown('<div class="howto"><b>What this suggests.</b> '
                        + "<br><br>".join(lines) + "</div>",
                        unsafe_allow_html=True)

            # --- brand detail ---------------------------------------------
            st.markdown("**Brands in these categories**")
            show = bt.sort_values("net_total", ascending=False).head(18)
            st.dataframe(pd.DataFrame({
                "Category": show.category,
                "Brand": show.brand,
                "Net $": show.net_total.round(0),
                "Change %": pd.to_numeric(show.change, errors="coerce").round(1),
                "Direction": show.dir,
            }), use_container_width=True, hide_index=True, column_config={
                "Net $": st.column_config.NumberColumn(
                    help="Net sales across the whole loaded period.",
                    format="$%d"),
                "Change %": st.column_config.NumberColumn(
                    help="Second half of the period versus the first half. "
                         "Shows which way the brand is moving.",
                    format="%.1f%%"),
                "Direction": st.column_config.TextColumn(
                    help="Growing is more than +10%, declining is worse than "
                         "-10%, anything between is flat."),
            })

            # --- brand pairs ----------------------------------------------
            bp = q(f"""
                SELECT brand_a, brand_b, SUM(joint_baskets) AS baskets
                FROM dash_brand_pairs
                WHERE cat_a = '{ca}' AND cat_b = '{cb}' {af}
                GROUP BY 1,2 ORDER BY baskets DESC LIMIT 12
            """)
            if not bp.empty:
                st.markdown(f"**Brand combinations actually bought together**")
                st.dataframe(pd.DataFrame({
                    f"{ca} brand": bp.brand_a,
                    f"{cb} brand": bp.brand_b,
                    "Baskets": bp.baskets,
                }), use_container_width=True, hide_index=True,
                    column_config={"Baskets": st.column_config.NumberColumn(
                        help="Transactions containing both. These are the "
                             "specific combinations customers already choose.",
                        format="%d")})

            # --- product movers -------------------------------------------
            pt = q(f"""
                SELECT category, brand, product,
                       SUM(net_early) AS net_early, SUM(net_late) AS net_late,
                       SUM(net_total) AS net_total
                FROM dash_product_trend
                WHERE category IN ('{ca}','{cb}') {af}
                GROUP BY 1,2,3 ORDER BY net_total DESC LIMIT 20
            """)
            if not pt.empty:
                pt["change"] = pct_change(pt.net_late, pt.net_early)
                st.markdown("**Top products in these categories**")
                st.dataframe(pd.DataFrame({
                    "Category": pt.category,
                    "Brand": pt.brand,
                    "Product": pt["product"],
                    "Net $": pt.net_total.round(0),
                    "Change %": pd.to_numeric(pt.change, errors="coerce").round(1),
                }), use_container_width=True, hide_index=True, column_config={
                    "Net $": st.column_config.NumberColumn(format="$%d"),
                    "Change %": st.column_config.NumberColumn(
                        help="Second half versus first half of the period.",
                        format="%.1f%%"),
                })
                st.markdown('<p class="note">Product names change often as SKUs '
                            'turn over, so read these as examples of where the '
                            'movement sits rather than a stable ranking.</p>',
                            unsafe_allow_html=True)

    inv = q(f"""
        SELECT category, SUM(inv_cost) AS inv_cost, SUM(qoh) AS qoh,
               SUM(skus) AS skus, MAX(snapshot_date) AS snap
        FROM dash_inventory {wf} GROUP BY 1
    """)
    if not inv.empty and inv.inv_cost.sum() > 0:
        st.divider()
        st.markdown("#### Inventory efficiency")
        howto("inventory")
        recent = weeks.tail(4)
        rk = set(zip(recent.iso_year, recent.iso_week))
        s = df[df.apply(lambda r: (r.iso_year, r.iso_week) in rk, axis=1)]
        s = s.groupby("category").agg(net=("net", "sum"), units=("units", "sum"))
        j = s.join(inv.set_index("category"), how="inner").reset_index()
        j["ssi"] = (j.net / j.net.sum()) / (j.inv_cost / j.inv_cost.sum())
        j["days"] = j.qoh / (j.units / 28)
        j = j.sort_values("net", ascending=False)
        st.dataframe(pd.DataFrame({
            "Category": j.category,
            "Net $ (4 wks)": j.net.round(0),
            "Inventory @ cost": j.inv_cost.round(0),
            "SKUs": j.skus,
            "SSI": j.ssi.round(2),
            "Days supply": j.days.round(0),
        }), use_container_width=True, hide_index=True, column_config={
            "Net $ (4 wks)": st.column_config.NumberColumn(
                help="Net sales over the last four weeks.", format="$%d"),
            "Inventory @ cost": st.column_config.NumberColumn(
                help=tip("inventory at cost"), format="$%d"),
            "SKUs": st.column_config.NumberColumn(
                help=tip("skus"), format="%d"),
            "SSI": st.column_config.NumberColumn(
                help=tip("ssi"), format="%.2f"),
            "Days supply": st.column_config.NumberColumn(
                help=tip("days supply"), format="%d"),
        })
        st.markdown(f'<p class="note">Stock position as at {inv.snap.max()}. '
                    f'Sellable stock only — sales floor, vault and day vault.'
                    f'</p>', unsafe_allow_html=True)


# ----------------------------------------------------------------- glossary
with t_gloss:
    st.markdown("#### What the terms mean")
    st.markdown('<p class="note">Everything on this dashboard, in plain '
                'language. Terms with a dotted underline elsewhere show a '
                'short version on hover.</p>', unsafe_allow_html=True)

    groups = [
        ("Money and volume",
         ["net sales", "basket", "average basket", "gross margin", "units"]),
        ("The metrics that matter most",
         ["$/100 baskets", "penetration"]),
        ("Reading the charts",
         ["control limits", "baseline", "run rule", "Δ WoW", "pp"]),
        ("Channels",
         ["channel", "channel index"]),
        ("How categories relate",
         ["co-purchase lift", "substitutes", "affinity"]),
        ("Inventory",
         ["SSI", "days supply", "inventory at cost", "SKUs", "sellable stock"]),
    ]

    for title, terms in groups:
        st.markdown(f"##### {title}")
        for t in terms:
            body = GLOSSARY.get(t.lower(), "")
            if not body:
                continue
            body = body.replace("\n\n", "<br><br>")
            st.markdown(
                f'<div class="gloss-entry"><b>{t}</b><br>'
                f'<span class="gloss-body">{body}</span></div>',
                unsafe_allow_html=True)
        st.markdown("")

    st.divider()
    st.markdown("##### A note on reading any of this")
    st.markdown(
        '<p class="note">These figures describe what happened, not why. A '
        'category falling outside its normal range is a reason to ask a '
        'question, not an answer. The most common causes — a stockout, a '
        'price change, a competitor promotion, a menu reshuffle — do not '
        'appear in this data at all, and someone on the floor will usually '
        'know which one it was.</p>', unsafe_allow_html=True)
