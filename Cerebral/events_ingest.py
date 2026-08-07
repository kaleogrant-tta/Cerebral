"""
events_ingest.py -- write the events calendar into the Cerebral DB.

    python events_ingest.py --db ..\\tta.duckdb

Reads Events.xlsx, normalises it, and writes `dim_event`. publish.py then
builds the dashboard tables from that table, so the scheduled refresh never
needs the spreadsheet.

Re-run whenever the calendar changes. The table is replaced, not appended.

NORMALISATION
-------------
* Event Start Date times are entry artefacts (sequential minutes across
  rows), so only the DATE is kept. Nothing here works at hour grain.
* Rows whose title says RESCHEDUL* are dropped -- they did not happen then.
* Store Location is exploded: "USQ,DTBK,FIFTH" becomes three rows.
* "Not In Store" is flagged off-site rather than mapped to a store.
* n_stores counts how many stores an event touches; an event touching every
  store has no control group and is marked accordingly.
"""

import argparse
import datetime as dt
import glob
import os
import sys

import duckdb
import pandas as pd

EVENTS_GLOB = ["Events.xlsx", "*vents*.xlsx"]
OFFSITE = "Not In Store"
STORE_MAP = {"DTBK": 1, "FIFTH": 2, "5TH": 2, "5TH AVENUE": 2,
             "SOHO": 3, "USQ": 4, "UNION SQUARE": 4}
N_STORES = 4
SERIES_PATTERNS = [("Tray Tables Up", r"TRAY TABLES UP|Tray Tables Up|^TTU"),
                   ("Witchcraft", r"Witchcraft"),
                   ("Get Lost", r"Get Lost"),
                   ("House of High", r"House of High"),
                   ("High Notes Live", r"High Notes Live"),
                   ("Open Book Club", r"Open Book Club")]


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


def load(path):
    d = pd.read_excel(path)
    d.columns = [c.strip() for c in d.columns]
    d["ts"] = pd.to_datetime(d["Event Start Date"],
                             format="%B %d, %Y %I:%M%p", errors="coerce")
    bad = d.ts.isna().sum()
    d = d.dropna(subset=["ts"])
    resched = d["Event"].astype(str).str.contains("RESCHEDUL", case=False,
                                                  na=False)
    n_resched = int(resched.sum())
    d = d[~resched].copy()

    d["event_date"] = d.ts.dt.normalize()
    d["event_name"] = d["Event"].astype(str).str.strip()
    d["event_type"] = d["Event Type"].fillna("Untyped").astype(str).str.strip()
    d["brand_partners"] = d["Brand Partners"].fillna("").astype(str).str.strip()
    d["loc"] = d["Store Location"].fillna("").astype(str)

    d["series"] = "One-off"
    for name, pat in SERIES_PATTERNS:
        m = d["event_name"].str.contains(pat, case=False, regex=True, na=False)
        d.loc[m & (d.series == "One-off"), "series"] = name

    d["tags"] = d["loc"].str.upper().str.split(",")
    e = d.explode("tags")
    e["tags"] = e["tags"].fillna("").str.strip()
    e["is_offsite"] = e["tags"] == OFFSITE.upper()
    e["store_key"] = e["tags"].map(STORE_MAP)

    unmapped = sorted(set(e[e.store_key.isna() & ~e.is_offsite]["tags"]) - {""})
    e = e[e.is_offsite | e.store_key.notna()].copy()
    e["store_key"] = e["store_key"].fillna(0).astype(int)

    stores = (e[~e.is_offsite].groupby(["event_name", "event_date"])
              ["store_key"].nunique().rename("n_stores"))
    e = e.merge(stores, on=["event_name", "event_date"], how="left")
    e["n_stores"] = e["n_stores"].fillna(0).astype(int)
    e["has_control"] = (~e.is_offsite) & (e.n_stores < N_STORES)
    e["event_id"] = (e["event_date"].dt.strftime("%Y%m%d") + "-" +
                     e.groupby(["event_date"]).cumcount().astype(str) + "-" +
                     e["store_key"].astype(str))
    return e, unmapped, bad, n_resched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--events", default=None)
    a = ap.parse_args()

    path = a.events or (find(EVENTS_GLOB) or [None])[0]
    if not path:
        print("No Events.xlsx found.")
        return 1
    if not os.path.exists(a.db):
        print("Database not found: %s" % a.db)
        return 1

    print("events file : %s" % path)
    print("database    : %s" % a.db)

    e, unmapped, bad_dates, n_resched = load(path)
    print()
    print("  rows after explode        : %s" % f"{len(e):,}")
    print("  distinct events           : %s" % f"{e.event_name.nunique():,}")
    print("  unparsable dates dropped  : %d" % bad_dates)
    print("  rescheduled rows dropped  : %d" % n_resched)
    if unmapped:
        print("  !! unmapped store tags    : %s" % ", ".join(unmapped))
    print()
    print("  off-site rows             : %d" % int(e.is_offsite.sum()))
    print("  single-store (has control): %d" % int(e.has_control.sum()))
    print("  chain-wide (no control)   : %d"
          % int(((~e.is_offsite) & (~e.has_control)).sum()))

    out = e[["event_id", "event_name", "event_date", "event_type", "series",
             "brand_partners", "store_key", "is_offsite", "n_stores",
             "has_control"]].copy()
    out["built_at"] = dt.datetime.now()

    con = duckdb.connect(a.db)
    con.register("ev", out)
    con.execute("DROP TABLE IF EXISTS dim_event")
    con.execute("""
        CREATE TABLE dim_event AS
        SELECT CAST(event_id AS VARCHAR)        AS event_id,
               CAST(event_name AS VARCHAR)      AS event_name,
               CAST(event_date AS DATE)         AS event_date,
               CAST(event_type AS VARCHAR)      AS event_type,
               CAST(series AS VARCHAR)          AS series,
               CAST(brand_partners AS VARCHAR)  AS brand_partners,
               CAST(store_key AS INTEGER)       AS store_key,
               CAST(is_offsite AS BOOLEAN)      AS is_offsite,
               CAST(n_stores AS INTEGER)        AS n_stores,
               CAST(has_control AS BOOLEAN)     AS has_control,
               CAST(built_at AS TIMESTAMP)      AS built_at
        FROM ev
    """)
    n = con.execute("SELECT COUNT(*) FROM dim_event").fetchone()[0]

    cov = con.execute("""
        SELECT MIN(txn_ts)::DATE, MAX(txn_ts)::DATE FROM fact_basket
    """).fetchone()
    inside = con.execute("""
        SELECT COUNT(*) FROM dim_event
        WHERE event_date BETWEEN ? AND ?
    """, [cov[0], cov[1]]).fetchone()[0]

    print()
    print("wrote dim_event: %s rows" % f"{n:,}")
    print("  transaction coverage      : %s -> %s" % cov)
    print("  event rows inside coverage: %s (%.0f%%)"
          % (f"{inside:,}", inside / max(n, 1) * 100))
    if inside < n * 0.4:
        print()
        print("  Most events fall outside the transaction window, so they")
        print("  cannot be measured. Load more history to use them.")
    con.close()
    print()
    print("Next: python publish.py --db %s" % a.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
