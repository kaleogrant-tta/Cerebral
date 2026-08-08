"""
events_lift.py -- did off-site events move sales?

    python events_lift.py --db ..\\tta.duckdb
    python events_lift.py --db ..\\tta.duckdb --csv

Read-only.

METHOD
------
For each off-site ("Not In Store") event, compare chain-wide sales on the
event day against a matched baseline: the SAME WEEKDAY, within +/- 28 days,
excluding any day within 2 days of another event.

Local weekday-matched controls matter here. Events cluster in Sept-Oct 2025
(18 of 47) while chain revenue fell roughly 20% across the window, so a naive
comparison of event days against all other days would report a lift that is
purely the time trend. Matching on weekday inside a four-week window removes
both the weekly cycle and the trend.

Event blocks (All Things Go, Sept 26-28; NYFF, Sept 29 - Oct 2) are
consecutive-day runs. Each day is reported, but the summary also groups them
so seven correlated days do not read as seven independent results.

Significance is a two-sided sign test plus a bootstrap interval on the mean
lift -- both distribution-free, which suits n in the tens.

WHAT IT CANNOT DO
-----------------
Attribute causation. An off-site event competes with other things happening
in New York that day. A matched control removes the weekday and the trend,
not the weather, a holiday, or a competitor promotion.
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
CONTROL_WINDOW_DAYS = 28
BUFFER_DAYS = 2            # keep controls clear of any other event
OFFSETS = (-2, -1, 0, 1, 2)
BOOT = 4000
SEED = 11


def find(globs):
    roots = [".", os.path.expanduser("~/Downloads"), os.path.expanduser("~")]
    seen, out = set(), []
    for r in roots:
        for g in globs:
            for h in glob.glob(os.path.join(r, "**", g), recursive=True):
                real = os.path.realpath(h)
                if real in seen:
                    continue
                seen.add(real)
                out.append(h)
    return sorted(out)


def load_events(path):
    d = pd.read_excel(path)
    d.columns = [c.strip() for c in d.columns]
    d["ts"] = pd.to_datetime(d["Event Start Date"],
                             format="%B %d, %Y %I:%M%p", errors="coerce")
    d = d.dropna(subset=["ts"])
    d["date"] = d.ts.dt.normalize()
    d["loc"] = d["Store Location"].fillna("")
    d["Event Type"] = d["Event Type"].fillna("Untyped")

    # A rescheduled entry did not happen on the date shown.
    resched = d["Event"].astype(str).str.contains("RESCHEDUL", case=False,
                                                  na=False)
    d = d[~resched]

    off = d[d["loc"].str.strip() == OFFSITE].copy()
    off["series"] = np.where(
        off["Event"].astype(str).str.contains("TRAY TABLES UP|Tray Tables Up"
                                              "|^TTU", case=False, regex=True,
                                              na=False),
        "Tray Tables Up", "One-off")
    return d, off


def daily_sales(con, lo, hi):
    return con.execute("""
        SELECT b.txn_ts::DATE AS date,
               SUM(b.basket_net)              AS net,
               COUNT(*)                       AS baskets,
               COUNT(DISTINCT b.customer_key) AS customers
        FROM src.fact_basket b
        WHERE NOT b.is_return AND b.txn_ts::DATE BETWEEN ? AND ?
        GROUP BY 1 ORDER BY 1
    """, [lo, hi]).df()


def daily_new(con, lo, hi):
    """First-ever basket date per customer, resolved identities only.

    Name-hash keys collapse several people, so a 'new customer' there may be
    an existing one under a different spelling."""
    return con.execute("""
        WITH firsts AS (
            SELECT customer_key, MIN(txn_ts)::DATE AS d
            FROM src.fact_basket
            WHERE NOT is_return AND customer_key NOT LIKE 'H%'
            GROUP BY 1
        )
        SELECT d AS date, COUNT(*) AS new_customers
        FROM firsts WHERE d BETWEEN ? AND ? GROUP BY 1 ORDER BY 1
    """, [lo, hi]).df()


def bootstrap_ci(x, n=BOOT, seed=SEED):
    x = np.asarray([v for v in x if np.isfinite(v)])
    if len(x) < 3:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return tuple(np.percentile(means, [2.5, 97.5]))


def permutation_p(sales, events, metric, observed, n_perm=1000, seed=SEED):
    """Reassign each event to a random day of the SAME WEEKDAY, recompute the
    mean lift, and ask how often chance beats what we saw.

    A sign test was used here first and produced a false positive on
    synthetic data with no effect planted (p=0.003) -- because clustered
    event dates make the days non-independent, which the sign test assumes
    away. Permuting whole event sets preserves that clustering under the
    null, so the resulting p-value is honest.
    """
    rng = np.random.default_rng(seed)
    s_idx = pd.DatetimeIndex(sales["date"])
    real = set(events["date"])
    by_dow = {}
    for d in s_idx:
        if d not in real:
            by_dow.setdefault(d.dayofweek, []).append(d)

    hits, done = 0, 0
    for _ in range(n_perm):
        fake = events.copy()
        newd = []
        for d in fake["date"]:
            pool = by_dow.get(d.dayofweek, [])
            newd.append(rng.choice(pool) if len(pool) else d)
        fake["date"] = pd.to_datetime(newd)
        r = analyse(sales, fake, metric, offsets=(0,))
        if r.empty:
            continue
        done += 1
        if abs(r["lift"].mean()) >= abs(observed):
            hits += 1
    return (hits + 1) / (done + 1) if done else np.nan


def analyse(sales, events, metric, offsets=OFFSETS):
    s = sales.set_index("date")[metric]
    all_event_days = set(events["date"])
    blocked = set()
    for d in all_event_days:
        for k in range(-BUFFER_DAYS, BUFFER_DAYS + 1):
            blocked.add(d + pd.Timedelta(days=k))

    rows = []
    for _, ev in events.iterrows():
        for off in offsets:
            day = ev["date"] + pd.Timedelta(days=off)
            if day not in s.index:
                continue
            lo = day - pd.Timedelta(days=CONTROL_WINDOW_DAYS)
            hi = day + pd.Timedelta(days=CONTROL_WINDOW_DAYS)
            ctrl = s[(s.index >= lo) & (s.index <= hi)
                     & (s.index.dayofweek == day.dayofweek)]
            ctrl = ctrl[[i not in blocked for i in ctrl.index]]
            if len(ctrl) < 2:
                continue
            base = ctrl.mean()
            if not base:
                continue
            rows.append({
                "event": ev["Event"], "date": ev["date"], "offset": off,
                "type": ev["Event Type"], "series": ev["series"],
                "brand": ev.get("Brand Partners"),
                "actual": s.loc[day], "baseline": base,
                "controls": len(ctrl),
                "lift": s.loc[day] / base - 1,
            })
    return pd.DataFrame(rows)


def summarise(df, label, by=None, perm=None):
    """perm: (sales, events, metric, n) to run a permutation test, else None."""
    print()
    print("  %-34s %5s %9s %9s %22s %8s"
          % (label, "n", "mean", "median", "95% CI", "perm p"))
    print("  " + "-" * 94)
    groups = [(label, df)] if by is None else list(df.groupby(by))
    for name, g in groups:
        x = g["lift"].values
        if len(x) < 2:
            continue
        lo, hi = bootstrap_ci(x)
        ci = ("[%+.1f%%, %+.1f%%]" % (lo * 100, hi * 100)) \
            if np.isfinite(lo) else "--"
        pstr, star = "--", ""
        if perm is not None:
            sales, allev, metric, n_perm = perm
            sub = allev[allev["date"].isin(set(g["date"]))]
            if len(sub) >= 3:
                p = permutation_p(sales, sub, metric, np.mean(x), n_perm)
                if np.isfinite(p):
                    pstr = "%.3f" % p
                    star = "***" if p < 0.01 else "**" if p < 0.05 else \
                           "*" if p < 0.10 else ""
        # An interval clear of zero is the stronger claim; say so plainly.
        if not star and np.isfinite(lo) and (lo > 0 or hi < 0):
            star = " (CI excludes 0)"
        print("  %-34s %5d %8.1f%% %8.1f%% %22s %8s%s"
              % (str(name)[:34], len(x), np.mean(x) * 100,
                 np.median(x) * 100, ci, pstr, star))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="../tta.duckdb")
    ap.add_argument("--events", default=None)
    ap.add_argument("--metric", default="net",
                    choices=["net", "baskets", "customers", "new_customers"])
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--perms", type=int, default=400,
                    help="permutation iterations (higher = slower, finer p)")
    a = ap.parse_args()

    ev_path = a.events or (find(EVENTS_GLOB) or [None])[0]
    if not ev_path or not os.path.exists(a.db):
        print("Need Events.xlsx and a database.")
        return 1

    print("events file : %s" % os.path.basename(ev_path))
    print("database    : %s" % a.db)

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS src (READ_ONLY)"
                % os.path.abspath(a.db).replace("'", "''"))

    lo, hi = con.execute(
        "SELECT MIN(txn_ts)::DATE, MAX(txn_ts)::DATE FROM src.fact_basket"
    ).fetchone()
    print("coverage    : %s -> %s" % (lo, hi))

    allev, off = load_events(ev_path)
    off = off[(off.date >= pd.Timestamp(lo)) & (off.date <= pd.Timestamp(hi))]
    allev = allev[(allev.date >= pd.Timestamp(lo)) &
                  (allev.date <= pd.Timestamp(hi))]
    print("off-site events in coverage: %d  (of %d events total)"
          % (len(off), len(allev)))

    sales = daily_sales(con, lo, hi)
    if a.metric == "new_customers":
        sales = sales.merge(daily_new(con, lo, hi), on="date", how="left")
        sales["new_customers"] = sales["new_customers"].fillna(0)
    sales["date"] = pd.to_datetime(sales["date"])

    print()
    print("=" * 94)
    print("SALES LIFT AROUND OFF-SITE EVENTS  --  metric: %s" % a.metric)
    print("=" * 94)
    print("  baseline = same weekday within +/-%d days, excluding days within"
          % CONTROL_WINDOW_DAYS)
    print("  %d days of any event.  p from %d permutations of the event dates"
          % (BUFFER_DAYS, a.perms))

    res = analyse(sales, off, a.metric)
    if res.empty:
        print("\n  No event had enough clean control days. Widen the window.")
        return 1

    perm = (sales, off, a.metric, a.perms)
    day0 = res[res.offset == 0]
    summarise(res, "by day offset", by="offset")
    summarise(day0, "event day, by type", by="type", perm=perm)
    summarise(day0, "event day, by series", by="series", perm=perm)
    summarise(day0, "ALL EVENT DAYS", perm=perm)

    print()
    print("=" * 94)
    print("BIGGEST MOVES ON THE DAY  (n controls in brackets)")
    print("=" * 94)
    d = day0.sort_values("lift", ascending=False)
    for _, r in pd.concat([d.head(8), d.tail(8)]).iterrows():
        print("  %s  %+7.1f%%  %-52s [%d]"
              % (r.date.date(), r.lift * 100, str(r.event)[:52], r.controls))

    print()
    print("=" * 94)
    print("READ THIS BEFORE ACTING ON THE ABOVE")
    print("=" * 94)
    print("  A sign test was tried first and produced p=0.003 on synthetic")
    print("  data with NO effect planted, because clustered event dates are")
    print("  not independent. The permutation test above shuffles whole event")
    print("  sets within weekday, so it keeps that clustering under the null.")
    print()
    print("  Controls remove the weekday cycle and the local trend. They do")
    print("  not remove weather, holidays, or anything else happening in the")
    print("  city that day. With n in the tens, a single unusual day moves")
    print("  the mean substantially -- prefer the median and the interval.")
    print("  An interval spanning zero means no detectable effect, which for")
    print("  a brand-awareness event is an unsurprising result rather than a")
    print("  failure: same-day revenue is not what they are for.")

    if a.csv:
        res.to_csv("events_lift.csv", index=False)
        print()
        print("wrote events_lift.csv (%d rows)" % len(res))

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
