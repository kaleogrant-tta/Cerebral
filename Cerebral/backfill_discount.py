"""
backfill_discount.py -- populate fact_basket.discount_amt from the POS
exports already archived in history/.

    python backfill_discount.py              dry run, shows coverage
    python backfill_discount.py --apply      writes to the database

Thirteen months of POS Transactions By Register exports are sitting in
history/, four stores each. They have always carried DiscountAmt at basket
level; the ETL never read it. This walks every archived file, maps
PosId -> basket_id, and writes the discount onto the existing baskets.

No re-run of the ETL is needed: nothing else about those baskets changes.
A timestamped backup is taken before any write.

Run wire_discount.py first — it adds the column. Run publish.py after.
"""

from __future__ import annotations

import datetime
import glob
import os
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd

DB = Path(os.path.expanduser("~/cerebral/tta.duckdb"))
APPLY = "--apply" in sys.argv

# The POS export puts four metadata rows above the header. read_export in
# tta_etl.py probes for this; here it is pinned, since the layout is known
# and importing the ETL just for the probe pulls in the whole pipeline.
HEADER_ROW = 4


def load_one(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, header=HEADER_ROW)
    cols = {str(c).strip(): c for c in df.columns}
    if "PosId" not in cols or "DiscountAmt" not in cols:
        return pd.DataFrame()
    out = pd.DataFrame({
        "basket_id": pd.to_numeric(df[cols["PosId"]], errors="coerce"),
        "discount_amt": pd.to_numeric(
            df[cols["DiscountAmt"]], errors="coerce").fillna(0.0),
    })
    if "PosStatus" in cols:
        out["status"] = df[cols["PosStatus"]].astype(str)
        out = out[out.status.str.lower() != "returned"]
    return out.dropna(subset=["basket_id"])[["basket_id", "discount_amt"]]


def main() -> int:
    files = sorted(Path(p) for p in
                   glob.glob("history/**/*POS Transactions*.xlsx",
                             recursive=True))
    if not files:
        print("No POS exports found under history/")
        return 1

    print(f"DB    : {DB}  ({DB.stat().st_size/1e6:.1f} MB)")
    print(f"MODE  : {'APPLY' if APPLY else 'DRY RUN'}")
    print(f"FILES : {len(files)}")
    print()

    frames = []
    for f in files:
        try:
            d = load_one(f)
        except Exception as exc:                       # noqa: BLE001
            print(f"  !! {f.name}: {exc}")
            continue
        if d.empty:
            print(f"  -- {f.name}: no PosId/DiscountAmt columns")
            continue
        frames.append(d)
        nz = int((d.discount_amt > 0).sum())
        print(f"  {f.parent.name}/{f.name[:52]:<52} "
              f"{len(d):>7,} rows  {nz:>6,} discounted  "
              f"${d.discount_amt.sum():>12,.2f}")

    if not frames:
        print("\nNothing loadable.")
        return 1

    all_disc = pd.concat(frames, ignore_index=True)
    before = len(all_disc)
    # A basket can appear in more than one export (supplemental re-pulls).
    # Last write wins, matching the ETL's drop_duplicates(keep="last").
    all_disc = all_disc.drop_duplicates(subset=["basket_id"], keep="last")
    all_disc["basket_id"] = all_disc.basket_id.astype("int64")

    print()
    print(f"TOTAL : {before:,} rows -> {len(all_disc):,} unique baskets")
    print(f"        {int((all_disc.discount_amt > 0).sum()):,} discounted, "
          f"${all_disc.discount_amt.sum():,.2f}")

    con = duckdb.connect(str(DB), read_only=True)
    cols = [r[1] for r in con.execute(
        "PRAGMA table_info('fact_basket')").fetchall()]
    have_col = "discount_amt" in cols
    n_baskets = con.execute(
        "SELECT count(*) FROM fact_basket WHERE NOT is_return").fetchone()[0]
    con.close()

    print(f"        fact_basket: {n_baskets:,} non-return baskets, "
          f"discount_amt column {'present' if have_col else 'MISSING'}")

    if not have_col:
        print("\nRun wire_discount.py first — the column does not exist yet.")
        return 1

    if not APPLY:
        print("\nDry run only. Re-run with --apply to write.")
        return 0

    bak = DB.with_name(f"tta.duckdb.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(DB, bak)
    print(f"\nbackup: {bak.name}")

    con = duckdb.connect(str(DB))
    con.register("disc", all_disc)
    con.execute("""
        UPDATE fact_basket AS b
        SET discount_amt = d.discount_amt
        FROM disc d
        WHERE b.basket_id = d.basket_id
    """)
    con.execute(
        "UPDATE fact_basket SET discount_amt = 0 WHERE discount_amt IS NULL")

    got = con.execute("""
        SELECT count(*) FILTER (WHERE discount_amt > 0),
               SUM(discount_amt),
               SUM(basket_net)
        FROM fact_basket WHERE NOT is_return
    """).fetchone()
    rng = con.execute("""
        SELECT MIN(CAST(txn_ts AS DATE)), MAX(CAST(txn_ts AS DATE))
        FROM fact_basket WHERE COALESCE(discount_amt,0) > 0
    """).fetchone()
    con.close()

    print(f"written: {got[0]:,} discounted baskets, "
          f"${got[1]:,.2f} discount on ${got[2]:,.2f} net "
          f"({got[1]/max(got[2],1)*100:.2f}%)")
    print(f"range  : {rng[0]} .. {rng[1]}")
    print("\nNext: python publish.py --db ~/cerebral/tta.duckdb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
