"""
Per-event 90-day return, split by whether the attendee was new.

Answers: of the people who attended an event and bought something, how many
came back within 90 days -- and does that differ for people meeting TTA for
the first time versus existing customers who happened to attend?

THE DENOMINATOR PROBLEM, STATED UP FRONT
----------------------------------------
Most attendees cannot be measured. The chain is:

    audience_members        everyone on the roster
      -> audience_pos_ids   only those with a leaflogix POS record
      -> customer_key       only those resolvable to a transacting identity

Roughly a fifth of roster members survive that. A member with no leaflogix
id has never transacted at TTA at all -- which is a finding about the event,
not a gap in the data, and is the same fact behind "74% of attendees have
never transacted". Every table below therefore reports the roster size and
the measurable size side by side. A return rate computed on the measurable
subset says nothing about the majority who never bought, and presenting it
alone would badly overstate what events achieve.

IDENTITY RESOLUTION
-------------------
A leaflogix pos_customer_id reaches fact_basket by two routes, and both are
needed: 43% of ids match customer_key directly (loyalty-identified rows),
while 59% appear in customer_xwalk.alpine_id and reach the basket through
the name_hash key instead. The union is used. Ambiguous crosswalk rows --
where a name hash maps to more than one person -- are excluded, because a
collision would attach someone else's purchase history to an attendee.

DEFINITIONS
-----------
new       the attendee's first ever purchase falls on or after the event
          date. They met TTA at the event, or close enough to it.
regular   they had already bought before the event date.
returned  they made at least one further purchase within 90 days of their
          anchor visit.

The anchor is the first purchase on or after the event date, for both
groups. Anchoring to the event date itself would give regulars a 90-day
window that starts before they next walked in, which inflates their rate
relative to new attendees for no reason other than the definition.

MATURITY
--------
An event needs 90 days of data after it to be measurable at all. Events
inside that trailing window are excluded and counted separately, rather
than reported with an artificially low rate.

MAPPING
-------
Reads event_audience_map.csv, which pairs AIQ audience ids to dim_event
rows. That file is data, not code: correct a row and re-run publish. Rows
with a blank event_id are skipped. Where several audiences map to one event
their members are unioned, so a person captured twice is counted once.
"""

from __future__ import annotations

import csv
from pathlib import Path

MAP_FILE = "event_audience_map.csv"
WINDOW_DAYS = 90
MIN_MEASURABLE = 10          # below this a per-event rate is noise


def _load_map(path: str | Path = MAP_FILE) -> dict[str, list[str]]:
    """event_id -> [audience_id, ...]. Missing file is not fatal."""
    p = Path(path)
    if not p.exists():
        print(f"  {MAP_FILE} not found — event return analysis skipped.")
        return {}
    out: dict[str, list[str]] = {}
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            eid = (row.get("event_id") or "").strip()
            aid = (row.get("audience_id") or "").strip()
            if eid and aid:
                out.setdefault(eid, []).append(aid)
    return out


def build_event_return(con, map_path: str | Path = MAP_FILE) -> None:
    """Create dash_event_return and dash_event_return_meta.

    Called from publish.build while src is attached.
    """
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
        print(f"  event return: missing {', '.join(sorted(missing))} — "
              f"skipped. Run migrate_audiences.py if the audience tables "
              f"are still in data/cerebral_audiences.duckdb.")
        return

    pairs = ", ".join(f"('{e}','{a}')"
                      for e, aud in mapping.items() for a in aud)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW ev_map AS
        SELECT * FROM (VALUES {pairs}) AS t(event_id, audience_id)
    """)

    # An attendee's candidate customer keys: the POS id itself, plus any
    # name_hash it resolves to through the crosswalk. Unambiguous rows only.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW ev_att AS
        WITH roster AS (
            SELECT DISTINCT m.event_id, am.contact_id
            FROM src.audience_members am
            JOIN ev_map m ON CAST(am.audience_id AS VARCHAR) = m.audience_id
        ), pos AS (
            SELECT DISTINCT r.event_id, r.contact_id,
                   CAST(p.pos_customer_id AS VARCHAR) AS pos_id
            FROM roster r
            JOIN src.audience_pos_ids p USING (contact_id)
            WHERE p.pos_system = 'leaflogix'
              AND p.pos_customer_id IS NOT NULL
        )
        SELECT DISTINCT event_id, contact_id, pos_id AS customer_key
        FROM pos
        UNION
        SELECT DISTINCT p.event_id, p.contact_id,
               CAST(x.name_hash AS VARCHAR)
        FROM pos p
        JOIN src.customer_xwalk x
          ON CAST(x.alpine_id AS VARCHAR) = p.pos_id
        WHERE COALESCE(x.ambiguous, FALSE) = FALSE
    """)

    # First ever purchase per key, across all loaded history -- not just the
    # audience's own sales export, which covers a limited period.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW ev_first AS
        SELECT CAST(customer_key AS VARCHAR) AS customer_key,
               MIN(CAST(txn_ts AS DATE)) AS first_day
        FROM src.fact_basket
        WHERE NOT is_return AND customer_key IS NOT NULL
        GROUP BY 1
    """)

    max_day = con.execute(
        "SELECT MAX(CAST(txn_ts AS DATE)) FROM src.fact_basket"
    ).fetchone()[0]

    con.execute(f"""
        CREATE OR REPLACE TABLE dash_event_return AS
        WITH ev AS (
            SELECT DISTINCT e.event_id, e.event_name,
                   CAST(e.event_date AS DATE) AS event_date,
                   e.event_type, e.store_key, e.is_offsite
            FROM src.dim_event e
            JOIN ev_map m ON CAST(e.event_id AS VARCHAR) = m.event_id
        ),
        -- One row per attendee per event, collapsing the several customer
        -- keys a person may carry.
        anchor AS (
            SELECT a.event_id, a.contact_id,
                   MIN(CAST(b.txn_ts AS DATE)) AS anchor_day,
                   MIN(f.first_day)            AS first_day
            FROM ev_att a
            JOIN ev USING (event_id)
            JOIN src.fact_basket b
              ON CAST(b.customer_key AS VARCHAR) = a.customer_key
             AND NOT b.is_return
             AND CAST(b.txn_ts AS DATE) >= ev.event_date
            JOIN ev_first f
              ON f.customer_key = CAST(b.customer_key AS VARCHAR)
            GROUP BY 1, 2
        ),
        seg AS (
            SELECT an.event_id, an.contact_id, an.anchor_day,
                   CASE WHEN an.first_day >= ev.event_date
                        THEN 'New' ELSE 'Regular' END AS segment,
                   EXISTS (
                       SELECT 1
                       FROM ev_att a2
                       JOIN src.fact_basket b2
                         ON CAST(b2.customer_key AS VARCHAR) = a2.customer_key
                       WHERE a2.contact_id = an.contact_id
                         AND a2.event_id = an.event_id
                         AND NOT b2.is_return
                         AND CAST(b2.txn_ts AS DATE) > an.anchor_day
                         AND CAST(b2.txn_ts AS DATE)
                             <= an.anchor_day + {WINDOW_DAYS}
                   ) AS returned
            FROM anchor an JOIN ev USING (event_id)
        ),
        roster AS (
            SELECT m.event_id,
                   COUNT(DISTINCT am.contact_id) AS roster_members
            FROM src.audience_members am
            JOIN ev_map m ON CAST(am.audience_id AS VARCHAR) = m.audience_id
            GROUP BY 1
        ),
        measurable AS (
            SELECT event_id, COUNT(DISTINCT contact_id) AS n
            FROM ev_att GROUP BY 1
        )
        SELECT ev.event_id, ev.event_name, ev.event_date, ev.event_type,
               ev.store_key, ev.is_offsite,
               COALESCE(r.roster_members, 0) AS roster_members,
               COALESCE(ms.n, 0)             AS pos_matched,
               s.segment,
               COUNT(*)                                   AS buyers,
               COUNT(*) FILTER (WHERE s.returned)         AS returned,
               ev.event_date + {WINDOW_DAYS} <= DATE '{max_day}' AS mature
        FROM ev
        LEFT JOIN roster     r  USING (event_id)
        LEFT JOIN measurable ms USING (event_id)
        LEFT JOIN seg        s  USING (event_id)
        WHERE s.segment IS NOT NULL
        GROUP BY 1,2,3,4,5,6,7,8,9,12
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE dash_event_return_meta AS
        SELECT {WINDOW_DAYS}      AS window_days,
               {MIN_MEASURABLE}   AS min_measurable,
               DATE '{max_day}'   AS data_through,
               (SELECT COUNT(DISTINCT event_id) FROM dash_event_return)
                                  AS events_mapped,
               (SELECT COUNT(DISTINCT event_id) FROM dash_event_return
                WHERE NOT mature) AS events_immature
    """)

    # These views reference src, and publish detaches src before the leak
    # check scans every object in the database. Leaving them behind makes
    # that scan fail on a view it cannot resolve.
    for v in ("ev_att", "ev_first", "ev_map"):
        con.execute(f"DROP VIEW IF EXISTS {v}")

    n = con.execute("SELECT COUNT(*) FROM dash_event_return").fetchone()[0]
    ev_n = con.execute(
        "SELECT COUNT(DISTINCT event_id) FROM dash_event_return").fetchone()[0]
    print(f"  dash_event_return: {n:,} rows across {ev_n} events")
