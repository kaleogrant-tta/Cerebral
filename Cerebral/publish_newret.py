"""
New vs returning customers, per store and ISO week.

Nothing already published can answer this. dash_basket_week counts baskets,
dash_category_week counts baskets carrying a category, and both are grouped
past the point where a customer exists. Splitting new from returning needs a
first-ever-purchase date per customer, which only lives in the working
database — so it has to be resolved here, before the identifiers are dropped.

Definitions, deliberately narrow:

  A customer is NEW in the week their first ever non-return basket falls,
  measured against the whole loaded history rather than the selected window.
  Every other week they appear in, they are RETURNING.

  The classification is on the customer-week, not the basket. Someone whose
  first visit was Monday and who came back Friday is one new customer that
  week, not one new and one returning — otherwise the two columns overlap
  and stop summing to the customer count.

  Store rows attribute a chain-new customer to whichever store they shopped
  that week. A customer new to the chain who visits two stores in one week
  is new at both, so per-store rows sum to slightly more than the chain
  figure. store_key = 0 is the exact chain rollup; use it when no store
  filter is applied.

Two identity scopes are published:

  'resolved'  customers whose key is a real loyalty identity
  'all'       every customer key, including name-hash fallbacks

The name-hash fallback collides — two people sharing a name become one key —
and every collision turns a genuinely new customer into a returning one. The
bias runs one way only, so 'all' understates new customers and the level is
not trustworthy even where the trend is. 'resolved' is the honest series and
is what the dashboard shows by default.
"""

from __future__ import annotations


# The ETL keys a customer either on their Alpine ID -- plain digits -- or,
# where there is none, on a hash of their name prefixed with H:
#
#     680033             loyalty identity, exact
#     Hd39edce77a35dcb   name hash, collides
#
# Confirmed against tta.duckdb on 2026-08-11: 80.0% of customers and 69.3%
# of baskets resolve. That gap is the collision bias made visible -- hash
# keys carry more baskets each because several people are stacked into one.
#
# Pinned rather than detected. Detection below still runs if this is set to
# None, but a silent change of rule would move every number on the chart
# without anything failing, which is the worst way for this to break.
ID_RULE: tuple[str, str] | None = (
    "numeric key = Alpine ID (pinned)",
    "regexp_matches(CAST(customer_key AS VARCHAR), '^[0-9]+$')",
)

# Fallback detection, used only when ID_RULE is None. Each candidate is
# tested against the data and the first that actually splits the population
# wins: a rule matching everything or nothing is not a rule, it is a bug.
_KIND_COLS = ("customer_key_kind", "key_kind", "customer_kind",
              "id_source", "key_source", "customer_source", "id_kind")

_PATTERN_RULES = [
    ("numeric key = loyalty ID",
     "regexp_matches(CAST(customer_key AS VARCHAR), '^[0-9]+$')"),
    ("key prefixed nh:",
     "CAST(customer_key AS VARCHAR) NOT LIKE 'nh:%'"),
    ("key prefixed nh_",
     "CAST(customer_key AS VARCHAR) NOT LIKE 'nh\\_%' ESCAPE '\\'"),
    ("key prefixed hash",
     "lower(CAST(customer_key AS VARCHAR)) NOT LIKE 'hash%'"),
    ("hex string = name hash",
     "NOT regexp_matches(lower(CAST(customer_key AS VARCHAR)), "
     "'^[0-9a-f]{12,}$')"),
]


def _basket_columns(con) -> set[str]:
    rows = con.execute("PRAGMA table_info('src.fact_basket')").fetchall()
    return {str(r[1]).lower() for r in rows}


def _pick_id_rule(con) -> tuple[str, str, float]:
    """Return (label, SQL predicate, share of baskets that are resolved).

    Falls back to a rule that matches everything, labelled as such, so the
    build never fails just because identity marking changed shape again.
    """
    if ID_RULE is not None:
        label, pred = ID_RULE
        share = con.execute(f"""
            SELECT AVG(CASE WHEN {pred} THEN 1.0 ELSE 0.0 END)
            FROM src.fact_basket
            WHERE NOT is_return AND customer_key IS NOT NULL
        """).fetchone()[0]
        # A pinned rule that has stopped splitting the population means the
        # ETL changed under it. Say so loudly and fall through to detection
        # rather than publishing a chart built on one bucket.
        if share is not None and 0.02 <= float(share) <= 0.995:
            return label, pred, float(share)
        print(f"  WARNING: pinned identity rule now matches "
              f"{(share or 0) * 100:.1f}% of baskets — falling back to "
              f"detection. Check how the ETL keys customers.")

    cols = _basket_columns(con)

    candidates: list[tuple[str, str]] = []
    for c in _KIND_COLS:
        if c in cols:
            candidates.append((f"column: {c}",
                               f"lower(CAST({c} AS VARCHAR)) NOT LIKE '%hash%'"))
    if "is_name_hash" in cols:
        candidates.append(("column: is_name_hash", "NOT is_name_hash"))
    candidates.extend(_PATTERN_RULES)

    for label, pred in candidates:
        try:
            share = con.execute(f"""
                SELECT AVG(CASE WHEN {pred} THEN 1.0 ELSE 0.0 END)
                FROM src.fact_basket
                WHERE NOT is_return AND customer_key IS NOT NULL
            """).fetchone()[0]
        except Exception:
            continue
        if share is None:
            continue
        # A usable rule splits the population. Anything outside this band is
        # matching on something that is not the identity marker.
        if 0.02 <= float(share) <= 0.995:
            return label, pred, float(share)

    return "none found — all keys treated as resolved", "TRUE", 1.0


def build_newret(con) -> None:
    """Create dash_newret_week and dash_newret_meta on the slim connection.

    Called from publish.build while src is still attached.
    """
    label, pred, share = _pick_id_rule(con)
    print(f"  new/returning identity rule: {label} "
          f"({share * 100:.1f}% of baskets resolved)")

    con.execute("""
        CREATE OR REPLACE VIEW nr_base AS
        SELECT store_key, iso_year, iso_week, customer_key, basket_net,
               txn_ts
        FROM src.fact_basket
        WHERE NOT is_return AND customer_key IS NOT NULL
    """)

    con.execute(f"""
        CREATE OR REPLACE VIEW nr_res AS
        SELECT store_key, iso_year, iso_week, customer_key, basket_net,
               txn_ts
        FROM src.fact_basket
        WHERE NOT is_return AND customer_key IS NOT NULL AND ({pred})
    """)

    con.execute("""
        CREATE TABLE dash_newret_week (
            store_key INTEGER, iso_year INTEGER, iso_week INTEGER,
            id_scope VARCHAR, segment VARCHAR,
            customers BIGINT, baskets BIGINT, net DOUBLE)
    """)

    for scope, view in (("resolved", "nr_res"), ("all", "nr_base")):
        # Per-store rows, then an exact chain rollup at store_key = 0.
        for store_expr in ("store_key", "0"):
            con.execute(f"""
                INSERT INTO dash_newret_week
                WITH f AS (
                    SELECT customer_key,
                           MIN(txn_ts) AS first_ts
                    FROM {view}
                    GROUP BY 1
                ),
                fw AS (
                    SELECT customer_key,
                           CAST(strftime(first_ts, '%G') AS INTEGER) AS f_year,
                           CAST(strftime(first_ts, '%V') AS INTEGER) AS f_week
                    FROM f
                ),
                cw AS (
                    -- one row per customer per store-week, so the segment is
                    -- a property of the customer-week rather than the basket
                    SELECT {store_expr}      AS store_key,
                           b.iso_year, b.iso_week, b.customer_key,
                           COUNT(*)          AS baskets,
                           SUM(b.basket_net) AS net,
                           CASE WHEN fw.f_year = b.iso_year
                                 AND fw.f_week = b.iso_week
                                THEN 'New' ELSE 'Returning' END AS segment
                    FROM {view} b
                    JOIN fw USING (customer_key)
                    GROUP BY 1, 2, 3, 4, 7
                )
                SELECT store_key, iso_year, iso_week,
                       '{scope}'                  AS id_scope,
                       segment,
                       COUNT(DISTINCT customer_key) AS customers,
                       SUM(baskets)               AS baskets,
                       SUM(net)                   AS net
                FROM cw
                GROUP BY 1, 2, 3, 4, 5
            """)

    # Anything in the first weeks of loaded history looks new simply because
    # there is nothing before it to have been seen in. The dashboard greys
    # those weeks out rather than drawing a fake spike.
    con.execute("""
        CREATE TABLE dash_newret_meta AS
        SELECT MIN(txn_ts)  AS first_txn,
               MAX(txn_ts)  AS last_txn,
               4            AS burn_in_weeks
        FROM nr_base
    """)

    con.execute("DROP VIEW IF EXISTS nr_res")
    con.execute("DROP VIEW IF EXISTS nr_base")

    n = con.execute("SELECT COUNT(*) FROM dash_newret_week").fetchone()[0]
    print(f"  dash_newret_week: {n:,} rows")
