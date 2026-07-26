# Cloud setup — one-time

The engine is the same code whether it runs on your laptop or in Actions.
Drive is a landing zone and state store; Sheets is a serving layer.

## 1. Google Cloud service account

1. console.cloud.google.com -> new project (e.g. `tta-analytics`)
2. APIs & Services -> Library -> enable **Google Drive API** and **Google Sheets API**
3. Credentials -> Create credentials -> **Service account** -> name it, no roles needed
4. Open the account -> Keys -> Add key -> **JSON** -> download
5. Copy the `client_email` from that JSON — looks like
   `tta-etl@tta-analytics.iam.gserviceaccount.com`

No billing account required. Drive and Sheets APIs are free at this volume.

## 2. Drive folders

Create three folders and **share each with the service account email as Editor**:

| Folder | Purpose |
|---|---|
| `TTA/inbox`   | Dutchie scheduled exports land here |
| `TTA/archive` | processed files moved here (never deleted) |
| `TTA/state`   | holds `tta.duckdb` and the lock file |

Folder ID is the last path segment of the URL:
`drive.google.com/drive/folders/`**`1a2B3c...`**

## 3. Sheet

Create one Google Sheet, share it with the service account as **Editor**.
Tabs are created automatically. ID is in its URL.

## 4. GitHub secrets

Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the downloaded JSON |
| `TTA_DRIVE_INBOX`   | inbox folder ID |
| `TTA_DRIVE_ARCHIVE` | archive folder ID |
| `TTA_DRIVE_STATE`   | state folder ID |
| `TTA_SHEET_ID`      | sheet ID |

## 5. Dutchie scheduled exports

Point them at `TTA/inbox`. Per period you need:

- Daily Dispensations — one per store
- POS Transactions by Register — one per store
- Detailed Sales Breakdown — **one, chain-wide**
- Alpine IQ Redemption — **one, chain-wide**
- Current Inventory — daily, chain-wide

Always export **closed days only**. Same-day exports pulled minutes apart
do not reconcile.

## 6. First run

Actions tab -> "TTA scheduled refresh" -> **Run workflow**.

Watch for: lock acquired, files pulled, validation table, rows published.
A failure leaves Drive untouched — fix and re-run.

## Bulk history (local, one time)

```
python tta_etl.py --inbox ./history --db ./tta.duckdb --period 2025-07
```

Load month by month, then upload the finished `tta.duckdb` to `TTA/state`.
Scheduled runs pick up from there.

## When a rule changes

Edit `tta_config.py`, bump `CONFIG_VERSION`, commit. Every row in `load_log`
records the version it was built under, so you can always tell which periods
need reprocessing.
