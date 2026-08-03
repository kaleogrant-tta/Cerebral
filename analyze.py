"""
Substitution analysis — framework sections 2.4 and 4.2.

    python analyze.py                          # whole database
    python analyze.py --a Flower --b Pre-Roll  # focus a specific pair
    python analyze.py --store DTBK

Four tests, weakest evidence to strongest:

  1. CO-PURCHASE LIFT      basket-level association. Cheap, but association
                           is not substitution.
  2. EXCLUSIVITY TREND     is the share of baskets containing exactly one of
                           the pair rising? Substitutes diverge over time.
  3. PENETRATION DECOMP    is the category losing BUYERS or losing FREQUENCY?
                           Different problems, different responses.
  4. CUSTOMER MIGRATION    the real test. Take customers who bought A early,
                           see what they bought later. Substitution shows up
                           as A-buyers becoming B-buyers, not as A-buyers
                           leaving.

Only test 4 supports a causal claim, and only weakly — this is observational
data, so treat everything here as a hypothesis worth testing deliberately.
"""

from __future__ import annotations

import argparse
import sys

import duckdb
import pandas as pd

STORES = {1: "DTBK", 2: "5AVE", 3: "SOHO", 4: "USQ"}
MIN_CATEGORY_SHARE = 0.005      # ignore categories under 0.5% of revenue


def hr(c="-", n=78):
    print(c * n)


def header(t):
    print()
    hr("=")
    print(f"  {t}")
    hr("=")


# ---------------------------------------------------------------------------

def co_purchase(con, sf) -> pd.DataFrame:
    """lift(A,B) = P(A and B) / (P(A) x P(B))

    Reported two ways, because the raw figure is misleading here.

    With ~1.9 lines and >50% single-line baskets, a basket usually CANNOT
    contain two categories. That suppresses every pairwise lift mechanically:
    on this data all pairs land under 0.7, which would read as "everything
    substitutes everything", which is meaningless.

    The conditional version restricts to baskets that already contain 2+
    categories, removing the basket-size effect. Among customers who bought
    more than one thing, which pairs actually go together? That is the
    comparison that carries information.
    """
    return con.execute(f"""
        WITH b AS (
            SELECT basket_id, category
            FROM fact_line WHERE NOT is_return {sf}
            GROUP BY 1,2
        ),
        multi AS (
            SELECT basket_id FROM b GROUP BY 1 HAVING COUNT(*) >= 2
        ),
        bm AS (SELECT b.* FROM b JOIN multi USING (basket_id)),

        n_all   AS (SELECT COUNT(DISTINCT basket_id)::DOUBLE t FROM b),
        n_multi AS (SELECT COUNT(DISTINCT basket_id)::DOUBLE t FROM bm),

        p_all AS (
            SELECT category, COUNT(DISTINCT basket_id)::DOUBLE/(SELECT t FROM n_all) pr
            FROM b GROUP BY 1
        ),
        p_multi AS (
            SELECT category, COUNT(DISTINCT basket_id)::DOUBLE/(SELECT t FROM n_multi) pr
            FROM bm GROUP BY 1
        ),
        pair_all AS (
            SELECT x.category a, y.category b,
                   COUNT(*)::DOUBLE/(SELECT t FROM n_all) joint
            FROM b x JOIN b y ON x.basket_id=y.basket_id AND x.category<y.category
            GROUP BY 1,2
        ),
        pair_multi AS (
            SELECT x.category a, y.category b,
                   COUNT(*)::DOUBLE/(SELECT t FROM n_multi) joint,
                   COUNT(*) n
            FROM bm x JOIN bm y ON x.basket_id=y.basket_id AND x.category<y.category
            GROUP BY 1,2
        )
        SELECT pa.a, pa.b, pa.joint,
               pa.joint/(qa.pr*qb.pr)                    AS lift_raw,
               pm.joint/(ma.pr*mb.pr)                    AS lift_multi,
               pm.n                                      AS pair_baskets
        FROM pair_all pa
        JOIN pair_multi pm ON pm.a=pa.a AND pm.b=pa.b
        JOIN p_all   qa ON qa.category=pa.a
        JOIN p_all   qb ON qb.category=pa.b
        JOIN p_multi ma ON ma.category=pa.a
        JOIN p_multi mb ON mb.category=pa.b
        WHERE qa.pr > 0.01 AND qb.pr > 0.01 AND pm.n >= 50
        ORDER BY lift_multi
    """).df()


def exclusivity_trend(con, sf, a, b) -> pd.DataFrame:
    """Among baskets containing A or B, what share contain only one?"""
    return con.execute(f"""
        WITH f AS (
            SELECT basket_id, iso_year, iso_week,
                   MAX(CASE WHEN category = '{a}' THEN 1 ELSE 0 END) AS has_a,
                   MAX(CASE WHEN category = '{b}' THEN 1 ELSE 0 END) AS has_b
            FROM fact_line WHERE NOT is_return {sf}
            GROUP BY 1,2,3
        )
        SELECT iso_year, iso_week,
               SUM(has_a) AS a_baskets,
               SUM(has_b) AS b_baskets,
               SUM(CASE WHEN has_a=1 AND has_b=1 THEN 1 ELSE 0 END) AS both,
               COUNT(*) AS all_baskets
        FROM f WHERE has_a=1 OR has_b=1
        GROUP BY 1,2 ORDER BY 1,2
    """).df()


def penetration_decomp(con, sf, cat) -> pd.DataFrame:
    """Split penetration change into buyer count vs purchase frequency."""
    return con.execute(f"""
        WITH mth AS (
            SELECT strftime(txn_ts,'%Y-%m') AS m,
                   COUNT(DISTINCT customer_key) AS all_buyers,
                   COUNT(DISTINCT basket_id)    AS all_baskets
            FROM fact_basket WHERE NOT is_return {sf} GROUP BY 1
        ),
        c AS (
            SELECT strftime(txn_ts,'%Y-%m') AS m,
                   COUNT(DISTINCT customer_key) AS buyers,
                   COUNT(DISTINCT basket_id)    AS baskets,
                   SUM(units)                   AS units
            FROM fact_line
            WHERE NOT is_return AND category = '{cat}' {sf}
            GROUP BY 1
        )
        SELECT c.m,
               c.buyers, mth.all_buyers,
               c.buyers::DOUBLE / mth.all_buyers      AS buyer_reach,
               c.baskets::DOUBLE / mth.all_baskets    AS penetration,
               c.baskets::DOUBLE / c.buyers           AS baskets_per_buyer,
               c.units::DOUBLE  / c.baskets           AS units_per_basket
        FROM c JOIN mth USING (m) ORDER BY c.m
    """).df()


def migration(con, sf, a, b) -> pd.DataFrame:
    """Cohort: customers who bought A in the first third of the window.
    What did they buy in the last third?"""
    bounds = con.execute(f"""
        SELECT MIN(txn_ts), MAX(txn_ts) FROM fact_line WHERE NOT is_return {sf}
    """).fetchone()
    start, end = pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1])
    span = (end - start) / 3
    p1_end = start + span
    p3_start = end - span

    return con.execute(f"""
        WITH early AS (
            SELECT DISTINCT customer_key
            FROM fact_line
            WHERE NOT is_return AND category = '{a}' {sf}
              AND txn_ts < TIMESTAMP '{p1_end}'
              AND customer_key IS NOT NULL
        ),
        late AS (
            SELECT customer_key,
                   MAX(CASE WHEN category='{a}' THEN 1 ELSE 0 END) AS kept_a,
                   MAX(CASE WHEN category='{b}' THEN 1 ELSE 0 END) AS has_b
            FROM fact_line
            WHERE NOT is_return {sf} AND txn_ts >= TIMESTAMP '{p3_start}'
              AND customer_key IS NOT NULL
            GROUP BY 1
        )
        SELECT
            COUNT(*) AS cohort,
            SUM(CASE WHEN l.customer_key IS NULL THEN 1 ELSE 0 END) AS lapsed,
            SUM(CASE WHEN l.kept_a=1 AND l.has_b=1 THEN 1 ELSE 0 END) AS both,
            SUM(CASE WHEN l.kept_a=1 AND l.has_b=0 THEN 1 ELSE 0 END) AS a_only,
            SUM(CASE WHEN l.kept_a=0 AND l.has_b=1 THEN 1 ELSE 0 END) AS switched,
            SUM(CASE WHEN l.kept_a=0 AND l.has_b=0 AND l.customer_key IS NOT NULL
                     THEN 1 ELSE 0 END) AS neither
        FROM early e LEFT JOIN late l USING (customer_key)
    """).df(), p1_end, p3_start


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tta.duckdb")
    ap.add_argument("--store")
    ap.add_argument("--a", default="Flower")
    ap.add_argument("--b", default="Pre-Roll")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    sf = ""
    label = "ALL STORES"
    if args.store:
        k = next((k for k, v in STORES.items() if v == args.store.upper()), None)
        if not k:
            print(f"Unknown store. Use {list(STORES.values())}")
            return 1
        sf, label = f" AND store_key = {k}", args.store.upper()

    a, b = args.a, args.b

    # ---- 1. co-purchase lift -------------------------------------------
    header(f"1. CO-PURCHASE LIFT — {label}")
    lift = co_purchase(con, sf)
    if lift.empty:
        print("  Not enough data.")
        return 1

    print("  RAW lift is suppressed by basket size: with most baskets holding")
    print("  one category, two categories rarely co-occur and every pair looks")
    print("  like a substitute. Read the MULTI column instead — it restricts to")
    print("  baskets that already contain 2+ categories.")
    print()
    print(f"  {'Pair':<26}{'raw':>7}{'MULTI':>8}{'baskets':>10}{'Read':>15}")
    hr()
    for _, r in lift.iterrows():
        if r.lift_multi < 0.7:
            read = "SUBSTITUTES"
        elif r.lift_multi > 1.3:
            read = "affinity"
        else:
            read = "independent"
        star = " <<<" if {r.a, r.b} == {a, b} else ""
        print(f"  {r.a + ' + ' + r.b:<26}{r.lift_raw:>7.2f}{r.lift_multi:>8.2f}"
              f"{int(r.pair_baskets):>10,}{read:>15}{star}")

    pair = lift[(lift.a.isin([a, b])) & (lift.b.isin([a, b]))]
    pl = float(pair.lift_multi.iloc[0]) if len(pair) else float("nan")

    # ---- 2. exclusivity trend ------------------------------------------
    header(f"2. EXCLUSIVITY TREND — {a} vs {b}")
    ex = exclusivity_trend(con, sf, a, b)
    if len(ex) >= 8:
        ex["both_share"] = ex.both / ex.all_baskets * 100
        ex["a_share"] = ex.a_baskets / ex.all_baskets * 100
        ex["b_share"] = ex.b_baskets / ex.all_baskets * 100
        first, last = ex.head(6), ex.tail(6)
        print(f"  Among baskets containing {a} or {b}:")
        print()
        print(f"  {'':>18}{'first 6 wks':>14}{'last 6 wks':>14}{'change':>12}")
        hr()
        for lbl, col in [(f"contain {a}", "a_share"),
                         (f"contain {b}", "b_share"),
                         ("contain both", "both_share")]:
            f0, l0 = first[col].mean(), last[col].mean()
            print(f"  {lbl:>18}{f0:>13.1f}%{l0:>13.1f}%{l0-f0:>+11.1f}pp")
        print()
        if last.both_share.mean() < first.both_share.mean() - 1:
            print("  Overlap is SHRINKING — consistent with substitution.")
        elif last.both_share.mean() > first.both_share.mean() + 1:
            print("  Overlap is GROWING — argues against substitution.")
        else:
            print("  Overlap is stable — no substitution signal here.")
    else:
        print("  Need at least 8 weeks of data.")

    # ---- 3. penetration decomposition ----------------------------------
    for cat in (a, b):
        header(f"3. PENETRATION DECOMPOSITION — {cat}")
        d = penetration_decomp(con, sf, cat)
        if len(d) < 3:
            print("  Need at least 3 months.")
            continue
        print(f"  {'Month':<10}{'Buyers':>10}{'Reach':>9}{'Penetr.':>10}"
              f"{'Bkts/buyer':>12}{'Units/bkt':>11}")
        hr()
        for _, r in d.iterrows():
            print(f"  {r.m:<10}{int(r.buyers):>10,}{r.buyer_reach*100:>8.1f}%"
                  f"{r.penetration*100:>9.1f}%{r.baskets_per_buyer:>12.2f}"
                  f"{r.units_per_basket:>11.2f}")
        f0, l0 = d.head(3), d.tail(3)
        dr = (l0.buyer_reach.mean() - f0.buyer_reach.mean()) * 100
        df_ = l0.baskets_per_buyer.mean() - f0.baskets_per_buyer.mean()
        print()
        print(f"  buyer reach {dr:+.1f}pp   baskets per buyer {df_:+.2f}")
        if dr < -0.5 and abs(df_) < 0.05:
            print(f"  -> {cat} is losing BUYERS, not frequency. A reach problem.")
        elif dr > -0.5 and df_ < -0.05:
            print(f"  -> {cat} keeps its buyers but they buy LESS OFTEN.")
        elif dr < -0.5 and df_ < -0.05:
            print(f"  -> {cat} is losing buyers AND frequency. Broad decline.")
        else:
            print(f"  -> {cat} reach and frequency are broadly stable.")

    # ---- 4. customer migration -----------------------------------------
    header(f"4. CUSTOMER MIGRATION — did {a} buyers move to {b}?")
    mig, p1, p3 = migration(con, sf, a, b)
    r = mig.iloc[0]
    n = int(r.cohort)
    if n < 100:
        print(f"  Cohort too small ({n}).")
    else:
        print(f"  Cohort: {n:,} customers who bought {a} before "
              f"{str(p1)[:10]}")
        print(f"  Behaviour on or after {str(p3)[:10]}:")
        print()
        rows = [
            (f"still buying {a}, also {b}", r.both),
            (f"still buying {a} only", r.a_only),
            (f"stopped {a}, now buying {b}", r.switched),
            (f"stopped {a}, not buying {b}", r.neither),
            ("no purchase at all (lapsed)", r.lapsed),
        ]
        for lbl, v in rows:
            print(f"  {lbl:<34}{int(v):>9,}{v/n*100:>8.1f}%")
        print()
        sw = r.switched / n * 100
        lp = r.lapsed / n * 100
        if sw > lp:
            print(f"  Switching ({sw:.1f}%) exceeds lapsing ({lp:.1f}%).")
            print(f"  Consistent with substitution into {b}.")
        else:
            print(f"  Lapsing ({lp:.1f}%) exceeds switching ({sw:.1f}%).")
            print(f"  Looks more like {a} customers LEAVING than switching.")
            print("  That is a retention problem, not a substitution one.")

    # ---- verdict --------------------------------------------------------
    header("VERDICT")
    print(f"  Co-purchase lift {a}/{b} (multi-category baskets): {pl:.2f}")
    if pl < 0.7:
        print("  -> basket-level evidence SUPPORTS substitution")
    elif pl > 1.3:
        print("  -> basket-level evidence CONTRADICTS substitution "
              "(bought together)")
    else:
        print("  -> basket-level evidence is NEUTRAL")
    print()
    print("  This is observational data. Association is not causation, and")
    print("  customer_key is a name hash for non-redeemers, so migration")
    print("  figures carry some collision noise. Treat as a hypothesis to")
    print("  test with a deliberate assortment change (framework 4.1).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
