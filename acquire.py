"""
Cerebral — acquisition strategy.

Turns the diagnosis into decisions: which categories acquire, which activate,
which channels source new customers, and where promotional spend is going.

    python acquire.py
    python acquire.py --store DTBK
    python acquire.py --alpine-only      # exact identity, no hash collisions

Sections
  1. ACQUISITION SOURCE      where new customers arrive: channel, store, day
  2. ACTIVATION BY CATEGORY   which first-basket category predicts a 2nd visit
  3. ACQUISITION EFFICIENCY   new customers acquired per $1k of category revenue
  4. OFFER TARGETING          are redemptions reaching new or existing customers
  5. PROCUREMENT SIGNAL       acquire / activate / retain role per category

The distinction that matters for campaign design:
  ACQUIRE   category appears in first baskets -> drives trial
  ACTIVATE  first-basket presence raises the odds of a second visit
  RETAIN    bought repeatedly by established customers

A category can do one well and another badly. Promoting an acquire-strong,
activate-weak category buys traffic that never comes back.
"""

from __future__ import annotations

import argparse
import sys

import duckdb
import pandas as pd

STORES = {1: "DTBK", 2: "5AVE", 3: "SOHO", 4: "USQ"}
BURN_IN_MONTHS = 2
MIN_COHORT = 200
RETURN_WINDOW_DAYS = 60


def hr(c="-", n=78):
    print(c * n)


def header(t):
    print()
    hr("=")
    print(f"  {t}")
    hr("=")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tta.duckdb")
    ap.add_argument("--store")
    ap.add_argument("--alpine-only", action="store_true",
                    help="restrict to customers with a real Alpine ID")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    sf, label = "", "ALL STORES"
    if args.store:
        k = next((k for k, v in STORES.items() if v == args.store.upper()), None)
        if not k:
            print(f"Unknown store. Use {list(STORES.values())}")
            return 1
        sf, label = f" AND store_key = {k}", args.store.upper()

    idf = ""
    if args.alpine_only:
        label += "  (Alpine IDs only)"
        print("\n  WARNING — --alpine-only is NOT a clean control.")
        print("  customer_key is the Alpine ID on baskets that carried a")
        print("  redemption and a name hash otherwise, so the SAME person")
        print("  holds two different keys. This mode therefore selects")
        print("  redemption baskets, not a population of exactly-identified")
        print("  customers. Return rates here mean 'redeemed again', not")
        print("  'came back'. Use it to sanity-check direction only.")

    # Base views ---------------------------------------------------------
    # customer_source lives on fact_line, so --alpine-only resolves the set of
    # exact-identity customers first, then filters baskets to those keys.
    if args.alpine_only:
        con.execute(f"""
            CREATE OR REPLACE TEMP VIEW exact_ids AS
            SELECT DISTINCT customer_key FROM fact_line
            WHERE customer_source = 'alpine' AND customer_key IS NOT NULL {sf}
        """)
        n_exact = con.execute("SELECT COUNT(*) FROM exact_ids").fetchone()[0]
        if n_exact < 500:
            print(f"\n  Only {n_exact:,} customers carry a real Alpine ID — too "
                  f"few for cohort work. Run without --alpine-only.")
            return 1
        idf = " AND customer_key IN (SELECT customer_key FROM exact_ids)"

    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW cust AS
        SELECT customer_key,
               MIN(txn_ts) AS first_ts,
               MAX(txn_ts) AS last_ts,
               COUNT(*)    AS visits
        FROM fact_basket
        WHERE NOT is_return AND customer_key IS NOT NULL {sf}{idf}
        GROUP BY 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW first_b AS
        SELECT b.*, strftime(b.txn_ts,'%Y-%m') AS cohort
        FROM fact_basket b JOIN cust c
          ON c.customer_key = b.customer_key AND c.first_ts = b.txn_ts
        WHERE NOT b.is_return {sf}
    """)

    months = con.execute("SELECT DISTINCT cohort FROM first_b ORDER BY 1").df()
    if len(months) <= BURN_IN_MONTHS + 2:
        print("Not enough history.")
        return 1
    valid = months.cohort.tolist()[BURN_IN_MONTHS:]

    # A trailing partial month is kept, not discarded — three weeks of data is
    # worth having. It is compared on a PER-DAY basis instead, so 19 days does
    # not read as a collapse against 31. Raw counts are still shown; only the
    # trend arithmetic is normalised.
    max_ts = pd.Timestamp(
        con.execute("SELECT MAX(txn_ts) FROM fact_basket").fetchone()[0])
    days_in = con.execute("""
        SELECT strftime(txn_ts, '%Y-%m') AS m,
               COUNT(DISTINCT date_key)  AS days
        FROM fact_basket WHERE NOT is_return GROUP BY 1
    """).df().set_index("m")["days"].to_dict()

    partial_month, partial_days, partial_full = None, None, None
    _end = (max_ts.replace(day=1) + pd.offsets.MonthEnd(1))
    if max_ts.date() < _end.date():
        partial_month = max_ts.strftime("%Y-%m")
        partial_days = days_in.get(partial_month, max_ts.day)
        partial_full = _end.day

    vstr = "','".join(valid)
    max_ts = con.execute("SELECT MAX(txn_ts) FROM fact_basket").fetchone()[0]
    mature = [m for m in valid
              if (pd.Timestamp(max_ts) - pd.Timestamp(m + "-01")).days
              > RETURN_WINDOW_DAYS + 28]
    mstr = "','".join(mature)

    print(f"\n  Cerebral — acquisition strategy — {label}")
    print(f"  cohorts {valid[0]}..{valid[-1]}   "
          f"mature (>{RETURN_WINDOW_DAYS}d observed): {len(mature)}")

    # ---- 1. acquisition source -----------------------------------------
    header("1. WHERE NEW CUSTOMERS ARRIVE")
    src = con.execute(f"""
        SELECT cohort, channel,
               COUNT(DISTINCT customer_key) AS new_cust
        FROM first_b WHERE cohort IN ('{vstr}')
        GROUP BY 1,2 ORDER BY 1,2
    """).df()
    piv = src.pivot(index="cohort", columns="channel", values="new_cust").fillna(0)
    cols = [c for c in ["In-Store", "Non-Stop", "Delivery"] if c in piv.columns]
    piv["days"] = [days_in.get(m, 30) for m in piv.index]

    print(f"  {'Month':<10}{'Total':>9}{'Days':>6}{'/day':>8}" +
          "".join(f"{c:>11}" for c in cols))
    hr()
    for m, r in piv.iterrows():
        tot = r[cols].sum()
        mark = "*" if m == partial_month else " "
        print(f"  {m:<9}{mark}{int(tot):>9,}{int(r['days']):>6}"
              f"{tot/r['days']:>8,.0f}" +
              "".join(f"{int(r[c]):>11,}" for c in cols))
    if partial_month:
        print(f"\n  * {partial_month} covers {partial_days} of "
              f"{partial_full} days. Kept, and compared per day below so the "
              f"short month does not read as a decline.")

    if len(piv) >= 6:
        # Per-day rates make a partial month directly comparable.
        rate = piv[cols].div(piv["days"], axis=0)
        f3, l3 = rate.head(3), rate.tail(3)
        print()
        print(f"  {'Channel':<12}{'new/day, first 3':>20}{'last 3':>12}"
              f"{'change':>10}")
        hr()
        for c in cols:
            a, b = f3[c].mean(), l3[c].mean()
            print(f"  {c:<12}{a:>20,.1f}{b:>12,.1f}"
                  f"{(b/max(a, 0.01)-1)*100:>+9.1f}%")

        ta, tb = f3.sum(axis=1).mean(), l3.sum(axis=1).mean()
        print()
        print(f"  Total new customers per day {ta:,.0f} -> {tb:,.0f} "
              f"({(tb/ta-1)*100:+.1f}%)")
        print(f"  Equivalent to {ta*30:,.0f} -> {tb*30:,.0f} per 30 days.")
        worst = min(cols, key=lambda c: l3[c].mean() / max(f3[c].mean(), 0.01))
        print(f"  Steepest decline: {worst}.")

    # ---- 2. activation --------------------------------------------------
    header(f"2. ACTIVATION — does a category in the first basket predict a "
           f"return within {RETURN_WINDOW_DAYS} days?")
    if not mature:
        print("  No cohort has a full return window yet.")
    else:
        act = con.execute(f"""
            WITH fb AS (
                SELECT f.customer_key, f.basket_id, f.txn_ts, f.cohort,
                       f.basket_lines
                FROM first_b f WHERE f.cohort IN ('{mstr}')
            ),
            cat AS (
                SELECT fb.customer_key, l.category
                FROM fb JOIN fact_line l ON l.basket_id = fb.basket_id
                WHERE NOT l.is_return
                GROUP BY 1,2
            ),
            ret AS (
                SELECT fb.customer_key,
                       MAX(CASE WHEN b.txn_ts > fb.txn_ts
                                 AND b.txn_ts <= fb.txn_ts
                                     + INTERVAL {RETURN_WINDOW_DAYS} DAY
                                THEN 1 ELSE 0 END) AS returned
                FROM fb JOIN fact_basket b USING (customer_key)
                WHERE NOT b.is_return {sf}
                GROUP BY 1
            ),
            sz AS (SELECT customer_key, basket_lines FROM fb)
            SELECT c.category,
                   COUNT(*)                              AS cohort_n,
                   AVG(r.returned)                       AS return_rate,
                   AVG(sz.basket_lines)                  AS avg_first_lines
            FROM cat c JOIN ret r USING (customer_key)
                       JOIN sz  USING (customer_key)
            GROUP BY 1 HAVING COUNT(*) >= {MIN_COHORT}
            ORDER BY return_rate DESC
        """).df()

        base = con.execute(f"""
            WITH fb AS (SELECT * FROM first_b WHERE cohort IN ('{mstr}')),
            ret AS (
                SELECT fb.customer_key,
                       MAX(CASE WHEN b.txn_ts > fb.txn_ts
                                 AND b.txn_ts <= fb.txn_ts
                                     + INTERVAL {RETURN_WINDOW_DAYS} DAY
                                THEN 1 ELSE 0 END) AS returned
                FROM fb JOIN fact_basket b USING (customer_key)
                WHERE NOT b.is_return {sf} GROUP BY 1
            )
            SELECT AVG(returned) r, COUNT(*) n FROM ret
        """).df().iloc[0]

        print(f"  Baseline: {base.r*100:.1f}% of {int(base.n):,} new customers "
              f"return within {RETURN_WINDOW_DAYS} days.")
        print()
        print(f"  {'First-basket category':<24}{'Customers':>11}{'Return %':>11}"
              f"{'vs base':>10}{'1st bkt lines':>15}")
        hr()
        for _, r in act.iterrows():
            lift = (r.return_rate - base.r) * 100
            print(f"  {r.category:<24}{int(r.cohort_n):>11,}"
                  f"{r.return_rate*100:>10.1f}%{lift:>+9.1f}pp"
                  f"{r.avg_first_lines:>15.2f}")
        print()
        print("  CAUTION: bigger first baskets return more often regardless of")
        print("  contents. Compare the lines column — a category that leads on")
        print("  return rate AND on basket size may just be riding basket size.")

    # ---- 3. acquisition efficiency --------------------------------------
    header("3. ACQUISITION EFFICIENCY — new customers per $1k of revenue")
    eff = con.execute(f"""
        WITH fb AS (SELECT * FROM first_b WHERE cohort IN ('{vstr}')),
        firsts AS (
            SELECT l.category, COUNT(DISTINCT fb.customer_key) AS acquired
            FROM fb JOIN fact_line l ON l.basket_id = fb.basket_id
            WHERE NOT l.is_return GROUP BY 1
        ),
        rev AS (
            SELECT category, SUM(net_sales) AS net, SUM(units) AS units
            FROM fact_line WHERE NOT is_return {sf} GROUP BY 1
        )
        SELECT r.category, r.net, f.acquired,
               f.acquired / (r.net/1000.0) AS per_1k
        FROM rev r JOIN firsts f USING (category)
        ORDER BY per_1k DESC
    """).df()
    print(f"  {'Category':<14}{'Net $':>13}{'New custs':>12}{'per $1k':>10}"
          f"{'role':>18}")
    hr()
    med = eff.per_1k.median()
    for _, r in eff.iterrows():
        role = "acquisition driver" if r.per_1k > med * 1.25 else \
               ("basket filler" if r.per_1k < med * 0.75 else "neutral")
        print(f"  {r.category:<14}{r.net:>13,.0f}{int(r.acquired):>12,}"
              f"{r.per_1k:>10.1f}  {role:>18}")
    print()
    print("  High per-$1k means the category appears in many first baskets")
    print("  relative to the revenue it produces — it pulls people in cheaply.")

    # ---- 4. offer targeting ---------------------------------------------
    header("4. OFFER TARGETING — do redemptions reach new or existing customers?")
    off = con.execute(f"""
        WITH fb AS (SELECT customer_key, txn_ts FROM first_b),
        r AS (
            SELECT b.customer_key, b.basket_id, b.txn_ts, b.loyalty_redeem,
                   date_diff('day', f.txn_ts, b.txn_ts) AS days_since_first
            FROM fact_basket b JOIN fb f USING (customer_key)
            WHERE NOT b.is_return AND b.used_redemption {sf}
        )
        SELECT
            CASE WHEN days_since_first = 0 THEN 'first visit'
                 WHEN days_since_first <= 30 THEN '1-30 days'
                 WHEN days_since_first <= 90 THEN '31-90 days'
                 ELSE '90+ days (established)' END AS stage,
            COUNT(*) AS baskets,
            SUM(loyalty_redeem) AS value
        FROM r GROUP BY 1
        ORDER BY MIN(days_since_first)
    """).df()
    if off.empty:
        print("  No redemption data in range.")
    else:
        tot_v = off.value.sum()
        print(f"  {'Customer stage':<26}{'Baskets':>11}{'Value $':>13}{'% spend':>10}")
        hr()
        for _, r in off.iterrows():
            print(f"  {r.stage:<26}{int(r.baskets):>11,}{r.value:>13,.0f}"
                  f"{r.value/tot_v*100:>9.1f}%")
        est = off[off.stage.str.contains("established")]
        if len(est):
            pct = est.value.iloc[0] / tot_v * 100
            print()
            print(f"  {pct:.0f}% of promotional value goes to customers already")
            print("  established for 90+ days. That is retention spend, not")
            print("  acquisition spend — reasonable if intended, expensive if not.")

    # ---- 5. procurement signal ------------------------------------------
    header("5. PROCUREMENT SIGNAL")
    proc = con.execute(f"""
        WITH fb AS (SELECT * FROM first_b WHERE cohort IN ('{vstr}')),
        acq AS (
            SELECT l.category, COUNT(DISTINCT fb.customer_key) AS first_basket
            FROM fb JOIN fact_line l ON l.basket_id = fb.basket_id
            WHERE NOT l.is_return GROUP BY 1
        ),
        est AS (
            SELECT l.category, COUNT(DISTINCT l.customer_key) AS estab
            FROM fact_line l JOIN cust c USING (customer_key)
            WHERE NOT l.is_return {sf}
              AND date_diff('day', c.first_ts, l.txn_ts) > 90
            GROUP BY 1
        ),
        rev AS (SELECT category, SUM(net_sales) net, SUM(gross_margin) gm
                FROM fact_line
                WHERE NOT is_return AND category IS NOT NULL {sf}
                GROUP BY 1)
        SELECT r.category, r.net, r.gm/NULLIF(r.net,0) AS margin,
               a.first_basket, e.estab,
               a.first_basket::DOUBLE / NULLIF(e.estab,0) AS acq_ratio
        FROM rev r LEFT JOIN acq a USING (category)
                   LEFT JOIN est e USING (category)
        ORDER BY r.net DESC
    """).df()
    print(f"  {'Category':<14}{'Net $':>13}{'GM%':>7}{'1st-bkt':>10}"
          f"{'Estab.':>10}{'ratio':>8}{'role':>12}")
    hr()
    for _, r in proc.iterrows():
        if pd.isna(r.acq_ratio):
            role = "—"
        elif r.acq_ratio > 1.1:
            role = "ACQUIRE"
        elif r.acq_ratio < 0.75:
            role = "RETAIN"
        else:
            role = "both"
        fb_n = 0 if pd.isna(r.first_basket) else int(r.first_basket)
        es_n = 0 if pd.isna(r.estab) else int(r.estab)
        rt = 0.0 if pd.isna(r.acq_ratio) else float(r.acq_ratio)
        print(f"  {r.category:<14}{r.net:>13,.0f}{r.margin*100:>6.1f}%"
              f"{fb_n:>10,}{es_n:>10,}{rt:>8.2f}{role:>12}")
    print()
    print("  ACQUIRE categories over-index in first baskets: protect stock and")
    print("  price position, they are the front door. RETAIN categories are")
    print("  bought by established customers: margin matters more than price")
    print("  visibility. Discounting a RETAIN category subsidises people who")
    print("  were going to buy anyway.")

    print()
    hr("=")
    print("  Everything here is observational. The only way to know a campaign")
    print("  worked is to withhold it from a random slice and compare — see")
    print("  framework 4.1 and 6.1.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
