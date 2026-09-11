"""checks.py - post-build validation for Cerebral.
Run from Cerebral/ after a build:
    python checks.py --dash ../cerebral_dash.duckdb --db ../tta.duckdb
Exit code 1 on any FAIL (add --strict to fail on WARN too).
"""
import argparse, datetime as dt, sys
import duckdb

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
RESULTS = []

def report(name, status, detail=""):
    RESULTS.append((name, status, detail))
    print(f"  [{status:4}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)

def tables(con):
    return {r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema='main'").fetchall()}

def cols(con, t):
    return [r[0] for r in con.execute(
        "select column_name from information_schema.columns where table_name=?", [t]).fetchall()]

def q1(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]

def as_date(v):
    return v.date() if isinstance(v, dt.datetime) else v

def yw_ord(y, w): return y * 53 + w

# ---------------------------------------------------------------- structural
REQUIRED_DASH = [
    "dash_brand_week", "dash_brand_alias", "dash_inventory", "dash_acc_product_inv",
    "dash_events_cost", "dash_event_tracker", "dash_newret_week", "dash_loyalty_week",
    "dash_gwp_receipt", "dash_gwp_day",
    "dash_vm_brand_week", "dash_vm_stock_week", "dash_vm_takeover_xref", "dash_vm_placement_week",
]
VM_LAG_WEEKS_OK = 2   # floor sets are logged by hand; allow a little lag before warning

def check_tables_present(dash):
    have = tables(dash)
    print(f"  ({len([t for t in have if t.startswith('dash_')])} dash_* tables found)")
    for t in REQUIRED_DASH:
        if t not in have:
            report(f"table present: {t}", FAIL, "missing - a publish step dropped or skipped it"); continue
        n = q1(dash, f'select count(*) from "{t}"')
        report(f"table present: {t}", PASS if n > 0 else FAIL, f"{n:,} rows")

def latest_yw(con, t):
    return con.execute(f'select iso_year, iso_week from "{t}" order by iso_year desc, iso_week desc limit 1').fetchone()

def check_weeks_aligned(dash):
    have = tables(dash)
    ry, rw = latest_yw(dash, "dash_brand_week")
    print(f"  (reference latest week from dash_brand_week = {ry}-W{rw:02d})")
    for t in sorted(have):
        if not t.startswith("dash_") or t == "dash_brand_week": continue
        cs = cols(dash, t)
        if "iso_year" not in cs or "iso_week" not in cs: continue
        y, w = latest_yw(dash, t)
        lag = yw_ord(ry, rw) - yw_ord(y, w)
        if lag == 0: report(f"latest week {t}", PASS, f"{y}-W{w:02d}")
        elif t.startswith("dash_vm_") and lag <= VM_LAG_WEEKS_OK: report(f"latest week {t}", PASS, f"{y}-W{w:02d} ({lag}w behind, within VM tolerance)")
        elif lag < 0: report(f"latest week {t}", FAIL, f"{y}-W{w:02d} is AHEAD of sales")
        else: report(f"latest week {t}", WARN, f"{y}-W{w:02d} - {lag} weeks behind sales (re-run vm_ingest?)")

def check_no_dupes(con, t, keys):
    if t not in tables(con): report(f"unique keys {t}", SKIP, "table missing"); return
    missing = [k for k in keys if k not in cols(con, t)]
    if missing: report(f"unique keys {t}", SKIP, f"no column {missing}"); return
    kl = ", ".join(f'"{k}"' for k in keys)
    d = q1(con, f'select count(*) from (select {kl}, count(*) c from "{t}" group by {kl} having c>1)')
    report(f"unique keys {t}({','.join(keys)})", PASS if d == 0 else FAIL, f"{d} duplicate key groups")

# ------------------------------------------------------------- source (tta.duckdb)
def last_sale_date(db):
    return as_date(q1(db, "select max(txn_ts) from fact_line"))

def check_fact_line_basics(db):
    nulls = q1(db, "select count(*) from fact_line where store_key is null")
    report("fact_line: no NULL store_key", PASS if nulls == 0 else FAIL, f"{nulls:,} null rows")
    bad = q1(db, "select count(*) from fact_line where store_key not in (1,2,3,4)")
    report("fact_line: store_key in {1,2,3,4}", PASS if bad == 0 else FAIL, f"{bad:,} rows outside")
    nb = q1(db, """select count(*) from fact_line
                   where (brand is null or trim(brand)='') and category not in ('Accessory')
                     and product not ilike '%sample%' and net_sales > 0""")
    report("fact_line: cannabis lines have a brand", PASS if nb == 0 else WARN,
           f"{nb:,} paid non-accessory lines with blank brand (missing from every brand table)")
    ly, lw = latest_yw(db, "fact_line")
    n = q1(db, "select count(*) from fact_line where iso_year=? and iso_week=?", [ly, lw])
    report(f"fact_line: latest week {ly}-W{lw:02d} populated", PASS if n > 1000 else WARN, f"{n:,} lines")
    last = last_sale_date(db); age = (dt.date.today() - last).days
    report("fact_line: sales data current (<= 8 days old)", PASS if age <= 8 else FAIL,
           f"last sale {last}, {age}d ago - Monday drop missing or unprocessed?")
    report("fact_line: latest week complete (last sale on a Sunday)", PASS if last.weekday() == 6 else WARN,
           f"last sale is a {last:%A}; {ly}-W{lw:02d} is a partial week on every weekly chart")

def check_inventory_fresh(db):
    last = last_sale_date(db)
    nxt = last + dt.timedelta(days=1)
    expected = nxt - dt.timedelta(days=nxt.weekday())          # Monday of the week after the last sale
    real = as_date(q1(db, "select max(snapshot_date) from fact_inventory where source='dutchie_export'"))
    newest = as_date(q1(db, "select max(snapshot_date) from fact_inventory"))
    report("fact_inventory: Dutchie export covers the latest sales week",
           PASS if real and real >= expected else FAIL,
           f"newest export {real}, expected >= {expected} (last sale {last}) - Monday drop missing Current Inventory?")
    report("fact_inventory: newest snapshot of any kind", PASS if (last - newest).days <= 7 else WARN,
           f"{newest} ({(last - newest).days}d before last sale)")
    neg = q1(db, "select count(*) from fact_inventory where qty_on_hand < 0")
    report("fact_inventory: no negative qty_on_hand", PASS if neg == 0 else FAIL, f"{neg:,} rows")
    if "fact_inventory_week" in tables(db):
        qc = [c for c in cols(db, "fact_inventory_week") if "qty" in c.lower() or "floor" in c.lower()]
        if qc:
            n = q1(db, "select " + " + ".join(f'(select count(*) from fact_inventory_week where "{c}" < 0)' for c in qc))
            report("fact_inventory_week: no negative stock", PASS if n == 0 else FAIL, f"{n:,} rows across {qc}")

def check_dim_event(db):
    check_no_dupes(db, "dim_event", ["event_id"])
    z = q1(db, "select count(*) from dim_event where cost_recorded = false and net_tta_cost is not null")
    report("dim_event: unrecorded cost stays NULL (never $0)", PASS if z == 0 else FAIL,
           f"{z} rows with cost_recorded=false but a net_tta_cost value")
    z = q1(db, "select count(*) from dim_event where cost_recorded = true and net_tta_cost is null")
    report("dim_event: recorded cost has a value", PASS if z == 0 else FAIL, f"{z} rows")
    over = q1(db, "select count(*) from dim_event where net_tta_cost > gross_cost + 0.01")
    report("dim_event: net_tta_cost <= gross_cost", PASS if over == 0 else FAIL, f"{over} rows")
    d = q1(db, "select count(*) from dim_event where abs(coalesce(realloc_delta,0)) > 0.01")
    big = q1(db, "select count(*) from dim_event where abs(coalesce(realloc_delta,0)) > 1000")
    report("dim_event: shared-cost re-split vs Airtable export", PASS if d == 0 else WARN,
           f"{d} rows differ from the export ({big} by more than $1,000)")
    ns = q1(db, """select count(*) from (select airtable_record_id, n_stores,
                   count(*) filter (where store_key > 0) c from dim_event group by 1,2 having c > n_stores)""")
    report("dim_event: per-store rows <= n_stores (store 0 = all-stores row)", PASS if ns == 0 else FAIL, f"{ns} events over")
    z = q1(db, "select count(*) from (select airtable_record_id from dim_event group by 1 having count(*) filter (where store_key = 0) > 1)")
    report("dim_event: at most one all-stores row per event", PASS if z == 0 else FAIL, f"{z} events")

# ---------------------------------------------------------- reconciliation
def check_brand_week_vs_fact(dash, db):
    y, w = latest_yw(dash, "dash_brand_week")
    d_tot = q1(dash, "select sum(net) from dash_brand_week where iso_year=? and iso_week=?", [y, w]) or 0
    f_tot = q1(db, "select sum(net_sales) from fact_line where iso_year=? and iso_week=?", [y, w]) or 0
    diff = abs(d_tot - f_tot) / f_tot if f_tot else 1
    report(f"dash_brand_week net = fact_line net_sales ({y}-W{w:02d})", PASS if diff <= 0.005 else FAIL,
           f"dash ${d_tot:,.0f} vs fact ${f_tot:,.0f} ({diff:.2%})")
    for sk in (1, 2, 3, 4):
        d = q1(dash, "select sum(net) from dash_brand_week where iso_year=? and iso_week=? and store_key=?", [y, w, sk]) or 0
        f = q1(db, "select sum(net_sales) from fact_line where iso_year=? and iso_week=? and store_key=?", [y, w, sk]) or 0
        if f == 0 and d == 0: continue
        dd = abs(d - f) / f if f else 1
        report(f"  store {sk} reconciles", PASS if dd <= 0.005 else FAIL, f"${d:,.0f} vs ${f:,.0f}")

def check_dash_inventory_current(dash, db):
    snap = as_date(q1(dash, "select max(snapshot_date) from dash_inventory"))
    real = as_date(q1(db, "select max(snapshot_date) from fact_inventory where source='dutchie_export'"))
    report("dash_inventory: built from newest Dutchie export", PASS if snap == real else FAIL,
           f"dash_inventory {snap} vs newest export {real}")
    d_tot = q1(dash, "select sum(qoh) from dash_inventory") or 0
    fcols = cols(db, "fact_inventory")
    where = "snapshot_date = ?" + (" and sellable" if "sellable" in fcols else "")
    f_tot = q1(db, f"select sum(qty_on_hand) from fact_inventory where {where}", [snap]) or 0
    diff = abs(d_tot - f_tot) / f_tot if f_tot else 1
    report("dash_inventory qoh = fact_inventory (same snapshot)", PASS if diff <= 0.02 else WARN,
           f"dash {d_tot:,.0f} vs fact {f_tot:,.0f} ({diff:.1%})")

# ----------------------------------------------------- business invariants
ALIAS_JOIN = "join dash_brand_alias a on w.brand = a.alias and a.alias <> a.canonical"

def check_alias_applied(dash):
    for t, sev in (("dash_brand_week", FAIL), ("dash_vm_brand_week", FAIL), ("dash_gwp_day", FAIL), ("dash_gwp_receipt", WARN)):
        if t not in tables(dash): continue
        leak = q1(dash, f"select count(*) from {t} w {ALIAS_JOIN}")
        report(f"brand aliases applied in {t}", PASS if leak == 0 else sev, f"{leak} rows still on a raw alias (e.g. 'Ruby Farms')")

def check_gwp(dash, db=None):
    # received (dash_gwp_receipt) vs given out (dash_gwp_day), brand canonicalised on both sides
    rows = dash.execute("""
        with r as (select coalesce(a.canonical, r.brand) brand, r.store_key, min(day) first_receipt, sum(units_received) recv
                   from dash_gwp_receipt r left join dash_brand_alias a on r.brand = a.alias group by 1,2),
             o as (select coalesce(a.canonical, o.brand) brand, o.store_key, sum(units) out_, max(day) last_out
                   from dash_gwp_day o left join dash_brand_alias a on o.brand = a.alias group by 1,2)
        select r.brand, r.store_key, r.first_receipt, r.recv, coalesce(o.out_, 0), o.last_out
        from r left join o using (brand, store_key) order by 1,2""").fetchall()
    if not rows: report("gwp: receipts present", WARN, "dash_gwp_receipt is empty"); return
    last = last_sale_date(db) if db else dt.date.today()
    for brand, sk, first, recv, out, last_out in rows:
        first = as_date(first)
        if out > recv:
            report(f"gwp: {brand} store {sk} units out <= received", WARN, f"out {out:.0f} > received {recv:.0f} - receipt log incomplete?")
        elif out == 0 and (last - first).days >= 7:
            report(f"gwp: {brand} store {sk} free units recorded", FAIL, f"received {recv:.0f} on {first}, nothing in dash_gwp_day - GWP lines not being captured")
        else:
            report(f"gwp: {brand} store {sk} out <= received", PASS, f"{out:.0f} / {recv:.0f} ({out/recv:.0%})")

def check_event_tracker(dash):
    t = "dash_event_tracker"
    check_no_dupes(dash, t, ["airtable_record_id", "bucket"])
    z = q1(dash, f"select count(*) from {t} where signups <> signups_matchable + signups_unmatchable")
    report("event tracker: signups = matchable + unmatchable", PASS if z == 0 else FAIL, f"{z} rows")
    for d in ("d0", "d30", "d90"):
        z = q1(dash, f"select count(*) from {t} where buyers_{d} > signups_matchable")
        report(f"event tracker: buyers_{d} <= matchable", PASS if z == 0 else FAIL, f"{z} rows")
    z = q1(dash, f"select count(*) from {t} where buyers_d0 > buyers_d30 or buyers_d30 > buyers_d90")
    report("event tracker: buyers monotonic d0 <= d30 <= d90", PASS if z == 0 else FAIL, f"{z} rows")
    z = q1(dash, f"select count(*) from {t} where (mature_d90 and not mature_d30) or (mature_d30 and not mature_d0)")
    report("event tracker: maturity flags consistent", PASS if z == 0 else FAIL, f"{z} rows")
    z = q1(dash, f"select count(*) from {t} where cost_recorded = false and net_tta_cost is not null")
    report("event tracker: unrecorded cost is NULL not $0", PASS if z == 0 else FAIL, f"{z} rows")

def check_takeover_calendar(dash):
    t = "dash_vm_takeover_xref"
    rows = dash.execute(f"""select takeover, max(covered_days) from {t}
                            where iso_year=2026 and iso_week=20 and in_window group by 1""").fetchall()
    m = {r[0]: r[1] for r in rows}
    ok = m.get("Timeless") == 4 and m.get("Woodstock", 0) <= 3
    report("takeover: W20 split Woodstock Mon-Wed / Timeless from May 14", PASS if ok else FAIL, str(m))
    z = q1(dash, f"select count(*) from {t} where covered_days < 0 or covered_days > 7")
    report("takeover: covered_days within 0-7", PASS if z == 0 else FAIL, f"{z} rows")
    z = q1(dash, f"select count(*) from {t} where in_window and phase not in ('before','during','after')")
    report("takeover: phase values valid", PASS if z == 0 else WARN, f"{z} rows with unexpected phase")

def check_stock_week(dash):
    t = "dash_vm_stock_week"
    tot = q1(dash, f"select count(*) from {t}") or 1
    unk = q1(dash, f"select count(*) from {t} where products > 0 and products_unknown = products")
    report("stock-week: fully-unknowable brand-weeks < 10%", PASS if unk / tot < 0.10 else WARN, f"{unk/tot:.1%}")
    z = q1(dash, f"select count(*) from {t} where floor_start < 0 or floor_end < 0 or total_end < 0")
    report("stock-week: no negative stock", PASS if z == 0 else FAIL, f"{z} rows")
    z = q1(dash, f"select count(*) from {t} where floor_sell_through < 0 or floor_sell_through > 1.5")
    report("stock-week: floor_sell_through sane (0-150%)", PASS if z == 0 else WARN, f"{z} rows")
    # publish_vm.py: brand_stockout = floor empty at week end, OR every product that was on the
    # floor provably ran dry (so products_stocked_out > 0). Anything else is a contradiction.
    z = q1(dash, f"select count(*) from {t} where brand_stockout and floor_end > 0 and products_stocked_out = 0")
    report("stock-week: brand_stockout has a cause (empty floor or a stocked-out product)", PASS if z == 0 else FAIL, f"{z} rows")
    z = q1(dash, f"select count(*) from {t} where brand_stockout and floor_end > 0")
    report("stock-week: stocked-out brands with floor stock at week end", PASS if z == 0 else WARN,
           f"{z} rows - stock ended on the floor for a product that never started there or was moved to floor (ledger gap?)")
    z = q1(dash, f"select count(*) from {t} where products_stocked_out > products or products_unknown > products or products_on_floor_end > products")
    report("stock-week: product counters <= products", PASS if z == 0 else FAIL, f"{z} rows")

# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dash", default="cerebral_dash.duckdb")
    ap.add_argument("--db", default=None, help="tta.duckdb (optional; enables source checks)")
    ap.add_argument("--strict", action="store_true", help="WARN counts as failure")
    a = ap.parse_args()
    try: dash = duckdb.connect(a.dash, read_only=True)
    except Exception as e: print(f"cannot open {a.dash}: {e}"); sys.exit(2)
    db = None
    if a.db:
        try: db = duckdb.connect(a.db, read_only=True)
        except Exception as e: print(f"cannot open {a.db} (is Streamlit holding it?): {e}")

    def run(label, fn, *args):
        try: fn(*args)
        except Exception as e: report(label, FAIL, f"check crashed: {e}")

    print(f"== Cerebral checks  dash={a.dash}  db={a.db or '-'}  {dt.datetime.now():%Y-%m-%d %H:%M}")
    print("-- structural")
    run("tables", check_tables_present, dash); run("weeks", check_weeks_aligned, dash)
    run("dupes brand_week", check_no_dupes, dash, "dash_brand_week", ["store_key", "iso_year", "iso_week", "brand", "category"])
    run("dupes events_cost", check_no_dupes, dash, "dash_events_cost", ["airtable_record_id"])
    run("dupes vm_brand_week", check_no_dupes, dash, "dash_vm_brand_week", ["brand", "store_key", "iso_year", "iso_week"])
    if db:
        print("-- source")
        run("fact_line", check_fact_line_basics, db); run("inventory", check_inventory_fresh, db); run("dim_event", check_dim_event, db)
        print("-- reconciliation")
        run("brand_week", check_brand_week_vs_fact, dash, db); run("dash_inventory", check_dash_inventory_current, dash, db)
    print("-- invariants")
    run("alias", check_alias_applied, dash); run("gwp", check_gwp, dash, db)
    run("event tracker", check_event_tracker, dash)
    run("takeover", check_takeover_calendar, dash); run("stock week", check_stock_week, dash)

    counts = {s: sum(1 for _, st, _ in RESULTS if st == s) for s in (PASS, FAIL, WARN, SKIP)}
    print(f"== {counts[PASS]} pass, {counts[FAIL]} fail, {counts[WARN]} warn, {counts[SKIP]} skip")
    for n, s, d in RESULTS:
        if s == FAIL: print(f"   FAIL {n} -- {d}")
    sys.exit(1 if counts[FAIL] + (counts[WARN] if a.strict else 0) else 0)

if __name__ == "__main__":
    main()
