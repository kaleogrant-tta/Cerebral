"""
publish_vm.py — build the Visual Merchandising dash tables in cerebral_dash.duckdb.

Run from the repo root, AFTER publish.py has rebuilt cerebral_dash.duckdb (it
needs dash_brand_week + dash_brand_alias from that file). publish.py --upload
REBUILDS before uploading, which would drop these tables, so upload from
here instead:

    python Cerebral\\publish.py            # build
    python Cerebral\\publish_vm.py --upload # add VM tables, push to Drive

--upload uses the same tta_env / tta_drive helpers and TTA_DRIVE_STATE folder
as publish.py.

Reads
    tta.duckdb          fact_vm_placement, dim_vm_week, dim_takeover (vm_ingest.py)
    cerebral_dash.duckdb dash_brand_week, dash_brand_alias
    cerebral_public.py  TAKEOVERS (the app's takeover calendar — single source)
    vm_brand_alias.csv  optional: vm_name,pos_brand for spellings the ladder misses
Writes (cerebral_dash.duckdb)
    dash_vm_placement_week   shelf rows × ISO week × brand sales
    dash_vm_brand_week       brand × store × ISO week with placement state
    dash_vm_coverage         positions with/without a brand recorded
    dash_vm_brand_resolve    how every VM brand name mapped to a POS brand (or didn't)
    dash_vm_takeover_week    takeover windows expanded to store-weeks
    dash_vm_takeover_xref    takeover × store × week × category: display vs sales
    dash_vm_ingest_log       pass-through of the ingest report

placement_state per brand-store-week: none | shelf.  A Takeover is NOT a
placement state — during a Takeover the brand gets kiosk featuring and maybe a
spotlight, not the store. It is carried as an annotation (takeover,
takeover_days) so weeks inside a window can be excluded from the generic
lift figures, and analysed on their own in dash_vm_takeover_xref: for each
Takeover x store x week (the window plus the 4 weeks before), what the brand
actually had on display, by category, next to its category sales.
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import os
import re
import sys
from pathlib import Path

import duckdb

# Paths follow publish.py's convention: tta.duckdb and cerebral_dash.duckdb are
# relative to the CURRENT DIRECTORY (run from the repo root,
# C:\Users\User\cerebral), while the app file and alias csv sit next to this
# script in Cerebral\.
HERE = Path(__file__).resolve().parent
SRC_DB = Path("tta.duckdb")
DASH_DB = Path("cerebral_dash.duckdb")
APP_FILE = HERE / "cerebral_public.py"
VM_ALIAS_CSV = HERE / "vm_brand_alias.csv"
MIN_FEATURED_DAYS = 4
ALL_STORE_KEYS = [1, 2, 3, 4]

# VM cells often carry a category suffix the POS brand doesn't ("Rythm Concentrate",
# "Find. 14g & 28g"). Stripped before prefix matching.
_SUFFIX = re.compile(r"\b(concentrate|tablets?|flower|vape|edibles?|pre.?rolls?|\d+g)\b.*$", re.I)


def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# --------------------------------------------------------------------------
# Takeover calendar: parsed out of cerebral_public.py without importing it
# (importing would start Streamlit). Same list the Takeovers tab uses.
# --------------------------------------------------------------------------
def load_takeovers(app_file: Path) -> list[dict]:
    tree = ast.parse(app_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TAKEOVERS" for t in node.targets):
            out = ast.literal_eval(node.value)
            for t in out:
                t["start"] = dt.date.fromisoformat(t["start"])
                t["end"] = dt.date.fromisoformat(t["end"])
            # Back-to-back windows sharing a boundary day: the later one owns it
            # (Woodstock ends May 14 / Timeless starts May 14 -> Timeless owns the 14th).
            out.sort(key=lambda t: t["start"])
            for a, b in zip(out, out[1:]):
                if a["end"] >= b["start"]:
                    a["end"] = b["start"] - dt.timedelta(days=1)
            return out
    raise SystemExit(f"TAKEOVERS not found in {app_file}")


# --------------------------------------------------------------------------
# Brand resolution ladder: VM name -> canonical POS brand
# --------------------------------------------------------------------------
def resolve_brands(vm_names: list[str], pos_brands: list[str],
                   dash_alias: dict[str, str], manual: dict[str, str]) -> list[dict]:
    pos_by_key = {key(b): b for b in pos_brands}
    rows = []
    for name in vm_names:
        low = name.strip().lower()
        k = key(name)
        hit, how = None, "unresolved"
        if k in pos_by_key:
            hit, how = pos_by_key[k], "exact"
        elif low in dash_alias and key(dash_alias[low]) in pos_by_key:
            hit, how = pos_by_key[key(dash_alias[low])], "dash_brand_alias"
        elif low in manual and key(manual[low]) in pos_by_key:
            hit, how = pos_by_key[key(manual[low])], "vm_brand_alias.csv"
        else:
            stem = key(_SUFFIX.sub("", name)) or k
            if stem in pos_by_key:
                hit, how = pos_by_key[stem], "suffix_stripped"
            else:
                cands = [b for kk, b in pos_by_key.items()
                         if len(stem) >= 4 and (kk.startswith(stem) or stem.startswith(kk))]
                if len(cands) == 1:
                    hit, how = cands[0], "unique_prefix"
                elif len(cands) > 1:
                    how = "ambiguous:" + "|".join(sorted(cands)[:4])
        rows.append(dict(vm_brand=name, pos_brand=hit, brand_key=key(hit) if hit else None,
                         method=how))
    return rows


def upload(dash: Path) -> int:
    """Same upload path as publish.py: tta_env.bootstrap() then DriveClient."""
    sys.path.insert(0, str(HERE))          # tta_env / tta_drive live next to publish.py
    from tta_env import bootstrap
    from tta_drive import DriveClient
    bootstrap()
    folder = os.environ.get("TTA_DRIVE_STATE")
    if not folder:
        print("  TTA_DRIVE_STATE not set — skipping upload")
        return 1
    print("\n  uploading…")
    DriveClient().upload(dash, folder)
    print(f"  {dash} pushed to Drive")
    return 0


def build(src: Path, dash: Path, app_file: Path) -> None:
    con = duckdb.connect(str(dash))
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")

    print("[1/8] takeover calendar")
    takeovers = load_takeovers(app_file)
    extra = con.execute("SELECT brand, start_date, end_date, stores, surface, source "
                        "FROM src.dim_takeover").fetchall() \
        if con.execute("SELECT count(*) FROM src.dim_takeover").fetchone()[0] else []
    print(f"      {len(takeovers)} from TAKEOVERS, {len(extra)} from takeovers.csv")

    print("[2/8] brand resolution")
    pos_brands = [b for (b,) in con.execute(
        "SELECT DISTINCT brand FROM dash_brand_week WHERE brand IS NOT NULL").fetchall()]
    dash_alias = {a.lower(): c for a, c in con.execute(
        "SELECT alias, canonical FROM dash_brand_alias").fetchall()}
    manual = {}
    if VM_ALIAS_CSV.exists():
        with VM_ALIAS_CSV.open(newline="", encoding="utf-8-sig") as f:
            manual = {r["vm_name"].strip().lower(): r["pos_brand"].strip()
                      for r in csv.DictReader(f) if r.get("vm_name")}
    vm_names = [b for (b,) in con.execute(
        "SELECT DISTINCT brand FROM src.fact_vm_placement WHERE is_brand").fetchall()]
    resolved = resolve_brands(vm_names, pos_brands, dash_alias, manual)
    con.execute("CREATE OR REPLACE TABLE dash_vm_brand_resolve "
                "(vm_brand VARCHAR, pos_brand VARCHAR, brand_key VARCHAR, method VARCHAR)")
    con.executemany("INSERT INTO dash_vm_brand_resolve VALUES (?,?,?,?)",
                    [[r["vm_brand"], r["pos_brand"], r["brand_key"], r["method"]] for r in resolved])
    n_ok = sum(1 for r in resolved if r["pos_brand"])
    print(f"      {n_ok}/{len(resolved)} VM brand names resolved to a POS brand")

    print("[3/8] takeover × store-week")
    con.execute("CREATE OR REPLACE TABLE dash_vm_takeover_week "
                "(takeover VARCHAR, pos_brand VARCHAR, brand_key VARCHAR, store_key INTEGER, "
                "iso_year INTEGER, iso_week INTEGER, covered_days INTEGER, surface VARCHAR)")
    weeks = con.execute("SELECT DISTINCT iso_year, iso_week, week_start, week_end "
                        "FROM src.dim_vm_week").fetchall()
    tk_rows = []
    pos_lower = {b.lower(): b for b in pos_brands}
    for t in takeovers:
        pats = [p.lower() for p in t.get("patterns", [])]
        matched = sorted({b for lb, b in pos_lower.items() if any(p in lb for p in pats)})
        for iy, iw, ws, we in weeks:
            if t["start"] <= we and t["end"] >= ws:
                days = (min(t["end"], we) - max(t["start"], ws)).days + 1
                for b in matched:
                    for sk in ALL_STORE_KEYS:
                        tk_rows.append([t["name"], b, key(b), sk, iy, iw, days, "kiosk"])
    for brand, s, e, stores, surface, source in extra:
        skeys = [{"DTBK": 1, "FIFTH": 2, "SOHO": 3, "USQ": 4}[x] for x in stores.split("|")]
        b = pos_lower.get(brand.lower(), brand)
        for iy, iw, ws, we in weeks:
            if s <= we and e >= ws:
                days = (min(e, we) - max(s, ws)).days + 1
                for sk in skeys:
                    tk_rows.append([f"{brand} ({source})", b, key(b), sk, iy, iw, days, surface])
    con.executemany("INSERT INTO dash_vm_takeover_week VALUES (?,?,?,?,?,?,?,?)", tk_rows)

    # dash_brand_week is brand x category per week -> collapse to brand per week
    con.execute("""
        CREATE OR REPLACE TEMP VIEW bw AS
        SELECT store_key, iso_year, iso_week, brand,
               sum(net) AS net, sum(units) AS units, sum(baskets) AS baskets, sum(gm) AS gm
        FROM dash_brand_week GROUP BY ALL""")

    print("[4/8] dash_vm_placement_week")
    con.execute("""
        CREATE OR REPLACE TABLE dash_vm_placement_week AS
        SELECT p.store, w.store_key, w.iso_year, w.iso_week, w.week_start,
               p.month, p.week_n, p.bay_raw, p.bay_type, p.shelf_tier, p.position_label,
               p.brand_raw, p.brand AS vm_brand, r.pos_brand, r.brand_key, p.product_hint,
               p.is_brand, p.comment, p.impact_note,
               s.net, s.units, s.baskets, s.gm
        FROM src.fact_vm_placement p
        JOIN src.dim_vm_week w USING (store, month, week_n)
        LEFT JOIN dash_vm_brand_resolve r ON r.vm_brand = p.brand AND p.is_brand
        LEFT JOIN bw s
          ON s.store_key = w.store_key AND s.iso_year = w.iso_year
         AND s.iso_week = w.iso_week AND s.brand = r.pos_brand
    """)

    print("[5/8] dash_vm_brand_week")
    con.execute(f"""
        CREATE OR REPLACE TABLE dash_vm_brand_week AS
        WITH wk AS (SELECT DISTINCT store_key, iso_year, iso_week, week_start FROM src.dim_vm_week),
        shelf AS (
            SELECT store_key, iso_year, iso_week, pos_brand,
                   count(*)                            AS shelf_slots,
                   sum(shelf_tier = 'Top')             AS top_slots,
                   sum(shelf_tier = 'Middle')          AS middle_slots,
                   sum(shelf_tier = 'Bottom')          AS bottom_slots,
                   sum(bay_type = 'Spotlight')         AS spotlight_slots,
                   string_agg(DISTINCT bay_type, ', ') AS bay_types
            FROM dash_vm_placement_week WHERE pos_brand IS NOT NULL
            GROUP BY ALL),
        kiosk AS (
            SELECT store_key, iso_year, iso_week, pos_brand,
                   max(covered_days) AS kiosk_days, string_agg(DISTINCT takeover, ', ') AS takeover
            FROM dash_vm_takeover_week GROUP BY ALL),
        brands AS (
            SELECT pos_brand FROM shelf UNION SELECT pos_brand FROM kiosk
            UNION SELECT DISTINCT brand FROM dash_brand_week WHERE iso_year >= 2026)
        SELECT b.pos_brand AS brand, wk.store_key, wk.iso_year, wk.iso_week, wk.week_start,
               coalesce(s.shelf_slots, 0) AS shelf_slots,
               coalesce(s.top_slots, 0) AS top_slots,
               coalesce(s.middle_slots, 0) AS middle_slots,
               coalesce(s.bottom_slots, 0) AS bottom_slots,
               coalesce(s.spotlight_slots, 0) AS spotlight_slots,
               s.bay_types, k.kiosk_days AS takeover_days, k.takeover,
               CASE WHEN s.shelf_slots > 0 THEN 'shelf' ELSE 'none' END AS placement_state,
               k.takeover IS NOT NULL AS in_takeover,
               CASE WHEN s.top_slots > 0 THEN 'Top' WHEN s.middle_slots > 0 THEN 'Middle'
                    WHEN s.bottom_slots > 0 THEN 'Bottom' END AS best_tier,
               coalesce(d.net, 0) AS net, coalesce(d.units, 0) AS units,
               coalesce(d.baskets, 0) AS baskets, coalesce(d.gm, 0) AS gm
        FROM brands b CROSS JOIN wk
        LEFT JOIN shelf s ON s.pos_brand = b.pos_brand AND s.store_key = wk.store_key
                         AND s.iso_year = wk.iso_year AND s.iso_week = wk.iso_week
        LEFT JOIN kiosk k ON k.pos_brand = b.pos_brand AND k.store_key = wk.store_key
                         AND k.iso_year = wk.iso_year AND k.iso_week = wk.iso_week
        LEFT JOIN bw d ON d.brand = b.pos_brand AND d.store_key = wk.store_key
                         AND d.iso_year = wk.iso_year AND d.iso_week = wk.iso_week
    """)

    print("[6/8] takeover cross-reference")
    # bay_type / product_hint -> POS category, so a Rythm flower-wall window can
    # be lined up with Rythm flower sales rather than the whole brand.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW placed_cat AS
        SELECT store_key, iso_year, iso_week, pos_brand, bay_raw, bay_type, shelf_tier,
               position_label, product_hint,
               CASE
                 WHEN bay_type IN ('Flower','Vape','Pre-Roll','Edible') THEN bay_type
                 WHEN product_hint ILIKE '%pre%roll%' THEN 'Pre-Roll'
                 WHEN product_hint ILIKE '%flower%'   THEN 'Flower'
                 WHEN product_hint ILIKE '%vape%'     THEN 'Vape'
                 WHEN product_hint ILIKE '%edible%'   THEN 'Edible'
                 WHEN product_hint ILIKE '%concentrate%' OR bay_raw ILIKE '%concentrate%' THEN 'Concentrate'
                 ELSE 'Spotlight/Other' END AS category
        FROM dash_vm_placement_week WHERE pos_brand IS NOT NULL""")
    con.execute("""
        CREATE OR REPLACE TABLE dash_vm_takeover_xref AS
        WITH tk AS (
            SELECT DISTINCT takeover, pos_brand, store_key, iso_year, iso_week, covered_days
            FROM dash_vm_takeover_week),
        win AS (  -- window weeks + 4 weeks before, per takeover x store
            SELECT t.takeover, t.pos_brand, t.store_key, w.iso_year, w.iso_week, w.week_start,
                   x.covered_days IS NOT NULL AS in_window, x.covered_days,
                   CASE WHEN x.covered_days IS NOT NULL THEN 'during' ELSE 'prior 4 wks' END AS phase
            FROM (SELECT DISTINCT takeover, pos_brand, store_key,
                         min(iso_year*100+iso_week) AS first_wk FROM tk GROUP BY ALL) t
            JOIN (SELECT DISTINCT store_key, iso_year, iso_week, week_start FROM src.dim_vm_week
                  UNION SELECT DISTINCT store_key, iso_year, iso_week,
                        date_trunc('week', make_date(iso_year,1,4)) + (iso_week-1)*INTERVAL 7 DAY
                        FROM dash_brand_week WHERE iso_year >= 2026) w
              ON w.store_key = t.store_key
             AND w.iso_year*100+w.iso_week BETWEEN t.first_wk - 4 AND
                 (SELECT max(iso_year*100+iso_week) FROM tk k WHERE k.takeover=t.takeover)
            LEFT JOIN tk x ON x.takeover=t.takeover AND x.store_key=w.store_key
                          AND x.iso_year=w.iso_year AND x.iso_week=w.iso_week),
        cats AS (
            SELECT DISTINCT brand, category FROM dash_brand_product_week
            WHERE iso_year >= 2026 AND category IS NOT NULL),
        sales AS (
            SELECT store_key, iso_year, iso_week, brand, category,
                   sum(net) AS net, sum(units) AS units, sum(baskets) AS baskets
            FROM dash_brand_product_week GROUP BY ALL),
        disp AS (
            SELECT store_key, iso_year, iso_week, pos_brand, category,
                   count(*) AS slots,
                   string_agg(bay_raw || ' / ' || shelf_tier ||
                              CASE WHEN position_label <> '' THEN ' ' || position_label ELSE '' END,
                              '; ' ORDER BY bay_raw) AS display
            FROM placed_cat GROUP BY ALL)
        SELECT w.takeover, w.pos_brand AS brand, w.store_key, w.iso_year, w.iso_week,
               w.week_start, w.phase, w.in_window, w.covered_days,
               c.category,
               coalesce(d.slots, 0) AS slots, d.display,
               coalesce(s.net, 0) AS net, coalesce(s.units, 0) AS units,
               coalesce(s.baskets, 0) AS baskets
        FROM win w
        JOIN cats c ON c.brand = w.pos_brand
        LEFT JOIN disp d ON d.store_key=w.store_key AND d.iso_year=w.iso_year
                        AND d.iso_week=w.iso_week AND d.pos_brand=w.pos_brand
                        AND d.category=c.category
        LEFT JOIN sales s ON s.store_key=w.store_key AND s.iso_year=w.iso_year
                         AND s.iso_week=w.iso_week AND s.brand=w.pos_brand
                         AND s.category=c.category
        ORDER BY takeover, store_key, iso_year, iso_week, category
    """)

    print("[7/8] coverage")
    con.execute("""
        CREATE OR REPLACE TABLE dash_vm_coverage AS
        SELECT store_key, iso_year, iso_week, week_start, bay_raw, bay_type, shelf_tier,
               count(*) AS positions,
               count(*) FILTER (WHERE brand_raw <> '') AS filled,
               count(*) FILTER (WHERE pos_brand IS NOT NULL) AS brand_matched
        FROM dash_vm_placement_week GROUP BY ALL
    """)
    print("[8/8] ingest log")
    con.execute("CREATE OR REPLACE TABLE dash_vm_ingest_log AS SELECT * FROM src.vm_ingest_log")

    n = con.execute("SELECT count(*) FROM dash_vm_brand_week").fetchone()[0]
    st = con.execute("SELECT placement_state, in_takeover, count(*) FROM dash_vm_brand_week "
                     "GROUP BY ALL ORDER BY 3 DESC").fetchall()
    nx = con.execute("SELECT count(*) FROM dash_vm_takeover_xref").fetchone()[0]
    print(f"dash_vm_brand_week: {n:,} rows; {st}\ndash_vm_takeover_xref: {nx:,} rows")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC_DB)
    ap.add_argument("--dash", type=Path, default=DASH_DB)
    ap.add_argument("--app", type=Path, default=APP_FILE)
    ap.add_argument("--upload", action="store_true",
                    help="after building, push cerebral_dash.duckdb to Drive (like publish.py --upload)")
    a = ap.parse_args()
    build(a.src, a.dash, a.app)
    if a.upload:
        sys.exit(upload(a.dash))
