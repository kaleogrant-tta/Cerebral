"""
Build the published dashboard database.

The working database is a few hundred MB and contains customer names,
addresses and per-line transaction detail. None of that should leave your
machine, and none of it is needed to draw a chart.

This distils it to pre-aggregated tables — a few thousand rows, low single-digit
MB — with no customer identifiers of any kind. That file is what gets shared.

    python publish.py                      # build locally
    python publish.py --upload             # build and push to Drive

Run automatically at the end of every scheduled refresh.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

SLIM = "cerebral_dash.duckdb"


def build(src: str, dest: str) -> dict:
    if Path(dest).exists():
        Path(dest).unlink()

    con = duckdb.connect(dest)
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")

    # --- category x store x channel x week -------------------------------
    con.execute("""
        CREATE TABLE dash_category_week AS
        WITH bw AS (
            SELECT store_key, iso_year, iso_week, channel,
                   COUNT(*)                    AS baskets,
                   COUNT(DISTINCT date_key)    AS days_open,
                   SUM(basket_net)             AS net_all,
                   AVG(basket_lines)           AS avg_lines
            FROM src.fact_basket WHERE NOT is_return
            GROUP BY 1,2,3,4
        ),
        cw AS (
            SELECT store_key, iso_year, iso_week, channel, category,
                   SUM(net_sales)              AS net,
                   SUM(gross_margin)           AS gm,
                   SUM(units)                  AS units,
                   COUNT(DISTINCT basket_id)   AS baskets_with
            FROM src.fact_line WHERE NOT is_return
            GROUP BY 1,2,3,4,5
        )
        SELECT cw.*, bw.baskets, bw.days_open, bw.net_all, bw.avg_lines
        FROM cw JOIN bw USING (store_key, iso_year, iso_week, channel)
    """)

    # --- basket totals per store x channel x week ------------------------
    con.execute("""
        CREATE TABLE dash_basket_week AS
        SELECT store_key, iso_year, iso_week, channel,
               COUNT(*)                        AS baskets,
               SUM(basket_net)                 AS net,
               AVG(basket_net)                 AS avg_net,
               AVG(basket_lines)               AS avg_lines,
               SUM(CASE WHEN basket_lines = 1 THEN 1 ELSE 0 END) AS single_line,
               SUM(CASE WHEN used_redemption THEN 1 ELSE 0 END)  AS redeem_baskets,
               SUM(loyalty_redeem)             AS redeem_value,
               COUNT(DISTINCT date_key)        AS days_open
        FROM src.fact_basket WHERE NOT is_return
        GROUP BY 1,2,3,4
    """)

    # --- co-purchase pairs, pre-aggregated -------------------------------
    # Restricted to baskets holding 2+ categories: the unrestricted figure is
    # suppressed by basket size and reads as "everything substitutes
    # everything". Only counts survive — no basket IDs.
    con.execute("""
        CREATE TABLE dash_pairs AS
        WITH b AS (
            SELECT store_key, basket_id, category
            FROM src.fact_line WHERE NOT is_return
            GROUP BY 1,2,3
        ),
        m AS (SELECT store_key, basket_id FROM b GROUP BY 1,2 HAVING COUNT(*) >= 2),
        bm AS (SELECT b.* FROM b JOIN m USING (store_key, basket_id))
        SELECT x.store_key, x.category AS cat_a, y.category AS cat_b,
               COUNT(*) AS joint_baskets
        FROM bm x JOIN bm y
          ON x.store_key = y.store_key AND x.basket_id = y.basket_id
         AND x.category < y.category
        GROUP BY 1,2,3
    """)
    con.execute("""
        CREATE TABLE dash_pair_base AS
        WITH b AS (
            SELECT store_key, basket_id, category
            FROM src.fact_line WHERE NOT is_return
            GROUP BY 1,2,3
        ),
        m AS (SELECT store_key, basket_id FROM b GROUP BY 1,2 HAVING COUNT(*) >= 2),
        bm AS (SELECT b.* FROM b JOIN m USING (store_key, basket_id))
        SELECT store_key, category, COUNT(DISTINCT basket_id) AS cat_baskets,
               (SELECT COUNT(*) FROM m m2 WHERE m2.store_key = bm.store_key)
                   AS multi_baskets
        FROM bm GROUP BY store_key, category
    """)

    # --- brand-level pairs within each category pair ----------------------
    # Category-level lift says two categories substitute. Procurement needs to
    # know WHICH brands drive it. Brand and product names are not personal
    # data, so they can be published; basket IDs are aggregated away.
    con.execute("""
        CREATE TABLE dash_brand_pairs AS
        WITH b AS (
            SELECT store_key, basket_id, category, brand
            FROM src.fact_line
            WHERE NOT is_return AND brand IS NOT NULL
            GROUP BY 1,2,3,4
        ),
        m AS (SELECT store_key, basket_id FROM b GROUP BY 1,2 HAVING COUNT(*) >= 2),
        bm AS (SELECT b.* FROM b JOIN m USING (store_key, basket_id))
        SELECT x.store_key,
               x.category AS cat_a, y.category AS cat_b,
               x.brand    AS brand_a, y.brand AS brand_b,
               COUNT(*)   AS joint_baskets
        FROM bm x JOIN bm y
          ON x.store_key = y.store_key AND x.basket_id = y.basket_id
         AND x.category < y.category
        GROUP BY 1,2,3,4,5
        HAVING COUNT(*) >= 15
    """)

    # --- brand trend: first half vs second half of the window -------------
    con.execute("""
        CREATE TABLE dash_brand_trend AS
        WITH span AS (
            SELECT MIN(txn_ts) AS t0, MAX(txn_ts) AS t1 FROM src.fact_line
        ),
        halves AS (
            SELECT l.store_key, l.category, l.brand,
                   CASE WHEN l.txn_ts < s.t0 + (s.t1 - s.t0)/2
                        THEN 'early' ELSE 'late' END AS half,
                   SUM(l.net_sales) AS net,
                   SUM(l.units)     AS units,
                   COUNT(DISTINCT l.basket_id) AS baskets
            FROM src.fact_line l CROSS JOIN span s
            WHERE NOT l.is_return AND l.brand IS NOT NULL
            GROUP BY 1,2,3,4
        )
        SELECT store_key, category, brand,
               SUM(CASE WHEN half='early' THEN net   ELSE 0 END) AS net_early,
               SUM(CASE WHEN half='late'  THEN net   ELSE 0 END) AS net_late,
               SUM(CASE WHEN half='early' THEN units ELSE 0 END) AS units_early,
               SUM(CASE WHEN half='late'  THEN units ELSE 0 END) AS units_late,
               SUM(net) AS net_total
        FROM halves GROUP BY 1,2,3
        HAVING SUM(net) >= 2000
    """)

    # --- product trend, top sellers per category --------------------------
    con.execute("""
        CREATE TABLE dash_product_trend AS
        WITH span AS (SELECT MIN(txn_ts) t0, MAX(txn_ts) t1 FROM src.fact_line),
        h AS (
            SELECT l.store_key, l.category, l.brand, l.product,
                   CASE WHEN l.txn_ts < s.t0 + (s.t1 - s.t0)/2
                        THEN 'early' ELSE 'late' END AS half,
                   SUM(l.net_sales) AS net, SUM(l.units) AS units
            FROM src.fact_line l CROSS JOIN span s
            WHERE NOT l.is_return AND l.product IS NOT NULL
            GROUP BY 1,2,3,4,5
        ),
        agg AS (
            SELECT store_key, category, brand, product,
                   SUM(CASE WHEN half='early' THEN net   ELSE 0 END) AS net_early,
                   SUM(CASE WHEN half='late'  THEN net   ELSE 0 END) AS net_late,
                   SUM(CASE WHEN half='early' THEN units ELSE 0 END) AS units_early,
                   SUM(CASE WHEN half='late'  THEN units ELSE 0 END) AS units_late,
                   SUM(net) AS net_total
            FROM h GROUP BY 1,2,3,4
        )
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY store_key, category ORDER BY net_total DESC) AS rk
            FROM agg
        ) WHERE rk <= 25 AND net_total >= 1000
    """)

    # --- brand scorecard --------------------------------------------------
    # Which brands bring customers IN versus which are bought by people who
    # were coming anyway. That distinction is what the 3P tier conversation
    # needs and what a sales ranking cannot show.
    con.execute("""
        CREATE TABLE dash_brand_scorecard AS
        WITH firsts AS (
            SELECT customer_key, MIN(txn_ts) AS first_ts
            FROM src.fact_basket
            WHERE NOT is_return AND customer_key IS NOT NULL
            GROUP BY 1
        ),
        tagged AS (
            SELECT l.store_key, l.category, l.brand, l.customer_key,
                   l.basket_id, l.net_sales, l.gross_margin, l.units,
                   l.product,
                   date_diff('day', f.first_ts, l.txn_ts) AS age_days
            FROM src.fact_line l
            LEFT JOIN firsts f USING (customer_key)
            WHERE NOT l.is_return AND l.brand IS NOT NULL
        )
        SELECT store_key, brand,
               MIN(category)                        AS primary_category,
               COUNT(DISTINCT category)             AS categories,
               COUNT(DISTINCT product)              AS skus,
               SUM(net_sales)                       AS net,
               SUM(gross_margin)                    AS gm,
               SUM(units)                           AS units,
               COUNT(DISTINCT basket_id)            AS baskets,
               COUNT(DISTINCT customer_key)         AS customers,
               COUNT(DISTINCT CASE WHEN age_days = 0 THEN customer_key END)
                                                    AS first_basket_customers,
               COUNT(DISTINCT CASE WHEN age_days > 90 THEN customer_key END)
                                                    AS established_customers
        FROM tagged
        GROUP BY 1,2
        HAVING SUM(net_sales) >= 1000
    """)

    # Chain totals, so a brand's share of new customers can be computed.
    con.execute("""
        CREATE TABLE dash_customer_totals AS
        WITH f AS (
            SELECT customer_key, MIN(txn_ts) AS first_ts
            FROM src.fact_basket WHERE NOT is_return AND customer_key IS NOT NULL
            GROUP BY 1
        )
        SELECT COUNT(*) AS total_customers,
               COUNT(*) FILTER (WHERE first_ts >=
                   (SELECT MAX(txn_ts) - INTERVAL 180 DAY FROM src.fact_line))
                   AS new_last_180d
        FROM f
    """)

    # --- brand redemption --------------------------------------------------
    # What the loyalty programme actually spends per brand, and on whom. This
    # is the 3P Reward Program conversation: a brand can be shown what its
    # offers cost, who redeemed them, and whether those were new customers.
    con.execute("""
        CREATE TABLE dash_brand_redemption AS
        WITH f AS (
            SELECT customer_key, MIN(txn_ts) AS first_ts
            FROM src.fact_basket WHERE NOT is_return AND customer_key IS NOT NULL
            GROUP BY 1
        )
        SELECT r.store_key,
               r.matched_brand           AS brand,
               r.matched_category        AS category,
               r.match_method,
               COUNT(*)                              AS redemptions,
               SUM(r.redeem_amt)                     AS redeem_value,
               AVG(r.redeem_amt)                     AS avg_redeem,
               AVG(r.basket_net)                     AS avg_basket,
               COUNT(DISTINCT r.customer_key)        AS redeemers,
               COUNT(DISTINCT CASE
                     WHEN date_diff('day', f.first_ts, r.txn_ts) = 0
                     THEN r.customer_key END)        AS first_visit_redeemers,
               COUNT(DISTINCT CASE
                     WHEN date_diff('day', f.first_ts, r.txn_ts) > 90
                     THEN r.customer_key END)        AS established_redeemers
        FROM src.fact_redemption r
        LEFT JOIN f USING (customer_key)
        GROUP BY 1,2,3,4
    """)

    # Offer-level detail, so a specific campaign can be looked up.
    con.execute("""
        CREATE TABLE dash_offer_performance AS
        SELECT store_key, offer_name, matched_brand AS brand,
               matched_category AS category, match_method,
               COUNT(*)                        AS redemptions,
               SUM(redeem_amt)                 AS redeem_value,
               AVG(basket_net)                 AS avg_basket,
               MIN(txn_ts)                     AS first_seen,
               MAX(txn_ts)                     AS last_seen
        FROM src.fact_redemption
        GROUP BY 1,2,3,4,5
        HAVING COUNT(*) >= 5
    """)

    # --- inventory, most recent snapshot only ----------------------------
    con.execute("""
        CREATE TABLE dash_inventory AS
        SELECT store_key, category,
               SUM(ext_cost)            AS inv_cost,
               SUM(ext_retail)          AS inv_retail,
               SUM(qty_on_hand)         AS qoh,
               COUNT(DISTINCT product)  AS skus,
               snapshot_date
        FROM src.fact_inventory
        WHERE sellable
          AND snapshot_date = (SELECT MAX(snapshot_date) FROM src.fact_inventory)
        GROUP BY store_key, category, snapshot_date
    """)

    # --- load log and metadata -------------------------------------------
    con.execute("""
        CREATE TABLE dash_load_log AS
        SELECT period, store_key, lines, baskets, warnings,
               config_version, loaded_at
        FROM src.load_log
    """)
    con.execute("""
        CREATE TABLE dash_meta AS
        SELECT COUNT(*)                     AS n_lines,
               COUNT(DISTINCT basket_id)    AS n_baskets,
               MIN(txn_ts)                  AS first_txn,
               MAX(txn_ts)                  AS last_txn,
               now()                        AS built_at
        FROM src.fact_line WHERE NOT is_return
    """)

    con.execute("DETACH src")

    # --- confirm no identifiers survived ---------------------------------
    # Checks what a column HOLDS, not what it is called. A count of customers
    # is fine; a customer key is not. Name-substring matching flagged
    # "first_basket_customers" and would keep doing so as tables are added,
    # which trains you to ignore the check.
    ALLOWED_TEXT = {"category", "raw_category", "brand", "product", "channel",
                    "cat_a", "cat_b", "brand_a", "brand_b", "period",
                    "config_version", "room", "primary_category",
                    "match_method", "offer_name"}
    leaked = []
    for (t,) in con.execute("SHOW TABLES").fetchall():
        info = con.execute(f"PRAGMA table_info('{t}')").fetchall()
        for row in info:
            col, typ = row[1], str(row[2]).upper()
            if col in ALLOWED_TEXT:
                continue
            if col.lower() in ("basket_id", "customer_key", "patient",
                               "patientname", "display_name", "name_hash",
                               "alpine_id", "address", "phone"):
                leaked.append(f"{t}.{col}  (identifier column)")
                continue
            # Any other free-text column is suspect: numeric aggregates are
            # safe by construction, text columns could carry a person.
            if "VARCHAR" in typ or "TEXT" in typ:
                leaked.append(f"{t}.{col}  (unrecognised text column — add it "
                              f"to ALLOWED_TEXT if it is not personal data)")
    if leaked:
        raise RuntimeError("Published file may contain personal data:\n  "
                           + "\n  ".join(leaked))

    stats = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for (t,) in con.execute("SHOW TABLES").fetchall()}
    con.close()
    stats["_size_mb"] = round(Path(dest).stat().st_size / 1e6, 2)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tta.duckdb")
    ap.add_argument("--out", default=SLIM)
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"No source database at {args.db}")
        return 1

    print(f"\nBuilding {args.out} from {args.db}")
    stats = build(args.db, args.out)
    size = stats.pop("_size_mb")
    for t, n in stats.items():
        print(f"  {t:<24}{n:>9,} rows")
    src_mb = Path(args.db).stat().st_size / 1e6
    print(f"\n  {size} MB  (from {src_mb:,.0f} MB — {size/src_mb*100:.1f}%)")
    print("  no customer, name, address, phone or basket identifiers included")

    if args.upload:
        from tta_env import bootstrap
        from tta_drive import DriveClient
        bootstrap()
        folder = os.environ.get("TTA_DRIVE_STATE")
        if not folder:
            print("  TTA_DRIVE_STATE not set — skipping upload")
            return 1
        print("\n  uploading…")
        DriveClient().upload(Path(args.out), folder)
        print(f"  {args.out} pushed to Drive")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
