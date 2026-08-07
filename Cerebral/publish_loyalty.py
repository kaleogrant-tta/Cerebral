"""
publish_loyalty.py -- loyalty tables for the published file.

Called from publish.py's build(), after `ATTACH '<src>' AS src (READ_ONLY)`
and before `DETACH src`. Uses the same single-connection pattern as every
other dash_ table.

    from publish_loyalty import build_loyalty
    ...
    build_loyalty(con)            # just before con.execute("DETACH src")

Requires `src.dim_customer_tier`, written by loyalty_ingest.py. If that table
is absent the function is a no-op, so a refresh never fails just because the
tier ingest has not been run yet.

STORE GRAIN
-----------
Every table carries store_key, plus a chain rollup at store_key = 0 computed
BEFORE small-cell suppression. Always filter to exactly one of the two:
store_key = 0 for all stores, or store_key IN (...) for a subset. Summing
across both double-counts.

SUPPRESSION
-----------
Store-level cells below MIN_CELL distinct customers are dropped, so no
published row describes a group small enough to expose one person's spend.
The chain rollup is never suppressed, so all-store totals stay exact.

PUBLISH.PY CHANGE REQUIRED
--------------------------
Add "tier" and "bin_label" to ALLOWED_TEXT in build()'s identifier guard,
otherwise the guard rejects them as unrecognised text columns.
"""

MIN_CELL = 25

_TIER = "COALESCE(d.tier, 'Non-Loyalty')"


def _src_has(con, table):
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name = 'src' AND table_name = ?", [table]
    ).fetchone()[0] > 0


def build_loyalty(con, min_cell: int = MIN_CELL) -> dict:
    """Create dash_loyalty_* on `con`. Returns {table: rowcount}."""
    if not _src_has(con, "dim_customer_tier"):
        print("  [loyalty] src.dim_customer_tier missing - skipping. "
              "Run loyalty_ingest.py to populate it.")
        return {}

    out = {}

    # --- tier x store x week ---------------------------------------------
    con.execute(f"""
        CREATE TABLE dash_loyalty_week AS
        WITH base AS (
            SELECT b.store_key, b.iso_year, b.iso_week,
                   {_TIER}          AS tier,
                   b.customer_key   AS ck,
                   b.basket_net     AS net,
                   b.loyalty_redeem AS redeem
            FROM src.fact_basket b
            LEFT JOIN src.dim_customer_tier d ON d.customer_key = b.customer_key
            WHERE NOT b.is_return
        ),
        per_store AS (
            SELECT store_key, iso_year, iso_week, tier,
                   COUNT(DISTINCT ck)                          AS customers,
                   COUNT(*)                                    AS baskets,
                   SUM(net)                                    AS net,
                   SUM(redeem)                                 AS redeem_value,
                   SUM(CASE WHEN redeem > 0 THEN 1 ELSE 0 END) AS redeem_baskets
            FROM base GROUP BY 1,2,3,4
            HAVING COUNT(DISTINCT ck) >= {min_cell}
        ),
        chain AS (
            SELECT 0 AS store_key, iso_year, iso_week, tier,
                   COUNT(DISTINCT ck)                          AS customers,
                   COUNT(*)                                    AS baskets,
                   SUM(net)                                    AS net,
                   SUM(redeem)                                 AS redeem_value,
                   SUM(CASE WHEN redeem > 0 THEN 1 ELSE 0 END) AS redeem_baskets
            FROM base GROUP BY 1,2,3,4
        )
        SELECT * FROM chain UNION ALL SELECT * FROM per_store
    """)

    # --- tier x store x channel x week ------------------------------------
    con.execute(f"""
        CREATE TABLE dash_loyalty_channel_week AS
        WITH base AS (
            SELECT b.store_key, b.iso_year, b.iso_week, b.channel,
                   {_TIER} AS tier, b.customer_key AS ck,
                   b.basket_net AS net, b.loyalty_redeem AS redeem
            FROM src.fact_basket b
            LEFT JOIN src.dim_customer_tier d ON d.customer_key = b.customer_key
            WHERE NOT b.is_return
        ),
        per_store AS (
            SELECT store_key, iso_year, iso_week, channel, tier,
                   COUNT(DISTINCT ck) AS customers, COUNT(*) AS baskets,
                   SUM(net) AS net,
                   SUM(CASE WHEN redeem > 0 THEN 1 ELSE 0 END) AS redeem_baskets
            FROM base GROUP BY 1,2,3,4,5
            HAVING COUNT(DISTINCT ck) >= {min_cell}
        ),
        chain AS (
            SELECT 0 AS store_key, iso_year, iso_week, channel, tier,
                   COUNT(DISTINCT ck) AS customers, COUNT(*) AS baskets,
                   SUM(net) AS net,
                   SUM(CASE WHEN redeem > 0 THEN 1 ELSE 0 END) AS redeem_baskets
            FROM base GROUP BY 1,2,3,4,5
        )
        SELECT * FROM chain UNION ALL SELECT * FROM per_store
    """)

    # --- tier x store x category x week -----------------------------------
    con.execute(f"""
        CREATE TABLE dash_loyalty_category_week AS
        WITH base AS (
            SELECT l.store_key, l.iso_year, l.iso_week, l.category,
                   {_TIER} AS tier, l.customer_key AS ck,
                   l.net_sales AS net, l.units AS units
            FROM src.fact_line l
            LEFT JOIN src.dim_customer_tier d ON d.customer_key = l.customer_key
            WHERE NOT l.is_return
        ),
        per_store AS (
            SELECT store_key, iso_year, iso_week, category, tier,
                   COUNT(DISTINCT ck) AS customers,
                   SUM(net) AS net, SUM(units) AS units
            FROM base GROUP BY 1,2,3,4,5
            HAVING COUNT(DISTINCT ck) >= {min_cell}
        ),
        chain AS (
            SELECT 0 AS store_key, iso_year, iso_week, category, tier,
                   COUNT(DISTINCT ck) AS customers,
                   SUM(net) AS net, SUM(units) AS units
            FROM base GROUP BY 1,2,3,4,5
        )
        SELECT * FROM chain UNION ALL SELECT * FROM per_store
    """)

    # --- tier x offer ------------------------------------------------------
    if _src_has(con, "fact_redemption"):
        con.execute(f"""
            CREATE TABLE dash_loyalty_offer AS
            WITH base AS (
                SELECT r.store_key, r.iso_year, r.iso_week, r.offer_name,
                       {_TIER} AS tier, r.customer_key AS ck,
                       r.redeem_amt AS redeem_amt, r.basket_net AS basket_net
                FROM src.fact_redemption r
                LEFT JOIN src.dim_customer_tier d
                       ON d.customer_key = r.customer_key
            ),
            per_store AS (
                SELECT store_key, iso_year, iso_week, offer_name, tier,
                       COUNT(DISTINCT ck) AS customers,
                       COUNT(*) AS redemptions, SUM(redeem_amt) AS redeem_value,
                       AVG(basket_net) AS avg_basket
                FROM base GROUP BY 1,2,3,4,5
                HAVING COUNT(DISTINCT ck) >= {min_cell}
            ),
            chain AS (
                SELECT 0 AS store_key, iso_year, iso_week, offer_name, tier,
                       COUNT(DISTINCT ck) AS customers,
                       COUNT(*) AS redemptions, SUM(redeem_amt) AS redeem_value,
                       AVG(basket_net) AS avg_basket
                FROM base GROUP BY 1,2,3,4,5
            )
            SELECT * FROM chain UNION ALL SELECT * FROM per_store
        """)

    # --- order-value bins x tier (the "Loyalty Base KPIs" table) ----------
    con.execute(f"""
        CREATE TABLE dash_loyalty_bins AS
        WITH base AS (
            SELECT b.store_key, b.iso_year, b.iso_week,
                   {_TIER} AS tier,
                   CASE WHEN b.basket_net >= 125 THEN 'At or above $125'
                        WHEN b.basket_net >= 100 THEN 'Between $100 and $124'
                        ELSE 'Below $100' END AS bin_label,
                   CASE WHEN b.basket_net >= 125 THEN 1
                        WHEN b.basket_net >= 100 THEN 2 ELSE 3 END AS bin_order,
                   b.customer_key AS ck, b.basket_net AS net
            FROM src.fact_basket b
            LEFT JOIN src.dim_customer_tier d ON d.customer_key = b.customer_key
            WHERE NOT b.is_return
        ),
        per_store AS (
            SELECT store_key, iso_year, iso_week, tier, bin_label, bin_order,
                   COUNT(*) AS baskets, SUM(net) AS net,
                   COUNT(DISTINCT ck) AS customers
            FROM base GROUP BY 1,2,3,4,5,6
            HAVING COUNT(DISTINCT ck) >= {min_cell}
        ),
        chain AS (
            SELECT 0 AS store_key, iso_year, iso_week, tier, bin_label,
                   bin_order,
                   COUNT(*) AS baskets, SUM(net) AS net,
                   COUNT(DISTINCT ck) AS customers
            FROM base GROUP BY 1,2,3,4,5,6
        )
        SELECT * FROM chain UNION ALL SELECT * FROM per_store
    """)

    # --- roster health (chain level; a roster has no store) ---------------
    # dim_customer_tier holds one row per POS id AND one per name hash for
    # the same person, linked by person_key. Count people once; treat a person
    # as transacting if EITHER of their keys appears in fact_basket.
    has_pk = con.execute(
        "SELECT COUNT(*) FROM duckdb_columns() WHERE database_name='src' "
        "AND table_name='dim_customer_tier' AND column_name='person_key'"
    ).fetchone()[0] > 0
    if not has_pk:
        print("  [loyalty] dim_customer_tier lacks person_key - roster counts "
              "may be inflated. Re-run loyalty_ingest.py.")
    pk = "person_key" if has_pk else "customer_key"
    con.execute(f"""
        CREATE TABLE dash_loyalty_roster AS
        WITH act AS (SELECT DISTINCT customer_key FROM src.fact_basket),
        seen AS (
            SELECT d.{pk} AS person, ANY_VALUE(d.tier) AS tier,
                   MAX(CASE WHEN a.customer_key IS NOT NULL THEN 1 ELSE 0 END)
                       AS transacted
            FROM src.dim_customer_tier d
            LEFT JOIN act a ON a.customer_key = d.customer_key
            GROUP BY 1
        )
        SELECT tier, COUNT(*) AS roster, SUM(transacted) AS transacted
        FROM seen GROUP BY 1
    """)

    # --- FF enrolment curve (dates only) ----------------------------------
    con.execute("""
        CREATE TABLE dash_loyalty_enrollment AS
        SELECT date_trunc('month', ff_enrolled_at)::DATE AS month,
               COUNT(*) AS enrollments
        FROM src.dim_customer_tier
        WHERE ff_enrolled_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)

    # --- meta --------------------------------------------------------------
    con.execute("""
        CREATE TABLE dash_loyalty_meta AS
        SELECT (SELECT COUNT(DISTINCT b.customer_key)
                  FROM src.fact_basket b
                  JOIN src.dim_customer_tier d
                    ON d.customer_key = b.customer_key)  AS matched_customers,
               (SELECT COUNT(DISTINCT customer_key)
                  FROM src.fact_basket)                  AS transacting_customers,
               (SELECT MAX(built_at) FROM src.dim_customer_tier)
                                                         AS tiers_built_at,
               now()                                     AS built_at
    """)

    for t in ("dash_loyalty_week", "dash_loyalty_channel_week",
              "dash_loyalty_category_week", "dash_loyalty_offer",
              "dash_loyalty_bins",
              "dash_loyalty_roster", "dash_loyalty_enrollment",
              "dash_loyalty_meta"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            continue
        out[t] = n
        print(f"  [loyalty] {t:<32} {n:,} rows")

    return out
