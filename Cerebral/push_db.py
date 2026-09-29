"""Upload a tta.duckdb to the Drive state folder, replacing the live copy.

The workflow runs this AFTER vm_ingest / inv_ingest so the database on Drive
carries fact_inventory_week and the ledger-rollback snapshots, not just the
ETL's sales load (tta_refresh.py uploads before those steps run).
"""
import os, sys
from pathlib import Path
from tta_env import bootstrap
from tta_drive import DriveClient

bootstrap()
try:
    from tta_config import DRIVE
    NAME = DRIVE["db_filename"]
except Exception:
    NAME = "tta.duckdb"
db = Path(sys.argv[1] if len(sys.argv) > 1 else "../tta.duckdb").resolve()
if not db.exists():
    sys.exit(f"no such file: {db}")
print(f"uploading {db} ({db.stat().st_size/1e6:.0f} MB) -> Drive state folder as {NAME}", flush=True)
DriveClient().upload(db, os.environ["TTA_DRIVE_STATE"], NAME)
print("done")
