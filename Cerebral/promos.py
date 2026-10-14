"""
promos.py -- the promo calendar for the Promo Lab tab.

This is the single list of promotions the dashboard knows about. It is plain
Python on purpose (no Streamlit, no DuckDB) so publish_promo.py, checks.py and
the app can all read it without side effects.

HOW TO ADD A PROMO (Kaleo)
  1. Copy one of the blocks in PROMOS below and paste it at the end of the list.
  2. Give it a new "id" -- short, lowercase, letters/digits/underscores, and not
     used by any other block. This is the promo_id used in every table, so do
     not rename it once a build has run.
  3. Fill in the brand patterns (lowercase pieces of the brand name as it
     appears in Dutchie, e.g. "wyld"), the category (one of the fact_line
     categories: Flower, Pre-Roll, Vape, Edible, Concentrate, Accessory,
     Tincture, Topical, CBD -- or None for any), the dates, and the params that
     the mechanic needs (see MECHANICS).
  4. Run  python -c "import promos; promos.validate_promos()"  -- it prints
     nothing when the list is fine and raises a clear error when it is not.
  5. Commit. The next refresh run picks it up; checks.py re-validates it.

Dates are inclusive and written YYYY-MM-DD. "stores": None means all four
stores; otherwise a list of store keys (1 DTBK, 2 5th Avenue, 3 Soho,
4 Union Square). "takeover" must be a name from TAKEOVERS in cerebral_public.py
or None. "program" is just a grouping label for the History timeline.

There are no discount names or promo codes in the sales data, so a promo is
detected from what is in the basket:
  qualifying = the basket looks like the offer (right brand, enough units ...)
  applied    = the prices in the basket, or the till discount on it, show the
               deal was actually given (an estimate -- line prices in the export
               are period averages, see publish_promo.py)
  near-miss  = the basket was one step short (one gummy short, $10 under the
               threshold) -- the people the offer did not quite reach

GOALS (config/promo_goals.json)
  JSON cannot hold comments, so the structure is documented here:
      { "<promo_id>": { "<store_key>": { "<metric>": <target>, ... } } }
  e.g. { "wyld_b2g1": { "4": { "qualifying_baskets": 150 } } }
  store_key is a string ("4" = Union Square). Metrics the Live board
  understands today: qualifying_baskets, matched_units, matched_net.
  The Live board can edit goals for the session and offers a download of the
  edited JSON; paste that over config/promo_goals.json and commit.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOALS_PATH = HERE / "config" / "promo_goals.json"

STORE_KEYS = [1, 2, 3, 4]

# The regex that picks out an eighth of flower from a product name.
EIGHTH_REGEX = r"(3\.5\s*g|1/8|eighth)"

# ---------------------------------------------------------------------------
# Mechanics. For each: the params that MUST be present, the params that MAY be
# present, and the plain-English rule the publisher applies to every basket.
# The same text is used in the app's captions.
# ---------------------------------------------------------------------------
MECHANICS = {
    "bundle_price": dict(
        required=["qty", "bundle_price"], optional=[],
        qualifying="at least {qty} matched units in the basket",
        applied="average matched unit price <= bundle_price / qty (plus 5%), or the till "
                "discount covers at least half of one bundle's markdown",
        nearmiss="exactly {qty}-1 matched units",
    ),
    "threshold_addon": dict(
        required=["threshold", "addon_price"], optional=["addon_regex"],
        qualifying="the rest of the basket (before discounts, add-on taken out) >= threshold and "
                   "a matched line priced within 30% of the add-on price -- or a matched line with "
                   "a till discount covering most of the add-on markdown",
        applied="same as qualifying (the add-on price or the till discount is the evidence)",
        nearmiss="gross between threshold-15 and threshold with no add-on line",
    ),
    "bogo": dict(
        required=["buy", "get"], optional=[],
        qualifying="at least buy+get matched units",
        applied="average matched unit price <= base price x buy/(buy+get) (plus 8%), "
                "a matched line rung at $0, or the till discount covers at least half a free unit",
        nearmiss="exactly {buy} matched units",
    ),
    "multi_pct_off": dict(
        required=["qty", "pct"], optional=["single_brand"],
        qualifying="at least {qty} matched units (of one brand when single_brand)",
        applied="average matched unit price <= base price x (1 - pct) (plus 8%), or the till "
                "discount covers at least half of pct x the units",
        nearmiss="exactly {qty}-1 matched units of one brand",
    ),
    "spotlight": dict(
        required=["items"], optional=[],
        qualifying="basket holds at least one spotlight item",
        applied="the item's average unit price <= its spotlight price (plus 5%)",
        nearmiss="none",
    ),
    "gwp": dict(
        required=["gift_regex"], optional=[],
        qualifying="basket holds a matched brand line (the GWP tables on the Takeovers "
                   "tab track the actual gift)",
        applied="not detectable here (applied_baskets is NULL)",
        nearmiss="none",
    ),
    "ghost_sku": dict(
        required=[], optional=[],   # product_regex on the promo itself does the work
        qualifying="basket holds a line whose product matches product_regex",
        applied="same as qualifying",
        nearmiss="none",
    ),
}

# ---------------------------------------------------------------------------
# THE CALENDAR. Copy a block to add a promo (see the top of the file).
# ---------------------------------------------------------------------------
PROMOS: list[dict] = [
    # ---- Wyld takeover, week 1 ------------------------------------------
    {
        "id": "wyld_3for60",
        "name": "3 Wyld Gummies for $60",
        "mechanic": "bundle_price",
        "brand_label": "Wyld",
        "brand_patterns": ["wyld"],
        "category": "Edible",
        "product_regex": None,
        "exclude_brand_patterns": ["good tide"],
        "start": "2026-09-15", "end": "2026-09-28",
        "stores": None,
        "takeover": "Wyld",
        "program": "Wyld takeover",
        "params": {"qty": 3, "bundle_price": 60.0},
        "notes": "Week 1 of the Wyld takeover offers",
    },
    # ---- Wyld takeover, week 2 ------------------------------------------
    {
        "id": "wyld_spend65",
        "name": "Spend $65, get a Wyld Gummy for $10",
        "mechanic": "threshold_addon",
        "brand_label": "Wyld",
        "brand_patterns": ["wyld"],
        "category": "Edible",
        "product_regex": None,
        "exclude_brand_patterns": [],
        "start": "2026-09-29", "end": "2026-10-12",
        "stores": None,
        "takeover": "Wyld",
        "program": "Wyld takeover",
        "params": {"threshold": 65.0, "addon_price": 10.0},
        "notes": "Week 2 of the Wyld takeover offers",
    },
    # ---- Wyld takeover, week 3 ------------------------------------------
    {
        "id": "wyld_b2g1",
        "name": "Buy 2 Wyld Gummies, Get 1 Free",
        "mechanic": "bogo",
        "brand_label": "Wyld",
        "brand_patterns": ["wyld"],
        "category": "Edible",
        "product_regex": None,
        "exclude_brand_patterns": [],
        "start": "2026-10-13", "end": "2026-10-18",
        "stores": None,
        "takeover": "Wyld",
        "program": "Wyld takeover",
        "params": {"buy": 2, "get": 1},
        "notes": "Week 3 of the Wyld takeover offers",
    },
    # ---- Good Green x Rythm flower ---------------------------------------
    {
        "id": "gg_rythm_4eighths",
        "name": "4 eighths, 25% off (Good Green or Rythm)",
        "mechanic": "multi_pct_off",
        "brand_label": "Good Green / Rythm",
        "brand_patterns": ["good green", "rythm", "rhythm"],
        "category": "Flower",
        "product_regex": EIGHTH_REGEX,
        "exclude_brand_patterns": [],
        # ASSUMED DATES: the October brand campaign -- Kaleo to confirm/correct.
        "start": "2026-10-01", "end": "2026-10-31",
        "stores": None,
        "takeover": None,
        "program": "Brand campaign",
        "params": {"qty": 4, "pct": 25, "single_brand": True},
        "notes": "Dates assumed (whole of October) -- correct them here when confirmed. "
                 "Four eighths must be from one brand.",
    },
    # ---- October store spotlight ----------------------------------------
    {
        "id": "fall_finds_u40",
        "name": "Fall Finds Under $40",
        "mechanic": "spotlight",
        "brand_label": "Mixed",
        "brand_patterns": [],            # per item, see params.items
        "category": None,
        "product_regex": None,
        "exclude_brand_patterns": [],
        "start": "2026-10-19", "end": "2026-11-22",
        "stores": None,
        "takeover": None,
        "program": "Store spotlight",
        "params": {"items": [
            # Clea's Midlife booster is filed under Beverages -> Edible in the
            # category map; the product name is the reliable handle.
            {"label": "Clea Midlife Beverage Booster $30", "brand_patterns": ["clea"],
             "category": None, "product_regex": r"midlife", "price": 30.0},
            {"label": "Good Green Flower $32", "brand_patterns": ["good green"],
             "category": "Flower", "product_regex": EIGHTH_REGEX, "price": 32.0},
            {"label": "MFNY .5 Carts $38", "brand_patterns": ["mfny"],
             "category": "Vape", "product_regex": r"(0?\.5\s*g|half)", "price": 38.0},
            {"label": "Papa & Barkley Edibles $30", "brand_patterns": ["papa"],
             "category": "Edible", "product_regex": None, "price": 30.0},
            {"label": "Deep Black Ashtray $19", "brand_patterns": ["deep black"],
             "category": "Accessory", "product_regex": r"ashtray", "price": 19.0},
            {"label": "Stone Road Hash $36", "brand_patterns": ["stone road"],
             "category": "Concentrate", "product_regex": r"hash", "price": 36.0},
        ]},
        "notes": "From the October store spotlight sheet",
    },
    # ---- Ruby Farms takeover GWP ----------------------------------------
    {
        "id": "ruby_gwp_2eighths",
        "name": "Buy 2 Ruby eighths, get a pre-roll",
        "mechanic": "gwp",
        "brand_label": "Ruby",
        "brand_patterns": ["ruby"],
        "category": "Flower",
        "product_regex": EIGHTH_REGEX,
        "exclude_brand_patterns": [],
        "start": "2026-07-16", "end": "2026-08-17",
        "stores": None,
        "takeover": "Ruby Farms",
        "program": "Ruby Farms takeover",
        "params": {"gift_regex": "gwp"},
        "notes": "The free pre-roll itself is tracked by the GWP tables (Takeovers tab)",
    },
    # ---- Ground Game boarding pass --------------------------------------
    {
        "id": "boarding_pass",
        "name": "Boarding Pass (free item + 200 pts)",
        "mechanic": "ghost_sku",
        "brand_label": "Ground Game",
        "brand_patterns": [],
        "category": None,
        "product_regex": r"boarding pass",
        "exclude_brand_patterns": [],
        "start": "2026-01-01", "end": "2026-12-31",
        "stores": None,
        "takeover": None,
        "program": "Ground Game",
        "params": {},
        "notes": "Redeemed as a $0 'Boarding Pass' line on the receipt",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _d(v) -> dt.date:
    """Accept a date or an ISO string."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def promo_status(p: dict, last_day) -> str:
    """'upcoming' | 'active' | 'done' as of last_day (the last sales day)."""
    last = _d(last_day)
    if _d(p["start"]) > last:
        return "upcoming"
    if _d(p["end"]) < last:
        return "done"
    return "active"


def promo_by_id(promo_id: str) -> dict | None:
    for p in PROMOS:
        if p["id"] == promo_id:
            return p
    return None


def promos_for_takeover(name: str | None) -> list[dict]:
    """Every promo attached to a takeover name (as in TAKEOVERS)."""
    if not name:
        return []
    return [p for p in PROMOS if (p.get("takeover") or "") == name]


def active_promos(day) -> list[dict]:
    d = _d(day)
    return [p for p in PROMOS if _d(p["start"]) <= d <= _d(p["end"])]


def promo_stores(p: dict) -> list[int]:
    return list(p["stores"]) if p.get("stores") else list(STORE_KEYS)


def validate_promos(takeover_names=None) -> None:
    """Raise ValueError describing every problem found; return None when clean.

    takeover_names: optional list of TAKEOVERS names -- when given, every
    promo's "takeover" must be one of them (or None).
    """
    problems: list[str] = []
    seen: set[str] = set()
    for i, p in enumerate(PROMOS):
        tag = f"PROMOS[{i}] ({p.get('id', '?')})"
        pid = p.get("id")
        if not pid or not _ID_RE.match(str(pid)):
            problems.append(f"{tag}: id must be lowercase letters/digits/underscores")
        elif pid in seen:
            problems.append(f"{tag}: duplicate id")
        seen.add(pid)
        for k in ("name", "mechanic", "brand_label", "start", "end", "params"):
            if k not in p:
                problems.append(f"{tag}: missing '{k}'")
        mech = p.get("mechanic")
        if mech not in MECHANICS:
            problems.append(f"{tag}: unknown mechanic '{mech}' (known: {', '.join(MECHANICS)})")
        else:
            params = p.get("params") or {}
            if not isinstance(params, dict):
                problems.append(f"{tag}: params must be a dict")
                params = {}
            for req in MECHANICS[mech]["required"]:
                if req not in params:
                    problems.append(f"{tag}: mechanic {mech} needs params['{req}']")
            if mech == "spotlight":
                items = params.get("items") or []
                if not items:
                    problems.append(f"{tag}: spotlight needs at least one item")
                labels = set()
                for j, it in enumerate(items):
                    for k in ("label", "brand_patterns", "price"):
                        if k not in it:
                            problems.append(f"{tag}: items[{j}] missing '{k}'")
                    if it.get("label") in labels:
                        problems.append(f"{tag}: items[{j}] duplicate label '{it.get('label')}'")
                    labels.add(it.get("label"))
            if mech == "ghost_sku" and not p.get("product_regex"):
                problems.append(f"{tag}: ghost_sku needs product_regex on the promo")
            if mech not in ("spotlight", "ghost_sku") and not p.get("brand_patterns"):
                problems.append(f"{tag}: brand_patterns is empty")
        try:
            s, e = _d(p["start"]), _d(p["end"])
            if s > e:
                problems.append(f"{tag}: start {s} is after end {e}")
        except Exception as exc:
            problems.append(f"{tag}: bad date ({exc})")
        for rx_key in ("product_regex",):
            rx = p.get(rx_key)
            if rx:
                try:
                    re.compile(rx)
                except re.error as exc:
                    problems.append(f"{tag}: {rx_key} does not compile ({exc})")
        if p.get("stores") is not None:
            bad = [s for s in p["stores"] if s not in STORE_KEYS]
            if bad:
                problems.append(f"{tag}: unknown store keys {bad}")
        if takeover_names is not None and p.get("takeover") and p["takeover"] not in takeover_names:
            problems.append(f"{tag}: takeover '{p['takeover']}' is not in TAKEOVERS "
                            f"({', '.join(takeover_names)})")
    if problems:
        raise ValueError("promos.py problems:\n  " + "\n  ".join(problems))


def load_goals(path=None) -> dict:
    """Read config/promo_goals.json -> {promo_id: {store_key(str): {metric: target}}}.
    Missing or unreadable file -> {} (the Live board then shows no goals)."""
    p = Path(path) if path else GOALS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


if __name__ == "__main__":
    validate_promos()
    print(f"{len(PROMOS)} promos OK")
    for p in PROMOS:
        print(f"  {p['id']:<20} {p['mechanic']:<16} {p['start']} -> {p['end']}  "
              f"{promo_status(p, dt.date.today())}")
