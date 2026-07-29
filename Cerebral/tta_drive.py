"""
Drive <-> local sync for the TTA ETL.

Drive is a landing zone and a state store, never a processing layer.

  inbox/    Dutchie scheduled exports land here
  archive/  processed files are moved here (never deleted)
  state/    tta.duckdb + a lock file

Auth: a service account JSON supplied via the GOOGLE_SERVICE_ACCOUNT_JSON
environment variable (a GitHub Actions secret). The three Drive folders and
the target Sheet must be shared with the service account's email address.

Two hard-won lessons baked into this file:

1. Every API call passes supportsAllDrives=True. Without it, calls against
   folders in a shared drive silently misbehave instead of raising errors.
2. Service accounts have NO storage quota in My Drive, so files().create
   fails there with a 403. Updates to existing files are fine, so the lock
   is a persistent file whose CONTENT holds the lock state, and uploads
   replace existing files whenever possible. Creating new files only works
   if the folders live in a shared drive.
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
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

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

    # -- diagnostics --------------------------------------------------------

    def folder_label(self, folder_id: str) -> str:
        """Human-readable label for logs: proves the secrets point at the
        folders you think they do."""
        try:
            meta = self.svc.files().get(
                fileId=folder_id, fields="name,driveId",
                supportsAllDrives=True,
            ).execute()
            where = "shared drive" if meta.get("driveId") else "My Drive"
            return f'"{meta["name"]}" ({where})'
        except Exception as e:
            return f"<UNREADABLE folder {folder_id}: {e}>"

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
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
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
        req = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        return dest

    def read_text(self, file_id: str) -> str:
        req = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue().decode("utf-8", "replace")

    def update_text(self, file_id: str, text: str) -> None:
        """Rewrite a small file's contents in place. Works for service
        accounts in My Drive because no new file is created."""
        media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")),
                                  mimetype="text/plain", resumable=False)
        self.svc.files().update(
            fileId=file_id, media_body=media, supportsAllDrives=True,
        ).execute()

    def upload(self, src: Path, folder_id: str, name: str | None = None,
               replace: bool = True) -> str:
        name = name or src.name
        media = MediaFileUpload(str(src), resumable=True,
                                chunksize=8 * 1024 * 1024)
        existing = self.find(folder_id, name) if replace else None
        if existing:
            f = self.svc.files().update(
                fileId=existing["id"], media_body=media,
                supportsAllDrives=True,
            ).execute()
        else:
            try:
                f = self.svc.files().create(
                    body={"name": name, "parents": [folder_id]},
                    media_body=media, fields="id",
                    supportsAllDrives=True,
                ).execute()
            except HttpError as e:
                if e.resp.status == 403:
                    raise RuntimeError(
                        f'Cannot create "{name}" in the target folder: '
                        f"service accounts have no storage quota in My Drive. "
                        f"Either upload {name} to that folder once by hand in "
                        f"Google Drive, or move the TTA folders into a "
                        f"shared drive."
                    ) from e
                raise
        return f["id"]

    def move(self, file_id: str, to_folder: str,
             from_folder: str | None = None) -> None:
        """Move a file between folders and VERIFY it happened.

        The Drive API returns success even when the parents didn't change
        the way you expected, so we re-read the file afterwards and raise
        if it isn't exactly where it should be. A failed archive now fails
        the whole run (red X) instead of passing silently.
        """
        # Drive often withholds the `parents` field from service accounts on
        # files they don't own, so never trust it for the remove side: we
        # KNOW the file came from from_folder (that's where it was listed).
        # Only fall back to reading parents when no source folder was given.
        if from_folder:
            remove = from_folder
            name = file_id
        else:
            meta = self.svc.files().get(
                fileId=file_id, fields="name,parents",
                supportsAllDrives=True,
            ).execute()
            name = meta.get("name", file_id)
            parents = meta.get("parents") or []
            if not parents:
                raise RuntimeError(
                    f"cannot move {name}: Drive won't reveal its current "
                    f"folder to the service account and no source folder "
                    f"was given."
                )
            remove = ",".join(parents)

        self.svc.files().update(
            fileId=file_id,
            addParents=to_folder,
            removeParents=remove,
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()

        # Best-effort verification. If Drive hides parents from us, skip
        # this — tta_refresh re-lists the inbox afterwards as the real check.
        after = self.svc.files().get(
            fileId=file_id, fields="parents",
            supportsAllDrives=True,
        ).execute()
        after_parents = after.get("parents")
        if after_parents is not None and to_folder not in after_parents:
            raise RuntimeError(
                f"archive move failed for {name}: not in archive folder "
                f"(parents now {after_parents}). Check the TTA_DRIVE_ARCHIVE "
                f"secret and the service account's access to that folder."
            )

    def delete(self, file_id: str) -> None:
        self.svc.files().delete(fileId=file_id, supportsAllDrives=True).execute()


class DriveLock:
    """Advisory lock so two runs can never write the database at once.

    The lock is a PERSISTENT _etl.lock file whose contents hold the state
    ({"status": "held"/"released", ...}). Service accounts cannot create
    files in My Drive (no storage quota), so we never create or delete the
    lock during normal operation -- we only rewrite its contents. If the
    file doesn't exist yet we try to create it, which succeeds on shared
    drives; on My Drive, upload() raises a clear error telling you to
    upload an empty _etl.lock by hand, once.
    """

    def __init__(self, drive: DriveClient, folder_id: str, name: str = "_etl.lock"):
        self.drive, self.folder_id, self.name = drive, folder_id, name
        self.file_id: str | None = None

    def __enter__(self):
        existing = self.drive.find(self.folder_id, self.name)
        if existing is None:
            tmp = Path("/tmp") / self.name
            tmp.write_text("{}")
            self.file_id = self.drive.upload(tmp, self.folder_id, self.name)
        else:
            self.file_id = existing["id"]
            try:
                state = json.loads(self.drive.read_text(self.file_id) or "{}")
            except json.JSONDecodeError:
                state = {}
            if state.get("status") == "held":
                age = time.time() - _epoch(
                    state.get("acquired", "1970-01-01T00:00:00"))
                if age < LOCK_MAX_AGE_SECONDS:
                    raise RuntimeError(
                        f"ETL lock held (age {age/60:.1f} min). Another run "
                        f"is in progress. To override, open {self.name} in "
                        f"the state folder in Google Drive and clear its "
                        f"contents."
                    )
                print(f"  ! stale lock ({age/60:.0f} min old) — overriding")

        self.drive.update_text(self.file_id, json.dumps({
            "status": "held",
            "acquired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runner": os.environ.get("GITHUB_RUN_ID", "local"),
        }))
        return self

    def __exit__(self, *exc):
        if self.file_id:
            try:
                self.drive.update_text(self.file_id, json.dumps({
                    "status": "released",
                    "released": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
                }))
            except Exception as e:                  # never mask the real error
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
