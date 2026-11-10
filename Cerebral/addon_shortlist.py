"""
Add-on menu shortlist: which products customers already add to a basket
alongside something else, per store.

Reads the working warehouse (fact_line, and fact_inventory when present).
Does NOT depend on the Visual Merchandising build. Opens the database
read-only, so it is safe to run while nothing else is writing.

How a line is classified
------------------------
Returns, $0 lines, GWP and *PROMO* SKUs, Non-Sale and numeric-only product names are
dropped. Each basket is then collapsed to one row per product.
  - Solo        : the basket holds only this product
  - Anchor      : the basket holds 2+ products and this one has the highest net $
  - Attachment  : the basket holds 2+ products and this one is NOT the anchor

What makes a candidate
----------------------
  attach_share   >= MIN_ATTACH_SHARE  (it usually rides along with something else)
  solo_share     <= MAX_SOLO_SHARE    (people don't come in just for it)
  price_ratio    <= MAX_PRICE_RATIO   (median unit price / store's median basket)
  baskets        >= MIN_BASKETS       (enough evidence)
  on hand >= MIN_ON_HAND in sellable rooms (SALES FLOOR / VAULT / DAY VAULT),
                 from --inventory if given, else the warehouse's latest snapshot

With --inventory, an "Expiring soon" sheet also lists add-on-priced stock
expiring within EXPIRY_DAYS: candidates for one rotating manager's-pick slot.

Candidates are ranked by attach_per_week: how many baskets a week it is
already being added to. Margin columns are there to break ties by hand.

Usage (from C:\\Users\\User\\cerebral\\Cerebral):
    python addon_shortlist.py
    python addon_shortlist.py --days 60
    python addon_shortlist.py --db ..\\tta.duckdb --channels In-Store Non-Stop
    python addon_shortlist.py --top 20
    python addon_shortlist.py --inventory "$HOME\\Downloads\\Current_Inventory.xlsx"

In the Monday refresh (GitHub Actions) it runs after the inventory ingest,
reads fact_inventory, and replaces "Add-On Shortlist (latest).xlsx" on Drive.

Output: addon_shortlist_<end-date>.xlsx in the current folder: one sheet per
store with the shortlist, plus an "All products" sheet with every product
that passed MIN_BASKETS, so the thresholds can be second-guessed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

STORES = {1: "DTBK", 2: "Fifth Avenue", 3: "Soho", 4: "Union Square"}

# --- thresholds: tune these, then re-run --------------------------------
MIN_BASKETS = 30          # baskets containing the product in the window
MIN_ATTACH_SHARE = 0.50   # share of those baskets where it was an attachment
MAX_SOLO_SHARE = 0.25     # share where it was the only product
MAX_PRICE_RATIO = 0.25    # median unit price / store median basket net
MIN_ON_HAND = 10          # sellable units needed to put it on a menu at all
EXPIRY_DAYS = 60          # "Expiring soon" sheet looks this far ahead
DRIVE_NAME = "Add-On Shortlist (latest).xlsx"   # stable name: one link, always current
DEFAULT_DAYS = 90
DEFAULT_TOP = 15

# Same rooms the ETL counts as sellable (tta_config.SELLABLE_ROOMS).
SELLABLE_ROOMS = {"SALES FLOOR", "VAULT", "DAY VAULT"}
STORE_FRAGMENTS = {"downtown brooklyn": 1, "fifth avenue": 2, "soho": 3, "union square": 4}


def read_dutchie_inventory(path: Path) -> pd.DataFrame:
    """Package-level sellable stock from a Dutchie 'Current Inventory' export.

    The header row sits below a few 'Export Date / From / To' lines, so it is
    found by looking for 'Location Name' rather than assumed.
    """
    raw = pd.read_excel(path, header=None)
    hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip() == "Location Name"]
    if len(hdr) == 0:
        raise SystemExit(f"[error] no 'Location Name' header row in {path.name}")
    df = pd.read_excel(path, header=int(hdr[0]))

    def store_of(loc: str):
        low = str(loc).lower()
        return next((k for frag, k in STORE_FRAGMENTS.items() if frag in low), None)

    df["store_key"] = df["Location Name"].map(store_of)
    room = df["Inventory Room"].astype(str).str.strip().str.upper()
    df = df[df["store_key"].notna() & room.isin(SELLABLE_ROOMS)]
    out = pd.DataFrame({
        "store_key": df["store_key"].astype(int),
        "product": df["Product Name"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip(),
        "category": df.get("Category"),
        "brand": df.get("Brand Name"),
        "qty": pd.to_numeric(df["Quantity on Hand"], errors="coerce").fillna(0),
        "price": pd.to_numeric(df.get("Inventory Price"), errors="coerce"),
        "expiry": pd.to_datetime(df.get("Inventory Expiration Date"), errors="coerce"),
    })
    # Samples and promo packages are not for sale at the register.
    junk = out["product"].str.contains(r"sample|promo|gwp", case=False, regex=True)
    return out[(out["qty"] > 0) & ~junk]


def log(msg: str) -> None:
    print(msg, flush=True)


def table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [name]).fetchone()[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=r"..\tta.duckdb")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--inventory", default=None,
                    help="Dutchie 'Current Inventory' export (.xlsx) to use for the "
                         "stock filter instead of the warehouse snapshot")
    ap.add_argument("--out", default=None,
                    help="Output .xlsx path (default: addon_shortlist_<end-date>.xlsx here)")
    ap.add_argument("--upload", action="store_true",
                    help=f"Also upload to Drive as {DRIVE_NAME} (folder from "
                         f"TTA_DRIVE_REPORTS, else TTA_DRIVE_STATE)")
    ap.add_argument("--channels", nargs="+", default=["In-Store"],
                    help="Channels to include (In-Store, Non-Stop, Delivery, Event)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        log(f"[error] warehouse not found at {db.resolve()} -- pass --db")
        return 1

    log(f"[1/5] opening {db.resolve()} read-only")
    con = duckdb.connect(str(db), read_only=True)

    end_ts = con.execute("SELECT MAX(txn_ts) FROM fact_line").fetchone()[0]
    if end_ts is None:
        log("[error] fact_line is empty")
        return 1
    log(f"      latest sale in warehouse: {end_ts}")
    days_stale = (pd.Timestamp.now() - pd.Timestamp(end_ts)).days
    if days_stale > 10:
        log(f"      [warn] data is {days_stale} days old -- consider running the ETL first")

    weeks = args.days / 7.0
    params = {"days": args.days, "end_ts": end_ts}
    chan_list = ", ".join("'" + c.replace("'", "''") + "'" for c in args.channels)
    log(f"      window: last {args.days} days, channels: {', '.join(args.channels)}")

    # ---------------------------------------------------------------------
    log("[2/5] collapsing baskets to one row per product")
    con.execute(f"""
        CREATE TEMP TABLE bp AS
        SELECT store_key, basket_id, product,
               ANY_VALUE(category)     AS category,
               ANY_VALUE(brand)        AS brand,
               SUM(units)              AS units,
               SUM(net_sales)          AS net,
               SUM(gross_margin)       AS margin
        FROM fact_line
        WHERE NOT is_return
          AND txn_ts >  $end_ts - to_days($days)
          AND txn_ts <= $end_ts
          AND channel IN ({chan_list})
          AND net_sales > 0
          AND units > 0
          AND category IS NOT NULL AND category <> 'EXCLUDE'
          AND product IS NOT NULL
          AND LOWER(product) NOT LIKE '%gwp%'
          AND LOWER(product) NOT LIKE '%promo%'
          AND NOT regexp_matches(product, '^[0-9]{{4,}}$')
        GROUP BY 1, 2, 3
    """, params)
    n_rows = con.execute("SELECT COUNT(*) FROM bp").fetchone()[0]
    log(f"      {n_rows:,} basket-product rows")
    if n_rows == 0:
        log("[error] nothing in the window -- check --days / --channels")
        return 1

    # ---------------------------------------------------------------------
    log("[3/5] classifying solo / anchor / attachment")
    con.execute("""
        CREATE TEMP TABLE role AS
        WITH r AS (
            SELECT *,
                   COUNT(*) OVER (PARTITION BY store_key, basket_id) AS n_products,
                   ROW_NUMBER() OVER (PARTITION BY store_key, basket_id
                                      ORDER BY net DESC, product) AS rk,
                   SUM(net) OVER (PARTITION BY store_key, basket_id) AS basket_net
            FROM bp
        )
        SELECT *,
               CASE WHEN n_products = 1 THEN 'solo'
                    WHEN rk = 1         THEN 'anchor'
                    ELSE 'attach' END AS role
        FROM r
    """)

    basket_med = con.execute("""
        SELECT store_key, MEDIAN(basket_net) AS median_basket
        FROM (SELECT DISTINCT store_key, basket_id, basket_net FROM role)
        GROUP BY 1
    """).df()

    # ---------------------------------------------------------------------
    log("[4/5] scoring products per store")
    prod = con.execute(f"""
        SELECT store_key, product,
               ANY_VALUE(category)                         AS category,
               ANY_VALUE(brand)                            AS brand,
               COUNT(*)                                    AS baskets,
               SUM(CASE WHEN role = 'attach' THEN 1 ELSE 0 END) AS attach_baskets,
               SUM(CASE WHEN role = 'solo'   THEN 1 ELSE 0 END) AS solo_baskets,
               SUM(CASE WHEN role = 'anchor' THEN 1 ELSE 0 END) AS anchor_baskets,
               MEDIAN(net / units)                         AS median_unit_price,
               SUM(units)                                  AS units,
               SUM(net)                                    AS net_sales,
               SUM(margin)                                 AS gross_margin
        FROM role
        GROUP BY 1, 2
        HAVING COUNT(*) >= {MIN_BASKETS}
    """).df()

    prod = prod.merge(basket_med, on="store_key", how="left")
    prod["attach_share"] = prod["attach_baskets"] / prod["baskets"]
    prod["solo_share"] = prod["solo_baskets"] / prod["baskets"]
    prod["anchor_share"] = prod["anchor_baskets"] / prod["baskets"]
    prod["price_ratio"] = prod["median_unit_price"] / prod["median_basket"]
    prod["attach_per_week"] = prod["attach_baskets"] / weeks
    prod["margin_pct"] = (prod["gross_margin"] / prod["net_sales"]).where(
        prod["gross_margin"].notna() & (prod["net_sales"] > 0))

    inv_pkg = None   # package-level rows from a Dutchie export, if given
    if args.inventory:
        inv_pkg = read_dutchie_inventory(Path(args.inventory))
        inv = (inv_pkg.groupby(["store_key", "product"], as_index=False)
               .agg(on_hand=("qty", "sum"), earliest_expiry=("expiry", "min")))
        log(f"      inventory: {Path(args.inventory).name} "
            f"({len(inv_pkg):,} sellable packages)")
    elif table_exists(con, "fact_inventory"):
        inv = con.execute("""
            SELECT store_key, product, SUM(qty_on_hand) AS on_hand, MAX(snapshot_date) AS inv_date
            FROM fact_inventory
            WHERE sellable
              AND snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory)
            GROUP BY 1, 2
        """).df()
        inv_date = inv["inv_date"].max() if not inv.empty else None
        log(f"      inventory snapshot: {inv_date}")
        if inv_date is not None and (pd.Timestamp(end_ts) - pd.Timestamp(inv_date)).days > 14:
            log("      [warn] inventory snapshot is much older than the sales -- "
                "pass --inventory with a current Dutchie export")
        inv["earliest_expiry"] = pd.NaT
        inv = inv.drop(columns="inv_date")
    else:
        inv = None

    if inv is not None:
        prod = prod.merge(inv, on=["store_key", "product"], how="left")
        prod["on_hand"] = prod["on_hand"].fillna(0)
        in_stock = prod["on_hand"] >= MIN_ON_HAND
    else:
        log("      [warn] no inventory available -- stock filter skipped")
        prod["on_hand"] = pd.NA
        prod["earliest_expiry"] = pd.NaT
        in_stock = pd.Series(True, index=prod.index)

    prod["store"] = prod["store_key"].map(STORES)
    prod["candidate"] = (
        (prod["attach_share"] >= MIN_ATTACH_SHARE)
        & (prod["solo_share"] <= MAX_SOLO_SHARE)
        & (prod["price_ratio"] <= MAX_PRICE_RATIO)
        & in_stock
    )

    cols = ["store", "category", "brand", "product", "attach_per_week",
            "baskets", "attach_share", "solo_share", "anchor_share",
            "median_unit_price", "median_basket", "price_ratio",
            "margin_pct", "net_sales", "on_hand", "earliest_expiry", "candidate"]
    prod = prod.sort_values(["store_key", "attach_per_week"],
                            ascending=[True, False])

    # ---------------------------------------------------------------------
    out = Path(args.out) if args.out else Path(f"addon_shortlist_{pd.Timestamp(end_ts):%Y-%m-%d}.xlsx")
    log(f"[5/5] writing {out.resolve()}")
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for sk, name in STORES.items():
            s = prod[(prod["store_key"] == sk) & prod["candidate"]].head(args.top)
            s[cols[:-1]].to_excel(xw, sheet_name=name, index=False)
            log(f"      {name}: {int(((prod['store_key'] == sk) & prod['candidate']).sum())} "
                f"candidates, top {len(s)} written")
            if not s.empty:
                for _, r in s.head(5).iterrows():
                    log(f"         {r['attach_per_week']:6.1f}/wk  "
                        f"${r['median_unit_price']:>6.2f}  {r['category']:<10} {r['product']}")
        prod[cols].to_excel(xw, sheet_name="All products", index=False)

        # Expiring soon: candidates for the one rotating "manager's pick" slot.
        # Cheap enough to be an add-on, stocked, expiring inside the window.
        if inv_pkg is not None and inv_pkg["expiry"].notna().any():
            today = pd.Timestamp.now().normalize()
            cutoff = today + pd.Timedelta(days=EXPIRY_DAYS)
            expired = inv_pkg[inv_pkg["expiry"].notna() & (inv_pkg["expiry"] < today)]
            if not expired.empty:
                log(f"      [warn] {len(expired)} sellable packages are already past "
                    f"expiration ({int(expired['qty'].sum())} units) -- not listed; "
                    f"pull them off the floor")
            ex = inv_pkg[inv_pkg["expiry"].notna() & (inv_pkg["expiry"] >= today)
                         & (inv_pkg["expiry"] <= cutoff)]
            ex = (ex.groupby(["store_key", "product"], as_index=False)
                  .agg(category=("category", "first"), brand=("brand", "first"),
                       price=("price", "median"), units_expiring=("qty", "sum"),
                       earliest_expiry=("expiry", "min")))
            ex = ex.merge(basket_med, on="store_key", how="left")
            ex = ex[ex["price"] <= MAX_PRICE_RATIO * ex["median_basket"]]
            ex = ex.merge(prod[["store_key", "product", "attach_per_week",
                                "attach_share", "solo_share"]],
                          on=["store_key", "product"], how="left")
            ex["store"] = ex["store_key"].map(STORES)
            ex = ex.sort_values(["store_key", "earliest_expiry", "units_expiring"],
                                ascending=[True, True, False])
            ex[["store", "category", "brand", "product", "price", "units_expiring",
                "earliest_expiry", "attach_per_week", "attach_share", "solo_share"]
               ].to_excel(xw, sheet_name="Expiring soon", index=False)
            log(f"      expiring within {EXPIRY_DAYS} days and add-on priced: "
                f"{len(ex)} store-products (see 'Expiring soon' sheet)")

        notes = pd.DataFrame({"setting": [
            "window_days", "window_end", "channels", "MIN_BASKETS",
            "MIN_ATTACH_SHARE", "MAX_SOLO_SHARE", "MAX_PRICE_RATIO",
            "MIN_ON_HAND", "EXPIRY_DAYS", "inventory_source"],
            "value": [args.days, str(end_ts), ", ".join(args.channels), MIN_BASKETS,
                      MIN_ATTACH_SHARE, MAX_SOLO_SHARE, MAX_PRICE_RATIO,
                      MIN_ON_HAND, EXPIRY_DAYS,
                      args.inventory or "fact_inventory (warehouse)"]})
        notes.to_excel(xw, sheet_name="Settings", index=False)

    if args.upload:
        import os
        from tta_env import bootstrap
        from tta_drive import DriveClient
        bootstrap()
        folder = os.environ.get("TTA_DRIVE_REPORTS") or os.environ["TTA_DRIVE_STATE"]
        log(f"      uploading to Drive as {DRIVE_NAME}")
        # Replaces the file in place, so a shared link always shows the latest.
        # The service account cannot CREATE files in My Drive: put a file with
        # this exact name in the folder once by hand, and every run updates it.
        DriveClient().upload(out, folder, DRIVE_NAME)

    log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
