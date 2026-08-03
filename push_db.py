from tta_env import bootstrap
from tta_drive import DriveClient
from pathlib import Path
import os
bootstrap()
DriveClient().upload(Path("tta.duckdb"), os.environ.get("TTA_DRIVE_STATE"))
print("tta.duckdb pushed to Drive")
