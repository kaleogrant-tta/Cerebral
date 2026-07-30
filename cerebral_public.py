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
import numpy as np
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

  /* Category colours, fixed by name. Plotly otherwise assigns colour by
     order of appearance, so changing the store filter reorders categories
     and silently reassigns every colour — the same category appears green
     in one view and rust in the next. Bound by name, they never move.     */
  --cat-flower:      #2F6F4F;
  --cat-pre-roll:    #B4632B;
  --cat-vape:        #3B6E8F;
  --cat-edible:      #8A5A9E;
  --cat-concentrate: #A8913C;
  --cat-accessory:   #6E7F8D;
  --cat-cbd:         #4C8C7A;
  --cat-tincture:    #9E3B32;
  --cat-topical:     #7A6A55;

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

# Category -> colour, bound by name so it is stable across store filters,
# week windows and category selections.
CATEGORY_ORDER = ["Flower", "Pre-Roll", "Vape", "Edible", "Concentrate",
                  "Accessory", "CBD", "Tincture", "Topical"]
CAT_COLORS = {}
for _i, _c in enumerate(CATEGORY_ORDER):
    _key = "cat-" + _c.lower().replace(" ", "-")
    CAT_COLORS[_c] = PALETTE.get(_key, SERIES[_i % len(SERIES)])


def cat_color(name: str) -> str:
    """Colour for a category, falling back deterministically for anything
    not in the canonical list — a hash, not position, so a new category
    still gets the same colour every time."""
    if name in CAT_COLORS:
        return CAT_COLORS[name]
    return SERIES[hash(str(name)) % len(SERIES)]


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
    # Look next to the script, then one level up (repo root when the app
    # lives in a subfolder, as on Streamlit Cloud), then the launch
    # directory — so the bundled data file always wins over Drive.
    here = Path(__file__).resolve().parent
    for local in (here / DASH_FILE, here.parent / DASH_FILE, Path(DASH_FILE)):
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
        orderBy="modifiedTime desc",
        fields="files(id,name,size,modifiedTime)").execute().get("files", [])
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
    """Run a query, returning an empty frame if the table is not there.

    The app and the published data file are versioned independently: the code
    deploys from git the moment it is pushed, while the data file only changes
    when the scheduled run rebuilds it. So a new tab can go live hours before
    the table it reads exists.

    A missing table should degrade that one section, not take down the whole
    dashboard. Every caller already handles an empty frame, because a store
    filter can legitimately return nothing.
    """
    path = load_db()
    if not path:
        return pd.DataFrame()
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(sql).df()
    except duckdb.CatalogException:
        return pd.DataFrame()
    except Exception:
        raise
    finally:
        con.close()


def table_exists(name: str) -> bool:
    path = load_db()
    if not path:
        return False
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?",
            [name]).fetchone()[0] > 0
    except Exception:
        return False
    finally:
        con.close()


def pct_change(late, early):
    """Percentage change, returning float NaN where there is no baseline.

    Products and brands absent from the first half have zero early sales.
    Dividing by pd.NA yields NAType, which has no __round__ and breaks every
    downstream format call — numpy NaN renders as an empty cell instead.

    Both arguments must be Series. Passing a bound method — which is what
    `frame.ne` or `frame.count` silently gives you — raises here with a clear
    message rather than a dimension error from deep inside pandas.
    """
    import numpy as np
    for label, v in (("late", late), ("early", early)):
        if not isinstance(v, pd.Series):
            raise TypeError(
                f"pct_change({label}=...) expected a Series, got "
                f"{type(v).__name__}. If you used frame.colname, the column "
                f"name collides with a DataFrame method — use "
                f"frame['colname'] instead."
            )
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

df = cw.merge(bw[["iso_year", "iso_week", "baskets", "days_open"]],
              on=["iso_year", "iso_week"])
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

# A trailing partial week is the single most misleading thing a dashboard can
# show: two days against seven reads as a 70% collapse. The week is kept and
# compared PER TRADING DAY, and labelled, rather than dropped — three days of
# data is still information.
_wk_days = (df.groupby(["iso_year", "iso_week"])["days_open"].max()
              .rename("days_open").reset_index())
_last = weeks.iloc[-1]
_last_days = int(_wk_days[(_wk_days.iso_year == _last.iso_year)
                          & (_wk_days.iso_week == _last.iso_week)]
                 ["days_open"].iloc[0])
_typical = int(_wk_days["days_open"].median())
PARTIAL_WEEK = _last_days < _typical
PARTIAL_DAYS = _last_days
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

t_charts, t_insights, t_brands, t_redeem, t_projections, t_promo, t_gloss = st.tabs(
    ["Charts", "Insights", "Brands", "Redemptions", "Projections", "Promo Lab", "What the terms mean"])

# ---------------------------------------------------------------- charts
with t_charts:
    wk = (dfv.groupby(["iso_year", "iso_week", "wk_date"])
             .agg(net=("net", "sum"), baskets=("baskets", "first"),
                  days=("days_open", "max"))
             .reset_index().sort_values("wk_date"))
    wk["atv"] = wk.net / wk.baskets
    wk["net_pd"] = wk.net / wk.days
    wk["bkt_pd"] = wk.baskets / wk.days
    cur = wk.iloc[-1]
    prev = wk.iloc[-2] if len(wk) > 1 else None

    is_partial = PARTIAL_WEEK and len(wk) and cur.days < wk.days.median()

    if is_partial:
        st.markdown(
            f'<div class="alert a-warn">The latest week is still in progress '
            f'— <b>{int(cur.days)} of {int(wk.days.median())} trading days</b>. '
            f'Its totals are therefore lower than a full week, so the headline '
            f'figures below are shown <b>per trading day</b> to stay '
            f'comparable. Charts show actual totals.</div>',
            unsafe_allow_html=True)

    c = st.columns(4)
    if is_partial:
        d_net = (cur.net_pd / prev.net_pd - 1) * 100 if prev is not None else None
        d_bkt = (cur.bkt_pd / prev.bkt_pd - 1) * 100 if prev is not None else None
        c[0].metric("Net sales per day", f"${cur.net_pd:,.0f}",
                    f"{d_net:+.1f}%" if d_net is not None else None,
                    help="Net sales divided by trading days, so a part-finished "
                         "week compares fairly with a complete one. "
                         + tip("net sales"))
        c[1].metric("Baskets per day", f"{cur.bkt_pd:,.0f}",
                    f"{d_bkt:+.1f}%" if d_bkt is not None else None,
                    help="Transactions per trading day. " + tip("basket"))
    else:
        c[0].metric("Net sales, latest week", f"${cur.net:,.0f}",
                    f"{(cur.net/prev.net-1)*100:+.1f}%" if prev is not None else None,
                    help=tip("net sales"))
        c[1].metric("Baskets", f"{int(cur.baskets):,}",
                    f"{(cur.baskets/prev.baskets-1)*100:+.1f}%" if prev is not None else None,
                    help=tip("basket"))
    c[2].metric("Average basket", f"${cur.atv:,.2f}",
                f"{(cur.atv/prev.atv-1)*100:+.1f}%" if prev is not None else None,
                help="Unaffected by a partial week — it is already a per-basket "
                     "figure. " + tip("average basket"))
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
        st.plotly_chart(fig, use_container_width=True, key="pc1")

    with R:
        st.markdown("##### Category share of revenue")
        mix = dfv.groupby("category").net.sum().reset_index().sort_values(
            "net", ascending=False)
        fig = px.pie(mix, values="net", names="category", hole=.58,
                     color="category",
                     color_discrete_map={c: cat_color(c)
                                         for c in mix.category})
        fig.update_traces(textposition="outside", textinfo="percent+label",
                          hovertemplate="%{label}<br>%{value:$,.0f}"
                                        "<extra></extra>")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          showlegend=False)
        st.plotly_chart(fig, use_container_width=True, key="pc2")

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
                      markers=not smooth,
                      category_orders={"category": [c for c in CATEGORY_ORDER
                                                    if c in pick]},
                      color_discrete_map={c: cat_color(c) for c in pick})
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
        st.plotly_chart(fig, use_container_width=True, key="pc3")

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
        st.plotly_chart(fig, use_container_width=True, key="pc4")

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

            # One panel per channel with its own y-axis. In-Store runs near 90%
            # and the others near 5%, so a stacked or shared-axis chart flattens
            # exactly the movement worth seeing.
            share_of = (ch.groupby("channel").baskets.sum()
                          / ch.baskets.sum() * 100).to_dict()
            order_ch = [c for c in ["In-Store", "Non-Stop", "Delivery"]
                        if c in share_of]
            cmap = {"In-Store": ACCENT, "Non-Stop": ACCENT_SOFT,
                    "Delivery": WARN}

            fig = px.line(ch, x="wk_date", y="share", color="channel",
                          facet_row="channel",
                          category_orders={"channel": order_ch},
                          color_discrete_map=cmap)
            fig.update_yaxes(matches=None, showticklabels=True,
                             ticksuffix="%", gridcolor="rgba(0,0,0,.07)",
                             title_text="")
            fig.update_xaxes(gridcolor="rgba(0,0,0,.04)", title_text="")
            fig.update_traces(line=dict(width=2.4))
            fig.for_each_annotation(lambda a: a.update(
                text=a.text.split("=")[-1]
                     + f"  ·  {share_of.get(a.text.split('=')[-1], 0):.0f}% of baskets",
                font=dict(size=12)))
            fig.update_layout(height=420, margin=dict(l=0, r=0, t=24, b=0),
                              showlegend=False, plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, key="pc5")
            st.markdown('<p class="note">Each channel has its own scale, so a '
                        'small channel\'s movement is visible rather than '
                        'flattened by the large one. The label shows how much '
                        'of total volume each carries.</p>',
                        unsafe_allow_html=True)

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
            st.plotly_chart(fig, use_container_width=True, key="pc6")
            howto("channel_grid")

# -------------------------------------------------------------- insights
with t_insights:
    st.markdown("#### Alerts")
    howto("alerts")

    # Alerts run on the last COMPLETE week. Penetration is a ratio so it is
    # not distorted by a short week, but basket counts are, and the control
    # limits are built from those — a two-day week has wide limits and noisy
    # penetration, which produces alarming alerts about nothing.
    alert_weeks = weeks.iloc[:-1] if PARTIAL_WEEK else weeks
    if PARTIAL_WEEK and len(weeks) >= 2:
        _aw = alert_weeks.iloc[-1]
        st.markdown(
            f'<p class="note">Assessed on the last complete week '
            f'({int(_aw.iso_year)}-W{int(_aw.iso_week):02d}). The week in '
            f'progress has too few days for the control limits to mean '
            f'anything.</p>', unsafe_allow_html=True)

    alerts = []
    if len(alert_weeks) >= 5:
        cy, cwk = alert_weeks.iloc[-1]
        hist = alert_weeks.iloc[max(0, len(alert_weeks) - 1 - BASELINE_WEEKS):
                                len(alert_weeks) - 1]
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
    sc_weeks = weeks.iloc[:-1] if PARTIAL_WEEK else weeks
    cy, cwk = sc_weeks.iloc[-1]
    st.markdown(f"#### Category scorecard — {int(cy)}-W{int(cwk):02d}"
                + ("  (last complete week)" if PARTIAL_WEEK else ""))
    howto("scorecard")
    cur = df[(df.iso_year == cy) & (df.iso_week == cwk)].copy()
    if len(sc_weeks) > 1:
        py, pw = sc_weeks.iloc[-2]
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
            # Built from this pair's own brands and figures rather than a
            # template with names substituted in. Every claim below names the
            # brand and the number behind it, so it can be checked.
            a_side = bt[bt.category == ca].sort_values("net_total", ascending=False)
            b_side = bt[bt.category == cb].sort_values("net_total", ascending=False)

            def movers(side, n=2):
                v = side.dropna(subset=["change"])
                up = v[v.change > 10].sort_values("change", ascending=False).head(n)
                dn = v[v.change < -10].sort_values("change").head(n)
                return up, dn

            def name_list(frame, with_cat=False):
                """Brand names with their trend. Include the category when the
                list mixes both sides — a brand can sell in two categories with
                different trajectories, and unlabelled it reads as a
                contradiction."""
                parts = []
                for _, r in frame.iterrows():
                    tag = f" in {r.category}" if with_cat else ""
                    gone = " — now at zero" if r.change <= -99 else ""
                    parts.append(f"**{r.brand}**{tag} ({r.change:+.0f}%{gone})")
                return ", ".join(parts)

            a_up, a_dn = movers(a_side)
            b_up, b_dn = movers(b_side)
            a_chg, b_chg = a_side.change.median(), b_side.change.median()
            a_top = a_side.iloc[0] if len(a_side) else None
            b_top = b_side.iloc[0] if len(b_side) else None

            # strongest co-purchased brand combination, for affinity reads
            bp_top = q(f"""
                SELECT brand_a, brand_b, SUM(joint_baskets) AS baskets
                FROM dash_brand_pairs
                WHERE cat_a = '{ca}' AND cat_b = '{cb}' {af}
                GROUP BY 1,2 ORDER BY baskets DESC LIMIT 1
            """)

            lines = []

            if read == "substitutes":
                strength = ("strongly" if lv < 0.5 else
                            "moderately" if lv < 0.62 else "mildly")
                lines.append(
                    f"Customers {strength} choose between **{ca}** and "
                    f"**{cb}** rather than buying both (lift {lv:.2f}). Demand "
                    f"is more likely to move between them than to disappear if "
                    f"you cut one.")

                if pd.notna(a_chg) and pd.notna(b_chg) and abs(a_chg - b_chg) > 15:
                    up_cat, dn_cat = (cb, ca) if b_chg > a_chg else (ca, cb)
                    up_side = b_up if b_chg > a_chg else a_up
                    dn_side = a_dn if b_chg > a_chg else b_dn
                    detail = ""
                    if len(up_side):
                        detail += (f" The movement is concentrated in "
                                   f"{name_list(up_side)} within {up_cat}.")
                    if len(dn_side):
                        detail += (f" On the {dn_cat} side, {name_list(dn_side)} "
                                   f"{'are' if len(dn_side) > 1 else 'is'} "
                                   f"giving ground.")
                    lines.append(
                        f"Demand is shifting toward **{up_cat}**.{detail} Weight "
                        f"the buy that way rather than carrying full depth in "
                        f"both — you are currently funding two ranges to serve "
                        f"one customer decision.")
                else:
                    lines.append(
                        f"Neither side is clearly winning — {ca} is moving "
                        f"{a_chg:+.0f}% and {cb} {b_chg:+.0f}%. Both are earning "
                        f"their place, so avoid promoting them against each "
                        f"other in the same week: you would be discounting a "
                        f"sale you were already getting.")

                if a_top is not None and b_top is not None:
                    lines.append(
                        f"Before cutting depth, check the brand table. "
                        f"**{a_top.brand}** carries ${a_top.net_total:,.0f} of "
                        f"{ca} and **{b_top.brand}** ${b_top.net_total:,.0f} of "
                        f"{cb} — a category-level signal usually comes down to "
                        f"a handful of brands while the rest are unaffected.")

            elif read == "affinity":
                lines.append(
                    f"**{ca}** and **{cb}** get bought together "
                    f"{'far ' if lv > 1.8 else ''}more than chance predicts "
                    f"(lift {lv:.2f}).")
                if len(bp_top):
                    r0 = bp_top.iloc[0]
                    lines.append(
                        f"The strongest combination is **{r0.brand_a}** with "
                        f"**{r0.brand_b}** — {int(r0.baskets):,} transactions "
                        f"contained both. That is the obvious bundle: it "
                        f"formalises what customers already do rather than "
                        f"trying to create a new habit.")
                lines.append(
                    f"Treat par levels as linked. A stockout in {ca} now costs "
                    f"you {cb} sales as well, so they should not be ordered "
                    f"independently.")
                growing = pd.concat([a_up.head(1), b_up.head(1)])
                if len(growing):
                    lines.append(
                        f"{name_list(growing, with_cat=True)} "
                        f"{'are' if len(growing) > 1 else 'is'} growing — the "
                        f"strongest candidates to feature in the pairing.")

            else:
                lines.append(
                    f"**{ca}** and **{cb}** have no meaningful relationship "
                    f"(lift {lv:.2f}). Buying, pricing and merchandising "
                    f"decisions on one carry no implication for the other.")
                interesting = pd.concat([a_up.head(1), a_dn.head(1),
                                         b_up.head(1), b_dn.head(1)])
                if len(interesting):
                    lines.append(
                        f"Within them, though, "
                        f"{name_list(interesting, with_cat=True)} "
                        f"{'are' if len(interesting) > 1 else 'is'} moving "
                        f"enough to be worth a separate look.")

            n_bask = int(row.baskets)
            lines.append(
                f"*Based on {n_bask:,} transactions containing both categories.*"
                + ("  Confidence is limited at this volume." if n_bask < 300 else ""))

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


# ------------------------------------------------------------------- brands
with t_brands:
    st.markdown("#### Brand scorecard")
    st.markdown('<div class="howto"><b>How to read this.</b> Revenue tells you '
                'what a brand sells. It does not tell you whether the brand '
                '<i>brought the customer in</i> or was simply bought by '
                'someone already coming.<br><br>'
                'That distinction is what this table adds. A brand appearing '
                'in many first-ever baskets is doing acquisition work — it is '
                'worth more to the business than its sales line suggests. A '
                'brand bought mostly by established customers is riding '
                'traffic you already had.</div>', unsafe_allow_html=True)

    bs = q(f"""
        SELECT brand,
               MIN(primary_category)            AS category,
               SUM(net)                         AS net,
               SUM(gm)                          AS gm,
               SUM(units)                       AS units,
               SUM(skus)                        AS skus,
               SUM(first_basket_customers)      AS first_basket,
               SUM(established_customers)       AS established
        FROM dash_brand_scorecard {wf}
        GROUP BY 1
    """)

    if bs.empty:
        st.info("No brand data in the published file.")
    else:
        bt_all = q(f"""
            SELECT brand, SUM(net_early) AS net_early,
                   SUM(net_late) AS net_late
            FROM dash_brand_trend {wf} GROUP BY 1
        """)
        # Bracket access, not attribute access: a column named `ne` would
        # resolve to DataFrame.ne (the not-equal method) and silently pass a
        # bound method into pct_change.
        bt_all["trend"] = pct_change(bt_all["net_late"], bt_all["net_early"])
        bs = bs.merge(bt_all[["brand", "trend"]], on="brand", how="left")

        min_net = st.slider("Minimum revenue to include", 1000, 100000, 5000,
                            step=1000, format="$%d",
                            help="Small brands produce unstable ratios. Raise "
                                 "this to focus on brands with enough volume "
                                 "to rank meaningfully.")
        bs = bs[bs.net >= min_net].copy()

        if bs.empty:
            st.info("No brands above that revenue threshold.")
        else:
            bs["margin"] = pd.to_numeric(bs.gm, errors="coerce") / \
                pd.to_numeric(bs.net, errors="coerce").replace(0, float("nan")) * 100
            bs["acq_share"] = bs.first_basket / max(bs.first_basket.sum(), 1) * 100

            est_total = bs.established.sum()
            has_tenure = est_total > 0
            if has_tenure:
                bs["acq_ratio"] = (bs.first_basket /
                                   bs.established.replace(0, float("nan")))
            else:
                bs["acq_ratio"] = float("nan")
                st.warning(
                    "The loaded window is too short to tell new customers from "
                    "established ones — nobody in it is yet 90 days past their "
                    "first purchase. Acquisition ratio is hidden until there is "
                    "more history. Everything else below is valid.")

            def z(col):
                x = pd.to_numeric(bs[col], errors="coerce").fillna(0)
                sd = x.std(ddof=0)
                return (x - x.mean()) / sd if sd else x * 0

            weights = {"net": .30, "acq_share": .25, "acq_ratio": .20,
                       "margin": .15, "trend": .10}
            if not has_tenure:                     # redistribute acq_ratio
                weights["net"] += .10
                weights["acq_share"] += .10
                weights["acq_ratio"] = 0.0
            bs["score"] = sum(w * z(c) for c, w in weights.items() if w > 0)
            bs = bs.sort_values("score", ascending=False).reset_index(drop=True)

            cut_first = bs.score.quantile(.80)
            cut_biz = bs.score.quantile(.50)
            bs["tier"] = bs.score.apply(
                lambda v: "First" if v >= cut_first else
                          ("Business" if v >= cut_biz else "Economy"))

            k = st.columns(4)
            k[0].metric("Brands ranked", f"{len(bs):,}")
            k[1].metric("First tier", f"{(bs.tier == 'First').sum()}",
                        help="Top quintile by composite score.")
            k[2].metric("Business tier", f"{(bs.tier == 'Business').sum()}")
            k[3].metric("Economy tier", f"{(bs.tier == 'Economy').sum()}")

            cols = {
                "Brand": bs.brand,
                "Category": bs.category,
                "Suggested tier": bs.tier,
                "Net $": bs.net.round(0),
                "Margin %": bs.margin.round(1),
                "Trend %": pd.to_numeric(bs.trend, errors="coerce").round(1),
                "First-basket customers": bs.first_basket,
                "% of all first baskets": bs.acq_share.round(2),
            }
            if has_tenure:
                cols["Acquisition ratio"] = bs.acq_ratio.round(2)
            cols["Score"] = bs.score.round(2)

            st.dataframe(pd.DataFrame(cols), use_container_width=True,
                         hide_index=True, column_config={
                "Suggested tier": st.column_config.TextColumn(
                    help="First is the top 20% by score, Business the next 30%, "
                         "Economy the rest. A starting point for the tier "
                         "conversation, not a decision."),
                "Net $": st.column_config.NumberColumn(
                    help="Net sales across the loaded period.", format="$%d"),
                "Margin %": st.column_config.NumberColumn(
                    help=tip("gross margin"), format="%.1f%%"),
                "Trend %": st.column_config.NumberColumn(
                    help="Second half of the period versus the first half.",
                    format="%.1f%%"),
                "First-basket customers": st.column_config.NumberColumn(
                    help="Customers whose first-ever purchase here included "
                         "this brand. The clearest evidence a brand pulls "
                         "people in rather than being picked up by regulars.",
                    format="%d"),
                "% of all first baskets": st.column_config.NumberColumn(
                    help="This brand's share of all first-basket appearances. "
                         "Compare against its share of revenue — a brand well "
                         "above its revenue share is punching up on "
                         "acquisition.", format="%.2f%%"),
                "Acquisition ratio": st.column_config.NumberColumn(
                    help="First-basket customers divided by customers who are "
                         "90+ days established. Above 1.0 means the brand "
                         "over-indexes on new customers; below 0.75 means it "
                         "is bought mainly by regulars.", format="%.2f"),
                "Score": st.column_config.NumberColumn(
                    help="Composite: 30% revenue, 25% share of first baskets, "
                         "20% acquisition ratio, 15% margin, 10% trend. "
                         "Standardised, so 0 is average and ±1 is one standard "
                         "deviation.", format="%.2f"),
            })

            st.divider()
            st.markdown("##### Acquisition against revenue")
            st.markdown('<p class="note">Brands above the diagonal contribute '
                        'more to acquisition than their revenue would predict. '
                        'Those are the ones worth protecting on shelf and '
                        'pricing keenly, whatever their sales rank.</p>',
                        unsafe_allow_html=True)
            plot = bs.copy()
            plot["rev_share"] = plot.net / plot.net.sum() * 100
            fig = px.scatter(plot, x="rev_share", y="acq_share",
                             size="net", color="tier", hover_name="brand",
                             color_discrete_map={"First": ACCENT,
                                                 "Business": ACCENT_SOFT,
                                                 "Economy": MUTED},
                             size_max=34)
            lim = max(plot.rev_share.max(), plot.acq_share.max()) * 1.05
            fig.add_shape(type="line", x0=0, y0=0, x1=lim, y1=lim,
                          line=dict(color=MUTED, dash="dot", width=1))
            fig.update_layout(height=440, margin=dict(l=0, r=0, t=10, b=0),
                              xaxis_title="% of revenue",
                              yaxis_title="% of first-basket appearances",
                              legend=dict(orientation="h", y=1.1, x=0,
                                          title_text=""),
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_xaxes(gridcolor="rgba(0,0,0,.07)", ticksuffix="%")
            fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", ticksuffix="%")
            st.plotly_chart(fig, use_container_width=True, key="pc7")

            over = plot[plot.acq_share > plot.rev_share * 1.25].nlargest(
                5, "acq_share")
            under = plot[plot.rev_share > plot.acq_share * 1.25].nlargest(
                5, "rev_share")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Punching above their weight on acquisition**")
                if len(over):
                    for _, r in over.iterrows():
                        st.markdown(
                            f'<div class="alert a-ok"><b>{r.brand}</b> — '
                            f'{r.acq_share:.1f}% of first baskets on '
                            f'{r.rev_share:.1f}% of revenue</div>',
                            unsafe_allow_html=True)
                else:
                    st.markdown('<p class="note">None stand out.</p>',
                                unsafe_allow_html=True)
            with c2:
                st.markdown("**Selling well, acquiring little**")
                if len(under):
                    for _, r in under.iterrows():
                        st.markdown(
                            f'<div class="alert a-warn"><b>{r.brand}</b> — '
                            f'{r.rev_share:.1f}% of revenue on '
                            f'{r.acq_share:.1f}% of first baskets</div>',
                            unsafe_allow_html=True)
                else:
                    st.markdown('<p class="note">None stand out.</p>',
                                unsafe_allow_html=True)

            st.markdown('<p class="note">Neither list is a verdict. A brand '
                        'bought mainly by regulars may be exactly why those '
                        'regulars keep coming — that is retention value, and '
                        'it is real. The point is that the two roles are '
                        'different and should not be priced identically.</p>',
                        unsafe_allow_html=True)

    # ------------------------------------------------------ brand deep dive
    st.divider()
    st.markdown("#### Brand deep dive")
    st.markdown('<div class="howto"><b>How to read this.</b> Pick a brand and '
                'see the company it keeps. <b>Where its revenue sits</b> shows '
                'which categories the brand actually wins in and which way '
                'each is moving. <b>Bought together</b> lists the other '
                'brands most often found in the same transactions — your '
                'readiest bundle and cross-promo candidates. <b>What it '
                'pulls along</b> rolls those partners up by category, which '
                'is the buying read: a stockout on this brand costs you '
                'sales there too.</div>', unsafe_allow_html=True)

    bl = q(f"""
        SELECT brand, SUM(net) AS net
        FROM dash_brand_scorecard {wf}
        GROUP BY 1 HAVING SUM(net) >= 1000
        ORDER BY net DESC
    """)

    if bl.empty:
        st.info("No brand data in the published file.")
    else:
        pick = st.selectbox(
            "Pick a brand", bl.brand.tolist(), key="deep_brand",
            help="Brands with at least $1,000 net sales in the loaded "
                 "period, largest first. Type to search.")
        psql = pick.replace("'", "''")            # safe inside SQL literals

        def num0(v):
            v = pd.to_numeric(v, errors="coerce")
            return float(v) if pd.notna(v) else 0.0

        # ---- snapshot ---------------------------------------------------
        sc = q(f"""
            SELECT SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
                   SUM(first_basket_customers) AS first_basket
            FROM dash_brand_scorecard
            WHERE brand = '{psql}' {af}
        """).iloc[0]
        tr = q(f"""
            SELECT SUM(net_early) AS e, SUM(net_late) AS l
            FROM dash_brand_trend
            WHERE brand = '{psql}' {af}
        """).iloc[0]

        net, gm = num0(sc.net), num0(sc.gm)
        e, l = num0(tr.e), num0(tr.l)
        trend = (l / e - 1) * 100 if e > 0 else float("nan")

        m = st.columns(4)
        m[0].metric("Net sales", f"${net:,.0f}")
        m[1].metric("Gross margin",
                    f"{gm / net * 100:.1f}%" if net > 0 else "—",
                    help=tip("gross margin"))
        m[2].metric("Units", f"{int(num0(sc.units)):,}")
        m[3].metric("Trend (2nd half vs 1st)",
                    f"{trend:+.1f}%" if pd.notna(trend) else "—",
                    help="Net sales in the second half of the loaded period "
                         "versus the first half.")
        fb = int(num0(sc.first_basket))
        if fb:
            st.markdown(f'<p class="note"><b>{fb:,}</b> customers had '
                        f'<b>{pick}</b> in their first-ever basket — '
                        f'acquisition value on top of the sales line.</p>',
                        unsafe_allow_html=True)

        # ---- category mix + co-purchases ---------------------------------
        cm = q(f"""
            SELECT category,
                   SUM(net_total) AS net,
                   SUM(net_early) AS net_early,
                   SUM(net_late)  AS net_late
            FROM dash_brand_trend
            WHERE brand = '{psql}' {af}
            GROUP BY 1 HAVING SUM(net_total) > 0
            ORDER BY net DESC
        """)

        bp = q(f"""
            SELECT other, other_cat, SUM(baskets) AS baskets FROM (
                SELECT brand_b AS other, cat_b AS other_cat,
                       SUM(joint_baskets) AS baskets
                FROM dash_brand_pairs
                WHERE brand_a = '{psql}' {af}
                GROUP BY 1, 2
                UNION ALL
                SELECT brand_a, cat_a, SUM(joint_baskets)
                FROM dash_brand_pairs
                WHERE brand_b = '{psql}' {af}
                GROUP BY 1, 2
            ) u
            WHERE other <> '{psql}'
            GROUP BY 1, 2
        """)
        if not bp.empty:
            bp = bp.rename(columns={"other": "partner",
                                    "other_cat": "partner_cat"})

        left, right = st.columns(2)

        with left:
            st.markdown("##### Where its revenue sits")
            if cm.empty:
                st.info("No category detail for this brand in the loaded "
                        "period.")
            else:
                cm["share"] = cm.net / cm.net.sum() * 100
                cm["change"] = pct_change(cm["net_late"], cm["net_early"])
                cplot = cm.sort_values("net")
                fig = px.bar(cplot, x="net", y="category", orientation="h",
                             color="category",
                             color_discrete_map={c: cat_color(c)
                                                 for c in cplot.category},
                             text=cplot["share"].map("{:.0f}%".format))
                fig.update_traces(textposition="outside", cliponaxis=False)
                fig.update_layout(height=90 + 46 * len(cplot),
                                  showlegend=False,
                                  margin=dict(l=0, r=0, t=10, b=0),
                                  xaxis_title="", yaxis_title="",
                                  plot_bgcolor="rgba(0,0,0,0)")
                fig.update_xaxes(showticklabels=False)
                st.plotly_chart(fig, use_container_width=True, key="deep_cm")
                st.caption("Bar labels are each category's share of the "
                           "brand's net sales.")

                st.dataframe(pd.DataFrame({
                    "Category": cm.category,
                    "Net $": cm.net.round(0),
                    "% of brand": cm["share"].round(1),
                    "Change %": pd.to_numeric(cm["change"],
                                              errors="coerce").round(1),
                }), use_container_width=True, hide_index=True, column_config={
                    "Net $": st.column_config.NumberColumn(format="$%d"),
                    "% of brand": st.column_config.NumberColumn(
                        help="Share of this brand's net sales.",
                        format="%.1f%%"),
                    "Change %": st.column_config.NumberColumn(
                        help="Second half of the period versus the first. "
                             "Shows which way the brand is moving inside "
                             "each category.", format="%.1f%%"),
                })

        with right:
            st.markdown("##### Bought together")
            if bp.empty:
                st.info("No co-purchase data for this brand — either it is "
                        "usually bought alone, or the published file "
                        "predates brand-pair tracking.")
            else:
                tot = (bp.groupby("partner", as_index=False)
                         .agg(baskets=("baskets", "sum")))
                # a partner's category is the one where it co-occurs with
                # the picked brand most often
                pcat = (bp.sort_values("baskets", ascending=False)
                          .drop_duplicates("partner")
                          .set_index("partner")["partner_cat"])
                tot["category"] = tot.partner.map(pcat)
                tot = tot.sort_values("baskets", ascending=False)
                tot["share"] = tot.baskets / tot.baskets.sum() * 100

                for _, r in tot.head(3).iterrows():
                    st.markdown(
                        f'<div class="alert a-ok"><b>{pick} + '
                        f'{r.partner}</b> — {int(r.baskets):,} transactions '
                        f'contained both ({r.share:.1f}% of all {pick} '
                        f'pairings)</div>', unsafe_allow_html=True)

                show = tot.head(12)
                st.dataframe(pd.DataFrame({
                    "Partner brand": show.partner,
                    "Category": show.category,
                    "Baskets together": show.baskets,
                    "% of pairings": show["share"].round(1),
                }), use_container_width=True, hide_index=True, column_config={
                    "Baskets together": st.column_config.NumberColumn(
                        help="Transactions containing both brands.",
                        format="%d"),
                    "% of pairings": st.column_config.NumberColumn(
                        help="Of all transactions where this brand appeared "
                             "alongside any other brand, how often it was "
                             "this one.", format="%.1f%%"),
                })
                st.markdown('<p class="note">Partners from other categories '
                            'are cross-sells — bundle and merchandise them '
                            'together. Brands from this brand\'s own '
                            'category that never appear here are likely '
                            'substitutes: customers choose between you, not '
                            'in addition to you.</p>', unsafe_allow_html=True)

        # ---- category pull, full width -----------------------------------
        if not bp.empty:
            st.markdown("##### What it pulls along")
            st.markdown('<p class="note">The partner brands above, rolled '
                        'up by category: when this brand is in the basket, '
                        'these are the aisles the rest of the basket comes '
                        'from. Treat par levels as linked — running out '
                        'here costs sales there.</p>',
                        unsafe_allow_html=True)
            bycat = (bp.groupby("partner_cat", as_index=False)
                       .agg(baskets=("baskets", "sum"))
                       .sort_values("baskets", ascending=False))
            fig = px.bar(bycat, x="partner_cat", y="baskets",
                         color="partner_cat",
                         color_discrete_map={c: cat_color(c)
                                             for c in bycat.partner_cat})
            fig.update_layout(height=320, showlegend=False,
                              margin=dict(l=0, r=0, t=10, b=0),
                              xaxis_title="",
                              yaxis_title="baskets together",
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_yaxes(gridcolor="rgba(0,0,0,.07)")
            st.plotly_chart(fig, use_container_width=True, key="deep_pull")



# -------------------------------------------------------------- redemptions
with t_redeem:
    st.markdown("#### Loyalty Redemptions")
    st.markdown('<div class="howto"><b>How to read this tab.</b> '
                'Redemptions are discounts given back to loyalty members. '
                'A high redemption rate means many customers are using their '
                'points — usually good, because engaged members buy more often. '
                'But watch the cost: if redemption dollars grow faster than '
                'net sales, the program is eating margin. The sweet spot is '
                'high redemption rate with low redemption cost as a percent '
                'of revenue, and high sales per redeeming basket.</div>',
                unsafe_allow_html=True)

    # --- Store-level redemption analytics (from dash_basket_week) ---
    red = q(f"""
        SELECT iso_year, iso_week,
               SUM(baskets) AS baskets,
               SUM(redeem_baskets) AS redeem_baskets,
               SUM(redeem_value) AS redeem_value,
               SUM(net) AS net
        FROM dash_basket_week {wf}
        GROUP BY 1,2
        ORDER BY 1,2
    """)
    red_store = q(f"""
        SELECT store_key,
               SUM(baskets) AS baskets,
               SUM(redeem_baskets) AS redeem_baskets,
               SUM(redeem_value) AS redeem_value,
               SUM(net) AS net
        FROM dash_basket_week {wf}
        GROUP BY 1
    """)

    if red.empty or red.redeem_baskets.sum() == 0:
        st.info("No redemption data in the published file.")
    else:
        red["wk_date"] = pd.to_datetime(
            red.iso_year.astype(str) + "-W" + red.iso_week.astype(str).str.zfill(2) + "-1",
            format="%G-W%V-%u", errors="coerce")
        red = red.sort_values("wk_date").reset_index(drop=True)
        red["redeem_rate"] = red.redeem_baskets / red.baskets.replace(0, float("nan"))
        red["redeem_per_basket"] = red.redeem_value / red.baskets.replace(0, float("nan"))
        red["redeem_pct_of_net"] = red.redeem_value / red.net.replace(0, float("nan"))

        # Use latest week with actual redemption activity for metrics
        red_active = red[red.redeem_baskets > 0].copy()
        if len(red_active) >= 2:
            cur = red_active.iloc[-1]
            prev = red_active.iloc[-2]
        elif len(red_active) == 1:
            cur = red_active.iloc[-1]
            prev = None
        else:
            cur = red.iloc[-1]
            prev = red.iloc[-2] if len(red) > 1 else None

        def fmt_delta(cur_val, prev_val):
            if prev_val is None or pd.isna(prev_val) or prev_val == 0:
                return None
            return f"{(cur_val/prev_val-1)*100:+.1f}%"

        c = st.columns(4)
        c[0].metric("Redemption Rate",
                    f"{cur.redeem_rate*100:.1f}%" if pd.notna(cur.redeem_rate) and cur.redeem_rate > 0 else "—",
                    fmt_delta(cur.redeem_rate, prev.redeem_rate) if prev is not None else None,
                    help="Share of baskets that included a loyalty redemption.")
        c[1].metric("Redemption Value",
                    f"${cur.redeem_value:,.0f}" if pd.notna(cur.redeem_value) and cur.redeem_value > 0 else "—",
                    fmt_delta(cur.redeem_value, prev.redeem_value) if prev is not None else None,
                    help="Total value redeemed in the latest active week.")
        c[2].metric("Avg Redemption / Basket",
                    f"${cur.redeem_per_basket:.2f}" if pd.notna(cur.redeem_per_basket) and cur.redeem_per_basket > 0 else "—",
                    fmt_delta(cur.redeem_per_basket, prev.redeem_per_basket) if prev is not None else None,
                    help="Average discount per total basket.")
        c[3].metric("Redemptions as % of Net",
                    f"{cur.redeem_pct_of_net*100:.1f}%" if pd.notna(cur.redeem_pct_of_net) and cur.redeem_pct_of_net > 0 else "—",
                    fmt_delta(cur.redeem_pct_of_net, prev.redeem_pct_of_net) if prev is not None else None,
                    help="Redemption value divided by net sales.")

        st.divider()
        L, R = st.columns([3, 2])

        with L:
            heading("Redemption value and rate by week")
            st.markdown('<p class="note">The bars show total dollars redeemed; '
                        'the line shows what share of baskets included a '
                        'redemption.</p>', unsafe_allow_html=True)
            fig = go.Figure()
            fig.add_bar(x=red.wk_date, y=red.redeem_value, name="Redemption $",
                        marker_color=ACCENT, opacity=.75)
            fig.add_scatter(x=red.wk_date, y=red.redeem_rate*100, name="Redemption rate %",
                            yaxis="y2", line=dict(color=WARN, width=2))
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis=dict(title="Redemption $", tickformat="$,.0s",
                                         gridcolor="rgba(0,0,0,.07)"),
                              yaxis2=dict(title="Rate %", overlaying="y", side="right",
                                          showgrid=False, tickformat=".1f",
                                          ticksuffix="%"),
                              hovermode="x unified",
                              legend=dict(orientation="h", y=1.12, x=0,
                                          title_text=""),
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, key="pc8")

        with R:
            st.markdown("##### Redemption by store")
            if len(keys) > 1 and not red_store.empty:
                red_store["store"] = red_store.store_key.map(STORES)
                red_store["redeem_rate"] = red_store.redeem_baskets / red_store.baskets.replace(0, float("nan"))
                red_store = red_store.sort_values("redeem_value", ascending=False)
                red_store["label"] = red_store.apply(
                    lambda r: f"${r.redeem_value/1e3:.0f}k<br>({r.redeem_rate*100:.1f}%)", axis=1)
                fig = px.bar(red_store, x="store", y="redeem_value",
                             color="redeem_rate", color_continuous_scale="Greens",
                             text="label")
                fig.update_traces(textposition="outside", textfont_size=11,
                                  cliponaxis=False)
                fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis_title="Redemption $", xaxis_title="",
                                  coloraxis_colorbar=dict(title="Rate %",
                                                          ticksuffix="%"),
                                  plot_bgcolor="rgba(0,0,0,0)")
                fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$,.0s")
                st.plotly_chart(fig, use_container_width=True, key="pc9")
                st.markdown('<p class="note"><b>Bar height</b> = total redemption '
                            'dollars. <b>Color</b> = redemption rate (darker green '
                            '= higher engagement).</p>',
                            unsafe_allow_html=True)
            else:
                st.markdown('<p class="note">Select multiple stores in the '
                            'sidebar to compare redemption performance.</p>',
                            unsafe_allow_html=True)

        st.divider()

        # --- Store scorecard ---------------------------------------------
        if not red_store.empty and len(keys) > 1:
            st.markdown("#### Store redemption scorecard")
            st.markdown('<div class="howto"><b>How to read the scorecard.</b> '
                        '<b>Redemption Rate</b> is engagement. '
                        '<b>Redemption Cost %</b> is the true price of the program. '
                        '<b>Sales / Redeem Bkt</b> is the ROI proxy.</div>',
                        unsafe_allow_html=True)

            red_store["redeem_rate"] = red_store.redeem_baskets / red_store.baskets.replace(0, float("nan"))
            red_store["redeem_pct_of_net"] = red_store.redeem_value / red_store.net.replace(0, float("nan"))
            red_store["sales_per_redeem_basket"] = red_store.net / red_store.redeem_baskets.replace(0, float("nan"))

            by_rate = red_store.sort_values("redeem_rate", ascending=False)
            by_roi = red_store.sort_values("sales_per_redeem_basket", ascending=False)

            h1, h2, h3, h4 = st.columns(4)
            with h1:
                top = by_rate.iloc[0]
                st.markdown(f'<div class="alert a-ok"><b>Most redeemed</b><br>'
                            f'{STORES.get(top.store_key, top.store_key)} — '
                            f'{top.redeem_rate*100:.1f}%</div>',
                            unsafe_allow_html=True)
            with h2:
                bot = by_rate.iloc[-1]
                st.markdown(f'<div class="alert a-warn"><b>Least redeemed</b><br>'
                            f'{STORES.get(bot.store_key, bot.store_key)} — '
                            f'{bot.redeem_rate*100:.1f}%</div>',
                            unsafe_allow_html=True)
            with h3:
                top_roi = by_roi.iloc[0]
                st.markdown(f'<div class="alert a-ok"><b>Best ROI</b><br>'
                            f'{STORES.get(top_roi.store_key, top_roi.store_key)} — '
                            f'${top_roi.sales_per_redeem_basket:.0f} / redeem bkt</div>',
                            unsafe_allow_html=True)
            with h4:
                bot_roi = by_roi.iloc[-1]
                st.markdown(f'<div class="alert a-bad"><b>Worst ROI</b><br>'
                            f'{STORES.get(bot_roi.store_key, bot_roi.store_key)} — '
                            f'${bot_roi.sales_per_redeem_basket:.0f} / redeem bkt</div>',
                            unsafe_allow_html=True)

            st.dataframe(pd.DataFrame({
                "Store": red_store.store_key.map(STORES),
                "Baskets": red_store.baskets,
                "Redeem Baskets": red_store.redeem_baskets,
                "Redemption $": red_store.redeem_value.round(0),
                "Net $": red_store.net.round(0),
                "Redemption Rate %": (red_store.redeem_rate * 100).round(1),
                "Redemption Cost %": (red_store.redeem_pct_of_net * 100).round(1),
                "Sales / Redeem Bkt": red_store.sales_per_redeem_basket.round(0),
            }), use_container_width=True, hide_index=True, column_config={
                "Redemption $": st.column_config.NumberColumn(format="$%d"),
                "Net $": st.column_config.NumberColumn(format="$%d"),
                "Redemption Rate %": st.column_config.NumberColumn(format="%.1f%%"),
                "Redemption Cost %": st.column_config.NumberColumn(format="%.1f%%"),
                "Sales / Redeem Bkt": st.column_config.NumberColumn(format="$%d"),
            })

        st.divider()
        heading("Redemptions as % of net sales")
        st.markdown('<p class="note">A steady 3-5% is healthy. A spike to 8-10% '
                    'means either a major promotion or runaway point accumulation. '
                    'A decline below 2% means customers are not engaging.</p>',
                    unsafe_allow_html=True)
        fig = px.line(red, x="wk_date", y=red.redeem_pct_of_net*100,
                      markers=True, color_discrete_sequence=[WARN])
        fig.update_traces(line=dict(width=2.5), marker=dict(size=6),
                          hovertemplate="%{y:.1f}%<extra></extra>")
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="% of net sales", xaxis_title="",
                          hovermode="x unified", showlegend=False,
                          plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", ticksuffix="%",
                         zeroline=False)
        fig.update_xaxes(gridcolor="rgba(0,0,0,.04)")
        st.plotly_chart(fig, use_container_width=True, key="pc10")

    # --- Brand redemption section (from original author, when table exists) ---
    st.divider()
    st.markdown("#### Brand performance in redeeming baskets")
    st.markdown('<div class="howto"><b>How to read this.</b> Alpine records a '
                'redemption against a basket, but its offers are named after '
                'the product they discount. Matching the offer name to the '
                'basket contents recovers which brand the money was spent '
                'on.<br><br>'
                'The column that matters most is <b>first-visit redeemers</b>. '
                'An offer redeemed by someone on their first-ever visit bought '
                'you a customer. One redeemed by a regular discounted a sale '
                'you were already getting. Both can be worth doing — but they '
                'are different purchases.</div>', unsafe_allow_html=True)

    rd = q(f"""
        SELECT brand, category,
               SUM(redemptions)            AS redemptions,
               SUM(redeem_value)           AS spend,
               SUM(redeemers)              AS redeemers,
               SUM(first_visit_redeemers)  AS first_visit,
               SUM(established_redeemers)  AS established,
               AVG(avg_basket)             AS avg_basket
        FROM dash_brand_redemption {wf}
        GROUP BY 1,2
    """)

    if not table_exists("dash_brand_redemption"):
        st.info("The published data file predates this tab. It will populate "
                "after the next scheduled refresh rebuilds it.")
    elif rd.empty or rd.spend.sum() == 0:
        st.info("No redemption data yet. Redemption attribution runs during "
                "the ETL, so periods loaded before it existed do not have it "
                "— a full reload populates them.")
    else:
        attributed = rd[rd.brand.notna()]
        unattributed = rd[rd.brand.isna()]
        total = rd.spend.sum()

        k = st.columns(4)
        k[0].metric("Total redeemed", f"${total:,.0f}",
                    help="Value of loyalty offers redeemed in the loaded period.")
        k[1].metric("Redemptions", f"{int(rd.redemptions.sum()):,}")
        k[2].metric("Attributed to a brand",
                    f"{attributed.spend.sum()/total*100:.0f}%",
                    help="The rest could not be matched to a product with confidence.")
        fv = attributed.first_visit.sum()
        est = attributed.established.sum()
        k[3].metric("First-visit redeemers", f"{int(fv):,}",
                    help="Customers who redeemed on their first-ever visit.")

        if est > 0:
            share_new = fv / max(fv + est, 1) * 100
            tone = "a-ok" if share_new > 40 else "a-warn"
            st.markdown(
                f'<div class="alert {tone}">Of redeemers whose tenure is '
                f'known, <b>{share_new:.0f}%</b> were on their first visit and '
                f'<b>{100-share_new:.0f}%</b> were established 90+ days. '
                f'{"Weighted toward acquisition." if share_new > 40 else "Weighted toward customers who were already returning."}'
                f'</div>', unsafe_allow_html=True)

        st.divider()
        st.markdown("##### By brand")
        a = attributed.groupby("brand", as_index=False).agg(
            category=("category", "first"), redemptions=("redemptions", "sum"),
            spend=("spend", "sum"), redeemers=("redeemers", "sum"),
            first_visit=("first_visit", "sum"),
            established=("established", "sum"),
            avg_basket=("avg_basket", "mean")).sort_values("spend", ascending=False)
        a["cost_per_redeemer"] = a.spend / a.redeemers.replace(0, float("nan"))

        st.dataframe(pd.DataFrame({
            "Brand": a.brand,
            "Category": a.category,
            "Redemptions": a.redemptions,
            "Spend $": a.spend.round(0),
            "Redeemers": a.redeemers,
            "First-visit": a.first_visit,
            "Established": a.established,
            "Cost per redeemer": a.cost_per_redeemer.round(2),
            "Avg basket": a.avg_basket.round(2),
        }), use_container_width=True, hide_index=True, column_config={
            "Redemptions": st.column_config.NumberColumn(format="%d"),
            "Spend $": st.column_config.NumberColumn(format="$%d"),
            "Redeemers": st.column_config.NumberColumn(format="%d"),
            "First-visit": st.column_config.NumberColumn(format="%d"),
            "Established": st.column_config.NumberColumn(format="%d"),
            "Cost per redeemer": st.column_config.NumberColumn(format="$%.2f"),
            "Avg basket": st.column_config.NumberColumn(format="$%.2f"),
        })

        if a.brand.astype(str).str.startswith(
                ("Secret Drops", "Travel Club Substitution")).any():
            st.markdown(
                '<p class="note"><b>Secret Drops</b> and <b>Travel Club '
                'Substitution</b> rows are promo families, not brands — the '
                'offer never names a product (mystery bags and out-of-stock '
                'swaps). Pick one in the drill-down below to see what '
                'customers actually received.</p>', unsafe_allow_html=True)

        if len(unattributed) and unattributed.spend.sum() > 0:
            st.markdown(
                f'<p class="note">${unattributed.spend.sum():,.0f} across '
                f'{int(unattributed.redemptions.sum()):,} redemptions could '
                f'not be matched to a brand or promo family.</p>',
                unsafe_allow_html=True)

        # --- Brand → SKU redemption drill-down -------------------------
        st.divider()
        st.markdown("##### Which SKUs were redeemed, by brand")
        st.markdown('<p class="note">Pick a brand to see every redeemed offer '
                    'tied to its products. Offers are named after the product '
                    'they discount, so this is the SKU-level view of where '
                    'the redemption dollars went.</p>', unsafe_allow_html=True)

        brand_opts = sorted(attributed.brand.dropna().unique())
        sel_brand = st.selectbox("Select a brand", brand_opts,
                                 key="redeem_brand_sku")

        # Group by the matched product (strain-level) when the published file
        # has it; older files only carry offer names. Offer-name variants —
        # "Loyalty …", the "Loytaly …" typo — roll up into one row per strain.
        try:
            sku = q(f"""
                SELECT COALESCE(product, offer_name) AS sku, category,
                       SUM(redemptions) AS redemptions,
                       SUM(redeem_value) AS spend,
                       AVG(avg_basket)  AS avg_basket
                FROM dash_offer_performance
                WHERE brand = '{sel_brand.replace("'", "''")}' {af}
                GROUP BY 1,2
                ORDER BY spend DESC
            """)
            if not len(sku):
                raise ValueError("empty")
        except Exception:
            sku = q(f"""
                SELECT offer_name AS sku, category,
                       SUM(redemptions) AS redemptions,
                       SUM(redeem_value) AS spend,
                       AVG(avg_basket)  AS avg_basket
                FROM dash_offer_performance
                WHERE brand = '{sel_brand.replace("'", "''")}' {af}
                GROUP BY 1,2
                ORDER BY spend DESC
            """)

        if sku.empty:
            st.info(f"No redeemed offers matched to {sel_brand}.")
        else:
            sku["cost_per_redemption"] = sku.spend / sku.redemptions.replace(
                0, float("nan"))

            m = st.columns(3)
            m[0].metric("SKUs redeemed", f"{len(sku):,}")
            m[1].metric("Redemptions", f"{int(sku.redemptions.sum()):,}")
            m[2].metric("Redemption $", f"${sku.spend.sum():,.0f}")

            st.dataframe(pd.DataFrame({
                "SKU / Offer": sku.sku,
                "Category": sku.category,
                "Redemptions": sku.redemptions,
                "Spend $": sku.spend.round(0),
                "Cost each": sku.cost_per_redemption.round(2),
                "Avg basket": sku.avg_basket.round(2),
            }), use_container_width=True, hide_index=True, column_config={
                "Redemptions": st.column_config.NumberColumn(format="%d"),
                "Spend $": st.column_config.NumberColumn(format="$%d"),
                "Cost each": st.column_config.NumberColumn(format="$%.2f"),
                "Avg basket": st.column_config.NumberColumn(format="$%.2f"),
            })

            top = sku.head(15)
            fig = px.bar(top, x="spend", y="sku", orientation="h",
                         color_discrete_sequence=[ACCENT],
                         labels={"spend": "Redemption $", "sku": ""})
            fig.update_layout(height=max(280, 36 * len(top)),
                              margin=dict(l=0, r=0, t=10, b=0),
                              yaxis=dict(autorange="reversed"),
                              plot_bgcolor="rgba(0,0,0,0)")
            fig.update_xaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$,.0s")
            st.plotly_chart(fig, use_container_width=True, key="pc17")

        # --- Off-menu picks: what redeemers chose instead -------------------
        st.divider()
        st.markdown("##### Chosen instead — off-menu picks")
        st.markdown(
            '<p class="note">When a redemption menu item is out of stock, '
            'staff let the customer pick something of similar value. These '
            'are the products redeemers actually walked out with, ranked by '
            'redemption dollars — a ready-made shortlist of candidates for '
            'the menu.</p>', unsafe_allow_html=True)
        subs = q(f"""
            SELECT product AS sku, category,
                   SUM(redemptions)             AS redemptions,
                   SUM(redeem_value)            AS spend,
                   COUNT(DISTINCT offer_name)   AS offers
            FROM dash_offer_performance
            WHERE match_method = 'substituted-line' {af}
            GROUP BY 1,2
            ORDER BY spend DESC
        """)
        if subs.empty:
            st.info("No substitutions recorded yet. They appear after the "
                    "next data rebuild, once re-matching has run.")
        else:
            st.dataframe(pd.DataFrame({
                "SKU chosen": subs.sku,
                "Category": subs.category,
                "Times picked": subs.redemptions,
                "Redemption $": subs.spend.round(0),
                "Via # of offers": subs.offers,
            }), use_container_width=True, hide_index=True, column_config={
                "Times picked": st.column_config.NumberColumn(format="%d"),
                "Redemption $": st.column_config.NumberColumn(format="$%d"),
                "Via # of offers": st.column_config.NumberColumn(format="%d"),
            })

        st.divider()
        st.markdown("##### Spend against basket size")
        fig = px.scatter(a, x="redemptions", y="avg_basket", size="spend",
                         color="category", hover_name="brand", size_max=38,
                         color_discrete_map={c: cat_color(c)
                                             for c in a.category.unique()})
        fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0),
                          xaxis_title="Redemptions",
                          yaxis_title="Average basket $",
                          legend=dict(orientation="h", y=1.1, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        fig.update_xaxes(gridcolor="rgba(0,0,0,.07)")
        fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$,.0f")
        st.plotly_chart(fig, use_container_width=True, key="pc11")

        off = q(f"""
            SELECT offer_name, brand, category,
                   SUM(redemptions) AS redemptions,
                   SUM(redeem_value) AS spend,
                   AVG(avg_basket) AS avg_basket
            FROM dash_offer_performance {wf}
            GROUP BY 1,2,3 ORDER BY spend DESC
        """)
        if not off.empty:
            st.divider()
            st.markdown("##### Individual offers")
            off["cost_per_redemption"] = off.spend / off.redemptions.replace(
                0, float("nan"))
            st.dataframe(pd.DataFrame({
                "Offer": off.offer_name,
                "Brand": off.brand,
                "Redemptions": off.redemptions,
                "Spend $": off.spend.round(0),
                "Cost each": off.cost_per_redemption.round(2),
                "Avg basket": off.avg_basket.round(2),
            }), use_container_width=True, hide_index=True, column_config={
                "Spend $": st.column_config.NumberColumn(format="$%d"),
                "Cost each": st.column_config.NumberColumn(format="$%.2f"),
                "Avg basket": st.column_config.NumberColumn(format="$%.2f"),
            })

        st.markdown('<p class="note">These figures describe what redemptions '
                    'cost and who used them. They do not show whether the '
                    'offer caused the visit — redeemers are your most engaged '
                    'customers by construction.</p>', unsafe_allow_html=True)



# -------------------------------------------------------------- redemptions
with t_redeem:
    st.markdown("#### Loyalty Redemptions")
    st.markdown('<div class="howto"><b>How to read this tab.</b> '
                'Redemptions are discounts given back to loyalty members. '
                'A high redemption rate means many customers are using their '
                'points — usually good, because engaged members buy more often. '
                'But watch the cost: if redemption dollars grow faster than '
                'net sales, the program is eating margin. The sweet spot is '
                'high redemption rate with low redemption cost as a percent '
                'of revenue, and high sales per redeeming basket.</div>',
                unsafe_allow_html=True)

    red = q(f"""
        SELECT iso_year, iso_week,
               SUM(baskets) AS baskets,
               SUM(redeem_baskets) AS redeem_baskets,
               SUM(redeem_value) AS redeem_value,
               SUM(net) AS net
        FROM dash_basket_week {wf}
        GROUP BY 1,2
        ORDER BY 1,2
    """)
    red_store = q(f"""
        SELECT store_key,
               SUM(baskets) AS baskets,
               SUM(redeem_baskets) AS redeem_baskets,
               SUM(redeem_value) AS redeem_value,
               SUM(net) AS net
        FROM dash_basket_week {wf}
        GROUP BY 1
    """)
    if red.empty or red.redeem_baskets.sum() == 0:
        st.info("No redemption data in the published file.")
    else:
        red["wk_date"] = pd.to_datetime(
            red.iso_year.astype(str) + "-W" + red.iso_week.astype(str).str.zfill(2) + "-1",
            format="%G-W%V-%u", errors="coerce")
        red = red.sort_values("wk_date").reset_index(drop=True)
        red["redeem_rate"] = red.redeem_baskets / red.baskets.replace(0, float("nan"))
        red["redeem_per_basket"] = red.redeem_value / red.baskets.replace(0, float("nan"))
        red["redeem_pct_of_net"] = red.redeem_value / red.net.replace(0, float("nan"))
        red_active = red[red.redeem_baskets > 0].copy()
        if len(red_active) >= 2:
            cur = red_active.iloc[-1]
            prev = red_active.iloc[-2]
        elif len(red_active) == 1:
            cur = red_active.iloc[-1]
            prev = None
        else:
            cur = red.iloc[-1]
            prev = red.iloc[-2] if len(red) > 1 else None
        def fmt_delta(cur_val, prev_val):
            if prev_val is None or pd.isna(prev_val) or prev_val == 0:
                return None
            return f"{(cur_val/prev_val-1)*100:+.1f}%"
        c = st.columns(4)
        c[0].metric("Redemption Rate",
                    f"{cur.redeem_rate*100:.1f}%" if pd.notna(cur.redeem_rate) and cur.redeem_rate > 0 else "—",
                    fmt_delta(cur.redeem_rate, prev.redeem_rate) if prev is not None else None)
        c[1].metric("Redemption Value",
                    f"${cur.redeem_value:,.0f}" if pd.notna(cur.redeem_value) and cur.redeem_value > 0 else "—",
                    fmt_delta(cur.redeem_value, prev.redeem_value) if prev is not None else None)
        c[2].metric("Avg Redemption / Basket",
                    f"${cur.redeem_per_basket:.2f}" if pd.notna(cur.redeem_per_basket) and cur.redeem_per_basket > 0 else "—",
                    fmt_delta(cur.redeem_per_basket, prev.redeem_per_basket) if prev is not None else None)
        c[3].metric("Redemptions as % of Net",
                    f"{cur.redeem_pct_of_net*100:.1f}%" if pd.notna(cur.redeem_pct_of_net) and cur.redeem_pct_of_net > 0 else "—",
                    fmt_delta(cur.redeem_pct_of_net, prev.redeem_pct_of_net) if prev is not None else None)
        st.divider()
        L, R = st.columns([3, 2])
        with L:
            heading("Redemption value and rate by week")
            st.markdown('<p class="note">The bars show total dollars redeemed; '
                        'the line shows what share of baskets included a '
                        'redemption.</p>', unsafe_allow_html=True)
            fig = go.Figure()
            fig.add_bar(x=red.wk_date, y=red.redeem_value, name="Redemption $",
                        marker_color=ACCENT, opacity=.75)
            fig.add_scatter(x=red.wk_date, y=red.redeem_rate*100, name="Redemption rate %",
                            yaxis="y2", line=dict(color=WARN, width=2))
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis=dict(title="Redemption $", tickformat="$,.0s",
                                         gridcolor="rgba(0,0,0,.07)"),
                              yaxis2=dict(title="Rate %", overlaying="y", side="right",
                                          showgrid=False, tickformat=".1f",
                                          ticksuffix="%"),
                              hovermode="x unified",
                              legend=dict(orientation="h", y=1.12, x=0,
                                          title_text=""),
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, key="pc12")
        with R:
            st.markdown("##### Redemption by store")
            if len(keys) > 1 and not red_store.empty:
                red_store["store"] = red_store.store_key.map(STORES)
                red_store["redeem_rate"] = red_store.redeem_baskets / red_store.baskets.replace(0, float("nan"))
                red_store = red_store.sort_values("redeem_value", ascending=False)
                red_store["label"] = red_store.apply(
                    lambda r: f"${r.redeem_value/1e3:.0f}k<br>({r.redeem_rate*100:.1f}%)", axis=1)
                fig = px.bar(red_store, x="store", y="redeem_value",
                             color="redeem_rate", color_continuous_scale="Greens",
                             text="label")
                fig.update_traces(textposition="outside", textfont_size=11,
                                  cliponaxis=False)
                fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis_title="Redemption $", xaxis_title="",
                                  coloraxis_colorbar=dict(title="Rate %",
                                                          ticksuffix="%"),
                                  plot_bgcolor="rgba(0,0,0,0)")
                fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$,.0s")
                st.plotly_chart(fig, use_container_width=True, key="pc13")
                st.markdown('<p class="note"><b>Bar height</b> = total redemption '
                            'dollars. <b>Color</b> = redemption rate (darker green '
                            '= higher engagement).</p>',
                            unsafe_allow_html=True)
            else:
                st.markdown('<p class="note">Select multiple stores in the '
                            'sidebar to compare redemption performance.</p>',
                            unsafe_allow_html=True)
        st.divider()
        if not red_store.empty and len(keys) > 1:
            st.markdown("#### Store redemption scorecard")
            st.markdown('<div class="howto"><b>How to read the scorecard.</b> '
                        '<b>Redemption Rate</b> is engagement. '
                        '<b>Redemption Cost %</b> is the true price of the program. '
                        '<b>Sales / Redeem Bkt</b> is the ROI proxy.</div>',
                        unsafe_allow_html=True)
            red_store["redeem_rate"] = red_store.redeem_baskets / red_store.baskets.replace(0, float("nan"))
            red_store["redeem_pct_of_net"] = red_store.redeem_value / red_store.net.replace(0, float("nan"))
            red_store["sales_per_redeem_basket"] = red_store.net / red_store.redeem_baskets.replace(0, float("nan"))
            by_rate = red_store.sort_values("redeem_rate", ascending=False)
            by_roi = red_store.sort_values("sales_per_redeem_basket", ascending=False)
            h1, h2, h3, h4 = st.columns(4)
            with h1:
                top = by_rate.iloc[0]
                st.markdown(f'<div class="alert a-ok"><b>Most redeemed</b><br>'
                            f'{STORES.get(top.store_key, top.store_key)} — '
                            f'{top.redeem_rate*100:.1f}%</div>',
                            unsafe_allow_html=True)
            with h2:
                bot = by_rate.iloc[-1]
                st.markdown(f'<div class="alert a-warn"><b>Least redeemed</b><br>'
                            f'{STORES.get(bot.store_key, bot.store_key)} — '
                            f'{bot.redeem_rate*100:.1f}%</div>',
                            unsafe_allow_html=True)
            with h3:
                top_roi = by_roi.iloc[0]
                st.markdown(f'<div class="alert a-ok"><b>Best ROI</b><br>'
                            f'{STORES.get(top_roi.store_key, top_roi.store_key)} — '
                            f'${top_roi.sales_per_redeem_basket:.0f} / redeem bkt</div>',
                            unsafe_allow_html=True)
            with h4:
                bot_roi = by_roi.iloc[-1]
                st.markdown(f'<div class="alert a-bad"><b>Worst ROI</b><br>'
                            f'{STORES.get(bot_roi.store_key, bot_roi.store_key)} — '
                            f'${bot_roi.sales_per_redeem_basket:.0f} / redeem bkt</div>',
                            unsafe_allow_html=True)
            st.dataframe(pd.DataFrame({
                "Store": red_store.store_key.map(STORES),
                "Baskets": red_store.baskets,
                "Redeem Baskets": red_store.redeem_baskets,
                "Redemption $": red_store.redeem_value.round(0),
                "Net $": red_store.net.round(0),
                "Redemption Rate %": (red_store.redeem_rate * 100).round(1),
                "Redemption Cost %": (red_store.redeem_pct_of_net * 100).round(1),
                "Sales / Redeem Bkt": red_store.sales_per_redeem_basket.round(0),
            }), use_container_width=True, hide_index=True, column_config={
                "Redemption $": st.column_config.NumberColumn(format="$%d"),
                "Net $": st.column_config.NumberColumn(format="$%d"),
                "Redemption Rate %": st.column_config.NumberColumn(format="%.1f%%"),
                "Redemption Cost %": st.column_config.NumberColumn(format="%.1f%%"),
                "Sales / Redeem Bkt": st.column_config.NumberColumn(format="$%d"),
            })
        st.divider()
        heading("Redemptions as % of net sales")
        fig = px.line(red, x="wk_date", y=red.redeem_pct_of_net*100,
                      markers=True, color_discrete_sequence=[WARN])
        fig.update_traces(line=dict(width=2.5), marker=dict(size=6),
                          hovertemplate="%{y:.1f}%<extra></extra>")
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="% of net sales", xaxis_title="",
                          hovermode="x unified", showlegend=False,
                          plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", ticksuffix="%",
                         zeroline=False)
        fig.update_xaxes(gridcolor="rgba(0,0,0,.04)")
        st.plotly_chart(fig, use_container_width=True, key="pc14")
        st.markdown('<p class="note">A rising line means the loyalty program '
                    'is taking a larger share of revenue.</p>',
                    unsafe_allow_html=True)


# -------------------------------------------------------------- projections
with t_projections:
    st.markdown("#### Next Quarter Projection")
    st.markdown('<p class="note">Simple linear projections from the last 26 '
                'weeks, extended 13 weeks forward. Use as a baseline, not a '
                'forecast.</p>', unsafe_allow_html=True)
    proj_bw = q(f"""
        SELECT iso_year, iso_week, SUM(baskets) AS baskets, SUM(net) AS net
        FROM dash_basket_week {wf}
        GROUP BY 1,2
        ORDER BY 1,2
    """)
    if len(proj_bw) < 8:
        st.info("Not enough weekly history to build a projection.")
    else:
        proj_bw["wk_date"] = pd.to_datetime(
            proj_bw.iso_year.astype(str) + "-W" + proj_bw.iso_week.astype(str).str.zfill(2) + "-1",
            format="%G-W%V-%u", errors="coerce")
        proj_bw = proj_bw.sort_values("wk_date").reset_index(drop=True)
        trend_n = min(26, len(proj_bw))
        trend_bw = proj_bw.tail(trend_n).copy().reset_index(drop=True)
        trend_bw["x"] = range(len(trend_bw))
        z_b = np.polyfit(trend_bw.x, trend_bw.baskets, 1)
        z_n = np.polyfit(trend_bw.x, trend_bw.net, 1)
        last_date = proj_bw.wk_date.iloc[-1]
        future_dates = pd.date_range(start=last_date + pd.Timedelta(weeks=1),
                                     periods=13, freq="W-MON")
        future_x = range(len(trend_bw), len(trend_bw) + 13)
        proj_baskets = np.maximum(np.polyval(z_b, future_x), 0)
        proj_net = np.maximum(np.polyval(z_n, future_x), 0)
        proj_df = pd.DataFrame({
            "wk_date": future_dates,
            "projected_baskets": proj_baskets,
            "projected_net": proj_net
        })
        total_proj_net = proj_df.projected_net.sum()
        total_proj_baskets = proj_df.projected_baskets.sum()
        cur_quarter = proj_bw.tail(13).net.sum()
        cur_baskets = proj_bw.tail(13).baskets.sum()
        c = st.columns(4)
        c[0].metric("Projected Quarter Net", f"${total_proj_net:,.0f}",
                    f"{(total_proj_net/cur_quarter-1)*100:+.1f}% vs last 13 wks")
        c[1].metric("Projected Baskets", f"{total_proj_baskets:,.0f}",
                    f"{(total_proj_baskets/cur_baskets-1)*100:+.1f}% vs last 13 wks")
        c[2].metric("Projected ATV", f"${total_proj_net/total_proj_baskets:.2f}")
        c[3].metric("Trend weeks used", f"{trend_n}")
        st.divider()
        heading("Projected net sales by week")
        fig = go.Figure()
        fig.add_scatter(x=proj_bw.wk_date, y=proj_bw.net, name="Historical",
                        mode="lines+markers", line=dict(color=ACCENT, width=2),
                        marker=dict(size=5))
        fig.add_scatter(x=proj_df.wk_date, y=proj_df.projected_net,
                        name="Projected", mode="lines+markers",
                        line=dict(color=WARN, width=2, dash="dash"),
                        marker=dict(size=5))
        fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis=dict(title="Net $", tickformat="$,.0s",
                                     gridcolor="rgba(0,0,0,.07)"),
                          xaxis_title="", hovermode="x unified",
                          legend=dict(orientation="h", y=1.12, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True, key="pc15")
        st.divider()
        heading("Category projections")
        proj_cat = q(f"""
            SELECT iso_year, iso_week, category, SUM(net) AS net
            FROM dash_category_week {wf}
            GROUP BY 1,2,3
            ORDER BY 1,2,3
        """)
        if not proj_cat.empty:
            proj_cat["wk_date"] = pd.to_datetime(
                proj_cat.iso_year.astype(str) + "-W" +
                proj_cat.iso_week.astype(str).str.zfill(2) + "-1",
                format="%G-W%V-%u", errors="coerce")
            proj_cat = proj_cat.sort_values(["category", "wk_date"])
            cat_rows = []
            for cat, g in proj_cat.groupby("category"):
                g = g.tail(min(26, len(g))).copy().reset_index(drop=True)
                if len(g) >= 4:
                    g["x"] = range(len(g))
                    try:
                        z = np.polyfit(g.x, g.net, 1)
                        future = np.maximum(
                            np.polyval(z, range(len(g), len(g) + 13)), 0)
                        cat_rows.append({
                            "category": cat,
                            "projected_quarter": future.sum(),
                            "trend_weekly": z[0],
                            "latest_13wk": g.net.tail(13).sum()
                        })
                    except Exception:
                        pass
            if cat_rows:
                cat_proj = pd.DataFrame(cat_rows).sort_values(
                    "projected_quarter", ascending=False)
                cat_proj["vs_last_quarter"] = (
                    cat_proj.projected_quarter / cat_proj.latest_13wk - 1) * 100
                st.dataframe(pd.DataFrame({
                    "Category": cat_proj.category,
                    "Projected Quarter $": cat_proj.projected_quarter.round(0),
                    "vs Last 13 Wks %": cat_proj.vs_last_quarter.round(1),
                    "Weekly Trend $": cat_proj.trend_weekly.round(0),
                }), use_container_width=True, hide_index=True, column_config={
                    "Projected Quarter $": st.column_config.NumberColumn(
                        format="$%d"),
                    "vs Last 13 Wks %": st.column_config.NumberColumn(
                        format="%.1f%%"),
                    "Weekly Trend $": st.column_config.NumberColumn(
                        format="$%d"),
                })
                fig = go.Figure()
                fig.add_bar(x=cat_proj.category, y=cat_proj.latest_13wk,
                            name="Last 13 wks", marker_color=ACCENT, opacity=.7)
                fig.add_bar(x=cat_proj.category, y=cat_proj.projected_quarter,
                            name="Projected 13 wks", marker_color=WARN,
                            opacity=.7)
                fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis=dict(title="Net $", tickformat="$,.0s",
                                             gridcolor="rgba(0,0,0,.07)"),
                                  xaxis_title="", barmode="group",
                                  hovermode="x unified",
                                  legend=dict(orientation="h", y=1.12, x=0,
                                              title_text=""),
                                  plot_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig, use_container_width=True, key="pc16")
            else:
                st.info("Not enough category history to project.")

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


def render_promo_lab():
    """Promo Lab: churn by category/store/brand + ROI estimates.
    Uses fact_line locally, or the privacy-safe dash_promo_* tables online."""
    import os
    import numpy as np
    import pandas as pd

    st.subheader("Promo Lab - Discount Intelligence & ROI")
    st.caption("Where churn concentrates, and the projected return on fixing it with discounts. "
               "Uses your real margins from the data, not a guess.")
    st.markdown('<p class="note"><b>How to read this.</b> This tab answers one question: where is it worth spending discount dollars? A customer counts as <b>churned</b> when they have not bought anything within the lapse window you set below. Every table is ranked by <b>Net gain</b> - the money a win-back promo is projected to make after paying for the discount itself - not by churn rate. That way small, noisy segments can never outrank big, reliable ones. Set the assumptions to your own guesses; nothing here is final until a real campaign measures a real response rate.</p>', unsafe_allow_html=True)

    # ---------- data: full detail locally, privacy-safe aggregates online ----------
    pub_cat, pub_brand = pd.DataFrame(), pd.DataFrame()
    df = q("""SELECT customer_key, store_key, txn_ts, category, brand,
                     basket_id, net_sales, gross_margin
              FROM fact_line WHERE customer_key IS NOT NULL""")
    if df.empty:
        try:
            import duckdb
            for cand in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "tta.duckdb"),
                         r"C:\Users\User\cerebral\Cerebral\tta.duckdb"]:
                if os.path.exists(cand):
                    try:
                        con = duckdb.connect(cand, read_only=True)
                        df = con.execute("""SELECT customer_key, store_key, txn_ts, category, brand,
                                                   basket_id, net_sales, gross_margin
                                            FROM fact_line WHERE customer_key IS NOT NULL""").df()
                        con.close()
                    except Exception:
                        pass
                if not df.empty:
                    break
        except Exception:
            pass

    if not df.empty:
        work = df.copy()
        work["txn_ts"] = pd.to_datetime(work["txn_ts"], errors="coerce")
        work = work.dropna(subset=["txn_ts", "customer_key"])
        today = work["txn_ts"].max()

        def build_pub(dim):
            pc = (work.groupby(["store_key", dim, "customer_key"])
                      .agg(last=("txn_ts", "max"), n=("basket_id", "nunique"),
                           spend=("net_sales", "sum"), gm=("gross_margin", "sum"))
                      .reset_index())
            days = (today - pc["last"]).dt.days
            pc["repeat"] = (pc["n"] > 1).astype(int)
            for wdays in (30, 45, 60, 90):
                pc[f"churned_{wdays}"] = (days > wdays).astype(int)
                pc[f"lapsed_spend_{wdays}"] = pc["spend"] * pc[f"churned_{wdays}"]
            return (pc.groupby(["store_key", dim])
                      .agg(customers=("customer_key", "count"),
                           repeat_buyers=("repeat", "sum"),
                           spend_sum=("spend", "sum"), gm_sum=("gm", "sum"),
                           **{f"churned_{w}": (f"churned_{w}", "sum") for w in (30, 45, 60, 90)},
                           **{f"lapsed_spend_{w}": (f"lapsed_spend_{w}", "sum") for w in (30, 45, 60, 90)})
                      .reset_index())

        pub_cat = build_pub("category")
        pub_brand = build_pub("brand") if work["brand"].notna().any() else pd.DataFrame()
    else:
        pub_cat = q("SELECT * FROM dash_promo_category")
        pub_brand = q("SELECT * FROM dash_promo_brand")

    if pub_cat.empty:
        st.warning("No customer-level data found. The published file needs a rebuild "
                   "(publish.py) that includes the dash_promo tables.")
        return

    # ---------- assumptions ----------
    a1, a2, a3, a4 = st.columns(4)
    LAPSE = a1.selectbox("Lapse window (days)", [30, 45, 60, 90], index=0,
                         help="No purchase within this window = churned")
    WINBACK = a2.slider("Expected win-back rate", 1, 40, 10) / 100
    DISCOUNT = a3.slider("Discount depth", 5, 50, 20) / 100
    MIN_CUST = a4.slider("Min customers per segment", 5, 100, 30)

    CH, LS = f"churned_{LAPSE}", f"lapsed_spend_{LAPSE}"

    def roi_table(pub, group_cols):
        g = pub.groupby(group_cols).agg(
            customers=("customers", "sum"), repeat_buyers=("repeat_buyers", "sum"),
            spend_sum=("spend_sum", "sum"), gm_sum=("gm_sum", "sum"),
            churned=(CH, "sum"), lapsed_spend=(LS, "sum")).reset_index()
        g["churn_rate"] = (g["churned"] / g["customers"].clip(lower=1) * 100).round(1)
        g["repeat_rate"] = (g["repeat_buyers"] / g["customers"].clip(lower=1) * 100).round(1)
        g["real_margin"] = np.where(g["spend_sum"] > 0, g["gm_sum"] / g["spend_sum"], 0.5)
        g["avg_lapsed_spend"] = (g["lapsed_spend"] / g["churned"].clip(lower=1)).round(0)
        g["reachable"] = g["churned"].astype(int)
        g["expected_winbacks"] = (g["reachable"] * WINBACK).round(1)
        g["incr_revenue"] = (g["expected_winbacks"] * g["avg_lapsed_spend"]).round(0)
        g["promo_cost"] = (g["incr_revenue"] * DISCOUNT).round(0)
        g["incr_profit"] = (g["incr_revenue"] * g["real_margin"]).round(0)
        g["net_gain"] = (g["incr_profit"] - g["promo_cost"]).round(0)
        g["roi_pct"] = np.where(g["promo_cost"] > 0,
                                (g["net_gain"] / g["promo_cost"] * 100).round(0), np.nan)
        g = g[(g["customers"] >= MIN_CUST) & (g["reachable"] >= 5)]
        return g.sort_values("net_gain", ascending=False)

    RENAME = {"category": "Segment", "customers": "Customers", "churned": "Lapsed",
              "churn_rate": "Churn %", "repeat_rate": "Repeat %",
              "avg_lapsed_spend": "Avg lapsed spend $", "reachable": "Targetable",
              "expected_winbacks": "Expected win-backs", "incr_revenue": "Incr. revenue $",
              "promo_cost": "Promo cost $", "net_gain": "Net gain $", "roi_pct": "ROI %",
              "real_margin": "Real margin", "store_key": "Store", "brand": "Brand"}
    MONEY_FMT = {"Avg lapsed spend $": "${:,.0f}", "Incr. revenue $": "${:,.0f}",
                 "Promo cost $": "${:,.0f}", "Net gain $": "${:,.0f}",
                 "ROI %": "{:,.0f}%", "Real margin": "{:.0%}"}

    tab1, tab2, tab3 = st.tabs(["Churn Map (Categories)", "Store Opportunities", "Brand Promos"])

    with tab1:
        st.markdown('<p class="note"><b>What you are looking at.</b> Each row is a product category across all stores. <b>Churn %</b> is the share of that category customers who have not come back within the lapse window. <b>Real margin</b> comes straight from your sales data, not a guess. <b>Targetable</b> is how many lapsed customers you could actually send an offer to. The greener the Net gain column, the more sense a discount makes there.</p>', unsafe_allow_html=True)
        seg = roi_table(pub_cat, ["category"])
        show = seg[["category", "customers", "churned", "churn_rate", "repeat_rate",
                    "real_margin", "avg_lapsed_spend", "expected_winbacks",
                    "incr_revenue", "promo_cost", "net_gain", "roi_pct"]].rename(columns=RENAME)
        st.markdown("**Segments ranked by net gain from a win-back promo**")
        st.dataframe(show.style.format(MONEY_FMT)
                     .background_gradient(subset=["Net gain $"], cmap="Greens"),
                     use_container_width=True, hide_index=True)

    with tab2:
        st.markdown('<p class="note"><b>How to use this.</b> The first table picks the single best promo for each store - start there. The dropdown below it shows every category inside one store. The verdict table at the bottom tells you whether a store needs one targeted offer (churn concentrated in a few categories) or a store-wide event like a double-points week (churn spread across nearly everything).</p>', unsafe_allow_html=True)
        store_seg = roi_table(pub_cat, ["store_key", "category"])
        store_seg["store_key"] = store_seg["store_key"].map(STORES).fillna(store_seg["store_key"].astype(str))
        st.markdown("**Each store's single best promo (highest net gain)**")
        if not store_seg.empty:
            idx = store_seg.groupby("store_key")["net_gain"].idxmax()
            best = store_seg.loc[idx][["store_key", "category", "reachable", "churn_rate",
                                       "expected_winbacks", "incr_revenue",
                                       "promo_cost", "net_gain", "roi_pct"]].rename(columns=RENAME)
            st.dataframe(best.style.format(MONEY_FMT), use_container_width=True, hide_index=True)

        st.markdown("**Drill into a store**")
        pick = st.selectbox("Store", sorted(store_seg["store_key"].unique()), key="promo_store_pick")
        drill = store_seg[store_seg["store_key"] == pick][
            ["category", "customers", "churned", "churn_rate", "reachable",
             "expected_winbacks", "incr_revenue", "promo_cost", "net_gain", "roi_pct"]
        ].rename(columns=RENAME)
        st.dataframe(drill.style.format(MONEY_FMT), use_container_width=True, hide_index=True)

        st.markdown("**Store-wide promo signal**")
        sw = store_seg.groupby("store_key").agg(
            segments=("category", "count"),
            high_churn_segs=("churn_rate", lambda s: (s > 50).sum()),
            total_net=("net_gain", "sum")).reset_index()
        sw["verdict"] = np.where(
            sw["high_churn_segs"] >= sw["segments"] * 0.6,
            "Store-wide event (e.g., double-points week)",
            "Targeted segment discounts")
        st.dataframe(sw.rename(columns={"store_key": "Store", "segments": "Segments",
                                        "high_churn_segs": "High-churn segments",
                                        "total_net": "Total net gain $"})
                     .style.format({"Total net gain $": "${:,.0f}"}),
                     use_container_width=True, hide_index=True)

    with tab3:
        st.markdown('<p class="note"><b>What this means.</b> Brands ranked by the return on winning back their lapsed buyers. A <b>positive ROI</b> means the promo pays for itself under your assumptions. A negative one means the discount would cost more than it brings back - protect those brands and use them as traffic drivers in marketing instead of discounting them. High ROI with tiny dollar figures means interesting but not a priority.</p>', unsafe_allow_html=True)
        st.markdown("**Brands ranked by win-back ROI**")
        if pub_brand.empty:
            st.warning("No brand data in this build.")
        else:
            brand = roi_table(pub_brand, ["brand"])
            show = brand[["brand", "customers", "churn_rate", "repeat_rate", "real_margin",
                          "reachable", "expected_winbacks", "incr_revenue", "promo_cost",
                          "net_gain", "roi_pct"]].rename(columns=RENAME)
            st.dataframe(show.style.format(MONEY_FMT)
                         .background_gradient(subset=["Net gain $"], cmap="Greens"),
                         use_container_width=True, hide_index=True)

    positive = roi_table(pub_cat, ["store_key", "category"])
    positive = positive[positive["net_gain"] > 0]
    st.divider()
    m1, m2, m3 = st.columns(3)
    m1.metric("Promos with positive ROI", f"{len(positive):,}")
    m2.metric("Total customers to target", f"{int(positive['reachable'].sum()):,}")
    m3.metric("Total projected net gain", f"${positive['net_gain'].sum():,.0f}")

    st.download_button("Download full ROI table (CSV)",
                       positive.rename(columns=RENAME).to_csv(index=False),
                       "promo_lab_roi.csv", "text/csv", key="promo_download")


with t_promo:
    render_promo_lab()
