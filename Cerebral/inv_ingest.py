"""
inv_ingest.py — weekly stock levels for the Visual Merch tab.

Rebuilds on-hand by store x product x ISO week from three things:

  1. the newest Dutchie "Current Inventory" export in the inventory folder
     (the ANCHOR: on hand by room at export time),
  2. every Dutchie "Inventory Adjustments" export in the folder (Receive,
     Move, Adjust, Convert, Combine, Change Product ...), deduplicated across
     overlapping files,
  3. POS sales lines already in tta.duckdb (fact_line: product, units, txn_ts).

On hand at any earlier moment = anchor - (everything that happened after it).
Rolling BACK from a known snapshot means errors do not compound over months.

Two balances are kept per store x product:
  total_sellable  units in sellable rooms (sales floor + vault/day vault/holding)
  floor           units in SALES FLOOR rooms only  <- the one that matters for VM

Ledger effects
  Receive        total += qty                    (lands in Receiving/Vault, not floor)
  Move           floor += qty if to a floor room; floor -= qty if from a floor room;
                 total +/- qty when moving into/out of an excluded room
                 (quarantine, destruction, sample, display)
  Adjust         total += qty (signed).  Room is not stated; floor gets the
                 adjustment only when the package's anchor room is a floor room.
  Convert/Combine/Change Product  qty is signed per row and product-specific,
                 so it applies to total; floor untouched (room not stated).
  Sales (POS)    total -= units, floor -= units  (sales come off the floor);
                 returns add back.

Output (tta.duckdb)
  fact_inventory_week   store_key, iso_year, iso_week, week_start, product,
                        brand, category,
                        total_start, total_end, floor_start, floor_end,
                        received, sold_units, moved_to_floor, adjusted,
                        floor_min (lowest floor level seen during the week),
                        stockout_floor (floor hit <= 0 during the week),
                        days_of_supply_floor (floor_end / avg daily sold, last 4 wks)
  inv_ingest_log        per-file parse report
  inv_anchor            the snapshot used (store_key, product, room, class, qty)
  inv_opening_offset    per store x product: opening stock inferred from the
                        deepest negative in the rolled-back series (see below)

Anchors: besides the newest Current Inventory export in the folder, every
real snapshot already in fact_inventory (loaded by the Monday ETL; source IS
NULL; >= 3000 rows) is an anchor. Walking back through time, balances reset
to each real count as it is crossed, so drift only accumulates between
anchors. Every future Monday snapshot therefore tightens history for free.
  fact_inventory        ALSO fed: the real anchor snapshot plus one reconstructed
                        snapshot per week-end (source column marks them), so
                        publish.py's dash_inventory / dash_bei / dash_acc_product_inv
                        and the Insights "Inventory efficiency" block go current

Opening stock: the ledger cannot see units received before --since (or
corrected later by a positive Adjust). Those surface as the series going
negative. Each product's series is shifted up by its deepest dip, so a
stockout is only flagged once the floor hits zero AFTER allowing for that
unknown opening stock. Conservative by design.

Usage
  python Cerebral\\inv_ingest.py                       (from the repo root)
  python Cerebral\\inv_ingest.py --folder inventory --db tta.duckdb --since 2026-01-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from collections import defaultdict
from pathlib import Path

import duckdb
from openpyxl import load_workbook

STORE_KEY = {  # Dutchie location name fragment -> Cerebral store_key
    "downtown brooklyn": 1, "fifth avenue": 2, "soho": 3, "union square": 4,
}


def store_of(location: str) -> int | None:
    low = (location or "").lower()
    return next((k for frag, k in STORE_KEY.items() if frag in low), None)


def norm_product(p) -> str:
    return re.sub(r"\s+", " ", str(p or "")).strip()


# ---------------------------------------------------------------- rooms
def room_class(room: str) -> str:
    """floor | backstock | display | excluded"""
    r = (room or "").strip().lower()
    if "display" in r or "marketing" in r or "reward" in r:
        return "display"
    if any(x in r for x in ("sample", "quarantine", "destruction", "discrepancy",
                            "return", "research")):
        return "excluded"
    if "sales floor" in r or r == "salesfloor":
        return "floor"
    if any(x in r for x in ("vault", "holding", "receiving", "move from")):
        return "backstock"
    return "backstock"  # unknown room: treat as sellable backstock, log it


SELLABLE = {"floor", "backstock"}


# ---------------------------------------------------------------- readers
def _header_block(ws):
    """Dutchie exports: 3 meta rows (Export/From/To Date), then the header."""
    rows = ws.iter_rows(min_row=1, max_row=6, values_only=True)
    meta, header_row = {}, None
    for i, r in enumerate(rows, 1):
        if r and isinstance(r[0], str) and r[0].endswith("Date:"):
            meta[r[0].rstrip(":").strip()] = r[1]
        elif r and r[0] and any(str(c) in ("Location", "Location Name") for c in r if c):
            header_row = i
            break
    return meta, header_row


def _parse_dt(s) -> dt.datetime | None:
    if isinstance(s, dt.datetime):
        return s
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            pass
    return None


def read_snapshot(path: Path):
    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    meta, h = _header_block(ws)
    hdr = [str(c or "").strip() for c in next(ws.iter_rows(min_row=h, max_row=h, values_only=True))]
    ix = {n: hdr.index(n) for n in ("Location Name", "Inventory Room", "Product Name",
                                    "Category", "Quantity on Hand", "Package ID", "Brand Name",
                                    "Inventory Cost", "Inventory Price")}
    rows = []
    for r in ws.iter_rows(min_row=h + 1, values_only=True):
        if not r or not r[ix["Location Name"]]:
            continue
        sk = store_of(r[ix["Location Name"]])
        if sk is None:
            continue
        rows.append(dict(store_key=sk, room=str(r[ix["Inventory Room"]] or ""),
                         room_class=room_class(r[ix["Inventory Room"]]),
                         product=norm_product(r[ix["Product Name"]]),
                         category=str(r[ix["Category"]] or ""),
                         brand=str(r[ix["Brand Name"]] or ""),
                         package_id=str(r[ix["Package ID"]] or ""),
                         qty=float(r[ix["Quantity on Hand"]] or 0),
                         unit_cost=float(r[ix["Inventory Cost"]] or 0),
                         unit_price=float(r[ix["Inventory Price"]] or 0)))
    return _parse_dt(meta.get("Export Date")), rows


_MOVE = re.compile(r"From Location\s*(.+?)\s+To Location\s*(.+?)\s*$", re.I)
_ROOM = re.compile(r"Room\s+(.+?)(?:\s+Table\b.*)?$", re.I)


def read_ledger(path: Path):
    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    meta, h = _header_block(ws)
    hdr = [str(c or "").strip() for c in next(ws.iter_rows(min_row=h, max_row=h, values_only=True))]
    ix = {n: hdr.index(n) for n in ("Location", "TransactionDate", "Action", "Product",
                                    "BrandName", "Category", "qty", "InventoryComment", "Cost")}
    rows = []
    for r in ws.iter_rows(min_row=h + 1, values_only=True):
        if not r or not r[ix["Location"]]:
            continue
        sk = store_of(r[ix["Location"]])
        ts = r[ix["TransactionDate"]]
        if sk is None or not isinstance(ts, dt.datetime):
            continue
        comment = str(r[ix["InventoryComment"]] or "")
        from_c = to_c = None
        from_sk = to_sk = sk
        if str(r[ix["Action"]]).strip() == "Move":
            m = _MOVE.search(comment)
            if m:
                fr, to = m.group(1), m.group(2)
                fm, tm = _ROOM.search(fr), _ROOM.search(to)
                from_c = room_class(fm.group(1) if fm else fr)
                to_c = room_class(tm.group(1) if tm else to)
                from_sk = store_of(fr.split(" Room")[0]) or sk
                to_sk = store_of(to.split(" Room")[0]) or sk
        pkg = re.search(r"Package\s+(\S+)", comment)
        rows.append(dict(store_key=sk, ts=ts, action=str(r[ix["Action"]]).strip(),
                         product=norm_product(r[ix["Product"]]),
                         brand=str(r[ix["BrandName"]] or ""),
                         category=str(r[ix["Category"]] or ""),
                         qty=float(r[ix["qty"]] or 0),
                         unit_cost=float(r[ix["Cost"]] or 0) if r[ix["Cost"]] not in (None, "") else None,
                         from_class=from_c, to_class=to_c,
                         from_sk=from_sk, to_sk=to_sk,
                         package_id=pkg.group(1) if pkg else "",
                         key=(sk, ts, str(r[ix["Action"]]), norm_product(r[ix["Product"]]),
                              float(r[ix["qty"]] or 0), comment[:120])))
    return meta, rows


# ---------------------------------------------------------------- build
def build(folder: Path, db: Path, since: dt.date) -> None:
    files = sorted(folder.glob("*.xlsx"))
    if not files:
        raise SystemExit(f"no .xlsx files in {folder.resolve()}")

    snapshots, ledgers, log = [], [], []
    for i, f in enumerate(files, 1):
        ws = load_workbook(f, read_only=True, data_only=True).worksheets[0]
        _, h = _header_block(ws)
        hdr = [str(c or "") for c in next(ws.iter_rows(min_row=h, max_row=h, values_only=True))] if h else []
        if "Quantity on Hand" in hdr:
            ts, rows = read_snapshot(f)
            snapshots.append((ts, f, rows))
            print(f"[{i}/{len(files)}] {f.name:<50} snapshot @ {ts}  rows={len(rows):,}")
            log.append(dict(file=f.name, kind="snapshot", rows=len(rows), note=str(ts)))
        elif "InventoryComment" in hdr:
            meta, rows = read_ledger(f)
            ledgers.append(rows)
            span = f"{meta.get('From Date')} -> {meta.get('To Date')}"
            print(f"[{i}/{len(files)}] {f.name:<50} ledger   {span}  rows={len(rows):,}")
            log.append(dict(file=f.name, kind="ledger", rows=len(rows), note=span))
        else:
            print(f"[{i}/{len(files)}] {f.name:<50} ⚠ not a Dutchie inventory export — skipped")
            log.append(dict(file=f.name, kind="skipped", rows=0, note="unrecognised header"))

    if not snapshots:
        raise SystemExit("no Current Inventory snapshot found — the anchor is required")
    anchor_ts, anchor_file, anchor_rows = max(snapshots, key=lambda s: s[0])
    print(f"\nanchor: {anchor_file.name} @ {anchor_ts}")

    # dedupe ledger rows across overlapping exports; keep only events before the anchor
    seen, events = set(), []
    for rows in ledgers:
        for r in rows:
            if r["key"] in seen or r["ts"] > anchor_ts or r["ts"].date() < since:
                continue
            seen.add(r["key"])
            events.append(r)
    print(f"ledger events kept: {len(events):,} (deduped, {since} -> anchor)")

    # anchor balances per store x product, plus package -> class for Adjust rows
    total = defaultdict(float)
    floor = defaultdict(float)
    pkg_class = {}
    prod_meta = {}
    prod_cost, prod_price = {}, {}
    for r in anchor_rows:
        k = (r["store_key"], r["product"])
        prod_meta.setdefault(k, (r["brand"], r["category"]))
        if r["unit_cost"]:
            prod_cost.setdefault(k, r["unit_cost"])
        if r["unit_price"]:
            prod_price.setdefault(k, r["unit_price"])
        pkg_class[(r["store_key"], r["package_id"])] = r["room_class"]
        if r["room_class"] in SELLABLE:
            total[k] += r["qty"]
        if r["room_class"] == "floor":
            floor[k] += r["qty"]

    # Real snapshots already loaded by the Monday ETL (source IS NULL) become
    # extra anchors: when the roll-back crosses one, balances reset to that
    # count, so error accumulates only between anchors, not across the year.
    con_r = duckdb.connect(str(db), read_only=True)
    extra_anchors = {}
    if con_r.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name='fact_inventory'").fetchone()[0]:
        has_src = con_r.execute("SELECT count(*) FROM information_schema.columns "
                                "WHERE table_name='fact_inventory' AND column_name='source'").fetchone()[0]
        src_filter = "AND source IS NULL" if has_src else ""
        for d, n in con_r.execute(f"SELECT snapshot_date, count(*) FROM fact_inventory "
                                  f"WHERE snapshot_date < ? {src_filter} GROUP BY 1 ORDER BY 1 DESC",
                                  [anchor_ts.date()]).fetchall():
            if n < 3000:      # partial load (one store, half a file): not a usable anchor
                continue
            rows = con_r.execute(f"SELECT store_key, product, room, qty_on_hand FROM fact_inventory "
                                 f"WHERE snapshot_date = ? {src_filter}", [d]).fetchall()
            tot, flr = defaultdict(float), defaultdict(float)
            for sk, product, room, qty in rows:
                rc = room_class(room)
                k = (sk, norm_product(product))
                if rc in SELLABLE:
                    tot[k] += qty or 0
                if rc == "floor":
                    flr[k] += qty or 0
            # Monday exports happen before opening: anchor at 06:00 that day
            extra_anchors[dt.datetime.combine(d, dt.time(6))] = (dict(tot), dict(flr))
            print(f"extra anchor: real snapshot {d} ({n:,} rows, {sum(tot.values()):,.0f} sellable units)")
    con_r.close()

    # sales from POS — and the guard that matters most: the anchor must not be
    # later than the sales data. Rolling back through days that have receipts
    # but no sales lines shifts EVERY product too low by those days' sales.
    con = duckdb.connect(str(db))
    max_sales = con.execute("SELECT max(txn_ts) FROM fact_line").fetchone()[0]
    if max_sales is not None and anchor_ts > max_sales + dt.timedelta(hours=6):
        gap = anchor_ts - max_sales
        cutoff = dt.datetime.combine(max_sales.date(), dt.time.max)
        print(f"\n⚠ sales in fact_line end {max_sales:%Y-%m-%d %H:%M} but the snapshot is "
              f"{anchor_ts:%Y-%m-%d %H:%M} ({gap.days} days later).\n"
              f"  Ledger events after {cutoff:%Y-%m-%d} are IGNORED so the anchor is treated as "
              f"end-of-day {max_sales:%Y-%m-%d}. Receipts and sales in the gap roughly cancel;\n"
              f"  the residual error is their difference, not ten days of sales.\n"
              f"  For an exact result, export Current Inventory the morning after the last sales day.")
        events = [e for e in events if e["ts"] <= cutoff]
        anchor_ts = cutoff
        print(f"ledger events after truncation: {len(events):,}")
    sales = con.execute("""
        SELECT store_key, txn_ts, product,
               sum(CASE WHEN is_return THEN -units ELSE units END) AS units
        FROM fact_line
        WHERE txn_ts >= ? AND txn_ts <= ?
        GROUP BY ALL""", [dt.datetime.combine(since, dt.time()), anchor_ts]).fetchall()
    print(f"sales rows: {len(sales):,}")

    # unified, signed event stream  (dt, store, product, d_total, d_floor, kind, qty)
    stream = []
    n_xfer = 0
    for r in sorted(events, key=lambda e: e["ts"]):   # oldest first so the newest cost wins
        k = (r["store_key"], r["product"])
        prod_meta.setdefault(k, (r["brand"], r["category"]))
        if r["unit_cost"] and k not in prod_cost:
            prod_cost[k] = r["unit_cost"]
        a, q = r["action"], r["qty"]
        if a == "Receive":
            stream.append((r["ts"], *k, q, 0.0, "received", q))
        elif a == "Move":
            if r["from_sk"] != r["to_sk"]:
                # transfer between stores: dispatch from one, receipt at the other
                n_xfer += 1
                if r["from_class"] in SELLABLE:
                    stream.append((r["ts"], r["from_sk"], k[1], -q,
                                   -q if r["from_class"] == "floor" else 0.0, "transferred_out", q))
                if r["to_class"] in SELLABLE:
                    prod_meta.setdefault((r["to_sk"], k[1]), (r["brand"], r["category"]))
                    stream.append((r["ts"], r["to_sk"], k[1], q,
                                   q if r["to_class"] == "floor" else 0.0, "received", q))
                continue
            d_total = 0.0
            if r["from_class"] in SELLABLE and r["to_class"] not in SELLABLE:
                d_total = -q
            elif r["from_class"] not in SELLABLE and r["to_class"] in SELLABLE:
                d_total = q
            d_floor = (q if r["to_class"] == "floor" else 0.0) - (q if r["from_class"] == "floor" else 0.0)
            stream.append((r["ts"], *k, d_total, d_floor, "moved_to_floor", q if r["to_class"] == "floor" else 0.0))
        elif a == "Adjust":
            on_floor = pkg_class.get((r["store_key"], r["package_id"])) == "floor"
            stream.append((r["ts"], *k, q, q if on_floor else 0.0, "adjusted", q))
        else:  # Convert / Combine / Change Product / anything else signed
            stream.append((r["ts"], *k, q, 0.0, "adjusted", q))
    for sk, ts, product, units in sales:
        k = (sk, norm_product(product))
        if k not in total and k not in prod_meta:
            continue  # product never seen in inventory; nothing to net
        stream.append((ts, sk, k[1], -units, -units, "sold_units", units))
    stream.sort(key=lambda e: e[0], reverse=True)  # newest first: we roll BACK
    print(f"cross-store transfers booked: {n_xfer}")

    # week boundaries (ISO weeks, Monday start), newest first
    def week_start(d: dt.date) -> dt.date:
        return d - dt.timedelta(days=d.weekday())
    first_ws = week_start(since)
    last_ws = week_start(anchor_ts.date())
    weeks = []
    w = last_ws
    while w >= first_ws:
        weeks.append(w)
        w -= dt.timedelta(days=7)

    # roll back, newest first. Balances are updated event by event; whenever
    # the walk crosses a real snapshot, balances reset to that count.
    anchors_desc = sorted(extra_anchors.items(), key=lambda a: a[0], reverse=True)
    out = []
    cur_total, cur_floor = dict(total), dict(floor)
    idx = 0
    n_resets = 0
    for ws_ in weeks:
        we_ts = dt.datetime.combine(ws_ + dt.timedelta(days=7), dt.time())  # exclusive end
        ws_ts = dt.datetime.combine(ws_, dt.time())
        # anchors that fall after this week's end but before the previous week's
        # start were handled in the previous iteration; those inside this week
        # are applied when the event walk passes them.
        end_total, end_floor = dict(cur_total), dict(cur_floor)
        flows = defaultdict(lambda: defaultdict(float))
        floor_min = dict(end_floor)
        wk_anchors = [a for a in anchors_desc if ws_ts <= a[0] < we_ts]

        def apply_anchor(a_ts):
            nonlocal cur_total, cur_floor, n_resets
            tot, flr = extra_anchors[a_ts]
            keys_ = set(cur_total) | set(tot) | set(cur_floor) | set(flr)
            cur_total = {k: tot.get(k, 0.0) for k in keys_}
            cur_floor = {k: flr.get(k, 0.0) for k in keys_}
            for k in keys_:
                floor_min[k] = min(floor_min.get(k, cur_floor[k]), cur_floor[k])
            n_resets += 1

        while idx < len(stream) and stream[idx][0] >= ws_ts:
            ts, sk, product, d_total, d_floor, kind, qty = stream[idx]
            idx += 1
            if ts >= we_ts:
                continue
            # cross any anchor that sits between this event and the previous one
            while wk_anchors and wk_anchors[0][0] > ts:
                apply_anchor(wk_anchors.pop(0)[0])
            k = (sk, product)
            cur_total[k] = cur_total.get(k, 0.0) - d_total
            cur_floor[k] = cur_floor.get(k, 0.0) - d_floor
            floor_min[k] = min(floor_min.get(k, cur_floor[k]), cur_floor[k])
            flows[k][kind] += qty
        while wk_anchors:                      # anchor earlier than every event this week
            apply_anchor(wk_anchors.pop(0)[0])
        start_total, start_floor = cur_total, cur_floor

        iy, iw, _ = ws_.isocalendar()
        keys = set(start_total) | set(end_total) | set(flows)
        for k in keys:
            et, ef = end_total.get(k, 0.0), end_floor.get(k, 0.0)
            st_, sf = start_total.get(k, 0.0), start_floor.get(k, 0.0)
            fl = flows.get(k, {})
            if not (et or ef or st_ or sf or fl):
                continue
            fmin = min(floor_min.get(k, ef), ef, sf)
            brand, cat = prod_meta.get(k, ("", ""))
            out.append((k[0], iy, iw, ws_, k[1], brand, cat,
                        st_, et, sf, ef,
                        fl.get("received", 0.0), fl.get("sold_units", 0.0),
                        fl.get("moved_to_floor", 0.0), fl.get("adjusted", 0.0),
                        fmin, fmin <= 0 and (sf > 0 or ef > 0 or fl.get("sold_units", 0) > 0)))
        cur_total, cur_floor = dict(start_total), dict(start_floor)
    if anchors_desc:
        print(f"anchor resets applied: {n_resets} of {len(anchors_desc)} real snapshots")

    con.execute("""
        CREATE OR REPLACE TABLE fact_inventory_week (
            store_key INTEGER, iso_year INTEGER, iso_week INTEGER, week_start DATE,
            product VARCHAR, brand VARCHAR, category VARCHAR,
            total_start DOUBLE, total_end DOUBLE, floor_start DOUBLE, floor_end DOUBLE,
            received DOUBLE, sold_units DOUBLE, moved_to_floor DOUBLE, adjusted DOUBLE,
            floor_min DOUBLE, stockout_floor BOOLEAN)""")
    con.executemany("INSERT INTO fact_inventory_week VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    # Opening stock the ledger never saw (received before --since, or corrected
    # later by a positive Adjust) shows up as the series dipping below zero.
    # The deepest dip is the least opening stock the product must have had:
    # shift each product's series up by that much, total and floor separately,
    # and re-derive the stockout flag on the shifted floor series.
    con.execute("""
        CREATE OR REPLACE TABLE inv_opening_offset AS
        SELECT store_key, product,
               greatest(0, -min(least(total_start, total_end)))            AS total_offset,
               greatest(0, -min(least(floor_start, floor_end, floor_min))) AS floor_offset
        FROM fact_inventory_week GROUP BY ALL""")
    con.execute("""
        UPDATE fact_inventory_week t SET
            total_start = t.total_start + o.total_offset,
            total_end   = t.total_end   + o.total_offset,
            floor_start = t.floor_start + o.floor_offset,
            floor_end   = t.floor_end   + o.floor_offset,
            floor_min   = t.floor_min   + o.floor_offset
        FROM inv_opening_offset o
        WHERE o.store_key = t.store_key AND o.product = t.product
          AND (o.total_offset > 0 OR o.floor_offset > 0)""")
    # stockout: floor hit zero this week. For products whose floor series needed
    # an offset, the dip week is UNKNOWN (NULL) rather than a stockout — the
    # offset put it at exactly zero by construction, which proves nothing.
    con.execute("""
        UPDATE fact_inventory_week t SET stockout_floor =
            CASE WHEN t.floor_min > 0 THEN FALSE
                 -- nothing on the floor and nothing happened: not ranged, not a stockout
                 WHEN t.floor_start <= 0 AND t.moved_to_floor <= 0 AND t.sold_units <= 0 THEN FALSE
                 -- hit zero with activity, but this product's opening stock was inferred:
                 -- the dip is where the inference came from, so it proves nothing
                 WHEN o.floor_offset > 0 AND t.floor_min <= 0 AND t.floor_start <= 0 THEN NULL
                 ELSE TRUE END
        FROM inv_opening_offset o
        WHERE o.store_key = t.store_key AND o.product = t.product""")
    n_off = con.execute("SELECT count(*), sum(total_offset) FROM inv_opening_offset WHERE total_offset > 0").fetchone()
    print(f"opening-stock offsets applied to {n_off[0]:,} store-products "
          f"({(n_off[1] or 0):,.0f} units inferred as pre-{since} stock)")

    # days of supply on the floor: floor_end / avg daily units over the trailing 4 weeks
    con.execute("""
        ALTER TABLE fact_inventory_week ADD COLUMN days_of_supply_floor DOUBLE""")
    con.execute("""
        UPDATE fact_inventory_week t SET days_of_supply_floor = s.dos FROM (
            SELECT store_key, product, iso_year, iso_week,
                   CASE WHEN avg(sold_units) OVER w > 0
                        THEN floor_end / (avg(sold_units) OVER w / 7.0) END AS dos
            FROM fact_inventory_week
            WINDOW w AS (PARTITION BY store_key, product ORDER BY week_start
                         ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)) s
        WHERE t.store_key = s.store_key AND t.product = s.product
          AND t.iso_year = s.iso_year AND t.iso_week = s.iso_week""")
    # ---- feed fact_inventory ---------------------------------------------
    # publish.py builds dash_inventory / dash_bei / dash_acc_product_inv from
    # fact_inventory at MAX(snapshot_date). Writing the reconstruction in as
    # weekly snapshots (plus the real anchor) makes Insights' Inventory
    # Efficiency, Brand Efficiency and accessory stock current without touching
    # publish.py. Reconstructed rows carry source='ledger_rollback'; the anchor
    # is source='dutchie_export'; real Monday loads keep source NULL.
    have_fi = con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name='fact_inventory'").fetchone()[0]
    if have_fi:
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='fact_inventory'").fetchall()}
        if "source" not in cols:
            con.execute("ALTER TABLE fact_inventory ADD COLUMN source VARCHAR")
        # raw -> canonical category map, learned from rows the ETL already loaded
        cat_map = dict(con.execute(
            "SELECT raw_category, category FROM fact_inventory WHERE raw_category IS NOT NULL "
            "AND category IS NOT NULL GROUP BY ALL").fetchall())
        con.execute("DELETE FROM fact_inventory WHERE source IN ('ledger_rollback','dutchie_export')")
        fi_rows = []
        # real anchor, one row per package x room (as the export has it)
        for r in anchor_rows:
            fi_rows.append((anchor_ts.date(), r["store_key"], r["package_id"], r["product"],
                            r["category"], cat_map.get(r["category"], r["category"]), r["room"],
                            r["room_class"] in SELLABLE, r["qty"], r["unit_cost"], r["unit_price"],
                            r["qty"] * r["unit_cost"], r["qty"] * r["unit_price"], "dutchie_export"))
        # reconstructed week-ends, two synthetic rooms per product
        for (sk, iy, iw, ws_, product, brand, cat, st_, et, sf, ef, *_rest) in out:
            we = ws_ + dt.timedelta(days=6)
            if we >= anchor_ts.date():
                continue  # the real anchor covers this week
            k = (sk, product)
            uc, up = prod_cost.get(k, 0.0), prod_price.get(k, 0.0)
            for room, qty in (("SALES FLOOR", ef), ("VAULT", et - ef)):
                if qty <= 0:
                    continue
                fi_rows.append((we, sk, None, product, cat, cat_map.get(cat, cat), room, True,
                                qty, uc, up, qty * uc, qty * up, "ledger_rollback"))
        con.executemany("INSERT INTO fact_inventory (snapshot_date, store_key, package_id, product, "
                        "raw_category, category, room, sellable, qty_on_hand, unit_cost, unit_price, "
                        "ext_cost, ext_retail, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fi_rows)
        unmapped = sorted({r[4] for r in fi_rows if r[4] and r[4] not in cat_map})
        if unmapped:
            print(f"  ⚠ {len(unmapped)} raw categories have no canonical mapping in fact_inventory "
                  f"(kept as-is, will show as their own rows in Insights): {', '.join(unmapped[:8])}")
        nd = con.execute("SELECT count(DISTINCT snapshot_date) FROM fact_inventory").fetchone()[0]
        print(f"fact_inventory: +{len(fi_rows):,} rows -> {nd} snapshot dates; "
              f"latest = {con.execute('SELECT max(snapshot_date) FROM fact_inventory').fetchone()[0]}")

    con.execute("CREATE OR REPLACE TABLE inv_anchor AS SELECT * FROM (SELECT * FROM (VALUES "
                + ",".join(["(?,?,?,?,?,?)"] * len(anchor_rows)) +
                ") v(store_key, product, package_id, room, room_class, qty))",
                [x for r in anchor_rows for x in (r["store_key"], r["product"], r["package_id"],
                                                 r["room"], r["room_class"], r["qty"])])
    con.execute("CREATE OR REPLACE TABLE inv_ingest_log (file VARCHAR, kind VARCHAR, rows INTEGER, "
                "note VARCHAR, anchor_ts TIMESTAMP, built_at TIMESTAMP)")
    con.executemany("INSERT INTO inv_ingest_log VALUES (?,?,?,?,?,?)",
                    [(l["file"], l["kind"], l["rows"], l["note"], anchor_ts, dt.datetime.now()) for l in log])

    n = con.execute("SELECT count(*) FROM fact_inventory_week").fetchone()[0]
    so = con.execute("SELECT count(*) FROM fact_inventory_week WHERE stockout_floor").fetchone()[0]
    unk = con.execute("SELECT count(*) FROM fact_inventory_week WHERE stockout_floor IS NULL").fetchone()[0]
    neg = con.execute("SELECT count(*) FROM fact_inventory_week WHERE total_end < -0.5").fetchone()[0]
    wk = con.execute("SELECT min(week_start), max(week_start) FROM fact_inventory_week").fetchone()
    con.close()
    print(f"\nfact_inventory_week: {n:,} product-weeks {wk[0]} -> {wk[1]}; "
          f"{so:,} floor stockout weeks, {unk:,} unknowable (opening-stock dip); "
          f"{neg:,} rows still negative (should be 0)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", type=Path, default=Path("inventory"))
    ap.add_argument("--db", type=Path, default=Path("tta.duckdb"))
    ap.add_argument("--since", type=dt.date.fromisoformat, default=dt.date(2026, 1, 1))
    a = ap.parse_args()
    build(a.folder, a.db, a.since)
