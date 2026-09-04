"""
publish_event_tracker.py -- Event Performance & Cost, phase one.

Called from publish.py's build(), after build_event_return():

    from publish_event_tracker import build_event_tracker
    ...
    build_event_tracker(con)

Writes dash_event_tracker (one row per event x audience bucket) and
dash_event_tracker_meta. Reads event_audience_map.csv, src.dim_event and
the audience tables, the same way publish_event_return.py does.

THE SHAPE (as specified for the exec report)
--------------------------------------------
Three audience buckets, decided by purchase history BEFORE the event:

    new       no TTA purchase before the event date. Includes signups with
              no POS record at all -- they have never bought anything.
    active    bought within the LAPSE_DAYS before the event
    lapsed    bought before, but not within LAPSE_DAYS

For each bucket: signups, then buyers and net revenue on the day, within
+30 days, and within +90 days of the event. Windows are anchored on the
EVENT DATE, inclusive, cumulative (the +90 figure contains the +30 figure
contains day-of). This differs from publish_event_return.py, which anchors
on each person's first post-event purchase; that design is right for a
return rate, this one is right for "what did the event produce by when".

WHAT IS DELIBERATELY ABSENT
---------------------------
Attendance. Signups are people who registered; there is no reliable
check-in capture (one event: 160+ RSVPs, 23 checked in), so nothing here
is per attendee and no showed/no-show split exists. When check-in data
is trustworthy it slots in as a fourth dimension without changing the
rest. Every rate downstream must say "of signups" in its header.

Same-day sales return. Not computed. Same-day sales lift on-site crosses
zero; net-new customers does not. The cost figure is per net-new customer.

COST
----
net_tta_cost from dim_event (marketing's corrected export), joined on
airtable_record_id. NULL where unrecorded, never 0. Cost per net-new
customer = net cost / new-bucket buyers within 90 days, only once the
event is 90 days old.

IDENTITY
--------
Same two-route resolution as publish_event_return.py: leaflogix POS id
matches customer_key directly, or reaches the basket via
customer_xwalk.name_hash. Ambiguous crosswalk rows are excluded.

PUBLISH.PY CHANGE REQUIRED
--------------------------
Add "bucket" to ALLOWED_TEXT.
"""

from __future__ import annotations

import csv
from pathlib import Path

MAP_FILE = "event_audience_map.csv"
LAPSE_DAYS = 90            # active = bought within this many days before
WINDOWS = (0, 30, 90)      # day-of, +30, +90 (cumulative from event date)
BUCKETS = ("new", "active", "lapsed")


def _load_map(path: str | Path = MAP_FILE) -> dict[str, list[str]]:
    p = Path(path)
    if not p.exists():
        print(f"  {MAP_FILE} not found - event tracker skipped.")
        return {}
    out: dict[str, list[str]] = {}
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            eid = (row.get("event_id") or "").strip()
            aid = (row.get("audience_id") or "").strip()
            if eid and aid:
                out.setdefault(eid, []).append(aid)
    return out


def build_event_tracker(con, map_path: str | Path = MAP_FILE) -> None:
    mapping = _load_map(map_path)
    if not mapping:
        return

    src_tables = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'"
    ).fetchall()}
    need = {"audience_members", "audience_pos_ids", "dim_event",
            "fact_basket", "customer_xwalk"}
    missing = need - src_tables
    if missing:
        print(f"  event tracker: missing {', '.join(sorted(missing))} - "
              f"skipped.")
        return
    dim_cols = {r[1] for r in con.execute(
        "PRAGMA table_info('src.dim_event')").fetchall()}
    if "airtable_record_id" not in dim_cols:
        print("  event tracker: dim_event has no airtable_record_id - "
              "re-run events_ingest.py against the cost export. Skipped.")
        return

    pairs = ", ".join(f"('{e}','{a}')"
                      for e, aud in mapping.items() for a in aud)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW tk_map AS
        SELECT * FROM (VALUES {pairs}) AS t(event_id, audience_id)
    """)

    # Events keyed on the Airtable record. Several dim_event rows (one per
    # store) and several audiences can point at one record; collapse both.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW tk_event AS
        SELECT e.airtable_record_id,
               MIN(e.event_name)              AS event_name,
               MIN(CAST(e.event_date AS DATE)) AS event_date,
               MIN(e.event_type)              AS event_type,
               MIN(e.series)                  AS series,
               BOOL_OR(e.is_offsite)          AS is_offsite,
               MIN(e.cost_state)              AS cost_state,
               BOOL_OR(e.cost_recorded)       AS cost_recorded,
               MIN(e.net_tta_cost)            AS net_tta_cost,
               MIN(e.gross_cost)              AS gross_cost,
               MIN(e.brand_offset)            AS brand_offset
        FROM src.dim_event e
        WHERE e.event_id IN (SELECT event_id FROM tk_map)
        GROUP BY 1
    """)

    # Roster per record: every signup, whether or not they can be resolved.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW tk_roster AS
        SELECT DISTINCT e.airtable_record_id, am.contact_id
        FROM src.audience_members am
        JOIN tk_map m ON CAST(am.audience_id AS VARCHAR) = m.audience_id
        JOIN src.dim_event e ON e.event_id = m.event_id
    """)

    # Candidate customer keys per signup, two routes, unambiguous only.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW tk_keys AS
        WITH pos AS (
            SELECT DISTINCT r.airtable_record_id, r.contact_id,
                   CAST(p.pos_customer_id AS VARCHAR) AS pos_id
            FROM tk_roster r
            JOIN src.audience_pos_ids p USING (contact_id)
            WHERE p.pos_system = 'leaflogix'
              AND p.pos_customer_id IS NOT NULL
        )
        SELECT DISTINCT airtable_record_id, contact_id, pos_id AS customer_key
        FROM pos
        UNION
        SELECT DISTINCT p.airtable_record_id, p.contact_id,
               CAST(x.name_hash AS VARCHAR)
        FROM pos p
        JOIN src.customer_xwalk x
          ON CAST(x.alpine_id AS VARCHAR) = p.pos_id
        WHERE COALESCE(x.ambiguous, FALSE) = FALSE
    """)

    # Every non-return basket a signup made, tagged with days from event.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW tk_txn AS
        SELECT k.airtable_record_id, k.contact_id,
               CAST(b.txn_ts AS DATE) AS day,
               b.basket_net,
               CAST(b.txn_ts AS DATE) - ev.event_date AS d
        FROM tk_keys k
        JOIN tk_event ev USING (airtable_record_id)
        JOIN src.fact_basket b
          ON CAST(b.customer_key AS VARCHAR) = k.customer_key
         AND NOT b.is_return
    """)

    # Bucket by history strictly before the event date.
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW tk_bucket AS
        SELECT r.airtable_record_id, r.contact_id,
               CASE WHEN h.last_before IS NULL THEN 'new'
                    WHEN h.last_before >= ev.event_date - {LAPSE_DAYS}
                         THEN 'active'
                    ELSE 'lapsed' END AS bucket,
               k.contact_id IS NOT NULL AS resolvable
        FROM tk_roster r
        JOIN tk_event ev USING (airtable_record_id)
        LEFT JOIN (SELECT DISTINCT airtable_record_id, contact_id
                   FROM tk_keys) k USING (airtable_record_id, contact_id)
        LEFT JOIN (SELECT airtable_record_id, contact_id,
                          MAX(day) AS last_before
                   FROM tk_txn WHERE d < 0 GROUP BY 1, 2) h
               USING (airtable_record_id, contact_id)
    """)

    max_day = con.execute(
        "SELECT MAX(CAST(txn_ts AS DATE)) FROM src.fact_basket"
    ).fetchone()[0]

    win_cols = []
    for w in WINDOWS:
        tag = "d0" if w == 0 else f"d{w}"
        win_cols.append(f"""
               COUNT(DISTINCT t.contact_id) FILTER (WHERE t.d BETWEEN 0 AND {w})
                                                       AS buyers_{tag},
               COALESCE(SUM(t.basket_net) FILTER (WHERE t.d BETWEEN 0 AND {w}), 0)
                                                       AS revenue_{tag},
               ev.event_date + {w} <= DATE '{max_day}' AS mature_{tag}""")
    win_sql = ",".join(win_cols)

    con.execute(f"""
        CREATE OR REPLACE TABLE dash_event_tracker AS
        WITH per_bucket AS (
            SELECT bk.airtable_record_id, bk.bucket,
                   COUNT(DISTINCT bk.contact_id)                 AS signups,
                   COUNT(DISTINCT bk.contact_id) FILTER (WHERE bk.resolvable)
                                                                 AS resolvable
            FROM tk_bucket bk
            GROUP BY 1, 2
        )
        SELECT ev.airtable_record_id, ev.event_name, ev.event_date,
               ev.event_type, ev.series, ev.is_offsite,
               ev.cost_state, ev.cost_recorded,
               ev.net_tta_cost, ev.gross_cost, ev.brand_offset,
               pb.bucket, pb.signups, pb.resolvable,
               {win_sql}
        FROM tk_event ev
        JOIN per_bucket pb USING (airtable_record_id)
        LEFT JOIN tk_bucket bk
               ON bk.airtable_record_id = pb.airtable_record_id
              AND bk.bucket = pb.bucket
        LEFT JOIN tk_txn t
               ON t.airtable_record_id = bk.airtable_record_id
              AND t.contact_id = bk.contact_id
        GROUP BY ev.airtable_record_id, ev.event_name, ev.event_date,
                 ev.event_type, ev.series, ev.is_offsite, ev.cost_state,
                 ev.cost_recorded, ev.net_tta_cost, ev.gross_cost,
                 ev.brand_offset, pb.bucket, pb.signups, pb.resolvable
        ORDER BY ev.event_date DESC, pb.bucket
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE dash_event_tracker_meta AS
        SELECT {LAPSE_DAYS}       AS lapse_days,
               DATE '{max_day}'   AS data_through,
               (SELECT COUNT(DISTINCT airtable_record_id)
                FROM dash_event_tracker)                    AS events,
               (SELECT COUNT(DISTINCT airtable_record_id)
                FROM dash_event_tracker WHERE cost_recorded) AS events_costed,
               (SELECT COUNT(DISTINCT airtable_record_id)
                FROM dash_event_tracker WHERE mature_d90)    AS events_mature_90,
               FALSE AS has_attendance
    """)

    for v in ("tk_bucket", "tk_txn", "tk_keys", "tk_roster", "tk_event",
              "tk_map"):
        con.execute(f"DROP VIEW IF EXISTS {v}")

    n, ev_n = con.execute("""
        SELECT COUNT(*), COUNT(DISTINCT airtable_record_id)
        FROM dash_event_tracker
    """).fetchone()
    print(f"  dash_event_tracker: {n:,} rows across {ev_n} events")
