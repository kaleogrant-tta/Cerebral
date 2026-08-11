"""
ingest_discount_groups.py -- load the Customer Discount Group Audit into
dim_discount_group_member in the ETL database.

    python ingest_discount_groups.py              dry run
    python ingest_discount_groups.py --apply      writes to the database

The audit is a Dutchie report listing every add/remove of a customer to a
discount group: who, which group, which action, when, by whom. It is the only
source that names a discount. The POS export records DiscountAmt but not the
reason, so without this the "everything else" bucket on the Discounting tab
stays anonymous.

MEMBERSHIP MODEL -- ever-member, floored at the first add:

  90.4% of customer x group pairs in the audit are a single Added with no
  Removed, so an interval reconstruction collapses to "from the add date
  onward" for nearly everyone. We keep first_added and last_removed so the
  5% of genuine closed intervals can be honoured, but the headline figure is
  ever-member: any discounted basket by someone who was ever in the group.

  Windowing costs real coverage -- 9,964 baskets against 12,897 ever -- and
  the audit's own left-censoring means the windows are not trustworthy
  anyway. Both are published; the tab leads with ever and offers windowed.

KNOWN LIMITS, stated here because they belong with the data:

  * LEFT-CENSORED. The audit starts 2025-07-01. Anyone added before that has
    no Added event and is invisible unless they were later removed. 93 pairs
    in the file start with a Removed, which is direct evidence of this.
  * PARTIAL MATCH. About 73% of audit customer IDs join to fact_basket.
    The rest never transacted, or transacted under a different key.
  * NOT THE WHOLE STORY. Group members account for roughly 15% of
    non-loyalty discount chain-wide. The remainder is promo codes, manual
    write-downs, and pre-window members.

Run this, then publish.py.
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
HEADER_ROW = 3

# Groups that are loyalty tiers rather than discount groups. They dominate the
# ranking by volume and answer a different question -- the Loyalty tab already
# covers tier behaviour -- so they are flagged, not dropped, and the tab
# separates them.
TIER_GROUPS = {
    "travel club frequent flyer",
    "travel club",
    "frequent flyer",
}


def classify(name: str) -> str:
    """Bucket a group name by what kind of discount it represents."""
    low = str(name or "").strip().lower()
    if low in TIER_GROUPS:
        return "Loyalty tier"
    if "employee" in low:
        return "Employee"
    if "first responder" in low or "veteran" in low:
        return "First responder / veteran"
    if "retail worker" in low or "friends and family" in low:
        return "Staff / friends & family"
    if "drinks on us" in low or "drinksonus" in low or "drink on us" in low:
        return "Neighbour business"
    if low.startswith(("soho -", "soho-", "5th ave", "5thave", "usq ",
                       "dtbk ", "fifth ave")):
        return "Neighbour business"
    return "Other"


def find_audit() -> Path | None:
    pats = ["**/*Discount Group Audit*.xls*", "**/*Discount_Group_Audit*.xls*"]
    for root in (".", os.path.expanduser("~/cerebral")):
        for pat in pats:
            hits = glob.glob(os.path.join(root, pat), recursive=True)
            if hits:
                return Path(sorted(hits)[-1])
    return None


def build(path: Path) -> pd.DataFrame:
    a = pd.read_excel(path, header=HEADER_ROW)
    need = {"Customer ID", "Discount Description", "Action", "Time"}
    missing = need - set(a.columns)
    if missing:
        raise ValueError(f"audit missing columns: {sorted(missing)}")

    a["ts"] = pd.to_datetime(a["Time"], errors="coerce")
    a = a.dropna(subset=["ts", "Customer ID", "Discount Description"])
    a["cid"] = a["Customer ID"].astype("Int64").astype(str)
    a["grp"] = a["Discount Description"].astype(str).str.strip()

    rows = []
    for (cid, grp), d in a.sort_values("ts").groupby(["cid", "grp"]):
        adds = d.loc[d.Action == "Added", "ts"]
        rems = d.loc[d.Action == "Removed", "ts"]
        # Closed only when a Removed follows the last Added. A Removed with no
        # Added at all means the member predates the audit window: treat the
        # membership as open from the beginning of time so their earlier
        # baskets are not silently dropped.
        closed = bool(len(rems)) and (not len(adds) or rems.max() > adds.max())
        rows.append({
            "customer_key": cid,
            "group_name": grp,
            "group_kind": classify(grp),
            "first_added": (adds.min() if len(adds)
                            else pd.Timestamp("2000-01-01")),
            "last_removed": (rems.max() if closed
                             else pd.Timestamp("2100-01-01")),
            "is_closed": closed,
            "pre_window": not len(adds),
            "events": int(len(d)),
        })
    return pd.DataFrame(rows)


def main() -> int:
    path = find_audit()
    if path is None:
        print("No Customer Discount Group Audit found under . or ~/cerebral")
        return 1

    print(f"AUDIT : {path}")
    print(f"DB    : {DB}  ({DB.stat().st_size/1e6:.1f} MB)")
    print(f"MODE  : {'APPLY' if APPLY else 'DRY RUN'}")
    print()

    mem = build(path)
    print(f"memberships     : {len(mem):,}")
    print(f"customers       : {mem.customer_key.nunique():,}")
    print(f"groups          : {mem.group_name.nunique():,}")
    print(f"closed intervals: {int(mem.is_closed.sum()):,} "
          f"({mem.is_closed.mean()*100:.1f}%)")
    print(f"pre-window      : {int(mem.pre_window.sum()):,}")
    print()
    print("BY KIND:")
    for k, n in mem.group_kind.value_counts().items():
        print(f"  {k:<28} {n:>5,}")

    con = duckdb.connect(str(DB), read_only=True)
    ids = set(mem.customer_key)
    con.execute("CREATE TEMP TABLE _m(cid VARCHAR)")
    con.executemany("INSERT INTO _m VALUES (?)", [(i,) for i in ids])
    hit = con.execute(
        "SELECT count(DISTINCT m.cid) FROM _m m "
        "JOIN fact_basket b ON b.customer_key = m.cid").fetchone()[0]
    print()
    print(f"join to fact_basket: {hit:,} of {len(ids):,} "
          f"({hit/max(len(ids),1)*100:.1f}%)")
    con.close()

    if not APPLY:
        print("\nDry run only. Re-run with --apply to write.")
        return 0

    bak = DB.with_name(f"tta.duckdb.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(DB, bak)
    print(f"\nbackup: {bak.name}")

    con = duckdb.connect(str(DB))
    con.register("mem_df", mem)
    con.execute("DROP TABLE IF EXISTS dim_discount_group_member")
    con.execute("""
        CREATE TABLE dim_discount_group_member AS
        SELECT customer_key, group_name, group_kind,
               CAST(first_added AS TIMESTAMP)  AS first_added,
               CAST(last_removed AS TIMESTAMP) AS last_removed,
               is_closed, pre_window, events
        FROM mem_df
    """)
    n = con.execute(
        "SELECT count(*) FROM dim_discount_group_member").fetchone()[0]
    con.close()
    print(f"written: dim_discount_group_member, {n:,} rows")
    print("\nNext: python publish.py --db ~/cerebral/tta.duckdb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
