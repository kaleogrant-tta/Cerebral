# Cerebral dev kit

Tools for working on the dashboard without the real data file. Everything here
uses made-up numbers, so nothing private is involved. Run all commands from the
`Cerebral/` folder (the one holding `cerebral_public.py`).

## 1. Make the fake data

    python dev/make_fixture.py

Writes `dev/fixture/cerebral_dash.duckdb` (about 25 MB, ~88 `dash_*` tables:
4 stores, 32 ISO weeks of 2026, every tab's tables). It is the same every run.
It reuses the real `publish_events.py`, `publish_vm.py`, `publish_promo.py` and `publish_model.py` code
for the Events, Visual Merch and Promo Lab tables. The other tables follow the column
lists in `publish.py` and the `publish_*.py` files. The file is git-ignored (`*.duckdb`).

For the Promo Lab tables it first fakes a source DB (`fact_line` / `fact_basket`,
W23-W42, 1 Jun - 18 Oct 2026) with the promo behaviour from
`dev/PROMO_LAB_SPEC.md` injected (3 Wyld gummies at $20, $10 gummies on $65+
baskets, buy-2-get-1 with a $0 line, 4 eighths at 25% off, spotlight items at
shelf price, Boarding Pass lines), then runs the real `publish_promo.build_promo`
on it. A slice of the promo baskets is written the way the real export looks
(line prices at the period average, the deal visible only in `discount_amt`) so
the publisher's till-discount route to "applied" is exercised; the rest carry
true per-basket prices, which real data never does (see the caveat at the top of
`publish_promo.py`). The fixture ends on Sunday 18 Oct, so "Fall Finds Under $40"
(from 19 Oct) is an upcoming promo with baseline rows only, and the designer
tables' 13 complete weeks run through W42, one ISO week past `dash_brand_week`
(checks.py reports that as a WARN). The baseline rows show the clean run-up at
work: the Wyld offers follow each other, so `wyld_spend65` and `wyld_b2g1` use
14 clean days (see `base_note`).

## 2. Run the app on it

    bash dev/run_local.sh              # http://localhost:8501
    PORT=8600 bash dev/run_local.sh
    bash dev/run_local.sh --rebuild    # regenerate the fake data first
    bash dev/run_local.sh --restore    # put back a real data file that was set aside

This copies the fixture to `Cerebral/cerebral_dash.duckdb` (git-ignored). A different
file already there is moved to `cerebral_dash.duckdb.real-backup`, never overwritten.

## 3. Smoke test (no browser)

    python dev/smoke_test.py               # every section, all stores
    python dev/smoke_test.py --each-store  # repeat with one store at a time
    python dev/smoke_test.py --only Loyalty,Events -v
    python dev/smoke_test.py --app /path/to/other/cerebral_public.py

Runs the app in-process, visits every section, and prints a table of how much
each one drew (metrics, tables, charts). It lists any exception or `st.error`,
queries that errored, queries that came back empty, and text that looks like a
"no data" notice. Exit code 0 means pass. It works with the row of `st.tabs` and
with the newer pill navigation. It always reads the fixture, even if a real data
file sits next to the app.

## 4. Screenshots

    python dev/screenshot.py                # dev/shots/<section>.png at 1440x900
    python dev/screenshot.py --full         # plus dev/shots/full/<section>.png, whole page
    python dev/screenshot.py --only loyalty,events
    python dev/screenshot.py --width 390 --height 844 --out dev/shots/phone
    python dev/screenshot.py --dark         # dark colour scheme
    python dev/screenshot.py --url http://localhost:8501   # use an app that's already running

Starts the app on port 8599, finds the section buttons from the page itself,
clicks each one and saves a picture. It reports any exception box or JavaScript
error seen in the browser. Needs `playwright` (Python package) and the browser at
`/opt/pw-browsers/chromium`; do not run `playwright install`. The fixture copy in
`Cerebral/` is removed afterwards unless you pass `--keep`.

## Known limits of the fake data

* The first 4 weeks show a high "new customer" share on purpose (the app greys those out).
* The "prior 4 weeks" baseline for the Rythm takeover is empty because data starts 2 March.
* Customer-level tables (loyalty, retention, audiences, events) are plausible shapes, not real behaviour.
* Rythm, Select and Dark Heart have no GWP receipts, so their GWP panel is absent.
