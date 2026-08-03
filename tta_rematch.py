"""Re-match redemptions against the current matcher.

Two modes:

  default    Re-runs attribution ONLY for rows still unmatched
             (matched_brand IS NULL). Safe top-up after incremental loads.

  --full     Re-runs attribution for EVERY row. Needed after a matcher
             upgrade, because rows matched WRONG by the old matcher (an
             eighth credited against an ounce offer, a .5g cart against a
             1g offer) are not null — only a full pass repairs them. Rows
             that no longer match are cleared back to unmatched rather than
             left on a stale guess.

Both modes fall back to the penny line (method = "substituted-line") when
the matcher finds nothing: substituted redemptions ("Travel Club 200 Points
Substitution", and named offers whose menu item was out of stock) put a
DIFFERENT product in the basket than the offer names, and the substitute
rings at $0.01 or is discounted to roughly its tax value.

  --pull     Download the latest tta.duckdb from the Drive state folder
             before running. Always use this — a stale local copy followed
             by publish --upload would roll the dashboard backwards.

A "TEST GWP TEST" campaign left test rows in the table. They are archived
to CSV and removed from fact_redemption (in --full mode, all of them, not
just unmatched ones).

Usage:
    python tta_rematch.py --pull --full
    python tta_rematch.py --db C:\path\to\tta.duckdb

Afterwards, rebuild + republish the dashboard:  python publish.py --upload
"""

import argparse
import os
import re
import shutil
from pathlib import Path

import duckdb
import pandas as pd

from tta_etl import _tokens, attribute_offer

PENNY_MAX = 0.05        # a substituted item usually rings at $0.01 (or $0.00)
TAX_RATE = 0.15         # ...but staff may discount it only to its tax value:
                        # the line keeps ~13% NY cannabis tax on the full price
CHEAP_CAP = 40.00       # never trust a "substitute" line above this
TEST_OFFER = "TEST GWP TEST"


def pick_substitute(lines: pd.DataFrame, redeem_amt: float):
    """Find the swapped-in product inside a substitution basket.

    The redeemer pays (almost) nothing for the item, so it is the line that
    costs nothing: a $0.01 penny line when there is one, otherwise the
    cheapest line up to roughly the tax on the item's value (13% NY tax on
    what the redemption was worth), capped at CHEAP_CAP so an unrelated
    full-price purchase is never mistaken for the substitute.
    """
    priced = lines[lines["net_sales"].fillna(-1) >= 0].sort_values("net_sales")
    penny = priced[priced["net_sales"] <= PENNY_MAX]
    if len(penny):
        return penny.iloc[0]
    ceiling = min(max(redeem_amt or 0, 0) * TAX_RATE, CHEAP_CAP)
    cheap = priced[priced["net_sales"] <= ceiling]
    if len(cheap):
        return cheap.iloc[0]
    return None


def pull_latest(db: Path) -> bool:
    """Download the freshest database from the Drive state folder."""
    from tta_config import DRIVE
    from tta_drive import DriveClient
    from tta_env import bootstrap
    bootstrap()
    state_id = os.environ.get(DRIVE["state_folder_env"])
    if not state_id:
        print(f"ERROR: {DRIVE['state_folder_env']} is not set")
        return False
    drive = DriveClient()
    existing = drive.find(state_id, DRIVE["db_filename"])
    if not existing:
        print("ERROR: no database in the Drive state folder")
        return False
    drive.download(existing["id"], db)
    print(f"  pulled latest {DRIVE['db_filename']} from Drive "
          f"({db.stat().st_size/1e6:.1f} MB)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tta.duckdb")
    ap.add_argument("--full", action="store_true",
                    help="re-match EVERY row, repairing wrong historical "
                         "matches, not just filling unmatched ones")
    ap.add_argument("--pull", action="store_true",
                    help="download the latest database from Drive first")
    args = ap.parse_args()

    db = Path(args.db)
    if args.pull and not pull_latest(db):
        return 1
    if not db.exists():
        print(f"ERROR: {db} not found")
        return 1

    backup = db.with_name(db.stem + "_backup_rematch.duckdb")
    if not backup.exists():
        shutil.copy(db, backup)
        print(f"  backup written: {backup.name}")

    con = duckdb.connect(str(db))
    where = "" if args.full else "WHERE matched_brand IS NULL"
    red = con.execute(f"""
        SELECT basket_id, offer_id, offer_name, redeem_amt
        FROM fact_redemption
        {where}
    """).df()
    scope = "ALL redemptions" if args.full else "unmatched redemptions"
    print(f"  re-examining {scope}: {len(red):,} "
          f"(${red['redeem_amt'].sum():,.0f})")
    if red.empty:
        print("Nothing to do.")
        con.close()
        return 0

    # --- 1. archive + remove TEST rows ------------------------------------
    if args.full:
        test_rows = con.execute(
            "SELECT * FROM fact_redemption WHERE offer_name = ?",
            [TEST_OFFER]).df()
    else:
        test_rows = red[red["offer_name"].astype(str) == TEST_OFFER]
    if len(test_rows):
        out_csv = db.with_name("test_gwp_rows_archived.csv")
        test_rows.to_csv(out_csv, index=False)
        con.execute("DELETE FROM fact_redemption WHERE offer_name = ?"
                    + ("" if args.full else " AND matched_brand IS NULL"),
                    [TEST_OFFER])
        print(f"  archived + removed {len(test_rows):,} '{TEST_OFFER}' "
              f"rows -> {out_csv.name}")
    red = red[red["offer_name"].astype(str) != TEST_OFFER]

    # --- 2. re-match with the current matcher + penny fallback ------------
    line = con.execute(
        "SELECT basket_id, brand, category, product, net_sales "
        "FROM fact_line").df()
    catalogue = {frozenset(_tokens(b)): b
                 for b in line["brand"].dropna().unique() if _tokens(b)}
    by_basket = {bid: g for bid, g in line.groupby("basket_id")}

    updates = []
    skipped = 0
    for r in red.itertuples(index=False):
        lines = by_basket.get(r.basket_id)
        if lines is None or lines.empty:
            skipped += 1                      # basket not in fact_line; leave as-is
            continue
        brand, catg, prod, method = attribute_offer(
            r.offer_name, lines[["brand", "category", "product"]], catalogue)
        if method == "unmatched":
            ln = pick_substitute(lines, r.redeem_amt)
            if ln is not None:
                brand, catg, prod = ln["brand"], ln["category"], ln["product"]
                method = "substituted-line"
        if method == "unmatched" and len(lines) == 1 and re.search(
                r"[0-9]+ *points? +substitution", str(r.offer_name).lower()):
            # A substitution redemption in a one-line basket: whatever that
            # line is, it is what the customer left with, even when it was
            # rung above the tax ceiling.
            ln = lines.iloc[0]
            brand, catg, prod = ln["brand"], ln["category"], ln["product"]
            method = "substituted-line"
        if method != "unmatched" and pd.isna(brand):
            method = "unmatched"
        updates.append({
            "basket_id": r.basket_id, "offer_id": r.offer_id,
            "offer_name": r.offer_name, "redeem_amt": r.redeem_amt,
            "brand": brand if method != "unmatched" else None,
            "category": catg if method != "unmatched" else None,
            "product": prod if method != "unmatched" else None,
            "method": method,
        })

    # --- 3. write back -----------------------------------------------------
    if updates:
        stage = pd.DataFrame(updates)
        con.register("stage_fix", stage)
        null_guard = ("" if args.full
                      else "AND fact_redemption.matched_brand IS NULL")
        con.execute(f"""
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
              {null_guard}
        """)

    fixed = pd.DataFrame(updates) if updates else pd.DataFrame()
    got = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(redeem_amt),0) FROM fact_redemption "
        "WHERE matched_brand IS NULL").fetchone()
    if skipped:
        print(f"  left untouched (basket missing from fact_line): {skipped:,}")
    if len(fixed):
        bym = fixed.groupby("method")["redeem_amt"].agg(["count", "sum"])
        print(f"\n  results by method:")
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
    print("\nDone. Rebuild the dashboard with:  python publish.py --upload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
