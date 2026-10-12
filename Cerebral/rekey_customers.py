"""One-off: move old name-hash customer history onto Alpine IDs.

    python rekey_customers.py --db ..\\tta_live.duckdb --dry-run
    python rekey_customers.py --db ..\\tta_live.duckdb

Dry run reports what would change and shows the weekly new-customer series as
it would look afterwards. Without --dry-run it applies Pipeline.rekey_history
logic (shared functions in tta_etl.py), logs to rekey_log, and prints the
before/after series. "New" = the customer_key's first basket ever, chain-wide.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from tta_etl import _XW_OK, _table_exists, rekey_apply, rekey_preview

WEEKS = 12


def weekly_new(con, resolved: bool) -> dict[int, int]:
    """New customers per ISO week for the last WEEKS weeks in fact_basket.

    resolved=True simulates the re-key by mapping keys through the crosswalk.
    """
    if resolved:
        # a join is far cheaper than a correlated subquery on big tables
        src = f"""(SELECT COALESCE(CASE WHEN x.name_hash IS NOT NULL AND {_XW_OK}
                                        THEN x.alpine_id END, b.customer_key) AS k,
                          b.txn_ts, b.iso_year * 100 + b.iso_week AS wk
                  FROM fact_basket b
                  LEFT JOIN customer_xwalk x ON b.customer_key = x.name_hash
                  WHERE b.customer_key IS NOT NULL)"""
    else:
        src = """(SELECT b.customer_key AS k, b.txn_ts,
                         b.iso_year * 100 + b.iso_week AS wk
                  FROM fact_basket b WHERE b.customer_key IS NOT NULL)"""
    rows = con.execute(f"""
        WITH first_wk AS (
            SELECT k, arg_min(wk, txn_ts) AS wk FROM {src} GROUP BY k),
        last_weeks AS (
            SELECT DISTINCT iso_year * 100 + iso_week AS wk FROM fact_basket
            ORDER BY wk DESC LIMIT {WEEKS})
        SELECT l.wk, COUNT(f.k) FROM last_weeks l
        LEFT JOIN first_wk f ON f.wk = l.wk GROUP BY l.wk ORDER BY l.wk
    """).fetchall()
    return {int(w): int(n) for w, n in rows}


def print_series(before: dict, after: dict, after_label: str) -> None:
    print(f"\n  {'ISO week':<10}{'before':>10}{after_label:>12}")
    for wk in sorted(before):
        print(f"  {wk:<10}{before[wk]:>10,}{after.get(wk, 0):>12,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help=r"path to the DuckDB file, e.g. ..\tta_live.duckdb")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    a = ap.parse_args()

    path = Path(a.db)
    if not path.is_file():
        print(f"ERROR: database not found: {path.resolve()}")
        return 2

    print(f"[1/4] Opening {path.resolve()} ({'read-only' if a.dry_run else 'read-write'})")
    con = duckdb.connect(str(path), read_only=a.dry_run)
    for t in ("fact_basket", "fact_line", "customer_xwalk"):
        if not _table_exists(con, t):
            print(f"ERROR: table {t} missing -- wrong database?")
            return 2

    print("[2/4] Counting what would be re-keyed ...")
    p = rekey_preview(con)
    amb = con.execute("SELECT COUNT(*) FROM customer_xwalk WHERE ambiguous").fetchone()[0]
    print(f"  baskets: {p['baskets_rekeyed']:,}   lines: {p['lines_rekeyed']:,}")
    print(f"  {p['distinct_customers_merged']:,} name-hash customers collapse into "
          f"{p['alpine_ids_targeted']:,} Alpine IDs")
    print(f"  (crosswalk rows skipped as ambiguous: {amb:,})")

    print(f"[3/4] Weekly new customers, last {WEEKS} weeks (before) ...")
    before = weekly_new(con, resolved=False)

    if a.dry_run:
        print("  --dry-run: computing the projected series ...")
        print_series(before, weekly_new(con, resolved=True), "projected")
        print("\nDry run only; nothing changed.")
        return 0

    if p["baskets_rekeyed"] == 0 and p["lines_rekeyed"] == 0:
        print("Nothing to re-key; database already consistent.")
        return 0

    print("[4/4] Applying re-key (single transaction) ...")
    res = rekey_apply(con)
    con.execute("""CREATE TABLE IF NOT EXISTS rekey_log (
                       run_ts TIMESTAMP, baskets BIGINT, lines BIGINT, customers BIGINT)""")
    con.execute("INSERT INTO rekey_log VALUES (now(), ?, ?, ?)",
                [res["baskets_rekeyed"], res["lines_rekeyed"],
                 res["distinct_customers_merged"]])
    print(f"  done: {res['baskets_rekeyed']:,} baskets, {res['lines_rekeyed']:,} lines, "
          f"{res['distinct_customers_merged']:,} customers merged "
          f"({res.get('redemptions_rekeyed', 0):,} redemption rows); logged to rekey_log")
    print_series(before, weekly_new(con, resolved=False), "after")
    con.close()
    print("\nNext: push the database with push_db.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
