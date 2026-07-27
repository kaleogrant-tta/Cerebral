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
from tta_drive import DriveClient, credentials, pull_inbox
from tta_etl import Pipeline, discover, read_export

from tta_env import bootstrap

bootstrap()

WORK = Path("/tmp/tta_work")
DB_LOCAL = WORK / DRIVE["db_filename"]


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------

def _jsonable(df: pd.DataFrame) -> list[list]:
    """Convert a frame to plain Python types gspread can serialise.

    Timestamps, numpy scalars, Decimals and NaN/NaT all reach the Sheets API
    as JSON and none of them survive the default encoder. Dates become ISO
    strings; missing values become empty cells.
    """
    import datetime as _dt
    import decimal as _dec

    out = []
    for row in df.itertuples(index=False, name=None):
        cells = []
        for v in row:
            # NaT is a Timestamp subclass and has no strftime, so the null
            # check has to come first and cover every scalar type.
            try:
                if v is None or (not isinstance(v, (list, tuple, dict))
                                 and pd.isna(v)):
                    cells.append("")
                    continue
            except (TypeError, ValueError):
                pass
            if isinstance(v, (pd.Timestamp, _dt.datetime)):
                cells.append(v.strftime("%Y-%m-%d %H:%M:%S"))
            elif isinstance(v, _dt.date):
                cells.append(v.strftime("%Y-%m-%d"))
            elif isinstance(v, _dec.Decimal):
                cells.append(float(v))
            elif hasattr(v, "item"):          # numpy scalar
                try:
                    cells.append(v.item())
                except Exception:
                    cells.append(str(v))
            else:
                cells.append(v)
        out.append(cells)
    return out


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
            SELECT strftime(loaded_at, '%Y-%m-%d %H:%M:%S') AS loaded_at,
                   store_key, period, lines, baskets,
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

        ws.update([df.columns.tolist()] + _jsonable(df),
                  value_input_option="RAW")
        print(f"    published {tab}: {len(df):,} rows")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

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

    # No Drive lock: a service account has no storage quota and cannot CREATE
    # files, only update ones you own. The workflow's `concurrency` group
    # already guarantees one run at a time, which is the real protection.
    if True:
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

        # Inventory is daily and append-only. Transactions are weekly. A run
        # may legitimately find only one of them — most days it is inventory
        # alone — so each is handled independently and neither is required.
        did_something = False

        if "inventory" in found:
            print("  [3/6] inventory snapshot(s)")
            pipe = Pipeline(str(DB_LOCAL))
            for p in found["inventory"]:
                stamp = _export_date(p)          # from the export header
                counts = pipe.load_inventory(read_export(p, "inventory"), stamp)
                print(f"    {stamp}: {counts}")
            pipe.close()
            did_something = True
        else:
            print("  [3/6] no inventory export today")

        have_txn = all(k in found for k in
                       ("dispensations", "breakdown", "pos_register"))
        if have_txn:
            print("  [4/6] running ETL")
            period = _infer_period(found["dispensations"][0])
            rc = os.system(
                f'python3 tta_etl.py --inbox "{local_inbox}" '
                f'--db "{DB_LOCAL}" --period {period}'
            )
            if rc != 0:
                print("  ETL reported failures — Drive left untouched")
                return 1
            did_something = True
        elif any(k in found for k in ("dispensations", "breakdown", "pos_register")):
            # A partial set is a mistake, not a valid state.
            missing = [k for k in ("dispensations", "breakdown", "pos_register")
                       if k not in found]
            print(f"  [4/6] INCOMPLETE transaction export set — missing "
                  f"{missing}. Nothing processed; files left in the inbox.")
            return 1
        else:
            print("  [4/6] no transaction exports today")

        if not did_something:
            print("    inbox had nothing loadable — stopping")
            return 0

        # --- publish -----------------------------------------------------
        print("  [5/6] publishing to Sheets")
        con = duckdb.connect(str(DB_LOCAL))
        publish(con, sheet_id)
        con.close()

        # --- persist -----------------------------------------------------
        print("  [6/6] pushing database back")
        drive.upload(DB_LOCAL, state_id, DRIVE["db_filename"])

        # Slim, PII-free copy for the shared dashboard.
        print("        building published dashboard file")
        from publish import build, SLIM
        slim = WORK / SLIM
        stats = build(str(DB_LOCAL), str(slim))
        print(f"        {SLIM}: {stats.pop('_size_mb')} MB")
        drive.upload(slim, state_id, SLIM)

        # Archiving is housekeeping, not data integrity. Everything that
        # matters — the database, the published dashboard file, the Sheets
        # tables — is already written by this point, so a failure here must
        # not fail the run. Files simply stay in the inbox and get reprocessed
        # next time, which is harmless: loading a period replaces it.
        print("        archiving processed exports")
        moved, failed = 0, []
        for _, file_id in pulled:
            try:
                drive.move(file_id, archive_id, from_folder=inbox_id)
                moved += 1
            except Exception as e:
                failed.append(f"{file_id}: {type(e).__name__}")
        print(f"    archived {moved}/{len(pulled)} file(s)")
        if failed:
            print(f"    ! {len(failed)} could not be archived — they remain in "
                  f"the inbox and will be reprocessed next run (harmless).")
            for f in failed[:3]:
                print(f"      {f}")

    print("done")
    return 0


def _infer_period(path: Path) -> str:
    """Label the load by the month its data covers.

    Reading the From Date header rather than the run date, so a Monday upload
    of last week's exports is labelled with last week's month.
    """
    head = pd.read_excel(path, header=None, nrows=4)
    for _, row in head.iterrows():
        cells = [str(v) for v in row.tolist()]
        if any("From Date" in c for c in cells):
            for c in cells:
                try:
                    return pd.to_datetime(c).strftime("%Y-%m")
                except Exception:
                    continue
    return pd.Timestamp.now().strftime("%Y-%m")


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
