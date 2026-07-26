"""
The Travel Agency — Category Analytics ETL

Reads raw Dutchie / Alpine IQ exports, builds fact_line, fact_basket and
agg_category_week into a DuckDB database, and emits a validation report.

Usage
-----
    python tta_etl.py --inbox ./inbox --db ./tta.duckdb
    python tta_etl.py --inbox ./inbox --db ./tta.duckdb --validate-only

Design notes
------------
* Idempotent: re-running a period deletes and rebuilds that period's rows.
* Every join is same-day / same-period. No cross-time product identity needed;
  the product-name join measured 100% in 2024, 2025 and 2026.
* Validation runs on every load and FAILS LOUD. A silent bad load is far more
  expensive than a refused one.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

from tta_config import (
    AVAILABILITY, BASKET_FLAG_CATEGORIES, CATEGORY_MAP, CHANNEL_NUMBERED_PATTERN,
    CHANNEL_RULES, CONFIG_VERSION, EXPORTS, MAX_HEADER_SCAN_ROWS, SELLABLE_ROOMS,
    STORES, THRESHOLDS,
)


# ===========================================================================
# Reading
# ===========================================================================

def find_header_row(path: Path, signature: list[str]) -> int:
    """Locate the true header row.

    Header position is not fixed. Reports carrying Location as a column begin
    at row 3; reports with a 'Location:' preamble begin at row 4. Scanning for
    the signature is robust to both, and to future preamble changes.
    """
    probe = pd.read_excel(path, header=None, nrows=MAX_HEADER_SCAN_ROWS)
    for i in range(len(probe)):
        row = {str(v).strip() for v in probe.iloc[i].tolist()}
        if all(col in row for col in signature):
            return i
    raise ValueError(
        f"{path.name}: no header row found in first {MAX_HEADER_SCAN_ROWS} rows "
        f"matching signature {signature}"
    )


def classify_export(path: Path) -> str | None:
    """Identify which export a file is, by header signature rather than filename.

    Filenames are unreliable -- Dutchie collides them across stores and the
    browser appends (1), (2), (3).
    """
    try:
        probe = pd.read_excel(path, header=None, nrows=MAX_HEADER_SCAN_ROWS)
    except Exception:
        return None
    cells = {str(v).strip() for _, r in probe.iterrows() for v in r.tolist()}
    for name, spec in EXPORTS.items():
        if all(col in cells for col in spec["signature"]):
            return name
    return None


def read_export(path: Path, kind: str) -> pd.DataFrame:
    spec = EXPORTS[kind]
    header = find_header_row(path, spec["signature"])
    df = pd.read_excel(path, header=header)
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ===========================================================================
# Normalisation
# ===========================================================================

def resolve_store(location: str) -> tuple[int, str]:
    key = str(location).strip()
    if key not in STORES:
        raise ValueError(
            f"Unknown location string: {key!r}. Add it to STORES in tta_config.py. "
            f"Do not guess -- Soho's official string is missing 'The'."
        )
    s = STORES[key]
    return s["store_key"], s["code"]


def map_channel(register) -> str:
    s = str(register).strip().lower()
    for label, needles in CHANNEL_RULES:
        if any(n in s for n in needles):
            return label
    if re.search(CHANNEL_NUMBERED_PATTERN, s):
        return "In-Store"
    return "UNKNOWN"


def strip_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Remove interleaved subtotal and grand-total rows from the Breakdown.

    The export interleaves one 'Total' row per category plus a grand total.
    Summing without this doubles every figure exactly.
    """
    out = df.copy()
    for col in ("Location", "Category", "Product"):
        if col in out.columns:
            out = out[out[col].astype(str).str.strip() != "Total"]
    return out


def customer_hash(name: str) -> str | None:
    """Stable surrogate key for customers lacking an Alpine ID.

    Name-only matching collides at scale. This is a fallback identity used for
    within-period basket attribution, NOT for cross-period retention analysis.
    Real Alpine Customer IDs always take precedence.
    """
    if not name or pd.isna(name):
        return None
    norm = re.sub(r"\s+", " ", str(name).strip().lower())
    if not norm:
        return None
    return "H" + hashlib.sha1(norm.encode()).hexdigest()[:15]


# ===========================================================================
# Build
# ===========================================================================

@dataclass
class LoadResult:
    store_key: int
    store_code: str
    period: str
    fact_line: pd.DataFrame
    fact_basket: pd.DataFrame
    checks: list[dict] = field(default_factory=list)
    source_files: str = ""

    @property
    def ok(self) -> bool:
        return all(c["pass"] for c in self.checks)


class Pipeline:
    def __init__(self, db_path: str):
        self.con = duckdb.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS fact_line (
                line_id           VARCHAR,
                basket_id         BIGINT,
                store_key         INTEGER,
                txn_ts            TIMESTAMP,
                date_key          INTEGER,
                iso_year          INTEGER,
                iso_week          INTEGER,
                day_of_week       INTEGER,
                channel           VARCHAR,
                customer_key      VARCHAR,
                customer_source   VARCHAR,
                product           VARCHAR,
                sku               VARCHAR,
                raw_category      VARCHAR,
                category          VARCHAR,
                brand             VARCHAR,
                units             DOUBLE,
                net_sales         DOUBLE,
                unit_cost         DOUBLE,
                cogs              DOUBLE,
                gross_margin      DOUBLE,
                is_return         BOOLEAN
            );
            CREATE TABLE IF NOT EXISTS fact_basket (
                basket_id         BIGINT,
                store_key         INTEGER,
                txn_ts            TIMESTAMP,
                date_key          INTEGER,
                iso_year          INTEGER,
                iso_week          INTEGER,
                day_of_week       INTEGER,
                channel           VARCHAR,
                customer_key      VARCHAR,
                customer_known    BOOLEAN,
                basket_net        DOUBLE,
                basket_margin     DOUBLE,
                basket_units      DOUBLE,
                basket_lines      INTEGER,
                distinct_cats     INTEGER,
                loyalty_redeem    DOUBLE,
                used_redemption   BOOLEAN,
                is_return         BOOLEAN
            );
            CREATE TABLE IF NOT EXISTS load_log (
                loaded_at         TIMESTAMP,
                store_key         INTEGER,
                period            VARCHAR,
                lines             BIGINT,
                baskets           BIGINT,
                passed            BOOLEAN,
                warnings          INTEGER,
                config_version    VARCHAR,
                source_files      VARCHAR
            );
            CREATE TABLE IF NOT EXISTS fact_inventory (
                snapshot_date     DATE,
                store_key         INTEGER,
                package_id        VARCHAR,
                product           VARCHAR,
                raw_category      VARCHAR,
                category          VARCHAR,
                room              VARCHAR,
                sellable          BOOLEAN,
                qty_on_hand       DOUBLE,
                unit_cost         DOUBLE,
                unit_price        DOUBLE,
                ext_cost          DOUBLE,
                ext_retail        DOUBLE
            );
        """)
        # Basket category flag columns are configuration-driven.
        existing = {r[1] for r in self.con.execute("PRAGMA table_info('fact_basket')").fetchall()}
        for cat in BASKET_FLAG_CATEGORIES:
            slug = cat.lower().replace("-", "_")
            if f"has_{slug}" not in existing:
                self.con.execute(f"ALTER TABLE fact_basket ADD COLUMN has_{slug} BOOLEAN")
            if f"net_ex_{slug}" not in existing:
                self.con.execute(f"ALTER TABLE fact_basket ADD COLUMN net_ex_{slug} DOUBLE")

    # -- build ------------------------------------------------------------

    def build(self, dispensations, breakdown, pos, alpine, period: str) -> LoadResult:
        checks: list[dict] = []

        def check(name, passed, detail, warn=False):
            """warn=True records a non-blocking advisory: visible, but the load
            still writes. Reserved for small drift that is expected between two
            reports generated by different Dutchie subsystems."""
            status = "PASS" if passed else ("WARN" if warn else "FAIL")
            checks.append({"check": name, "status": status,
                           "pass": bool(passed) or warn, "detail": detail})

        # --- store scope -------------------------------------------------
        locs = dispensations["Location"].dropna().unique()
        if len(locs) != 1:
            raise ValueError(f"Dispensations must be single-store, found: {list(locs)}")
        store_key, store_code = resolve_store(locs[0])
        location_str = str(locs[0]).strip()

        # --- breakdown: strip totals, FILTER TO STORE --------------------
        # Chain-scoped export. Without the location filter the product join
        # fans out (measured 3.72x on June 2026).
        det = strip_totals(breakdown)
        det["Location"] = det["Location"].astype(str).str.strip()
        det = det[det["Location"] == location_str].copy()
        if det.empty:
            raise ValueError(f"No breakdown rows for {location_str}")

        raw_cats = set(det["Category"].dropna().astype(str).str.strip())
        unmapped = sorted(raw_cats - set(CATEGORY_MAP))
        check("unmapped_categories", len(unmapped) <= THRESHOLDS["unmapped_category"],
              f"unmapped: {unmapped}" if unmapped else "all raw categories mapped")

        det["category"] = det["Category"].astype(str).str.strip().map(CATEGORY_MAP)

        # A product recategorised mid-period appears as TWO rows under different
        # raw categories (observed at Soho and USQ, June 2026). Left unhandled
        # the product join fans out. Collapse to one row per product, keeping
        # the canonical category -- but refuse if the duplicates disagree on it.
        conflict = (det.groupby("Product")["category"].nunique() > 1)
        conflicting = sorted(conflict[conflict].index.tolist())
        check("category_conflict", len(conflicting) == 0,
              f"{len(conflicting)} products mapped to >1 canonical category"
              + (f": {conflicting[:3]}" if conflicting else ""))

        dup_products = int(det["Product"].duplicated().sum())
        if dup_products:
            det = (det.groupby(["Product"], as_index=False)
                      .agg(Category=("Category", "first"),
                           category=("category", "first"),
                           **{"Brand Name": ("Brand Name", "first")},
                           QuantitySold=("QuantitySold", "sum"),
                           GrossSales=("GrossSales", "sum"),
                           NetSales=("NetSales", "sum"),
                           Cost=("Cost", "sum")))
        check("breakdown_products_collapsed", True,
              f"{dup_products} duplicate product row(s) merged (mid-period recategorisation)")

        # Recompute per-unit figures AFTER collapsing. Guard against zero and
        # negative QuantitySold: USQ June 2026 has rows with revenue but zero
        # units, which silently produced NaN and crashed the writer.
        qty = det["QuantitySold"].where(det["QuantitySold"] > 0)
        det["AvgPricePerUnit"] = (det["NetSales"] / qty).astype("float64")
        det["unit_cost"] = (det["Cost"] / qty).astype("float64")
        bad_qty = int((det["QuantitySold"] <= 0).sum())
        check("nonpositive_quantity", True,
              f"{bad_qty} product row(s) with zero/negative units -> per-unit values null")

        # --- POS: channel + returns --------------------------------------
        pos = pos.copy()
        pos["channel"] = pos["Register"].map(map_channel)
        unknown = int((pos["channel"] == "UNKNOWN").sum())
        check("unknown_channel", unknown <= THRESHOLDS["unknown_channel"],
              f"{unknown} transactions with unclassifiable register")

        pos["is_return"] = pos["PosStatus"].astype(str).str.strip().str.lower() == "returned"
        excluded = int((pos["channel"] == "EXCLUDE").sum())
        pos_all = pos.copy()                                   # retained for join-rate measurement
        pos = pos[pos["channel"] != "EXCLUDE"].copy()
        check("sample_register_excluded", True,
              f"{excluded:,} sample-register transactions flagged for exclusion")

        # --- Alpine: chain-scoped, filter to store -----------------------
        redeem = pd.Series(dtype=float)
        cust = pd.Series(dtype=object)
        if alpine is not None and len(alpine):
            al = alpine.copy()
            al["Location"] = al["Location"].astype(str).str.strip()
            al = al[al["Location"] == location_str]
            if len(al):
                redeem = al.groupby("Order Number")["Alpine Discount Amount"].sum()
                cust = (al.sort_values("Order Date")
                          .groupby("Order Number")["Customer ID"].last().astype(str))

        # --- fact_line ---------------------------------------------------
        cols = ["Product", "category", "Category", "Brand Name", "AvgPricePerUnit", "unit_cost"]
        fl = dispensations.merge(det[cols], on="Product", how="left")

        matched = fl["category"].notna().mean()
        check("product_join_rate", matched >= THRESHOLDS["product_join_rate"],
              f"{matched*100:.2f}% of {len(fl):,} lines matched to breakdown")

        # Join against the FULL POS set (including sample) so the join-rate check
        # measures genuine orphans rather than deliberate exclusions.
        fl = fl.merge(pos_all[["PosId", "channel", "is_return", "PatientName"]],
                      left_on="ReceiptNo", right_on="PosId", how="left")
        rmatch = fl["channel"].notna().mean()
        orphans = int(fl["channel"].isna().sum())
        check("receipt_join_rate", rmatch >= THRESHOLDS["receipt_join_rate"],
              f"{rmatch*100:.2f}% matched ({orphans:,} orphan lines with no POS transaction)")

        # Now drop deliberate exclusions and account for them separately.
        excl_units = float(fl.loc[fl["channel"] == "EXCLUDE", "Qty"].sum())
        excl_lines = int((fl["channel"] == "EXCLUDE").sum())
        fl = fl[fl["channel"].notna() & (fl["channel"] != "EXCLUDE")].copy()

        ts = pd.to_datetime(fl["ReceiptDate"])
        iso = ts.dt.isocalendar()

        # AvgPricePerUnit is NET of discount (validated: derived revenue matched
        # breakdown NetSales to 0.03% on June 2026). Line-level values are a
        # period-average allocation, exact in aggregate, approximate per line.
        line = pd.DataFrame({
            "line_id": [f"{store_code}-{r}-{i}" for i, r in enumerate(fl["ReceiptNo"])],
            "basket_id": fl["ReceiptNo"].astype("int64"),
            "store_key": store_key,
            "txn_ts": ts,
            "date_key": ts.dt.strftime("%Y%m%d").astype(int),
            "iso_year": iso["year"].astype(int),
            "iso_week": iso["week"].astype(int),
            "day_of_week": ts.dt.dayofweek.astype(int) + 1,
            "channel": fl["channel"],
            "product": fl["Product"],
            "sku": fl.get("NDC\\SKU"),
            "raw_category": fl["Category"],
            "category": fl["category"],
            "brand": fl["Brand Name"],
            "units": pd.to_numeric(fl["Qty"], errors="coerce").astype("float64"),
            "net_sales": (pd.to_numeric(fl["Qty"], errors="coerce")
                          * pd.to_numeric(fl["AvgPricePerUnit"], errors="coerce")).astype("float64"),
            "unit_cost": pd.to_numeric(fl["unit_cost"], errors="coerce").astype("float64"),
            "is_return": fl["is_return"].fillna(False).astype(bool),
        })
        line["cogs"] = line["units"] * line["unit_cost"]
        line["gross_margin"] = line["net_sales"] - line["cogs"]

        excluded_cat_mask = line["category"] == "EXCLUDE"
        excluded_cat_units = float(line.loc[excluded_cat_mask, "units"].sum())
        excluded_cat_lines = int(excluded_cat_mask.sum())
        line = line[~excluded_cat_mask].copy()

        # customer identity: Alpine ID where available, hashed name otherwise
        alpine_id = line["basket_id"].map(cust)
        hashed = fl.loc[line.index, "Patient"].map(customer_hash)
        line["customer_key"] = alpine_id.fillna(hashed)
        line["customer_source"] = alpine_id.notna().map({True: "alpine", False: "name_hash"})

        # --- reconciliation ----------------------------------------------
        # The Breakdown includes sample-register and Non-Sale volume, so compare
        # like for like: add back what we deliberately excluded.
        bd_units = float(det["QuantitySold"].sum())
        our_units = float(line["units"].sum()) + excl_units + float(excluded_cat_units)
        qty_gap = abs(our_units - bd_units) / max(bd_units, 1)
        check("qty_reconciliation",
              qty_gap <= THRESHOLDS["qty_recon_tolerance"],
              f"{our_units:,.0f} vs breakdown {bd_units:,.0f} "
              f"({qty_gap*100:.3f}%, {bd_units-our_units:+,.0f} units; "
              f"{excl_lines:,} sample + {excluded_cat_lines:,} non-sale excluded)",
              warn=qty_gap <= THRESHOLDS["qty_recon_fail"])

        net_gap = abs(line["net_sales"].sum() - det["NetSales"].sum()) / max(det["NetSales"].sum(), 1)
        check("net_reconciliation",
              net_gap <= THRESHOLDS["net_recon_tolerance"],
              f"derived net revenue differs from breakdown by {net_gap*100:.3f}%",
              warn=net_gap <= THRESHOLDS["net_recon_fail"])

        # --- fact_basket --------------------------------------------------
        g = line.groupby("basket_id")
        basket = g.agg(
            store_key=("store_key", "first"),
            txn_ts=("txn_ts", "min"),
            date_key=("date_key", "first"),
            iso_year=("iso_year", "first"),
            iso_week=("iso_week", "first"),
            day_of_week=("day_of_week", "first"),
            channel=("channel", "first"),
            customer_key=("customer_key", "first"),
            basket_net=("net_sales", "sum"),
            basket_margin=("gross_margin", "sum"),
            basket_units=("units", "sum"),
            basket_lines=("line_id", "size"),
            distinct_cats=("category", "nunique"),
            is_return=("is_return", "max"),
        ).reset_index()

        basket["customer_known"] = basket["customer_key"].notna()
        basket["loyalty_redeem"] = basket["basket_id"].map(redeem).fillna(0.0)
        basket["used_redemption"] = basket["loyalty_redeem"] > 0

        # has_<cat> / net_ex_<cat>: precomputed so basket-lift analysis needs no self-join
        cat_net = line.pivot_table(index="basket_id", columns="category",
                                   values="net_sales", aggfunc="sum").fillna(0.0)
        for cat in BASKET_FLAG_CATEGORIES:
            slug = cat.lower().replace("-", "_")
            vals = cat_net[cat] if cat in cat_net.columns else 0.0
            series = basket["basket_id"].map(vals).fillna(0.0) if cat in cat_net.columns else 0.0
            basket[f"has_{slug}"] = (series > 0) if cat in cat_net.columns else False
            basket[f"net_ex_{slug}"] = basket["basket_net"] - (series if cat in cat_net.columns else 0.0)

        return LoadResult(store_key, store_code, period, line, basket, checks)

    # -- persist -----------------------------------------------------------

    def write(self, res: LoadResult) -> None:
        self.con.execute("DELETE FROM fact_line WHERE store_key = ? AND date_key IN "
                         "(SELECT DISTINCT date_key FROM fact_line_stage)"
                         if False else "SELECT 1")

        dates = res.fact_line["date_key"].unique().tolist()
        self.con.execute(
            f"DELETE FROM fact_line WHERE store_key = {res.store_key} "
            f"AND date_key IN ({','.join(map(str, dates))})")
        self.con.execute(
            f"DELETE FROM fact_basket WHERE store_key = {res.store_key} "
            f"AND date_key IN ({','.join(map(str, dates))})")

        line_cols = [r[1] for r in self.con.execute("PRAGMA table_info('fact_line')").fetchall()]
        bask_cols = [r[1] for r in self.con.execute("PRAGMA table_info('fact_basket')").fetchall()]

        ldf = res.fact_line.reindex(columns=line_cols)          # noqa: F841
        bdf = res.fact_basket.reindex(columns=bask_cols)        # noqa: F841
        self.con.execute("INSERT INTO fact_line SELECT * FROM ldf")
        self.con.execute("INSERT INTO fact_basket SELECT * FROM bdf")
        warns = sum(1 for c in res.checks if c.get("status") == "WARN")
        self.con.execute(
            "INSERT INTO load_log VALUES (now(), ?, ?, ?, ?, ?, ?, ?, ?)",
            [res.store_key, res.period, len(res.fact_line), len(res.fact_basket),
             res.ok, warns, CONFIG_VERSION, res.source_files])

    def load_inventory(self, inv: pd.DataFrame, snapshot_date: str) -> dict[str, int]:
        """Load an inventory snapshot.

        Two things this must get right:
        1. The export has NO date column. `snapshot_date` is supplied by the
           caller from the export timestamp -- never inferred.
        2. The location column is 'Location Name' (not 'Location'), and the
           export may be chain-wide OR store-scoped. Always split by location;
           never assume a single store.
        """
        loc_col = next((c for c in inv.columns if str(c).strip() == "Location Name"), None)
        if loc_col is None:
            raise ValueError("Inventory export has no 'Location Name' column")

        snap = pd.to_datetime(snapshot_date).date()
        loaded: dict[str, int] = {}

        for location, grp in inv.groupby(loc_col):
            store_key, code = resolve_store(location)
            df = grp.copy()
            df["category"] = df["Category"].astype(str).str.strip().map(CATEGORY_MAP)
            df["sellable"] = (df["Inventory Room"].astype(str).str.strip().str.upper()
                              .isin([r.upper() for r in SELLABLE_ROOMS]))
            out = pd.DataFrame({
                "snapshot_date": snap,
                "store_key": store_key,
                "package_id": df["Package ID"].astype(str),
                "product": df["Product Name"],
                "raw_category": df["Category"],
                "category": df["category"],
                "room": df["Inventory Room"],
                "sellable": df["sellable"],
                "qty_on_hand": pd.to_numeric(df["Quantity on Hand"], errors="coerce").astype("float64"),
                "unit_cost": pd.to_numeric(df["Inventory Cost"], errors="coerce").astype("float64"),
                "unit_price": pd.to_numeric(df["Inventory Price"], errors="coerce").astype("float64"),
            })
            out["ext_cost"] = out["qty_on_hand"] * out["unit_cost"]
            out["ext_retail"] = out["qty_on_hand"] * out["unit_price"]
            self.con.execute(
                f"DELETE FROM fact_inventory WHERE store_key = {store_key} "
                f"AND snapshot_date = DATE '{snap}'")
            self.con.execute("INSERT INTO fact_inventory SELECT * FROM out")
            loaded[code] = len(out)

        return loaded

    # -- aggregate ---------------------------------------------------------

    def build_aggregates(self) -> None:
        """agg_category_week honours the availability calendar: a channel that
        did not exist yields NULL, never 0."""
        self.con.execute("DROP TABLE IF EXISTS agg_category_week")
        self.con.execute("""
            CREATE TABLE agg_category_week AS
            WITH bw AS (
                SELECT store_key, iso_year, iso_week, channel,
                       COUNT(*) AS total_baskets,
                       COUNT(DISTINCT date_key) AS days_open,
                       SUM(basket_net) AS channel_net
                FROM fact_basket WHERE NOT is_return
                GROUP BY 1,2,3,4
            ),
            cw AS (
                SELECT l.store_key, l.iso_year, l.iso_week, l.channel, l.category,
                       SUM(l.net_sales) AS net_sales,
                       SUM(l.gross_margin) AS gross_margin,
                       SUM(l.units) AS units,
                       COUNT(DISTINCT l.basket_id) AS baskets_containing,
                       COUNT(DISTINCT l.customer_key) AS buyers
                FROM fact_line l WHERE NOT l.is_return
                GROUP BY 1,2,3,4,5
            )
            SELECT
                cw.*,
                bw.total_baskets,
                bw.days_open,
                cw.baskets_containing::DOUBLE / NULLIF(bw.total_baskets,0) AS penetration,
                cw.net_sales / NULLIF(bw.total_baskets,0) * 100          AS dollars_per_100_baskets,
                cw.gross_margin / NULLIF(cw.net_sales,0)                 AS margin_pct,
                cw.units / NULLIF(bw.days_open,0)                        AS units_per_day
            FROM cw JOIN bw USING (store_key, iso_year, iso_week, channel)
        """)

    def close(self):
        self.con.close()


# ===========================================================================
# Reporting
# ===========================================================================

def print_validation(res: LoadResult) -> None:
    print(f"\n  VALIDATION — {res.store_code} / {res.period}")
    print("  " + "-" * 74)
    for c in res.checks:
        print(f"  [{c['status']}] {c['check']:<30} {c['detail']}")
    print("  " + "-" * 74)
    warns = sum(1 for c in res.checks if c["status"] == "WARN")
    if not res.ok:
        print("  *** LOAD FAILED — NOT WRITTEN ***")
    elif warns:
        print(f"  LOADED with {warns} advisory warning(s)")
    else:
        print("  ALL CHECKS PASSED")


def discover(inbox: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for p in sorted(inbox.glob("*.xls*")):
        kind = classify_export(p)
        if kind:
            found.setdefault(kind, []).append(p)
        else:
            print(f"  ! unrecognised export, skipped: {p.name}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True, type=Path)
    ap.add_argument("--db", default="tta.duckdb")
    ap.add_argument("--period", default="unnamed")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()

    print(f"\nTTA Category Analytics ETL")
    print(f"inbox: {args.inbox}   db: {args.db}\n")

    files = discover(args.inbox)
    for k, v in files.items():
        print(f"  found {k:<14} {len(v)} file(s)")

    required = ["dispensations", "breakdown", "pos_register"]
    missing = [r for r in required if r not in files]
    if missing:
        print(f"\nERROR: missing required exports: {missing}")
        return 1

    pipe = Pipeline(args.db)
    alpine = read_export(files["alpine"][0], "alpine") if "alpine" in files else None
    breakdown = read_export(files["breakdown"][0], "breakdown")

    failed = 0
    for disp_path in files["dispensations"]:
        disp = read_export(disp_path, "dispensations")
        loc = disp["Location"].dropna().iloc[0]
        pos_df = None
        for pp in files["pos_register"]:
            cand = read_export(pp, "pos_register")
            ids = set(cand["PosId"])
            if disp["ReceiptNo"].isin(ids).mean() > 0.9:
                pos_df = cand
                break
        if pos_df is None:
            print(f"\nERROR: no POS register export matches {loc}")
            failed += 1
            continue

        res = pipe.build(disp, breakdown, pos_df, alpine, args.period)
        print_validation(res)
        if res.ok and not args.validate_only:
            pipe.write(res)
        elif not res.ok:
            failed += 1

    if not args.validate_only and not failed:
        pipe.build_aggregates()
        print("\n  aggregates rebuilt: agg_category_week")

    pipe.close()
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
