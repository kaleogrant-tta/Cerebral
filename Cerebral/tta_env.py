"""
Environment loading — works the same on Windows, macOS, Linux and Actions.

Precedence:
  1. real environment variables (GitHub Actions secrets) — always win
  2. a local .env file sitting next to this module

This exists so local setup never depends on shell syntax. `export FOO=bar`
is bash; PowerShell wants `$env:FOO = "bar"`; cmd wants `set FOO=bar`. A file
avoids all three.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"


def load_env(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Read .env into os.environ. Real env vars win unless override=True."""
    path = path or ENV_FILE
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # tolerate quotes and a stray `export ` prefix pasted from bash docs
        if key.startswith("export "):
            key = key[7:].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
        loaded[key] = val
    return loaded


def resolve_service_account() -> str | None:
    """Return the service-account JSON as a string.

    GOOGLE_SERVICE_ACCOUNT_JSON may hold either the JSON itself (Actions) or a
    path to the key file (local). Paths are expanded, and a `*` glob is
    resolved so `customer-origin-*.json` works on Windows too, where the shell
    does not expand globs.
    """
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not val:
        return None
    if val.startswith("{"):
        return val

    p = Path(val).expanduser()
    if "*" in p.name:
        matches = sorted(p.parent.expanduser().glob(p.name))
        if not matches:
            raise FileNotFoundError(
                f"No key file matches {p}. Put the full filename in .env, "
                f"not a wildcard."
            )
        if len(matches) > 1:
            raise ValueError(
                f"{len(matches)} key files match {p.name}: "
                f"{[m.name for m in matches]}. Name exactly one in .env."
            )
        p = matches[0]

    if not p.exists():
        raise FileNotFoundError(f"Service account key not found: {p}")
    text = p.read_text(encoding="utf-8-sig")
    json.loads(text)                      # fail early on a malformed file
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = text
    return text


def bootstrap() -> None:
    """Call at the top of any entry point."""
    load_env()
    resolve_service_account()
