"""
audience_ingest.py — AIQ audience roster + sales exports into Cerebral.

Reads AIQ's two per-audience exports and writes three normalized tables:

    audience_members   one row per (audience_id, contact_id)
    audience_pos_ids   one row per (contact_id, pos_customer_id)  <- crosswalk feed
    audience_sales     one row per sales line item

AIQ filename conventions (both start with the account id, 2260):
    2260-{audience_id}-{epoch}-rp.csv                     roster
    2260-{audience_id}-{from}-{to}-sl.csv                 sales

The audience id is parsed from the filename, so nothing depends on the
audience name — which is unreliable (HighNotes609Guests vs HighNotes 6 30 2026).

Design notes
------------
* Append-only. Every ingest is stamped with a snapshot_date and nothing is
  overwritten. Exports are manual point-in-time pulls, so if a member drops out
  of a form-capture audience later, the earlier snapshot still holds them.
  Downstream takes first-seen per (audience_id, contact_id).

* src_ids is exploded. AIQ's format is
      leaflogix:::946287:::SoHo| leaflogix:::898670:::Downtown Brooklyn
  Segment 2 is the POS customer id; segment 3 is the store the customer record
  was registered at, NOT where they shopped. 511,699 of 622,522 personas carry
  all four stores. Do not read it as visit history.

  A persona commonly carries several distinct POS ids (32% of the base) because
  AIQ merged duplicate POS records. Cohort spend must sum across all of them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

log = logging.getLogger(__name__)

RE_ROSTER = re.compile(r"^2260-(\d+)-\d+-rp\.csv$", re.I)
RE_SALES = re.compile(r"^2260-(\d+)-.*-sl\.csv$", re.I)

# leaflogix:::946287:::SoHo   (the store segment is optional on some rows)
RE_SRC = re.compile(r"(?P<system>[a-z]+):::(?P<pos_id>[^:|]+)(?::::?(?P<store>[^|]*))?")

# Only this system's ids are POS customer ids. Verified against 31 audiences:
# a leaflogix id is present for 1,340 of 1,356 members who have ever transacted,
# and for 0 of 3,600 who have not. Its presence is therefore equivalent to
# "this person has a POS record", not to "we managed to match this person".
POS_SYSTEM = "leaflogix"

ROSTER_KEEP = {
    "contact_id": "contact_id",
    "src_ids": "src_ids",
    "email": "email",
    "mobile_phone": "phone",
    "loyalty": "is_loyalty",
    "loyalty_signup_time": "loyalty_signup_at",
    "favorite_store": "favorite_store",
    "count_of_sales": "lifetime_sales_count",
    "total_spent_as_member": "lifetime_spend",
    "File Upload": "upload_tag",
    "Employee Signup": "signup_employee",
    "seed_source": "seed_source",
}

SALES_KEEP = {
    "transaction_id": "transaction_id",
    "time_of_sale": "sold_at",
    "pos_customer_id": "pos_customer_raw",
    "store_id": "store",
    "product_name": "product_name",
    "brand": "brand",
    "quantity": "quantity",
    "cost": "unit_cost",
    "price": "price",
    "discount": "discount",
    "parent_category": "parent_category",
    "category": "category",
    "sku": "sku",
    "customer_aiq_contact_id": "contact_id",
    "employee": "employee",
}


@dataclass
class IngestResult:
    audience_id: str
    roster_rows: int
    member_rows: int
    pos_id_rows: int
    sales_rows: int
    members_with_pos_record: int

    @property
    def pos_record_rate(self) -> float:
        """Share of the roster that has ever had a POS record.

        NOT a match rate. A member without a leaflogix id has never transacted
        at TTA — this is a fact about the cohort, not a limitation of the join.
        """
        return self.members_with_pos_record / self.member_rows if self.member_rows else 0.0


def parse_src_ids(raw: str | None) -> list[tuple[str, str, str | None]]:
    """Explode an src_ids cell into [(system, source_id, store), ...].

    Deduplicated on (system, id) — the same POS id repeats once per store it was
    registered at, which would otherwise multiply every join.

    Returns ALL systems. Callers must filter: only `leaflogix` tokens are POS
    customer ids. `rawfiles` tokens are 36-char upload-provenance UUIDs that
    look nothing like a POS id, and treating them as one inflates the
    denominator of every match-rate calculation.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    seen: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    for token in raw.split("|"):
        token = token.strip()
        if not token:
            continue
        m = RE_SRC.match(token)
        if not m:
            log.debug("unparsed src_ids token: %r", token)
            continue
        system = m.group("system").lower()
        source_id = m.group("pos_id").strip()
        store = (m.group("store") or "").strip() or None
        seen.setdefault((system, source_id), (system, source_id, store))
    return list(seen.values())


def _split_pos_customer(raw: str | None) -> str | None:
    """`946287:::SoHo` -> `946287`. Matches segment 2 of src_ids."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.split(":::")[0].strip() or None


def discover(inbox: Path) -> dict[str, dict[str, Path]]:
    """Map audience_id -> {'roster': path, 'sales': path}.

    If AIQ has been asked for the same audience twice, the newest file wins —
    the epoch in the filename sorts correctly as a string because all AIQ
    timestamps are the same width.
    """
    found: dict[str, dict[str, Path]] = {}
    for path in sorted(inbox.rglob("*.csv")):
        for regex, kind in ((RE_ROSTER, "roster"), (RE_SALES, "sales")):
            m = regex.match(path.name)
            if m:
                found.setdefault(m.group(1), {})[kind] = path
                break
    return found


def load_roster(path: Path, audience_id: str, snapshot: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(path, dtype=str, low_memory=False)
    present = {src: dst for src, dst in ROSTER_KEEP.items() if src in df.columns}
    missing = set(ROSTER_KEEP) - set(present)
    if missing:
        log.warning("audience %s roster missing columns: %s", audience_id, sorted(missing))

    members = df[list(present)].rename(columns=present).copy()
    members["audience_id"] = audience_id
    members["snapshot_date"] = snapshot
    members["is_loyalty"] = members.get("is_loyalty", pd.Series(dtype=str)).eq("true")
    for col in ("lifetime_sales_count", "lifetime_spend"):
        if col in members:
            members[col] = pd.to_numeric(members[col], errors="coerce").fillna(0)
    if "loyalty_signup_at" in members:
        members["loyalty_signup_at"] = pd.to_datetime(members["loyalty_signup_at"], errors="coerce")

    pos_rows, other_rows = [], []
    for contact_id, raw in zip(df.get("contact_id", []), df.get("src_ids", [])):
        for system, source_id, store in parse_src_ids(raw):
            row = {
                "contact_id": contact_id,
                "pos_system": system,
                "pos_customer_id": source_id,
                "registered_store": store,
                "snapshot_date": snapshot,
            }
            (pos_rows if system == POS_SYSTEM else other_rows).append(row)

    cols = ["contact_id", "pos_system", "pos_customer_id", "registered_store", "snapshot_date"]
    pos_ids = pd.DataFrame(pos_rows, columns=cols)
    if other_rows:
        systems = sorted({r["pos_system"] for r in other_rows})
        log.debug("audience %s: %d non-POS src_ids tokens (%s) held out of "
                  "audience_pos_ids", audience_id, len(other_rows), ", ".join(systems))

    members = members.drop(columns=["src_ids"], errors="ignore")
    return members, pos_ids


def load_sales(path: Path, audience_id: str, snapshot: date) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, low_memory=False)
    if df.empty:
        return pd.DataFrame(columns=list(SALES_KEEP.values()) + ["audience_id", "snapshot_date"])

    present = {src: dst for src, dst in SALES_KEEP.items() if src in df.columns}
    sales = df[list(present)].rename(columns=present).copy()
    sales["audience_id"] = audience_id
    sales["snapshot_date"] = snapshot
    sales["sold_at"] = pd.to_datetime(sales["sold_at"], errors="coerce")
    sales["pos_customer_id"] = sales["pos_customer_raw"].map(_split_pos_customer)
    for col in ("quantity", "unit_cost", "price", "discount"):
        if col in sales:
            sales[col] = pd.to_numeric(sales[col], errors="coerce").fillna(0)

    bad = sales["sold_at"].isna().sum()
    if bad:
        log.warning("audience %s: %d sales rows with unparseable time_of_sale", audience_id, bad)
    return sales.drop(columns=["pos_customer_raw"])


DDL = """
CREATE TABLE IF NOT EXISTS audience_members (
    audience_id          VARCHAR,
    contact_id           VARCHAR,
    email                VARCHAR,
    phone                VARCHAR,
    is_loyalty           BOOLEAN,
    loyalty_signup_at    TIMESTAMP,
    favorite_store       VARCHAR,
    lifetime_sales_count DOUBLE,
    lifetime_spend       DOUBLE,
    upload_tag           VARCHAR,
    signup_employee      VARCHAR,
    seed_source          VARCHAR,
    snapshot_date        DATE
);
CREATE TABLE IF NOT EXISTS audience_pos_ids (
    contact_id       VARCHAR,
    pos_system       VARCHAR,
    pos_customer_id  VARCHAR,
    registered_store VARCHAR,
    snapshot_date    DATE
);
CREATE TABLE IF NOT EXISTS audience_sales (
    audience_id     VARCHAR,
    transaction_id  VARCHAR,
    sold_at         TIMESTAMP,
    contact_id      VARCHAR,
    pos_customer_id VARCHAR,
    store           VARCHAR,
    product_name    VARCHAR,
    brand           VARCHAR,
    category        VARCHAR,
    parent_category VARCHAR,
    sku             VARCHAR,
    quantity        DOUBLE,
    unit_cost       DOUBLE,
    price           DOUBLE,
    discount        DOUBLE,
    employee        VARCHAR,
    snapshot_date   DATE
);
"""


def _append(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    for col in cols:
        if col not in df.columns:
            df[col] = None
    con.register("_staged", df[cols])
    con.execute(f"INSERT INTO {table} SELECT * FROM _staged")
    con.unregister("_staged")


def ingest(inbox: Path, db_path: Path, snapshot: date | None = None) -> list[IngestResult]:
    snapshot = snapshot or date.today()
    files = discover(inbox)
    if not files:
        log.warning("no AIQ exports found under %s", inbox)
        return []

    con = duckdb.connect(str(db_path))
    con.execute(DDL)
    results: list[IngestResult] = []

    for audience_id, paths in sorted(files.items()):
        if "roster" not in paths:
            log.warning("audience %s has a sales export but no roster — skipping", audience_id)
            continue

        members, pos_ids = load_roster(paths["roster"], audience_id, snapshot)
        sales = (
            load_sales(paths["sales"], audience_id, snapshot)
            if "sales" in paths
            else pd.DataFrame()
        )

        _append(con, "audience_members", members)
        _append(con, "audience_pos_ids", pos_ids)
        _append(con, "audience_sales", sales)

        with_pos = pos_ids["contact_id"].nunique() if not pos_ids.empty else 0
        res = IngestResult(
            audience_id=audience_id,
            roster_rows=len(members),
            member_rows=len(members),
            pos_id_rows=len(pos_ids),
            sales_rows=len(sales),
            members_with_pos_record=with_pos,
        )
        results.append(res)
        log.info(
            "audience %s: %d members, %d POS ids (%.0f%% have a POS record), %d sales rows",
            audience_id, res.member_rows, res.pos_id_rows,
            100 * res.pos_record_rate, res.sales_rows,
        )
        if res.pos_record_rate < 0.50 and res.member_rows >= 25:
            log.info(
                "audience %s: %.0f%% of attendees have ever transacted at TTA. "
                "This is a finding about the event, not a data gap.",
                audience_id, 100 * res.pos_record_rate,
            )

    con.close()
    return results


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Ingest AIQ audience exports into Cerebral.")
    ap.add_argument("--inbox", type=Path, required=True, help="directory of AIQ CSV exports")
    ap.add_argument("--db", type=Path, required=True, help="target DuckDB file")
    ap.add_argument("--snapshot", type=date.fromisoformat, default=None)
    args = ap.parse_args()

    out = ingest(args.inbox, args.db, args.snapshot)
    print(f"\ningested {len(out)} audiences")
    for r in out:
        print(f"  {r.audience_id:>8}  {r.member_rows:>5} members  "
              f"{100 * r.pos_record_rate:>5.0f}% w/ POS record  "
              f"{r.sales_rows:>6} sales rows")
