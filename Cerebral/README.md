# TTA Category Analytics — ETL

## Setup
```
pip install duckdb pandas openpyxl
```

## Run
```
python tta_etl.py --inbox ./inbox --db ./tta.duckdb --period 2026-06
python tta_etl.py --inbox ./inbox --db ./tta.duckdb --validate-only   # dry run
```

Drop raw exports into `inbox/`. Files are identified by header signature, not
filename, so Dutchie's colliding names and browser `(1)` `(2)` suffixes are fine.

## Outputs
`fact_line`, `fact_basket`, `fact_inventory`, `agg_category_week`, `load_log`.

## Rules encoded (all in tta_config.py)
- Breakdown and Alpine are CHAIN-scoped -> filtered by Location before joining.
  Without this the product join fans out 3.72x.
- Breakdown subtotal rows (`Product`/`Category`/`Location` == 'Total') stripped.
- Header row position detected per file (varies between row 3 and row 4).
- Channel from `Register`: Non-Stop / Doobie=Delivery / Sample=excluded /
  `register \d+`=In-Store. Case-insensitive; handles USQ's "835 " prefix.
- `AvgPricePerUnit` is NET of discount. Line revenue = Qty x AvgPricePerUnit,
  exact in aggregate (0.03%), an average allocation per line.
- `PreTaxLoyalty` ignored (always zero). Alpine IQ is the loyalty source.
- Sample Register excluded unconditionally.
- Inventory has no date column -> `snapshot_date` supplied at ingest.
- Availability calendar: Non-Stop and Alpine yield NULL before launch, not 0.

## Before loading history
Set real launch dates in `AVAILABILITY` (tta_config.py). Placeholders now.

## Validation
Banded checks run on every load:
- PASS  - within tolerance
- WARN  - small expected drift; load proceeds, flagged
- FAIL  - load refused, nothing written

Dispensations and the Breakdown come from different Dutchie subsystems and
never agree exactly. The Breakdown runs slightly high. Bands are in
`THRESHOLDS` (tta_config.py).

## Store scope of each export
| Export | Scope |
|---|---|
| Daily Dispensations | one per store |
| POS Transactions by Register | one per store |
| Detailed Sales Breakdown | CHAIN-WIDE (all 4) |
| Alpine IQ Redemption | CHAIN-WIDE (all 4) |
| Current Inventory | either - split on `Location Name` |

Three exports per store + two chain-wide per period.

## Inventory
```python
from tta_etl import Pipeline, read_export
p = Pipeline('tta.duckdb')
p.load_inventory(read_export(Path('inbox/inventory.xlsx'), 'inventory'), '2026-07-26')
```
`snapshot_date` is supplied by the caller - the export has no date column.
