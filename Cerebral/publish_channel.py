"""
publish_channel.py -- channel x day-of-week tables for the Promo Lab
"Channel & Day Promo" sub-tab.

Nothing already in cerebral_dash.duckdb carries channel AND a date grain,
so a store x channel x weekday promo cannot be simulated from the existing
tables. This adds three.

    dash_channel_dow    store x channel x weekday baseline. Net, margin,
                        units, baskets, discount and the number of distinct
                        calendar days behind each cell, over a trailing
                        window. The spine of the simulator.

    dash_channel_pair   within-customer basket-size ratio between channels.
                        The raw AOV gap between Delivery ($104.79) and
                        In-Store ($61.22) is mostly selection -- delivery
                        attracts stock-up buyers and carries order minimums.
                        Comparing the same PERSON against themselves gives
                        ~1.20, and that is the number the switching term
                        needs. Emitted per store and chain-wide (store 0),
                        because per-store cells get thin fast.

    dash_channel_stick  of customers whose FIRST basket in a non-In-Store
                        channel landed in the window, what share used that
                        channel again. This is the retention case for paying
                        to move someone across channels, and it is the
                        argument that survives when the margin math is flat.

RUN ORDER

publish.py rebuilds cerebral_dash.duckdb wholesale, so this must run AFTER
it, the same way publish_retention.py and publish_events.py do. Running it
before means the tables get thrown away.

    cd C:\\Users\\User\\cerebral\\Cerebral
    python publish.py
    python publish_channel.py

Reads ..\\tta.duckdb read-only. Writes only the three tables above.

    python publish_channel.py --check    print row counts and exit
    python publish_channel.py --upload   build, then push the DB to Drive

UPLOAD ORDERING -- THE THING THAT BITES

publish.py --upload pushes cerebral_dash.duckdb to Drive as its final act.
Running this script afterwards writes the three tables to the LOCAL copy
only, so the deployed app downloads a database without them and the tab
renders its "not published" message to everyone while working perfectly on
your machine.

So do NOT use publish.py --upload any more. Let this script do the upload,
because it runs last:

    python publish.py                     build, no upload
    python publish_channel.py --upload    add tables, THEN push

Same DriveClient and the same TTA_DRIVE_STATE environment variable that
publish.py uses, so there is no second credential path to maintain.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

SRC = Path(r"..\tta.duckdb")
DST = Path("cerebral_dash.duckdb")

# Trailing window for the baseline. Long enough that a single store x
# channel x weekday cell has real days behind it (26 Saturdays at 180),
# short enough that it reflects how the stores trade now.
WINDOW = 180
# Behavioural sections look back further -- switching and channel
# stickiness need history, not currency.
LOOKBACK = 365

TABLES = ["dash_channel_dow", "dash_channel_pair", "dash_channel_stick"]

DOW_NAMES = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
             5: "Friday", 6: "Saturday", 7: "Sunday"}


def _dk(d: date) -> int:
    """date -> the YYYYMMDD integer the warehouse stores."""
    return int(d.strftime("%Y%m%d"))


def _from_dk(n: int) -> date:
    return date(n // 10000, (n // 100) % 100, n % 100)


def check() -> int:
    if not DST.exists():
        print(f"{DST} not found")
        return 1
    con = duckdb.connect(str(DST), read_only=True)
    for t in TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:22s} {n:>8,} rows")
        except Exception:
            print(f"  {t:22s}  MISSING")
    con.close()
    return 0


def upload() -> int:
    """Push cerebral_dash.duckdb to Drive, exactly as publish.py does."""
    folder = os.environ.get("TTA_DRIVE_STATE")
    if not folder:
        print("  TTA_DRIVE_STATE not set -- SKIPPING UPLOAD.\n"
              "  The deployed app will not see the new tables.")
        return 1
    try:
        from tta_drive import DriveClient
    except ImportError as e:
        print(f"  could not import tta_drive: {e}")
        return 1
    print("\n  uploading...")
    DriveClient().upload(DST, folder)
    print(f"  {DST} pushed to Drive")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="print row counts and exit")
    ap.add_argument("--upload", action="store_true",
                    help="push the database to Drive after building")
    args = ap.parse_args()

    if args.check:
        return check()

    if not SRC.exists():
        print(f"{SRC} not found -- run this from the Cerebral repo directory")
        return 1
    if not DST.exists():
        print(f"{DST} not found -- run publish.py first")
        return 1

    src = duckdb.connect(str(SRC), read_only=True)

    last = src.execute("SELECT MAX(date_key) FROM fact_basket").fetchone()[0]
    last_d = _from_dk(last)
    cut = _dk(last_d - timedelta(days=WINDOW))
    cut_back = _dk(last_d - timedelta(days=LOOKBACK))
    print(f"last day in data {last} | baseline from {cut} | "
          f"behaviour from {cut_back}")

    # ------------------------------------------------------ dash_channel_dow
    # Net, margin and units come from fact_line; baskets and discount from
    # fact_basket. They are joined at basket grain first so a multi-line
    # basket is not counted once per line.
    dow = src.execute(f"""
        WITH bl AS (
          SELECT basket_id,
                 SUM(net_sales)    AS net,
                 SUM(gross_margin) AS gm,
                 SUM(units)        AS units
          FROM fact_line
          WHERE date_key >= {cut}
          GROUP BY 1
        )
        SELECT b.store_key,
               b.channel,
               b.day_of_week           AS dow,
               COUNT(*)                AS baskets,
               SUM(bl.net)             AS net,
               SUM(bl.gm)              AS gm,
               SUM(bl.units)           AS units,
               SUM(b.discount_amt)     AS discount,
               COUNT(DISTINCT b.date_key) AS days,
               {WINDOW}                AS window_days
        FROM fact_basket b
        JOIN bl USING (basket_id)
        WHERE NOT b.is_return AND b.date_key >= {cut}
        GROUP BY 1, 2, 3
    """).df()
    dow["dow_name"] = dow["dow"].map(DOW_NAMES)
    print(f"  dash_channel_dow    {len(dow):>6,} rows")

    # ----------------------------------------------------- dash_channel_pair
    # Ordered pairs in both directions, so the tab can look up
    # (at-risk channel -> promoted channel) without reversing a ratio.
    # A customer must have at least two baskets in EACH channel; one
    # experimental order is not a channel preference.
    pair_store = src.execute(f"""
        WITH per AS (
          SELECT store_key, customer_key, channel,
                 COUNT(*) AS n, AVG(basket_net) AS aov
          FROM fact_basket
          WHERE NOT is_return AND customer_key IS NOT NULL
            AND date_key >= {cut_back}
          GROUP BY 1, 2, 3
        )
        SELECT a.store_key,
               a.channel AS ch_from, b.channel AS ch_to,
               COUNT(*)        AS customers,
               AVG(a.aov)      AS aov_from,
               AVG(b.aov)      AS aov_to,
               MEDIAN(b.aov / NULLIF(a.aov, 0)) AS median_ratio
        FROM per a
        JOIN per b ON a.store_key = b.store_key
                  AND a.customer_key = b.customer_key
                  AND a.channel <> b.channel
        WHERE a.n >= 2 AND b.n >= 2
        GROUP BY 1, 2, 3
    """).df()

    # store_key 0 = chain-wide, aggregating each customer across stores.
    # The per-store cells thin out badly (Fifth Ave Delivery is 3,663
    # baskets all year), so the tab falls back to this when a cell is
    # too small to trust.
    pair_chain = src.execute(f"""
        WITH per AS (
          SELECT customer_key, channel,
                 COUNT(*) AS n, AVG(basket_net) AS aov
          FROM fact_basket
          WHERE NOT is_return AND customer_key IS NOT NULL
            AND date_key >= {cut_back}
          GROUP BY 1, 2
        )
        SELECT 0 AS store_key,
               a.channel AS ch_from, b.channel AS ch_to,
               COUNT(*)        AS customers,
               AVG(a.aov)      AS aov_from,
               AVG(b.aov)      AS aov_to,
               MEDIAN(b.aov / NULLIF(a.aov, 0)) AS median_ratio
        FROM per a
        JOIN per b ON a.customer_key = b.customer_key
                  AND a.channel <> b.channel
        WHERE a.n >= 2 AND b.n >= 2
        GROUP BY 1, 2, 3
    """).df()

    import pandas as pd
    pair = pd.concat([pair_chain, pair_store], ignore_index=True)
    print(f"  dash_channel_pair   {len(pair):>6,} rows")

    # ---------------------------------------------------- dash_channel_stick
    # "First" is measured over all history, not just the window, so a
    # long-standing delivery user is not miscounted as a new adopter
    # because their earliest basket predates the lookback. Only the
    # first-time COHORT is windowed.
    stick_store = src.execute(f"""
        WITH firsts AS (
          SELECT customer_key, store_key, channel, MIN(date_key) AS first_dk
          FROM fact_basket
          WHERE NOT is_return AND customer_key IS NOT NULL
            AND channel <> 'In-Store'
          GROUP BY 1, 2, 3
        ),
        later AS (
          SELECT f.store_key, f.channel, f.customer_key,
                 COUNT(b.basket_id) AS repeats
          FROM firsts f
          LEFT JOIN fact_basket b
            ON  b.customer_key = f.customer_key
            AND b.channel      = f.channel
            AND b.store_key    = f.store_key
            AND b.date_key     > f.first_dk
            AND NOT b.is_return
          WHERE f.first_dk BETWEEN {cut_back} AND {cut}
          GROUP BY 1, 2, 3
        )
        SELECT store_key, channel,
               COUNT(*) AS first_timers,
               AVG(CASE WHEN repeats > 0 THEN 1.0 ELSE 0.0 END) AS repeat_rate,
               AVG(repeats) AS avg_later_baskets
        FROM later GROUP BY 1, 2
    """).df()

    stick_chain = src.execute(f"""
        WITH firsts AS (
          SELECT customer_key, channel, MIN(date_key) AS first_dk
          FROM fact_basket
          WHERE NOT is_return AND customer_key IS NOT NULL
            AND channel <> 'In-Store'
          GROUP BY 1, 2
        ),
        later AS (
          SELECT f.channel, f.customer_key, COUNT(b.basket_id) AS repeats
          FROM firsts f
          LEFT JOIN fact_basket b
            ON  b.customer_key = f.customer_key
            AND b.channel      = f.channel
            AND b.date_key     > f.first_dk
            AND NOT b.is_return
          WHERE f.first_dk BETWEEN {cut_back} AND {cut}
          GROUP BY 1, 2
        )
        SELECT 0 AS store_key, channel,
               COUNT(*) AS first_timers,
               AVG(CASE WHEN repeats > 0 THEN 1.0 ELSE 0.0 END) AS repeat_rate,
               AVG(repeats) AS avg_later_baskets
        FROM later GROUP BY 1, 2
    """).df()

    stick = pd.concat([stick_chain, stick_store], ignore_index=True)
    print(f"  dash_channel_stick  {len(stick):>6,} rows")

    src.close()

    # ------------------------------------------------------------- write
    dst = duckdb.connect(str(DST))
    dst.execute("CREATE OR REPLACE TABLE dash_channel_dow   AS SELECT * FROM dow")
    dst.execute("CREATE OR REPLACE TABLE dash_channel_pair  AS SELECT * FROM pair")
    dst.execute("CREATE OR REPLACE TABLE dash_channel_stick AS SELECT * FROM stick")
    dst.close()

    print("written to", DST)

    # A quick read-back so a silent failure cannot look like success.
    rc = check()
    if rc:
        return rc

    if args.upload:
        return upload()

    print("\n  local only -- pass --upload to push this to Drive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
