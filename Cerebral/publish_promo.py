"""
publish_promo.py -- build the Promo Lab tables (dash_promo_*) in cerebral_dash.duckdb.

Runs from Cerebral/ AFTER publish.py (and publish_vm.py) have built the dash
file. It ATTACHes the source DB read-only as `src`, reads fact_line and
fact_basket, and writes aggregate-only dash_promo_* tables. No customer_key,
basket_id, name, phone or any other identifier is written -- a leak check on
the column names runs at the end and raises if one slips through.

    python publish_promo.py --src ../tta.duckdb --dash cerebral_dash.duckdb
    python publish_promo.py --check            # row counts of the promo tables
    python publish_promo.py --upload           # build, then push the dash file to Drive
                                               # (same Drive helper publish_vm.py uses)

The promo calendar lives in promos.py (PROMOS). There are no discount names or
promo codes in the data, so every promo is detected from basket composition:
    qualifying = the basket looks like the offer
    applied    = the price evidence says the discount was actually given
    near-miss  = one step short of qualifying
The per-mechanic rules are in dev/PROMO_LAB_SPEC.md section 1 and in
promos.MECHANICS; the SQL that applies them is in _mechanic_sql() below.

Tables written (column names are the contract with the app -- see the spec):
    dash_promo_def              one row per promo in PROMOS
    dash_promo_day              promo x store x day, from start-28d to min(end, last sales day)
    dash_promo_baseline         promo x store: per-day rates and base unit price for the clean days before
                                (28 days; sibling-offer days excluded, window grown back when needed; base_note says)
    dash_promo_channel          promo x store x channel, in-window
    dash_promo_attach           promo x store x category: what else is in qualifying baskets
    dash_promo_customer         promo x store x segment (new / returning / unknown / tier:<tier>)
    dash_promo_hour             promo x store x day_of_week x hour
    dash_promo_spotlight_item   spotlight promos: item x store x day
    dash_promo_gross_band       designer: gross-basket histogram per store-week-channel ($5 bands)
    dash_promo_qty_dist         designer: units-of-a-brand-in-basket distribution
    dash_promo_brand_price      designer: brand x category x store x week price/volume

Conventions used throughout
  * "matched" lines = brand pattern (minus excludes) AND category AND product
    regex all match, units > 0, not a return.
  * matched_units / matched_net / matched_gm in dash_promo_day and
    dash_promo_channel count ALL matched lines in the store that day/channel
    (brand volume), not just those in qualifying baskets -- that is what makes
    the pre-window baseline comparable. In dash_promo_customer, matched_net is
    inside the segment's qualifying baskets (segments only exist for those).
  * the "before" window is the 28 days before start, minus any day inside
    another promo on an overlapping brand pattern and the same category (the
    three Wyld weeks run back to back). If that leaves fewer than 7 clean days
    the window grows further back, up to 90 days before start, until 14 clean
    days are found or the data runs out. Sibling days are dropped from
    dash_promo_day's in_window=false rows too, so the run-up bars, the dashed
    baseline and dash_promo_baseline describe the same days; base_note says
    what was used ("28 clean days", "sibling offers excluded; window extended
    back to 2026-09-01; 14 days used", ...).
  * base_unit_price = median unit price (net/units, > $0) of matched lines per
    store over those clean days; falls back to chain-wide, then to the
    in-window median from NON-qualifying baskets (store, then chain).
  * gross basket = basket_net + ABS(discount_amt).
  * est_discount = sum over applied baskets of max(0, base_unit_price - actual
    unit price) x matched units (threshold_addon: base - add-on price, per
    add-on unit). gwp and ghost_sku write 0.

A REAL-DATA CAVEAT THAT SHAPES "APPLIED" (read tta_etl.py around line 696)
  fact_line.net_sales is Qty x the product's CHAIN-WIDE AVERAGE net price over
  the load period (the Detailed Sales Breakdown has no per-receipt prices). So
  within one load every line of a product carries the same unit price, and a
  per-basket price test is all-or-nothing per product: a 3-for-$60 bundle that
  was rung on 60% of the units makes every basket look "not applied" (average
  above $21), while 90% makes every 3-unit basket look applied. The fixture
  has true per-basket prices, so it cannot show this.

  fact_basket.discount_amt IS per receipt (straight from the POS export). So
  when BASKET_DISCOUNT_EVIDENCE is True a basket also counts as applied when
  its till discount covers at least DISC_SHARE of the markdown the offer
  implies at the base price (one bundle / one free unit / pct x units), and
  est_discount for those baskets is min(discount_amt, implied markdown). For
  threshold_addon the $10 line is itself the qualifying evidence, so the
  discount route also feeds qualifying (share THR_DISC_SHARE, stricter), and
  the "rest of the basket" is basket_net minus the add-on line plus whatever
  discount is left after the add-on markdown. Set the flag False to go back to
  price evidence only. Any basket-level discount (loyalty, staff) can trip the
  discount route, so "applied" stays an estimate -- the captions say so.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import promos  # noqa: E402

SRC_DB = Path("..") / "tta.duckdb"
DASH_DB = Path("cerebral_dash.duckdb")

TABLES = [
    "dash_promo_def", "dash_promo_day", "dash_promo_baseline", "dash_promo_channel",
    "dash_promo_attach", "dash_promo_customer", "dash_promo_hour",
    "dash_promo_spotlight_item", "dash_promo_gross_band", "dash_promo_qty_dist",
    "dash_promo_brand_price",
]
DESIGNER_WEEKS = 13          # complete ISO weeks (the current week only when the data ends on its Sunday)
QTY_DIST_MIN_BASKETS = 200   # brands below this are left out of dash_promo_qty_dist
BASKET_DISCOUNT_EVIDENCE = True   # see the module docstring: discount_amt as a second route to "applied"
DISC_SHARE = 0.5             # till discount must cover this share of the implied markdown (applied)
THR_DISC_SHARE = 0.75        # ... and this share for threshold_addon, where it also decides qualifying

# Columns that must never appear in a published table. `name` in dash_promo_def
# is the promo's display name (from promos.py), so it is allow-listed below.
FORBIDDEN_COL = re.compile(r"(customer_key|basket_id|phone|email|address|patient|alpine_id|"
                           r"display_name|first_name|last_name|name_hash|^name$)", re.I)
ALLOWED_NAME_COLS = {("dash_promo_def", "name")}


# ---------------------------------------------------------------------------
# small SQL helpers
# ---------------------------------------------------------------------------
def _q(s: str) -> str:
    """SQL string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _brand_pred(patterns, excludes, col="brand") -> str:
    low = f"lower(coalesce({col}, ''))"
    inc = " OR ".join(f"contains({low}, {_q(p.lower())})" for p in (patterns or [])) or "TRUE"
    exc = " OR ".join(f"contains({low}, {_q(p.lower())})" for p in (excludes or []))
    return f"(({inc}) AND NOT ({exc}))" if exc else f"({inc})"


def _line_pred(brand_patterns, excludes, category, product_regex,
               bcol="brand", ccol="category", pcol="product") -> str:
    parts = []
    if brand_patterns or excludes:
        parts.append(_brand_pred(brand_patterns, excludes, bcol))
    if category:
        parts.append(f"{ccol} = {_q(category)}")
    if product_regex:
        parts.append(f"regexp_matches(coalesce({pcol}, ''), {_q(product_regex)}, 'i')")
    return "(" + " AND ".join(parts) + ")" if parts else "TRUE"


def _dk(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _canon_brand_sql(col: str) -> str:
    """Brand canonicalisation using publish.py's alias map (falls back to raw)."""
    try:
        import publish  # heavy import chain, but no side effects
        return publish._canon_brand_sql(col)
    except Exception:
        return col


def _has_table(con, name: str, schema="src") -> bool:
    return con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = ?",
                       [schema, name]).fetchone()[0] > 0


# ---------------------------------------------------------------------------
# output schemas (exact column names from the spec)
# ---------------------------------------------------------------------------
SCHEMAS = {
    "dash_promo_def": """
        promo_id VARCHAR, name VARCHAR, mechanic VARCHAR, brand_label VARCHAR, category VARCHAR,
        start DATE, "end" DATE, takeover VARCHAR, program VARCHAR, notes VARCHAR, params_json VARCHAR,
        stores VARCHAR, status VARCHAR, last_day DATE, built_at TIMESTAMP""",
    "dash_promo_day": """
        promo_id VARCHAR, store_key INTEGER, day DATE, in_window BOOLEAN,
        store_baskets BIGINT, store_net DOUBLE,
        qualifying_baskets BIGINT, applied_baskets BIGINT,
        matched_units DOUBLE, matched_net DOUBLE, matched_gm DOUBLE,
        qual_basket_net DOUBLE, qual_basket_gross DOUBLE, est_discount DOUBLE,
        new_customers BIGINT, known_customers BIGINT, nearmiss_baskets BIGINT""",
    "dash_promo_baseline": """
        promo_id VARCHAR, store_key INTEGER, base_days INTEGER,
        base_qualifying_pd DOUBLE, base_matched_units_pd DOUBLE, base_matched_net_pd DOUBLE,
        base_store_baskets_pd DOUBLE, base_unit_price DOUBLE, base_gm_pct DOUBLE, base_qual_share DOUBLE,
        base_note VARCHAR""",
    "dash_promo_channel": """
        promo_id VARCHAR, store_key INTEGER, channel VARCHAR,
        qualifying_baskets BIGINT, matched_units DOUBLE, matched_net DOUBLE, store_baskets BIGINT""",
    "dash_promo_attach": """
        promo_id VARCHAR, store_key INTEGER, category VARCHAR,
        qual_baskets_with_cat BIGINT, qual_cat_net DOUBLE, other_baskets_with_cat BIGINT,
        other_baskets BIGINT, qual_baskets BIGINT""",
    "dash_promo_customer": """
        promo_id VARCHAR, store_key INTEGER, segment VARCHAR,
        qualifying_baskets BIGINT, customers BIGINT, matched_net DOUBLE, qual_basket_net DOUBLE,
        repeat_baskets BIGINT""",
    "dash_promo_hour": """
        promo_id VARCHAR, store_key INTEGER, day_of_week INTEGER, hour INTEGER,
        qualifying_baskets BIGINT, store_baskets BIGINT""",
    "dash_promo_spotlight_item": """
        promo_id VARCHAR, store_key INTEGER, item_label VARCHAR, day DATE, in_window BOOLEAN,
        units DOUBLE, net DOUBLE, baskets BIGINT, avg_unit_price DOUBLE,
        base_units_pd DOUBLE, base_unit_price DOUBLE""",
    "dash_promo_gross_band": """
        store_key INTEGER, iso_year INTEGER, iso_week INTEGER, channel VARCHAR, band_lo INTEGER,
        baskets BIGINT, net DOUBLE, discount DOUBLE""",
    "dash_promo_qty_dist": """
        store_key INTEGER, brand VARCHAR, category VARCHAR, units_in_basket INTEGER,
        baskets BIGINT, units DOUBLE, net DOUBLE, avg_unit_price DOUBLE""",
    "dash_promo_brand_price": """
        store_key INTEGER, brand VARCHAR, category VARCHAR, iso_year INTEGER, iso_week INTEGER,
        units DOUBLE, net DOUBLE, gm DOUBLE, median_unit_price DOUBLE, baskets BIGINT""",
}


def _create_outputs(con):
    for t, cols in SCHEMAS.items():
        con.execute(f"CREATE OR REPLACE TABLE {t} ({cols})")


# ---------------------------------------------------------------------------
# per-mechanic SQL: expressions evaluated against the per-basket temp table pb
# (columns: m_units, m_net, m_gm, m_min_px, zero_line, addon_units, addon_net,
#  top_units, top_net, gross, basket_net, disc, item_applied, item_disc) and
#  base_px.base.
#   qual0   provisional qualifying, must not use `base` (it feeds the in-window
#           fallback for the base price); for most mechanics qual0 == qual
#   qual    final qualifying (base available)
#   near    near-miss, evaluated only where NOT qualifying
#   applied price evidence OR (when BASKET_DISCOUNT_EVIDENCE) till-discount evidence
#   est     estimated discount given on an applied basket
# ---------------------------------------------------------------------------
def _mechanic_sql(p: dict) -> dict:
    m, pr = p["mechanic"], p.get("params") or {}
    avg = "(m_net / nullif(m_units, 0))"
    D = BASKET_DISCOUNT_EVIDENCE
    if m == "bundle_price":
        qty, bp = float(pr["qty"]), float(pr["bundle_price"])
        md1 = f"greatest(0, base * {qty} - {bp})"            # markdown on one bundle at the base price
        bundles = f"greatest(1, floor(m_units / {qty}))"
        price_ok = f"({avg} <= {bp / qty} * 1.05)"
        disc_ok = f"({md1} >= 1 AND disc >= {DISC_SHARE} * {md1})" if D else "FALSE"
        qual = f"m_units >= {qty}"
        return dict(
            qual0=qual, qual=qual,
            near=f"round(m_units) = {qty - 1} AND m_units < {qty}",
            applied=f"qualifying AND ({price_ok} OR {disc_ok})",
            est=f"CASE WHEN NOT applied THEN 0 WHEN {price_ok} THEN greatest(0, base - {avg}) * m_units "
                f"ELSE least(disc, {md1} * {bundles}) END")
    if m == "threshold_addon":
        thr, ap = float(pr["threshold"]), float(pr["addon_price"])
        ref = "coalesce(base, m_min_px)"                     # regular price of the add-on item
        px_ok = "coalesce(addon_units, 0) >= 1"              # a matched line priced near the add-on price
        disc_ok = (f"(m_units >= 1 AND {ref} - {ap} >= 2 AND disc >= {THR_DISC_SHARE} * ({ref} - {ap}))"
                   if D else "FALSE")
        addon_ok = f"({px_ok} OR {disc_ok})"
        a_units = f"(CASE WHEN {px_ok} THEN addon_units ELSE 1 END)"
        a_net = f"(CASE WHEN {px_ok} THEN addon_net ELSE m_min_px END)"
        markdown = f"(greatest(0, {ref} - {ap}) * {a_units})"
        # the rest of the basket before discounts: take the add-on line out of the
        # net, then add back any discount that is not the add-on's own markdown
        rest = f"(basket_net - coalesce({a_net}, 0) + greatest(0, disc - {markdown}))"
        return dict(
            qual0=f"(gross - coalesce(addon_net, 0)) >= {thr} AND {px_ok}",
            qual=f"{addon_ok} AND {rest} >= {thr}",
            near=f"NOT {addon_ok} AND gross >= {thr - 15} AND gross < {thr}",
            applied="qualifying",
            est=f"CASE WHEN NOT applied THEN 0 WHEN {px_ok} THEN "
                f"greatest(0, base - addon_net / nullif(addon_units, 0)) * addon_units "
                f"ELSE least(disc, {markdown}) END")
    if m == "bogo":
        buy, get = float(pr["buy"]), float(pr["get"])
        md1 = f"(base * {get})"                               # the free unit(s) in one bundle
        bundles = f"greatest(1, floor(m_units / {buy + get}))"
        price_ok = f"(zero_line OR {avg} <= base * {buy / (buy + get)} * 1.08)"
        disc_ok = f"(base >= 1 AND disc >= {DISC_SHARE} * {md1})" if D else "FALSE"
        qual = f"m_units >= {buy + get}"
        return dict(
            qual0=qual, qual=qual,
            near=f"round(m_units) = {buy}",
            applied=f"qualifying AND ({price_ok} OR {disc_ok})",
            est=f"CASE WHEN NOT applied THEN 0 WHEN {price_ok} THEN greatest(0, base - {avg}) * m_units "
                f"ELSE least(disc, {md1} * {bundles}) END")
    if m == "multi_pct_off":
        qty, pct = float(pr["qty"]), float(pr["pct"])
        single = pr.get("single_brand", True)
        u, n = ("top_units", "top_net") if single else ("m_units", "m_net")
        a = f"({n} / nullif({u}, 0))"
        md = f"(base * {pct / 100.0} * {u})"
        price_ok = f"({a} <= base * {1 - pct / 100.0} * 1.08)"
        disc_ok = f"({md} >= 1 AND disc >= {DISC_SHARE} * {md})" if D else "FALSE"
        qual = f"{u} >= {qty}"
        return dict(
            qual0=qual, qual=qual,
            near=f"round({u}) = {qty - 1}",
            applied=f"qualifying AND ({price_ok} OR {disc_ok})",
            est=f"CASE WHEN NOT applied THEN 0 WHEN {price_ok} THEN greatest(0, base - {a}) * {u} "
                f"ELSE least(disc, {md}) END")
    if m == "spotlight":
        return dict(qual0="m_units > 0", qual="m_units > 0", near="FALSE",
                    applied="qualifying AND coalesce(item_applied, FALSE)",
                    est="CASE WHEN applied THEN coalesce(item_disc, 0) ELSE 0 END")
    if m == "gwp":
        return dict(qual0="m_units > 0", qual="m_units > 0", near="FALSE", applied="NULL", est="0")
    if m == "ghost_sku":
        return dict(qual0="m_units > 0", qual="m_units > 0", near="FALSE", applied="qualifying", est="0")
    raise ValueError(f"unknown mechanic {m}")


BASE_DAYS = 28          # the normal "before" window
BASE_MIN_CLEAN = 7      # fewer clean days than this -> look further back ...
BASE_TARGET_CLEAN = 14  # ... until this many clean days are found ...
BASE_MAX_BACK = 90      # ... or the window would reach this far back


def _siblings(p: dict) -> list[tuple[dt.date, dt.date]]:
    """Windows of OTHER promos on an overlapping brand pattern and the same category.
    Their days are not a fair "before" for this promo (the three Wyld weeks run
    back to back), so they are left out of the baseline."""
    mine = {b.lower() for b in (p.get("brand_patterns") or [])}
    out = []
    for o in promos.PROMOS:
        if o["id"] == p["id"] or not mine:
            continue
        theirs = {b.lower() for b in (o.get("brand_patterns") or [])}
        if not (mine & theirs) or (o.get("category") or "") != (p.get("category") or ""):
            continue
        out.append((promos._d(o["start"]), promos._d(o["end"])))
    return out


def _is_sibling_day(d: dt.date, sibs) -> bool:
    return any(a <= d <= b for a, b in sibs)


def _baseline_plan(p: dict, start: dt.date, first_day: dt.date) -> tuple[dt.date, list, str]:
    """Where the "before" window starts, the sibling windows to leave out, and a
    plain-English note for dash_promo_baseline.base_note.

    Normal: the 28 days before start. If sibling offers leave fewer than 7 clean
    days in them, the window grows further back (one day at a time, at most 90
    days before start) until 14 clean days are found or the data runs out."""
    sibs = _siblings(p)
    lo = start - dt.timedelta(days=BASE_DAYS)

    def clean(lo_):
        d, n = max(lo_, first_day), 0
        while d < start:
            n += not _is_sibling_day(d, sibs); d += dt.timedelta(days=1)
        return n

    n = clean(lo)
    excluded = sum(1 for i in range(BASE_DAYS) if _is_sibling_day(start - dt.timedelta(days=i + 1), sibs))
    extended = False
    if excluded and n < BASE_MIN_CLEAN:
        floor = start - dt.timedelta(days=BASE_MAX_BACK)
        while n < BASE_TARGET_CLEAN and lo > floor and lo > first_day:
            lo -= dt.timedelta(days=1); n = clean(lo); extended = True
    lo = max(lo, first_day)
    if not excluded and lo == start - dt.timedelta(days=BASE_DAYS):
        note = f"{BASE_DAYS} clean days"
    else:
        bits = []
        if excluded:
            bits.append("sibling offers excluded")
        if extended:
            bits.append(f"window extended back to {lo}")
        if lo == first_day and lo > start - dt.timedelta(days=BASE_DAYS):
            bits.append(f"data starts {first_day}")
        note = "; ".join(bits) + f"; {n} days used"
    return lo, sibs, note


def _sibling_sql(sibs, col="day") -> str:
    """AND NOT (...) fragments dropping sibling-offer days from a query on `col`."""
    return " ".join(f"AND NOT ({col} BETWEEN DATE {_q(a)} AND DATE {_q(b)})" for a, b in sibs)


# ---------------------------------------------------------------------------
# one promo
# ---------------------------------------------------------------------------
def _build_one(con, p: dict, S: str, first_day: dt.date, last_day: dt.date, has_tier: bool) -> dict:
    pid = p["id"]
    start, end = promos._d(p["start"]), promos._d(p["end"])
    end_eff = min(end, last_day)
    # "before" window: normally start-28d, clipped to the data, grown further back
    # when sibling offers on the same brand eat the clean days (see _baseline_plan)
    range_lo, sibs, base_note = _baseline_plan(p, start, first_day)
    # rows before `start` on a sibling-offer day are dropped everywhere (lines,
    # baskets, day spine), so the run-up bars, the dashed baseline line and
    # dash_promo_baseline all describe the same clean days
    def keep_day(col: str) -> str:
        return f"({col} >= DATE {_q(start)} OR (TRUE {_sibling_sql(sibs, col)}))" if sibs else "TRUE"
    stores = promos.promo_stores(p)
    st_list = ",".join(str(s) for s in stores)
    pr = p.get("params") or {}
    mech = p["mechanic"]
    rules = _mechanic_sql(p)
    day_expr = "make_date(date_key // 10000, (date_key // 100) % 100, date_key % 100)"
    summary = dict(id=pid, qualifying=0, applied=None, nearmiss=0, est=0.0, base=None, base_src=None)

    # Nothing to see yet (window plus its 28-day run-up is in the future, or
    # the promo ended before the data starts).
    if range_lo > last_day:
        return summary

    # ---- matched lines ------------------------------------------------
    if mech == "spotlight":
        items = pr["items"]
        preds = [_line_pred(it.get("brand_patterns"), None, it.get("category"), it.get("product_regex"))
                 for it in items]
        match_pred = "(" + " OR ".join(preds) + ")"
        label_expr = "CASE " + " ".join(f"WHEN {pd_} THEN {_q(it['label'])}" for pd_, it in zip(preds, items)) + " END"
        addon_expr = "FALSE"
    else:
        match_pred = _line_pred(p.get("brand_patterns"), p.get("exclude_brand_patterns"),
                                p.get("category"), p.get("product_regex"))
        label_expr = "NULL::VARCHAR"
        if mech == "threshold_addon":
            ap = float(pr["addon_price"])
            addon_expr = f"(net_sales / units BETWEEN {0.7 * ap} AND {1.3 * ap})"
            if pr.get("addon_regex"):
                addon_expr += f" AND regexp_matches(coalesce(product, ''), {_q(pr['addon_regex'])}, 'i')"
        else:
            addon_expr = "FALSE"
    canon = _canon_brand_sql("brand")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ml_raw AS
        SELECT basket_id, store_key, {day_expr} AS day, units, net_sales, gross_margin,
               net_sales / units AS unit_price,
               lower({canon}) AS brand_group,
               {label_expr} AS item_label,
               {addon_expr} AS is_addon
        FROM {S}fact_line
        WHERE NOT is_return AND units > 0 AND store_key IN ({st_list})
          AND date_key BETWEEN {_dk(range_lo)} AND {_dk(end_eff)}
          AND {keep_day(day_expr)}
          AND {match_pred}""")

    # ---- every basket in range, with its matched aggregates -------------
    # (customer_key lives only in this TEMP table; it is never written out)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pb AS
        WITH agg AS (
            SELECT basket_id, store_key,
                   sum(units) AS m_units, sum(net_sales) AS m_net, sum(gross_margin) AS m_gm,
                   min(unit_price) FILTER (WHERE unit_price > 0) AS m_min_px,
                   bool_or(net_sales <= 0.05) AS zero_line,
                   sum(units) FILTER (WHERE is_addon) AS addon_units,
                   sum(net_sales) FILTER (WHERE is_addon) AS addon_net
            FROM ml_raw GROUP BY 1, 2),
        top AS (
            SELECT basket_id, store_key, brand_group AS top_brand,
                   sum(units) AS top_units, sum(net_sales) AS top_net
            FROM ml_raw GROUP BY 1, 2, 3
            QUALIFY row_number() OVER (PARTITION BY basket_id, store_key
                                       ORDER BY sum(units) DESC, sum(net_sales) DESC) = 1)
        SELECT b.basket_id, b.store_key, {day_expr} AS day,
               {day_expr} BETWEEN DATE {_q(start)} AND DATE {_q(end)} AS in_window,
               b.channel, b.day_of_week, hour(b.txn_ts) AS hr, b.txn_ts, b.customer_key,
               b.basket_net, b.basket_net + abs(coalesce(b.discount_amt, 0)) AS gross,
               abs(coalesce(b.discount_amt, 0)) AS disc,
               coalesce(a.m_units, 0) AS m_units, coalesce(a.m_net, 0) AS m_net, coalesce(a.m_gm, 0) AS m_gm,
               a.m_min_px, coalesce(a.zero_line, FALSE) AS zero_line, a.addon_units, a.addon_net,
               t.top_brand, coalesce(t.top_units, 0) AS top_units, coalesce(t.top_net, 0) AS top_net
        FROM {S}fact_basket b
        LEFT JOIN agg a ON a.basket_id = b.basket_id AND a.store_key = b.store_key
        LEFT JOIN top t ON t.basket_id = b.basket_id AND t.store_key = b.store_key
        WHERE NOT b.is_return AND b.store_key IN ({st_list})
          AND b.date_key BETWEEN {_dk(range_lo)} AND {_dk(end_eff)}
          AND {keep_day(day_expr)}""")
    # ml = matched lines whose basket exists on the basket side (same non-return rule)
    con.execute("CREATE OR REPLACE TEMP TABLE ml AS SELECT l.* FROM ml_raw l "
                "JOIN pb b ON b.basket_id = l.basket_id AND b.store_key = l.store_key")
    con.execute("DROP TABLE ml_raw")

    # provisional qualifying (no base price yet): it only decides which in-window
    # baskets count as "non-qualifying" for the base-price fallback below
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pb2 AS
        SELECT *, coalesce(({rules['qual0']}), FALSE) AS qualifying FROM pb""")

    # ---- base unit price per store (clean days before -> chain -> in-window non-qualifying) --
    # ml already holds clean days only (sibling-offer days before start are gone).
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE base_px AS
        WITH pre AS (SELECT store_key, median(unit_price) AS px FROM ml
                     WHERE day < DATE {_q(start)} AND unit_price > 0 GROUP BY 1),
             pre_chain AS (SELECT median(unit_price) AS px FROM ml
                           WHERE day < DATE {_q(start)} AND unit_price > 0),
             win AS (SELECT l.store_key, median(l.unit_price) AS px FROM ml l
                     JOIN pb2 b ON b.basket_id = l.basket_id AND b.store_key = l.store_key
                     WHERE b.in_window AND NOT b.qualifying AND l.unit_price > 0 GROUP BY 1),
             win_chain AS (SELECT median(l.unit_price) AS px FROM ml l
                     JOIN pb2 b ON b.basket_id = l.basket_id AND b.store_key = l.store_key
                     WHERE b.in_window AND NOT b.qualifying AND l.unit_price > 0)
        SELECT s.store_key,
               coalesce(pre.px, pre_chain.px, win.px, win_chain.px) AS base,
               CASE WHEN pre.px IS NOT NULL THEN 'store, clean days before'
                    WHEN pre_chain.px IS NOT NULL THEN 'chain, clean days before'
                    WHEN win.px IS NOT NULL THEN 'store, in-window non-qualifying'
                    WHEN win_chain.px IS NOT NULL THEN 'chain, in-window non-qualifying' END AS base_src
        FROM (SELECT unnest([{st_list}]) AS store_key) s
        LEFT JOIN pre USING (store_key) LEFT JOIN win USING (store_key)
        CROSS JOIN pre_chain CROSS JOIN win_chain""")

    # ---- spotlight: per-item evidence per basket --------------------------
    if mech == "spotlight":
        price_case = "CASE " + " ".join(f"WHEN l.item_label = {_q(it['label'])} THEN {float(it['price'])}"
                                        for it in pr["items"]) + " END"
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE item_base AS
            WITH pre AS (SELECT store_key, item_label, median(unit_price) AS px FROM ml
                         WHERE day < DATE {_q(start)} AND unit_price > 0 GROUP BY 1, 2),
                 pre_chain AS (SELECT item_label, median(unit_price) AS px FROM ml
                               WHERE day < DATE {_q(start)} AND unit_price > 0 GROUP BY 1),
                 win AS (SELECT l.store_key, l.item_label, median(l.unit_price) AS px FROM ml l
                         JOIN pb2 b ON b.basket_id = l.basket_id AND b.store_key = l.store_key
                         WHERE b.in_window AND NOT b.qualifying AND l.unit_price > 0 GROUP BY 1, 2),
                 win_chain AS (SELECT l.item_label, median(l.unit_price) AS px FROM ml l
                         JOIN pb2 b ON b.basket_id = l.basket_id AND b.store_key = l.store_key
                         WHERE b.in_window AND NOT b.qualifying AND l.unit_price > 0 GROUP BY 1),
                 grid AS (SELECT s.store_key, i.item_label
                          FROM (SELECT unnest([{st_list}]) AS store_key) s
                          CROSS JOIN (SELECT unnest([{",".join(_q(it["label"]) for it in pr["items"])}]) AS item_label) i)
            SELECT g.store_key, g.item_label,
                   coalesce(pre.px, pre_chain.px, win.px, win_chain.px) AS base
            FROM grid g LEFT JOIN pre USING (store_key, item_label)
            LEFT JOIN pre_chain USING (item_label) LEFT JOIN win USING (store_key, item_label)
            LEFT JOIN win_chain USING (item_label)""")
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE pi AS
            SELECT l.basket_id, l.store_key, l.day, l.item_label,
                   sum(l.units) AS units, sum(l.net_sales) AS net,
                   sum(l.net_sales) / sum(l.units) AS avg_price,
                   {price_case} AS promo_price, any_value(ib.base) AS base
            FROM ml l LEFT JOIN item_base ib ON ib.store_key = l.store_key AND ib.item_label = l.item_label
            GROUP BY 1, 2, 3, 4""")
        con.execute("""
            CREATE OR REPLACE TEMP TABLE pb4 AS
            SELECT b.* EXCLUDE (qualifying), x.item_applied, x.item_disc, bp.base
            FROM pb2 b
            LEFT JOIN (SELECT basket_id, store_key,
                              bool_or(avg_price <= promo_price * 1.05) AS item_applied,
                              sum(CASE WHEN avg_price <= promo_price * 1.05
                                       THEN greatest(0, coalesce(base, 0) - avg_price) * units ELSE 0 END) AS item_disc
                       FROM pi GROUP BY 1, 2) x ON x.basket_id = b.basket_id AND x.store_key = b.store_key
            LEFT JOIN base_px bp ON bp.store_key = b.store_key""")
    else:
        con.execute("""
            CREATE OR REPLACE TEMP TABLE pb4 AS
            SELECT b.* EXCLUDE (qualifying), NULL::BOOLEAN AS item_applied, NULL::DOUBLE AS item_disc, bp.base
            FROM pb2 b LEFT JOIN base_px bp ON bp.store_key = b.store_key""")

    # final rules, with the base price in hand: qualifying -> near-miss -> applied -> est. discount
    applied_expr = "NULL::BOOLEAN" if rules["applied"] == "NULL" else f"coalesce(({rules['applied']}), FALSE)"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pbf AS
        SELECT *, coalesce(({rules['est']}), 0)::DOUBLE AS est_disc
        FROM (SELECT *, {applied_expr} AS applied
              FROM (SELECT *, coalesce(NOT qualifying AND ({rules['near']}), FALSE) AS nearmiss
                    FROM (SELECT *, coalesce(({rules['qual']}), FALSE) AS qualifying FROM pb4)))""")

    applied_agg = ("NULL::BIGINT" if rules["applied"] == "NULL"
                   else "count(*) FILTER (WHERE applied)")
    PID = _q(pid)

    # ---- dash_promo_day -------------------------------------------------
    con.execute(f"""
        INSERT INTO dash_promo_day
        WITH spine AS (
            SELECT s.store_key, d.day::DATE AS day
            FROM (SELECT unnest([{st_list}]) AS store_key) s
            CROSS JOIN (SELECT unnest(generate_series(DATE {_q(range_lo)}, DATE {_q(end_eff)}, INTERVAL 1 DAY)) AS day) d
            WHERE {keep_day('d.day::DATE')}),
        sb AS (
            SELECT b.store_key, b.day,
                   count(*) AS store_baskets, sum(b.basket_net) AS store_net,
                   count(*) FILTER (WHERE qualifying) AS qualifying_baskets,
                   {applied_agg} AS applied_baskets,
                   sum(basket_net) FILTER (WHERE qualifying) AS qual_basket_net,
                   sum(gross) FILTER (WHERE qualifying) AS qual_basket_gross,
                   sum(est_disc) AS est_discount,
                   count(*) FILTER (WHERE qualifying AND b.customer_key IS NOT NULL AND cf.first_day = b.day) AS new_customers,
                   count(*) FILTER (WHERE qualifying AND b.customer_key IS NOT NULL) AS known_customers,
                   count(*) FILTER (WHERE nearmiss) AS nearmiss_baskets
            FROM pbf b LEFT JOIN pr_cust_first cf ON cf.customer_key = b.customer_key
            GROUP BY 1, 2),
        mu AS (
            SELECT store_key, day, sum(units) AS matched_units, sum(net_sales) AS matched_net,
                   sum(gross_margin) AS matched_gm
            FROM ml GROUP BY 1, 2)
        SELECT {PID}, sp.store_key, sp.day,
               sp.day BETWEEN DATE {_q(start)} AND DATE {_q(end)} AS in_window,
               coalesce(sb.store_baskets, 0), coalesce(sb.store_net, 0),
               coalesce(sb.qualifying_baskets, 0),
               {"NULL" if rules["applied"] == "NULL" else "coalesce(sb.applied_baskets, 0)"},
               coalesce(mu.matched_units, 0), coalesce(mu.matched_net, 0), coalesce(mu.matched_gm, 0),
               coalesce(sb.qual_basket_net, 0), coalesce(sb.qual_basket_gross, 0), coalesce(sb.est_discount, 0),
               coalesce(sb.new_customers, 0), coalesce(sb.known_customers, 0), coalesce(sb.nearmiss_baskets, 0)
        FROM spine sp
        LEFT JOIN sb ON sb.store_key = sp.store_key AND sb.day = sp.day
        LEFT JOIN mu ON mu.store_key = sp.store_key AND mu.day = sp.day
        ORDER BY 2, 3""")

    # ---- dash_promo_baseline ---------------------------------------------
    con.execute(f"""
        INSERT INTO dash_promo_baseline
        SELECT {PID}, d.store_key,
               count(*) FILTER (WHERE store_baskets > 0) AS base_days,
               sum(qualifying_baskets) / nullif(count(*) FILTER (WHERE store_baskets > 0), 0),
               sum(matched_units) / nullif(count(*) FILTER (WHERE store_baskets > 0), 0),
               sum(matched_net) / nullif(count(*) FILTER (WHERE store_baskets > 0), 0),
               sum(store_baskets) / nullif(count(*) FILTER (WHERE store_baskets > 0), 0),
               any_value(bp.base),
               sum(matched_gm) / nullif(sum(matched_net), 0),
               sum(qualifying_baskets) / nullif(sum(store_baskets), 0),
               {_q(base_note)}
        FROM dash_promo_day d LEFT JOIN base_px bp ON bp.store_key = d.store_key
        WHERE d.promo_id = {PID} AND NOT d.in_window
        GROUP BY d.store_key
        UNION ALL
        -- stores with no pre-window day rows at all (data starts inside the window)
        SELECT {PID}, bp.store_key, 0, NULL, NULL, NULL, NULL, bp.base, NULL, NULL, {_q(base_note)}
        FROM base_px bp
        WHERE NOT EXISTS (SELECT 1 FROM dash_promo_day d
                          WHERE d.promo_id = {PID} AND d.store_key = bp.store_key AND NOT d.in_window)""")

    # ---- dash_promo_channel ---------------------------------------------
    con.execute(f"""
        INSERT INTO dash_promo_channel
        WITH bk AS (
            SELECT store_key, channel, count(*) AS store_baskets,
                   count(*) FILTER (WHERE qualifying) AS qualifying_baskets
            FROM pbf WHERE in_window GROUP BY 1, 2),
        mu AS (
            SELECT b.store_key, b.channel, sum(l.units) AS matched_units, sum(l.net_sales) AS matched_net
            FROM ml l JOIN pbf b ON b.basket_id = l.basket_id AND b.store_key = l.store_key
            WHERE b.in_window GROUP BY 1, 2)
        SELECT {PID}, bk.store_key, bk.channel, bk.qualifying_baskets,
               coalesce(mu.matched_units, 0), coalesce(mu.matched_net, 0), bk.store_baskets
        FROM bk LEFT JOIN mu ON mu.store_key = bk.store_key AND mu.channel = bk.channel
        ORDER BY 2, 3""")

    # ---- dash_promo_attach -----------------------------------------------
    con.execute(f"""
        INSERT INTO dash_promo_attach
        WITH per AS (
            SELECT b.store_key, b.basket_id, b.qualifying, l.category, sum(l.net_sales) AS net
            FROM pbf b JOIN {S}fact_line l ON l.basket_id = b.basket_id AND l.store_key = b.store_key
            WHERE b.in_window AND NOT l.is_return AND l.category IS NOT NULL
              AND l.date_key BETWEEN {_dk(start)} AND {_dk(end_eff)}
            GROUP BY 1, 2, 3, 4),
        tot AS (
            SELECT store_key, count(*) FILTER (WHERE qualifying) AS qual_baskets,
                   count(*) FILTER (WHERE NOT qualifying) AS other_baskets
            FROM pbf WHERE in_window GROUP BY 1)
        SELECT {PID}, per.store_key, per.category,
               count(*) FILTER (WHERE qualifying) AS qual_baskets_with_cat,
               coalesce(sum(net) FILTER (WHERE qualifying), 0) AS qual_cat_net,
               count(*) FILTER (WHERE NOT qualifying) AS other_baskets_with_cat,
               any_value(tot.other_baskets), any_value(tot.qual_baskets)
        FROM per JOIN tot ON tot.store_key = per.store_key
        WHERE tot.qual_baskets > 0
        GROUP BY 1, 2, 3 ORDER BY 2, 3""")

    # ---- dash_promo_customer ---------------------------------------------
    tier_sql = ""
    if has_tier:
        tier_sql = f"""
        UNION ALL
        SELECT {PID}, q.store_key, 'tier:' || t.tier, count(*), count(DISTINCT q.customer_key),
               sum(q.m_net), sum(q.basket_net), count(*) FILTER (WHERE q.rn > 1)
        FROM qb q JOIN (SELECT customer_key, any_value(tier) AS tier FROM {S}dim_customer_tier
                        WHERE tier IS NOT NULL GROUP BY 1) t ON t.customer_key = q.customer_key
        GROUP BY 1, 2, 3"""
    con.execute(f"""
        INSERT INTO dash_promo_customer
        WITH qb AS (
            SELECT b.*,
                   CASE WHEN b.customer_key IS NULL THEN 'unknown'
                        WHEN cf.first_day = b.day THEN 'new' ELSE 'returning' END AS seg,
                   CASE WHEN b.customer_key IS NULL THEN 1 ELSE
                        row_number() OVER (PARTITION BY b.customer_key ORDER BY b.txn_ts, b.basket_id) END AS rn
            FROM pbf b LEFT JOIN pr_cust_first cf ON cf.customer_key = b.customer_key
            WHERE b.in_window AND b.qualifying)
        SELECT {PID}, store_key, seg, count(*), count(DISTINCT customer_key),
               sum(m_net), sum(basket_net), count(*) FILTER (WHERE rn > 1)
        FROM qb GROUP BY 1, 2, 3
        {tier_sql}
        ORDER BY 2, 3""")

    # ---- dash_promo_hour -------------------------------------------------
    con.execute(f"""
        INSERT INTO dash_promo_hour
        SELECT {PID}, store_key, day_of_week, hr,
               count(*) FILTER (WHERE qualifying), count(*)
        FROM pbf WHERE in_window GROUP BY 1, 2, 3, 4 ORDER BY 2, 3, 4""")

    # ---- dash_promo_spotlight_item ---------------------------------------
    if mech == "spotlight":
        con.execute(f"""
            INSERT INTO dash_promo_spotlight_item
            WITH spine AS (
                SELECT s.store_key, i.item_label, d.day::DATE AS day
                FROM (SELECT unnest([{st_list}]) AS store_key) s
                CROSS JOIN (SELECT unnest([{",".join(_q(it["label"]) for it in pr["items"])}]) AS item_label) i
                CROSS JOIN (SELECT unnest(generate_series(DATE {_q(range_lo)}, DATE {_q(end_eff)}, INTERVAL 1 DAY)) AS day) d
            WHERE {keep_day('d.day::DATE')}),
            dd AS (
                SELECT store_key, item_label, day, sum(units) AS units, sum(net) AS net,
                       count(*) AS baskets, sum(net) / nullif(sum(units), 0) AS avg_unit_price
                FROM pi GROUP BY 1, 2, 3),
            bd AS (
                SELECT store_key, count(DISTINCT day) AS days FROM pbf WHERE NOT in_window GROUP BY 1),
            pre AS (
                SELECT store_key, item_label, sum(units) AS units FROM pi
                WHERE day < DATE {_q(start)} GROUP BY 1, 2)
            SELECT {PID}, sp.store_key, sp.item_label, sp.day,
                   sp.day BETWEEN DATE {_q(start)} AND DATE {_q(end)},
                   coalesce(dd.units, 0), coalesce(dd.net, 0), coalesce(dd.baskets, 0), dd.avg_unit_price,
                   coalesce(pre.units, 0) / nullif(bd.days, 0), ib.base
            FROM spine sp
            LEFT JOIN dd ON dd.store_key = sp.store_key AND dd.item_label = sp.item_label AND dd.day = sp.day
            LEFT JOIN bd ON bd.store_key = sp.store_key
            LEFT JOIN pre ON pre.store_key = sp.store_key AND pre.item_label = sp.item_label
            LEFT JOIN item_base ib ON ib.store_key = sp.store_key AND ib.item_label = sp.item_label
            ORDER BY 2, 3, 4""")

    # ---- summary line ------------------------------------------------------
    row = con.execute("""
        SELECT count(*) FILTER (WHERE qualifying), count(*) FILTER (WHERE applied),
               count(*) FILTER (WHERE nearmiss), coalesce(sum(est_disc), 0)
        FROM pbf WHERE in_window""").fetchone()
    summary.update(qualifying=row[0], applied=(None if rules["applied"] == "NULL" else row[1]),
                   nearmiss=row[2], est=row[3])
    b = con.execute("SELECT any_value(base), any_value(base_src) FROM base_px WHERE base IS NOT NULL").fetchone()
    summary.update(base=b[0], base_src=b[1])
    for t in ("ml_raw", "ml", "pb", "pb2", "pb4", "pbf", "base_px", "item_base", "pi"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return summary


# ---------------------------------------------------------------------------
# designer tables (promo-agnostic)
# ---------------------------------------------------------------------------
def designer_window(last_day: dt.date) -> tuple[dt.date, dt.date]:
    """[lo, hi) covering exactly DESIGNER_WEEKS complete Mon-Sun weeks. The week
    the data ends in counts only when the data runs through its Sunday; the app
    divides these tables by DESIGNER_WEEKS, so a part-week here would understate
    every per-week figure."""
    monday = last_day - dt.timedelta(days=last_day.weekday())
    hi = monday + dt.timedelta(days=7) if last_day.weekday() == 6 else monday
    return hi - dt.timedelta(weeks=DESIGNER_WEEKS), hi


def _build_designer(con, S: str, last_day: dt.date):
    lo_d, hi_d = designer_window(last_day)
    lo, hi = _dk(lo_d), _dk(hi_d)
    canon = _canon_brand_sql("brand")
    con.execute(f"""
        INSERT INTO dash_promo_gross_band
        SELECT store_key, iso_year, iso_week, channel,
               least(200, greatest(0, CAST(floor((basket_net + abs(coalesce(discount_amt, 0))) / 5) * 5 AS INTEGER))) AS band_lo,
               count(*), sum(basket_net), sum(abs(coalesce(discount_amt, 0)))
        FROM {S}fact_basket
        WHERE NOT is_return AND date_key >= {lo} AND date_key < {hi}
        GROUP BY ALL ORDER BY 1, 2, 3, 4, 5""")
    con.execute(f"""
        INSERT INTO dash_promo_qty_dist
        WITH bl AS (
            SELECT store_key, basket_id, {canon} AS brand, category,
                   sum(units) AS units, sum(net_sales) AS net
            FROM {S}fact_line
            WHERE NOT is_return AND date_key >= {lo} AND date_key < {hi} AND units > 0
              AND brand IS NOT NULL AND trim(brand) <> ''
            GROUP BY ALL),
        big AS (SELECT brand FROM bl GROUP BY 1 HAVING count(DISTINCT basket_id) >= {QTY_DIST_MIN_BASKETS})
        SELECT store_key, brand, category,
               least(6, CAST(round(units) AS INTEGER)) AS units_in_basket,
               count(*), sum(units), sum(net), sum(net) / sum(units)
        FROM bl JOIN big USING (brand)
        WHERE round(units) >= 1
        GROUP BY ALL ORDER BY 1, 2, 3, 4""")
    con.execute(f"""
        INSERT INTO dash_promo_brand_price
        SELECT store_key, {canon} AS brand, category, iso_year, iso_week,
               sum(units), sum(net_sales), sum(gross_margin),
               median(net_sales / units), count(DISTINCT basket_id)
        FROM {S}fact_line
        WHERE NOT is_return AND date_key >= {lo} AND date_key < {hi} AND units > 0
          AND brand IS NOT NULL AND trim(brand) <> ''
        GROUP BY ALL ORDER BY 1, 2, 3, 4, 5""")


# ---------------------------------------------------------------------------
def leak_check(con) -> list[str]:
    """Column names in dash_promo_* that look like identifiers."""
    bad = []
    for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() "
                            "AND table_name LIKE 'dash_promo_%'").fetchall():
        for row in con.execute(f"PRAGMA table_info('{t}')").fetchall():
            col = row[1]
            if (t, col) in ALLOWED_NAME_COLS:
                continue
            if FORBIDDEN_COL.search(col):
                bad.append(f"{t}.{col}")
    return bad


def build_promo(con, src_attached: bool = True) -> dict:
    """Write every dash_promo_* table into the database `con` is connected to.

    src_attached=True: fact_line / fact_basket are read from the attached
    database `src`; False: they are in the main schema (fixture use).
    Returns {table: rowcount}.
    """
    S = "src." if src_attached else ""
    promos.validate_promos()
    first_dk, last_dk = con.execute(
        f"SELECT min(date_key), max(date_key) FROM {S}fact_basket WHERE NOT is_return").fetchone()
    if last_dk is None:
        raise SystemExit("fact_basket is empty -- nothing to build")
    to_date = lambda k: dt.date(k // 10000, (k // 100) % 100, k % 100)
    first_day, last_day = to_date(first_dk), to_date(last_dk)
    has_tier = _has_table(con, "dim_customer_tier", "src" if src_attached else con.execute(
        "SELECT current_database()").fetchone()[0])
    print(f"  sales data {first_day} -> {last_day}; {len(promos.PROMOS)} promos; "
          f"customer tiers {'present' if has_tier else 'absent'}")

    _create_outputs(con)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pr_cust_first AS
        SELECT customer_key,
               min(make_date(date_key // 10000, (date_key // 100) % 100, date_key % 100)) AS first_day
        FROM {S}fact_basket WHERE NOT is_return AND customer_key IS NOT NULL GROUP BY 1""")

    built_at = dt.datetime.now().replace(microsecond=0)
    for p in promos.PROMOS:
        status = promos.promo_status(p, last_day)
        con.execute("INSERT INTO dash_promo_def VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            p["id"], p["name"], p["mechanic"], p["brand_label"], p.get("category"),
            promos._d(p["start"]), promos._d(p["end"]), p.get("takeover"), p.get("program"),
            p.get("notes"), json.dumps(p.get("params") or {}, default=str),
            "all" if not p.get("stores") else ",".join(str(s) for s in p["stores"]),
            status, last_day, built_at])
        s = _build_one(con, p, S, first_day, last_day, has_tier)
        if s["qualifying"] == 0 and promos._d(p["start"]) - dt.timedelta(days=28) > last_day:
            print(f"  {p['id']:<20} {p['mechanic']:<16} {status:<8} not started -- no day rows yet")
            continue
        share = ("n/a" if s["applied"] is None else
                 f"{s['applied'] / s['qualifying']:.0%}" if s["qualifying"] else "-")
        base = f"${s['base']:.2f} ({s['base_src']})" if s["base"] else "no base price"
        print(f"  {p['id']:<20} {p['mechanic']:<16} {status:<8} "
              f"qualifying {s['qualifying']:>6,}  applied {share:>4}  near-miss {s['nearmiss']:>5,}  "
              f"est. discount ${s['est']:>9,.0f}  base {base}")

    print("  designer tables")
    _build_designer(con, S, last_day)
    con.execute("DROP TABLE IF EXISTS pr_cust_first")

    bad = leak_check(con)
    if bad:
        raise RuntimeError("dash_promo_* tables carry identifier-looking columns: " + ", ".join(bad))
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    return counts


# ---------------------------------------------------------------------------
def check(dash: Path) -> int:
    if not dash.exists():
        print(f"{dash} not found"); return 1
    con = duckdb.connect(str(dash), read_only=True)
    for t in TABLES:
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t:<28}{n:>9,} rows")
        except Exception:
            print(f"  {t:<28}  MISSING")
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="build the dash_promo_* tables")
    ap.add_argument("--src", type=Path, default=SRC_DB, help="tta.duckdb (read-only)")
    ap.add_argument("--dash", type=Path, default=DASH_DB, help="cerebral_dash.duckdb to write into")
    ap.add_argument("--check", action="store_true", help="print row counts of the promo tables and exit")
    ap.add_argument("--upload", action="store_true",
                    help="after building, push the dash file to Drive (publish_vm.upload)")
    a = ap.parse_args()
    if a.check:
        return check(a.dash)
    if not a.src.exists():
        print(f"{a.src} not found"); return 1
    if not a.dash.exists():
        print(f"{a.dash} not found -- run publish.py first"); return 1
    con = duckdb.connect(str(a.dash))
    con.execute(f"ATTACH '{a.src}' AS src (READ_ONLY)")
    print(f"== publish_promo  src={a.src}  dash={a.dash}")
    counts = build_promo(con, src_attached=True)
    con.execute("DETACH src")
    con.close()
    for t, n in counts.items():
        print(f"  {t:<28}{n:>9,} rows")
    if a.upload:
        from publish_vm import upload
        return upload(a.dash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
