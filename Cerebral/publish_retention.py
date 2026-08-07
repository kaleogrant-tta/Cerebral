"""
publish_retention.py -- retention tables for the published file.

Called from publish.py's build(), after `ATTACH '<src>' AS src (READ_ONLY)`
and before `DETACH src`.

    from publish_retention import build_retention
    ...
    build_retention(con)

SCOPE -- identity-resolved customers only
-----------------------------------------
Retention is inherently cross-period: it asks whether the SAME person came
back. tta_etl line 230 warns that name_hash is a within-period fallback and
explicitly not for cross-period retention, because common names collapse
several people into one key.

After the crosswalk rebuild, 44,636 of 222,688 customers still key on a name
hash (~20%) and those are the ambiguous ones the crosswalk correctly refused
to resolve. Including them would invent repeat customers: two people called
John Smith read as one person who came back.

So every table here excludes customer_key LIKE 'H%'. The tab says so.

RIGHT-CENSORING
---------------
Someone first seen last month cannot have a twelve-month lifespan. Cohort
tables carry days_observed so the tab can hold maturity constant, and
lifespan/frequency are only meaningful for cohorts old enough to have run.

PUBLISH.PY CHANGE REQUIRED
--------------------------
Add "first_channel", "seq_label" and "gap_bucket" to ALLOWED_TEXT.
"""

MIN_CELL = 25

# Excludes ambiguous name-hash identities from every retention measure.
RESOLVED = "b.customer_key NOT LIKE 'H%'"


def _src_has(con, table):
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name = 'src' AND table_name = ?", [table]
    ).fetchone()[0] > 0


def build_retention(con, min_cell: int = MIN_CELL) -> dict:
    """Create dash_retention_* on `con`. Returns {table: rowcount}."""
    if not _src_has(con, "fact_basket"):
        print("  [retention] src.fact_basket missing - skipping.")
        return {}

    out = {}

    # ---- per-customer spine ------------------------------------------
    # One row per resolved customer: their order sequence, first channel,
    # lifespan and totals. Never published; the aggregates below are.
    con.execute(f"""
        CREATE TEMP TABLE _cust_orders AS
        SELECT b.customer_key,
               b.basket_id,
               b.txn_ts,
               b.channel,
               b.store_key,
               b.basket_net,
               ROW_NUMBER() OVER (PARTITION BY b.customer_key
                                  ORDER BY b.txn_ts, b.basket_id) AS seq,
               LAG(b.txn_ts) OVER (PARTITION BY b.customer_key
                                   ORDER BY b.txn_ts, b.basket_id) AS prev_ts
        FROM src.fact_basket b
        WHERE NOT b.is_return AND {RESOLVED}
    """)

    con.execute("""
        CREATE TEMP TABLE _cust AS
        SELECT customer_key,
               MIN(txn_ts)                                   AS first_ts,
               MAX(txn_ts)                                   AS last_ts,
               COUNT(*)                                      AS orders,
               SUM(basket_net)                               AS revenue,
               ANY_VALUE(CASE WHEN seq = 1 THEN channel END) AS first_channel,
               ANY_VALUE(CASE WHEN seq = 1 THEN store_key END) AS first_store,
               date_trunc('month',
                   MIN(txn_ts))::DATE                        AS cohort_month,
               date_diff('day', MIN(txn_ts), MAX(txn_ts))    AS lifespan_days
        FROM _cust_orders GROUP BY 1
    """)

    # ---- cohort x first channel x first store ------------------------
    con.execute(f"""
        CREATE TABLE dash_retention_cohort AS
        WITH obs AS (SELECT MAX(txn_ts) AS asof FROM _cust_orders),
        base AS (
            SELECT c.cohort_month, c.first_channel, c.first_store,
                   c.customer_key, c.orders, c.revenue, c.lifespan_days,
                   date_diff('day', c.first_ts, o.asof) AS days_observed
            FROM _cust c CROSS JOIN obs o
        ),
        per_store AS (
            SELECT cohort_month, first_channel, first_store,
                   COUNT(*)                                     AS customers,
                   SUM(orders)                                  AS orders,
                   SUM(revenue)                                 AS revenue,
                   SUM(CASE WHEN orders >= 2 THEN 1 ELSE 0 END) AS repeaters,
                   SUM(CASE WHEN orders >= 5 THEN 1 ELSE 0 END) AS sticky,
                   SUM(lifespan_days)                           AS lifespan_days,
                   AVG(days_observed)                           AS days_observed
            FROM base GROUP BY 1,2,3
            HAVING COUNT(*) >= {min_cell}
        ),
        chain AS (
            SELECT cohort_month, first_channel, 0 AS first_store,
                   COUNT(*)                                     AS customers,
                   SUM(orders)                                  AS orders,
                   SUM(revenue)                                 AS revenue,
                   SUM(CASE WHEN orders >= 2 THEN 1 ELSE 0 END) AS repeaters,
                   SUM(CASE WHEN orders >= 5 THEN 1 ELSE 0 END) AS sticky,
                   SUM(lifespan_days)                           AS lifespan_days,
                   AVG(days_observed)                           AS days_observed
            FROM base GROUP BY 1,2,3
        )
        SELECT * FROM chain UNION ALL SELECT * FROM per_store
    """)

    # ---- basket 1 / 2 / 3 --------------------------------------------
    con.execute(f"""
        CREATE TABLE dash_retention_sequence AS
        WITH s AS (
            SELECT c.first_channel,
                   o.seq,
                   o.basket_net,
                   date_diff('day', o.prev_ts, o.txn_ts) AS days_since_prev
            FROM _cust_orders o
            JOIN _cust c USING (customer_key)
            WHERE o.seq <= 3
        )
        SELECT first_channel,
               seq,
               'Basket ' || seq          AS seq_label,
               COUNT(*)                  AS customers,
               AVG(basket_net)           AS avg_value,
               MEDIAN(basket_net)        AS median_value,
               SUM(basket_net)           AS revenue,
               AVG(days_since_prev)      AS avg_days_since_prev,
               MEDIAN(days_since_prev)   AS median_days_since_prev
        FROM s GROUP BY 1,2,3
        HAVING COUNT(*) >= {min_cell}
    """)

    # ---- days-until-next-order distribution --------------------------
    con.execute(f"""
        CREATE TABLE dash_retention_gaps AS
        WITH g AS (
            SELECT c.first_channel,
                   date_diff('day', o.prev_ts, o.txn_ts) AS gap
            FROM _cust_orders o
            JOIN _cust c USING (customer_key)
            WHERE o.prev_ts IS NOT NULL
        )
        SELECT first_channel,
               CASE WHEN gap <= 7   THEN '0-7 days'
                    WHEN gap <= 14  THEN '8-14 days'
                    WHEN gap <= 30  THEN '15-30 days'
                    WHEN gap <= 60  THEN '31-60 days'
                    WHEN gap <= 90  THEN '61-90 days'
                    ELSE '90+ days' END       AS gap_bucket,
               CASE WHEN gap <= 7 THEN 1 WHEN gap <= 14 THEN 2
                    WHEN gap <= 30 THEN 3 WHEN gap <= 60 THEN 4
                    WHEN gap <= 90 THEN 5 ELSE 6 END AS bucket_order,
               COUNT(*) AS gaps
        FROM g GROUP BY 1,2,3
        HAVING COUNT(*) >= {min_cell}
    """)

    # ---- headline per first channel ----------------------------------
    con.execute(f"""
        CREATE TABLE dash_retention_summary AS
        WITH q AS (
            SELECT c.first_channel,
                   QUANTILE_CONT(g.gap, 0.25) AS gap_p25,
                   QUANTILE_CONT(g.gap, 0.50) AS gap_p50,
                   QUANTILE_CONT(g.gap, 0.75) AS gap_p75
            FROM _cust c
            JOIN (SELECT o.customer_key,
                         date_diff('day', o.prev_ts, o.txn_ts) AS gap
                  FROM _cust_orders o WHERE o.prev_ts IS NOT NULL) g
              USING (customer_key)
            GROUP BY 1
        )
        SELECT c.first_channel,
               COUNT(*)                                     AS customers,
               SUM(c.orders)                                AS orders,
               SUM(c.revenue)                               AS revenue,
               SUM(CASE WHEN c.orders >= 2 THEN 1 ELSE 0 END) AS repeaters,
               SUM(CASE WHEN c.orders >= 5 THEN 1 ELSE 0 END) AS sticky,
               AVG(c.lifespan_days)                         AS avg_lifespan_days,
               AVG(CASE WHEN c.orders >= 2
                        THEN c.lifespan_days * 1.0 / (c.orders - 1) END)
                                                            AS avg_gap_days,
               ANY_VALUE(q.gap_p25)                         AS gap_p25,
               ANY_VALUE(q.gap_p50)                         AS gap_p50,
               ANY_VALUE(q.gap_p75)                         AS gap_p75
        FROM _cust c LEFT JOIN q USING (first_channel)
        GROUP BY 1
        HAVING COUNT(*) >= {min_cell}
    """)

    # ---- rolling six-month visit frequency -----------------------------
    # For each calendar month, look back six months: how many customers were
    # active, and how many visits did they make? Chain level only -- a
    # customer can visit several stores in six months, so splitting by store
    # would count them more than once.
    con.execute(f"""
        CREATE TABLE dash_retention_rolling AS
        WITH months AS (
            SELECT DISTINCT date_trunc('month', txn_ts)::DATE AS m
            FROM _cust_orders
        ),
        span AS (SELECT MIN(txn_ts)::DATE AS lo FROM _cust_orders),
        j AS (
            SELECT mo.m,
                   c.first_channel,
                   o.customer_key,
                   COUNT(*) AS visits
            FROM months mo
            JOIN _cust_orders o
              ON o.txn_ts >= mo.m - INTERVAL 5 MONTH
             AND o.txn_ts <  mo.m + INTERVAL 1 MONTH
            JOIN _cust c USING (customer_key)
            GROUP BY 1, 2, 3
        )
        SELECT j.m                                          AS month,
               j.first_channel,
               COUNT(*)                                     AS customers,
               SUM(j.visits)                                AS visits,
               SUM(CASE WHEN j.visits >= 2 THEN 1 ELSE 0 END)
                                                            AS repeat_customers,
               SUM(CASE WHEN j.visits >= 5 THEN 1 ELSE 0 END)
                                                            AS heavy_customers,
               LEAST(6, date_diff('month',
                     (SELECT lo FROM span), j.m) + 1)       AS window_months
        FROM j GROUP BY 1, 2, 7
        HAVING COUNT(*) >= {min_cell}
    """)

    # ---- meta ---------------------------------------------------------
    con.execute("""
        CREATE TABLE dash_retention_meta AS
        SELECT (SELECT COUNT(*) FROM _cust)                  AS customers,
               (SELECT COUNT(DISTINCT customer_key)
                  FROM src.fact_basket)                      AS all_customers,
               (SELECT MIN(txn_ts)::DATE FROM src.fact_basket) AS cov_start,
               (SELECT MAX(txn_ts)::DATE FROM src.fact_basket) AS cov_end,
               now()                                         AS built_at
    """)

    for t in ("dash_retention_summary", "dash_retention_cohort",
              "dash_retention_sequence", "dash_retention_gaps",
              "dash_retention_rolling",
              "dash_retention_meta"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            continue
        out[t] = n
        print(f"  [retention] {t:<32} {n:,} rows")

    con.execute("DROP TABLE IF EXISTS _cust_orders")
    con.execute("DROP TABLE IF EXISTS _cust")
    return out
