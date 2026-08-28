"""
scorecard_tab.py — GM Weekly Scorecard as a native Cerebral tab.

Ports https://tta-weekly-review.netlify.app/ into Streamlit, writing to DuckDB
instead of a Google Sheet. Every field key from the original form is preserved
verbatim so historical Sheet rows backfill 1:1.

WIRING
------
One function to implement, marked  >>> WIRE ME <<<  below:

    fetch_prefill(con, store, week_start) -> dict

Return whatever Cerebral already knows for that store-week, keyed by scorecard
field name. Return {} and the form still works — every field stays manual.
Everything else runs off the FIELDS spec: DDL, widgets, validation, upsert.

    from scorecard_tab import render, ensure_schema
    ensure_schema(con)
    render(con, user_email=st.experimental_user.email)

WEEK BOUNDARY
-------------
The business week is Monday 01:00 -> Monday 01:00. The original form asked for
"week ending (Sunday)" as a free date input, which invites 8/23 from one GM and
8/24 from the next — and since (store, week) is the primary key, that silently
splits a store's history. Here the GM picks from a generated dropdown of real
weeks and the stored key is always week_start (the Monday).

Note Cerebral itself buckets weeks at midnight, not 01:00. That gap is latent
(no orders fell in the affected hour the week this was written) but it is real,
and it means prefilled figures may drift from a GM's closing report in a week
with post-midnight orders. That is exactly what the prefill audit trail is for.

PREFILL POLICY
--------------
Reconciliation against Dutchie closing reports for 2026-08-17 showed:

    net sales     within 0.11%   safe
    transactions  within 1.5%    safe
    units         no Cerebral equivalent at store level
    discount      ties to the penny at 3 of 4 stores, Soho off 9.7%
    customers     only ~54% of transacting customers resolve to an identity
    new customers 4.3x divergence from Dutchie

So: sales, transactions, UPT and the product tables prefill. Anything
customer-derived (ff_signups, loyalty_redeemed) stays manual until identity
coverage is understood. Never prefill a field into a leadership document when
the underlying number is known to be wrong.

ATV POLICY
----------
    total_sales -> ItemTotal = gross - discount - returns
    atv         -> ItemTotal / transactions, NOT the fee-inclusive figure

Dutchie's printed AverageCartNetSales divides by net-with-fees and so runs
0.30%-0.79% above the figure here. That gap is deliberate. Delivery and ASAP
fees are a service charge, not basket value, and they land unevenly: DTBK and
Soho carry ~2.5x the fee-per-transaction of Fifth and UNSQ because they run
~2.5x the delivery volume. Fee-inclusive ATV therefore rewards delivery mix.
Fees stay in revenue reporting; they stay out of ATV.

The channel effect is an order of magnitude larger than the fee effect.
Measured 2026-08-17, delivery baskets ran 1.8x-2.3x retail baskets, and the
lift a blended ATV takes from delivery mix ranged 4.1% (UNSQ) to 10.5% (Soho).
Blended, Soho reads 62.36 against UNSQ's 60.79. Retail-only, Soho reads 56.45
against UNSQ's 58.37 - the ranking inverts. A leadership scorecard showing
blended ATV across four stores is ranking delivery mix and calling it
merchandising.

So the tab carries all three: blended (kept for continuity with the Google
Sheet history), retail (In-Store + Non-Stop) and delivery. Retail is the
number to compare across stores and across weeks.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

# ---------------------------------------------------------------- palette ---

PALETTE = {
    "green": "#1a3a2a",
    "green_mid": "#2d6a4f",
    "green_light": "#52b788",
    "green_bg": "#f0faf4",
    "border": "#cce4d6",
    "text": "#1b1b1b",
    "muted": "#6b7280",
    "gold": "#b8952a",
    "gold_light": "#fef9ee",
    "page_bg": "#e8ede9",
}

STORES = ["Union Square", "Downtown Brooklyn", "Fifth Avenue", "Soho"]

# Confirmed 2026-08-24. The same mapping appears in acquire.py, acquisition.py,
# analyze.py, build_event_audience_map.py and cerebral_public.py, and is borne
# out by data: dash_events_detail carries store_key alongside store_name.
#
# Short codes vary across the codebase for the same store - Fifth Avenue is
# "5AVE" / "FIFTH" / "5th Avenue", Union Square is "USQ" / "UNSQ". The integer
# is the only stable identifier, so that is what this joins on.
STORE_KEY_IDS = {
    "Downtown Brooklyn": 1,
    "Fifth Avenue": 2,
    "Soho": 3,
    "Union Square": 4,
}

# short codes as used by recon.py and the closing-report tooling
STORE_KEYS = {
    "Union Square": "USQ",
    "Downtown Brooklyn": "DTBK",
    "Fifth Avenue": "FIFTH",
    "Soho": "SOHO",
}

# Cerebral's channel vocabulary, verbatim. Non-Stop is prepaid online pickup -
# still a walk-in basket, so it groups with In-Store. Delivery is separated
# because its baskets run ~2x retail and its share varies 3-9% across stores.
RETAIL_CHANNELS = ("In-Store", "Non-Stop")
DELIVERY_CHANNELS = ("Delivery",)

PRODUCT_CATEGORIES = [
    ("flower", "Flower", "🌸"),
    ("preroll", "Pre-Rolls", "🚬"),
    ("concentrate", "Concentrate", "🧪"),
    ("edible", "Edibles", "🍬"),
    ("vape", "Vape", "💨"),
]
PRODUCT_ROWS = 5


# ------------------------------------------------------------------ spec ---


@dataclass
class Field:
    key: str
    label: str
    kind: str          # int | money | pct | hours | text | textarea | radio | rating
    help: str = ""
    options: list[str] = dc_field(default_factory=list)
    prefill: bool = False      # Cerebral can fill this
    placeholder: str = ""

    SQL = {
        "int": "INTEGER", "money": "DOUBLE", "pct": "DOUBLE", "hours": "DOUBLE",
        "text": "VARCHAR", "textarea": "VARCHAR", "radio": "VARCHAR",
        "rating": "INTEGER",
    }

    @property
    def column(self) -> str:
        return self.key.replace("-", "_")

    @property
    def sql_type(self) -> str:
        return self.SQL[self.kind]


@dataclass
class Section:
    key: str
    title: str
    icon: str
    subtitle: str
    fields: list[Field]
    note: str = ""


def _product_fields() -> list[Field]:
    out: list[Field] = []
    for cat, label, icon in PRODUCT_CATEGORIES:
        for n in range(1, PRODUCT_ROWS + 1):
            out += [
                Field(f"tbl-{cat}_name_{n}", f"{label} #{n} product", "text", prefill=True),
                Field(f"tbl-{cat}_sales_{n}", f"{label} #{n} sales", "money", prefill=True),
                Field(f"tbl-{cat}_units_{n}", f"{label} #{n} units", "int", prefill=True),
            ]
    return out


SECTIONS: list[Section] = [
    Section(
        "sales", "Sales", "💲", "Top-line performance for the week",
        [
            Field("total_sales", "Total sales", "money", prefill=True,
                  help="Net sales: gross less discounts and returns. Matches "
                       "Dutchie's ItemTotal on the closing report."),
            Field("sales_budget", "Sales budget", "money",
                  help="Not in Cerebral or the closing report - enter from plan."),
            Field("atv", "ATV - blended", "money", prefill=True,
                  help="All channels. Kept for continuity with the Sheet history. "
                       "Runs 4-10% above retail ATV depending on delivery mix, so "
                       "do not compare it across stores."),
            Field("atv_retail", "ATV - retail", "money", prefill=True,
                  help="In-Store + Non-Stop. This is the comparable number - it "
                       "reflects basket building, not delivery mix."),
            Field("atv_delivery", "ATV - delivery", "money", prefill=True,
                  help="Delivery baskets run 1.8-2.3x retail. Track it, but never "
                       "average it into a cross-store comparison."),
            Field("upt", "UPT - blended", "money", prefill=True),
            Field("upt_retail", "UPT - retail", "money", prefill=True),
            Field("upt_delivery", "UPT - delivery", "money", prefill=True),
            Field("total_tx", "Transactions - total", "int", prefill=True,
                  help="Dutchie TransactionCount. Excludes returns, voids and "
                       "cancelled orders."),
            Field("tx_retail", "Transactions - retail", "int", prefill=True),
            Field("tx_delivery", "Transactions - delivery", "int", prefill=True,
                  help="Delivery share drives the gap between blended and retail ATV."),
            Field("labor_cost", "Labor cost", "money"),
            Field("labor_pct", "Labor %", "pct"),
            Field("labor_budget_pct", "Labor budget %", "pct"),
            Field("sales_drivers", "What drove sales", "textarea",
                  placeholder="Promos, foot traffic, budtender performance..."),
            Field("sales_headwinds", "What held sales back", "textarea",
                  placeholder="Staffing, inventory, competitors..."),
        ],
    ),
    Section(
        "people", "People", "👥", "Attendance, accountability & culture",
        [
            Field("callouts", "Call-outs", "int"),
            Field("latenesses", "Latenesses", "int"),
            Field("ca_attendance", "CAs - attendance", "int"),
            Field("ca_other", "CAs - other", "int"),
            Field("coaching_cas_pending", "Coaching CAs pending", "int"),
            Field("misdispensing", "Misdispensing incidents", "int"),
            Field("ca_misdispense", "CAs - misdispensing", "int"),
            Field("sched_hrs", "Scheduled hours", "hours"),
            Field("actual_hrs", "Actual hours", "hours"),
            Field("open_positions", "Open positions", "int"),
            Field("pips_active", "PIPs active", "int"),
            Field("tta_cyphers", "TTA Cyphers", "int"),
            # CA pipeline, stage by stage
            Field("ca_written_not_submitted", "Written, not submitted", "int"),
            Field("ca_submitted_this_week", "Submitted this week", "int",
                  help="To Melissa/Mike"),
            Field("ca_pending_approval", "Pending approval", "int"),
            Field("ca_approved_not_delivered", "Approved, not delivered", "int"),
            Field("ca_delivered", "Delivered", "int"),
            Field("review_shoutouts_people", "Shout-outs", "textarea"),
            Field("coaching", "Coaching", "textarea"),
            Field("engagement", "Engagement", "textarea"),
            Field("training", "Training", "textarea"),
            Field("hiring", "Hiring", "textarea"),
            Field("morale", "Team morale", "radio",
                  options=["🔥 High energy", "👍 Solid",
                           "😐 Neutral", "⚠️ Concerns"]),
        ],
        note="CA = Corrective Action - a documented conversation or written warning.",
    ),
    Section(
        "process", "Process", "⚙️", "Compliance, ops & integrity",
        [
            Field("metrc_exceptions", "Metrc exceptions", "int"),
            Field("discrepancy_room", "Discrepancy room", "int"),
            Field("pending_destruction", "Pending destruction", "int"),
            Field("loyalty_redeemed", "Loyalty redeemed", "int",
                  help="Manual for now - Cerebral's loyalty attribution is under "
                       "reconciliation."),
            Field("ff_signups", "Frequent Flyer sign-ups", "int",
                  help="Manual. Non-revenue fee lines on the closing report read "
                       "0 / 495 / 99 / 8.47 across the four stores, which is not a "
                       "consistent enrollment count."),
            # split from the original single void_refund_count - see docstring
            Field("void_count", "Voids", "int",
                  help="Same-session cancellations. Dutchie VoidCount."),
            Field("refund_count", "Refunds", "int",
                  help="Merchandise returns. Some are against sales from earlier "
                       "weeks - count them in the week the refund happened."),
            Field("refused_deliveries", "Refused deliveries", "int"),
            Field("cash_over_short", "Cash over/short", "money",
                  help="Negative = short."),
            Field("compliance_issues", "Compliance issues", "textarea"),
            Field("ops_issues", "Ops issues", "textarea"),
            Field("customer_issues", "Customer issues", "textarea"),
            Field("checklists", "Checklists", "radio",
                  options=["✅ All clear",
                           "⚠️ Minor gaps (resolved)",
                           "🔴 Ongoing concern"]),
            Field("checklist_notes", "Checklist notes", "textarea"),
        ],
    ),
    Section(
        "product", "Product", "🌿", "Category performance & inventory health",
        _product_fields() + [
            Field("oos", "Out-of-stocks / gaps", "textarea"),
            Field("product_feedback", "Product feedback", "textarea"),
            Field("incoming_product", "Incoming product", "textarea"),
        ],
    ),
    Section(
        "reviews", "Reviews", "⭐", "Reputation & recognition this week",
        [
            Field("rev_google", "Google reviews", "int"),
            Field("rev_yelp", "Yelp reviews", "int"),
            Field("rev_wm_leafly", "Weedmaps + Leafly", "int"),
            Field("review_shoutouts", "Shout-outs", "textarea"),
            Field("review_negatives", "Negatives", "textarea"),
            Field("review_ask", "Are we asking for reviews?", "radio",
                  options=["✅ Yes, consistently",
                           "⚡ Sometimes",
                           "❌ Not consistently"]),
        ],
    ),
    Section(
        "reflection", "GM Reflection", "🪞", "Your honest take - no one is graded on this",
        [
            Field("win", "Biggest win", "textarea"),
            Field("challenge", "Biggest challenge", "textarea"),
            Field("next_focus", "Next week's focus", "textarea"),
            Field("support_needed", "Support needed", "textarea"),
            Field("r_sales", "Sales performance & floor presence", "rating"),
            Field("r_people", "Team leadership & coaching", "rating"),
            Field("r_ops", "Compliance & operational execution", "rating"),
            Field("r_energy", "Energy, proactivity & presence", "rating"),
        ],
    ),
]

ALL_FIELDS: list[Field] = [f for s in SECTIONS for f in s.fields]
FIELDS_BY_KEY: dict[str, Field] = {f.key: f for f in ALL_FIELDS}
PREFILLABLE: list[str] = [f.key for f in ALL_FIELDS if f.prefill]


# ---------------------------------------------------------------- schema ---


def schema_sql() -> str:
    """DDL generated from the field spec, so the form and table cannot drift."""
    cols = ",\n    ".join(f'"{f.column}" {f.sql_type}' for f in ALL_FIELDS)
    return f"""
CREATE TABLE IF NOT EXISTS gm_scorecard (
    store_location  VARCHAR NOT NULL,
    week_start      DATE    NOT NULL,
    gm_name         VARCHAR,
    submitted_at    TIMESTAMP,
    submitted_by    VARCHAR,
    {cols},
    PRIMARY KEY (store_location, week_start)
);

-- Every prefilled value alongside what the GM actually submitted. When a GM
-- overwrites a Cerebral figure, that disagreement is a reconciliation signal:
-- either they are reading a different report or Cerebral is wrong that week.
CREATE TABLE IF NOT EXISTS gm_scorecard_prefill_audit (
    store_location  VARCHAR NOT NULL,
    week_start      DATE    NOT NULL,
    field_key       VARCHAR NOT NULL,
    cerebral_value  DOUBLE,
    submitted_value DOUBLE,
    delta           DOUBLE,
    pct             DOUBLE,
    recorded_at     TIMESTAMP,
    PRIMARY KEY (store_location, week_start, field_key)
);
"""


def ensure_schema(con) -> None:
    for stmt in schema_sql().split(";"):
        if stmt.strip():
            con.execute(stmt)


# ------------------------------------------------------------------ weeks ---


def week_starts(n: int = 8, today: dt.date | None = None) -> list[dt.date]:
    """Mondays of the last n completed business weeks, newest first.

    A week is complete once its closing Monday 01:00 has passed. Before that,
    the current week is still open and must not be offered.
    """
    today = today or dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    last_complete = this_monday - dt.timedelta(days=7)
    return [last_complete - dt.timedelta(days=7 * i) for i in range(n)]


def week_label(start: dt.date) -> str:
    end = start + dt.timedelta(days=7)
    return (f"{start:%b %d} → {end:%b %d, %Y}  "
            f"(Mon 1:00 AM to Mon 1:00 AM)")


# ---------------------------------------------------------------- prefill ---


def iso_key(week_start: dt.date) -> tuple[int, int]:
    """(iso_year, iso_week) for a Monday. Cerebral keys weeks this way."""
    y, w, _ = week_start.isocalendar()
    return y, w


def store_key_for(con, store: str) -> int:
    """Scorecard store label -> Cerebral's integer store_key."""
    return STORE_KEY_IDS[store]


def fetch_prefill(con, store: str, week_start: dt.date) -> dict[str, Any]:
    """What Cerebral knows about this store-week, keyed by scorecard field.

    Reads dash_basket_week (baskets and net per store x channel x week) and
    dash_category_week (units on the same grain). Both already exist in
    publish.py. Product tables need dash_product_week - see PRODUCT_WEEK_DDL.

    Returns {} on any miss so the form degrades to fully manual rather than
    showing a GM half a row of numbers.

    Deliberately NOT prefilled: ff_signups, loyalty_redeemed, and anything
    else customer-derived, while identity coverage sits near 54%.
    """
    sk = store_key_for(con, store)
    iy, iw = iso_key(week_start)

    try:
        rows = con.execute("""
            WITH b AS (
                SELECT channel, baskets, net
                FROM dash_basket_week
                WHERE store_key = ? AND iso_year = ? AND iso_week = ?
            ),
            u AS (
                SELECT channel, SUM(units) AS units
                FROM dash_category_week
                WHERE store_key = ? AND iso_year = ? AND iso_week = ?
                GROUP BY 1
            )
            SELECT b.channel, b.baskets, b.net, COALESCE(u.units, 0) AS units
            FROM b LEFT JOIN u USING (channel)
        """, [sk, iy, iw, sk, iy, iw]).fetchall()
    except Exception:
        return {}

    if not rows:
        return {}

    def roll(channels):
        sel = [r for r in rows if r[0] in channels]
        return (sum(r[1] for r in sel),          # baskets
                sum(r[2] or 0 for r in sel),     # net
                sum(r[3] or 0 for r in sel))     # units

    all_tx, all_net, all_units = roll([r[0] for r in rows])
    r_tx, r_net, r_units = roll(RETAIL_CHANNELS)
    d_tx, d_net, d_units = roll(DELIVERY_CHANNELS)

    div = lambda a, b: round(a / b, 2) if b else None
    out: dict[str, Any] = {
        "total_sales":  round(all_net, 2),
        "total_tx":     int(all_tx),
        "tx_retail":    int(r_tx),
        "tx_delivery":  int(d_tx),
        # ItemTotal-based, never fee-inclusive. See the module docstring.
        "atv":          div(all_net, all_tx),
        "atv_retail":   div(r_net, r_tx),
        "atv_delivery": div(d_net, d_tx),
        "upt":          div(all_units, all_tx),
        "upt_retail":   div(r_units, r_tx),
        "upt_delivery": div(d_units, d_tx),
    }

    # A channel value outside the known vocabulary would silently vanish from
    # the retail/delivery split while still counting in the blended figure.
    known = set(RETAIL_CHANNELS) | set(DELIVERY_CHANNELS)
    unknown = {r[0] for r in rows} - known
    if unknown:
        out["_unmapped_channels"] = sorted(unknown)

    try:
        for cat, _, _ in PRODUCT_CATEGORIES:
            prods = con.execute("""
                SELECT product, net, units
                FROM dash_product_week
                WHERE store_key = ? AND iso_year = ? AND iso_week = ?
                  AND lower(category) LIKE ?
                ORDER BY net DESC LIMIT ?
            """, [sk, iy, iw, f"{cat}%", PRODUCT_ROWS]).fetchall()
            for n, (name, net, units) in enumerate(prods, start=1):
                out[f"tbl-{cat}_name_{n}"] = name
                out[f"tbl-{cat}_sales_{n}"] = round(net or 0, 2)
                out[f"tbl-{cat}_units_{n}"] = int(units or 0)
    except Exception:
        pass  # dash_product_week not built yet - product tables stay manual

    return out


# Add to publish.py. There is no product x store x week table today except
# dash_acc_product_week, which is Accessories only, so the scorecard's five
# category tables cannot prefill without this. Capped at the top 10 per
# category so it stays a few tens of thousands of rows.
PRODUCT_WEEK_DDL = """
    CREATE TABLE dash_product_week AS
    SELECT * FROM (
        SELECT store_key, iso_year, iso_week, category, brand, product,
               SUM(units)                AS units,
               SUM(net_sales)            AS net,
               SUM(gross_margin)         AS gm,
               COUNT(DISTINCT basket_id) AS baskets,
               ROW_NUMBER() OVER (
                   PARTITION BY store_key, iso_year, iso_week, category
                   ORDER BY SUM(net_sales) DESC) AS rank_in_cat
        FROM fl
        WHERE NOT is_return AND product IS NOT NULL
        GROUP BY 1,2,3,4,5,6
    ) WHERE rank_in_cat <= 10
"""


def load_existing(con, store: str, week_start: dt.date) -> dict[str, Any]:
    row = con.execute(
        "SELECT * FROM gm_scorecard WHERE store_location = ? AND week_start = ?",
        [store, week_start],
    ).fetchdf()
    if row.empty:
        return {}
    rec = row.iloc[0].to_dict()
    return {f.key: rec.get(f.column) for f in ALL_FIELDS}


# ----------------------------------------------------------------- upsert ---


def _coerce(f: Field, v: Any) -> Any:
    if v is None or v == "":
        return None
    if f.kind == "int" or f.kind == "rating":
        return int(v)
    if f.kind in ("money", "pct", "hours"):
        return float(v)
    return str(v)


def save(con, store: str, week_start: dt.date, gm_name: str,
         values: dict[str, Any], prefilled: dict[str, Any],
         submitted_by: str | None = None) -> None:
    """Upsert one scorecard and record where the GM diverged from Cerebral."""
    now = dt.datetime.now()
    cols = ["store_location", "week_start", "gm_name", "submitted_at", "submitted_by"]
    vals: list[Any] = [store, week_start, gm_name, now, submitted_by]
    for f in ALL_FIELDS:
        cols.append(f'"{f.column}"')
        vals.append(_coerce(f, values.get(f.key)))

    placeholders = ", ".join("?" for _ in vals)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols[2:])
    con.execute(
        f"INSERT INTO gm_scorecard ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (store_location, week_start) DO UPDATE SET {updates}",
        vals,
    )

    for key, cer in prefilled.items():
        f = FIELDS_BY_KEY.get(key)
        if f is None or f.kind not in ("int", "money", "pct", "hours", "rating"):
            continue
        try:
            cer_v = float(cer)
            sub_v = float(values.get(key))
        except (TypeError, ValueError):
            continue
        delta = sub_v - cer_v
        con.execute(
            "INSERT INTO gm_scorecard_prefill_audit VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (store_location, week_start, field_key) DO UPDATE SET "
            "cerebral_value = excluded.cerebral_value, "
            "submitted_value = excluded.submitted_value, "
            "delta = excluded.delta, pct = excluded.pct, "
            "recorded_at = excluded.recorded_at",
            [store, week_start, key, cer_v, sub_v, round(delta, 4),
             round(100 * delta / cer_v, 4) if cer_v else None, now],
        )


def divergences(con, week_start: dt.date | None = None, min_pct: float = 1.0):
    """Fields where a GM overrode Cerebral by more than min_pct."""
    sql = ("SELECT * FROM gm_scorecard_prefill_audit "
           "WHERE abs(coalesce(pct, 0)) >= ?")
    args: list[Any] = [min_pct]
    if week_start:
        sql += " AND week_start = ?"
        args.append(week_start)
    return con.execute(sql + " ORDER BY abs(pct) DESC", args).fetchdf()


def completion(values: dict[str, Any]) -> float:
    filled = sum(1 for f in ALL_FIELDS
                 if values.get(f.key) not in (None, "", 0.0))
    return 100.0 * filled / len(ALL_FIELDS)


# -------------------------------------------------------------------- CSS ---

CSS = """
<style>
  .sc-wrap {{ --green:{green}; --green-mid:{green_mid}; --green-light:{green_light};
    --green-bg:{green_bg}; --border:{border}; --text:{text}; --muted:{muted};
    --gold:{gold}; --gold-light:{gold_light};
    font-family:"Segoe UI", system-ui, sans-serif; color:var(--text); }}
  .sc-banner {{ background:var(--green); color:#fff; padding:14px 18px;
    border-radius:8px; border-left:5px solid var(--gold); margin-bottom:14px; }}
  .sc-banner strong {{ color:var(--gold); }}
  .sc-meter {{ height:8px; background:var(--border); border-radius:4px;
    overflow:hidden; margin:10px 0 4px; }}
  .sc-meter > div {{ height:100%; background:linear-gradient(
    90deg, var(--green-mid), var(--green-light)); }}
  .sc-meter-label {{ font-size:11px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); }}
  .sc-note {{ background:var(--gold-light); border-left:3px solid var(--gold);
    padding:8px 12px; font-size:13px; color:var(--muted); margin-bottom:10px; }}
  .sc-sub {{ color:var(--muted); font-size:13px; margin-top:-4px; }}
  .sc-cat {{ font-size:12px; letter-spacing:.1em; text-transform:uppercase;
    color:var(--green-mid); font-weight:600; margin:14px 0 4px; }}
  .stApp {{ background:{page_bg}; }}
</style>
"""


# ----------------------------------------------------------------- render ---


def _widget(st, f: Field, value: Any, prefilled: bool, key_prefix: str):
    label = f.label + ("  🔌" if prefilled else "")
    help_ = f.help or None
    if prefilled:
        help_ = (help_ + "  " if help_ else "") + "Prefilled from Cerebral - edit if it looks wrong."
    k = f"{key_prefix}:{f.key}"

    if f.kind == "textarea":
        return st.text_area(label, value=value or "", key=k, help=help_,
                            placeholder=f.placeholder, height=90)
    if f.kind == "text":
        return st.text_input(label, value=value or "", key=k, help=help_,
                             placeholder=f.placeholder)
    if f.kind == "radio":
        idx = f.options.index(value) if value in f.options else None
        return st.radio(label, f.options, index=idx, key=k, help=help_,
                        horizontal=True)
    if f.kind == "rating":
        return st.select_slider(label, options=[1, 2, 3, 4, 5],
                                value=value or 3, key=k, help=help_)
    if f.kind == "int":
        return st.number_input(label, value=int(value) if value is not None else 0,
                               step=1, key=k, help=help_)
    fmt = "%.2f"
    prefix = "$" if f.kind == "money" else None
    suffix = "%" if f.kind == "pct" else None
    return st.number_input(
        (f"{prefix}{label}" if prefix else f"{label}{suffix or ''}"),
        value=float(value) if value is not None else 0.0,
        step=0.01, format=fmt, key=k, help=help_,
    )


def render(con, user_email: str | None = None, prefill_fn: Callable | None = None):
    import streamlit as st

    st.markdown(CSS.format(**PALETTE), unsafe_allow_html=True)
    st.markdown('<div class="sc-wrap">', unsafe_allow_html=True)
    st.markdown(
        '<div class="sc-banner">✈️ <strong>GM Weekly Scorecard</strong><br>'
        'This scorecard goes directly to leadership. Be thorough and honest.</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns([2, 2, 3])
    gm_name = c1.text_input("General Manager", value="")
    store = c2.selectbox("Location", STORES, index=None, placeholder="Choose store")
    weeks = week_starts()
    week_start = c3.selectbox("Week", weeks, format_func=week_label)

    if not store:
        st.info("Pick a location to begin.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    existing = load_existing(con, store, week_start)
    fn = prefill_fn or fetch_prefill
    raw_prefill = fn(con, store, week_start)
    unmapped = raw_prefill.pop("_unmapped_channels", None)
    if unmapped:
        st.warning(
            f"Channel value(s) not in the retail/delivery split: {', '.join(unmapped)}. "
            "Blended figures include them; retail and delivery do not."
        )
    prefilled = {k: v for k, v in raw_prefill.items()
                 if k in PREFILLABLE and v is not None}

    # existing submission wins over prefill - never clobber a GM's own entry
    seed = dict(prefilled)
    seed.update({k: v for k, v in existing.items() if v is not None})

    if existing:
        st.caption(f"Editing a submission saved {existing.get('submitted_at') or 'earlier'}.")
    elif prefilled:
        st.caption(f"{len(prefilled)} fields prefilled from Cerebral. "
                   "Edit anything that disagrees with your closing report.")

    meter = st.empty()
    values: dict[str, Any] = {}

    for sec in SECTIONS:
        done = sum(1 for f in sec.fields if seed.get(f.key) not in (None, "", 0.0))
        with st.expander(f"{sec.icon}  {sec.title}  ·  "
                         f"{done}/{len(sec.fields)}", expanded=(sec.key == "sales")):
            st.markdown(f'<div class="sc-sub">{sec.subtitle}</div>',
                        unsafe_allow_html=True)
            if sec.note:
                st.markdown(f'<div class="sc-note">{sec.note}</div>',
                            unsafe_allow_html=True)

            if sec.key == "product":
                for cat, label, icon in PRODUCT_CATEGORIES:
                    st.markdown(f'<div class="sc-cat">{icon} {label} - top {PRODUCT_ROWS}</div>',
                                unsafe_allow_html=True)
                    for n in range(1, PRODUCT_ROWS + 1):
                        cols = st.columns([4, 2, 2])
                        for col, part in zip(cols, ("name", "sales", "units")):
                            fk = f"tbl-{cat}_{part}_{n}"
                            f = FIELDS_BY_KEY[fk]
                            with col:
                                values[fk] = _widget(st, f, seed.get(fk),
                                                     fk in prefilled, sec.key)
                    st.caption("Partial fills are fine.")
                for fk in ("oos", "product_feedback", "incoming_product"):
                    f = FIELDS_BY_KEY[fk]
                    values[fk] = _widget(st, f, seed.get(fk), fk in prefilled, sec.key)
            else:
                narrative = [f for f in sec.fields
                             if f.kind in ("textarea", "radio", "rating")]
                numeric = [f for f in sec.fields if f not in narrative]
                for i in range(0, len(numeric), 3):
                    for col, f in zip(st.columns(3), numeric[i:i + 3]):
                        with col:
                            values[f.key] = _widget(st, f, seed.get(f.key),
                                                    f.key in prefilled, sec.key)
                for f in narrative:
                    values[f.key] = _widget(st, f, seed.get(f.key),
                                            f.key in prefilled, sec.key)

    pct = completion(values)
    meter.markdown(
        f'<div class="sc-meter-label">{pct:.0f}% complete</div>'
        f'<div class="sc-meter"><div style="width:{pct:.0f}%"></div></div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="sc-note">By submitting, you confirm this is your honest '
        'assessment of the week. Submissions are timestamped.</div>',
        unsafe_allow_html=True,
    )
    if st.button("Submit Scorecard ✈️", type="primary",
                 use_container_width=True):
        if not gm_name.strip():
            st.error("Enter your name before submitting.")
        else:
            save(con, store, week_start, gm_name.strip(), values, prefilled,
                 submitted_by=user_email)
            st.success(f"Saved - {store}, week of {week_start:%b %d, %Y}.")
            d = divergences(con, week_start)
            mine = d[(d.store_location == store)] if len(d) else d
            if len(mine):
                st.warning(
                    f"{len(mine)} field(s) differ from Cerebral by more than 1%. "
                    "Logged for reconciliation - nothing for you to do."
                )

    st.markdown("</div>", unsafe_allow_html=True)
