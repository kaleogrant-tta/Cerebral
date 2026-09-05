"""
The Travel Agency — Category Analytics ETL
Configuration: canonical maps, store registry, channel rules, availability calendar.

Everything that encodes a business decision lives here, not in the pipeline.
If a number looks wrong, this file is the first place to look.
"""

# ---------------------------------------------------------------------------
# STORES
# Never key on the location string: Soho's is missing "The".
# Integer keys are the join key everywhere downstream.
# ---------------------------------------------------------------------------
STORES = {
    "The Travel Agency Downtown Brooklyn": {"store_key": 1, "code": "DTBK", "name": "Downtown Brooklyn"},
    "The Travel Agency Fifth Avenue":      {"store_key": 2, "code": "5AVE", "name": "Fifth Avenue"},
    "Travel Agency Soho":                  {"store_key": 3, "code": "SOHO", "name": "Soho"},
    "The Travel Agency Union Square":      {"store_key": 4, "code": "USQ",  "name": "Union Square"},
}

# ---------------------------------------------------------------------------
# CANONICAL CATEGORY MAP
# Validated across 2 stores x 3 years (2024/2025/2026): 17 raw strings, zero drift.
# Raw Dutchie category -> canonical category.
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    "Flower":              "Flower",

    "Pre-Rolls":           "Pre-Roll",
    "Single Pre-Roll":     "Pre-Roll",
    "Multi-Pack Pre-Roll": "Pre-Roll",

    "All-In-One":          "Vape",
    "Cartridge | 510":     "Vape",
    "Pod":                 "Vape",

    "Gummies":             "Edible",
    "Chocolate":           "Edible",
    "Beverages":           "Edible",
    "Tablets":             "Edible",
    "Edibles":             "Edible",
    "Capsules":            "Edible",
    "Cooking":             "Edible",

    "Concentrate":         "Concentrate",
    "Tincture":            "Tincture",
    "Topical":             "Topical",
    "Accessories":         "Accessory",

    # CBD is a cannabinoid attribute, not a form factor. Held separate until
    # the underlying SKUs are reassigned to their true form.
    "CBD":                 "CBD",

    # Excluded from all revenue analysis.
    "Non-Sale":            "EXCLUDE",
}

# Order used in scorecards / reports.
CATEGORY_ORDER = [
    "Flower", "Pre-Roll", "Vape", "Edible",
    "Concentrate", "Accessory", "CBD", "Tincture", "Topical",
]

# Categories that get has_/net_ex_ columns on fact_basket.
BASKET_FLAG_CATEGORIES = ["Flower", "Pre-Roll", "Vape", "Edible", "Concentrate", "Accessory"]

# ---------------------------------------------------------------------------
# CHANNEL
# Register naming varies by store AND by year:
#   DTBK      "REGISTER 6"        (caps)
#   5th/Soho  "Register 10"       (title case)
#   USQ       "835 Register 12"   (store-code prefix)
#   sample    "Sample Register" / "SAMPLE REGISTER"
# Case-insensitive substring + regex handles all observed variants.
# ---------------------------------------------------------------------------
CHANNEL_RULES = [
    ("Non-Stop", ["non stop", "nonstop"]),
    # "Van N - xxx" registers are TTA's own delivery vans, May 2023 - Feb 2024,
    # before delivery moved to Doobie. Same channel, different fulfilment.
    ("Event", ["event van"]),        # MUST precede Delivery: van rule would catch it
    ("Delivery", ["doobie", "van"]),
    # Excluded from customer analytics. Sample = testing; Internal Purchase =
    # employee sales, which are real revenue but not customer behaviour and
    # would distort ATV, penetration and channel mix.
    # "Test register surface" -- 9 Dutchie commissioning rings in Jul 2023,
    # $0.50-$1.00, one item each, numeric placeholder names.
    ("EXCLUDE", ["sample", "internal purchase", "test register"]),
]
CHANNEL_NUMBERED_PATTERN = r"register\s*\d+"   # -> In-Store
CHANNELS = ["In-Store", "Non-Stop", "Delivery", "Event"]

# ---------------------------------------------------------------------------
# AVAILABILITY CALENDAR
# Absent != zero. A channel or program that did not exist must be NULL in the
# aggregates, never 0, or trend lines show phantom launches and collapses.
#
# TWO DIFFERENT DATES, and conflating them corrupts the In-Store series:
#
#   launched          -- when customers could actually transact on the channel
#   data_visible_from -- when the channel became separately IDENTIFIABLE in the
#                        export (i.e. got its own register)
#
# Between those two dates the volume exists but is booked to numbered
# registers, so it is silently counted as In-Store. Any metric spanning that
# window must treat In-Store as "In-Store + unattributed Non-Stop", not as a
# clean channel.
# ---------------------------------------------------------------------------
AVAILABILITY = {
    "channel": {
        "In-Store": {
            "launched": "2022-01-01",
            "data_visible_from": "2022-01-01",
        },
        "Delivery": {
            # Delivery began on the vans (Van 1, first txn 2023-05-26) and
            # moved to Doobie 2024-03-22. Same channel, different fulfilment.
            # CHANNEL_RULES maps both. Launch must predate Doobie or
            # agg_category_week NULLs 1,216 van baskets as "did not exist".
            "launched": "2023-05-26",
            "data_visible_from": "2023-05-26",
        },
        "Event": {
            # Offsite activations, Doobie Event Van. 15 trading days total,
            # 3 of them 88% of revenue. Dormant since 2024-10-05.
            "launched": "2023-08-18",
            "data_visible_from": "2023-08-18",
        },
        "Non-Stop": {
            # Business launch, confirmed by Kaleo.
            "launched": "2025-05-12",
            # Dedicated register created 2025-09-30 16:24 (12 transactions that
            # afternoon, full volume from 10-01). Confirmed from the 13-month
            # DTBK POS export.
            "data_visible_from": "2025-10-01",
            # DARK WINDOW: 2025-05-12 -> 2025-09-30.
            # Non-Stop volume exists but was rung on numbered registers and is
            # NOT recoverable -- no single register shows a matching step change,
            # so it was spread across the floor. In-Store figures in this window
            # are "In-Store + unattributed Non-Stop".
            "dark_window": ("2025-05-12", "2025-09-30"),
            "dark_window_recoverable": False,
        },
    },
    "loyalty": {
        # Alpine IQ go-live, confirmed by Kaleo.
        # Consistent with the data: 0 rows on 2024-07-23/24 (pre-launch),
        # 101 rows on 2025-07-23/24.
        "launched": "2024-08-10",
        "data_visible_from": "2024-08-10",
    },
}

# ---------------------------------------------------------------------------
# INVENTORY
# ---------------------------------------------------------------------------
SELLABLE_ROOMS = ["SALES FLOOR", "VAULT", "DAY VAULT"]

# ---------------------------------------------------------------------------
# EXPORT SIGNATURES
# Header row position VARIES: reports that carry Location as a column start at
# row 3; reports with a "Location:" preamble line start at row 4. Detected by
# signature match rather than assumed.
#
# scope="chain" -> one export covers all stores, MUST be filtered by Location
#                  before joining or product rows fan out (measured 3.72x).
# scope="store" -> one export per store.
# ---------------------------------------------------------------------------
EXPORTS = {
    "dispensations": {
        "scope": "store",
        "signature": ["ReceiptNo", "ReceiptDate", "Product", "Qty"],
    },
    "breakdown": {
        "scope": "chain",
        "signature": ["Location", "Category", "Product", "GrossSales", "NetSales"],
    },
    "pos_register": {
        "scope": "store",
        "signature": ["PosId", "PosDate", "Register", "PosStatus"],
    },
    "alpine": {
        "scope": "chain",
        "signature": ["Order Number", "Customer ID", "Alpine Discount Amount"],
    },
    "inventory": {
        "scope": "store",
        "signature": ["Package ID", "Product Name", "Quantity on Hand", "Inventory Room"],
    },
    # Inventory Receipt Report - Detail. Carries "Location Name" like the
    # inventory snapshot, so one file may be store-scoped OR chain-wide; the
    # loader splits either way. Signature columns exist in no other export.
    "inventory_receipt": {
        "scope": "chain",
        "signature": ["Product SKU", "Receive Date", "Transfer From Location",
                      "Vendor Name"],
    },
}

MAX_HEADER_SCAN_ROWS = 12

# ---------------------------------------------------------------------------
# VALIDATION THRESHOLDS
# ---------------------------------------------------------------------------
THRESHOLDS = {
    'bulk_event_min_lines': 50,
    "product_join_rate":   0.995,   # dispensation lines matched to breakdown
    # TEMPORARY 2026-09-03, was 0.995. Returns produce no dispensation
    # lines, so raw receipt match rate runs 0.991-0.996 in EVERY month
    # Feb 2023 - Aug 2026 (return rate 0.28-0.64%, no trend). Excluding
    # returns, all 43 months clear 0.995 (worst 0.9973). RETIGHTEN to
    # 0.995 once the check excludes PosStatus=Returned from the
    # denominator -- before the post-backfill overnight_rebuild.
    "receipt_join_rate":   0.995,    # receipts matched to POS register
    "unmapped_category":   0,       # raw category strings not in CATEGORY_MAP
    "unknown_channel":     0,       # registers the rules could not classify
    # Reconciliation is banded. Dispensations and the Breakdown are produced by
    # different Dutchie subsystems and never agree perfectly: the Breakdown runs
    # slightly high (period-boundary and void handling differ). Small drift is
    # expected and advisory; large drift means something is actually broken.
    #   <= tolerance -> PASS
    #   <= fail      -> WARN (loads, flagged)
    #   >  fail      -> FAIL (refused)
    "qty_recon_tolerance": 0.001,   # 0.1%
    "qty_recon_fail":      0.005,   # 0.5%
    "net_recon_tolerance": 0.005,   # 0.5%
    "net_recon_fail":      0.020,   # 2.0%
}


# ---------------------------------------------------------------------------
# CONFIG VERSION
# Bump whenever a rule above changes (category map, channel rules, exclusions,
# availability dates, thresholds). Stamped onto every row of load_log so you
# can always tell which rules a given period was built under.
# ---------------------------------------------------------------------------
CONFIG_VERSION = "2026.09.03-1"

# ---------------------------------------------------------------------------
# REPROCESS WINDOW
# Returns and late-posting delivery orders land in prior periods. Every run
# reprocesses the trailing N periods, not just the newest one.
# Inventory is EXEMPT: snapshots are append-only and never late.
# ---------------------------------------------------------------------------
REPROCESS_PERIODS = 2

# ---------------------------------------------------------------------------
# CLOUD
# Folder/sheet identifiers come from environment variables so nothing
# sensitive lives in the repo.
# ---------------------------------------------------------------------------
DRIVE = {
    "inbox_folder_env":   "TTA_DRIVE_INBOX",      # exports land here
    "archive_folder_env": "TTA_DRIVE_ARCHIVE",    # processed files moved here
    "state_folder_env":   "TTA_DRIVE_STATE",      # holds tta.duckdb + lock
    "db_filename":        "tta.duckdb",
    "lock_filename":      "_etl.lock",
}

SHEETS = {
    "workbook_id_env": "TTA_SHEET_ID",
    "tabs": {
        "agg_category_week": "agg_category_week",
        "scorecard":         "scorecard",
        "load_log":          "load_log",
    },
    "max_rows": 200_000,
}

