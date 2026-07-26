"""
Setup — run this once. It asks a few questions and writes your .env file.

    python setup.py

You do not need to create or edit any files by hand.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
ENV = HERE / ".env"

# Already known from the Cerebral project — offered as defaults.
DEFAULTS = {
    "TTA_DRIVE_STATE":   "150fo2U9wWQfncc-yB_p7hYKjl1I9ah3q",
    "TTA_DRIVE_ARCHIVE": "13zOVT87rC8760St4IJITvPfw4b-CNs1p",
    "TTA_DRIVE_INBOX":   "17GL1j3sAO1fexQb4RG5LTANGPO_TBbnj",
    "TTA_SHEET_ID":      "1lX_Ri1G3fh5wMCV-PHZ_Uu4apzQimzydK15ij7AYkKE",
}

LABELS = {
    "TTA_DRIVE_STATE":   "Drive folder: state",
    "TTA_DRIVE_ARCHIVE": "Drive folder: archive",
    "TTA_DRIVE_INBOX":   "Drive folder: inbox",
    "TTA_SHEET_ID":      "Google Sheet: Cerebral",
}


def hr(char: str = "-") -> None:
    print(char * 68)


def find_key_files() -> list[Path]:
    """Look in the usual places for a downloaded service account key.

    Must survive whatever else is sitting in Downloads: JSON arrays, huge
    files, unreadable files, wrong encodings. Anything unexpected is skipped
    silently rather than crashing the setup.
    """
    candidates: list[Path] = []
    for folder in [Path.home() / "Downloads", Path.home() / "Desktop",
                   Path.home() / "Documents", HERE]:
        try:
            if not folder.exists():
                continue
            files = list(folder.glob("*.json"))
        except (OSError, PermissionError):
            continue

        for p in files:
            try:
                if p.stat().st_size > 64_000:      # keys are ~2 KB
                    continue
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                continue                            # not JSON, unreadable, wrong encoding
            if isinstance(data, dict) and data.get("type") == "service_account":
                candidates.append(p)
    return candidates


def describe(p: Path) -> str:
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return f"{p.name}  ({data.get('client_email', 'unknown account')})"
    except Exception:
        pass
    return p.name


def ask_key_path() -> str | None:
    print("\nSTEP 1 of 2 — your Google key file")
    hr()
    print("This is the .json file Google gave you when you created the")
    print("service account. It is usually in your Downloads folder and its")
    print("name starts with 'customer-origin'.\n")

    found = find_key_files()

    if found:
        print("I found these key file(s):\n")
        for i, p in enumerate(found, 1):
            print(f"   {i}. {describe(p)}")
        print(f"   {len(found) + 1}. None of these — I'll type the location myself")
        print(f"   {len(found) + 2}. I haven't downloaded it yet\n")

        choice = input("Type a number and press Enter: ").strip()
        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(found):
                return str(found[n - 1])
            if n == len(found) + 2:
                return None
    else:
        print("I couldn't find a key file in Downloads, Desktop or Documents.\n")
        print("   1. I have it somewhere else — I'll type the location")
        print("   2. I haven't downloaded it yet\n")
        if input("Type a number and press Enter: ").strip() == "2":
            return None

    print("\nTo get the file's location in Windows:")
    print("  - find the .json file in File Explorer")
    print("  - hold SHIFT, right-click it, choose 'Copy as path'")
    print("  - right-click here and paste, then press Enter\n")

    while True:
        raw = input("Paste the location: ").strip().strip('"').strip("'")
        if not raw:
            return None
        p = Path(raw).expanduser()
        if not p.exists():
            print(f"  I can't find a file at: {p}")
            print("  Try 'Copy as path' again, or press Enter to skip.\n")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            print("  That file isn't readable as JSON. Wrong file?\n")
            continue
        if not isinstance(data, dict) or data.get("type") != "service_account":
            print("  That's a JSON file, but not a service account key.\n")
            continue
        print(f"  Looks right — account: {data.get('client_email')}\n")
        return str(p)


def key_instructions() -> None:
    print("\n" + "=" * 68)
    print("You need to download the key file first. Here's how:")
    hr("=")
    print("""
 1. Go to  console.cloud.google.com
 2. Make sure the project selector at the top says 'customer-origin'
 3. Left menu: APIs & Services  ->  Credentials
 4. Under 'Service Accounts', click
       cerebral@customer-origin.iam.gserviceaccount.com
 5. Click the 'KEYS' tab
 6. ADD KEY  ->  Create new key  ->  choose JSON  ->  CREATE
 7. A .json file downloads. Leave it in Downloads.

While you're in the Cloud Console, also do this:

 8. Left menu: APIs & Services  ->  Library
 9. Search 'Google Sheets API'  ->  click it  ->  ENABLE
10. Search 'Google Drive API'   ->  click it  ->  ENABLE
    (if it says 'Manage' instead of 'Enable', it's already on)

Then run  python setup.py  again.
""")
    hr("=")


def confirm_ids() -> dict[str, str]:
    print("\nSTEP 2 of 2 — your Drive folders and Sheet")
    hr()
    print("These are already filled in from what you sent me.")
    print("Press Enter to accept each one, or paste a different ID.\n")

    values: dict[str, str] = {}
    for key, default in DEFAULTS.items():
        entered = input(f"  {LABELS[key]:<28} [{default[:18]}...]: ").strip()
        values[key] = entered or default
    return values


def main() -> int:
    print("\n" + "=" * 68)
    print("  Cerebral — one-time setup")
    hr("=")
    print("This writes a small settings file so the tools can reach your")
    print("Google Drive and Sheet. It takes about a minute.")

    if ENV.exists():
        print(f"\nA settings file already exists at:\n  {ENV}")
        if input("\nReplace it? (y/n): ").strip().lower() != "y":
            print("\nLeft alone. Nothing changed.\n")
            return 0

    key_path = ask_key_path()
    if key_path is None:
        key_instructions()
        return 1

    values = confirm_ids()
    values["GOOGLE_SERVICE_ACCOUNT_JSON"] = key_path

    lines = [
        "# Created by setup.py. Safe to edit by hand.",
        "# This file is never uploaded to GitHub.",
        "",
        f"GOOGLE_SERVICE_ACCOUNT_JSON={key_path}",
        "",
    ] + [f"{k}={values[k]}" for k in DEFAULTS]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 68)
    print(f"  Saved: {ENV}")
    hr("=")
    print("""
Next step — check that everything connects:

    python tta_preflight.py

It will print a list of checks. If anything says FAIL, it also says
exactly what to fix. Nothing is changed or uploaded by that command.
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nStopped. Nothing was saved.\n")
        sys.exit(1)
