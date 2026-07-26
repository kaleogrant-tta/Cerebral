"""
Drive <-> local sync for the TTA ETL.

Drive is a landing zone and a state store, never a processing layer.

  inbox/    Dutchie scheduled exports land here
  archive/  processed files are moved here (never deleted)
  state/    tta.duckdb + a lock file

Auth: a service account JSON supplied via the GOOGLE_SERVICE_ACCOUNT_JSON
environment variable (a GitHub Actions secret). The three Drive folders and
the target Sheet must be shared with the service account's email address.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

LOCK_MAX_AGE_SECONDS = 3600   # a lock older than this is treated as stale


def credentials() -> Credentials:
    from tta_env import bootstrap
    bootstrap()
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set. In GitHub Actions this "
            "comes from a repository secret; locally, export it or point it at "
            "a file's contents."
        )
    if raw.strip().startswith("{"):
        info = json.loads(raw)
    else:                                   # allow a path for local use
        info = json.loads(Path(raw).read_text())
    return Credentials.from_service_account_info(info, scopes=SCOPES)


class DriveClient:
    def __init__(self, creds: Credentials | None = None):
        self.svc = build("drive", "v3", credentials=creds or credentials(),
                         cache_discovery=False)

    # -- listing ----------------------------------------------------------

    def list_files(self, folder_id: str, name_contains: str | None = None) -> list[dict]:
        q = [f"'{folder_id}' in parents", "trashed = false"]
        if name_contains:
            q.append(f"name contains '{name_contains}'")
        out, token = [], None
        while True:
            resp = self.svc.files().list(
                q=" and ".join(q),
                fields="nextPageToken, files(id, name, size, modifiedTime)",
                pageSize=1000, pageToken=token,
            ).execute()
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def find(self, folder_id: str, name: str) -> dict | None:
        for f in self.list_files(folder_id):
            if f["name"] == name:
                return f
        return None

    # -- transfer ---------------------------------------------------------

    def download(self, file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = self.svc.files().get_media(fileId=file_id)
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        return dest

    def upload(self, src: Path, folder_id: str, name: str | None = None,
               replace: bool = True) -> str:
        name = name or src.name
        media = MediaFileUpload(str(src), resumable=True,
                                chunksize=8 * 1024 * 1024)
        existing = self.find(folder_id, name) if replace else None
        if existing:
            # Update in place. This works for a service account; create does
            # not, because service accounts have no storage quota of their own.
            f = self.svc.files().update(fileId=existing["id"],
                                        media_body=media).execute()
            return f["id"]
        try:
            f = self.svc.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media, fields="id").execute()
            return f["id"]
        except HttpError as e:
            if "storageQuotaExceeded" in str(e):
                raise RuntimeError(
                    f"Cannot create '{name}' in Drive: service accounts have "
                    f"no storage quota. Upload the file once from your own "
                    f"Google account into that folder; every run after that "
                    f"updates it in place."
                ) from e
            raise

    def move(self, file_id: str, to_folder: str) -> None:
        meta = self.svc.files().get(fileId=file_id, fields="parents").execute()
        self.svc.files().update(
            fileId=file_id,
            addParents=to_folder,
            removeParents=",".join(meta.get("parents", [])),
            fields="id, parents",
        ).execute()

    def delete(self, file_id: str) -> None:
        self.svc.files().delete(fileId=file_id).execute()


class DriveLock:
    """Advisory lock so two runs can never write the database at once.

    Not bulletproof -- Drive has no atomic create-if-absent -- but the only
    writer is a single scheduled job, so this exists to catch a manual run
    overlapping the scheduled one, which is the realistic collision.
    """

    def __init__(self, drive: DriveClient, folder_id: str, name: str = "_etl.lock"):
        self.drive, self.folder_id, self.name = drive, folder_id, name
        self.file_id: str | None = None

    def __enter__(self):
        existing = self.drive.find(self.folder_id, self.name)
        if existing:
            age = time.time() - _epoch(existing["modifiedTime"])
            if age < LOCK_MAX_AGE_SECONDS:
                raise RuntimeError(
                    f"ETL lock held (age {age/60:.1f} min). Another run is in "
                    f"progress. Delete {self.name} in the state folder to override."
                )
            print(f"  ! stale lock ({age/60:.0f} min old) — overriding")
            self.drive.delete(existing["id"])

        tmp = Path("/tmp") / self.name
        tmp.write_text(json.dumps({
            "acquired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runner": os.environ.get("GITHUB_RUN_ID", "local"),
        }))
        self.file_id = self.drive.upload(tmp, self.folder_id, self.name)
        return self

    def __exit__(self, *exc):
        if self.file_id:
            try:
                self.drive.delete(self.file_id)
            except Exception as e:                      # never mask the real error
                print(f"  ! could not release lock: {e}")
        return False


def _epoch(rfc3339: str) -> float:
    return time.mktime(time.strptime(rfc3339[:19], "%Y-%m-%dT%H:%M:%S"))


def pull_inbox(drive: DriveClient, folder_id: str, dest: Path) -> list[tuple[Path, str]]:
    """Download every spreadsheet in the Drive inbox. Returns (path, file_id)."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for f in drive.list_files(folder_id):
        if not f["name"].lower().endswith((".xlsx", ".xls", ".csv")):
            continue
        p = drive.download(f["id"], dest / f["name"])
        print(f"    pulled {f['name']} ({int(f.get('size', 0))/1e6:.1f} MB)")
        out.append((p, f["id"]))
    return out
