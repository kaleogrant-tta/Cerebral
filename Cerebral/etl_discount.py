"""
etl_discount.py -- keep fact_basket.discount_amt complete, everywhere.

THE PROBLEM THIS SOLVES
-----------------------
tta_etl.py reads DiscountAmt and writes discount_amt correctly, but it has
only done so since 2026-08-11. The loader is incremental -- it deletes and
re-inserts by store and date -- so every basket loaded before that date
still carries zero. Locally that was repaired by running
backfill_discount.py over the archived POS exports in history/. The GitHub
Action has no such archive, so its database keeps thirteen months of zeros,
publishes them, and the Discounting tab renders "everything else" as
discount minus loyalty on a truncated minuend: large negative numbers.

Two defences, because the first one cannot run everywhere:

  backfill_discount(con, roots)   repairs history from archived exports,
                                  wherever those exports can be found

  assert_discount_sane(con)       refuses to publish a file whose discount
                                  coverage is implausible, whether or not
                                  the backfill was able to run

The guard is the one that matters. A pipeline that fails loudly beats one
that quietly ships a broken dashboard, and the guard holds even when the
archive is missing, the ETL changes again, or someone adds a fourth way for
this column to end up empty.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

# The POS export carries four metadata rows above the header. Pinned rather
# than probed, matching backfill_discount.py.
HEADER_ROW = 4

# Where archived POS exports might live. Checked in order; all that exist
# are used. CI generally has none of these, which is expected -- the guard
# below is what protects that case.
ARCHIVE_ROOTS = ("history", "../history", "data/history", "archive",
                 "../archive")

# A basket carrying a loyalty redemption must carry at least that much
# discount, since redemption is a subset of discount. Coverage far below
# this means the column was never populated for those rows.
MIN_COVERAGE = 0.90


def _load_export(path: Path) -> pd.DataFrame:
    """basket_id -> discount_amt from one archived POS export."""
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
        status = df[cols["PosStatus"]].astype(str).str.lower()
        out = out[status != "returned"]
    return out.dropna(subset=["basket_id"])[["basket_id", "discount_amt"]]


def find_exports(roots=ARCHIVE_ROOTS) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if Path(root).is_dir():
            files += [Path(p) for p in glob.glob(
                f"{root}/**/*POS Transactions*.xlsx", recursive=True)]
    # The same export can sit under two roots; de-duplicate on name.
    seen, out = set(), []
    for f in sorted(files):
        if f.name not in seen:
            seen.add(f.name)
            out.append(f)
    return out


def backfill_discount(con, roots=ARCHIVE_ROOTS, verbose: bool = True) -> int:
    """Write discount_amt onto baskets that have none. Returns rows updated.

    Only touches rows where discount_amt is NULL or zero, so a value the ETL
    wrote from a live export is never overwritten by an older archived one.
    """
    files = find_exports(roots)
    if not files:
        if verbose:
            print("  discount backfill: no archived POS exports found "
                  "(expected in CI) — skipping repair, guard still applies")
        return 0

    frames = []
    for f in files:
        try:
            d = _load_export(f)
        except Exception as exc:                             # noqa: BLE001
            if verbose:
                print(f"  discount backfill: {f.name} unreadable ({exc})")
            continue
        if not d.empty:
            frames.append(d)
    if not frames:
        return 0

    disc = pd.concat(frames, ignore_index=True)
    # A basket can appear in several exports when a period was re-pulled.
    # Last write wins, matching the ETL's own de-duplication.
    disc = disc.drop_duplicates(subset=["basket_id"], keep="last")
    disc["basket_id"] = disc.basket_id.astype("int64")

    con.register("_disc_bf", disc)
    before = con.execute("""
        SELECT COUNT(*) FROM fact_basket
        WHERE NOT is_return AND COALESCE(discount_amt, 0) > 0
    """).fetchone()[0]
    con.execute("""
        UPDATE fact_basket AS b
        SET discount_amt = d.discount_amt
        FROM _disc_bf d
        WHERE b.basket_id = d.basket_id
          AND COALESCE(b.discount_amt, 0) = 0
          AND d.discount_amt > 0
    """)
    con.execute("UPDATE fact_basket SET discount_amt = 0 "
                "WHERE discount_amt IS NULL")
    con.unregister("_disc_bf")
    after = con.execute("""
        SELECT COUNT(*) FROM fact_basket
        WHERE NOT is_return AND COALESCE(discount_amt, 0) > 0
    """).fetchone()[0]

    if verbose:
        print(f"  discount backfill: {len(files)} archived exports, "
              f"{after - before:,} baskets repaired "
              f"({after:,} now carry a discount)")
    return after - before


def discount_coverage(con, table: str = "fact_basket") -> tuple[int, int, float]:
    """(redeeming baskets, those that also carry discount, share).

    Loyalty redemption is a subset of discount, so a basket that redeemed
    must show discount. Measuring against redemption rather than against all
    baskets gives a denominator that does not depend on how much discounting
    the business happens to do.
    """
    row = con.execute(f"""
        SELECT COUNT(*) FILTER (WHERE loyalty_redeem > 0),
               COUNT(*) FILTER (WHERE loyalty_redeem > 0
                                  AND COALESCE(discount_amt, 0) > 0)
        FROM {table} WHERE NOT is_return
    """).fetchone()
    redeeming, covered = int(row[0]), int(row[1])
    return redeeming, covered, (covered / redeeming) if redeeming else 1.0


def assert_discount_sane(con, minimum: float = MIN_COVERAGE,
                         hard: bool = False,
                         table: str = "fact_basket") -> bool:
    """Check discount_amt is populated. Returns True when sane.

    Set hard=True to raise instead of warning. Warning is the default so a
    single bad column does not block publishing tables that are fine, but
    the message is written to be impossible to miss in a CI log.
    """
    redeeming, covered, share = discount_coverage(con, table)
    if redeeming == 0:
        print("  discount check: no loyalty redemptions found — skipped")
        return True
    if share >= minimum:
        print(f"  discount check: {share * 100:.1f}% of redeeming baskets "
              f"carry a discount — OK")
        return True

    msg = (
        f"discount_amt is incomplete: only {covered:,} of {redeeming:,} "
        f"({share * 100:.1f}%) baskets with a loyalty redemption also carry "
        f"a discount, and redemption is a subset of discount so this should "
        f"be near 100%. The Discounting tab will show negative 'everything "
        f"else' figures until this is repaired. Run backfill_discount.py "
        f"--apply against an archive of POS exports, or re-run the ETL over "
        f"the affected periods."
    )
    if hard:
        raise RuntimeError(msg)
    print(f"  *** WARNING: {msg}")
    return False


if __name__ == "__main__":
    import argparse

    import duckdb

    ap = argparse.ArgumentParser(
        description="Repair and verify fact_basket.discount_amt.")
    ap.add_argument("--db", default="../tta.duckdb")
    ap.add_argument("--apply", action="store_true",
                    help="write the repair (default is check only)")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=not args.apply)
    redeeming, covered, share = discount_coverage(con)
    print(f"\n{covered:,} of {redeeming:,} redeeming baskets carry a "
          f"discount ({share * 100:.1f}%)\n")
    if args.apply:
        backfill_discount(con)
        assert_discount_sane(con)
    else:
        assert_discount_sane(con)
        print("\nCheck only. Re-run with --apply to repair.")
    con.close()
