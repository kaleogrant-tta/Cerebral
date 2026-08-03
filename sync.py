"""
Pull the current database from Drive and rebuild the dashboard file.

The scheduled GitHub run loads new data into the Drive copy, so a local
machine falls behind after every run. This brings it level.

    python sync.py
"""

import os
import subprocess
import sys
from pathlib import Path

from tta_env import bootstrap
from tta_drive import DriveClient


def main() -> int:
    bootstrap()
    folder = os.environ.get("TTA_DRIVE_STATE")
    if not folder:
        print("TTA_DRIVE_STATE is not set. Check .env.")
        return 1

    drive = DriveClient()
    f = drive.find(folder, "tta.duckdb")
    if not f:
        print("tta.duckdb not found in the Drive state folder.")
        return 1

    print(f"downloading {int(f['size'])/1e6:.0f} MB ...")
    drive.download(f["id"], Path("tta.duckdb"))

    print("rebuilding dashboard file ...")
    # Check the return code. An earlier version printed "local is now current"
    # unconditionally, so a failed rebuild looked like a success and the stale
    # dashboard file was blamed on caching.
    rc = subprocess.run([sys.executable, "publish.py"]).returncode
    if rc != 0:
        print("\n  REBUILD FAILED — tta.duckdb is current, but the dashboard")
        print("  file was not rebuilt. Fix the error above and run:")
        print("      python publish.py")
        return 1

    print("\nLocal is current. Run:  python -m streamlit run cerebral_public.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
