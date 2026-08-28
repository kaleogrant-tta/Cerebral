"""
scorecard_prefill.py — the query layer behind the GM Weekly Scorecard.

Implements the `>>> WIRE ME <<<` function from scorecard_tab.py against the
tables publish.py actually writes. Kept as its own module so scorecard_tab.py
stays a pure form spec: the tab takes it as an argument, no edit required.

    import scorecard_tab as sc
    from scorecard_prefill import fetch_prefill

    sc.ensure_schema(con)
    sc.render(con, user_email=..., prefill_fn=fetch_prefill)

Return {} and the form still runs fully manual. Nothing else depends on it.

WHAT IT READS
-------------
    dash_basket_week    store_key, iso_year, iso_week, channel, baskets, net
    dash_category_week  ... + category, units          (units live only here)
    dash_product_week   store_key, iso_year, iso_week, category, brand,
                        product, units, net, rank_in_cat
    dash_meta           last_txn                        (coverage guard)
    dash_incomplete_days store_key, day                 (coverage guard)

dash_basket_week carries no unit count, so UPT is units from
dash_category_week over baskets from dash_basket_week. The two agree by
construction — dash_category_week derives its basket counts from the same
`fb` view.

STORE KEYS ARE INTEGERS
-----------------------
publish.py declares `store_key INTEGER` throughout. An earlier draft of this
query passed the string keys recon.py uses ("SOHO"); tested against a fixture
whose store_key was VARCHAR it looked fine, and against the real published
file it raises a DuckDB conversion error the first time a GM opens the tab.
STORE_KEY below is the mapping. Call `assert_store_keys()` once at app start
so a drift shows up on boot rather than in front of a GM.

COVERAGE
--------
Two ways a prefill can be confidently wrong rather than absent:

  1. The published file predates the end of the selected week. A build run
     Wednesday covers three days of that week; every figure reads ~40% low and
     nothing about it looks broken.
  2. publish.py excluded a truncated day inside the week via
     dash_incomplete_days.

Either one and this returns {} rather than a partial week, with a warning.
A leadership document gets a blank field or a right one, never a plausible
wrong one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

# publish.py: `store_key INTEGER`. Confirmed against the 2026-08-17 closing
# report reconciliation, where each key tied to its store's own report.
STORE_KEY = {
    "Downtown Brooklyn": 1,
    "Fifth Avenue": 2,
    "Soho": 3,
    "Union Square": 4,
}

# store_key 0 is the deduplicated chain aggregate, not a store. Never select it
# here — a store-scoped scorecard reading it would report chain totals.
CHAIN_STORE_KEY = 0

RETAIL_CHANNELS = ("In-Store", "Non-Stop")
DELIVERY_CHANNELS = ("Delivery",)

PRODUCT_ROWS = 5

# Scorecard category -> a SQL predicate over dash_product_week.category.
# Prefix matching, following publish.py's own `category ILIKE 'Accessor%'`
# convention, so a rename from "Pre-Rolls" to "Pre-Roll" doesn't empty a table.
# Hyphens and spaces are stripped before matching so Pre-Rolls / Pre Rolls /
# PreRolls all land in the same bucket.
CATEGORY_MATCH = {
    "flower":      "flower%",
    "preroll":     "preroll%",
    "concentrate": "concentrat%",
    "edible":      "edible%",
    "vape":        "vap%",
}

_NORMALISE = "lower(replace(replace({c}, '-', ''), ' ', ''))"


# ------------------------------------------------------------------ warn ---


def _warn(msg: str) -> None:
    """Surface a problem in the Streamlit tab if we're inside it, else stdout."""
    try:
        import streamlit as st
        st.warning(msg)
    except Exception:
        print(f"[scorecard prefill] {msg}")


def _has(con, table: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [table]
    ).fetchone()[0] > 0


def assert_store_keys(con) -> None:
    """Fail loudly if STORE_KEY doesn't line up with what was published.

    Call once at app start. Checks only that every mapped key exists and that
    the chain aggregate isn't among them; it cannot verify that key 3 is Soho
    rather than Fifth Avenue. If store labels are ever added to the published
    file, tighten this to a name comparison.
    """
    if not _has(con, "dash_basket_week"):
        raise RuntimeError("dash_basket_week missing — publish.py has not run.")
    present = {r[0] for r in
               con.execute("SELECT DISTINCT store_key FROM dash_basket_week").fetchall()}
    missing = set(STORE_KEY.values()) - present
    if missing:
        raise RuntimeError(
            f"store_key(s) {sorted(missing)} are not in dash_basket_week "
            f"(found {sorted(present)}). STORE_KEY in scorecard_prefill.py is "
            f"out of date with the published file."
        )
    if CHAIN_STORE_KEY in STORE_KEY.values():
        raise RuntimeError("STORE_KEY maps a store onto the chain aggregate (0).")


# -------------------------------------------------------------- coverage ---


def coverage(con, store: str, week_start: dt.date) -> tuple[bool, str]:
    """(ok, reason). False means the published file cannot describe this week."""
    sk = STORE_KEY.get(store)
    if sk is None:
        return False, f"{store!r} is not a known store."

    week_end = week_start + dt.timedelta(days=7)

    if _has(con, "dash_meta"):
        last = con.execute("SELECT max(last_txn) FROM dash_meta").fetchone()[0]
        if last is not None:
            last_d = last.date() if hasattr(last, "date") else last
            # The week closes at Monday 01:00, so data must reach that Monday.
            if last_d < week_end - dt.timedelta(days=1):
                return False, (
                    f"The published file only holds data through {last_d:%b %d}, "
                    f"which does not cover the week ending {week_end:%b %d}. "
                    f"Prefill is off for this week — enter figures from your "
                    f"closing report, or rerun publish.py."
                )

    if _has(con, "dash_incomplete_days"):
        bad = con.execute(
            "SELECT day FROM dash_incomplete_days "
            "WHERE store_key = ? AND day >= ? AND day < ?",
            [sk, week_start, week_end],
        ).fetchall()
        if bad:
            days = ", ".join(f"{d[0]:%b %d}" for d in bad)
            return False, (
                f"publish.py excluded {days} at this store as a truncated "
                f"export day, so the week is incomplete in Cerebral. Prefill is "
                f"off — enter figures from your closing report."
            )

    return True, ""


# --------------------------------------------------------------- prefill ---


def _channel_totals(con, sk: int, iy: int, iw: int):
    """baskets and net per channel, plus units per channel."""
    baskets = con.execute(
        "SELECT channel, SUM(baskets), SUM(net) FROM dash_basket_week "
        "WHERE store_key = ? AND iso_year = ? AND iso_week = ? GROUP BY 1",
        [sk, iy, iw],
    ).fetchall()
    units = dict(con.execute(
        "SELECT channel, SUM(units) FROM dash_category_week "
        "WHERE store_key = ? AND iso_year = ? AND iso_week = ? GROUP BY 1",
        [sk, iy, iw],
    ).fetchall()) if _has(con, "dash_category_week") else {}
    return {ch: dict(baskets=b or 0, net=float(n or 0), units=float(units.get(ch) or 0))
            for ch, b, n in baskets}


def _roll(rows: dict, channels) -> dict:
    keep = [v for ch, v in rows.items() if ch in channels]
    return dict(
        baskets=sum(v["baskets"] for v in keep),
        net=sum(v["net"] for v in keep),
        units=sum(v["units"] for v in keep),
    )


def _rate(num: float, den: float, nd: int = 2):
    return round(num / den, nd) if den else None


def fetch_prefill(con, store: str, week_start: dt.date) -> dict[str, Any]:
    """Everything Cerebral can say about one store-week, keyed by field."""
    sk = STORE_KEY.get(store)
    if sk is None or not _has(con, "dash_basket_week"):
        return {}

    ok, why = coverage(con, store, week_start)
    if not ok:
        _warn(why)
        return {}

    iy, iw, _ = week_start.isocalendar()
    rows = _channel_totals(con, sk, iy, iw)
    if not rows:
        return {}

    known = set(RETAIL_CHANNELS) | set(DELIVERY_CHANNELS)
    unmapped = sorted(set(rows) - known)

    total = _roll(rows, set(rows))          # every channel, mapped or not
    retail = _roll(rows, RETAIL_CHANNELS)
    deliv = _roll(rows, DELIVERY_CHANNELS)

    out: dict[str, Any] = {
        "total_sales":   round(total["net"], 2),
        "total_tx":      int(total["baskets"]),
        "tx_retail":     int(retail["baskets"]),
        "tx_delivery":   int(deliv["baskets"]),
        "atv":           _rate(total["net"], total["baskets"]),
        "atv_retail":    _rate(retail["net"], retail["baskets"]),
        "atv_delivery":  _rate(deliv["net"], deliv["baskets"]),
        "upt":           _rate(total["units"], total["baskets"]),
        "upt_retail":    _rate(retail["units"], retail["baskets"]),
        "upt_delivery":  _rate(deliv["units"], deliv["baskets"]),
    }
    if unmapped:
        out["_unmapped_channels"] = unmapped

    out.update(_product_tables(con, sk, iy, iw))
    return out


def _product_tables(con, sk: int, iy: int, iw: int) -> dict[str, Any]:
    """Top PRODUCT_ROWS by net for each of the five scorecard categories.

    All-channel: dash_product_week carries no channel dimension. That is the
    right grain for this — a GM's top sellers are top sellers regardless of how
    the basket left the building.
    """
    if not _has(con, "dash_product_week"):
        _warn("dash_product_week is not in the published file, so the product "
              "tables stay manual. Rerun publish.py to enable them.")
        return {}

    out: dict[str, Any] = {}
    matched_any = False
    norm = _NORMALISE.format(c="category")

    for cat, pattern in CATEGORY_MATCH.items():
        rows = con.execute(
            f"""
            SELECT product, net, units
            FROM dash_product_week
            WHERE store_key = ? AND iso_year = ? AND iso_week = ?
              AND {norm} LIKE ?
            ORDER BY net DESC
            LIMIT ?
            """,
            [sk, iy, iw, pattern, PRODUCT_ROWS],
        ).fetchall()
        if rows:
            matched_any = True
        for n, (product, net, units) in enumerate(rows, start=1):
            out[f"tbl-{cat}_name_{n}"] = product
            out[f"tbl-{cat}_sales_{n}"] = round(float(net or 0), 2)
            out[f"tbl-{cat}_units_{n}"] = int(units or 0)

    # A category map rename would empty every table at once and look like a
    # quiet week rather than a broken join. Say so.
    if not matched_any:
        present = [r[0] for r in con.execute(
            "SELECT DISTINCT category FROM dash_product_week "
            "WHERE store_key = ? AND iso_year = ? AND iso_week = ? "
            "AND category IS NOT NULL ORDER BY 1",
            [sk, iy, iw],
        ).fetchall()]
        if present:
            _warn(
                "No product category matched the scorecard's five tables. "
                f"Categories in the data this week: {', '.join(present)}. "
                "Update CATEGORY_MATCH in scorecard_prefill.py."
            )
    return out
