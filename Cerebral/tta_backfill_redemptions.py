"""Backfill loyalty redemption history from Alpine IQ Redemption Reports.

Why this exists: redemption attribution was added to the ETL in July 2026.
Periods loaded before that have baskets and sales lines but no fact_redemption
rows, and their fact_basket.loyalty_redeem was never set. This script rebuilds
both directly from Alpine report exports, joined against the existing
fact_line / fact_basket data. It does NOT touch fact_line or fact_basket
amounts other than the two loyalty columns.

Usage:
    python tta_backfill_redemptions.py --reports "C:\\path\\to\\alpine_reports" --db "C:\\path\\to\\tta.duckdb"

Put every Alpine Redemption Report export in the reports folder first — one
per store, or a single chain-wide export, covering the full history you want
restored. Overlapping exports are de-duplicated on order + offer.

When it finishes, run your usual refresh (tta_refresh.py) so the published
dashboard file picks up the restored history.
"""

import argparse
from pathlib import Path

import duckdb
import pandas as pd

from tta_etl import _tokens, attribute_offer


def read_report(path: Path) -> pd.DataFrame | None:
    """Read one Alpine export, locating the header row like the ETL does."""
    probe = pd.read_excel(path, header=None, nrows=12)
    hits = probe[probe.apply(
        lambda r: r.astype(str).str.contains("Order Number").any(), axis=1)
    ].index
    if not len(hits):
        print(f"  ! no header row found, skipped: {path.name}")
        return None
    df = pd.read_excel(path, header=hits[0])
    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["Order Number"].notna()].copy()
    df = df[df["Order Number"].astype(str).str.strip() != "Total"]
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", required=True, type=Path,
                    help="Folder containing Alpine Redemption Report .xlsx exports")
    ap.add_argument("--db", default="tta.duckdb")
    args = ap.parse_args()

    paths = sorted(args.reports.glob("*.xls*"))
    if not paths:
        print(f"ERROR: no .xlsx files in {args.reports}")
        return 1

    frames = []
    for p in paths:
        d = read_report(p)
        if d is not None and len(d):
            print(f"  read {p.name}: {len(d):,} rows")
            frames.append(d)
    if not frames:
        print("ERROR: nothing readable in that folder")
        return 1

    rep = pd.concat(frames, ignore_index=True)
    before = len(rep)
    rep = rep.drop_duplicates(
        subset=["Order Number", "AlpineIQ Discount ID", "Discount Description"],
        keep="first")
    print(f"\n  combined {len(paths)} report(s): {before:,} rows -> "
          f"{len(rep):,} after de-duplication")

    con = duckdb.connect(args.db)
    line = con.execute(
        "SELECT basket_id, brand, category, product FROM fact_line").df()
    bask = con.execute(
        "SELECT basket_id, store_key, txn_ts, date_key, iso_year, iso_week, "
        "channel, customer_key, basket_net FROM fact_basket").df()
    if line.empty or bask.empty:
        print("ERROR: fact_line / fact_basket are empty — run the ETL first")
        con.close()
        return 1

    catalogue = {frozenset(_tokens(b)): b
                 for b in line["brand"].dropna().unique() if _tokens(b)}
    by_basket = {bid: g[["brand", "category", "product"]]
                 for bid, g in line.groupby("basket_id")}
    bmeta = bask.set_index("basket_id")

    out = []
    skipped_basket = 0
    for _, r in rep.iterrows():
        try:
            bid = int(r["Order Number"])
        except (TypeError, ValueError):
            continue
        if bid not in by_basket or bid not in bmeta.index:
            skipped_basket += 1
            continue
        b = bmeta.loc[bid]
        brand, catg, prod, method = attribute_offer(
            r.get("Discount Description"), by_basket[bid], catalogue)
        offer_id = r.get("AlpineIQ Discount ID")
        out.append({
            "basket_id": bid,
            "store_key": int(b["store_key"]),
            "txn_ts": b["txn_ts"],
            "date_key": int(b["date_key"]),
            "iso_year": int(b["iso_year"]),
            "iso_week": int(b["iso_week"]),
            "channel": b["channel"],
            "customer_key": b["customer_key"],
            "offer_id": "" if pd.isna(offer_id) else str(offer_id),
            "offer_name": r.get("Discount Description"),
            "redeem_amt": float(r.get("Alpine Discount Amount") or 0),
            "matched_brand": brand,
            "matched_category": catg,
            "matched_product": prod,
            "match_method": method,
            "basket_net": float(b["basket_net"]),
        })

    df = pd.DataFrame(out)
    if df.empty:
        print("\nERROR: none of the report's orders joined to baskets. "
              "Nothing written.")
        con.close()
        return 1

    # Replace, never duplicate: wipe existing rows for every (store, date)
    # the rebuilt data covers, then insert.
    for sk, grp in df.groupby("store_key"):
        dates = ",".join(map(str, sorted(grp["date_key"].unique())))
        con.execute(
            f"DELETE FROM fact_redemption WHERE store_key = {int(sk)} "
            f"AND date_key IN ({dates})")

    rcols = [r[1] for r in
             con.execute("PRAGMA table_info('fact_redemption')").fetchall()]
    stage = df.reindex(columns=rcols)
    con.register("stage_red", stage)
    con.execute("INSERT INTO fact_redemption SELECT * FROM stage_red")

    # Restore basket-level loyalty values for the same orders.
    redeem = (df.groupby("basket_id")["redeem_amt"].sum()
                .reset_index().rename(columns={"redeem_amt": "amt"}))
    con.register("stage_bask", redeem)
    con.execute("""
        UPDATE fact_basket
        SET loyalty_redeem = s.amt,
            used_redemption = (s.amt > 0)
        FROM stage_bask s
        WHERE fact_basket.basket_id = s.basket_id
    """)

    matched = df["match_method"] != "unmatched"
    print(f"\n  inserted {len(df):,} redemptions "
          f"({df['txn_ts'].min()} -> {df['txn_ts'].max()})")
    print(f"  attributed to a brand: {matched.sum():,} "
          f"({df.loc[matched, 'redeem_amt'].sum():,.0f}$ of "
          f"{df['redeem_amt'].sum():,.0f}$)")
    print(f"  skipped (order not found in baskets): {skipped_basket:,}")
    print(f"  fact_basket loyalty columns updated for {len(redeem):,} baskets")
    print("\n  by offer family:")
    fam = (df.assign(family=df["offer_name"].astype(str).str.split().str[0])
             .groupby("family")
             .agg(n=("basket_id", "count"), spend=("redeem_amt", "sum"))
             .sort_values("spend", ascending=False))
    print(fam.to_string())
    top = (df[df["matched_brand"].notna()]
           .groupby("matched_brand")["redeem_amt"].sum()
           .sort_values(ascending=False).head(12))
    print("\n  top attributed brands:")
    print(top.to_string())

    con.close()
    print("\nDone. Now run your usual refresh (tta_refresh.py) so the "
          "published dashboard file picks this up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
