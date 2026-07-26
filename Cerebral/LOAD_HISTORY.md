# Loading sales history

One year first: **July 2025 – June 2026**, all four stores. That window sits
entirely after Alpine IQ went live (2024-08-10) and after the Non-Stop
register appeared (2025-10-01), so the data is complete throughout.

Roughly 3 million lines. A few minutes of processing per month.

---

## What to export, per month

**Three per store** (DTBK, 5th Ave, Soho, USQ):
- Daily Dispensations
- POS Transactions by Register

**Two chain-wide** (one file covers all four stores):
- Detailed Sales Breakdown by Product
- Alpine IQ Redemption Report

So 10 files per month: 8 store-specific + 2 chain-wide.

Set the date range to the full calendar month. Export **closed days only** --
never a partial current day.

---

## Folder layout

Make one folder per month inside the project:

```
history\2025-07\
history\2025-08\
history\2025-09\
   ...
history\2026-06\
```

Drop that month's 10 files into its folder. Filenames do not matter -- files
are identified by their contents.

---

## Running it

One command per month:

```powershell
python tta_etl.py --inbox .\history\2025-07 --db .\tta.duckdb --period 2025-07
```

Then repeat with the next month. Order does not matter, but chronological is
easier to track.

Or all at once:

```powershell
Get-ChildItem .\history -Directory | ForEach-Object {
    Write-Host "`n=== $($_.Name) ===" -ForegroundColor Cyan
    python tta_etl.py --inbox $_.FullName --db .\tta.duckdb --period $_.Name
}
```

---

## What to watch for

Each store prints a validation block. Expect:

- `ALL CHECKS PASSED` -- loaded
- `LOADED with N advisory warning(s)` -- loaded, small drift noted
- `*** LOAD FAILED -- NOT WRITTEN ***` -- refused, nothing written

A failed month is safe to fix and re-run. Re-running a month replaces it
rather than duplicating.

Send me any FAIL output. The likely cause is a new register name or a new
product category appearing at some point in the year -- both are one-line
config fixes.

---

## After loading

```powershell
python -c "import duckdb; c=duckdb.connect('tta.duckdb'); print(c.execute('SELECT store_key, period, lines, baskets, passed, warnings FROM load_log ORDER BY period, store_key').df().to_string())"
```

That lists every month loaded, per store, with row counts.

---

## Then upload the database

```powershell
python -c "from tta_drive import DriveClient; from pathlib import Path; import os; from tta_env import bootstrap; bootstrap(); DriveClient().upload(Path('tta.duckdb'), os.environ['TTA_DRIVE_STATE'])"
```

Scheduled runs pick up from there and only process new files.
