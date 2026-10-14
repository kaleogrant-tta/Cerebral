# Promo Lab — build spec (Oct 2026)

This is the contract between the three workstreams (data layer, Promo Lab tab,
Takeover tab). Column names and promo ids here are binding; everything else is
guidance. Plain English in every caption — the audience is store managers.

## 0. Context you need

* App: `Cerebral/cerebral_public.py` (Streamlit, `st.pills` nav; sections are
  `section_*()` functions listed in `NAV` at the end of the file). Shared chart
  helper `charts.py` (`charts.show(fig, key=..., kind=...)`). Theme colours
  come from CSS variables (`ACCENT`, `WARN`, `BAD`, `SERIES` in the app).
* Helpers inside the app: `q(sql) -> DataFrame` (empty frame on error),
  `table_exists(name)`, `has_col(table, col)`, `keys` (list of selected
  store_keys), `STORES = {1: DTBK, 2: 5th Avenue, 3: Soho, 4: Union Square}`,
  `canon_brand(series)`, `TAKEOVERS` (list of dicts: name/start/end/notes/
  patterns), `PARTIAL_WEEK`, `GLOSSARY` (glossary.py).
* Sub-tab modules follow the signature
  `render_x(q, keys, stores, heading=None, table_exists=None, accent=..., series=...)`
  (see `promo_calendar.py`, `channel_promo.py`, `brand_roi.py`).
* Published file `cerebral_dash.duckdb` must stay privacy-safe: NO
  `customer_key`, names, phones. Aggregates only. `publish.py` runs a leak
  check over every table — any column holding identifier-looking values fails
  the build.
* Source DB `tta.duckdb`: `fact_line` (line_id, basket_id, store_key, txn_ts,
  date_key, iso_year, iso_week, day_of_week, channel, customer_key,
  customer_source, product, sku, raw_category, category, brand, units,
  net_sales, unit_cost, cogs, gross_margin, is_return) and `fact_basket`
  (basket_id, store_key, txn_ts, date_key, iso_year, iso_week, day_of_week,
  channel, customer_key, customer_known, basket_net, basket_margin,
  basket_units, basket_lines, distinct_cats, loyalty_redeem, discount_amt,
  used_redemption, is_return). `net_sales` is after line discounts;
  `discount_amt` is the basket-level discount total; gross basket =
  `basket_net + ABS(discount_amt)` (validated against the ELT deck).
  Categories in fact_line are coarse: Flower, Pre-Roll, Vape, Edible,
  Concentrate, Beverage?, Accessory, Tincture, Topical, CBD. Check with
  `SELECT DISTINCT category` — do not assume a category exists.
  `dim_customer_tier` (customer_key, tier, ...) exists only when
  loyalty_ingest.py has run — treat as optional.
* There are NO discount names or promo codes in the DB. Promo participation is
  inferred from basket composition (rules below). Say so in captions:
  "qualifying" = the basket looks like the offer; "applied" = the price
  evidence says the discount was actually given.
* Publishers are run from `Cerebral/` as separate steps after `publish.py`
  (see `publish_vm.py`, `publish_channel.py`): they ATTACH the source as `src`
  and write `dash_*` tables into the dash file. CLI pattern:
  `python publish_promo.py --src ..\tta.duckdb --dash cerebral_dash.duckdb`.
* Dev harness: `dev/make_fixture.py` builds a fake dash file;
  `dev/smoke_test.py` runs every section in-process; `dev/screenshot.py`
  takes screenshots (Playwright at /opt/pw-browsers/chromium). All new tables
  must exist in the fixture so the smoke test covers the new UI.
* Validation: `checks.py` (post-build gate; FAIL blocks the upload).

## 1. Promo definitions — `Cerebral/promos.py`

A plain-Python module (no Streamlit import) exporting `PROMOS: list[dict]`
and helper functions. Same spirit as `TAKEOVERS`: Kaleo adds a promo by
copying a block. Each dict:

```python
{
  "id": "wyld_3for60",            # stable slug, used as promo_id everywhere
  "name": "3 Wyld Gummies for $60",
  "mechanic": "bundle_price",     # see table below
  "brand_label": "Wyld",          # display
  "brand_patterns": ["wyld"],     # case-insensitive substring match on brand
  "category": "Edible",           # fact_line category to match (None = any)
  "product_regex": None,          # optional regex on product name
  "exclude_brand_patterns": ["good tide"],  # optional
  "start": "2026-09-15", "end": "2026-09-28",
  "stores": None,                 # None = all four, else list of store_key
  "takeover": "Wyld",             # name in TAKEOVERS or None
  "program": "Wyld takeover",     # grouping label for the History timeline
  "params": {"qty": 3, "bundle_price": 60.0},
  "notes": "Week 1 of the Wyld takeover offers",
}
```

Mechanics and their params / qualification rules (per basket, non-return
lines, within the window and store scope):

| mechanic | params | QUALIFYING basket | APPLIED evidence | near-miss bucket |
|---|---|---|---|---|
| `bundle_price` | qty, bundle_price | ≥ qty matched units | matched avg unit price ≤ bundle_price/qty × 1.05 | exactly qty−1 matched units |
| `threshold_addon` | threshold (65), addon_price (10), addon_regex (optional) | gross basket excluding the add-on line ≥ threshold AND ≥1 matched line with unit price between 0.7× and 1.3× addon_price | same as qualifying (the $10 price IS the evidence) | gross basket in [threshold−15, threshold) with no add-on |
| `bogo` | buy (2), get (1) | ≥ buy+get matched units | matched avg unit price ≤ base_unit_price × buy/(buy+get) × 1.08, OR a matched line with net ≤ $0.05 | exactly buy matched units |
| `multi_pct_off` | qty (4), pct (25), single_brand (True) | ≥ qty matched units of ONE brand (when single_brand) | matched avg unit price ≤ base_unit_price × (1−pct/100) × 1.08 | exactly qty−1 units of one brand |
| `spotlight` | items: list of {label, brand_patterns, category, product_regex, price} | basket contains ≥1 spotlight item | item avg unit price ≤ price × 1.05 | none |
| `gwp` | gift_regex | handled by the existing GWP tables (dash_gwp_day / dash_gwp_receipt); publisher writes rows for day/baseline using matched brand units only and sets `applied_baskets = NULL` | — | none |
| `ghost_sku` | product_regex (e.g. `boarding pass`) | basket has a line whose product matches | same | none |

> **Deviation (review, Oct 2026):** `fact_line` unit prices are the product's
> chain-wide *period-average* net price (`tta_etl.py` ~line 696), not the price
> on the receipt, so the price tests above are all-or-nothing per product on real
> data. `publish_promo.py` therefore also accepts the basket's POS
> `discount_amt` as evidence (`BASKET_DISCOUNT_EVIDENCE`), and the baseline is a
> *clean* run-up (sibling-offer days excluded, `base_days` / `base_note` in
> `dash_promo_baseline`). The module docstring has the exact rules.

`base_unit_price` = median unit price (net_sales/units) of the matched
brand+category lines per store in the 28 days before `start` (fall back to
chain-wide, then to the in-window median of NON-qualifying baskets).
"Matched" = line brand matches brand_patterns (minus excludes) AND category
matches AND product_regex matches (eighths for the flower promo:
`(3\.5\s*g|1/8|eighth)` ).

### The promo calendar to ship with

Dates from the October store spotlight sheet and the takeover calendar. Where
a date is an assumption it is marked — Kaleo will correct in `promos.py`.

| id | name | mechanic | brand | window | takeover | params |
|---|---|---|---|---|---|---|
| `wyld_3for60` | 3 Wyld Gummies for $60 | bundle_price | Wyld (exclude Good Tide), Edible | 2026-09-15 → 2026-09-28 | Wyld | qty 3, bundle_price 60 |
| `wyld_spend65` | Spend $65, get a Wyld Gummy for $10 | threshold_addon | Wyld, Edible | 2026-09-29 → 2026-10-12 | Wyld | threshold 65, addon_price 10 |
| `wyld_b2g1` | Buy 2 Wyld Gummies, Get 1 Free | bogo | Wyld, Edible | 2026-10-13 → 2026-10-18 | Wyld | buy 2, get 1 |
| `gg_rythm_4eighths` | 4 eighths, 25% off (Good Green or Rythm) | multi_pct_off | Good Green / Rythm (patterns `good green`, `rythm`, `rhythm`), Flower, eighth regex | 2026-10-01 → 2026-10-31 **(dates assumed)** | None | qty 4, pct 25, single_brand True |
| `fall_finds_u40` | Fall Finds Under $40 | spotlight | mixed | 2026-10-19 → 2026-11-22 | None | items: Clea Midlife Beverage Booster $30 (brand `clea`, category Beverage/Edible — match on product regex `midlife`), Good Green Flower $32 (brand `good green`, Flower, product regex eighth), MFNY .5 Carts $38 (brand `mfny`, Vape, regex `0?\.5\s*g|half`), Papa & Barkley Edibles $30 (brand `papa`, Edible), Deep Black Ashtray $19 (brand `deep black`, Accessory, regex `ashtray`), Stone Road Hash $36 (brand `stone road`, Concentrate, regex `hash`) |
| `ruby_gwp_2eighths` | Buy 2 Ruby eighths, get a pre-roll | gwp | Ruby (`ruby`), Flower, eighth regex | 2026-07-16 → 2026-08-17 | Ruby Farms | gift_regex `gwp` |
| `boarding_pass` | Boarding Pass (free item + 200 pts) | ghost_sku | — | 2026-01-01 → 2026-12-31 | None | product_regex `boarding pass` ; program "Ground Game" |

Helpers in `promos.py`: `promo_status(p, last_day) -> "upcoming"|"active"|"done"`,
`promos_for_takeover(name)`, `active_promos(day)`, `promo_by_id(id)`,
`validate_promos()` (unique ids, start ≤ end, known mechanic, required
params) — call it from checks.py.

## 2. Published tables — `Cerebral/publish_promo.py`

`build_promo(con, src_attached=True) -> dict[table, rowcount]` plus a CLI
`main()` like publish_vm.py (`--src`, `--dash`, `--upload` optional passthrough
to the same Drive helper publish_vm uses, `--check` prints row counts). Must
also work when called from `dev/make_fixture.py` against a fake src.
Idempotent (`CREATE OR REPLACE TABLE`). Skip a promo cleanly when nothing
matches (still write its def row and zero/NULL day rows are NOT required).

All day tables cover `start − 28 days` → `min(end, last sales day)` so the
app can draw the pre-window baseline on the same chart. `in_window` flags
the promo days. Store rows only (no chain row — the app sums). No customer
keys anywhere.

### `dash_promo_def`
promo_id, name, mechanic, brand_label, category, start DATE, end DATE,
takeover VARCHAR, program VARCHAR, notes VARCHAR, params_json VARCHAR,
stores VARCHAR ("all" or comma list), status VARCHAR (as of build),
last_day DATE (last sales day in source), built_at TIMESTAMP.

### `dash_promo_day`  (promo_id × store_key × day)
promo_id, store_key, day DATE, in_window BOOLEAN,
store_baskets BIGINT (all non-return baskets that day),
store_net DOUBLE,
qualifying_baskets BIGINT, applied_baskets BIGINT (NULL for gwp),
matched_units DOUBLE, matched_net DOUBLE, matched_gm DOUBLE,
qual_basket_net DOUBLE (sum of basket_net over qualifying baskets),
qual_basket_gross DOUBLE,
est_discount DOUBLE (Σ over applied baskets of max(0, base_unit_price −
actual unit price) × matched units; for threshold_addon: Σ max(0, base −
addon price)),
new_customers BIGINT (qualifying baskets whose customer's first-ever basket
is that day), known_customers BIGINT (qualifying baskets with a customer_key),
nearmiss_baskets BIGINT.

### `dash_promo_baseline` (promo_id × store_key)
promo_id, store_key, base_days INTEGER (28 or fewer if data starts later),
base_qualifying_pd DOUBLE, base_matched_units_pd DOUBLE,
base_matched_net_pd DOUBLE, base_store_baskets_pd DOUBLE,
base_unit_price DOUBLE, base_gm_pct DOUBLE (matched gm / matched net),
base_qual_share DOUBLE (qualifying / store baskets in the baseline).

### `dash_promo_channel` (promo_id × store_key × channel, in-window only)
promo_id, store_key, channel, qualifying_baskets, matched_units, matched_net,
store_baskets (all baskets in that channel in-window, for share).

### `dash_promo_attach` (promo_id × store_key × category, in-window only)
What else is in qualifying baskets. promo_id, store_key, category,
qual_baskets_with_cat BIGINT, qual_cat_net DOUBLE,
other_baskets_with_cat BIGINT (non-qualifying baskets in the window with
that category), other_baskets BIGINT, qual_baskets BIGINT.
(App computes attach index = share among qualifying ÷ share among others.)

### `dash_promo_customer` (promo_id × store_key × segment, in-window only)
segment ∈ {"new", "returning", "unknown"} plus, when `dim_customer_tier`
exists, rows with segment = "tier:<tier>". Columns: promo_id, store_key,
segment, qualifying_baskets, customers BIGINT (distinct, count only),
matched_net, qual_basket_net, repeat_baskets BIGINT (qualifying baskets from
a customer who had already qualified earlier in the window).

### `dash_promo_hour` (promo_id × store_key × dow × hour, in-window only)
promo_id, store_key, day_of_week INTEGER, hour INTEGER, qualifying_baskets,
store_baskets. (For the "when do people buy it" heat strip.)

### `dash_promo_spotlight_item` (spotlight promos only; promo_id × store_key × item × day)
promo_id, store_key, item_label, day, in_window, units, net, baskets,
avg_unit_price, base_units_pd (28-day prior), base_unit_price.

### Designer tables (promo-agnostic, last 13 complete ISO weeks + window)
* `dash_promo_gross_band` — store_key, iso_year, iso_week, channel,
  band_lo INTEGER (0,5,10,…,195, 200 = "200+"), baskets, net, discount.
  Gross = basket_net + ABS(discount_amt).
* `dash_promo_qty_dist` — store_key, brand, category, units_in_basket
  INTEGER (1..5, 6 = "6+"), baskets, units, net, avg_unit_price. Brands with
  ≥ 200 baskets in the 13 weeks only (keeps the table small). Brand
  canonicalised via the same alias map publish.py uses.
* `dash_promo_brand_price` — store_key, brand, category, iso_year,
  iso_week, units, net, gm, median_unit_price, baskets.

### checks.py additions
* `check_promo_defs` — `promos.validate_promos()` clean; every PROMOS id has
  a `dash_promo_def` row; takeover names resolve to TAKEOVERS (import from
  cerebral_public is heavy — read TAKEOVERS by regex from the file or move the
  comparison into `promos.validate_promos(takeover_names)`).
* `check_promo_day` — for each active/done promo: day rows exist through
  last_day; `applied_baskets ≤ qualifying_baskets ≤ store_baskets`;
  `est_discount ≥ 0`; no duplicate (promo_id, store_key, day).
* `check_promo_leak` — none of the dash_promo_* tables have a column named
  like customer/name/phone/email.
* WARN (not FAIL) when an active promo has 0 qualifying baskets chain-wide
  on its most recent 3 days (probably a wrong pattern or category).

### Workflow / build order
Add to `.github/workflows/refresh.yml` after "Publish VM tables":
`python publish_promo.py --src /tmp/tta_work/tta.duckdb --dash cerebral_dash.duckdb`
and run BEFORE checks.py. Document the local recipe line in CADENCE.md or
README (same place the VM step is documented). `publish_vm.py --upload`
uploads whatever dash file exists, so no upload change is needed — confirm
by reading publish_vm.py.

### Fixture
`dev/make_fixture.py`: build a fake `src` DuckDB with `fact_line` and
`fact_basket` (same columns as above) covering 2026-W23 → W42 for 4 stores,
with injected behaviour: Wyld gummies baskets at 3 units priced $20 in the
3-for-$60 window, $10 Wyld lines on $65+ baskets in the spend-65 window,
3-unit baskets with one $0 line in the B2G1 window, 4-eighth Good Green /
Rythm baskets at 25% off in October, spotlight items, a few "Boarding Pass"
lines, plus ordinary baskets. Then call `publish_promo.build_promo` for real
(mirror how `build_vm` runs `publish_vm.build`). The fixture must make every
sub-tab show data.

## 3. Promo Lab tab — `Cerebral/promo_lab_tab.py`

`render_promo_lab(q, keys, stores, heading=None, table_exists=None,
accent=..., series=..., warn=..., bad=..., takeovers=TAKEOVERS,
partial_week=False, legacy=callable)` — `legacy` is the existing
`render_promo_lab()` body (the win-back model) which moves to its own
sub-tab unchanged in logic. `section_promo_lab()` in cerebral_public.py
calls the new module. Store filter (`keys`) applies everywhere.

Sub-tabs, in order:

1. **Live board** — what is running now.
   * Status strip: one card per promo (active first, then upcoming within 30
     days, then the last two finished). Card: name, brand, window, days
     left, qualifying baskets so far, applied %, units, net, est. discount,
     "vs the 4 weeks before" (qualifying per day ÷ baseline per day, with the
     dollar difference beside every %), new-customer share.
   * Pick a promo → daily chart: qualifying baskets per day (bars) with the
     baseline per-day rate as a dashed line and the window shaded; second
     trace or small multiple for est. discount $.
   * Store leaderboard with goals (see §5): store, qualifying, applied %,
     units, net, est. discount, goal, pace (current per-day × days in window
     vs goal), status word ("on pace" / "behind" / "done").
   * Alerts (plain English, `alert a-warn` style): applied % < 70% of
     qualifying ("looks like the discount is not being rung — check the
     till"), near-miss count ("N baskets were one gummy short"), zero
     qualifying in the last 3 days of an active promo.
   * Not-yet-detectable note where applicable (gwp → points to Takeovers
     tab GWP section).
2. **Anatomy** — pick a promo (default: most recent active).
   * Who: new / returning / unknown share and spend; tier rows when present.
   * When: dow × hour heat strip of qualifying baskets as share of store
     baskets.
   * Channel: in-store / Non-Stop / delivery share vs the store's channel mix.
   * Attach: top categories by attach index, with net $ beside each.
   * Price & volume: units per day during vs baseline, unit price during vs
     base, and the **break-even volume lift** = d/(m−d) where d = depth
     (1 − price_during/base_price) and m = baseline gm%; verdict sentence:
     "Units/day rose X%; the discount needed Y% just to break even" →
     paid / did not pay / too early to call (< 5 window days).
   * Basket size: avg qualifying basket vs avg non-qualifying basket in the
     window (from qual_basket_net and store_net).
3. **Designer** — what-if for a new promo. Mechanic picker (bundle_price /
   threshold_addon / bogo / multi_pct_off), brand (from dash_promo_qty_dist),
   category, qty/threshold/depth sliders. Outputs from the designer tables:
   baskets per week that already qualify at current behaviour (so the
   discount is pure cost on them), baskets in the stretch band (one unit
   short / within $15 of the threshold), weekly cost at current behaviour,
   incremental qualifying baskets needed to break even (cost ÷ (margin per
   qualifying basket − discount)), and a sentence. For threshold_addon show
   the gross-band histogram with the threshold marked and the share of
   baskets at/above it (this reproduces the "$65" analysis). Depth ceiling
   from `dash_channel_pair` if the table exists (reuse promo_calendar's
   maths; import, do not copy). Button "Copy definition" that renders the
   `promos.py` dict block as a code snippet.
4. **History** — timeline (Plotly Gantt-style bars) of every promo, coloured
   by program, with takeover windows as light bands behind; table of
   finished promos with headline results (qualifying, applied %, net, est.
   discount, vs baseline, new share); compare two promos side by side,
   per-day normalised.
5. **Win-back model** — the existing render_promo_lab body (Churn Map, Store
   Opportunities, Brand Promos, Brand Discount ROI) nested as before. Keep
   the audit relabels (break-even wording). No logic change.
6. **Channel & Day** — existing `render_channel_promo`.
7. **Promo Calendar** — existing `render_promo_calendar`.

Every table gets `st.column_config` number formats; every % has the $ next
to it; captions say "qualifying (looks like the offer)" vs "applied (price
shows the discount)". Use `charts.show`. Downloads: CSV of the live board
and of the anatomy tables.

## 4. Takeover tab — `Cerebral/takeover_promos.py`

Two render functions wired into `section_takeovers()`:

* `render_current_promos(q, keys, stores, ...)` — a compact strip at the TOP
  of the Takeover tab: "Promos running now" — one row per active promo
  (any program), with qualifying baskets, applied %, units, net, est.
  discount, days left, and a per-store mini-bar; a line for upcoming promos
  starting within 14 days. Links the reader to Promo Lab for detail
  (`?tab=promo-lab`).
* `render_takeover_offers(q, keys, stores, tk, ...)` — inside the selected
  takeover's detail (after the headline metrics, before the GWP section):
  "Offers inside this takeover" — one block per promo with
  `takeover == tk["name"]`: window, qualifying, applied %, units, net, est.
  discount, vs baseline; a stacked daily chart across the takeover showing
  qualifying baskets per offer so the three Wyld weeks read as one story;
  per-store table. When the takeover has no offers, one quiet caption.

## 5. Goals — `Cerebral/config/promo_goals.json`

```json
{ "wyld_b2g1": {"4": {"qualifying_baskets": 150}},
  "gg_rythm_4eighths": {"4": {"qualifying_baskets": 120}, "1": {...}} }
```
Keyed by promo_id → store_key (string) → metric → target. Loaded by a tiny
helper in `promos.py` (`load_goals(path=None)`), editable in the Live board
via `st.number_input` per store (session only) with a "Download goals JSON"
button so Kaleo can commit it. Ship the file with placeholder goals for USQ
on the active promos and a comment-free structure (JSON has no comments —
document it in promos.py).

## 6. Done means

* `python dev/make_fixture.py && python dev/smoke_test.py` passes with
  Promo Lab and Takeovers drawing every new block (no exceptions, no
  missing-table queries for dash_promo_*).
* `python checks.py` new checks pass on the fixture (or skip cleanly when
  `--db` is absent).
* `python -c "import promos; promos.validate_promos()"` clean.
* Screenshots of Promo Lab sub-tabs and Takeovers look right at 1440 wide.
