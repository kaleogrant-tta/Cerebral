"""
events_onsite.py -- did in-store events move that store's sales?

    python events_onsite.py --db ..\\tta.duckdb
    python events_onsite.py --db ..\\tta.duckdb --metric new_customers --csv

Read-only.

WHY THIS IS A BETTER TEST THAN THE OFF-SITE ONE
-----------------------------------------------
An in-store event happens at ONE store while three others carry on as normal.
That gives a control group the off-site analysis never had.

    within-store lift = event store today  vs  its own matched weekday baseline
    control drift     = other stores today vs  their matched weekday baselines
    DiD               = within-store lift - control drift

Anything affecting the whole city on that day -- weather, a holiday, a
transit strike, a citywide promotion -- hits all four stores and cancels out
in the subtraction. What survives is specific to the store running the event.

Multi-store events (23 of them are tagged to three or four stores at once)
have no clean control. They are measured on the within-store lift alone and
reported separately, clearly marked as the weaker design.

WHAT IT STILL CANNOT DO
-----------------------
Rule out that the event was scheduled BECAUSE a good day was expected --
a product drop, a holiday weekend, a partner promotion running in parallel.
DiD removes shared shocks, not deliberate scheduling.
"""

import argparse
import glob
import os
import sys

import duckdb
import numpy as np
import pandas as pd

EVENTS_GLOB = ["Events.xlsx", "*vents*.xlsx"]
OFFSITE = "Not In Store"

# Events sheet tag -> store_key in Cerebral
STORE_MAP = {"DTBK": 1, "FIFTH": 2, "5TH": 2, "5TH AVENUE": 2,
             "SOHO": 3, "USQ": 4, "UNION SQUARE": 4}
STORE_NAME = {1: "DTBK", 2: "5th Avenue", 3: "Soho", 4: "Union Square"}

# A +/-2 day buffer around ALL events removed 71% of candidate control days
# -- events touch about a third of the calendar -- leaving a median of two
# controls per event and a baseline too noisy to trust. Controls are now
# blocked only by events at the SAME store (plus chain-wide ones), with a
# tighter buffer and a wider window.
CONTROL_WINDOW_DAYS = 42
BUFFER_DAYS = 1
MIN_CONTROLS = 3
OFFSETS = (-1, 0, 1)
BOOT = 4000
SEED = 11


def find(globs):
    roots = [".", os.path.expanduser("~/Downloads"), os.path.expanduser("~")]
    seen, out = set(), []
    for r in roots:
        for g in globs:
            for h in glob.glob(os.path.join(r, "**", g), recursive=True):
                real = os.path.realpath(h)
                if real not in seen:
                    seen.add(real)
                    out.append(h)
    return sorted(out)


def load_events(path):
    d = pd.read_excel(path)
    d.columns = [c.strip() for c in d.columns]
    d["ts"] = pd.to_datetime(d["Event Start Date"],
                             format="%B %d, %Y %I:%M%p", errors="coerce")
    d = d.dropna(subset=["ts"])
    d = d[~d["Event"].astype(str).str.contains("RESCHEDUL", case=False,
                                               na=False)]
    d["date"] = d.ts.dt.normalize()
    d["Event Type"] = d["Event Type"].fillna("Untyped")
    d["loc"] = d["Store Location"].fillna("").astype(str)

    on = d[(d["loc"].str.strip() != "") &
           (d["loc"].str.strip() != OFFSITE)].copy()
    on["tags"] = on["loc"].str.upper().str.split(",")
    on = on.explode("tags")
    on["tags"] = on["tags"].str.strip()
    on = on[on["tags"] != OFFSITE.upper()]
    on["store_key"] = on["tags"].map(STORE_MAP)
    unmapped = sorted(set(on[on.store_key.isna()]["tags"].dropna()))
    on = on.dropna(subset=["store_key"])
    on["store_key"] = on["store_key"].astype(int)

    # how many distinct stores each event touches -> is there a control group?
    per_event = on.groupby(["Event", "date"])["store_key"].nunique()
    on = on.merge(per_event.rename("n_stores"),
                  left_on=["Event", "date"], right_index=True)
    on["has_control"] = on["n_stores"] < len(STORE_NAME)
    return on, unmapped, d


def store_day(con, metric, lo, hi):
    if metric == "new_customers":
        q = """
            WITH firsts AS (
                SELECT customer_key, MIN(txn_ts) AS first_ts
                FROM src.fact_basket
                WHERE NOT is_return AND customer_key NOT LIKE 'H%'
                GROUP BY 1
            )
            SELECT b.store_key, b.txn_ts::DATE AS date,
                   COUNT(*) AS value
            FROM src.fact_basket b JOIN firsts f
              ON f.customer_key = b.customer_key AND f.first_ts = b.txn_ts
            WHERE b.txn_ts::DATE BETWEEN ? AND ?
            GROUP BY 1, 2
        """
    else:
        col = {"net": "SUM(basket_net)", "baskets": "COUNT(*)",
               "customers": "COUNT(DISTINCT customer_key)"}[metric]
        q = f"""
            SELECT store_key, txn_ts::DATE AS date, {col} AS value
            FROM src.fact_basket
            WHERE NOT is_return AND txn_ts::DATE BETWEEN ? AND ?
            GROUP BY 1, 2
        """
    df = con.execute(q, [lo, hi]).df()
    df["date"] = pd.to_datetime(df["date"])
    return df


def lift_for(series, day, blocked, min_controls=None):
    """One store's value on `day` against its own matched weekday baseline."""
    if day not in series.index:
        return np.nan, 0
    lo = day - pd.Timedelta(days=CONTROL_WINDOW_DAYS)
    hi = day + pd.Timedelta(days=CONTROL_WINDOW_DAYS)
    ctrl = series[(series.index >= lo) & (series.index <= hi)
                  & (series.index.dayofweek == day.dayofweek)]
    ctrl = ctrl[[i not in blocked for i in ctrl.index]]
    if len(ctrl) < (min_controls or MIN_CONTROLS):
        return np.nan, len(ctrl)
    base = ctrl.mean()
    if not base:
        return np.nan, len(ctrl)
    return series.loc[day] / base - 1, len(ctrl)


def analyse(sd, events, offsite_dates):
    """Per-store blocked sets: a Soho event should not disqualify a control
    day for DTBK. Chain-wide off-site events block every store."""
    def expand(dates):
        out = set()
        for d in dates:
            for k in range(-BUFFER_DAYS, BUFFER_DAYS + 1):
                out.add(d + pd.Timedelta(days=k))
        return out

    chain_blocked = expand(offsite_dates)
    blocked_by_store = {}
    for sk, g in events.groupby("store_key"):
        blocked_by_store[sk] = expand(set(g["date"])) | chain_blocked

    by_store = {k: g.set_index("date")["value"].sort_index()
                for k, g in sd.groupby("store_key")}
    rows = []
    for _, ev in events.iterrows():
        sk = ev["store_key"]
        if sk not in by_store:
            continue
        others = [k for k in by_store if k != sk]
        for off in OFFSETS:
            day = ev["date"] + pd.Timedelta(days=off)
            own, n_ctrl = lift_for(
                by_store[sk], day, blocked_by_store.get(sk, chain_blocked))
            if not np.isfinite(own):
                continue
            drift = [lift_for(by_store[k], day,
                              blocked_by_store.get(k, chain_blocked))[0]
                     for k in others]
            drift = [v for v in drift if np.isfinite(v)]
            ctrl_drift = float(np.mean(drift)) if drift else np.nan
            rows.append({
                "event": ev["Event"], "date": ev["date"], "offset": off,
                "store": STORE_NAME.get(sk, sk), "store_key": sk,
                "type": ev["Event Type"], "brand": ev.get("Brand Partners"),
                "n_stores": ev["n_stores"], "has_control": ev["has_control"],
                "own_lift": own, "control_drift": ctrl_drift,
                "did": own - ctrl_drift if np.isfinite(ctrl_drift) else np.nan,
                "n_control_days": n_ctrl,
            })
    return pd.DataFrame(rows)


def bootstrap_ci(x, n=BOOT, seed=SEED):
    x = np.asarray([v for v in x if np.isfinite(v)])
    if len(x) < 3:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    return tuple(np.percentile(
        rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1),
        [2.5, 97.5]))


def summarise(df, col, label, by=None, min_n=5):
    print()
    print("  %-30s %5s %9s %9s %24s"
          % (label, "n", "mean", "median", "95% CI"))
    print("  " + "-" * 84)
    groups = [(label, df)] if by is None else list(df.groupby(by))
    for name, g in groups:
        x = g[col].dropna().values
        if len(x) < min_n:
            if len(x):
                print("  %-30s %5d %9s   (too few to summarise)"
                      % (str(name)[:30], len(x), "--"))
            continue
        lo, hi = bootstrap_ci(x)
        ci = ("[%+.1f%%, %+.1f%%]" % (lo * 100, hi * 100)) \
            if np.isfinite(lo) else "--"
        # A bootstrap interval on fewer than ~10 observations excludes zero
        # far too readily. Validation on data with no effect planted produced
        # a "significant" -15% at n=5. Say so rather than let it read as real.
        mark = ""
        if np.isfinite(lo) and (lo > 0 or hi < 0):
            mark = ("  <-- CI excludes 0" if len(x) >= 10
                    else "  <-- CI excludes 0, but n=%d is too small to "
                         "trust" % len(x))
        elif len(x) < 10:
            mark = "  (n small)"
        print("  %-30s %5d %8.1f%% %8.1f%% %24s%s"
              % (str(name)[:30], len(x), np.mean(x) * 100,
                 np.median(x) * 100, ci, mark))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="../tta.duckdb")
    ap.add_argument("--events", default=None)
    ap.add_argument("--metric", default="net",
                    choices=["net", "baskets", "customers", "new_customers"])
    ap.add_argument("--csv", action="store_true")
    a = ap.parse_args()

    ev_path = a.events or (find(EVENTS_GLOB) or [None])[0]
    if not ev_path or not os.path.exists(a.db):
        print("Need Events.xlsx and a database.")
        return 1

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS src (READ_ONLY)"
                % os.path.abspath(a.db).replace("'", "''"))
    lo, hi = con.execute(
        "SELECT MIN(txn_ts)::DATE, MAX(txn_ts)::DATE FROM src.fact_basket"
    ).fetchone()

    on, unmapped, alld = load_events(ev_path)
    on = on[(on.date >= pd.Timestamp(lo)) & (on.date <= pd.Timestamp(hi))]
    alld = alld[(alld.date >= pd.Timestamp(lo)) &
                (alld.date <= pd.Timestamp(hi))]

    print("events file : %s" % os.path.basename(ev_path))
    print("coverage    : %s -> %s" % (lo, hi))
    print("store-event pairs in coverage: %d across %d events"
          % (len(on), on["Event"].nunique()))
    if unmapped:
        print("  !! unmapped store tags (ignored): %s" % ", ".join(unmapped))
    print()
    print("  by store:")
    for k, n in on.store_key.value_counts().sort_index().items():
        print("    %-14s %3d" % (STORE_NAME.get(k, k), n))
    print("  single-store events (have a control group): %d"
          % int(on.has_control.sum()))
    print("  chain-wide events   (no control group):     %d"
          % int((~on.has_control).sum()))

    sd = store_day(con, a.metric, lo, hi)
    offsite = set(alld[alld["loc"].str.strip() == OFFSITE]["date"])
    res = analyse(sd, on, offsite)
    if res.empty:
        print("\n  Nothing measurable -- too few clean control days.")
        return 1

    print()
    print("=" * 88)
    print("IN-STORE EVENT LIFT  --  metric: %s" % a.metric)
    print("=" * 88)
    if "n_control_days" in res:
        print("  control days per event: median %.0f (min %d)"
              % (res.n_control_days.median(), res.n_control_days.min()))
    print("  DiD = the event store's lift minus the average drift of the")
    print("  other stores that day. A CI clear of zero is the real claim;")
    print("  with n in the tens the mean alone is easily moved by one day.")

    d0 = res[(res.offset == 0) & res.has_control]
    print()
    print("  --- SINGLE-STORE EVENTS (difference-in-differences) ---")
    summarise(d0, "did", "all single-store events")
    summarise(d0, "did", "by store", by="store")
    summarise(d0, "did", "by event type", by="type")

    print()
    print("  --- for comparison, the raw within-store lift ---")
    summarise(d0, "own_lift", "same events, no control subtracted")

    print()
    print("  --- BY DAY OFFSET (single-store events) ---")
    summarise(res[res.has_control], "did", "offset", by="offset")

    multi = res[(res.offset == 0) & (~res.has_control)]
    if not multi.empty:
        print()
        print("  --- CHAIN-WIDE EVENTS (no control group; weaker) ---")
        summarise(multi, "own_lift", "within-store lift only")

    print()
    print("=" * 88)
    print("BIGGEST SINGLE-STORE MOVES ON THE DAY")
    print("=" * 88)
    dd = d0.dropna(subset=["did"]).sort_values("did", ascending=False)
    for _, r in pd.concat([dd.head(8), dd.tail(8)]).iterrows():
        print("  %s %-6s %+7.1f%% DiD  (own %+6.1f%%, others %+6.1f%%)  %s"
              % (r.date.date(), r.store[:6], r.did * 100,
                 r.own_lift * 100, r.control_drift * 100,
                 str(r.event)[:38]))

    if not on.empty and on["Brand Partners"].notna().any():
        bp = d0[d0.brand.notna()]
        if len(bp) >= 6:
            print()
            print("=" * 88)
            print("BY BRAND PARTNER  (single-store, >=3 events only)")
            print("=" * 88)
            summarise(bp, "did", "brand partner", by="brand", min_n=3)

    print()
    print("=" * 88)
    print("CAVEATS")
    print("=" * 88)
    print("  DiD removes anything hitting all four stores that day -- weather,")
    print("  holidays, citywide news. It does NOT remove the possibility that")
    print("  an event was scheduled onto a day already expected to be good,")
    print("  or that a product drop ran alongside it.")
    print()
    print("  Groups under about ten events cannot support a reliable")
    print("  interval. On validation data with nothing planted, a five-event")
    print("  store produced a -15%% interval clear of zero. Treat small-n")
    print("  rows as descriptive only.")
    print()
    print("  Chain-wide events have no control store, so their numbers are")
    print("  the weaker within-store measure and are not comparable with the")
    print("  DiD figures above.")

    if a.csv:
        res.to_csv("events_onsite.csv", index=False)
        print()
        print("wrote events_onsite.csv (%d rows)" % len(res))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
