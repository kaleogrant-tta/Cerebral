"""
publish_group_lift.py -- before/after enrollment lift for discount groups.

Called from publish.py's build(), after ATTACH and before DETACH:

    from publish_group_lift import build_group_lift
    ...
    build_group_lift(con)

Requires src.dim_discount_group_member. No-ops without it.

WHY THIS EXISTS
---------------
Basket size by group is not a behavioural measure. Frequent Flyer baskets
average far more than everyone else's, but on baskets with NO offer attached
the gap nearly vanishes and the medians reverse: the tier's 2x accrual means
its baskets are roughly four times as likely to carry a redemption, so the
comparison measures offer attachment, not spending.

The measure that survives is total spend per member over a fixed window
either side of the member's own enrolment date. Each member is their own
control, so the self-selection in a paid membership cancels.

TWO GUARDS, BOTH LEARNED THE HARD WAY
-------------------------------------
1. first_added carries a 2000-01-01 sentinel for members with no real date.
   Those members have every basket classed as "after" and drag the result
   down. Excluded via MIN_JOIN_DATE.

2. The window is symmetric in calendar days, not tenure. A member who joined
   within WINDOW_DAYS of their first ever purchase has a "before" period
   that is partly pre-customer -- no visits because they had not shopped
   here yet, not because they shopped less. That alone manufactures a lift.
   Only members with WINDOW_DAYS of prior tenure are counted.

Both halves are also required to contain at least one visit, so a member who
simply stopped shopping does not read as a decline from a phantom baseline.

PUBLISH.PY CHANGE REQUIRED
--------------------------
"group_name" and "group_kind" are already in ALLOWED_TEXT for
dash_discount_group. No change needed.
"""

import numpy as np
import pandas as pd

WINDOW_DAYS = 90
MIN_JOIN_DATE = "2020-01-01"   # anything earlier is the null sentinel
MIN_MEMBERS = 30               # below this a group is not summarised

# Kinds where membership tracks employment. Someone leaving the group is
# usually someone leaving the job, so a spend decline is attrition rather
# than a change in how people shop. Published, but flagged.
NON_BEHAVIOURAL = {"Staff / friends & family", "Employee"}
BOOT = 3000
SEED = 11


def _src_has(con, table):
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name = 'src' AND table_name = ?", [table]
    ).fetchone()[0] > 0


def _ci_median(x, n=BOOT, seed=SEED):
    x = np.asarray([v for v in x if np.isfinite(v)])
    if len(x) < 3:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    draws = rng.choice(x, size=(n, len(x)), replace=True)
    return tuple(np.percentile(np.median(draws, axis=1), [2.5, 97.5]))


def _per_member(con):
    """One row per (group, member): spend, visits, discount, margin either
    side of that member's own enrolment date."""
    has_margin = con.execute("""
        SELECT COUNT(*) FROM duckdb_columns()
        WHERE table_name = 'fact_basket' AND column_name = 'basket_margin'
    """).fetchone()[0] > 0
    marg = "b.basket_margin" if has_margin else "CAST(NULL AS DOUBLE)"

    return con.execute(f"""
        WITH m AS (
            SELECT group_name, group_kind, customer_key,
                   MIN(first_added::DATE) AS joined
            FROM src.dim_discount_group_member
            WHERE NOT COALESCE(pre_window, FALSE)
              AND first_added::DATE > DATE '{MIN_JOIN_DATE}'
              AND customer_key IS NOT NULL
              AND customer_key NOT LIKE 'H%'
            GROUP BY 1, 2, 3
        ),
        firsts AS (
            SELECT customer_key, MIN(txn_ts::DATE) AS first_buy
            FROM src.fact_basket WHERE NOT is_return GROUP BY 1
        ),
        elig AS (
            SELECT m.* FROM m JOIN firsts f USING (customer_key)
            WHERE f.first_buy <= m.joined - {WINDOW_DAYS}
        ),
        b AS (
            SELECT e.group_name, e.group_kind, e.customer_key,
                   CASE WHEN b.txn_ts::DATE < e.joined THEN 'pre'
                        ELSE 'post' END               AS phase,
                   b.basket_net                       AS net,
                   COALESCE(b.discount_amt, 0)        AS disc,
                   {marg}                             AS margin
            FROM elig e
            JOIN src.fact_basket b ON b.customer_key = e.customer_key
            WHERE NOT b.is_return
              AND b.txn_ts::DATE >= e.joined - {WINDOW_DAYS}
              AND b.txn_ts::DATE <  e.joined + {WINDOW_DAYS}
        )
        SELECT group_name, group_kind, customer_key,
               SUM(CASE WHEN phase='pre'  THEN net    ELSE 0 END) AS pre_net,
               SUM(CASE WHEN phase='post' THEN net    ELSE 0 END) AS post_net,
               SUM(CASE WHEN phase='pre'  THEN disc   ELSE 0 END) AS pre_disc,
               SUM(CASE WHEN phase='post' THEN disc   ELSE 0 END) AS post_disc,
               SUM(CASE WHEN phase='pre'  THEN margin ELSE 0 END) AS pre_marg,
               SUM(CASE WHEN phase='post' THEN margin ELSE 0 END) AS post_marg,
               SUM(CASE WHEN phase='pre'  THEN 1 ELSE 0 END)      AS pre_visits,
               SUM(CASE WHEN phase='post' THEN 1 ELSE 0 END)      AS post_visits
        FROM b
        GROUP BY 1, 2, 3
        HAVING SUM(CASE WHEN phase='pre'  THEN 1 ELSE 0 END) > 0
           AND SUM(CASE WHEN phase='post' THEN 1 ELSE 0 END) > 0
    """).df()


def build_group_lift(con) -> dict:
    if not _src_has(con, "dim_discount_group_member"):
        print("  [group lift] src.dim_discount_group_member missing "
              "- skipping.")
        return {}

    pm = _per_member(con)
    if pm.empty:
        print("  [group lift] no members with tenure either side of "
              "enrolment - skipping.")
        return {}

    pm["d_net"] = pm.post_net - pm.pre_net
    pm["d_marg"] = pm.post_marg - pm.pre_marg

    rows = []
    for (gname, gkind), g in pm.groupby(["group_name", "group_kind"]):
        if len(g) < MIN_MEMBERS:
            continue
        lo, hi = _ci_median(g.d_net.values)
        rows.append({
            "group_name": gname, "group_kind": gkind,
            "window_days": WINDOW_DAYS,
            "interpretable": bool(gkind not in NON_BEHAVIOURAL),
            "members": int(len(g)),
            "spend_pre": float(g.pre_net.mean()),
            "spend_post": float(g.post_net.mean()),
            "median_change": float(g.d_net.median()),
            "ci_lo": lo, "ci_hi": hi,
            "excludes_zero": bool(np.isfinite(lo) and (lo > 0 or hi < 0)),
            "pct_increased": float((g.d_net > 0).mean() * 100),
            "visits_pre": float(g.pre_visits.mean()),
            "visits_post": float(g.post_visits.mean()),
            "discount_pre": float(g.pre_disc.mean()),
            "discount_post": float(g.post_disc.mean()),
            "margin_pre": float(g.pre_marg.mean()),
            "margin_post": float(g.post_marg.mean()),
            "median_margin_change": float(g.d_marg.median()),
        })

    summary = pd.DataFrame(rows)
    meta = pd.DataFrame([{
        "window_days": WINDOW_DAYS,
        "min_members": MIN_MEMBERS,
        "min_join_date": pd.Timestamp(MIN_JOIN_DATE).date(),
        "members_evaluated": int(len(pm)),
        "groups_published": int(len(summary)),
        "built_at": pd.Timestamp.now(),
    }])

    out = {}
    for name, df in (("dash_group_lift", summary),
                     ("dash_group_lift_meta", meta)):
        con.execute(f"DROP TABLE IF EXISTS {name}")
        con.register("_gl_tmp", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _gl_tmp")
        con.unregister("_gl_tmp")
        out[name] = len(df)
        print(f"  [group lift] {name:<28} {len(df):,} rows")
    return out

