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

from publish_events import build_events

from publish_retention import build_retention

from publish_loyalty import build_loyalty

from publish_newret import build_newret
from publish_event_return import build_event_return

SLIM = "cerebral_dash.duckdb"

# Products with less than this in lifetime net sales are left out of the
# week-level product table. Without a floor that table is every SKU the chain
# has ever sold times every week times every store, which is most of the file
# size for rows nobody looks at.
PRODUCT_WEEK_MIN_NET = 250


# --------------------------------------------------------------- brand names
# The same brand arrives under more than one spelling depending on which
# store keyed the PO and which menu version Dutchie was on. Left alone they
# rank as separate brands, each below the reporting floor, and the combined
# business is invisible.
#
# Keys are matched after lowercasing, collapsing runs of whitespace, and
# stripping trailing punctuation — so "RUBY FARMS", "Ruby  Farms" and
# "Ruby Farms." all land on the same key. Everything else is left exactly as
# it came in: this is an explicit list, not fuzzy matching, because a wrong
# merge is silent and very hard to spot downstream.
BRAND_ALIASES = {
    "ruby": "Ruby",
    "ruby farms": "Ruby",
}


def _brand_key_sql(col: str) -> str:
    """Normalised lookup key for a brand column."""
    return (f"regexp_replace(regexp_replace(lower(trim({col})), "
            f"'\\s+', ' ', 'g'), '[.,]+$', '')")


def _canon_brand_sql(col: str) -> str:
    """CASE expression rewriting a brand column to its canonical name."""
    if not BRAND_ALIASES:
        return col
    key = _brand_key_sql(col)
    whens = "\n".join(
        f"        WHEN {key} = '{alias.replace(chr(39), chr(39) * 2)}' "
        f"THEN '{canon.replace(chr(39), chr(39) * 2)}'"
        for alias, canon in sorted(BRAND_ALIASES.items()))
    return f"CASE\n{whens}\n        ELSE {col}\n    END"


# Promo families that have no brand of their own but are worth tracking as
# their own line items: the April "Secret Drop" mystery promos and the
# Travel Club point-substitution tiers (staff swap an out-of-stock menu item
# for something else). Family takes precedence over the matched brand in the
# rollup tables, so they appear as their own rows; the offer-level table
# still carries the actual product the customer received.
_FAMILY_RE = "[0-9]+ *points? +substitution"

def _family_sql(offer_col: str, brand_col: str) -> str:
    return f"""CASE
        WHEN lower({offer_col}) LIKE '%secret drop%' THEN 'Secret Drops'
        WHEN regexp_matches(lower({offer_col}), '{_FAMILY_RE}')
        THEN 'Travel Club Substitution — ' ||
             regexp_extract(lower({offer_col}), '([0-9]+) *points? +substitution', 1) ||
             ' pts'
        ELSE {brand_col}
    END"""


def build(src: str, dest: str) -> dict:
    """Build the published file atomically.

    Writes to a temporary path and only replaces the destination once every
    table exists. A crash midway used to leave a partial file where the
    dashboard looks, which then failed with a confusing missing-table error
    while the previous good copy was already gone.
    """
    dest_p = Path(dest)
    tmp = dest_p.with_suffix(dest_p.suffix + ".building")
    if tmp.exists():
        tmp.unlink()

    con = duckdb.connect(str(tmp))
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")

    # --- canonical brand names -------------------------------------------
    # Every rollup below reads `fl` rather than src.fact_line, so brand
    # consolidation happens once, before any grouping. Doing it per-table
    # would mean any table added later quietly misses it.
    con.execute(f"""
        CREATE VIEW fl AS
        SELECT * REPLACE ({_canon_brand_sql('brand')} AS brand)
        FROM src.fact_line
    """)

    has_redemption = con.execute("""
        SELECT COUNT(*) FROM duckdb_tables()
        WHERE database_name = 'src' AND table_name = 'fact_redemption'
    """).fetchone()[0] > 0
    if has_redemption:
        con.execute(f"""
            CREATE VIEW fr AS
            SELECT * REPLACE ({_canon_brand_sql('matched_brand')}
                              AS matched_brand)
            FROM src.fact_redemption
        """)

    # What actually got merged, so the dashboard can say so out loud rather
    # than silently showing one brand where the POS shows two.
    con.execute("""
        CREATE TABLE dash_brand_alias (
            alias VARCHAR, canonical VARCHAR, lines BIGINT, net DOUBLE)
    """)
    if BRAND_ALIASES:
        con.execute(f"""
            INSERT INTO dash_brand_alias
            SELECT brand                       AS alias,
                   {_canon_brand_sql('brand')} AS canonical,
                   COUNT(*)                    AS lines,
                   SUM(net_sales)              AS net
            FROM src.fact_line          -- raw, pre-canonicalisation
            WHERE NOT is_return AND brand IS NOT NULL
              AND {_brand_key_sql('brand')} IN (
                  {','.join("'" + a.replace("'", "''") + "'"
                            for a in sorted(BRAND_ALIASES))})
            GROUP BY 1, 2
        """)

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
            FROM fl WHERE NOT is_return
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
            FROM fl WHERE NOT is_return
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
            FROM fl WHERE NOT is_return
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
            FROM fl
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
            SELECT MIN(txn_ts) AS t0, MAX(txn_ts) AS t1 FROM fl
        ),
        halves AS (
            SELECT l.store_key, l.category, l.brand,
                   CASE WHEN l.txn_ts < s.t0 + (s.t1 - s.t0)/2
                        THEN 'early' ELSE 'late' END AS half,
                   SUM(l.net_sales) AS net,
                   SUM(l.units)     AS units,
                   COUNT(DISTINCT l.basket_id) AS baskets
            FROM fl l CROSS JOIN span s
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
        WITH span AS (SELECT MIN(txn_ts) t0, MAX(txn_ts) t1 FROM fl),
        h AS (
            SELECT l.store_key, l.category, l.brand, l.product,
                   CASE WHEN l.txn_ts < s.t0 + (s.t1 - s.t0)/2
                        THEN 'early' ELSE 'late' END AS half,
                   SUM(l.net_sales) AS net, SUM(l.units) AS units
            FROM fl l CROSS JOIN span s
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

    # --- accessory products per store x week -------------------------------
    # Feeds the Accessories tab: the category rollup can't say how many SKUs
    # sold exactly one unit in a week, only product-level rows can. Kept to
    # the Accessory category so the table stays a few thousand rows.
    con.execute("""
        CREATE TABLE dash_acc_product_week AS
        SELECT store_key, iso_year, iso_week, product,
               SUM(units)     AS units,
               SUM(net_sales) AS net
        FROM fl
        WHERE NOT is_return
          AND category ILIKE 'Accessor%'
          AND product IS NOT NULL
        GROUP BY 1,2,3,4
    """)

    # --- brand x week ------------------------------------------------------
    # The scorecard and trend tables are whole-file rollups: they cannot
    # answer "how did this brand do in the last eight weeks" because the
    # period is baked in at build time. This carries the week so the
    # dashboard's window control can actually move the numbers.
    #
    # first_basket_customers is safe to sum across weeks — a customer has
    # exactly one first basket, so they appear in one week and one only.
    # established_customers is NOT: someone buying in three weeks counts
    # three times. It is published for completeness and the dashboard uses
    # it only on the full window, where it matches the scorecard.
    con.execute("""
        CREATE TABLE dash_brand_week AS
        WITH firsts AS (
            SELECT customer_key, MIN(txn_ts) AS first_ts
            FROM src.fact_basket
            WHERE NOT is_return AND customer_key IS NOT NULL
            GROUP BY 1
        ),
        tagged AS (
            SELECT l.store_key, l.iso_year, l.iso_week, l.brand, l.category,
                   l.customer_key, l.basket_id, l.net_sales, l.gross_margin,
                   l.units,
                   date_diff('day', f.first_ts, l.txn_ts) AS age_days
            FROM fl l
            LEFT JOIN firsts f USING (customer_key)
            WHERE NOT l.is_return AND l.brand IS NOT NULL
        )
        SELECT store_key, iso_year, iso_week, brand, category,
               SUM(net_sales)              AS net,
               SUM(gross_margin)           AS gm,
               SUM(units)                  AS units,
               COUNT(DISTINCT basket_id)   AS baskets,
               COUNT(DISTINCT CASE WHEN age_days = 0 THEN customer_key END)
                   AS first_basket_customers,
               COUNT(DISTINCT CASE WHEN age_days > 90 THEN customer_key END)
                   AS established_customers
        FROM tagged
        GROUP BY 1,2,3,4,5
    """)

    # --- brand x category x product x week ---------------------------------
    # Feeds "pick a brand, pick a category, see its SKUs" and lets that list
    # respond to the window control. The floor is applied on a product's
    # lifetime total, not per week, so a real SKU keeps its slow weeks
    # instead of appearing to have gaps in its sales history.
    con.execute(f"""
        CREATE TABLE dash_brand_product_week AS
        WITH keep AS (
            SELECT brand, category, product
            FROM fl
            WHERE NOT is_return AND brand IS NOT NULL AND product IS NOT NULL
            GROUP BY 1,2,3
            HAVING SUM(net_sales) >= {PRODUCT_WEEK_MIN_NET}
        )
        SELECT l.store_key, l.iso_year, l.iso_week,
               l.brand, l.category, l.product,
               SUM(l.net_sales)             AS net,
               SUM(l.gross_margin)          AS gm,
               SUM(l.units)                 AS units,
               COUNT(DISTINCT l.basket_id)  AS baskets
        FROM fl l
        JOIN keep k USING (brand, category, product)
        WHERE NOT l.is_return
        GROUP BY 1,2,3,4,5,6
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
            FROM fl l
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
                   (SELECT MAX(txn_ts) - INTERVAL 180 DAY FROM fl))
                   AS new_last_180d
        FROM f
    """)

    # --- brand redemption --------------------------------------------------
    # What the loyalty programme actually spends per brand, and on whom. This
    # is the 3P Reward Program conversation: a brand can be shown what its
    # offers cost, who redeemed them, and whether those were new customers.
    # has_redemption was resolved above, where the canonical-brand view over
    # the table is created.
    if not has_redemption:
        # Periods loaded before redemption attribution existed have no such
        # table. Create the shape so the dashboard finds it, and leave it
        # empty rather than failing the whole build.
        con.execute("""
            CREATE TABLE dash_brand_redemption (
                store_key INTEGER, brand VARCHAR, category VARCHAR,
                match_method VARCHAR, redemptions BIGINT, redeem_value DOUBLE,
                avg_redeem DOUBLE, avg_basket DOUBLE, redeemers BIGINT,
                first_visit_redeemers BIGINT, established_redeemers BIGINT)
        """)
        con.execute("""
            CREATE TABLE dash_offer_performance (
                store_key INTEGER, offer_name VARCHAR, product VARCHAR,
                brand VARCHAR, category VARCHAR, match_method VARCHAR,
                redemptions BIGINT, redeem_value DOUBLE, avg_basket DOUBLE,
                first_seen TIMESTAMP, last_seen TIMESTAMP)
        """)
        con.execute("""
            CREATE TABLE dash_redemption_day (
                store_key INTEGER, day DATE, brand VARCHAR, channel VARCHAR,
                redemptions BIGINT, redeem_value DOUBLE)
        """)
        print("  ! source has no fact_redemption — reload history to populate "
              "brand redemption. Empty tables created.")
    else:
      con.execute(f"""
        CREATE TABLE dash_brand_redemption AS
        WITH f AS (
            SELECT customer_key, MIN(txn_ts) AS first_ts
            FROM src.fact_basket WHERE NOT is_return AND customer_key IS NOT NULL
            GROUP BY 1
        )
        SELECT r.store_key,
               {_family_sql('r.offer_name', 'r.matched_brand')} AS brand,
               CASE WHEN lower(r.offer_name) LIKE '%secret drop%'
                      OR regexp_matches(lower(r.offer_name), '{_FAMILY_RE}')
                    THEN 'Promo' ELSE r.matched_category END   AS category,
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
        FROM fr r
        LEFT JOIN f USING (customer_key)
        GROUP BY 1,2,3,4
      """)

      # Offer-level detail, so a specific campaign can be looked up. No
      # minimum-row filter: the brand -> SKU drill-down needs every row, and
      # a substitution offer split across many substitute products would
      # otherwise drop below any threshold and vanish from the drill-down.
      con.execute(f"""
        CREATE TABLE dash_offer_performance AS
        SELECT store_key, offer_name, matched_product AS product,
               {_family_sql('offer_name', 'matched_brand')} AS brand,
               matched_category AS category, match_method,
               COUNT(*)                        AS redemptions,
               SUM(redeem_amt)                 AS redeem_value,
               AVG(basket_net)                 AS avg_basket,
               MIN(txn_ts)                     AS first_seen,
               MAX(txn_ts)                     AS last_seen
        FROM fr
        GROUP BY 1,2,3,4,5,6
      """)

      # Redemptions per brand per day. The offer tables above cover the whole
      # loaded period, which cannot answer "how many GWP redemptions happened
      # during this takeover window" — this one can.
      con.execute(f"""
        CREATE TABLE dash_redemption_day AS
        SELECT r.store_key,
               CAST(r.txn_ts AS DATE)                          AS day,
               {_family_sql('r.offer_name', 'r.matched_brand')} AS brand,
               r.channel                                       AS channel,
               COUNT(*)                                        AS redemptions,
               SUM(r.redeem_amt)                               AS redeem_value
        FROM fr r
        GROUP BY 1,2,3,4
      """)


    # --- promo lab: privacy-safe churn aggregates --------------------------
    # Per-customer behaviour reduced to counts per store x category / brand.
    # No customer keys, no basket keys - sums and counts only.
    for _name, _dim in [("dash_promo_category", "category"),
                        ("dash_promo_brand", "brand")]:
        con.execute(f"""
            CREATE TABLE {_name} AS
            WITH mx AS (SELECT MAX(txn_ts) AS t1 FROM fl WHERE NOT is_return),
            pc AS (
                SELECT l.store_key, l.{_dim} AS dim, l.customer_key,
                       MAX(l.txn_ts)               AS last_ts,
                       COUNT(DISTINCT l.basket_id) AS n,
                       SUM(l.net_sales)            AS spend,
                       SUM(l.gross_margin)         AS gm
                FROM fl l
                WHERE NOT l.is_return AND l.customer_key IS NOT NULL
                  AND l.{_dim} IS NOT NULL
                GROUP BY 1,2,3
            )
            SELECT store_key, dim AS {_dim},
                   COUNT(*)                AS customers,
                   COUNT(*) FILTER (WHERE n > 1) AS repeat_buyers,
                   SUM(spend)              AS spend_sum,
                   SUM(gm)                 AS gm_sum,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 30) AS churned_30,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 30) AS lapsed_spend_30,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 45) AS churned_45,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 45) AS lapsed_spend_45,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 60) AS churned_60,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 60) AS lapsed_spend_60,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 90) AS churned_90,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 90) AS lapsed_spend_90
            FROM pc GROUP BY 1,2
        """)

    # --- brand x store x day ----------------------------------------------
    # Day-level brand sales for the Takeover tab. Takeover windows (e.g.
    # April 1-15) do not align with the ISO weeks every other table uses, so
    # the weekly tables cannot measure them. Still only sums and counts —
    # no basket keys, no customer keys. The small-brand floor keeps the file
    # slim without dropping any brand a takeover would ever feature.
    con.execute("""
        CREATE TABLE dash_brand_day AS
        WITH big AS (
            -- Brand-level floor, same as the scorecard. Filtering per day
            -- instead would silently erase slow days for smaller brands.
            SELECT brand FROM fl
            WHERE NOT is_return AND brand IS NOT NULL
            GROUP BY 1 HAVING SUM(net_sales) >= 1000
        )
        SELECT CAST(txn_ts AS DATE)             AS day,
               store_key, brand,
               SUM(net_sales)                   AS net,
               SUM(units)                       AS units,
               COUNT(DISTINCT basket_id)        AS baskets,
               SUM(gross_margin)                AS gm
        FROM fl
        WHERE NOT is_return
          AND brand IN (SELECT brand FROM big)
        GROUP BY 1,2,3
    """)

    # --- discounting -------------------------------------------------------
    # Basket-level discount: the money actually taken off at the till. The
    # offer tables above carry net_sales only, so before this there was no
    # published figure for what a discount COST -- only what the customer paid
    # on a discounted line. Sourced from the POS export's DiscountAmt, which
    # the ETL carried but never read until wire_discount.py.
    #
    # This is EVERY till discount: group and employee discounts, manual
    # write-downs, promo codes, and loyalty offers. It overlaps loyalty_redeem
    # rather than excluding it, so the two must never be summed together.
    has_discount = con.execute("""
        SELECT COUNT(*) FROM duckdb_columns()
        WHERE database_name = 'src' AND table_name = 'fact_basket'
          AND column_name = 'discount_amt'
    """).fetchone()[0] > 0

    if not has_discount:
        con.execute("""
            CREATE TABLE dash_discount_day (
                store_key INTEGER, day DATE, channel VARCHAR,
                baskets BIGINT, discounted_baskets BIGINT,
                gross DOUBLE, discount DOUBLE, net DOUBLE, margin DOUBLE,
                loyalty_redeem DOUBLE)
        """)
        con.execute("""
            CREATE TABLE dash_discount_brand (
                store_key INTEGER, brand VARCHAR, category VARCHAR,
                baskets BIGINT, units DOUBLE,
                net DOUBLE, discount DOUBLE, margin DOUBLE)
        """)
        print("  ! source fact_basket has no discount_amt — run "
              "wire_discount steps then backfill_discount.py. "
              "Empty tables created.")
    else:
        con.execute("""
            CREATE TABLE dash_discount_day AS
            SELECT store_key, CAST(txn_ts AS DATE) AS day, channel,
                   COUNT(*)                                   AS baskets,
                   COUNT(*) FILTER (WHERE discount_amt > 0)   AS discounted_baskets,
                   SUM(basket_net + COALESCE(discount_amt,0)) AS gross,
                   SUM(COALESCE(discount_amt, 0))             AS discount,
                   SUM(basket_net)                            AS net,
                   SUM(basket_margin)                         AS margin,
                   SUM(COALESCE(loyalty_redeem, 0))           AS loyalty_redeem
            FROM src.fact_basket
            WHERE NOT is_return
            GROUP BY 1,2,3
        """)

        # Basket discount spread across the basket's lines by net-sales share.
        # APPROXIMATE BY CONSTRUCTION: the till records a discount against the
        # basket, not the line, so a single-brand discount on a mixed basket is
        # smeared across every brand in it. Fine for ranking which brands sit in
        # discounted baskets; not a per-brand cost figure. dash_discount_day has
        # no such caveat. The tab says so.
        con.execute("""
            CREATE TABLE dash_discount_brand AS
            WITH b AS (
                SELECT basket_id, basket_net,
                       COALESCE(discount_amt, 0) AS disc
                FROM src.fact_basket
                WHERE NOT is_return AND COALESCE(discount_amt, 0) > 0
                  AND basket_net > 0
            )
            SELECT l.store_key, l.brand, l.category,
                   COUNT(DISTINCT l.basket_id)        AS baskets,
                   SUM(l.units)                       AS units,
                   SUM(l.net_sales)                   AS net,
                   SUM(b.disc * l.net_sales / b.basket_net) AS discount,
                   SUM(l.gross_margin)                AS margin
            FROM fl l JOIN b ON l.basket_id = b.basket_id
            WHERE NOT l.is_return
            GROUP BY 1,2,3
        """)

    # --- discount groups ---------------------------------------------------
    # Who the "everything else" discount went to, by name. dim_discount_group_
    # member comes from the Customer Discount Group Audit via
    # ingest_discount_groups.py -- the only source that names a discount, since
    # the POS export records an amount and no reason.
    #
    # EVER-MEMBER is the headline: any discounted basket by someone who was
    # ever in the group. 90% of memberships in the audit are an Added with no
    # Removed, so windowing mostly just drops baskets from before the add date
    # while the audit's left-censoring makes those windows untrustworthy
    # anyway. Both are published; `windowed` is the stricter subset.
    has_groups = con.execute("""
        SELECT COUNT(*) FROM duckdb_tables()
        WHERE database_name = 'src'
          AND table_name = 'dim_discount_group_member'
    """).fetchone()[0] > 0

    if not (has_groups and has_discount):
        con.execute("""
            CREATE TABLE dash_discount_group (
                store_key INTEGER, group_name VARCHAR, group_kind VARCHAR,
                members BIGINT, baskets BIGINT, net DOUBLE,
                discount DOUBLE, loyalty DOUBLE, other_discount DOUBLE,
                windowed_baskets BIGINT, windowed_other DOUBLE)
        """)
        if not has_groups:
            print("  ! no dim_discount_group_member — run "
                  "ingest_discount_groups.py. Empty table created.")
    else:
        con.execute("""
            CREATE TABLE dash_discount_group AS
            SELECT b.store_key, m.group_name, m.group_kind,
                   COUNT(DISTINCT m.customer_key)          AS members,
                   COUNT(DISTINCT b.basket_id)             AS baskets,
                   SUM(b.basket_net)                       AS net,
                   SUM(b.discount_amt)                     AS discount,
                   SUM(COALESCE(b.loyalty_redeem, 0))      AS loyalty,
                   SUM(b.discount_amt
                       - COALESCE(b.loyalty_redeem, 0))    AS other_discount,
                   COUNT(DISTINCT CASE
                         WHEN b.txn_ts >= m.first_added
                          AND b.txn_ts <  m.last_removed
                         THEN b.basket_id END)             AS windowed_baskets,
                   SUM(CASE
                       WHEN b.txn_ts >= m.first_added
                        AND b.txn_ts <  m.last_removed
                       THEN b.discount_amt - COALESCE(b.loyalty_redeem, 0)
                       ELSE 0 END)                         AS windowed_other
            FROM src.dim_discount_group_member m
            JOIN src.fact_basket b ON b.customer_key = m.customer_key
            WHERE NOT b.is_return
              AND COALESCE(b.discount_amt, 0) > 0
            GROUP BY 1,2,3
        """)

    # --- GWP receipts + suspect lines --------------------------------------
    # The two sides of GWP reconciliation. dash_gwp_receipt is what came in
    # the door; dash_suspect_lines is sale lines whose "product" is a bare
    # SKU number — the mis-ring symptom, where staff keyed the SKU instead of
    # picking the product. Matching those numbers against the receipt SKUs
    # identifies what the customer actually walked out with.
    has_receipt = con.execute("""
        SELECT COUNT(*) FROM duckdb_tables()
        WHERE database_name = 'src' AND table_name = 'fact_receipt'
    """).fetchone()[0] > 0

    if not has_receipt:
        con.execute("""
            CREATE TABLE dash_gwp_receipt (
                store_key INTEGER, day DATE, brand VARCHAR, product VARCHAR,
                product_sku VARCHAR, units_received DOUBLE)
        """)
    else:
        con.execute("""
            CREATE TABLE dash_gwp_receipt AS
            SELECT store_key, receive_date AS day, brand, product, product_sku,
                   SUM(quantity) AS units_received
            FROM src.fact_receipt
            WHERE is_gwp
            GROUP BY 1,2,3,4,5
        """)

    # Built by concatenation, not .format(): the regex contains literal
    # braces ({4,}) which .format() reads as a replacement field.
    extra = ("\n           OR product IN "
             "(SELECT product_sku FROM src.fact_receipt)") if has_receipt else ""
    con.execute("""
        CREATE TABLE dash_suspect_lines AS
        SELECT store_key, CAST(txn_ts AS DATE) AS day, product,
               COUNT(*)      AS lines,
               SUM(units)    AS units,
               SUM(net_sales) AS net
        FROM fl
        WHERE NOT is_return
          AND (regexp_matches(product, '^[0-9]{4,}$')""" + extra + """)
        GROUP BY 1,2,3
    """)

    # Properly-rung GWP: sale lines on the promo's own "(GWP)" SKU, kept per
    # product so the app can reconcile item by item. This is the count that
    # moves day to day while a takeover is running — the "GWP so far" tile.
    # Channel (In-Store / Non-Stop / Delivery) answers "which method moves
    # the most GWP".
    con.execute("""
        CREATE TABLE dash_gwp_day AS
        SELECT store_key, CAST(txn_ts AS DATE) AS day, brand, product, channel,
               SUM(units)     AS units,
               SUM(net_sales) AS net
        FROM fl
        WHERE NOT is_return
          AND LOWER(product) LIKE '%gwp%'
        GROUP BY 1,2,3,4,5
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

    # Accessory stock at product level, so the dashboard can count SKUs down
    # to their last sellable unit. Same latest-snapshot, sellable-only scope
    # as dash_inventory; quantity is summed across sellable rooms per product.
    con.execute("""
        CREATE TABLE dash_acc_product_inv AS
        SELECT store_key, product,
               SUM(qty_on_hand) AS qoh,
               snapshot_date
        FROM src.fact_inventory
        WHERE sellable
          AND category ILIKE 'Accessor%'
          AND product IS NOT NULL
          AND snapshot_date = (SELECT MAX(snapshot_date) FROM src.fact_inventory)
        GROUP BY store_key, product, snapshot_date
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
        FROM fl WHERE NOT is_return
    """)

    # The canonical-brand views select * from the source, customer keys and
    # all. They must go before DETACH — and before the leak check below,
    # which walks SHOW TABLES and would otherwise flag them.
    con.execute("DROP VIEW IF EXISTS fl")
    con.execute("DROP VIEW IF EXISTS fr")
    # --- loyalty tiers x channel x store ---
    build_loyalty(con)
    # --- retention: cohorts, first three baskets, gaps ---
    build_retention(con)
    # --- events: lift, DiD, offsets ---
    build_events(con)
    # --- new vs returning customers by week ---
    build_newret(con)
    # --- event attendees: 90-day return, new vs regular ---
    build_event_return(con)
    con.execute("DETACH src")

    # --- confirm no identifiers survived ---------------------------------
    # Checks what a column HOLDS, not what it is called. A count of customers
    # is fine; a customer key is not. Name-substring matching flagged
    # "first_basket_customers" and would keep doing so as tables are added,
    # which trains you to ignore the check.
    # product_sku is a stock-keeping number, not a person.
    ALLOWED_TEXT = {"category", "raw_category", "brand", "product", "channel",
                    "cat_a", "cat_b", "brand_a", "brand_b", "period",
                    "config_version", "room", "primary_category",
                    "match_method", "offer_name", "product_sku",
                    "alias", "canonical", "tier", "bin_label", "first_channel", "seq_label", "gap_bucket", "event_name", "event_id", "event_type", "series", "scope", "measure", "group_kind", "group_value", "store_name", "brand_partners", "metric", "id_scope", "segment",
    "group_name", "group_kind",
}
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

    required = {"dash_meta", "dash_category_week", "dash_basket_week",
                "dash_brand_week", "dash_brand_product_week"}
    missing = required - set(stats)
    con.close()
    if missing:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Build incomplete, missing {sorted(missing)}. "
                           f"Destination left untouched.")

    if dest_p.exists():
        dest_p.unlink()
    tmp.replace(dest_p)
    stats["_size_mb"] = round(dest_p.stat().st_size / 1e6, 2)
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
    try:
        stats = build(args.db, args.out)
    except Exception as e:
        Path(args.out + ".building").unlink(missing_ok=True)
        print(f"\n  BUILD FAILED: {type(e).__name__}: {e}")
        print(f"  {args.out} was not modified.")
        return 1
    size = stats.pop("_size_mb")
    for t, n in stats.items():
        print(f"  {t:<24}{n:>9,} rows")
    src_mb = Path(args.db).stat().st_size / 1e6
    print(f"\n  {size} MB  (from {src_mb:,.0f} MB — {size/src_mb*100:.1f}%)")
    print("  no customer, name, address, phone or basket identifiers included")

    # Audience/event cohorts live in their own database and are added after
    # the rebuild, since build() drops and recreates every table.
    try:
        import publish_audiences
        publish_audiences.publish(Path("data/cerebral_audiences.duckdb"),
                                  Path("config/audience_event_mapping.xlsx"),
                                  Path(args.out))
        print("  audience tables added")
    except Exception as e:
        print(f"  audience tables skipped: {type(e).__name__}: {e}")

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
