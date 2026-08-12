"""
build_event_audience_map.py -- regenerate event_audience_map.csv from the
mapping workbook, so both event analyses read one source of truth.

Why this exists
---------------
Two files described the same audience-to-event relationship and disagreed.
`event_audience_map.csv` was built by fuzzy name/date matching against
dim_event; `config/audience_event_mapping.xlsx` was built from loyalty
enrolment clustering and purchase-spike detection. Where they conflicted the
workbook was usually right, because enrolment dates come from the attendees
themselves rather than from string similarity.

So the workbook is now authoritative and this script derives the CSV from it.
Correct a row in the workbook, re-run this, re-run publish.

What it does
------------
Reads the workbook's Mapping sheet, joins each mapped audience to dim_event on
event_date, and picks the dim_event row whose name best matches. Multi-store
events (Ghostface Killah ran at all four stores, the 420 food drops at all
four) produce several dim_event rows for one date; where the workbook says
"Multi" the chain-level row is preferred, and where it names a store that
store's row is used.

Rows the workbook marks EXCLUDE (membership defined by purchase) or leaves
UNMAPPED are written with a blank event_id, which publish_event_return.py
skips. That keeps the exclusions visible rather than silently dropping them.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

# dim_event.store_key, decoded from the event names it carries
STORE_KEY = {"DTBK": 1, "FIFTH": 2, "SOHO": 3, "USQ": 4}
CHAIN_KEY = 0


def _norm(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2}


def _score(want_name: str, want_store: str, row) -> tuple[int, int]:
    """(name overlap, store match) — higher is better."""
    overlap = len(_norm(want_name) & _norm(row.event_name))
    if want_store == "Multi":
        store = 2 if row.store_key == CHAIN_KEY else 0
    elif want_store in STORE_KEY:
        store = 2 if row.store_key == STORE_KEY[want_store] else 0
    elif want_store == "Not In Store":
        store = 2 if bool(row.is_offsite) else 0
    else:
        store = 0
    return overlap, store


def build(workbook: Path, dim_event: Path, out: Path) -> pd.DataFrame:
    wb = pd.read_excel(workbook, sheet_name="Mapping", dtype={"audience_id": str})
    de = pd.read_csv(dim_event)
    de["event_date"] = pd.to_datetime(de["event_date"]).dt.strftime("%Y-%m-%d")

    rows = []
    for r in wb.itertuples():
        aid = str(r.audience_id).strip()
        if not aid or aid == "nan":
            continue
        excluded = str(getattr(r, "purchase_defined", "")).strip().upper() == "YES"
        if pd.isna(r.event_date) or excluded:
            rows.append({
                "audience_id": aid,
                "audience_name": r.audience_name,
                "event_id": "",
                "event_date": "" if pd.isna(r.event_date) else
                              pd.to_datetime(r.event_date).strftime("%Y-%m-%d"),
                "resolved_name": "",
                "source": "excluded: purchase-defined" if excluded else "unmapped",
                "note": str(getattr(r, "notes", "") or "")[:200],
            })
            continue

        want_date = pd.to_datetime(r.event_date).strftime("%Y-%m-%d")
        want_name = str(getattr(r, "event_name", "") or "")
        want_store = str(getattr(r, "store", "") or "").strip()
        cands = de[de["event_date"] == want_date]

        if cands.empty:
            rows.append({
                "audience_id": aid, "audience_name": r.audience_name,
                "event_id": "", "event_date": want_date, "resolved_name": "",
                "source": "no dim_event row on this date",
                "note": want_name[:200],
            })
            continue

        scored = sorted(cands.itertuples(),
                        key=lambda c: _score(want_name, want_store, c),
                        reverse=True)
        best = scored[0]
        overlap, store = _score(want_name, want_store, best)
        source = ("exact" if overlap and store else
                  "name match" if overlap else
                  "store match" if store else
                  "date only — VERIFY")
        rows.append({
            "audience_id": aid, "audience_name": r.audience_name,
            "event_id": best.event_id, "event_date": want_date,
            "resolved_name": best.event_name,
            "source": source,
            "note": f"{len(cands)} dim_event row(s) on this date",
        })

    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", type=Path,
                    default=Path("config/audience_event_mapping.xlsx"))
    ap.add_argument("--dim", type=Path, default=Path("dim_event.csv"))
    ap.add_argument("--out", type=Path, default=Path("event_audience_map.csv"))
    a = ap.parse_args()

    df = build(a.workbook, a.dim, a.out)
    mapped = (df["event_id"] != "").sum()
    print(f"\nwrote {a.out}: {mapped} mapped, {len(df) - mapped} blank\n")
    with pd.option_context("display.width", 200, "display.max_colwidth", 40):
        print(df[["audience_id", "audience_name", "event_id",
                  "resolved_name", "source"]].to_string(index=False))
    bad = df[df["source"].str.contains("VERIFY|no dim_event")]
    if len(bad):
        print(f"\n{len(bad)} row(s) need a human check:")
        for b in bad.itertuples():
            print(f"  {b.audience_id} {b.audience_name} ({b.event_date}) -> {b.source}")
