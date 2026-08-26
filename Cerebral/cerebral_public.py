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
from loyalty_tab import render_loyalty
from retention_tab import render_retention
from events_tab import render_events
from audiences_tab import render_audiences
from discounting_tab import render_discounting
from bei_tab import render_bei
from brand_roi import render_brand_roi


# --- rewards menu, for the off-menu picks panel ------------------------
# (brand substring, category, optional size regex on product)
# Matched on brand + form, not product name: the menu says "Rythm Infused
# Pre-Roll 1G" while the POS says "Rythm Remix Infused Pre Roll Multi Pack
# Strawberry Sour Diesel 5pk".
# Size is tested only where one brand+category spans two tiers -- Rythm
# Flower is 3.5g at 1,000 and 28g at 3,000.
REWARD_MENU = {
    100: [("travel agency", "Accessory", None)],
    200: [("nanticoke", "Pre-Roll", None),
          ("foy", "Edible", None)],
    500: [("rythm", "Pre-Roll", None),
          ("rythm", "Edible", None),
          ("wana", "Edible", None),
          ("papa", "Topical", None),
          ("incredibles", "Edible", None)],
    1000: [("rythm", "Pre-Roll", None),
           ("rythm", "Flower", r"3\.5"),
           ("rythm", "Vape", None),
           ("dark heart", "Flower", r"3\.5")],
    1500: [("rythm", "Concentrate", None),
           ("select", "Vape", None)],
    3000: [("rythm", "Flower", r"\b28")],
}


def _tier_points(offer_name):
    """Pull the points tier out of 'Travel Club 1000 Points Substitution'."""
    m = re.search(r"([0-9][0-9,]*)\s*points?", str(offer_name), re.I)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def is_on_menu(offer_name, brand, category, product):
    """True when the item taken IS the advertised reward for that tier."""
    pts = _tier_points(offer_name)
    if pts is None or pts not in REWARD_MENU:
        return False
    b = str(brand or "").lower()
    c = str(category or "")
    p = str(product or "")
    for want_brand, want_cat, size_rx in REWARD_MENU[pts]:
        if want_brand in b and c == want_cat:
            if size_rx and not re.search(size_rx, p, re.I):
                continue
            return True
    return False
# --- end rewards menu --------------------------------------------------

DASH_FILE = "cerebral_dash.duckdb"
CACHE_MINUTES = 30
BASELINE_WEEKS = 13

STORES = {1: "DTBK", 2: "5th Avenue", 3: "Soho", 4: "Union Square"}

# Mirrors PRODUCT_WEEK_MIN_NET in publish.py. Only used for captions, so a
# drift between the two is cosmetic rather than a wrong number — but keep
# them in step.
PRODUCT_FLOOR = 250

# Mirrors BRAND_ALIASES in publish.py. The published tables already arrive
# consolidated, so this is only for the Takeover tab's fallback path, which
# reads raw fact_line off the local build when no published table is there.
# Keys are lowercased and whitespace-collapsed before lookup.
BRAND_ALIASES = {
    "ruby": "Ruby",
    "ruby farms": "Ruby",
}


def canon_brand(s: "pd.Series") -> "pd.Series":
    """Rewrite a brand column to canonical names, leaving unknowns alone."""
    if not BRAND_ALIASES:
        return s
    key = (s.astype("string").str.strip().str.lower()
            .str.replace(r"\s+", " ", regex=True)
            .str.replace(r"[.,]+$", "", regex=True))
    return key.map(BRAND_ALIASES).fillna(s)

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

# Why the last load_db() attempt failed — shown on the no-data screen so a
# deployment problem names itself instead of guessing. Kept INSIDE the
# cached return value: a cached None skips the function body, so reasons
# recorded in a module-level list would be wiped on the next rerun.
LOAD_DB_WHYS: list[str] = []


@st.cache_resource(ttl=CACHE_MINUTES * 60)
def _load_db_cached() -> tuple[str | None, list[str]]:
    """Local file if present, otherwise pull the published copy from Drive.

    Returns (path, reasons). reasons is empty on success; on failure it
    names the stage that broke so the caller can show it.
    """
    # Look next to the script, then one level up (repo root when the app
    # lives in a subfolder, as on Streamlit Cloud), then the launch
    # directory — so the bundled data file always wins over Drive.
    here = Path(__file__).resolve().parent
    for local in (here / DASH_FILE, here.parent / DASH_FILE, Path(DASH_FILE)):
        if local.exists():
            return str(local), []

    whys: list[str] = []
    sa = secret("gcp_service_account")
    folder = secret("TTA_DRIVE_STATE") or os.environ.get("TTA_DRIVE_STATE")
    if not sa:
        whys.append("the `gcp_service_account` secret is missing "
                    "or empty in the app's Streamlit settings")
    if not folder:
        whys.append("the `TTA_DRIVE_STATE` secret is missing "
                    "or empty in the app's Streamlit settings")
    if not sa or not folder:
        return None, whys

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    try:
        info = dict(sa) if not isinstance(sa, str) else json.loads(sa)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        # supportsAllDrives + corpora=allDrives: without them a folder that
        # lives in a SHARED drive searches as if it were empty.
        res = svc.files().list(
            q=f"'{folder}' in parents and name = '{DASH_FILE}' and trashed = false",
            orderBy="modifiedTime desc",
            fields="files(id,name,size,modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives").execute().get("files", [])
    except Exception as e:
        msg = str(e).replace("\n", " ")[:300]
        whys.append(f"Drive access failed — {type(e).__name__}: {msg}")
        return None, whys
    if not res:
        whys.append(
            f"the credentials work, but there is no file named `{DASH_FILE}` "
            "in the Drive folder that `TTA_DRIVE_STATE` points to — either the "
            "folder ID is wrong or the publish step has not uploaded it there")
        return None, whys

    try:
        dest = Path(tempfile.gettempdir()) / DASH_FILE
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(
                fh, svc.files().get_media(fileId=res[0]["id"],
                                          supportsAllDrives=True),
                chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        return str(dest), []
    except Exception as e:
        msg = str(e).replace("\n", " ")[:300]
        whys.append(f"the file was found (modified {res[0].get('modifiedTime', '?')}) "
                    f"but the download failed — {type(e).__name__}: {msg}")
        return None, whys


def load_db() -> str | None:
    global LOAD_DB_WHYS
    path, LOAD_DB_WHYS = _load_db_cached()
    return path


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


def has_col(table: str, col: str) -> bool:
    """True when the published table exists AND carries the column.

    The published file and the app deploy on separate clocks: a column added
    to publish.py only appears after the next refresh, while the app goes
    live on push. A missing column must hide its section, not crash it —
    q() only swallows missing-TABLE errors, not missing-column ones.
    """
    if not table_exists(table):
        return False
    path = load_db()
    if not path:
        return False
    con = duckdb.connect(path, read_only=True)
    try:
        cols = con.execute(f"SELECT * FROM {table} LIMIT 0").df().columns
        return col in cols
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
    for why in LOAD_DB_WHYS:
        st.markdown(f"- {why}")
    st.markdown("""
The dashboard reads `cerebral_dash.duckdb`, built by the scheduled refresh
and pulled from Google Drive using the app's secrets.
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

# ---------------------------------------------------------------- window
# The slider used to move charts only, so a tab could show eight weeks of
# trend above a brand table covering thirteen months. Everything that CAN be
# scoped to the window now is; the exceptions are named where they occur.
#
# iso_year * 100 + iso_week gives one sortable integer per week, which keeps
# the SQL to a plain IN list instead of a row-comparison the older DuckDB
# builds on Streamlit Cloud do not all support.
WIN = weeks.tail(n_wk)
WIN_FULL = n_wk >= len(weeks)
WIN_IDS = [int(y) * 100 + int(w)
           for y, w in zip(WIN.iso_year, WIN.iso_week)]
COND_WEEK = "" if WIN_FULL else \
    f"(iso_year * 100 + iso_week) IN ({','.join(map(str, WIN_IDS))})"
COND_STORE = "" if len(keys) == len(STORES) else \
    f"store_key IN ({','.join(map(str, keys))})"

_win_dates = pd.to_datetime(
    WIN.iso_year.astype(str) + "-W"
    + WIN.iso_week.astype(str).str.zfill(2) + "-1",
    format="%G-W%V-%u", errors="coerce")
WIN_START = _win_dates.min()
WIN_END = _win_dates.max() + pd.Timedelta(days=6) if len(_win_dates) else None


def scoped(*extra: str, where: bool = True) -> str:
    """Store + window filter, plus any extra conditions.

    where=True returns a WHERE clause, where=False an AND fragment to append
    to a query that already has one. Empty conditions drop out, so a full
    window over all stores costs nothing.
    """
    parts = [p for p in (COND_STORE, COND_WEEK, *extra) if p]
    if not parts:
        return ""
    joined = " AND ".join(parts)
    return f" WHERE {joined}" if where else f" AND {joined}"


def day_scoped(*extra: str, col: str = "day", where: bool = True) -> str:
    """Same window, expressed as dates, for day-grained tables.

    dash_redemption_day and dash_brand_day carry a date rather than an ISO
    week, so they cannot use the integer week list.
    """
    parts = [p for p in (COND_STORE, *extra) if p]
    if not WIN_FULL and WIN_START is not None:
        parts.append(f"{col} BETWEEN DATE '{WIN_START:%Y-%m-%d}' "
                     f"AND DATE '{WIN_END:%Y-%m-%d}'")
    if not parts:
        return ""
    joined = " AND ".join(parts)
    return f" WHERE {joined}" if where else f" AND {joined}"


def win_halves(frame: pd.DataFrame, value: str, by: list[str] | None = None):
    """Split a week-grained frame into early/late halves OF THE WINDOW.

    dash_brand_trend bakes its halves in at publish time against the whole
    file, so it cannot answer "which way is this moving in the last eight
    weeks". This does the same split against whatever the slider selected.
    """
    if frame.empty:
        return pd.DataFrame(columns=(by or []) + ["early", "late"])
    f = frame.copy()
    f["_wk"] = f.iso_year.astype(int) * 100 + f.iso_week.astype(int)
    ordered = sorted(f["_wk"].unique())
    cut = ordered[len(ordered) // 2] if len(ordered) > 1 else ordered[0] + 1
    f["_half"] = f["_wk"].map(lambda v: "early" if v < cut else "late")
    grp = (by or []) + ["_half"]
    piv = (f.groupby(grp, as_index=False)[value].sum()
             .pivot_table(index=by or None, columns="_half", values=value,
                          aggfunc="sum", fill_value=0))
    for col in ("early", "late"):
        if col not in piv.columns:
            piv[col] = 0.0
    piv = piv.reset_index() if by else piv
    return piv


# The old fragments stay: plenty of queries are deliberately whole-file.
wfw, afw = scoped(), scoped(where=False)
if not WIN_FULL:
    st.sidebar.caption(
        f"window {WIN_START:%b %d, %Y} → {WIN_END:%b %d, %Y}  ·  "
        f"{n_wk} of {len(weeks)} weeks")

st.sidebar.divider()
st.sidebar.caption(
    f"{int(meta.n_baskets):,} baskets\n\n"
    f"{str(meta.first_txn)[:10]} → {str(meta.last_txn)[:10]}\n\n"
    f"updated {str(meta.built_at)[:16]}")

st.title("Cerebral")
label = "All stores" if len(keys) == len(STORES) else ", ".join(STORES[k] for k in keys)
st.caption(f"Category analytics · The Travel Agency · {label}")

t_charts, t_insights, t_brands, t_bei, t_acc, t_redeem, t_discount, \
    t_loyalty, t_retention, t_events, t_audiences, \
    t_takeover, t_projections, t_promo, t_gloss = st.tabs(
    ["Charts", "Insights", "Brands", "Brand Efficiency", "Accessories",
     "Redemptions", "Discounting", "Loyalty", "Retention", "Events",
     "Audiences", "Takeovers", "Projections", "Promo Lab",
     "What the terms mean"])

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
        # fixedrange on both y-axes: y2 overlays y, and plotly rescales
        # them independently, so any zoom or pan detaches the Baskets line
        # from the bars it is drawn against.
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          xaxis=dict(fixedrange=True),
                          yaxis=dict(title="Net $", tickformat="$~s",
                                     gridcolor="rgba(0,0,0,.07)",
                                     fixedrange=True),
                          yaxis2=dict(title="Baskets", overlaying="y",
                                      side="right", showgrid=False,
                                      tickformat=",.0f", fixedrange=True),
                          hovermode="x unified",
                          legend=dict(orientation="h", y=1.12, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        # Toolbar stays, minus every button that changes an axis range:
        # with fixedrange set they would do nothing, and a dead button is
        # worse than no button. scrollZoom off so the wheel scrolls the
        # page rather than the plot.
        st.plotly_chart(fig, use_container_width=True, key="pc1",
                        config={"scrollZoom": False, "displaylogo": False,
                                "modeBarButtonsToRemove": [
                                    "zoom2d", "pan2d", "select2d", "lasso2d",
                                    "zoomIn2d", "zoomOut2d", "autoScale2d",
                                    "resetScale2d"]})

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

    # ---------------------------------------------------- transactions
    st.divider()
    heading("Transactions by week", "basket")
    st.markdown(
        '<p class="note">One transaction is one completed basket, returns '
        'excluded. This is the volume line underneath revenue: net sales can '
        'hold up on a smaller number of larger baskets, and the two need to '
        'be read together.</p>', unsafe_allow_html=True)

    tx = q(f"""
        SELECT iso_year, iso_week, SUM(baskets) AS baskets,
               MAX(days_open) AS days_open
        FROM dash_basket_week {wfw} GROUP BY 1,2
    """)
    if not tx.empty:
        tx["wk_date"] = pd.to_datetime(
            tx.iso_year.astype(str) + "-W"
            + tx.iso_week.astype(str).str.zfill(2) + "-1",
            format="%G-W%V-%u", errors="coerce")
        tx = tx.sort_values("wk_date")

        tc1, tc2 = st.columns([3, 1])
        with tc2:
            per_day = st.checkbox(
                "Per trading day", value=bool(PARTIAL_WEEK),
                help="Divides each week by the days it actually traded. A "
                     "week in progress, or one containing a closure, "
                     "otherwise reads as a collapse in volume.")
        tx["value"] = tx.baskets / tx.days_open if per_day else tx.baskets
        tx["trend"] = tx["value"].rolling(4, min_periods=2).mean()

        _cur, _prev = tx.iloc[-1], (tx.iloc[-2] if len(tx) > 1 else None)
        with tc1:
            m = st.columns(3)
            m[0].metric(
                "Transactions per day" if per_day else "Transactions, latest week",
                f"{_cur.value:,.0f}",
                f"{(_cur.value / _prev.value - 1) * 100:+.1f}%"
                if _prev is not None and _prev.value else None)
            m[1].metric("4-week average", f"{_cur.trend:,.0f}",
                        help="The latest point of the trend line. Week-to-week "
                             "movement is mostly noise; this is the level.")
            m[2].metric("Window total", f"{tx.baskets.sum():,.0f}",
                        help="Every transaction across the weeks and stores "
                             "currently selected.")

        fig = go.Figure()
        fig.add_bar(x=tx.wk_date, y=tx.value, name="Transactions",
                    marker_color=ACCENT, opacity=.7)
        fig.add_scatter(x=tx.wk_date, y=tx.trend, name="4-week average",
                        line=dict(color=MUTED, width=2.4))
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis=dict(title="Transactions per day" if per_day
                                     else "Transactions",
                                     tickformat=",.0f",
                                     gridcolor="rgba(0,0,0,.07)"),
                          xaxis_title="", hovermode="x unified",
                          legend=dict(orientation="h", y=1.14, x=0,
                                      title_text=""),
                          plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True, key="pc_tx")

    # ---------------------------------------------- new vs returning
    st.divider()
    heading("New and returning customers by week")

    _nr_store = ("store_key = 0" if len(keys) == len(STORES)
                 else f"store_key IN ({','.join(map(str, keys))})")
    _nr_scope_label = st.radio(
        "Who counts as a customer",
        ["Loyalty-identified only", "Everyone in the POS"],
        horizontal=True, key="nr_scope",
        help="Customers without a loyalty ID are keyed on a hash of their "
             "name. Two people sharing a name collapse into one key, and "
             "every collision turns a genuinely new customer into a "
             "returning one — so the second option understates new "
             "customers. Its trend is usable; its level is not.")
    _nr_scope = ("resolved" if _nr_scope_label.startswith("Loyalty")
                 else "all")

    nr = q(f"""
        SELECT iso_year, iso_week, segment,
               SUM(customers) AS customers, SUM(net) AS net
        FROM dash_newret_week
        WHERE id_scope = '{_nr_scope}' AND {_nr_store}
              {(' AND ' + COND_WEEK) if COND_WEEK else ''}
        GROUP BY 1,2,3
    """)

    if nr.empty:
        st.markdown(
            '<div class="alert a-warn">This chart needs '
            '<code>dash_newret_week</code>, which the next scheduled rebuild '
            'will create. Everything else on this tab is unaffected.</div>',
            unsafe_allow_html=True)
    else:
        nr["wk_date"] = pd.to_datetime(
            nr.iso_year.astype(str) + "-W"
            + nr.iso_week.astype(str).str.zfill(2) + "-1",
            format="%G-W%V-%u", errors="coerce")

        # The first weeks of loaded history have nothing before them, so
        # every customer in them looks new. Dropping those weeks is the
        # difference between a trend and a cliff that is an artefact of
        # where the data starts.
        _nrm = q("SELECT * FROM dash_newret_meta")
        _burn = 4
        _cut = None
        if not _nrm.empty:
            _burn = int(_nrm.iloc[0].get("burn_in_weeks", 4) or 4)
            _cut = pd.to_datetime(_nrm.iloc[0].first_txn) + \
                pd.Timedelta(weeks=_burn)
            nr = nr[nr.wk_date >= _cut]

        if nr.empty:
            st.markdown(
                f'<p class="note">The selected window sits inside the first '
                f'{_burn} weeks of loaded history, where everyone counts as '
                f'new. Widen the window to see this.</p>',
                unsafe_allow_html=True)
        else:
            piv = (nr.pivot_table(index="wk_date", columns="segment",
                                  values="customers", aggfunc="sum")
                     .fillna(0).sort_index())
            for _c in ("New", "Returning"):
                if _c not in piv.columns:
                    piv[_c] = 0.0
            piv["total"] = piv.New + piv.Returning
            piv["new_share"] = (piv.New / piv.total.replace(0, np.nan)) * 100

            _c2, _p2 = piv.iloc[-1], (piv.iloc[-2] if len(piv) > 1 else None)
            nm = st.columns(4)
            nm[0].metric("New customers, latest week", f"{_c2.New:,.0f}",
                         f"{(_c2.New / _p2.New - 1) * 100:+.1f}%"
                         if _p2 is not None and _p2.New else None,
                         help="Customers whose first ever purchase, across "
                              "the whole loaded history, falls in that week.")
            nm[1].metric("Returning", f"{_c2.Returning:,.0f}",
                         f"{(_c2.Returning / _p2.Returning - 1) * 100:+.1f}%"
                         if _p2 is not None and _p2.Returning else None,
                         help="Everyone else who shopped that week.")
            nm[2].metric("New as % of customers", f"{_c2.new_share:,.1f}%",
                         f"{_c2.new_share - _p2.new_share:+.1f}pp"
                         if _p2 is not None and pd.notna(_p2.new_share) else None,
                         help="Acquisition rate. Falling share with flat "
                              "totals means the base is ageing, not growing.")
            _half = max(1, len(piv) // 2)
            _early, _late = piv.New.iloc[:_half].mean(), piv.New.iloc[-_half:].mean()
            nm[3].metric("New, window trend",
                         f"{(_late / _early - 1) * 100:+.1f}%" if _early else "—",
                         help="Average weekly new customers in the second "
                              "half of the window against the first.")

            fig = go.Figure()
            fig.add_bar(x=piv.index, y=piv.Returning, name="Returning",
                        marker_color=MUTED, opacity=.55)
            fig.add_bar(x=piv.index, y=piv.New, name="New",
                        marker_color=ACCENT)
            fig.add_scatter(x=piv.index, y=piv.new_share, name="New %",
                            yaxis="y2", line=dict(color=WARN, width=2.4))
            fig.update_layout(barmode="stack", height=380,
                              margin=dict(l=0, r=0, t=10, b=0),
                              yaxis=dict(title="Customers", tickformat=",.0f",
                                         gridcolor="rgba(0,0,0,.07)"),
                              yaxis2=dict(title="New %", overlaying="y",
                                          side="right", showgrid=False,
                                          ticksuffix="%", rangemode="tozero"),
                              xaxis_title="", hovermode="x unified",
                              legend=dict(orientation="h", y=1.12, x=0,
                                          title_text=""),
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, key="pc_nr")

            _notes = ["A customer is new in the week of their first ever "
                      "purchase and returning in every week after it, so the "
                      "two bars sum to that week's customer count."]
            if _cut is not None:
                _notes.append(f"The first {_burn} weeks of history are "
                              f"excluded — with nothing loaded before them, "
                              f"everyone in them reads as new.")
            if len(keys) != len(STORES):
                _notes.append("With a store filter applied, a customer who "
                              "shopped two of the selected stores in one week "
                              "is counted once per store. Select all stores "
                              "for the exact chain figure.")
            st.markdown('<p class="note">' + " ".join(_notes) + "</p>",
                        unsafe_allow_html=True)

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
            FROM dash_basket_week {wfw} GROUP BY 1,2,3
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
                       FROM dash_category_week {wfw} GROUP BY 1,2)
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

            # --- product movers: one table per category, side by side -------
            st.markdown("**Top products in these categories**")
            tca, tcb = st.columns(2)
            shown = 0
            # dash_product_trend bakes its early/late split against the whole
            # file, so it cannot follow the window. Where the week-level
            # product table exists, use it and split the window itself.
            for tcol, cat in ((tca, ca), (tcb, cb)):
                csql = cat.replace("'", "''")
                if table_exists("dash_brand_product_week"):
                    raw = q(f"""
                        SELECT brand, product, iso_year, iso_week,
                               SUM(net) AS net
                        FROM dash_brand_product_week
                        {scoped(f"category = '{csql}'")}
                        GROUP BY 1,2,3,4
                    """)
                    if raw.empty:
                        pt = pd.DataFrame()
                    else:
                        raw["net"] = pd.to_numeric(raw.net,
                                                   errors="coerce").fillna(0)
                        pt = (raw.groupby(["brand", "product"], as_index=False)
                                 .net.sum()
                                 .rename(columns={"net": "net_total"}))
                        hv = win_halves(raw, "net", by=["brand", "product"])
                        pt = pt.merge(hv, on=["brand", "product"], how="left")
                        pt = pt.rename(columns={"early": "net_early",
                                                "late": "net_late"})
                        pt = pt.nlargest(10, "net_total")
                else:
                    pt = q(f"""
                        SELECT brand, product,
                               SUM(net_early) AS net_early,
                               SUM(net_late) AS net_late,
                               SUM(net_total) AS net_total
                        FROM dash_product_trend
                        WHERE category = '{csql}' {af}
                        GROUP BY 1,2 ORDER BY net_total DESC LIMIT 10
                    """)
                with tcol:
                    st.markdown(f"**{cat}**")
                    if pt.empty:
                        st.caption(f"No {cat} movement in this period.")
                        continue
                    pt["change"] = pct_change(pt.net_late, pt.net_early)
                    shown += 1
                    st.dataframe(pd.DataFrame({
                        "Brand": pt.brand,
                        "Product": pt["product"],
                        "Net $": pt.net_total.round(0),
                        "Change %": pd.to_numeric(
                            pt.change, errors="coerce").round(1),
                    }), use_container_width=True, hide_index=True,
                        column_config={
                            "Net $": st.column_config.NumberColumn(
                                format="$%d"),
                            "Change %": st.column_config.NumberColumn(
                                help="Second half versus first half of the "
                                     "selected window.",
                                format="%.1f%%"),
                        })
            if shown:
                st.markdown('<p class="note">Product names change often as '
                            'SKUs turn over, so read these as examples of '
                            'where the movement sits rather than a stable '
                            'ranking.</p>', unsafe_allow_html=True)

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
    HAVE_BW = table_exists("dash_brand_week")
    HAVE_BPW = table_exists("dash_brand_product_week")

    if not WIN_FULL and not HAVE_BW:
        st.markdown(
            f'<div class="alert a-warn">This tab is showing the whole loaded '
            f'period, not your {n_wk}-week window — the published file '
            f'predates week-level brand data. It will follow the slider after '
            f'the next refresh.</div>', unsafe_allow_html=True)

    # Brands that were merged under one name, said out loud. Silently
    # collapsing two POS spellings into one row is the kind of thing that
    # costs you a meeting when a partner's own numbers do not match.
    if table_exists("dash_brand_alias"):
        al = q("SELECT * FROM dash_brand_alias ORDER BY canonical, net DESC")
        if not al.empty:
            merged = (al.groupby("canonical")["alias"]
                        .apply(lambda s: sorted(set(s)))
                        .to_dict())
            bits = "; ".join(
                f"<b>{c}</b> ← {', '.join(a for a in v if a != c)}"
                for c, v in merged.items()
                if len([a for a in v if a != c]))
            if bits:
                st.markdown(
                    f'<p class="note">Consolidated brands: {bits}. These '
                    f'arrive from the POS under more than one spelling and '
                    f'are reported here as one business.</p>',
                    unsafe_allow_html=True)

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

    # ---- source the scorecard ------------------------------------------
    # Preferred path is dash_brand_week, which carries the week and so can
    # be cut to the slider window. dash_brand_scorecard is the fallback for
    # files built before that table existed; it is whole-period by
    # construction and cannot respond to the window.
    if HAVE_BW:
        bw_raw = q(f"""
            SELECT brand, category, iso_year, iso_week,
                   SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
                   SUM(baskets) AS baskets,
                   SUM(first_basket_customers) AS first_basket,
                   SUM(established_customers)  AS established
            FROM dash_brand_week {wfw}
            GROUP BY 1,2,3,4
        """)
        if bw_raw.empty:
            bs = pd.DataFrame()
        else:
            for c in ("net", "gm", "units", "baskets", "first_basket",
                      "established"):
                bw_raw[c] = pd.to_numeric(bw_raw[c], errors="coerce").fillna(0)
            bs = (bw_raw.groupby("brand", as_index=False)
                        .agg(net=("net", "sum"), gm=("gm", "sum"),
                             units=("units", "sum"),
                             first_basket=("first_basket", "sum"),
                             established=("established", "sum")))
            # A brand's headline category is where most of its money sat in
            # the window — not MIN(category), which is alphabetical accident.
            topcat = (bw_raw.groupby(["brand", "category"], as_index=False)
                            .net.sum()
                            .sort_values("net", ascending=False)
                            .drop_duplicates("brand")
                            .set_index("brand")["category"])
            bs["category"] = bs.brand.map(topcat)
            if HAVE_BPW:
                sk = q(f"""
                    SELECT brand, COUNT(DISTINCT product) AS skus
                    FROM dash_brand_product_week {wfw} GROUP BY 1
                """)
                bs = bs.merge(sk, on="brand", how="left")
            else:
                bs["skus"] = float("nan")
            # Trend against the window's own halves, not the file's.
            half = win_halves(bw_raw, "net", by=["brand"])
            half["trend"] = pct_change(half["late"], half["early"])
            bs = bs.merge(half[["brand", "trend"]], on="brand", how="left")
    else:
        bs = q(f"""
            SELECT brand,
                   MIN(primary_category)            AS category,
                   SUM(net)                         AS net,
                   SUM(gm)                          AS gm,
                   SUM(units)                       AS units,
                   SUM(skus)                        AS skus,
                   SUM(first_basket_customers)      AS first_basket,
                   SUM(established_customers)       AS established
            FROM dash_brand_scorecard {sfilter(keys)}
            GROUP BY 1
        """)
        if not bs.empty:
            bt_all = q(f"""
                SELECT brand, SUM(net_early) AS net_early,
                       SUM(net_late) AS net_late
                FROM dash_brand_trend {sfilter(keys)} GROUP BY 1
            """)
            bt_all["trend"] = pct_change(bt_all["net_late"],
                                         bt_all["net_early"])
            bs = bs.merge(bt_all[["brand", "trend"]], on="brand", how="left")

    if bs.empty:
        st.info("No brand data for the selected stores and window.")
    else:
        # The revenue floor has to scale with the window: $5,000 over
        # thirteen months and $5,000 over four weeks are not the same ask,
        # and a fixed floor empties the table as you narrow the slider.
        floor_default = max(250, int(round(5000 * n_wk / max(len(weeks), 1)
                                           / 250)) * 250)
        min_net = st.slider("Minimum revenue to include", 250, 100000,
                            min(floor_default, 100000), step=250, format="$%d",
                            help="Small brands produce unstable ratios. Raise "
                                 "this to focus on brands with enough volume "
                                 "to rank meaningfully. The default scales "
                                 "with the number of weeks you have selected.")
        bs = bs[bs.net >= min_net].copy()

        if bs.empty:
            st.info("No brands above that revenue threshold in this window.")
        else:
            bs["margin"] = pd.to_numeric(bs.gm, errors="coerce") / \
                pd.to_numeric(bs.net, errors="coerce").replace(0, float("nan")) * 100
            bs["acq_share"] = bs.first_basket / max(bs.first_basket.sum(), 1) * 100

            est_total = bs.established.sum()
            # Established-customer counts are per week in dash_brand_week, so
            # summing them across a multi-week window counts a repeat buyer
            # once per week they shopped. That is fine as a denominator only
            # when the window is the whole file, where it matches the
            # scorecard's own definition.
            has_tenure = est_total > 0 and (WIN_FULL or not HAVE_BW)
            if has_tenure:
                bs["acq_ratio"] = (bs.first_basket /
                                   bs.established.replace(0, float("nan")))
            else:
                bs["acq_ratio"] = float("nan")
                if est_total <= 0:
                    st.warning(
                        "The loaded window is too short to tell new customers "
                        "from established ones — nobody in it is yet 90 days "
                        "past their first purchase. Acquisition ratio is "
                        "hidden until there is more history. Everything else "
                        "below is valid.")
                else:
                    st.info(
                        "Acquisition ratio is a whole-period measure — its "
                        "denominator counts customers 90+ days established, "
                        "which cannot be summed across a narrowed window "
                        "without double-counting repeat buyers. Set the "
                        "slider to all weeks to see it. First-basket counts "
                        "below are exact at any window.")

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

            period_help = ("Net sales across the selected window."
                           if not WIN_FULL else
                           "Net sales across the loaded period.")
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
                    help=period_help, format="$%d"),
                "Margin %": st.column_config.NumberColumn(
                    help=tip("gross margin"), format="%.1f%%"),
                "Trend %": st.column_config.NumberColumn(
                    help="Second half of the selected window versus the "
                         "first half.",
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
                'each is moving. <b>Top SKUs</b> drills into a single '
                'category to rank the brand\'s individual products. '
                '<b>Bought together</b> lists the other '
                'brands most often found in the same transactions — your '
                'readiest bundle and cross-promo candidates. <b>What it '
                'pulls along</b> rolls those partners up by category, which '
                'is the buying read: a stockout on this brand costs you '
                'sales there too.</div>', unsafe_allow_html=True)

    if HAVE_BW:
        bl = q(f"""
            SELECT brand, SUM(net) AS net
            FROM dash_brand_week {wfw}
            GROUP BY 1 HAVING SUM(net) >= 250
            ORDER BY net DESC
        """)
        pick_help = ("Brands with at least $250 net sales in the selected "
                     "window, largest first. Type to search.")
    else:
        bl = q(f"""
            SELECT brand, SUM(net) AS net
            FROM dash_brand_scorecard {sfilter(keys)}
            GROUP BY 1 HAVING SUM(net) >= 1000
            ORDER BY net DESC
        """)
        pick_help = ("Brands with at least $1,000 net sales in the loaded "
                     "period, largest first. Type to search.")

    if bl.empty:
        st.info("No brand data for the selected stores and window.")
    else:
        pick = st.selectbox("Pick a brand", bl.brand.tolist(),
                            key="deep_brand", help=pick_help)
        psql = pick.replace("'", "''")            # safe inside SQL literals

        def num0(v):
            v = pd.to_numeric(v, errors="coerce")
            return float(v) if pd.notna(v) else 0.0

        # ---- snapshot ---------------------------------------------------
        if HAVE_BW:
            bwd = q(f"""
                SELECT category, iso_year, iso_week,
                       SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
                       SUM(first_basket_customers) AS first_basket
                FROM dash_brand_week
                {scoped(f"brand = '{psql}'")}
                GROUP BY 1,2,3
            """)
            for c in ("net", "gm", "units", "first_basket"):
                if c in bwd.columns:
                    bwd[c] = pd.to_numeric(bwd[c], errors="coerce").fillna(0)
            net = float(bwd.net.sum()) if not bwd.empty else 0.0
            gm = float(bwd.gm.sum()) if not bwd.empty else 0.0
            units = float(bwd.units.sum()) if not bwd.empty else 0.0
            fb = int(bwd.first_basket.sum()) if not bwd.empty else 0
            hv = win_halves(bwd, "net")
            e = float(hv["early"].sum()) if len(hv) else 0.0
            l = float(hv["late"].sum()) if len(hv) else 0.0
        else:
            sc = q(f"""
                SELECT SUM(net) AS net, SUM(gm) AS gm, SUM(units) AS units,
                       SUM(first_basket_customers) AS first_basket
                FROM dash_brand_scorecard
                WHERE brand = '{psql}' {and_filter(keys)}
            """).iloc[0]
            tr = q(f"""
                SELECT SUM(net_early) AS e, SUM(net_late) AS l
                FROM dash_brand_trend
                WHERE brand = '{psql}' {and_filter(keys)}
            """).iloc[0]
            net, gm = num0(sc.net), num0(sc.gm)
            units, fb = num0(sc.units), int(num0(sc.first_basket))
            e, l = num0(tr.e), num0(tr.l)
            bwd = pd.DataFrame()

        trend = (l / e - 1) * 100 if e > 0 else float("nan")

        m = st.columns(4)
        m[0].metric("Net sales", f"${net:,.0f}")
        m[1].metric("Gross margin",
                    f"{gm / net * 100:.1f}%" if net > 0 else "—",
                    help=tip("gross margin"))
        m[2].metric("Units", f"{int(units):,}")
        m[3].metric("Trend (2nd half vs 1st)",
                    f"{trend:+.1f}%" if pd.notna(trend) else "—",
                    help="Net sales in the second half of the selected "
                         "window versus the first half.")
        if fb:
            st.markdown(f'<p class="note"><b>{fb:,}</b> customers had '
                        f'<b>{pick}</b> in their first-ever basket — '
                        f'acquisition value on top of the sales line.</p>',
                        unsafe_allow_html=True)

        # ---- category mix + co-purchases ---------------------------------
        if HAVE_BW and not bwd.empty:
            cm = (bwd.groupby("category", as_index=False).net.sum()
                     .query("net > 0").sort_values("net", ascending=False))
            cmh = win_halves(bwd, "net", by=["category"])
            cm = cm.merge(cmh, on="category", how="left")
            cm["net_early"] = cm["early"].fillna(0)
            cm["net_late"] = cm["late"].fillna(0)
        else:
            cm = q(f"""
                SELECT category,
                       SUM(net_total) AS net,
                       SUM(net_early) AS net_early,
                       SUM(net_late)  AS net_late
                FROM dash_brand_trend
                WHERE brand = '{psql}' {and_filter(keys)}
                GROUP BY 1 HAVING SUM(net_total) > 0
                ORDER BY net DESC
            """)

        # Co-purchase pairs are published without a week, so this block is
        # whole-file whatever the slider says. Labelled rather than hidden:
        # pairing behaviour is stable enough that the lifetime read is still
        # the right input for bundling.
        bp = q(f"""
            SELECT other, other_cat, SUM(baskets) AS baskets FROM (
                SELECT brand_b AS other, cat_b AS other_cat,
                       SUM(joint_baskets) AS baskets
                FROM dash_brand_pairs
                WHERE brand_a = '{psql}' {and_filter(keys)}
                GROUP BY 1, 2
                UNION ALL
                SELECT brand_a, cat_a, SUM(joint_baskets)
                FROM dash_brand_pairs
                WHERE brand_b = '{psql}' {and_filter(keys)}
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
                st.info("No category detail for this brand in the selected "
                        "window.")
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
                        help="Second half of the selected window versus the "
                             "first. Shows which way the brand is moving "
                             "inside each category.", format="%.1f%%"),
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
                if not WIN_FULL:
                    st.markdown('<p class="note">Pairings cover the whole '
                                'loaded period, not your selected window — '
                                'basket co-occurrence is published without a '
                                'week.</p>', unsafe_allow_html=True)
                st.markdown('<p class="note">Partners from other categories '
                            'are cross-sells — bundle and merchandise them '
                            'together. Brands from this brand\'s own '
                            'category that never appear here are likely '
                            'substitutes: customers choose between you, not '
                            'in addition to you.</p>', unsafe_allow_html=True)

        # ---- top SKUs within a category ----------------------------------
        st.markdown("##### Top SKUs")
        if not HAVE_BPW:
            st.info("Product-level brand detail is not in the published file "
                    "yet. It appears after the next refresh.")
        else:
            cats = q(f"""
                SELECT category, SUM(net) AS net
                FROM dash_brand_product_week
                {scoped(f"brand = '{psql}'")}
                GROUP BY 1 HAVING SUM(net) > 0
                ORDER BY net DESC
            """)
            if cats.empty:
                st.info(f"No product-level sales for {pick} in this window.")
            else:
                cl, cr = st.columns([2, 1])
                cat_pick = cl.selectbox(
                    "Category", cats.category.tolist(), key="deep_cat",
                    help="Categories this brand sold in during the selected "
                         "window, largest first.")
                rank_by = cr.selectbox("Rank by", ["Net $", "Units",
                                                   "Baskets", "Margin %"],
                                       key="deep_rank")
                csql = cat_pick.replace("'", "''")

                sk = q(f"""
                    SELECT product, iso_year, iso_week,
                           SUM(net) AS net, SUM(gm) AS gm,
                           SUM(units) AS units, SUM(baskets) AS baskets
                    FROM dash_brand_product_week
                    {scoped(f"brand = '{psql}'", f"category = '{csql}'")}
                    GROUP BY 1,2,3
                """)
                if sk.empty:
                    st.info("No SKUs for that combination in this window.")
                else:
                    for c in ("net", "gm", "units", "baskets"):
                        sk[c] = pd.to_numeric(sk[c], errors="coerce").fillna(0)
                    agg = (sk.groupby("product", as_index=False)
                             .agg(net=("net", "sum"), gm=("gm", "sum"),
                                  units=("units", "sum"),
                                  baskets=("baskets", "sum")))
                    hv = win_halves(sk, "net", by=["product"])
                    agg = agg.merge(hv, on="product", how="left")
                    agg["trend"] = pct_change(agg["late"], agg["early"])
                    agg["margin"] = agg.gm / agg.net.replace(0, float("nan")) * 100
                    agg["share"] = agg.net / agg.net.sum() * 100
                    agg["wks"] = (sk[sk.units > 0]
                                  .groupby("product").size()
                                  .reindex(agg["product"]).fillna(0).values)

                    sort_col = {"Net $": "net", "Units": "units",
                                "Baskets": "baskets",
                                "Margin %": "margin"}[rank_by]
                    agg = agg.sort_values(sort_col, ascending=False)

                    # A slider needs room to move. Brands with a handful of
                    # SKUs in a category are common — show them all rather
                    # than asking for a choice between 5 and 5.
                    if len(agg) <= 6:
                        show = agg
                    else:
                        n_show = st.slider("SKUs to show", 5,
                                           min(50, len(agg)),
                                           min(15, len(agg)),
                                           key="deep_sku_n")
                        show = agg.head(n_show)

                    s1, s2, s3 = st.columns(3)
                    s1.metric("SKUs sold", f"{len(agg):,}")
                    s2.metric(f"{cat_pick} net", f"${agg.net.sum():,.0f}")
                    top3 = agg.nlargest(3, "net").net.sum()
                    s3.metric("Top 3 SKU concentration",
                              f"{top3 / agg.net.sum() * 100:.0f}%",
                              help="Share of this brand's revenue in this "
                                   "category coming from its three biggest "
                                   "SKUs. High concentration means the "
                                   "brand's performance here is really one "
                                   "or two products — treat those par "
                                   "levels as critical.")

                    st.dataframe(pd.DataFrame({
                        "SKU": show["product"],
                        "Net $": show.net.round(0),
                        "% of category": show["share"].round(1),
                        "Units": show.units.astype(int),
                        "Baskets": show.baskets.astype(int),
                        "Margin %": show.margin.round(1),
                        "Trend %": pd.to_numeric(show["trend"],
                                                 errors="coerce").round(1),
                        "Weeks selling": show.wks.astype(int),
                    }), use_container_width=True, hide_index=True,
                        column_config={
                        "Net $": st.column_config.NumberColumn(format="$%d"),
                        "% of category": st.column_config.NumberColumn(
                            help="Share of this brand's net sales within "
                                 "this category and window.",
                            format="%.1f%%"),
                        "Margin %": st.column_config.NumberColumn(
                            help=tip("gross margin"), format="%.1f%%"),
                        "Trend %": st.column_config.NumberColumn(
                            help="Second half of the selected window versus "
                                 "the first half.", format="%.1f%%"),
                        "Baskets": st.column_config.NumberColumn(
                            help="Transactions containing this SKU.",
                            format="%d"),
                        "Weeks selling": st.column_config.NumberColumn(
                            help="Weeks in the window where this SKU moved "
                                 "at least one unit. A high net on few weeks "
                                 "is a drop, not a staple.", format="%d"),
                    })

                    splot = show.sort_values("net")
                    fig = px.bar(splot, x="net", y="product",
                                 orientation="h",
                                 color_discrete_sequence=[cat_color(cat_pick)])
                    fig.update_layout(height=120 + 26 * len(splot),
                                      showlegend=False,
                                      margin=dict(l=0, r=0, t=10, b=0),
                                      xaxis_title="net $", yaxis_title="",
                                      plot_bgcolor="rgba(0,0,0,0)")
                    fig.update_xaxes(gridcolor="rgba(0,0,0,.07)")
                    st.plotly_chart(fig, use_container_width=True,
                                    key="deep_sku")
                    st.markdown(
                        f'<p class="note">SKUs below '
                        f'${PRODUCT_FLOOR:,.0f} in lifetime net sales are not '
                        f'published at product level, so a very long tail of '
                        f'one-off items will not appear here.</p>',
                        unsafe_allow_html=True)

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


# -------------------------------------------------------------- accessories
with t_acc:
    st.markdown("#### Accessory performance by store")
    st.markdown(
        '<div class="howto"><b>How to read this tab.</b> Pick a week and '
        'compare the stores on how accessories move: how often an accessory '
        'makes it into a basket, what share of revenue the category earns, '
        'and the long tail — SKUs that sold exactly one unit, what those '
        'sales were worth, and SKUs down to their last unit on the sales '
        'floor (restock candidates).</div>',
        unsafe_allow_html=True)

    ACC_SHORT = {1: "DTBK", 4: "USQ", 2: "5th AVE", 3: "SoHo"}
    acc_keys = [k for k in ACC_SHORT if k in keys]

    _wkopts = weeks.copy()
    _wkopts["wk_date"] = pd.to_datetime(
        _wkopts.iso_year.astype(str) + "-W"
        + _wkopts.iso_week.astype(str).str.zfill(2) + "-1",
        format="%G-W%V-%u", errors="coerce")
    _wklabels = [
        f"Week of {d:%b} {d.day}, {d:%Y}" if pd.notna(d)
        else f"{y} week {w}"
        for d, y, w in zip(_wkopts.wk_date, _wkopts.iso_year,
                           _wkopts.iso_week)]
    # Deliberately built from `weeks`, not WIN: the week picker offers every
    # week in the published file regardless of where the sidebar slider sits.
    # Narrowing it would hide history for no reason — you pick one week here
    # anyway. The window total column below is what respects the slider.
    acc_pick = st.selectbox("Week", list(range(len(_wkopts))),
                            format_func=lambda i: _wklabels[i],
                            index=len(_wkopts) - 1, key="acc_week")
    st.caption(
        f"Week list covers the full published range — all {len(_wkopts)} "
        f"weeks, {_wkopts.wk_date.min():%b %d, %Y} to "
        f"{_wkopts.wk_date.max():%b %d, %Y}. The sidebar slider does not "
        f"trim it; it drives the window column only.")
    wy = int(_wkopts.iso_year.iloc[acc_pick])
    ww = int(_wkopts.iso_week.iloc[acc_pick])
    if PARTIAL_WEEK and acc_pick == len(_wkopts) - 1:
        st.markdown(
            f'<div class="alert a-warn">This week is still in progress — '
            f'<b>{PARTIAL_DAYS} trading day'
            f'{"s" if PARTIAL_DAYS != 1 else ""} so far</b>. Counts and '
            f'revenue will grow as the week completes; the two percentage '
            f'rows are already comparable.</div>',
            unsafe_allow_html=True)

    acc_cat = q(f"""
        SELECT store_key,
               SUM(CASE WHEN category ILIKE 'Accessor%' THEN baskets_with
                        ELSE 0 END) AS acc_baskets,
               SUM(CASE WHEN category ILIKE 'Accessor%' THEN net
                        ELSE 0 END) AS acc_net
        FROM dash_category_week
        WHERE iso_year = {wy} AND iso_week = {ww} {af}
        GROUP BY 1
    """)
    acc_tot = q(f"""
        SELECT store_key, SUM(baskets) AS baskets, SUM(net) AS net
        FROM dash_basket_week
        WHERE iso_year = {wy} AND iso_week = {ww} {af}
        GROUP BY 1
    """)

    # The single-unit rows need product-level detail. Those tables are
    # published by the refresh on its own schedule, so until they exist the
    # rows show a dash instead of failing.
    have_apw = table_exists("dash_acc_product_week")
    have_api = table_exists("dash_acc_product_inv")
    acc_single = q(f"""
        SELECT store_key,
               COUNT(*) FILTER (units = 1) AS single_skus,
               COALESCE(SUM(net) FILTER (units = 1), 0) AS single_net
        FROM dash_acc_product_week
        WHERE iso_year = {wy} AND iso_week = {ww} {af}
        GROUP BY 1
    """) if have_apw else pd.DataFrame()
    acc_onhand = q(f"""
        SELECT store_key, COUNT(*) FILTER (qoh = 1) AS single_onhand
        FROM dash_acc_product_inv
        WHERE snapshot_date = (SELECT MAX(snapshot_date)
                               FROM dash_acc_product_inv) {af}
        GROUP BY 1
    """) if have_api else pd.DataFrame()

    def acc_lookup(frame: pd.DataFrame, store_key: int, col: str):
        if frame.empty or "store_key" not in frame.columns:
            return None
        hit = frame.loc[frame.store_key == store_key, col]
        if hit.empty or pd.isna(hit.iloc[0]):
            return None
        return float(hit.iloc[0])

    ACC_ROWS = ["% transactions w/ accessory",
                "% revenue by category",
                "# single unit skus sold",
                "revenue of single unit skus sold",
                "# single unit skus on hand"]
    acc_tbl = pd.DataFrame(index=ACC_ROWS,
                           columns=[ACC_SHORT[k] for k in acc_keys])
    for k in acc_keys:
        baskets = acc_lookup(acc_tot, k, "baskets")
        net_tot = acc_lookup(acc_tot, k, "net")
        acc_b = acc_lookup(acc_cat, k, "acc_baskets")
        acc_n = acc_lookup(acc_cat, k, "acc_net")
        s_skus = acc_lookup(acc_single, k, "single_skus")
        s_net = acc_lookup(acc_single, k, "single_net")
        s_oh = acc_lookup(acc_onhand, k, "single_onhand")
        col = ACC_SHORT[k]
        acc_tbl.loc["% transactions w/ accessory", col] = (
            f"{acc_b / baskets:.2%}"
            if acc_b is not None and baskets else "—")
        acc_tbl.loc["% revenue by category", col] = (
            f"{acc_n / net_tot:.2%}"
            if acc_n is not None and net_tot else "—")
        acc_tbl.loc["# single unit skus sold", col] = (
            f"{int(s_skus)}" if s_skus is not None else "—")
        acc_tbl.loc["revenue of single unit skus sold", col] = (
            f"${s_net:,.0f}" if s_net is not None else "—")
        acc_tbl.loc["# single unit skus on hand", col] = (
            f"{int(s_oh)}" if s_oh is not None else "—")

    if acc_keys and not acc_tot.empty:
        st.dataframe(acc_tbl, use_container_width=True)
    else:
        st.caption("No data for the selected stores in this week.")

    # ---- the same two ratios across the slider window --------------------
    # One week is a small sample for a category this thin: a single busy
    # Saturday moves accessory penetration by a point. The window read is
    # the one to act on; the week above is for spotting the outlier.
    st.markdown("##### Across the selected window")
    accw_cat = q(f"""
        SELECT store_key,
               SUM(CASE WHEN category ILIKE 'Accessor%' THEN baskets_with
                        ELSE 0 END) AS acc_baskets,
               SUM(CASE WHEN category ILIKE 'Accessor%' THEN net
                        ELSE 0 END) AS acc_net
        FROM dash_category_week {wfw}
        GROUP BY 1
    """)
    accw_tot = q(f"""
        SELECT store_key, SUM(baskets) AS baskets, SUM(net) AS net
        FROM dash_basket_week {wfw}
        GROUP BY 1
    """)
    ACCW_ROWS = ["% transactions w/ accessory", "% revenue by category",
                 "accessory net $"]
    accw_tbl = pd.DataFrame(index=ACCW_ROWS,
                            columns=[ACC_SHORT[k] for k in acc_keys])
    for k in acc_keys:
        col = ACC_SHORT[k]
        baskets = acc_lookup(accw_tot, k, "baskets")
        net_tot = acc_lookup(accw_tot, k, "net")
        acc_b = acc_lookup(accw_cat, k, "acc_baskets")
        acc_n = acc_lookup(accw_cat, k, "acc_net")
        accw_tbl.loc["% transactions w/ accessory", col] = (
            f"{acc_b / baskets:.2%}" if acc_b is not None and baskets else "—")
        accw_tbl.loc["% revenue by category", col] = (
            f"{acc_n / net_tot:.2%}" if acc_n is not None and net_tot else "—")
        accw_tbl.loc["accessory net $", col] = (
            f"${acc_n:,.0f}" if acc_n is not None else "—")
    if acc_keys and not accw_tot.empty:
        st.dataframe(accw_tbl, use_container_width=True)
        st.caption(
            f"{len(WIN)} week{'s' if len(WIN) != 1 else ''}, "
            f"{WIN_START:%b %d, %Y} → {WIN_END:%b %d, %Y}. Move the sidebar "
            f"slider to change this.")

    st.markdown(
        '<p class="note"><b>Single unit</b> means exactly one unit: sold '
        'one unit in the chosen week, or one unit left on the sales floor. '
        'On-hand counts come from the latest inventory snapshot, so they '
        'read “right now” rather than as of the chosen week.</p>',
        unsafe_allow_html=True)
    if not (have_apw and have_api):
        st.markdown(
            '<p class="note">The last three rows light up after the next '
            'data refresh publishes product-level accessory detail — the '
            'two percentage rows already run on existing tables.</p>',
            unsafe_allow_html=True)


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
        FROM dash_basket_week {wfw}
        GROUP BY 1,2
        ORDER BY 1,2
    """)
    red_store = q(f"""
        SELECT store_key,
               SUM(baskets) AS baskets,
               SUM(redeem_baskets) AS redeem_baskets,
               SUM(redeem_value) AS redeem_value,
               SUM(net) AS net
        FROM dash_basket_week {wfw}
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
            # fixedrange on both y-axes: y2 overlays y and plotly rescales
            # them independently, so a zoom or pan detaches the rate line
            # from the bars.
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                              xaxis=dict(fixedrange=True),
                              yaxis=dict(title="Redemption $", tickformat="$~s",
                                         gridcolor="rgba(0,0,0,.07)",
                                         fixedrange=True),
                              yaxis2=dict(title="Rate %", overlaying="y", side="right",
                                          showgrid=False, tickformat=".1f",
                                          ticksuffix="%", fixedrange=True),
                              hovermode="x unified",
                              legend=dict(orientation="h", y=1.12, x=0,
                                          title_text=""),
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, key="pc8",
                            config={"scrollZoom": False, "displaylogo": False,
                                    "modeBarButtonsToRemove": [
                                        "zoom2d", "pan2d", "select2d",
                                        "lasso2d", "zoomIn2d", "zoomOut2d",
                                        "autoScale2d", "resetScale2d"]})

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
                fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
                st.plotly_chart(fig, use_container_width=True, key="pc9")
                st.markdown('<p class="note"><b>Bar height</b> = total redemption '
                            'dollars. <b>Color</b> = redemption rate (darker green '
                            '= higher engagement).</p>',
                            unsafe_allow_html=True)
            else:
                st.markdown('<p class="note">Select multiple stores in the '
                            'sidebar to compare redemption performance.</p>',
                            unsafe_allow_html=True)

        # --- Redemptions by method (channel) ------------------------------
        if has_col("dash_redemption_day", "channel"):
            red_ch = q(f"""
                SELECT channel,
                       SUM(redemptions)  AS redemptions,
                       SUM(redeem_value) AS redeem_value
                FROM dash_redemption_day {day_scoped()}
                GROUP BY 1 ORDER BY redeem_value DESC
            """)
            if not red_ch.empty and red_ch.redemptions.sum() > 0:
                st.divider()
                st.markdown("#### Redemptions by method")
                st.markdown(
                    '<div class="howto"><b>How to read this.</b> Method is '
                    '<b>how the order was taken</b> — rung at an '
                    '<b>In-Store</b> register, placed on <b>Non-Stop</b>, or '
                    'sent out for <b>Delivery</b> — mapped from the register '
                    'each redemption went through. Use it to see where '
                    'loyalty value is actually being claimed: a method with '
                    'lots of redemptions but low redemption dollars is many '
                    'small claims; the reverse is a few big ones.</div>',
                    unsafe_allow_html=True)
                tot_v = red_ch.redeem_value.sum()
                tot_n = red_ch.redemptions.sum()
                Lc, Rc = st.columns([2, 3])
                with Lc:
                    st.dataframe(pd.DataFrame({
                        "Method": red_ch.channel,
                        "Redemptions": red_ch.redemptions,
                        "Redemption $": red_ch.redeem_value.round(0),
                        "% of value": (red_ch.redeem_value
                                       / tot_v * 100).round(1),
                        "$ per claim": (red_ch.redeem_value
                                        / red_ch.redemptions.replace(
                                            0, np.nan)).round(2),
                    }), use_container_width=True, hide_index=True,
                        column_config={
                            "Redemptions": st.column_config.NumberColumn(
                                format="%d"),
                            "Redemption $": st.column_config.NumberColumn(
                                format="$%d"),
                            "% of value": st.column_config.NumberColumn(
                                format="%.1f%%"),
                            "$ per claim": st.column_config.NumberColumn(
                                format="$%.2f"),
                        })
                with Rc:
                    red_ch["label"] = red_ch.apply(
                        lambda r: f"{int(r.redemptions):,} claims · "
                                  f"{r.redeem_value/tot_v*100:.0f}% of $",
                        axis=1)
                    fig = px.bar(red_ch.sort_values("redeem_value"),
                                 x="redeem_value", y="channel",
                                 orientation="h", text="label",
                                 color_discrete_sequence=[ACCENT])
                    fig.update_traces(textposition="outside",
                                      textfont_size=11, cliponaxis=False)
                    fig.update_layout(height=260,
                                      margin=dict(l=0, r=0, t=10, b=0),
                                      xaxis_title="Redemption $",
                                      yaxis_title="",
                                      xaxis=dict(gridcolor="rgba(0,0,0,.07)",
                                                 tickformat="$~s"),
                                      plot_bgcolor="rgba(0,0,0,0)")
                    st.plotly_chart(fig, use_container_width=True,
                                    key="pc_channel")

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
        st.markdown(
            '<div class="howto"><b>How to read this.</b> Pick a brand — or a '
            'promo family like Secret Drops — to see what was actually rung '
            'up for its redemptions. The same offer appears under different '
            'names over time ("Loyalty …", the "Loytaly …" typo, "Travel '
            'Club …"), so rows group by the product, not the offer name. '
            'Where a menu item was swapped at the register, the SKU shown is '
            'the one the customer actually received. A substitution that '
            'could not be traced to a product is listed as unresolved rather '
            'than by its offer name, so it is never mistaken for a SKU.'
            '</div>',
            unsafe_allow_html=True)

        brand_opts = sorted(attributed.brand.dropna().unique())
        sel_brand = st.selectbox("Select a brand", brand_opts,
                                 key="redeem_brand_sku")

        # Group by the matched product (strain-level) when the published file
        # has it; older files only carry offer names. Offer-name variants —
        # "Loyalty …", the "Loytaly …" typo — roll up into one row per strain.
        try:
            sku = q(f"""
                SELECT CASE
                         WHEN product IS NOT NULL THEN product
                         WHEN lower(offer_name) LIKE '%substitution%'
                           THEN 'Not resolved to a product'
                         ELSE offer_name
                       END AS sku,
                       CASE
                         WHEN product IS NULL
                          AND lower(offer_name) LIKE '%substitution%'
                           THEN 'Unresolved'
                         ELSE category
                       END AS category,
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
            _real = sku[sku.sku.astype(str) != "Not resolved to a product"]
            m[0].metric("SKUs redeemed", f"{len(_real):,}")
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
            fig.update_xaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
            st.plotly_chart(fig, use_container_width=True, key="pc17")

        # --- Off-menu picks: what redeemers chose instead -------------------
        st.divider()
        st.markdown("##### Chosen instead — off-menu picks")
        st.markdown(
            '<div class="howto"><b>How to read this.</b> A substitution is '
            'rung when the reward a customer wants is not in stock — they '
            'pick something of similar value and the discount is written '
            'down to the price of that item. Because the discount equals '
            'what they took, the item is identifiable from the amount. Rows '
            'where the customer received the advertised reward are excluded, '
            'so what remains is the demand your rewards menu is not serving '
            '— ranked by redemption dollars, a ready-made shortlist of '
            'candidates. Not every substitution resolves; the unmatched '
            'count is shown below.</div>',
            unsafe_allow_html=True)
        subs_raw = q(f"""
            SELECT offer_name, brand, product AS sku, category,
                   SUM(redemptions)  AS redemptions,
                   SUM(redeem_value) AS spend
            FROM dash_offer_performance
            WHERE match_method = 'substituted-line'
              AND lower(offer_name) LIKE '%substitution%'
              AND product IS NOT NULL {af}
            GROUP BY 1,2,3,4
        """)
        if subs_raw.empty:
            st.info("No substitutions resolved yet. They appear after the "
                    "next data rebuild, once re-matching has run.")
        else:
            on = subs_raw.apply(
                lambda r: is_on_menu(r.offer_name, r.brand, r.category, r.sku),
                axis=1)
            kept, menu_rows = subs_raw[~on], subs_raw[on]

            subs = (kept.groupby(["sku", "category"], as_index=False)
                        .agg(redemptions=("redemptions", "sum"),
                             spend=("spend", "sum"),
                             offers=("offer_name", "nunique"))
                        .sort_values("spend", ascending=False))

            if subs.empty:
                st.info("Every resolved substitution was the advertised "
                        "reward — no off-menu picks in this window.")
            else:
                st.dataframe(pd.DataFrame({
                    "SKU chosen": subs.sku,
                    "Category": subs.category,
                    "Times picked": subs.redemptions,
                    "Redemption $": subs.spend.round(0),
                    "Via # of tiers": subs.offers,
                }), use_container_width=True, hide_index=True, column_config={
                    "Times picked": st.column_config.NumberColumn(format="%d"),
                    "Redemption $": st.column_config.NumberColumn(format="$%d"),
                    "Via # of tiers": st.column_config.NumberColumn(
                        format="%d"),
                })

                st.markdown("###### Where the gap is, by tier")
                tier = (kept.assign(tier=kept.offer_name)
                            .groupby(["tier", "category"], as_index=False)
                            .agg(picks=("redemptions", "sum"),
                                 spend=("spend", "sum"))
                            .sort_values(["tier", "spend"], ascending=False))
                st.dataframe(pd.DataFrame({
                    "Tier": tier.tier,
                    "Category taken": tier.category,
                    "Times picked": tier.picks,
                    "Redemption $": tier.spend.round(0),
                }), use_container_width=True, hide_index=True, column_config={
                    "Times picked": st.column_config.NumberColumn(format="%d"),
                    "Redemption $": st.column_config.NumberColumn(format="$%d"),
                })

            if len(menu_rows):
                st.markdown(
                    f'<p class="note">'
                    f'{int(menu_rows.redemptions.sum()):,} substitutions were '
                    f'the advertised reward for their tier and are excluded '
                    f'above — the discount is also used as the ordinary way '
                    f'to ring a reward, so substitution volume is an upper '
                    f'bound on stockouts, not a measure of them.</p>',
                    unsafe_allow_html=True)

            unres = q(f"""
                SELECT SUM(redemptions) AS n
                FROM dash_offer_performance
                WHERE match_method = 'unmatched'
                  AND lower(offer_name) LIKE '%substitution%' {af}
            """)
            if len(unres) and pd.notna(unres.n.iloc[0]) and unres.n.iloc[0]:
                st.markdown(
                    f'<p class="note">{int(unres.n.iloc[0]):,} substitutions '
                    f'could not be resolved to a product — usually a '
                    f'redemption worth more than anything in the basket, or a '
                    f'reward split across items.</p>',
                    unsafe_allow_html=True)

        st.divider()
        st.markdown("##### Spend against basket size")
        st.markdown('<p class="note">Each point is a brand or promo family. '
                    'Right means redeemed often; up means customers spend '
                    'big when they redeem. The upper right is where the '
                    'programme is earning its keep.</p>',
                    unsafe_allow_html=True)
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



# -------------------------------------------------------------- projections
with t_projections:
    st.markdown("#### Next Quarter Projection")
    st.markdown(f'<p class="note">Simple linear projections fitted to the '
                f'<b>{len(WIN)} week{"s" if len(WIN) != 1 else ""}</b> you '
                f'have selected in the sidebar, extended 13 weeks forward. '
                f'Use as a baseline, not a forecast.</p>',
                unsafe_allow_html=True)
    if len(WIN) < 12:
        st.markdown(
            f'<div class="alert a-warn">A 13-week projection off '
            f'{len(WIN)} weeks of history will swing hard on one unusual '
            f'week. Widen the sidebar window to at least 12 weeks before '
            f'quoting these numbers.</div>', unsafe_allow_html=True)
    proj_bw = q(f"""
        SELECT iso_year, iso_week, SUM(baskets) AS baskets, SUM(net) AS net
        FROM dash_basket_week {wfw}
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
                          yaxis=dict(title="Net $", tickformat="$~s",
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
            FROM dash_category_week {wfw}
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
                                  yaxis=dict(title="Net $", tickformat="$~s",
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

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Churn Map (Categories)", "Store Opportunities", "Brand Promos",
         "Brand Discount ROI"])

    # Declared here, rendered here. Tab ORDER comes from the list above, so
    # this block sitting ahead of `with tab1:` costs nothing and keeps the
    # three original blocks untouched.
    with tab4:
        render_brand_roi(q=q, keys=keys, stores=STORES,
                         heading=None, table_exists=table_exists,
                         accent=ACCENT, series=SERIES)

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


# ===========================================================================
# Brand Takeover tracker
# ===========================================================================

# The takeover calendar. Add a new takeover by copying one of these blocks.
# `patterns` are matched case-insensitively against the brand field, so
# spelling variants ("RYTHM", "Rythm") roll up into one takeover.
TAKEOVERS = [
    {"name": "Rythm (GTI)", "start": "2026-03-01", "end": "2026-03-31",
     "notes": "Full month, GTI-scale",
     "patterns": ["rythm", "rhythm"]},
    {"name": "Select (Curaleaf)", "start": "2026-04-01", "end": "2026-04-15",
     "notes": "420 campaign — first half",
     "patterns": ["select"]},
    {"name": "Dark Heart (Curaleaf)", "start": "2026-04-16", "end": "2026-04-30",
     "notes": "420 campaign — second half",
     "patterns": ["dark heart", "darkheart"]},
    {"name": "Woodstock", "start": "2026-05-01", "end": "2026-05-14",
     "notes": "Staff samples arrived May 11",
     "patterns": ["woodstock"]},
    {"name": "Timeless", "start": "2026-05-14", "end": "2026-06-07",
     "notes": "Installed across all 4 stores May 14",
     "patterns": ["timeless"]},
    {"name": "Ruby Farms", "start": "2026-07-16", "end": "2026-08-17",
     "notes": "4-week SBC, all 4 stores + e-comm",
     "patterns": ["ruby farms", "ruby"]},
]


def _fact_sql(cols: list[str]) -> str | None:
    """Build a fact_line SELECT from whatever columns the local build
    actually carries. Older files have no product or units column, and a
    hard-coded SELECT would fail outright there."""
    have = set(cols)
    if not {"store_key", "txn_ts", "brand", "basket_id",
            "net_sales"}.issubset(have):
        return None
    sel = ["store_key", "txn_ts", "brand", "basket_id", "net_sales"]
    if "gross_margin" in have:
        sel.append("gross_margin")
    for cand in ("units", "quantity", "qty"):
        if cand in have:
            sel.append(f"{cand} AS units")
            break
    for cand in ("product", "product_name", "sku", "item_name"):
        if cand in have:
            sel.append(f"{cand} AS product")
            break
    return f"SELECT {', '.join(sel)} FROM fact_line WHERE brand IS NOT NULL"


@st.cache_data(ttl=CACHE_MINUTES * 60)
def _fact_line() -> pd.DataFrame:
    """Full line detail: published file first, then the local build — the
    same two-step the Promo Lab uses."""
    cols = list(q("SELECT * FROM fact_line LIMIT 0").columns)
    if cols:
        sql = _fact_sql(cols)
        out = q(sql) if sql else pd.DataFrame()
        if not out.empty and "brand" in out.columns:
            out["brand"] = canon_brand(out["brand"])
        return out
    import duckdb
    for cand in [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "tta.duckdb"),
                 r"C:\Users\User\cerebral\Cerebral\tta.duckdb"]:
        if not os.path.exists(cand):
            continue
        try:
            con = duckdb.connect(cand, read_only=True)
            cols = [r[1] for r in
                    con.execute("PRAGMA table_info('fact_line')").fetchall()]
            sql = _fact_sql(cols)
            out = con.execute(sql).df() if sql else pd.DataFrame()
            con.close()
            if not out.empty:
                if "brand" in out.columns:
                    out["brand"] = canon_brand(out["brand"])
                return out
        except Exception:
            pass
    return pd.DataFrame()


@st.cache_data(ttl=CACHE_MINUTES * 60)
def takeover_daily() -> pd.DataFrame:
    """Day x store x brand sales: day, store_key, brand, net, units, baskets,
    gm (whichever of the last three exist).

    The published dashboard file is weekly by design; day-level numbers come
    from a small dash_brand_day table once the refresh builds it, or from
    fact_line directly when the app runs next to the full local data."""
    d = q("SELECT * FROM dash_brand_day")
    if not d.empty:
        d = d.rename(columns={"date": "day"})
        d["day"] = pd.to_datetime(d["day"], errors="coerce")
        return d.dropna(subset=["day"])

    fl = _fact_line()
    if fl.empty:
        return pd.DataFrame()
    fl["day"] = pd.to_datetime(fl["txn_ts"], errors="coerce").dt.normalize()
    fl = fl.dropna(subset=["day"])
    agg = {"net": ("net_sales", "sum"),
           "baskets": ("basket_id", "nunique")}
    if "units" in fl.columns:
        agg["units"] = ("units", "sum")
    if "gross_margin" in fl.columns:
        agg["gm"] = ("gross_margin", "sum")
    return (fl.groupby(["day", "store_key", "brand"])
              .agg(**agg).reset_index())


def takeover_stats(daily: pd.DataFrame, tk: dict, keys: list[int]) -> dict:
    """Per-day rates for the takeover window and its two baselines.

    Everything is a PER CALENDAR DAY rate. Takeover windows do not align
    with the ISO weeks the rest of the dashboard uses, and a takeover can
    still be in progress — raw totals would mislead in both cases."""
    start = pd.Timestamp(tk["start"])
    end = pd.Timestamp(tk["end"])
    pat = "|".join(re.escape(p) for p in tk["patterns"])

    d = daily[daily.store_key.isin(keys)]
    if d.empty:
        return {}
    last_day = d.day.max()
    b = d[d.brand.fillna("").str.lower().str.contains(pat, na=False)]
    matched = sorted(b.brand.dropna().unique().tolist())

    if last_day < start:
        return {"status": "upcoming", "matched": matched}
    eff_end = min(end, last_day)
    status = "active" if last_day < end else "done"

    def rates(frame, lo, hi, tot):
        n = (hi - lo).days + 1
        if n <= 0:
            return None
        w = frame[(frame.day >= lo) & (frame.day <= hi)]
        t = tot[(tot.day >= lo) & (tot.day <= hi)]
        net = w.net.sum()
        tot_net = t.net.sum()
        return {"days": n, "net": net, "net_pd": net / n,
                "baskets_pd": (w.baskets.sum() / n
                               if "baskets" in w.columns else np.nan),
                "units_pd": (w.units.sum() / n
                             if "units" in w.columns else np.nan),
                "share": net / tot_net * 100 if tot_net > 0 else np.nan,
                "gm_pct": (w.gm.sum() / net * 100
                           if ("gm" in w.columns and net > 0) else np.nan)}

    during = rates(b, start, eff_end, d)
    prior = rates(b, start - pd.Timedelta(days=28),
                  start - pd.Timedelta(days=1), d)

    # Rest-of-period baseline: every loaded day outside the takeover window.
    n_all = (last_day - d.day.min()).days + 1
    n_out = n_all - during["days"]
    b_out = b[(b.day < start) | (b.day > eff_end)]
    tot_out = d[(d.day < start) | (d.day > eff_end)].net.sum()
    net_out = b_out.net.sum()
    period = {"days": n_out, "net": net_out, "net_pd": net_out / n_out,
              "baskets_pd": (b_out.baskets.sum() / n_out
                             if "baskets" in b_out.columns else np.nan),
              "units_pd": (b_out.units.sum() / n_out
                           if "units" in b_out.columns else np.nan),
              "share": net_out / tot_out * 100 if tot_out > 0 else np.nan,
              "gm_pct": (b_out.gm.sum() / net_out * 100
                         if ("gm" in b_out.columns
                             and net_out > 0) else np.nan)}

    return {"status": status, "matched": matched, "start": start,
            "end": end, "eff_end": eff_end, "last_day": last_day,
            "during": during, "prior": prior, "period": period,
            "planned_days": (end - start).days + 1}


def _lift(now, base):
    if base and base > 0:
        return (now / base - 1) * 100
    return np.nan


def render_takeovers():
    st.subheader("Brand Takeovers")
    st.markdown(
        '<div class="howto"><b>How to read this.</b> Each takeover window is '
        'measured <b>per day</b> and compared against two baselines, side by '
        'side: the <b>4 weeks before</b> the takeover, and the brand\'s '
        'average across <b>the rest of the loaded data</b>. Per-day rates '
        'keep a half-month takeover (Select, Dark Heart) comparable with a '
        'full-month one (Rythm), and they keep a takeover that is still '
        'running honest.<br><br>'
        '<b>Share of store</b> is the brand\'s slice of total store sales — '
        'the number that tells you whether the takeover moved the brand or '
        'whether the whole store simply had a good month. A takeover that '
        'lifts the brand\'s sales but not its share mostly rode the '
        'tide.</div>', unsafe_allow_html=True)

    daily = takeover_daily()
    if daily.empty:
        st.info(
            "This tab reads day-level brand sales. On your own computer it "
            "uses the full local data file automatically. Online it fills in "
            "after the next data refresh — the refresh now publishes the "
            "small daily table (`dash_brand_day`) it reads.")
        return

    for col in ("units", "baskets", "gm"):
        if col not in daily.columns:
            daily[col] = np.nan

    # ---- scorecard across all takeovers -----------------------------------
    rows = []
    details = {}
    for tk in TAKEOVERS:
        s = takeover_stats(daily, tk, keys)
        details[tk["name"]] = s
        # %-d is Unix-only; building the day number by hand works everywhere.
        _s, _e = pd.Timestamp(tk["start"]), pd.Timestamp(tk["end"])
        win = f"{_s:%b} {_s.day} – {_e:%b} {_e.day}"
        if not s or s.get("status") == "upcoming":
            rows.append({"Takeover": tk["name"], "Window": win,
                         "Status": "Upcoming", "Net/day": np.nan,
                         "vs prior 4 wks %": np.nan,
                         "vs period avg %": np.nan,
                         "Share during %": np.nan,
                         "Share prior %": np.nan,
                         "Baskets/day": np.nan})
            continue
        du, pr, pe = s["during"], s["prior"], s["period"]
        rows.append({
            "Takeover": tk["name"], "Window": win,
            "Status": ("In progress — day "
                       f"{du['days']} of {s['planned_days']}"
                       if s["status"] == "active" else "Complete"),
            "Net/day": du["net_pd"],
            "vs prior 4 wks %": _lift(du["net_pd"],
                                      pr["net_pd"] if pr else np.nan),
            "vs period avg %": _lift(du["net_pd"], pe["net_pd"]),
            "Share during %": du["share"],
            "Share prior %": pr["share"] if pr else np.nan,
            "Baskets/day": du["baskets_pd"],
        })
    score = pd.DataFrame(rows)

    st.markdown("##### Takeover scorecard")
    st.markdown('<p class="note">Every window, ranked side by side. The two '
                '<b>vs</b> columns are the lift in sales per day against each '
                'baseline — green means the takeover beat it.</p>',
                unsafe_allow_html=True)
    st.dataframe(
        score.style.format({
            "Net/day": "${:,.0f}", "vs prior 4 wks %": "{:+.0f}%",
            "vs period avg %": "{:+.0f}%", "Share during %": "{:.1f}%",
            "Share prior %": "{:.1f}%", "Baskets/day": "{:,.1f}"}),
        use_container_width=True, hide_index=True)
    st.download_button("Download scorecard (CSV)", score.to_csv(index=False),
                       "takeover_scorecard.csv", "text/csv", key="tk_dl")

    # ---- detail view -------------------------------------------------------
    st.divider()
    pick = st.selectbox("Takeover to inspect",
                        [t["name"] for t in TAKEOVERS], key="tk_pick")
    tk = next(t for t in TAKEOVERS if t["name"] == pick)
    s = details[pick]

    st.caption(f"**{pick}** · {tk['start']} → {tk['end']} · {tk['notes']}")
    if s.get("matched"):
        st.caption("Matched brand names in the data: "
                   + ", ".join(s["matched"]))

    if not s or s.get("status") == "upcoming":
        st.info("This takeover has not started yet in the loaded data.")
        return
    du, pr, pe = s["during"], s["prior"], s["period"]
    if du["net"] <= 0:
        st.warning("No sales for this brand inside the takeover window. "
                   "Check the matched brand names above against the menu.")
        return

    if s["status"] == "active":
        st.markdown(
            f'<div class="alert a-warn">This takeover is <b>still running</b> '
            f'— day <b>{du["days"]} of {s["planned_days"]}</b>. Figures are '
            f'per day, so they are comparable, but the story is not '
            f'finished.</div>', unsafe_allow_html=True)

    m = st.columns(4)
    m[0].metric("Net sales / day", f"${du['net_pd']:,.0f}",
                delta=(f"{_lift(du['net_pd'], pr['net_pd']):+.0f}% vs prior 4 wks"
                       if pr and pr["net_pd"] > 0 else None))
    m[1].metric("Share of store", f"{du['share']:.1f}%",
                delta=(f"{du['share'] - pr['share']:+.1f} pts vs prior"
                       if pr and pd.notna(pr["share"]) else None))
    if pd.notna(du["baskets_pd"]):
        m[2].metric("Baskets w/ brand / day", f"{du['baskets_pd']:,.1f}",
                    delta=(f"{_lift(du['baskets_pd'], pr['baskets_pd']):+.0f}%"
                           if pr and pr["baskets_pd"] > 0 else None))
    if pd.notna(du["units_pd"]):
        m[3].metric("Units / day", f"{du['units_pd']:,.1f}",
                    delta=(f"{_lift(du['units_pd'], pr['units_pd']):+.0f}%"
                           if pr and pr["units_pd"] > 0 else None))

    # --- the two baselines, side by side ------------------------------------
    st.markdown("##### During the takeover vs both baselines")
    base_rows = []
    for label, r in (("During takeover", du),
                     ("Prior 4 weeks", pr),
                     ("Rest of period (avg day)", pe)):
        if not r:
            continue
        base_rows.append({
            "Period": label, "Days": r["days"],
            "Net/day": r["net_pd"], "Units/day": r["units_pd"],
            "Baskets/day": r["baskets_pd"], "Share of store %": r["share"],
            "Margin %": r["gm_pct"]})
    st.dataframe(
        pd.DataFrame(base_rows).style.format({
            "Net/day": "${:,.0f}", "Units/day": "{:,.1f}",
            "Baskets/day": "{:,.1f}", "Share of store %": "{:.1f}%",
            "Margin %": "{:.0f}%"}),
        use_container_width=True, hide_index=True)

    # --- per-store breakdown -------------------------------------------------
    st.markdown("##### Store by store")
    st.markdown('<p class="note">Where the takeover landed. A brand that only '
                'moved in one store tells you where its audience actually '
                'shops — useful when negotiating the next takeover.</p>',
                unsafe_allow_html=True)
    st_rows = []
    for k in keys:
        ds = takeover_stats(daily, tk, [k])
        if not ds or not ds.get("during") or ds["during"]["net"] <= 0:
            continue
        d_s, p_s = ds["during"], ds["prior"]
        st_rows.append({
            "Store": STORES.get(k, str(k)),
            "Net/day": d_s["net_pd"],
            "vs prior 4 wks %": _lift(d_s["net_pd"],
                                      p_s["net_pd"] if p_s else np.nan),
            "Share during %": d_s["share"],
            "Share prior %": p_s["share"] if p_s else np.nan,
            "Baskets/day": d_s["baskets_pd"]})
    if st_rows:
        st.dataframe(
            pd.DataFrame(st_rows).style.format({
                "Net/day": "${:,.0f}", "vs prior 4 wks %": "{:+.0f}%",
                "Share during %": "{:.1f}%", "Share prior %": "{:.1f}%",
                "Baskets/day": "{:,.1f}"}),
            use_container_width=True, hide_index=True)
    else:
        st.info("No per-store data for this takeover and store selection.")

    # --- daily chart with the window shaded ----------------------------------
    st.markdown("##### Daily sales, takeover window shaded")
    pat = "|".join(re.escape(p) for p in tk["patterns"])
    b_all = daily[daily.store_key.isin(keys)
                  & daily.brand.fillna("").str.lower().str.contains(pat,
                                                                    na=False)]
    ts = (b_all.groupby("day").net.sum().reset_index().sort_values("day"))
    fig = px.bar(ts, x="day", y="net",
                 color_discrete_sequence=[ACCENT_SOFT],
                 labels={"day": "", "net": "Net sales $ / day"})
    fig.add_vrect(x0=s["start"], x1=s["eff_end"], fillcolor=ACCENT,
                  opacity=0.15, line_width=0,
                  annotation_text="takeover", annotation_position="top left")
    if pr and pr["net_pd"] > 0:
        fig.add_hline(y=pr["net_pd"], line_dash="dot", line_color="#888",
                      annotation_text="prior 4 wks / day",
                      annotation_position="bottom right")
    if pe["net_pd"] > 0:
        fig.add_hline(y=pe["net_pd"], line_dash="dash", line_color=ACCENT,
                      annotation_text="rest of period / day",
                      annotation_position="top right")
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=20, b=0),
                      plot_bgcolor="rgba(0,0,0,0)", showlegend=False)
    fig.update_yaxes(gridcolor="rgba(0,0,0,.07)", tickformat="$~s")
    st.plotly_chart(fig, use_container_width=True, key="tk_chart")

    # --- top products during the window (local detail only) ------------------
    fl = _fact_line()
    if not fl.empty and "product" in fl.columns:
        fl["day"] = pd.to_datetime(fl["txn_ts"], errors="coerce").dt.normalize()
        win = fl[(fl.day >= s["start"]) & (fl.day <= s["eff_end"])
                 & fl.store_key.isin(keys)
                 & fl.brand.fillna("").str.lower().str.contains(pat, na=False)]
        if not win.empty:
            top = (win.groupby("product")
                      .agg(net=("net_sales", "sum"),
                           baskets=("basket_id", "nunique"))
                      .reset_index()
                      .sort_values("net", ascending=False).head(12))
            st.markdown("##### What sold during the takeover")
            st.dataframe(pd.DataFrame({
                "Product": top["product"],
                "Net $": top.net.round(0),
                "Baskets": top.baskets,
            }), use_container_width=True, hide_index=True, column_config={
                "Net $": st.column_config.NumberColumn(format="$%d"),
                "Baskets": st.column_config.NumberColumn(format="%d"),
            })

    # --- GWP / redemptions for the brand -------------------------------------
    like = "(" + " OR ".join(
        "LOWER(brand) LIKE '%" + p.replace("'", "''") + "%'"
        for p in tk["patterns"]) + ")"
    inwin = pd.DataFrame()
    has_inwin = False
    # Window-scoped only. dash_offer_performance carries no dates, so the
    # whole-period view it powered has moved to the Discounting tab, where a
    # 58-week baseline is the expected frame. A takeover page shows only
    # numbers scoped to the takeover.
    if table_exists("dash_redemption_day"):
        inwin = q(f"""
            SELECT SUM(redemptions) AS redemptions,
                   SUM(redeem_value) AS spend
            FROM dash_redemption_day
            WHERE {like} {af}
              AND day BETWEEN '{tk["start"]}'
                          AND '{s["eff_end"]:%Y-%m-%d}'
        """)
        has_inwin = (not inwin.empty
                     and pd.notna(inwin.redemptions.iloc[0])
                     and inwin.redemptions.iloc[0] > 0)
        if has_inwin:
            st.divider()
            st.markdown("##### Discount-attached sales in the window")
            st.markdown('<p class="note">Sales lines for this brand that had '
                        'a discount or promotion applied <b>during the '
                        'takeover window</b>. These are paid baskets — worth '
                        'checking against the sell-through above, since '
                        'discounting can flatter a takeover. Free GWP units '
                        'are counted separately below. Whole-period offer '
                        'analysis lives on the Discounting tab.</p>',
                        unsafe_allow_html=True)
            g = st.columns(3)
            g[0].metric("Discounted units",
                        f"{int(inwin.redemptions.iloc[0]):,}")
            g[1].metric("Net sales", f"${inwin.spend.iloc[0]:,.0f}")
            g[2].metric("Avg unit price",
                        f"${inwin.spend.iloc[0] / max(inwin.redemptions.iloc[0], 1):,.2f}")

    # --- GWP reconciliation: received vs processed ---------------------------
    if table_exists("dash_gwp_receipt"):
        # Promo stock usually lands a few days BEFORE the takeover opens.
        # Count receipts from 14 days ahead of launch, or a brand whose
        # delivery arrived early shows units out the door against zero
        # received.
        rec_from = (pd.Timestamp(tk["start"]) - pd.Timedelta(days=14)).date()
        rec = q(f"""
            SELECT product, product_sku,
                   SUM(units_received) AS received
            FROM dash_gwp_receipt
            WHERE {like} {af}
              AND day BETWEEN '{rec_from}' AND '{s["eff_end"]:%Y-%m-%d}'
            GROUP BY 1,2 ORDER BY received DESC
        """)
        if not rec.empty and rec.received.sum() > 0:
            st.divider()
            st.markdown("##### GWP reconciliation — in the door vs out of it")
            st.markdown(
                '<div class="howto"><b>How to read this.</b> The big tile is '
                '<b>GWP so far</b>: every gift unit that left the shop during '
                'the promotion, and it moves with each refresh. <b>Received</b> '
                'is the GWP stock that arrived for the window, from the '
                'inventory receipt reports — deliveries often land a few days '
                'before launch, so the two weeks ahead of the window count '
                'too. <b>On the GWP SKU</b> is units '
                'rung properly on the promo\'s own "(GWP)" item. '
                '<b>Mis-rung</b> is the discrepancy you asked about: sale '
                'lines where staff keyed the <b>SKU number</b> instead of '
                'the product — spotted because the line\'s "product" is a '
                'bare number, and identified by matching that number back '
                'to the receipt SKU. <b>Still out there</b> is stock on the '
                'shelf — or units to chase down.</div>',
                unsafe_allow_html=True)

            skus = ",".join("'" + str(x).replace("'", "''") + "'"
                            for x in rec.product_sku.unique())
            sus = q(f"""
                SELECT product AS sku, SUM(units) AS misrung,
                       COUNT(*) AS lines, SUM(net) AS net
                FROM dash_suspect_lines
                WHERE product IN ({skus}) {af}
                  AND day BETWEEN '{tk["start"]}' AND '{s["eff_end"]:%Y-%m-%d}'
                GROUP BY 1
            """) if table_exists("dash_suspect_lines") else pd.DataFrame()

            merged = rec.merge(
                sus.rename(columns={"sku": "product_sku"}),
                on="product_sku", how="left")
            for c in ("misrung", "lines", "net"):
                if c not in merged:
                    merged[c] = 0
            merged[["misrung", "lines", "net"]] = \
                merged[["misrung", "lines", "net"]].fillna(0)

            # Offer-attached sales lines for the brand in the window. This is
            # NOT a GWP unit count — it counts paid lines that carried any
            # promotion — so it is shown as context only and never used as
            # the out-the-door basis below.
            offer_lines = (float(inwin.redemptions.iloc[0])
                           if has_inwin else np.nan)

            # Properly-rung GWP: units sold on the promo's own "(GWP)" SKU,
            # published per product per day as dash_gwp_day. Preferred over
            # the brand-level loyalty count because it is per SKU and moves
            # daily while the promotion runs.
            rung = pd.DataFrame()
            if table_exists("dash_gwp_day"):
                rung = q(f"""
                    SELECT product, SUM(units) AS rung
                    FROM dash_gwp_day
                    WHERE {like} {af}
                      AND day BETWEEN '{tk["start"]}'
                                  AND '{s["eff_end"]:%Y-%m-%d}'
                    GROUP BY 1
                """)
            has_rung = not rung.empty
            if has_rung:
                merged = merged.merge(rung, on="product", how="left")
            if "rung" not in merged:
                merged["rung"] = 0.0
            merged["rung"] = merged["rung"].fillna(0)
            rung_tot = float(merged.rung.sum())

            # The headline: how many GWP units left the shop during the
            # window, by whatever route — rung on the GWP SKU (preferred) or
            # loyalty redemptions (fallback) — plus mis-rung lines.
            # Only units rung on the promo's own "(GWP)" SKU count as a gift
            # leaving the shop. The brand-level offer count was previously
            # used as a fallback here, which conflated promo-attached paid
            # sales with free goods and could put out-the-door in the
            # thousands against a 100-unit drop.
            basis = rung_tot if has_rung else 0
            out_door = merged.misrung.sum() + basis
            received_tot = rec.received.sum()

            # Attach rate: GWP issued per paid brand unit in the window —
            # the number to watch if the offer is "GWP with any purchase".
            attach = np.nan
            du = s.get("during") or {}
            _upd = du.get("units_pd")
            if has_rung and _upd is not None and pd.notna(_upd):
                paid = _upd * du.get("days", 0) - rung_tot
                if paid > 0:
                    attach = out_door / paid * 100

            # --- hero tile: GWP so far ------------------------------------
            _ee = s["eff_end"]
            pct_stock = out_door / received_tot if received_tot > 0 else 0
            sub = (f"{pct_stock:.0%} of the stock is out the door"
                   + (f" · attach rate {attach:.1f}% of paid "
                      f"{tk['name']} units" if pd.notna(attach) else "")
                   + f" · data through {_ee:%b} {_ee.day}"
                     " · updates with every refresh")
            st.markdown(
                '<div style="background:var(--tint);border:1px solid '
                'var(--rule);border-left:4px solid var(--accent);'
                'border-radius:10px;padding:1rem 1.3rem;'
                'margin:.5rem 0 1rem 0;">'
                '<div style="font-size:.76rem;text-transform:uppercase;'
                'letter-spacing:.09em;color:var(--muted);">GWP so far — '
                + tk["name"] + '</div>'
                '<div style="font-size:2.5rem;font-weight:700;'
                'color:var(--ink);line-height:1.15;">'
                f"{int(out_door):,}"
                '<span style="font-size:1.05rem;font-weight:400;'
                'color:var(--muted);"> of '
                f"{int(received_tot):,} received</span></div>"
                f'<div style="font-size:.85rem;color:var(--body);">'
                f'{sub}</div></div>', unsafe_allow_html=True)

            # Which method moved the GWP: channel split of the properly-rung
            # units (In-Store register / Non-Stop / Delivery).
            if has_col("dash_gwp_day", "channel"):
                gwp_ch = q(f"""
                    SELECT channel, SUM(units) AS units
                    FROM dash_gwp_day
                    WHERE {like} {af}
                      AND day BETWEEN '{tk["start"]}'
                                  AND '{s["eff_end"]:%Y-%m-%d}'
                    GROUP BY 1 ORDER BY units DESC
                """)
                if not gwp_ch.empty and gwp_ch.units.sum() > 0:
                    bits = " · ".join(
                        f"**{r.channel}** {int(r.units):,}"
                        for r in gwp_ch.itertuples())
                    st.markdown("GWP by method: " + bits +
                                "  \n*(properly-rung units only — mis-rung "
                                "lines are counted in the tile above)*")

            c = st.columns(5)
            c[0].metric("GWP out the door", f"{int(out_door):,}",
                        help="Every GWP unit that left during the window, "
                             "by any route.")
            c[1].metric("Received", f"{int(received_tot):,}")
            if has_rung:
                c[2].metric("On the GWP SKU", f"{int(rung_tot):,}",
                            help="Units rung on the promo's own '(GWP)' "
                                 "item — per SKU, inside the window.")
            else:
                c[2].metric("On the GWP SKU", "—",
                            help="Per-SKU GWP sales (dash_gwp_day) are not "
                                 "published yet, so only mis-rung lines are "
                                 "counted above. Out-the-door will read low "
                                 "until the next refresh.")
            c[3].metric("Mis-rung", f"{int(merged.misrung.sum()):,}",
                        help="Sale lines keyed as a bare SKU number, matched "
                             "to these GWP receipts.")
            remaining = received_tot - out_door
            c[4].metric("Still out there", f"{int(remaining):,}",
                        help="Received minus out-the-door. Includes stock "
                             "still on the shelf — check the back of house "
                             "before chasing a gap.")

            tbl = pd.DataFrame({
                "GWP item": merged["product"],
                "SKU": merged["product_sku"],
                "Received": merged.received.round(0),
                "Mis-rung": merged.misrung.round(0),
                "Mis-ring value $": merged.net.round(0),
            })
            cfg = {
                "Received": st.column_config.NumberColumn(format="%d"),
                "Mis-rung": st.column_config.NumberColumn(format="%d"),
                "Mis-ring value $": st.column_config.NumberColumn(format="$%d"),
            }
            if has_rung:
                tbl.insert(3, "On GWP SKU", merged.rung.round(0))
                tbl["% of stock"] = (
                    (merged.rung + merged.misrung)
                    / merged.received.replace(0, np.nan) * 100).round(0)
                cfg["On GWP SKU"] = st.column_config.NumberColumn(format="%d")
                cfg["% of stock"] = st.column_config.NumberColumn(format="%d%%")
            st.dataframe(tbl, use_container_width=True, hide_index=True,
                         column_config=cfg)
            if merged.misrung.sum() > 0:
                st.markdown(
                    '<div class="alert a-warn"><b>Mis-rung GWP found.</b> '
                    'The lines above were keyed by SKU number rather than '
                    'processed as gift-with-purchase. They count as ordinary '
                    'sales in the till, which understates the GWP programme '
                    'and muddies the brand\'s real margin.</div>',
                    unsafe_allow_html=True)


with t_promo:
    render_promo_lab()

with t_takeover:
    render_takeovers()


# ---------------------------------------------------------------- loyalty
with t_loyalty:
    render_loyalty(q=q, keys=keys, keep=keep, stores=STORES,
                   heading=heading, table_exists=table_exists,
                   partial_week=PARTIAL_WEEK)


# ------------------------------------------------ retention
with t_retention:
    render_retention(q=q, keys=keys, stores=STORES,
                     heading=heading, table_exists=table_exists)


# --------------------------------------------------- events
with t_events:
    render_events(q=q, keys=keys, stores=STORES,
                  heading=heading, table_exists=table_exists)


# ------------------------------------------- audiences x events
with t_audiences:
    render_audiences(q=q, keys=keys, stores=STORES,
                     heading=heading, table_exists=table_exists)


# ------------------------------------------------------------ discounting
with t_discount:
    render_discounting(q=q, keys=keys, keep=keep, stores=STORES,
                       heading=heading, table_exists=table_exists,
                       accent=ACCENT, series=SERIES)


# -------------------------------------------------------- brand efficiency
with t_bei:
    render_bei(q=q, keys=keys, stores=STORES,
               heading=heading, table_exists=table_exists,
               accent=ACCENT, series=SERIES)

