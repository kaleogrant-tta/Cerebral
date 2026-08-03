"""
Weekly Category Scorecard.

    python scorecard.py                     # latest complete week, all stores
    python scorecard.py --store DTBK        # one store
    python scorecard.py --week 2026-W22     # a specific week

Implements the framework's sections 2, 3 and 5:
  Panel A  category scorecard, normalised per 100 baskets
  Panel B  control-limit alerts and run rules
  Panel C  channel index
  Panel D  loyalty

Everything is expressed as a rate rather than a raw total, so traffic swings
do not masquerade as category swings.
"""

from __future__ import annotations

import argparse
import sys

import duckdb
import pandas as pd

STORES = {1: "DTBK", 2: "5AVE", 3: "SOHO", 4: "USQ"}
BASELINE_WEEKS = 13
TREND_WEEKS = 8


def hr(c="-", n=78):
    print(c * n)


def fmt(v, dp=1, suffix=""):
    if pd.isna(v):
        return "—"
    return f"{v:,.{dp}f}{suffix}"


# ---------------------------------------------------------------------------

def latest_week(con, store_filter):
    row = con.execute(f"""
        SELECT iso_year, iso_week, COUNT(DISTINCT date_key) AS days
        FROM fact_basket WHERE NOT is_return {store_filter}
        GROUP BY 1,2 HAVING days >= 7
        ORDER BY iso_year DESC, iso_week DESC LIMIT 1
    """).fetchone()
    if not row:
        row = con.execute(f"""
            SELECT iso_year, iso_week FROM fact_basket
            WHERE NOT is_return {store_filter}
            GROUP BY 1,2 ORDER BY 1 DESC, 2 DESC LIMIT 1
        """).fetchone()
    return int(row[0]), int(row[1])


def week_series(con, store_filter):
    """Category x week, normalised. One row per category-week."""
    return con.execute(f"""
        WITH bw AS (
            SELECT iso_year, iso_week,
                   COUNT(*) AS baskets,
                   COUNT(DISTINCT date_key) AS days_open,
                   SUM(basket_net) AS net_all
            FROM fact_basket WHERE NOT is_return {store_filter}
            GROUP BY 1,2
        ),
        cw AS (
            SELECT iso_year, iso_week, category,
                   SUM(net_sales)    AS net,
                   SUM(gross_margin) AS gm,
                   SUM(units)        AS units,
                   COUNT(DISTINCT basket_id) AS baskets_with
            FROM fact_line WHERE NOT is_return {store_filter}
            GROUP BY 1,2,3
        )
        SELECT cw.iso_year, cw.iso_week, cw.category,
               cw.net, cw.gm, cw.units, cw.baskets_with,
               bw.baskets, bw.days_open, bw.net_all,
               cw.baskets_with::DOUBLE / bw.baskets              AS penetration,
               cw.net / bw.baskets * 100                         AS per100,
               cw.gm / NULLIF(cw.net,0)                          AS margin_pct
        FROM cw JOIN bw USING (iso_year, iso_week)
        ORDER BY cw.iso_year, cw.iso_week, cw.category
    """).df()


def control_limits(hist: pd.Series, n_baskets: float):
    """Binomial control limits on penetration (framework 3.2)."""
    if len(hist) < 4 or n_baskets <= 0:
        return None, None, None
    p = hist.mean()
    se = (p * (1 - p) / n_baskets) ** 0.5
    return p, p - 2 * se, p + 2 * se


def run_rules(hist: pd.Series) -> str | None:
    """7 consecutive on one side, or 6 consecutive in one direction."""
    if len(hist) < 7:
        return None
    base = hist.iloc[:-1].mean()
    last7 = hist.tail(7)
    if (last7 > base).all():
        return "7 consecutive weeks above baseline"
    if (last7 < base).all():
        return "7 consecutive weeks below baseline"
    last6 = hist.tail(6)
    d = last6.diff().dropna()
    if len(d) >= 5 and (d > 0).all():
        return "6 consecutive weeks rising"
    if len(d) >= 5 and (d < 0).all():
        return "6 consecutive weeks falling"
    return None


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tta.duckdb")
    ap.add_argument("--store", help="DTBK / 5AVE / SOHO / USQ")
    ap.add_argument("--week", help="e.g. 2026-W22")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)

    sf = ""
    label = "ALL STORES"
    if args.store:
        key = next((k for k, v in STORES.items() if v == args.store.upper()), None)
        if not key:
            print(f"Unknown store {args.store}. Use one of {list(STORES.values())}")
            return 1
        sf = f" AND store_key = {key}"
        label = args.store.upper()

    if args.week:
        y, w = args.week.upper().split("-W")
        yr, wk = int(y), int(w)
    else:
        yr, wk = latest_week(con, sf)

    df = week_series(con, sf)
    if df.empty:
        print("No data in the database.")
        return 1

    cur = df[(df.iso_year == yr) & (df.iso_week == wk)]
    if cur.empty:
        print(f"No data for {yr}-W{wk:02d}.")
        return 1

    # ordered list of weeks up to the reporting week
    weeks = (df[["iso_year", "iso_week"]].drop_duplicates()
               .sort_values(["iso_year", "iso_week"]).reset_index(drop=True))
    idx = weeks[(weeks.iso_year == yr) & (weeks.iso_week == wk)].index
    pos = int(idx[0]) if len(idx) else len(weeks) - 1
    prev = weeks.iloc[pos - 1] if pos > 0 else None
    hist_weeks = weeks.iloc[max(0, pos - BASELINE_WEEKS):pos]

    tot_baskets = cur.baskets.iloc[0]
    tot_net = cur.net.sum()
    days = cur.days_open.iloc[0]

    # ---- header --------------------------------------------------------
    print()
    hr("=")
    print(f"  WEEKLY CATEGORY SCORECARD — {label} — {yr}-W{wk:02d}"
          f"   ({days} trading day(s))")
    hr("=")

    if prev is not None:
        p = df[(df.iso_year == prev.iso_year) & (df.iso_week == prev.iso_week)]
        pb, pn = p.baskets.iloc[0], p.net.sum()
        print(f"  Baskets     {tot_baskets:>9,}    vs prior week "
              f"{(tot_baskets/pb-1)*100:+.1f}%")
        print(f"  Net sales   {tot_net:>9,.0f}    vs prior week "
              f"{(tot_net/pn-1)*100:+.1f}%")
        print(f"  Avg basket  {tot_net/tot_baskets:>9,.2f}    vs prior week "
              f"{((tot_net/tot_baskets)/(pn/pb)-1)*100:+.1f}%")
    else:
        print(f"  Baskets {tot_baskets:,}   Net sales {tot_net:,.0f}   "
              f"Avg basket {tot_net/tot_baskets:,.2f}")

    # ---- Panel A -------------------------------------------------------
    print()
    print("  PANEL A — CATEGORY SCORECARD")
    hr()
    print(f"  {'Category':<13}{'Net $':>11}{'% tot':>7}{'$/100bkt':>10}"
          f"{'ΔWoW':>8}{'Pen %':>8}{'ΔPen':>8}{'GM%':>7}{'Trend':>9}")
    hr()

    alerts: list[str] = []
    for _, r in cur.sort_values("net", ascending=False).iterrows():
        cat = r.category
        h = df[(df.category == cat) & df.set_index(["iso_year", "iso_week"]).index
               .isin(list(zip(hist_weeks.iso_year, hist_weeks.iso_week)))]
        h = h.sort_values(["iso_year", "iso_week"])

        d_per100 = d_pen = float("nan")
        if prev is not None:
            pr = df[(df.category == cat) & (df.iso_year == prev.iso_year)
                    & (df.iso_week == prev.iso_week)]
            if len(pr):
                d_per100 = (r.per100 / pr.per100.iloc[0] - 1) * 100
                d_pen = (r.penetration - pr.penetration.iloc[0]) * 100

        trend = ""
        series = pd.concat([h.per100, pd.Series([r.per100])]).tail(TREND_WEEKS)
        if len(series) >= 4:
            x = range(len(series))
            slope = pd.Series(list(series)).corr(pd.Series(list(x)))
            if pd.notna(slope):
                trend = f"{slope*100:+.0f}"

        print(f"  {cat:<13}{r.net:>11,.0f}{r.net/tot_net*100:>6.1f}%"
              f"{r.per100:>10,.0f}{fmt(d_per100,1,'%'):>8}"
              f"{r.penetration*100:>7.1f}%{fmt(d_pen,1,'pp'):>8}"
              f"{r.margin_pct*100:>6.1f}%{trend:>9}")

        # control limits on penetration
        if len(h) >= 4:
            base, lcl, ucl = control_limits(h.penetration, r.baskets)
            if base is not None:
                if r.penetration < lcl:
                    alerts.append(
                        f"{cat}: penetration {r.penetration*100:.1f}% is BELOW "
                        f"lower control limit {lcl*100:.1f}% (baseline {base*100:.1f}%)")
                elif r.penetration > ucl:
                    alerts.append(
                        f"{cat}: penetration {r.penetration*100:.1f}% is ABOVE "
                        f"upper control limit {ucl*100:.1f}% (baseline {base*100:.1f}%)")
            rr = run_rules(pd.concat([h.penetration, pd.Series([r.penetration])]))
            if rr:
                alerts.append(f"{cat}: {rr} on penetration")

    # ---- Panel B -------------------------------------------------------
    print()
    print("  PANEL B — ALERTS")
    hr()
    if not alerts:
        n = len(hist_weeks)
        print(f"  No control-limit breaches or run-rule signals "
              f"({n} week baseline).")
        if n < 8:
            print("  Baseline is short; alerts get more reliable past 8 weeks.")
    else:
        for a in alerts:
            print(f"  !  {a}")

    # ---- Panel C -------------------------------------------------------
    ch = con.execute(f"""
        WITH t AS (
            SELECT channel, category, SUM(net_sales) net
            FROM fact_line
            WHERE NOT is_return AND iso_year = {yr} AND iso_week = {wk} {sf}
            GROUP BY 1,2
        )
        SELECT category, channel, net,
               net / SUM(net) OVER (PARTITION BY channel)  AS share_in_ch,
               SUM(net) OVER (PARTITION BY category) / SUM(net) OVER () AS share_all
        FROM t
    """).df()

    if not ch.empty:
        ch["index"] = ch.share_in_ch / ch.share_all * 100
        piv = ch.pivot_table(index="category", columns="channel",
                             values="index", aggfunc="first")
        order = cur.sort_values("net", ascending=False).category.tolist()
        piv = piv.reindex([c for c in order if c in piv.index])
        print()
        print("  PANEL C — CHANNEL INDEX   (100 = neutral, >115 over-indexes)")
        hr()
        cols = [c for c in ["In-Store", "Non-Stop", "Delivery"] if c in piv.columns]
        print(f"  {'Category':<13}" + "".join(f"{c:>12}" for c in cols))
        hr()
        for cat, row in piv.iterrows():
            cells = ""
            for c in cols:
                v = row.get(c)
                mark = "*" if pd.notna(v) and (v >= 115 or v <= 85) else " "
                cells += f"{fmt(v,0):>11}{mark}"
            print(f"  {cat:<13}{cells}")

    # ---- Panel D -------------------------------------------------------
    loy = con.execute(f"""
        SELECT channel,
               COUNT(*) AS baskets,
               SUM(CASE WHEN used_redemption THEN 1 ELSE 0 END) AS redeemed,
               SUM(loyalty_redeem) AS redeem_val,
               AVG(CASE WHEN used_redemption THEN basket_net END) AS avg_redeem_bkt,
               AVG(CASE WHEN NOT used_redemption THEN basket_net END) AS avg_other_bkt
        FROM fact_basket
        WHERE NOT is_return AND iso_year = {yr} AND iso_week = {wk} {sf}
        GROUP BY 1 ORDER BY baskets DESC
    """).df()

    if not loy.empty:
        print()
        print("  PANEL D — LOYALTY")
        hr()
        print(f"  {'Channel':<13}{'Baskets':>10}{'Redeem':>9}{'Rate':>8}"
              f"{'Value $':>11}{'Redeem bkt':>12}{'Other bkt':>11}")
        hr()
        for _, r in loy.iterrows():
            rate = r.redeemed / r.baskets * 100 if r.baskets else 0
            print(f"  {r.channel:<13}{r.baskets:>10,}{int(r.redeemed):>9,}"
                  f"{rate:>7.1f}%{r.redeem_val:>11,.0f}"
                  f"{fmt(r.avg_redeem_bkt,2):>12}{fmt(r.avg_other_bkt,2):>11}")
        tot_r = loy.redeemed.sum()
        print()
        print(f"  Redemption reached {tot_r/loy.baskets.sum()*100:.1f}% of baskets, "
              f"${loy.redeem_val.sum():,.0f} "
              f"({loy.redeem_val.sum()/tot_net*100:.1f}% of net sales).")
        print("  Redeemer baskets are larger by construction (offers require a")
        print("  specific purchase) — not evidence of incremental lift.")

    print()
    hr("=")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
