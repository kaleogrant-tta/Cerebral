"""Re-match redemptions that are still unattributed (matched_brand IS NULL).

Why this exists: the July 2026 backfill attributed offers with the matcher as
it was then. Since, three things surfaced:

  1. Alpine misspells "Loyalty" as "Loytaly" (Aug-Sep 2025), and plurals
     differ between offer names and POS product names ("Doobies" / "Doobie").
     Both are now handled in tta_etl._tokens.
  2. Substituted redemptions ("Travel Club 200 Points Substitution", and
     named offers whose menu item was out of stock) put a DIFFERENT product
     in the basket than the offer names. The substitute rings at $0.01, so
     the penny line identifies what the customer actually received.
  3. A "TEST GWP TEST" campaign left test rows in the table. They are
     archived to CSV and removed from fact_redemption.

What it does: re-runs attribution ONLY for rows still unmatched (attributed
rows are never touched), first with the improved matcher, then with the
penny-line fallback (method = "substituted-line"). Safe to re-run.

Usage:
    python tta_rematch.py --db C:\\Users\\User\\cerebral\\tta.duckdb

Afterwards, rebuild + republish the dashboard (publish.py --upload).
"""

import argparse
import shutil
from pathlib import Path

import duckdb
import pandas as pd

from tta_etl import _tokens, attribute_offer

PENNY_MAX = 0.05        # a redeemed/substituted item rings at $0.01 (or $0.00)
TEST_OFFER = "TEST GWP TEST"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tta.duckdb")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: {db} not found")
        return 1

    backup = db.with_name(db.stem + "_backup_rematch.duckdb")
    if not backup.exists():
        shutil.copy(db, backup)
        print(f"  backup written: {backup.name}")

    con = duckdb.connect(str(db))
    red = con.execute("""
        SELECT basket_id, offer_id, offer_name, redeem_amt
        FROM fact_redemption
        WHERE matched_brand IS NULL
    """).df()
    print(f"  unmatched redemptions to re-examine: {len(red):,} "
          f"(${red['redeem_amt'].sum():,.0f})")
    if red.empty:
        print("Nothing to do.")
        con.close()
        return 0

    # --- 1. archive + remove TEST rows ------------------------------------
    is_test = red["offer_name"].astype(str) == TEST_OFFER
    if is_test.any():
        out_csv = db.with_name("test_gwp_rows_archived.csv")
        red[is_test].to_csv(out_csv, index=False)
        con.execute(
            "DELETE FROM fact_redemption WHERE matched_brand IS NULL "
            "AND offer_name = ?", [TEST_OFFER])
        print(f"  archived + removed {int(is_test.sum()):,} '{TEST_OFFER}' "
              f"rows -> {out_csv.name}")
    red = red[~is_test]

    # --- 2. re-match with the improved matcher + penny fallback -----------
    line = con.execute(
        "SELECT basket_id, brand, category, product, net_sales "
        "FROM fact_line").df()
    catalogue = {frozenset(_tokens(b)): b
                 for b in line["brand"].dropna().unique() if _tokens(b)}
    by_basket = {bid: g for bid, g in line.groupby("basket_id")}

    updates = []
    for r in red.itertuples(index=False):
        lines = by_basket.get(r.basket_id)
        if lines is None or lines.empty:
            continue
        brand, catg, prod, method = attribute_offer(
            r.offer_name, lines[["brand", "category", "product"]], catalogue)
        if method == "unmatched":
            penny = lines[(lines["net_sales"].fillna(99) >= 0)
                          & (lines["net_sales"] <= PENNY_MAX)]
            if len(penny):
                ln = penny.iloc[0]
                brand, catg, prod = ln["brand"], ln["category"], ln["product"]
                method = "substituted-line"
        if method != "unmatched" and pd.notna(brand):
            updates.append({
                "basket_id": r.basket_id, "offer_id": r.offer_id,
                "offer_name": r.offer_name, "redeem_amt": r.redeem_amt,
                "brand": brand, "category": catg, "product": prod,
                "method": method,
            })

    # --- 3. write back -----------------------------------------------------
    if updates:
        stage = pd.DataFrame(updates)
        con.register("stage_fix", stage)
        con.execute("""
            UPDATE fact_redemption
            SET matched_brand    = s.brand,
                matched_category = s.category,
                matched_product  = s.product,
                match_method     = s.method
            FROM stage_fix s
            WHERE fact_redemption.basket_id  = s.basket_id
              AND fact_redemption.offer_id   = s.offer_id
              AND fact_redemption.offer_name = s.offer_name
              AND ROUND(fact_redemption.redeem_amt, 2)
                  = ROUND(s.redeem_amt, 2)
              AND fact_redemption.matched_brand IS NULL
        """)

    recovered = sum(u["redeem_amt"] for u in updates)
    got = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(redeem_amt),0) FROM fact_redemption "
        "WHERE matched_brand IS NULL").fetchone()
    print(f"\n  newly attributed: {len(updates):,} "
          f"(${recovered:,.0f} recovered)")
    if updates:
        fixed = pd.DataFrame(updates)
        bym = fixed.groupby("method")["redeem_amt"].agg(["count", "sum"])
        print(bym.to_string())
    print(f"\n  still unmatched: {got[0]:,} (${got[1]:,.0f}) — "
          "this is what the dashboard note will now report")

    # --- 4. show what remains, with a peek inside one basket each ---------
    left = con.execute("""
        SELECT offer_name, COUNT(*) AS n, ROUND(SUM(redeem_amt), 2) AS dollars
        FROM fact_redemption WHERE matched_brand IS NULL
        GROUP BY 1 ORDER BY dollars DESC LIMIT 12
    """).df()
    if len(left):
        print("\n  top remaining unmatched offers:")
        print(left.to_string(index=False))
        print("\n  peek inside one basket per top offer "
              "(what the customer actually bought):")
        for name in left["offer_name"].head(5):
            bid = con.execute(
                "SELECT basket_id FROM fact_redemption "
                "WHERE matched_brand IS NULL AND offer_name = ? LIMIT 1",
                [name]).fetchone()
            if not bid:
                continue
            basket = con.execute(
                "SELECT brand, product, net_sales FROM fact_line "
                "WHERE basket_id = ?", [bid[0]]).df()
            print(f"\n  {name}  (basket {bid[0]}):")
            print(basket.to_string(index=False))

    con.close()
    print("\nDone. Rebuild the dashboard with:  python Cerebral\\publish.py --upload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
