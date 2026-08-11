"""
backfill_receipts.py -- load archived Inventory Receipt Reports into
fact_receipt in the live ETL database.

    python backfill_receipts.py              dry run, shows what would load
    python backfill_receipts.py --apply      writes to the database

The live DB is one level up from this folder. A timestamped backup is taken
before any write. load_receipts is idempotent per store per receive_date,
so re-running over an overlapping range replaces rather than duplicates.
"""
import glob, os, shutil, sys, datetime
from pathlib import Path
import duckdb, pandas as pd
from tta_etl import Pipeline, read_export

DB = Path(os.path.expanduser("~/cerebral/tta.duckdb"))
APPLY = "--apply" in sys.argv

files = sorted(Path(p) for p in glob.glob("history/**/*eceipt*.xlsx", recursive=True))
print(f"DB      : {DB}  ({DB.stat().st_size/1e6:.1f} MB)")
print(f"MODE    : {'APPLY' if APPLY else 'DRY RUN'}")
print(f"FILES   : {len(files)}")
for f in files:
    print("   ", f)
print()

for f in files:
    df = read_export(f, "inventory_receipt")
    n = df["Product Name"].astype(str).str.lower().str.contains("gwp").sum()
    print(f"{f.name}: {len(df):,} rows, {n} GWP rows, "
          f"stores={sorted(df['Location Name'].astype(str).unique())}")
print()

if not APPLY:
    print("Dry run only. Re-run with --apply to write.")
    sys.exit(0)

bak = DB.with_name(f"tta.duckdb.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
shutil.copy2(DB, bak)
print(f"backup  : {bak.name}")

pipe = Pipeline(str(DB))
for f in files:
    counts = pipe.load_receipts(read_export(f, "inventory_receipt"))
    print(f"loaded {f.name}: {counts}")
pipe.close()

c = duckdb.connect(str(DB), read_only=True)
print("rows :", c.execute("SELECT count(*) FROM fact_receipt").fetchone()[0])
print("range:", c.execute("SELECT min(receive_date), max(receive_date) FROM fact_receipt").fetchone())
print("gwp  :", c.execute("SELECT count(*) FROM fact_receipt WHERE is_gwp").fetchone()[0])
print("ruby :", c.execute(
    "SELECT product, SUM(quantity) FROM fact_receipt "
    "WHERE is_gwp AND lower(brand) LIKE '%ruby%' GROUP BY 1 ORDER BY 2 DESC").fetchall())
c.close()
