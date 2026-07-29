from tta_env import bootstrap; bootstrap()
from tta_drive import DriveClient
from pathlib import Path
import os, duckdb, tempfile

d = DriveClient(); folder = os.environ["TTA_DRIVE_STATE"]
f = d.find(folder, "cerebral_dash.duckdb")
if not f:
    print("cerebral_dash.duckdb is NOT in Drive"); raise SystemExit(1)

print(f"in Drive: {int(f['size'])/1e6:.2f} MB   modified {f['modifiedTime']}")
print(f"local   : {Path('cerebral_dash.duckdb').stat().st_size/1e6:.2f} MB")

tmp = Path(tempfile.gettempdir()) / "verify.duckdb"
d.download(f["id"], tmp)
c = duckdb.connect(str(tmp), read_only=True)
n = c.execute("SELECT COUNT(*) FROM dash_brand_redemption").fetchone()[0]
o = c.execute("SELECT COUNT(*) FROM dash_offer_performance").fetchone()[0]
built = c.execute("SELECT built_at FROM dash_meta").fetchone()[0]
c.close(); tmp.unlink()

print(f"\nThe file Drive is serving:")
print(f"  dash_brand_redemption : {n} rows")
print(f"  dash_offer_performance: {o} rows")
print(f"  built at              : {built}")
print("\n" + ("Drive is current — the app is showing a cached copy. Reboot it."
               if n > 0 else
               "Drive still has the OLD file. Re-run the upload."))
