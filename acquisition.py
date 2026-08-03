"""
Acquisition analysis — is a category losing buyers because it fails to
attract new customers, or because it loses existing ones?

    python acquisition.py
    python acquisition.py --store DTBK

Four views:

  1. NEW CUSTOMER VOLUME     how many first-ever buyers per month
  2. FIRST-BASKET MIX        what new customers buy on day one
  3. NEW vs RETURNING SPLIT  where each category's buyers come from
  4. COHORT ADOPTION         does a cohort's category mix hold over time

METHOD WARNING — read before trusting any of this.

  Burn-in. "First purchase" is measured inside the loaded window only. Every
  customer active in the first months looks new because there is no earlier
  data. Those months are excluded from cohort figures automatically.

  Identity. customer_key is a real Alpine ID for redeemers and a hash of the
  name otherwise. Two different people sharing a name collapse into one key,
  which makes a genuinely new customer look like a returning one. That biases
  new-customer counts DOWNWARD. Direction survives; precise rates do not.
"""

from __future__ import annotations

import argparse
import sys

import duckdb
import pandas as pd

STORES = {1: "DTBK", 2: "5AVE", 3: "SOHO", 4: "USQ"}
BURN_IN_MONTHS = 2
MIN_COHORT = 50        # below this, cohort percentages are noise


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
    ap.add_argument("--cats", default="Flower,Pre-Roll,Vape,Edible")
    ap.add_argument("--alpine-only", action="store_true",
                    help="restrict to customers with a real Alpine ID -- "
                         "exact identity, no name-hash collisions")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    sf, label = "", "ALL STORES"
    if args.store:
        k = next((k for k, v in STORES.items() if v == args.store.upper()), None)
        if not k:
            print(f"Unknown store. Use {list(STORES.values())}")
            return 1
        sf, label = f" AND store_key = {k}", args.store.upper()

    cats = [c.strip() for c in args.cats.split(",")]
    idf = ""

    if args.alpine_only:
        con.execute(f"""
            CREATE OR REPLACE TEMP VIEW exact_ids AS
            SELECT DISTINCT customer_key FROM fact_line
            WHERE customer_source = 'alpine' AND customer_key IS NOT NULL {sf}
        """)
        n = con.execute("SELECT COUNT(*) FROM exact_ids").fetchone()[0]
        if n < 500:
            print(f"Only {n:,} customers carry a real Alpine ID. Too few.")
            return 1
        idf = " AND customer_key IN (SELECT customer_key FROM exact_ids)"
        label += "  (Alpine IDs only)"

    # Base table: every customer's first-ever basket in the window.
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW first_basket AS
        WITH f AS (
            SELECT customer_key, MIN(txn_ts) AS first_ts
            FROM fact_basket
            WHERE NOT is_return AND customer_key IS NOT NULL {sf}{idf}
            GROUP BY 1
        )
        SELECT b.customer_key, b.basket_id, b.txn_ts,
               strftime(b.txn_ts, '%Y-%m') AS cohort
        FROM fact_basket b JOIN f
          ON f.customer_key = b.customer_key AND f.first_ts = b.txn_ts
        WHERE NOT b.is_return {sf}
    """)

    months = con.execute("""
        SELECT DISTINCT cohort FROM first_basket ORDER BY cohort
    """).df().cohort.tolist()
    if len(months) <= BURN_IN_MONTHS + 2:
        print("Not enough history for cohort analysis.")
        return 1
    valid = months[BURN_IN_MONTHS:]

    # A trailing partial month is kept and compared per day rather than
    # dropped — see acquire.py. Raw counts stay; only trend maths normalises.
    _mx = pd.Timestamp(
        con.execute("SELECT MAX(txn_ts) FROM fact_basket").fetchone()[0])
    _end = (_mx.replace(day=1) + pd.offsets.MonthEnd(1))
    partial_month = (_mx.strftime("%Y-%m")
                     if _mx.date() < _end.date() else None)
    days_in = con.execute(f"""
        SELECT strftime(txn_ts, '%Y-%m') AS m,
               COUNT(DISTINCT date_key)  AS days
        FROM fact_basket WHERE NOT is_return {sf} GROUP BY 1
    """).df().set_index("m")["days"].to_dict()

    # ---- 1. new customer volume ----------------------------------------
    header(f"1. NEW CUSTOMER VOLUME — {label}")
    vol = con.execute(f"""
        WITH n AS (
            SELECT cohort, COUNT(DISTINCT customer_key) AS new_cust
            FROM first_basket GROUP BY 1
        ),
        a AS (
            SELECT strftime(txn_ts,'%Y-%m') AS cohort,
                   COUNT(DISTINCT customer_key) AS active
            FROM fact_basket WHERE NOT is_return
              AND customer_key IS NOT NULL {sf}{idf}
            GROUP BY 1
        )
        SELECT a.cohort, n.new_cust, a.active,
               n.new_cust::DOUBLE / a.active AS new_share
        FROM a JOIN n USING (cohort) ORDER BY a.cohort
    """).df()

    vol["days"] = [days_in.get(m, 30) for m in vol.cohort]
    vol["new_per_day"] = vol.new_cust / vol.days

    print(f"  {'Month':<10}{'Active':>10}{'New':>10}{'Days':>6}"
          f"{'New/day':>9}{'% new':>8}   note")
    hr()
    for _, r in vol.iterrows():
        note = ""
        if r.cohort not in valid:
            note = "  <- burn-in, excluded"
        elif r.cohort == partial_month:
            note = "  <- partial month"
        print(f"  {r.cohort:<10}{int(r.active):>10,}{int(r.new_cust):>10,}"
              f"{int(r.days):>6}{r.new_per_day:>9,.0f}"
              f"{r.new_share*100:>7.1f}%{note}")

    v = vol[vol.cohort.isin(valid)]
    if len(v) >= 6:
        f3, l3 = v.head(3), v.tail(3)
        print()
        print("  Compared per day, so a short month is not read as a decline:")
        ab, al = f3.active.mean() / f3.days.mean(), l3.active.mean() / l3.days.mean()
        nb, nl = f3.new_per_day.mean(), l3.new_per_day.mean()
        print(f"  Active per day  {ab:>8,.0f} -> {al:>8,.0f}"
              f"   {(al/ab-1)*100:+.1f}%")
        print(f"  New per day     {nb:>8,.0f} -> {nl:>8,.0f}"
              f"   {(nl/nb-1)*100:+.1f}%")
        print(f"  New per 30 days {nb*30:>8,.0f} -> {nl*30:>8,.0f}")

    # ---- 2. first-basket category mix ----------------------------------
    header("2. FIRST-BASKET MIX — what new customers buy on day one")
    mix = con.execute(f"""
        WITH fl AS (
            SELECT fb.cohort, fb.customer_key, l.category
            FROM first_basket fb
            JOIN fact_line l ON l.basket_id = fb.basket_id
            WHERE NOT l.is_return
            GROUP BY 1,2,3
        ),
        n AS (SELECT cohort, COUNT(DISTINCT customer_key) tot
              FROM first_basket GROUP BY 1)
        SELECT fl.cohort, fl.category,
               COUNT(DISTINCT fl.customer_key)::DOUBLE / n.tot AS adopt
        FROM fl JOIN n USING (cohort)
        GROUP BY 1,2,n.tot ORDER BY 1,2
    """).df()

    piv = mix[mix.cohort.isin(valid)].pivot(
        index="cohort", columns="category", values="adopt")
    sizes = vol.set_index("cohort").new_cust
    show = [c for c in cats if c in piv.columns]
    print("  Share of new customers whose FIRST basket contained the category")
    print()
    print(f"  {'Month':<10}{'Cohort':>9}" + "".join(f"{c:>12}" for c in show))
    hr()
    kept = []
    for m, row in piv.iterrows():
        n = int(sizes.get(m, 0))
        if n < MIN_COHORT:
            print(f"  {m:<10}{n:>9,}" +
                  "".join(f"{'—':>12}" for _ in show) + "   too small")
            continue
        kept.append(m)
        cells = ""
        for c in show:
            v = row.get(c)
            cells += f"{'—':>12}" if pd.isna(v) else f"{v*100:>11.1f}%"
        print(f"  {m:<10}{n:>9,}{cells}")
    ok = piv.loc[kept] if kept else piv.iloc[0:0]
    if len(ok) >= 6:
        f3, l3 = ok.head(3), ok.tail(3)
        print()
        print(f"  {'change':<19}" +
              "".join(f"{(l3[c].mean()-f3[c].mean())*100:>+10.1f}pp" for c in show))
    elif len(ok) < 3:
        print()
        print(f"  Too few cohorts above {MIN_COHORT} customers to read a trend.")

    # ---- 3. new vs returning -------------------------------------------
    header("3. WHERE EACH CATEGORY'S BUYERS COME FROM")
    src = con.execute(f"""
        WITH f AS (
            SELECT customer_key, MIN(txn_ts) first_ts
            FROM fact_basket WHERE NOT is_return
              AND customer_key IS NOT NULL {sf}{idf}
            GROUP BY 1
        ),
        m AS (
            SELECT strftime(l.txn_ts,'%Y-%m') AS mth, l.category, l.customer_key,
                   CASE WHEN strftime(f.first_ts,'%Y-%m')
                             = strftime(l.txn_ts,'%Y-%m')
                        THEN 1 ELSE 0 END AS is_new
            FROM fact_line l JOIN f USING (customer_key)
            WHERE NOT l.is_return {sf}
            GROUP BY 1,2,3,4
        )
        SELECT mth, category,
               COUNT(DISTINCT customer_key) buyers,
               COUNT(DISTINCT CASE WHEN is_new=1 THEN customer_key END) new_buyers
        FROM m GROUP BY 1,2 ORDER BY 1,2
    """).df()
    src = src[src.mth.isin(valid)]

    for cat in cats:
        d = src[src.category == cat].copy()
        if len(d) < 6:
            continue
        d["ret"] = d.buyers - d.new_buyers
        f3, l3 = d.head(3), d.tail(3)
        dn = (l3.new_buyers.mean() / f3.new_buyers.mean() - 1) * 100
        dr = (l3.ret.mean() / f3.ret.mean() - 1) * 100
        db = (l3.buyers.mean() / f3.buyers.mean() - 1) * 100
        print(f"  {cat:<12} buyers {db:>+6.1f}%   "
              f"new {dn:>+6.1f}%   returning {dr:>+6.1f}%")

    print()
    print("  A category whose NEW buyers fall faster than its returning buyers")
    print("  has an acquisition problem. The reverse is a retention problem.")

    # ---- 4. cohort adoption over time -----------------------------------
    header("4. DO COHORTS KEEP BUYING THE CATEGORY?")
    coh = con.execute(f"""
        WITH f AS (
            SELECT customer_key, MIN(txn_ts) first_ts
            FROM fact_basket WHERE NOT is_return
              AND customer_key IS NOT NULL {sf}{idf}
            GROUP BY 1
        ),
        sized AS (SELECT strftime(first_ts,'%Y-%m') coh, COUNT(*) n
                  FROM f GROUP BY 1),
        buys AS (
            SELECT strftime(f.first_ts,'%Y-%m') AS coh,
                   l.category,
                   date_diff('month', f.first_ts, l.txn_ts) AS age,
                   COUNT(DISTINCT l.customer_key) AS buyers
            FROM fact_line l JOIN f USING (customer_key)
            WHERE NOT l.is_return {sf}
            GROUP BY 1,2,3
        )
        SELECT b.coh, b.category, b.age,
               b.buyers::DOUBLE / s.n AS rate
        FROM buys b JOIN sized s ON s.coh = b.coh
        WHERE b.age BETWEEN 0 AND 5 ORDER BY 1,2,3
    """).df()
    coh = coh[coh.coh.isin(valid)]

    for cat in cats[:2]:
        d = coh[coh.category == cat]
        if d.empty:
            continue
        p = d.pivot_table(index="coh", columns="age", values="rate")
        p = p[[c for c in range(6) if c in p.columns]]
        print(f"\n  {cat} — share of each cohort buying it, by months since first visit")
        print(f"  {'Cohort':<10}" + "".join(f"{'m'+str(a):>9}" for a in p.columns))
        hr()
        for c, row in p.iterrows():
            n = int(sizes.get(c, 0))
            if n < MIN_COHORT:
                continue
            print(f"  {c:<10}" + "".join(
                "        —" if pd.isna(row[a]) else f"{row[a]*100:>8.1f}%"
                for a in p.columns))

    # ---- verdict --------------------------------------------------------
    header("READ")
    print("  Compare section 3's new vs returning columns.")
    print()
    print("  If a category's NEW buyers are falling much faster than its")
    print("  returning buyers, the category is failing to attract first-time")
    print("  customers. That is a menu-position, pricing or budtender-script")
    print("  problem, not an assortment or loyalty one.")
    print()
    print("  Caveats: cohort months are measured inside the loaded window, so")
    print("  the first two are excluded as burn-in. customer_key is a name")
    print("  hash for non-redeemers, which undercounts new customers. Treat")
    print("  the direction as informative and the levels as approximate.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
