# Upload cadence

Two rhythms, because the data has two rhythms.

---

## Daily — inventory only

**One file.** Current Inventory, chain-wide, into `TTA/inbox`.

This is the only export that cannot be backfilled. A transaction from last
Tuesday can be pulled any time; on-hand position last Tuesday is gone. Every
day without a snapshot is a permanent hole in days-of-supply, SSI and
stockout analysis.

Takes about thirty seconds. Do it whenever, before 11am.

---

## Weekly — transactions

**Ten files**, Monday morning:

| Export | Count |
|---|---|
| Daily Dispensations | 4 (one per store) |
| POS Transactions by Register | 4 (one per store) |
| Detailed Sales Breakdown | 1 (chain-wide) |
| Alpine IQ Redemption | 1 (chain-wide) |

**Set the date range to the trailing TWO weeks, not one.**

Returns and late-posting delivery orders land in earlier periods. A two-week
window that overlaps the previous upload catches them. Re-loading a period
replaces it rather than duplicating, so the overlap is free.

Always export closed days only — never a partial current day.

---

## The run

Daily at 11:00, whether or not you uploaded anything.

- inventory only → loads the snapshot, done
- inventory + transactions → loads both, rebuilds aggregates, publishes
- nothing → exits cleanly
- a partial transaction set (say 3 of 4 stores) → refuses and leaves the
  files in the inbox, because a partial load silently skews every rate

Trigger it manually any time from the Actions tab.

---

## Why not monthly

The scorecard's control limits and run rules work on weekly data. A run of
seven weeks below baseline is the signal that catches slow erosion — the
failure mode that never trips a threshold but costs a quarter's margin.
Monthly uploads mean finding out up to four weeks late.

Ten files a week is roughly ten minutes.

## Why not daily transactions

Nothing in the framework operates below weekly resolution, and it would be
forty files a week for no analytical gain.

---

## What happens after a run

`agg_category_week` and `load_log` land in the Cerebral sheet. The database
goes back to `TTA/state`. Processed files move to `TTA/archive` — never
deleted, so a bad load can always be re-run from the originals.

## Building the dashboard file by hand

The workflow (`.github/workflows/refresh.yml`) runs these in order after the
load; the same recipe works locally from the `Cerebral/` folder:

    python publish.py --db ..\tta.duckdb                         # rebuilds cerebral_dash.duckdb
    python publish_vm.py --src ..\tta.duckdb                     # adds the Visual Merch tables
    python publish_promo.py --src ..\tta.duckdb --dash cerebral_dash.duckdb   # adds the Promo Lab tables
    python publish_model.py --dash cerebral_dash.duckdb                       # adds the Model Lab tables + TL;DR
    python checks.py --dash cerebral_dash.duckdb --db ..\tta.duckdb           # validation gate (FAIL blocks upload)
    python publish_vm.py --src ..\tta.duckdb --upload            # pushes the finished file to Drive

Do not use `publish.py --upload`: it rebuilds first and drops the VM, promo and
model tables. The promo calendar itself lives in `promos.py`.

`publish_model.py` reads nothing but the dash file, so it also runs on a copy
pulled from Drive. `python publish_model.py --explain Flower 4` prints one fit
as a Stata-style table (store keys: 1 DTBK, 2 5th Ave, 3 Soho, 4 USQ, 0 chain).
