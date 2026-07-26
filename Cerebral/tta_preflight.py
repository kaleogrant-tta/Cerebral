"""
Preflight — verify cloud wiring before any data moves.

Checks, in order of how likely they are to be the problem:
  1. service account JSON parses, identity matches expectation
  2. Drive API enabled and reachable
  3. Sheets API enabled and reachable
  4. each folder exists, is a folder, and is WRITABLE (create + delete a probe)
  5. the Sheet exists and is writable
  6. folder roles look right (inbox/archive/state not obviously swapped)

Run locally before the first workflow run:

    export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
    export TTA_DRIVE_INBOX=...
    export TTA_DRIVE_ARCHIVE=...
    export TTA_DRIVE_STATE=...
    export TTA_SHEET_ID=...
    python tta_preflight.py

Nothing here writes real data. Probe files are deleted immediately.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

EXPECTED_SA = "cerebral@customer-origin.iam.gserviceaccount.com"

from tta_env import bootstrap

bootstrap()

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:<34} {detail}")
    return ok


def main() -> int:
    print("\nTTA preflight\n" + "-" * 78)

    # --- 1. credentials --------------------------------------------------
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        record("service_account_json", False, "GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return summary()
    try:
        info = json.loads(raw) if raw.strip().startswith("{") else json.loads(Path(raw).read_text())
    except Exception as e:
        record("service_account_json", False, f"could not parse: {e}")
        return summary()

    email = info.get("client_email", "")
    record("service_account_json", True, email)
    if email != EXPECTED_SA:
        record("service_account_identity", False,
               f"expected {EXPECTED_SA}, got {email} — wrong key file?")
    else:
        record("service_account_identity", True, "matches expected account")

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    try:
        creds = Credentials.from_service_account_info(info, scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ])
        record("credentials", True, "key accepted")
    except Exception as e:
        record("credentials", False,
               f"key file is malformed or truncated ({type(e).__name__}). "
               f"Re-download it from the Cloud Console.")
        return summary()

    # --- 2/3. APIs enabled ----------------------------------------------
    try:
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        drive.files().list(pageSize=1, fields="files(id)").execute()
        record("drive_api", True, "enabled and reachable")
    except HttpError as e:
        record("drive_api", False, _api_hint(e, "Google Drive API"))
        return summary()

    try:
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        record("sheets_api_client", True, "client built")
    except Exception as e:
        record("sheets_api_client", False, str(e))
        sheets = None

    # --- 4. folders ------------------------------------------------------
    folders = {
        "inbox":   os.environ.get("TTA_DRIVE_INBOX"),
        "archive": os.environ.get("TTA_DRIVE_ARCHIVE"),
        "state":   os.environ.get("TTA_DRIVE_STATE"),
    }
    contents: dict[str, list[str]] = {}

    for role, fid in folders.items():
        if not fid:
            record(f"folder_{role}", False, "env var not set")
            continue
        try:
            meta = drive.files().get(fileId=fid, fields="id,name,mimeType").execute()
        except HttpError as e:
            record(f"folder_{role}", False, _share_hint(e, email))
            continue
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            record(f"folder_{role}", False, f"not a folder ({meta.get('mimeType')})")
            continue
        record(f"folder_{role}", True, f"{meta['name']!r}")

        # writability probe
        try:
            probe = drive.files().create(
                body={"name": "_preflight_probe", "parents": [fid]},
                media_body=None, fields="id").execute()
            drive.files().delete(fileId=probe["id"]).execute()
            record(f"folder_{role}_writable", True, "create + delete OK")
        except HttpError as e:
            record(f"folder_{role}_writable", False,
                   "service account needs Editor, not Viewer" if e.resp.status == 403
                   else str(e))

        try:
            listing = drive.files().list(
                q=f"'{fid}' in parents and trashed = false",
                fields="files(name)", pageSize=50).execute().get("files", [])
            contents[role] = [f["name"] for f in listing]
        except HttpError:
            contents[role] = []

    # --- 5. sheet --------------------------------------------------------
    sheet_id = os.environ.get("TTA_SHEET_ID")
    if not sheet_id:
        record("sheet", False, "TTA_SHEET_ID not set")
    elif sheets:
        try:
            meta = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
            title = meta["properties"]["title"]
            tabs = [s["properties"]["title"] for s in meta["sheets"]]
            record("sheet", True, f"{title!r} — tabs: {tabs}")
            try:
                sheets.spreadsheets().values().update(
                    spreadsheetId=sheet_id, range=f"{tabs[0]}!ZZ1000",
                    valueInputOption="RAW", body={"values": [["preflight"]]}).execute()
                sheets.spreadsheets().values().clear(
                    spreadsheetId=sheet_id, range=f"{tabs[0]}!ZZ1000", body={}).execute()
                record("sheet_writable", True, "write + clear OK")
            except HttpError as e:
                record("sheet_writable", False,
                       "share the Sheet with the service account as Editor"
                       if e.resp.status == 403 else str(e))
        except HttpError as e:
            if "SERVICE_DISABLED" in str(e) or e.resp.status == 403:
                record("sheet", False, _api_hint(e, "Google Sheets API"))
            else:
                record("sheet", False, str(e))

    # --- 6. folder roles sanity -----------------------------------------
    if len(contents) == 3:
        db_in = [r for r, names in contents.items()
                 if any(n.endswith(".duckdb") for n in names)]
        exports_in = [r for r, names in contents.items()
                      if any(n.lower().endswith((".xlsx", ".xls", ".csv")) for n in names)]
        if db_in and db_in != ["state"]:
            record("folder_roles", False,
                   f"tta.duckdb found in {db_in[0]!r}, expected 'state' — IDs swapped?")
        elif exports_in and "archive" in exports_in and "inbox" not in exports_in:
            record("folder_roles", False,
                   "exports sitting in 'archive' but not 'inbox' — IDs may be swapped")
        else:
            record("folder_roles", True,
                   "; ".join(f"{r}={len(n)} file(s)" for r, n in contents.items()))

    return summary()


def _api_hint(e, api_name: str) -> str:
    if "SERVICE_DISABLED" in str(e) or "has not been used" in str(e):
        return (f"{api_name} not enabled — console.cloud.google.com -> "
                f"APIs & Services -> Library -> enable it")
    return f"HTTP {e.resp.status}: {str(e)[:90]}"


def _share_hint(e, email: str) -> str:
    if e.resp.status == 404:
        return f"not found — wrong ID, or not shared with {email}"
    if e.resp.status == 403:
        return f"forbidden — share with {email} as Editor"
    return f"HTTP {e.resp.status}"


def summary() -> int:
    failed = [n for n, ok, _ in results if not ok]
    print("-" * 78)
    if failed:
        print(f"  {len(failed)} check(s) failed: {', '.join(failed)}")
        print("  Fix these before running the workflow.\n")
        return 1
    print("  All checks passed — safe to run the workflow.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
