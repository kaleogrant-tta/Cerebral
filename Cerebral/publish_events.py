"""
publish_events.py -- event lift tables for the published file.

Called from publish.py's build(), after ATTACH and before DETACH:

    from publish_events import build_events
    ...
    build_events(con)

Requires src.dim_event (written by events_ingest.py). No-ops without it.

The statistics are computed here, not in the dashboard, so the tab is a
renderer and every number is reproducible from the published file.

METHOD -- two designs, deliberately kept apart
----------------------------------------------
ON-SITE, single store: difference-in-differences. The event store's lift
against its own matched-weekday baseline, minus the average drift of the
other stores that day. Anything citywide cancels.

OFF-SITE and CHAIN-WIDE: within-store lift only, because there is no control
store. Weaker, and labelled as such in the tab. On real data the off-site
series shows a positive lift at EVERY offset from -2 to +2, which is the
signature of events being scheduled into already-busy weeks rather than
causing anything -- an event cannot lift the day before it.

Controls are blocked only by events at the same store plus chain-wide
off-site events. Blocking around every event removed 71% of candidate days
and left a median of two controls per event.

PUBLISH.PY CHANGE REQUIRED
--------------------------
Add "event_name", "event_type", "series", "brand_partners", "store_name",
"scope", "group_kind", "group_value" and "metric" to ALLOWED_TEXT.
"""

import numpy as np
import pandas as pd

CONTROL_WINDOW_DAYS = 42
BUFFER_DAYS = 1
MIN_CONTROLS = 3
OFFSETS = (-2, -1, 0, 1, 2)
METRICS = ("net", "baskets", "new_customers")
BOOT = 3000
SEED = 11
MIN_GROUP = 5          # below this a group is not summarised
TRUST_N = 10           # below this an interval is flagged as unreliable

STORE_NAME = {0: "Chain", 1: "DTBK", 2: "5th Avenue", 3: "Soho",
              4: "Union Square"}


def _src_has(con, table):
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name = 'src' AND table_name = ?", [table]
    ).fetchone()[0] > 0


def _store_day(con, metric):
    if metric == "new_customers":
        q = """
            WITH firsts AS (
                SELECT customer_key, MIN(txn_ts) AS first_ts
                FROM src.fact_basket
                WHERE NOT is_return AND customer_key NOT LIKE 'H%'
                GROUP BY 1
            )
            SELECT b.store_key, b.txn_ts::DATE AS date, COUNT(*) AS value
            FROM src.fact_basket b JOIN firsts f
              ON f.customer_key = b.customer_key AND f.first_ts = b.txn_ts
            GROUP BY 1, 2
        """
    else:
        col = {"net": "SUM(basket_net)", "baskets": "COUNT(*)"}[metric]
        q = f"""SELECT store_key, txn_ts::DATE AS date, {col} AS value
                FROM src.fact_basket WHERE NOT is_return GROUP BY 1, 2"""
    df = con.execute(q).df()
    df["date"] = pd.to_datetime(df["date"])
    return df


def _chain_day(sd):
    return (sd.groupby("date", as_index=False)["value"].sum()
            .assign(store_key=0))


def _expand(dates):
    out = set()
    for d in dates:
        for k in range(-BUFFER_DAYS, BUFFER_DAYS + 1):
            out.add(d + pd.Timedelta(days=k))
    return out


def _lift(series, day, blocked):
    if day not in series.index:
        return np.nan, 0
    lo = day - pd.Timedelta(days=CONTROL_WINDOW_DAYS)
    hi = day + pd.Timedelta(days=CONTROL_WINDOW_DAYS)
    c = series[(series.index >= lo) & (series.index <= hi)
               & (series.index.dayofweek == day.dayofweek)]
    c = c[[i not in blocked for i in c.index]]
    if len(c) < MIN_CONTROLS:
        return np.nan, len(c)
    base = c.mean()
    if not base:
        return np.nan, len(c)
    return series.loc[day] / base - 1, len(c)


def _ci(x, n=BOOT, seed=SEED, stat="mean"):
    """Bootstrap interval for the mean or the median of x.

    The tab quotes the median, so a mean interval next to it can exclude
    the very number it is printed beside. SEED is fixed, so both intervals
    reproduce run to run.
    """
    x = np.asarray([v for v in x if np.isfinite(v)])
    if len(x) < 3:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    draws = rng.choice(x, size=(n, len(x)), replace=True)
    s = draws.mean(axis=1) if stat == "mean" else np.median(draws, axis=1)
    return tuple(np.percentile(s, [2.5, 97.5]))


def _detail_for_metric(ev, sd, chain, metric):
    by_store = {k: g.set_index("date")["value"].sort_index()
                for k, g in sd.groupby("store_key")}
    by_store[0] = chain.set_index("date")["value"].sort_index()

    offsite_dates = set(ev[ev.is_offsite]["event_date"])
    chain_blocked = _expand(offsite_dates)
    blocked = {sk: _expand(set(g["event_date"])) | chain_blocked
               for sk, g in ev[~ev.is_offsite].groupby("store_key")}

    rows = []
    for _, e in ev.iterrows():
        sk = 0 if e.is_offsite else int(e.store_key)
        if sk not in by_store:
            continue
        blk = chain_blocked if sk == 0 else blocked.get(sk, chain_blocked)
        others = [k for k in by_store if k not in (sk, 0)]
        for off in OFFSETS:
            day = e["event_date"] + pd.Timedelta(days=off)
            own, nc = _lift(by_store[sk], day, blk)
            if not np.isfinite(own):
                continue
            did = np.nan
            drift = np.nan
            if e.has_control:
                vals = [_lift(by_store[k], day,
                              blocked.get(k, chain_blocked))[0]
                        for k in others]
                vals = [v for v in vals if np.isfinite(v)]
                if vals:
                    drift = float(np.mean(vals))
                    did = own - drift
            rows.append({
                "metric": metric, "event_id": e.event_id,
                "event_name": e.event_name, "event_date": e.event_date,
                "event_type": e.event_type, "series": e.series,
                "brand_partners": e.brand_partners,
                "store_name": STORE_NAME.get(sk, str(sk)),
                "store_key": sk, "is_offsite": bool(e.is_offsite),
                "has_control": bool(e.has_control), "offset": off,
                "own_lift": own, "control_drift": drift, "did": did,
                "n_controls": nc,
            })
    return pd.DataFrame(rows)


def _scope(r):
    if r.is_offsite:
        return "Off-site"
    return "On-site (single store)" if r.has_control else "On-site (chain-wide)"


def _summaries(detail):
    out = []
    for (metric, scope), g in detail.groupby(["metric", "scope"]):
        col = "did" if scope == "On-site (single store)" else "own_lift"
        d0 = g[g.offset == 0]
        cuts = [("all", "All events", d0),
                ("offset", None, g),
                ("type", None, d0),
                ("store", None, d0),
                ("series", None, d0)]
        for kind, fixed, src in cuts:
            if kind == "all":
                groups = [(fixed, src)]
            elif kind == "offset":
                groups = [(str(k), v) for k, v in src.groupby("offset")]
            else:
                key = {"type": "event_type", "store": "store_name",
                       "series": "series"}[kind]
                groups = list(src.groupby(key))
            for name, sub in groups:
                x = sub[col].dropna().values
                if len(x) < MIN_GROUP:
                    continue
                lo, hi = _ci(x)
                mlo, mhi = _ci(x, stat="median")
                out.append({
                    "metric": metric, "scope": scope, "group_kind": kind,
                    "group_value": str(name), "measure": col,
                    "n": len(x), "mean": float(np.mean(x)),
                    "median": float(np.median(x)),
                    "ci_lo": lo, "ci_hi": hi,
                    "ci_lo_med": mlo, "ci_hi_med": mhi,
                    "reliable": bool(len(x) >= TRUST_N),
                    "excludes_zero": bool(np.isfinite(lo) and
                                          (lo > 0 or hi < 0)),
                    "excludes_zero_med": bool(np.isfinite(mlo) and
                                              (mlo > 0 or mhi < 0)),
                })
    return pd.DataFrame(out)


def build_events(con) -> dict:
    if not _src_has(con, "dim_event"):
        print("  [events] src.dim_event missing - skipping. "
              "Run events_ingest.py to populate it.")
        return {}

    lo, hi = con.execute(
        "SELECT MIN(txn_ts)::DATE, MAX(txn_ts)::DATE FROM src.fact_basket"
    ).fetchone()
    ev = con.execute("""
        SELECT * FROM src.dim_event WHERE event_date BETWEEN ? AND ?
    """, [lo, hi]).df()
    if ev.empty:
        print("  [events] no events inside the transaction window - skipping.")
        return {}
    ev["event_date"] = pd.to_datetime(ev["event_date"])

    frames = []
    for m in METRICS:
        sd = _store_day(con, m)
        frames.append(_detail_for_metric(ev, sd, _chain_day(sd), m))
    detail = pd.concat(frames, ignore_index=True)
    if detail.empty:
        print("  [events] nothing measurable - skipping.")
        return {}
    detail["scope"] = detail.apply(_scope, axis=1)

    summary = _summaries(detail)

    meta = pd.DataFrame([{
        "events": int(ev.event_name.nunique()),
        "event_rows": int(len(ev)),
        "single_store": int(ev.has_control.sum()),
        "offsite": int(ev.is_offsite.sum()),
        "cov_start": lo, "cov_end": hi,
        "control_window_days": CONTROL_WINDOW_DAYS,
        "min_controls": MIN_CONTROLS,
        "trust_n": TRUST_N,
        "built_at": pd.Timestamp.now(),
    }])

    out = {}
    for name, df in (("dash_events_detail", detail),
                     ("dash_events_summary", summary),
                     ("dash_events_meta", meta)):
        con.execute(f"DROP TABLE IF EXISTS {name}")
        con.register("_ev_tmp", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _ev_tmp")
        con.unregister("_ev_tmp")
        out[name] = len(df)
        print(f"  [events] {name:<32} {len(df):,} rows")
    return out
