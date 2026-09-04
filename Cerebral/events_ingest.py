"""
events_ingest.py -- write the events calendar into the Cerebral DB.

    python events_ingest.py --db ..\\tta.duckdb
    python events_ingest.py --db ..\\tta.duckdb --cost events-cost-calendar.csv
    python events_ingest.py --db ..\\tta.duckdb --cost ... --events Events.xlsx

Reads marketing's corrected cost export (events-cost-calendar*.csv), normalises
it, and writes `dim_event`. publish.py then builds the dashboard tables from
that table, so the scheduled refresh never needs the spreadsheet.

Re-run whenever the calendar changes. The table is replaced, not appended.

SOURCE OF TRUTH
---------------
The cost export is authoritative for what an event was and what it cost.
It replaces Events.xlsx as the calendar. Events.xlsx is still read, if
present, for two things the export does not carry correctly:

* Brand Partners -- the export has no such column.
* The LOCAL event date. The export's event_start_date is in UTC, so any
  event starting at 7pm or later New York time is dated the following
  day. Events.xlsx carries the local timestamp. Where the two disagree by
  exactly one day, the xlsx date wins; the export date is kept in
  `event_date_export` and the run summary counts the corrections. The
  lift model matches on date, so a one-day shift measures the wrong day.

Both are joined on event name and are optional -- once the export carries
Brand Partners and a local-time date the xlsx can be dropped.

Cost columns come straight from the export. `Total Cost for Event` from the
Airtable rollup is NOT used: it counts every shared budget line in full
against each event it touches. The export keeps that figure as
`airtable_rollup_DO_NOT_USE` for audit only, and it is not written here.

SHARED-LINE REALLOCATION
------------------------
Marketing splits every shared budget line evenly across the events it is
attached to. The export does that split against ALL attached events,
including ones that were cancelled or declined -- so a flowers order
shared with ten events, one of which was cancelled, leaves a tenth of the
cost charged to nothing. This ingest re-splits each shared line across
the attached events that are not `exclude_*` (planned events keep their
share: they will happen). The export's own figures are kept in
`*_export` columns and `realloc_delta` shows the change per event, so the
correction is visible rather than silent.

COST STATE
----------
* has_cost               net_tta_cost is a real figure
* no_budget_linked       nothing recorded. This is UNRECORDED, not free --
                         net cost is NULL, never 0. Marketing's convention.
* exclude_*              did not happen or is not being chased; dropped.

Only completed events are kept. Declined, cancelled and future rows are
counted in the run summary and dropped; the lift model must not look for a
sales effect on a day nothing happened.

NORMALISATION
-------------
* Only the DATE is kept. Nothing here works at hour grain.
* Rows whose title says RESCHEDUL* are dropped as a belt-and-braces check;
  the export's status field should already have caught them.
* Store Location is exploded on "|" or ",": "USQ|DTBK|FIFTH" is three rows.
* "Not In Store" is flagged off-site rather than mapped to a store.
* n_stores counts how many stores an event touches; an event touching every
  store has no control group and is marked accordingly.
* event_id is <airtable_record_id>-<store_key>, so it is stable across
  re-exports. NOTE: this changes every event_id from the previous scheme
  (date-index-store). event_audience_map.csv must be rebuilt after the
  first run (build_event_audience_map.py).
* Cost is carried at EVENT level on every exploded row. Summing
  net_tta_cost across rows of a multi-store event would multiply it;
  aggregate by airtable_record_id, never by row.
"""

import argparse
import datetime as dt
import glob
import os
import re
import sys

import duckdb
import pandas as pd

COST_GLOB = ["events-cost-calendar*.csv", "*cost*calendar*.csv"]
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
                   ("Open Book Club", r"Open Book Club"),
                   ("Buds & Blooms", r"Buds & Blooms|Buds and Blooms"),
                   ("Cali Sober Sips", r"Cali Sober Sips"),
                   ("Airwaves", r"Airwaves"),
                   ("Clear for Takeoff", r"Clear for Takeoff")]

COMPLETE = "Complete"          # matched by substring; export has an emoji
SHARED_LINE = re.compile(
    r"^(?P<label>.+?)\s+\$(?P<total>[\d,]+\.\d{2})\s*/\s*(?P<n>\d+)\s+events?"
    r"\s*=\s*\$(?P<share>[\d,]+\.\d{2})\s*$")
COST_COLS = ["direct_cost", "shared_cost_allocated", "gross_cost",
             "brand_offset", "net_tta_cost"]


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


def _attendance(s):
    """expected_attendance is free text ('600-800 customers', '40+', '~100').
    Take the first integer as a rough figure; keep the raw string too."""
    if pd.isna(s):
        return pd.NA
    m = re.search(r"\d[\d,]*", str(s))
    return int(m.group(0).replace(",", "")) if m else pd.NA


def reallocate_shared(d):
    """Re-split shared lines across non-excluded sharers.

    Returns (d, changes). d gains shared_cost_allocated_export,
    gross_cost_export, net_tta_cost_export, realloc_delta, and has
    shared_cost_allocated / gross_cost / net_tta_cost recomputed where a
    sharer was excluded. Lines that do not parse, or whose stated sharer
    count does not match the rows in the file, are left exactly as
    exported.
    """
    d = d.copy()
    for c in ("shared_cost_allocated", "gross_cost", "net_tta_cost"):
        d[f"{c}_export"] = d[c]
    d["realloc_delta"] = 0.0

    parts = []
    unparsed = 0
    for idx, r in d.iterrows():
        raw = r.get("shared_line_detail")
        if pd.isna(raw) or not str(raw).strip():
            continue
        for seg in str(raw).split("||"):
            m = SHARED_LINE.match(seg.strip())
            if not m:
                unparsed += 1
                continue
            parts.append({
                "idx": idx,
                "label": m["label"].strip(),
                "total": float(m["total"].replace(",", "")),
                "n": int(m["n"]),
                "share": float(m["share"].replace(",", "")),
                "keep": not str(r.cost_state).startswith("exclude"),
            })
    if not parts:
        return d, {"lines": 0, "reallocated": 0, "unparsed": unparsed,
                   "mismatched": 0, "orphaned_dollars": 0.0, "detail": []}

    L = pd.DataFrame(parts)
    g = (L.groupby(["label", "total"])
           .agg(rows=("idx", "count"), n=("n", "first"), keep=("keep", "sum"))
           .reset_index())
    ok = g[g.rows == g.n]
    fix = ok[ok.keep < ok.n]
    detail = []
    orphaned = 0.0
    for a in fix.itertuples():
        old = a.total / a.n
        new = a.total / a.keep if a.keep else 0.0
        orphaned += old * (a.n - a.keep)
        detail.append((a.label, a.total, a.n, int(a.keep), old, new))
        sel = (L.label == a.label) & (L.total == a.total)
        for row in L[sel].itertuples():
            delta = (new - old) if row.keep else -old
            d.at[row.idx, "realloc_delta"] += delta

    moved = d.realloc_delta != 0
    d.loc[moved, "shared_cost_allocated"] = (
        d.loc[moved, "shared_cost_allocated"].fillna(0) + d.loc[moved, "realloc_delta"])
    d.loc[moved, "gross_cost"] = (
        d.loc[moved, "direct_cost"].fillna(0) + d.loc[moved, "shared_cost_allocated"])
    d.loc[moved, "net_tta_cost"] = (
        d.loc[moved, "gross_cost"] - d.loc[moved, "brand_offset"].fillna(0))
    return d, {"lines": int(len(g)), "reallocated": int(len(fix)),
               "unparsed": unparsed, "mismatched": int((g.rows != g.n).sum()),
               "orphaned_dollars": float(orphaned), "detail": detail}


def load_events_xlsx(path):
    """name -> (local event date, Brand Partners) from the legacy
    Events.xlsx. Optional. Names that appear more than once with different
    dates are dropped -- an ambiguous match is worse than none."""
    if not path or not os.path.exists(path):
        return None
    x = pd.read_excel(path)
    x.columns = [c.strip() for c in x.columns]
    if "Event" not in x.columns or "Event Start Date" not in x.columns:
        return None
    x["xlsx_date"] = pd.to_datetime(x["Event Start Date"],
                                    format="%B %d, %Y %I:%M%p",
                                    errors="coerce").dt.normalize()
    x["event_name"] = x["Event"].astype(str).str.strip()
    x["brand_partners"] = (x["Brand Partners"].fillna("").astype(str).str.strip()
                           if "Brand Partners" in x.columns else "")
    x = x.dropna(subset=["xlsx_date"])
    amb = x.groupby("event_name")["xlsx_date"].nunique()
    x = x[~x.event_name.isin(amb[amb > 1].index)]
    return x[["event_name", "xlsx_date", "brand_partners"]].drop_duplicates("event_name")


def load(cost_path, events_path=None, today=None):
    today = pd.Timestamp(today or dt.date.today()).normalize()
    d = pd.read_csv(cost_path)
    d.columns = [c.strip() for c in d.columns]
    stats = {}

    d = d.dropna(subset=["airtable_record_id", "event_name"]).copy()
    d["event_date"] = pd.to_datetime(d["event_start_date"], errors="coerce")
    stats["unparsable dates dropped"] = int(d.event_date.isna().sum())
    d = d.dropna(subset=["event_date"])
    d["event_date"] = d.event_date.dt.normalize()

    d["event_name"] = d["event_name"].astype(str).str.strip()
    d["event_status"] = d["event_status"].fillna("").astype(str).str.strip()
    d["cost_state"] = d["cost_state"].fillna("no_budget_linked").astype(str)

    # --- cost columns numeric; re-split shared lines while dead rows are
    #     still present (their shares are what gets redistributed) --------
    for c in COST_COLS:
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    d, realloc = reallocate_shared(d)
    stats["shared lines parsed"] = realloc["lines"]
    stats["shared lines re-split"] = realloc["reallocated"]
    if realloc["unparsed"]:
        stats["!! shared lines unparsed"] = realloc["unparsed"]
    if realloc["mismatched"]:
        stats["!! shared lines count mismatch"] = realloc["mismatched"]
    stats["$ recovered from dead events"] = round(realloc["orphaned_dollars"])

    # --- what actually happened -----------------------------------------
    excluded = d.cost_state.str.startswith("exclude")
    not_complete = ~d.event_status.str.contains(COMPLETE, case=False)
    future = d.event_date > today
    resched = d.event_name.str.contains("RESCHEDUL", case=False, na=False)
    stats["excluded by cost_state"] = int(excluded.sum())
    stats["not complete (declined/planning)"] = int((~excluded & not_complete).sum())
    stats["dated in the future"] = int((~excluded & ~not_complete & future).sum())
    stats["rescheduled by title"] = int(
        (~excluded & ~not_complete & ~future & resched).sum())
    d = d[~excluded & ~not_complete & ~future & ~resched].copy()

    # --- cost -------------------------------------------------------------
    unrecorded = d.cost_state == "no_budget_linked"
    # Never let an unrecorded event read as free.
    d.loc[unrecorded, COST_COLS] = pd.NA
    d["cost_recorded"] = ~unrecorded
    d["shared_lines"] = (d.get("shared_line_detail", pd.Series(index=d.index))
                         .fillna("").astype(str)
                         .apply(lambda s: s.count("||") + 1 if s.strip() else 0))
    d["expected_attendance_raw"] = d.get("expected_attendance")
    d["expected_attendance"] = d["expected_attendance_raw"].apply(_attendance)
    stats["events with recorded cost"] = int(d.cost_recorded.sum())
    stats["events UNRECORDED"] = int(unrecorded.sum())

    # --- descriptive -------------------------------------------------------
    d["event_type"] = d["event_type"].fillna("Untyped").astype(str).str.strip()
    d["internal_external"] = (d.get("internal_vs_external", pd.Series(index=d.index))
                              .fillna("").astype(str).str.strip())
    d["series"] = "One-off"
    for name, pat in SERIES_PATTERNS:
        m = d["event_name"].str.contains(pat, case=False, regex=True, na=False)
        d.loc[m & (d.series == "One-off"), "series"] = name

    d["event_date_export"] = d["event_date"]
    xl = load_events_xlsx(events_path)
    if xl is not None:
        d = d.merge(xl, on="event_name", how="left")
        shift = (d["event_date"] - d["xlsx_date"]).dt.days
        # +1 exactly is the UTC rollover signature; anything else is a
        # genuine disagreement and the export stands.
        fix = shift == 1
        d.loc[fix, "event_date"] = d.loc[fix, "xlsx_date"]
        stats["dates corrected (export is UTC)"] = int(fix.sum())
        stats["dates disagree by >1 day (export kept)"] = int(
            (shift.notna() & (shift != 0) & (shift != 1)).sum())
        stats["brand partners matched from xlsx"] = int(
            (d.brand_partners.fillna("") != "").sum())
        d = d.drop(columns=["xlsx_date"])
    d["brand_partners"] = d.get("brand_partners", pd.Series(index=d.index)).fillna("")

    # --- explode stores ----------------------------------------------------
    d["loc"] = d["store_location"].fillna("").astype(str)
    d["tags"] = d["loc"].str.upper().str.replace(",", "|").str.split("|")
    e = d.explode("tags")
    e["tags"] = e["tags"].fillna("").str.strip()
    e["is_offsite"] = e["tags"] == OFFSITE.upper()
    e["store_key"] = e["tags"].map(STORE_MAP)

    unmapped = sorted(set(e[e.store_key.isna() & ~e.is_offsite]["tags"]) - {""})
    stats["rows with no store tag dropped"] = int(
        ((e.tags == "") & ~e.is_offsite).sum())
    e = e[e.is_offsite | e.store_key.notna()].copy()
    e["store_key"] = e["store_key"].fillna(0).astype(int)

    stores = (e[~e.is_offsite].groupby("airtable_record_id")
              ["store_key"].nunique().rename("n_stores"))
    e = e.merge(stores, on="airtable_record_id", how="left")
    e["n_stores"] = e["n_stores"].fillna(0).astype(int)
    e["has_control"] = (~e.is_offsite) & (e.n_stores < N_STORES)
    e["event_id"] = e["airtable_record_id"] + "-" + e["store_key"].astype(str)
    dup = e.event_id.duplicated().sum()
    if dup:
        e = e.drop_duplicates("event_id")
        stats["duplicate store rows collapsed"] = int(dup)
    return e, unmapped, stats, realloc["detail"]


OUT_COLS = ["event_id", "airtable_record_id", "event_name", "event_date",
            "event_type", "series", "brand_partners", "internal_external",
            "store_key", "is_offsite", "n_stores", "has_control",
            "cost_state", "cost_recorded", "direct_cost",
            "shared_cost_allocated", "gross_cost", "brand_offset",
            "net_tta_cost", "shared_lines", "expected_attendance",
            "expected_attendance_raw", "event_date_export",
            "shared_cost_allocated_export",
            "gross_cost_export", "net_tta_cost_export", "realloc_delta"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cost", default=None,
                    help="marketing cost export (events-cost-calendar*.csv)")
    ap.add_argument("--events", default=None,
                    help="legacy Events.xlsx, only for Brand Partners")
    ap.add_argument("--today", default=None,
                    help="override the as-of date (YYYY-MM-DD)")
    a = ap.parse_args()

    cost = a.cost or (find(COST_GLOB) or [None])[0]
    if not cost:
        print("No events-cost-calendar*.csv found.")
        return 1
    events = a.events or (find(EVENTS_GLOB) or [None])[0]
    if not os.path.exists(a.db):
        print("Database not found: %s" % a.db)
        return 1

    print("cost export : %s" % cost)
    print("brand xlsx  : %s" % (events or "(none - brand_partners blank)"))
    print("database    : %s" % a.db)

    e, unmapped, stats, realloc_detail = load(cost, events, a.today)
    print()
    for k, v in stats.items():
        print("  %-36s: %s" % (k, f"{v:,}"))
    for label, total, n, keep, old, new in realloc_detail:
        print(f"    re-split  {label[:45]:<45} ${total:>9,.2f}  {n} -> {keep} "
              f"sharers  ${old:>8,.2f} -> ${new:>8,.2f}")
    if unmapped:
        print("  !! unmapped store tags              : %s" % ", ".join(unmapped))
    print()
    print("  rows after explode        : %s" % f"{len(e):,}")
    print("  distinct events           : %s" % f"{e.airtable_record_id.nunique():,}")
    print("  off-site rows             : %d" % int(e.is_offsite.sum()))
    print("  single-store (has control): %d" % int(e.has_control.sum()))
    print("  chain-wide (no control)   : %d"
          % int(((~e.is_offsite) & (~e.has_control)).sum()))

    out = e[OUT_COLS].copy()
    out["built_at"] = dt.datetime.now()
    for c in COST_COLS + ["shared_cost_allocated_export", "gross_cost_export",
                          "net_tta_cost_export", "realloc_delta"]:
        out[c] = out[c].astype("Float64")
    out["expected_attendance"] = out["expected_attendance"].astype("Int64")

    con = duckdb.connect(a.db)
    con.register("ev", out)
    con.execute("DROP TABLE IF EXISTS dim_event")
    con.execute("""
        CREATE TABLE dim_event AS
        SELECT CAST(event_id AS VARCHAR)             AS event_id,
               CAST(airtable_record_id AS VARCHAR)  AS airtable_record_id,
               CAST(event_name AS VARCHAR)          AS event_name,
               CAST(event_date AS DATE)             AS event_date,
               CAST(event_type AS VARCHAR)          AS event_type,
               CAST(series AS VARCHAR)              AS series,
               CAST(brand_partners AS VARCHAR)      AS brand_partners,
               CAST(internal_external AS VARCHAR)   AS internal_external,
               CAST(store_key AS INTEGER)           AS store_key,
               CAST(is_offsite AS BOOLEAN)          AS is_offsite,
               CAST(n_stores AS INTEGER)            AS n_stores,
               CAST(has_control AS BOOLEAN)         AS has_control,
               CAST(cost_state AS VARCHAR)          AS cost_state,
               CAST(cost_recorded AS BOOLEAN)       AS cost_recorded,
               CAST(direct_cost AS DOUBLE)          AS direct_cost,
               CAST(shared_cost_allocated AS DOUBLE) AS shared_cost_allocated,
               CAST(gross_cost AS DOUBLE)           AS gross_cost,
               CAST(brand_offset AS DOUBLE)         AS brand_offset,
               CAST(net_tta_cost AS DOUBLE)         AS net_tta_cost,
               CAST(shared_lines AS INTEGER)        AS shared_lines,
               CAST(expected_attendance AS INTEGER) AS expected_attendance,
               CAST(expected_attendance_raw AS VARCHAR) AS expected_attendance_raw,
               CAST(event_date_export AS DATE)      AS event_date_export,
               CAST(shared_cost_allocated_export AS DOUBLE) AS shared_cost_allocated_export,
               CAST(gross_cost_export AS DOUBLE)    AS gross_cost_export,
               CAST(net_tta_cost_export AS DOUBLE)  AS net_tta_cost_export,
               CAST(realloc_delta AS DOUBLE)        AS realloc_delta,
               CAST(built_at AS TIMESTAMP)          AS built_at
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
    net = con.execute("""
        SELECT SUM(net_tta_cost) FROM (
            SELECT DISTINCT airtable_record_id, net_tta_cost FROM dim_event)
    """).fetchone()[0] or 0

    print()
    print("wrote dim_event: %s rows" % f"{n:,}")
    print("  transaction coverage      : %s -> %s" % cov)
    print("  event rows inside coverage: %s (%.0f%%)"
          % (f"{inside:,}", inside / max(n, 1) * 100))
    print("  total net TTA cost        : $%s" % f"{net:,.0f}")
    if inside < n * 0.4:
        print()
        print("  Most events fall outside the transaction window, so they")
        print("  cannot be measured. Load more history to use them.")
    con.close()
    print()
    print("Next: python build_event_audience_map.py   (event_ids changed)")
    print("      python publish.py --db %s" % a.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
