"""
Scheduled refresh: Drive -> DuckDB -> Sheets.

Runs unattended in GitHub Actions. One engine, one set of rules -- this
orchestrates the same tta_etl code that processes bulk history locally.

Sequence
--------
  1. acquire Drive lock
  2. pull tta.duckdb from Drive state folder
  3. pull new exports from Drive inbox
  4. run the ETL (rolling reprocess window)
  5. rebuild aggregates
  6. publish small agg tables to Sheets
  7. push tta.duckdb back, archive processed exports
  8. release lock

Failure at any step leaves Drive untouched: the database is only uploaded
after a successful run, and inbox files are only archived after that.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import duckdb
import gspread
import pandas as pd

from tta_config import CONFIG_VERSION, DRIVE, REPROCESS_PERIODS, SHEETS
from tta_drive import DriveClient, DriveLock, credentials, pull_inbox
from tta_etl import Pipeline, discover, read_export

from tta_env import bootstrap

bootstrap()

WORK = Path("/tmp/tta_work")
DB_LOCAL = WORK / DRIVE["db_filename"]


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------

def publish(con: duckdb.DuckDBPyConnection, sheet_id: str) -> None:
    """Push only small aggregate tables. Raw facts never leave DuckDB."""
    gc = gspread.authorize(credentials())
    wb = gc.open_by_key(sheet_id)

    queries = {
        "agg_category_week": """
            SELECT store_key, iso_year, iso_week, channel, category,
                   ROUND(net_sales,2)    AS net_sales,
                   ROUND(gross_margin,2) AS gross_margin,
                   units, baskets_containing, total_baskets, days_open,
                   ROUND(penetration,4)              AS penetration,
                   ROUND(dollars_per_100_baskets,2)  AS dollars_per_100_baskets,
                   ROUND(margin_pct,4)               AS margin_pct
            FROM agg_category_week
            ORDER BY iso_year DESC, iso_week DESC, store_key, channel, category
        """,
        "load_log": """
            SELECT loaded_at, store_key, period, lines, baskets,
                   passed, warnings, config_version
            FROM load_log ORDER BY loaded_at DESC
        """,
    }

    for tab, sql in queries.items():
        df = con.execute(sql).df()
        if len(df) > SHEETS["max_rows"]:
            raise RuntimeError(
                f"{tab} has {len(df):,} rows, above the {SHEETS['max_rows']:,} "
                f"cap. Aggregate further before publishing -- Sheets is a "
                f"serving layer, not storage."
            )
        try:
            ws = wb.worksheet(tab)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = wb.add_worksheet(tab, rows=len(df) + 10, cols=max(len(df.columns), 5))

        # Sheets accepts only JSON scalars. Timestamps/Dates (load_log's
        # loaded_at is one) must become text, and NaN -> "".
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
        df = df.astype(object).where(pd.notna(df), "")
        rows = [[_json_safe(v) for v in row] for row in df.values.tolist()]
        ws.update([df.columns.tolist()] + rows,
                  value_input_option="RAW")
        print(f"    published {tab}: {len(df):,} rows")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _json_safe(v):
    """Last-line defense for Sheets publishing: convert any lingering
    date/datetime objects to strings so JSON serialization can't fail."""
    import datetime as _dt
    if isinstance(v, (pd.Timestamp, _dt.datetime)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, _dt.date):
        return v.strftime("%Y-%m-%d")
    return v


def env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} is not set")
    return val


def main() -> int:
    inbox_id   = env(DRIVE["inbox_folder_env"])
    archive_id = env(DRIVE["archive_folder_env"])
    state_id   = env(DRIVE["state_folder_env"])
    sheet_id   = env(SHEETS["workbook_id_env"])

    WORK.mkdir(parents=True, exist_ok=True)
    local_inbox = WORK / "inbox"
    drive = DriveClient()

    print(f"TTA scheduled refresh — config {CONFIG_VERSION}")
    # Prove the secrets point at the folders you think they do. If this shows
    # an old/duplicate folder name, the secret IDs are the real bug.
    print(f"    service account: "
          f"{getattr(credentials(), 'service_account_email', '<unknown>')}")
    print(f"    inbox   -> {drive.folder_label(inbox_id)}")
    print(f"    archive -> {drive.folder_label(archive_id)}")
    print(f"    state   -> {drive.folder_label(state_id)}")

    with DriveLock(drive, state_id, DRIVE["lock_filename"]):
        # --- state -------------------------------------------------------
        print("  [1/6] pulling database")
        existing = drive.find(state_id, DRIVE["db_filename"])
        if existing:
            drive.download(existing["id"], DB_LOCAL)
            print(f"    {DB_LOCAL.stat().st_size/1e6:.1f} MB")
        else:
            print("    no database in Drive — starting fresh")

        # --- inbox -------------------------------------------------------
        print("  [2/6] pulling exports")
        pulled = pull_inbox(drive, inbox_id, local_inbox)
        if not pulled:
            print("    inbox empty — nothing to do")
            return 0

        found = discover(local_inbox)
        if "inventory" in found:
            pipe = Pipeline(str(DB_LOCAL))
            for p in found["inventory"]:
                # Snapshot date comes from the export header, not the filename.
                stamp = _export_date(p)
                counts = pipe.load_inventory(read_export(p, "inventory"), stamp)
                print(f"    inventory {stamp}: {counts}")
            pipe.close()

        # --- transform ---------------------------------------------------
        print("  [3/6] running ETL")
        rc = os.system(
            f"python3 tta_etl.py --inbox {local_inbox} --db {DB_LOCAL} "
            f"--period scheduled"
        )
        if rc != 0:
            print("  ETL reported failures — Drive left untouched")
            return 1

        # --- publish -----------------------------------------------------
        print("  [4/6] publishing to Sheets")
        con = duckdb.connect(str(DB_LOCAL))
        publish(con, sheet_id)
        con.close()

        # --- persist -----------------------------------------------------
        print("  [5/6] pushing database back")
        drive.upload(DB_LOCAL, state_id, DRIVE["db_filename"])

        print("  [6/6] archiving processed exports")
        failures = []
        for path, file_id in pulled:
            try:
                drive.archive(file_id, archive_id)
                print(f"    archived {path.name}")
            except Exception as e:
                failures.append(path.name)
                print(f"    !! could not archive {path.name}: {e}")
        if failures:
            raise RuntimeError(
                f"{len(failures)} file(s) could not be archived: {failures}. "
                f"Copies may already be in TTA/archive; originals remain in "
                f"the inbox. Check that the service account printed at the "
                f"top of this log is a Content manager on the TTA shared "
                f"drive."
            )
        # The real verification: re-list the inbox. Drive hides the parents
        # field from service accounts, but folder listings always tell the
        # truth -- if anything we processed is still there, fail loudly.
        remaining = {f["id"] for f in drive.list_files(inbox_id)}
        leftovers = [p.name for p, fid in pulled if fid in remaining]
        if leftovers:
            raise RuntimeError(
                f"still in the inbox after archiving: {leftovers}. "
                f"Check the service account's access to TTA/archive."
            )
        print(f"    archived {len(pulled)} file(s)")

    print("done")
    return 0


def _export_date(path: Path) -> str:
    """Read the 'Export Date:' cell. Inventory has no date column, so this is
    the only trustworthy source for snapshot_date."""
    head = pd.read_excel(path, header=None, nrows=3)
    for _, row in head.iterrows():
        cells = [str(v) for v in row.tolist()]
        if any("Export Date" in c for c in cells):
            for c in cells:
                try:
                    return pd.to_datetime(c).strftime("%Y-%m-%d")
                except Exception:
                    continue
    raise ValueError(f"{path.name}: no Export Date found — refusing to guess "
                     f"the snapshot date")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
