# GitHub secrets — values to paste

Repo -> Settings -> Secrets and variables -> Actions -> New repository secret

| Secret name | Value |
|---|---|
| `TTA_DRIVE_STATE`   | `150fo2U9wWQfncc-yB_p7hYKjl1I9ah3q` |
| `TTA_DRIVE_ARCHIVE` | `13zOVT87rC8760St4IJITvPfw4b-CNs1p` |
| `TTA_DRIVE_INBOX`   | `17GL1j3sAO1fexQb4RG5LTANGPO_TBbnj` |
| `TTA_SHEET_ID`      | `1lX_Ri1G3fh5wMCV-PHZ_Uu4apzQimzydK15ij7AYkKE` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the downloaded key file |

Service account: `cerebral@customer-origin.iam.gserviceaccount.com`

## Folder order — CONFIRMED

Order supplied was state, archive, inbox. The table above reflects that.
(An earlier draft had inbox and state reversed.)

`tta_preflight.py` re-checks this once files exist: a `.duckdb` outside the
state folder, or exports sitting in archive but not inbox, both trip the
`folder_roles` check. It cannot detect a swap while all three are empty, so
the first run with real files is the real test.

## Local preflight — Windows

Do NOT use `export`. That is bash syntax and will not work in PowerShell,
cmd, or the Python interpreter. Use a `.env` file instead.

1. Copy `.env.example` to `.env`
2. Set `GOOGLE_SERVICE_ACCOUNT_JSON` to the FULL PATH of your key file, e.g.
   `C:\Users\Kaleo\Downloads\customer-origin-abc123.json`
3. From **PowerShell** (not the Python interpreter — no `>>>` prompt):

```powershell
cd path\to\repo
python tta_preflight.py
```

`.env` is gitignored. GitHub Actions ignores it entirely and uses repository
secrets, so the same code runs both places with no changes.

## Reusing the customer-origin project

Drive API is likely already enabled there. **Sheets API almost certainly is
not** — Customer Origins never needed it. Enable both:

console.cloud.google.com -> APIs & Services -> Library -> Google Sheets API

## Sharing

Each of the three folders AND the Sheet must be shared with
`cerebral@customer-origin.iam.gserviceaccount.com` as **Editor**.

A service account is a separate identity. It cannot see anything in your
Drive that has not been explicitly shared, even inside its own project.
