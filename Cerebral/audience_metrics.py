"""
audience_metrics.py — event cohort metrics from ingested AIQ audience data.

Answers, per event:
    who attended, how many were new, did they buy that day,
    did they come back, what did they spend, what did it cost per customer.

Depends on audience_ingest.py having populated audience_members /
audience_pos_ids / audience_sales, and on the audience_event_mapping workbook
supplying the true event date.

Why the mapping sheet is required
---------------------------------
AIQ's audience `Created` timestamp is when the list was uploaded, not when the
event happened. Observed lag across 31 audiences: 1 to 47 days, and one case of
103 days ("11/15-Event", created in February for a November event). Using
Created as the event date silently misdates every metric.

Return windows anchor on each attendee's first purchase on or after the event,
matching publish_event_return.py. See the comment in compute_cohort().

`detect_event_date()` recovers the date independently — the day the most
distinct roster members transacted. Where a cohort has enough purchasers this
lands exactly on the calendar event, so it doubles as a check on the mapping.
Below ~7 purchasers it is noise and must not be trusted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

log = logging.getLogger(__name__)

PEAK_MIN_CUSTOMERS = 7      # below this, peak detection is noise
LOW_POS_RATE = 0.50         # below this, most attendees never transacted at all
RETURN_WINDOWS = (30, 60, 90)


@dataclass
class CohortMetrics:
    audience_id: str
    event_name: str
    event_date: date
    store: str
    event_type: str
    campaign: str

    roster: int
    with_pos_record: int
    pos_record_rate: float

    ever_purchased: int
    new_customers: int
    existing_customers: int

    same_day_buyers: int
    same_day_receipts: int
    same_day_revenue: float

    returned_30d: int
    returned_60d: int
    returned_90d: int
    revenue_90d: float

    # 90-day outcomes split by whether the attendee was already a customer
    new_returned_90d: int
    new_revenue_90d: float
    existing_returned_90d: int
    existing_revenue_90d: float

    detected_peak_date: date | None
    peak_customers: int
    date_check: str
    warnings: str

    def as_row(self) -> dict:
        return asdict(self)


def load_mapping(path: Path) -> pd.DataFrame:
    """Read the mapping workbook and keep only rows cleared for analysis."""
    df = pd.read_excel(path, sheet_name="Mapping", dtype={"audience_id": str})
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce").dt.date

    unmapped = df["event_date"].isna().sum()
    excluded = (df["purchase_defined"].astype(str).str.upper() == "YES").sum()
    if unmapped:
        log.warning("%d audiences have no event_date and are skipped", unmapped)
    if excluded:
        log.info("%d audiences excluded as purchase-defined", excluded)

    keep = df["event_date"].notna() & (df["purchase_defined"].astype(str).str.upper() != "YES")
    return df[keep].copy()


def detect_event_date(con: duckdb.DuckDBPyConnection, audience_id: str) -> tuple[date | None, int]:
    """Day with the most distinct roster members purchasing."""
    row = con.execute(
        """
        SELECT CAST(sold_at AS DATE) AS d, COUNT(DISTINCT contact_id) AS n
        FROM audience_sales
        WHERE audience_id = ? AND sold_at IS NOT NULL
        GROUP BY 1 ORDER BY n DESC, d ASC LIMIT 1
        """,
        [audience_id],
    ).fetchone()
    return (row[0], int(row[1])) if row else (None, 0)


def _scalar(con, sql: str, params: list) -> float:
    val = con.execute(sql, params).fetchone()[0]
    return float(val or 0)


def compute_cohort(
    con: duckdb.DuckDBPyConnection,
    audience_id: str,
    event_date: date,
    *,
    event_name: str = "",
    store: str = "",
    event_type: str = "",
    campaign: str = "",
) -> CohortMetrics:
    warnings: list[str] = []

    roster = int(_scalar(con, "SELECT COUNT(DISTINCT contact_id) FROM audience_members WHERE audience_id = ?", [audience_id]))
    with_pos = int(_scalar(
        con,
        """
        SELECT COUNT(DISTINCT m.contact_id)
        FROM audience_members m
        JOIN audience_pos_ids p USING (contact_id)
        WHERE m.audience_id = ?
        """,
        [audience_id],
    ))
    pos_rate = with_pos / roster if roster else 0.0
    if roster >= 25 and pos_rate < LOW_POS_RATE:
        warnings.append(
            f"{1 - pos_rate:.0%} of attendees have never transacted at TTA. "
            "This is a result, not a data gap: a persona only receives a POS id "
            "once a POS record exists for it."
        )

    ever = int(_scalar(con, "SELECT COUNT(DISTINCT contact_id) FROM audience_sales WHERE audience_id = ?", [audience_id]))

    # first-ever purchase relative to the event separates new from existing
    firsts = con.execute(
        """
        SELECT contact_id, MIN(CAST(sold_at AS DATE)) AS first_purchase
        FROM audience_sales WHERE audience_id = ? AND sold_at IS NOT NULL
        GROUP BY 1
        """,
        [audience_id],
    ).df()
    if firsts.empty:
        new = existing = 0
    else:
        firsts["first_purchase"] = pd.to_datetime(firsts["first_purchase"]).dt.date
        new = int((firsts["first_purchase"] >= event_date).sum())
        existing = int((firsts["first_purchase"] < event_date).sum())

    same_day = con.execute(
        """
        SELECT COUNT(DISTINCT contact_id), COUNT(DISTINCT transaction_id), COALESCE(SUM(price), 0)
        FROM audience_sales
        WHERE audience_id = ? AND CAST(sold_at AS DATE) = ?
        """,
        [audience_id, event_date],
    ).fetchone()

    # ------------------------------------------------------------------
    # Return behaviour, anchored on each attendee's own first visit
    # ------------------------------------------------------------------
    # The window opens at the attendee's first purchase on or after the event,
    # not at the event date. Anchoring on the event date gives an existing
    # customer who reappears three weeks later only 69 usable days while a
    # same-day buyer gets 90 -- which depresses the existing-customer rate for
    # no reason other than the definition. This matches publish_event_return.py
    # so the two tabs report the same rate.
    #
    # An attendee with no purchase on or after the event has no anchor and is
    # absent from every return figure. They are still counted in the roster.
    anchors = con.execute(
        """
        SELECT contact_id, MIN(CAST(sold_at AS DATE)) AS anchor_day
        FROM audience_sales
        WHERE audience_id = ? AND sold_at IS NOT NULL
          AND CAST(sold_at AS DATE) >= ?
        GROUP BY 1
        """,
        [audience_id, event_date],
    ).df()

    returns = {w: 0 for w in RETURN_WINDOWS}
    rev90 = 0.0
    new_r90 = new_rev90 = ex_r90 = ex_rev90 = 0

    if not anchors.empty:
        anchors["anchor_day"] = pd.to_datetime(anchors["anchor_day"]).dt.date
        sales = con.execute(
            """
            SELECT contact_id, CAST(sold_at AS DATE) AS day,
                   COALESCE(SUM(price), 0) AS spend
            FROM audience_sales
            WHERE audience_id = ? AND sold_at IS NOT NULL
            GROUP BY 1, 2
            """,
            [audience_id],
        ).df()
        sales["day"] = pd.to_datetime(sales["day"]).dt.date
        joined = sales.merge(anchors, on="contact_id", how="inner")
        joined["offset"] = [
            (d - a).days for d, a in zip(joined["day"], joined["anchor_day"])
        ]

        for w in RETURN_WINDOWS:
            in_w = joined[(joined["offset"] > 0) & (joined["offset"] <= w)]
            returns[w] = int(in_w["contact_id"].nunique())

        w90 = joined[(joined["offset"] > 0) & (joined["offset"] <= 90)]
        rev90 = float(w90["spend"].sum())

        if not firsts.empty:
            new_ids = set(firsts.loc[firsts["first_purchase"] >= event_date, "contact_id"])
            ex_ids = set(firsts.loc[firsts["first_purchase"] < event_date, "contact_id"])
            n_w = w90[w90["contact_id"].isin(new_ids)]
            e_w = w90[w90["contact_id"].isin(ex_ids)]
            new_r90 = int(n_w["contact_id"].nunique())
            new_rev90 = float(n_w["spend"].sum())
            ex_r90 = int(e_w["contact_id"].nunique())
            ex_rev90 = float(e_w["spend"].sum())

        # Maturity now runs from the last anchor, since that is the last point
        # at which a 90-day window could have opened.
        last_anchor = max(anchors["anchor_day"])
        if (date.today() - last_anchor).days < 90:
            warnings.append(
                f"the latest attendee anchor is {last_anchor}; 90-day figures "
                "are still accruing for the slowest returners"
            )

    peak_date, peak_n = detect_event_date(con, audience_id)
    if peak_n < PEAK_MIN_CUSTOMERS:
        date_check = "insufficient"
    elif peak_date == event_date:
        date_check = "confirmed"
    elif peak_date and abs((peak_date - event_date).days) <= 2:
        date_check = "near"
    else:
        date_check = "MISMATCH"
        warnings.append(
            f"purchase peak is {peak_date} but the mapping says {event_date} — verify the mapping"
        )

    days_elapsed = (date.today() - event_date).days
    if days_elapsed < 90:
        warnings.append(f"only {days_elapsed} days since the event; 90-day figures are incomplete")

    return CohortMetrics(
        audience_id=audience_id,
        event_name=event_name,
        event_date=event_date,
        store=store,
        event_type=event_type,
        campaign=campaign,
        roster=roster,
        with_pos_record=with_pos,
        pos_record_rate=round(pos_rate, 3),
        ever_purchased=ever,
        new_customers=new,
        existing_customers=existing,
        same_day_buyers=int(same_day[0] or 0),
        same_day_receipts=int(same_day[1] or 0),
        same_day_revenue=round(float(same_day[2] or 0), 2),
        returned_30d=returns[30],
        returned_60d=returns[60],
        returned_90d=returns[90],
        revenue_90d=round(rev90, 2),
        new_returned_90d=new_r90,
        new_revenue_90d=round(new_rev90, 2),
        existing_returned_90d=ex_r90,
        existing_revenue_90d=round(ex_rev90, 2),
        detected_peak_date=peak_date,
        peak_customers=peak_n,
        date_check=date_check,
        warnings="; ".join(warnings),
    )


def matched_control(
    con: duckdb.DuckDBPyConnection,
    audience_id: str,
    event_date: date,
    window_days: int = 90,
) -> pd.DataFrame:
    """Compare each cohort member's own post-event window against their pre-event window.

    This is a within-person before/after, NOT a true control group — it removes
    person-level differences (a whale stays a whale) but not seasonality or
    simple ageing. A real control requires non-attendees matched on
    first-purchase week and pre-event spend decile, which needs the Cerebral
    customer table rather than the audience-scoped export.

    Read this as a sanity check on direction, not as a causal estimate.
    """
    return con.execute(
        """
        WITH windows AS (
            SELECT contact_id,
                   SUM(CASE WHEN CAST(sold_at AS DATE) > ?
                             AND CAST(sold_at AS DATE) <= ? THEN price ELSE 0 END) AS after_spend,
                   SUM(CASE WHEN CAST(sold_at AS DATE) < ?
                             AND CAST(sold_at AS DATE) >= ? THEN price ELSE 0 END) AS before_spend
            FROM audience_sales WHERE audience_id = ?
            GROUP BY 1
        )
        SELECT COUNT(*)                        AS customers,
               ROUND(SUM(before_spend), 2)     AS spend_before,
               ROUND(SUM(after_spend), 2)      AS spend_after,
               ROUND(AVG(after_spend - before_spend), 2) AS avg_delta
        FROM windows
        """,
        [
            event_date, event_date + timedelta(days=window_days),
            event_date, event_date - timedelta(days=window_days),
            audience_id,
        ],
    ).df()


def load_campaigns(path: Path, db_path: Path | None = None,
                   map_csv: Path | str = "event_audience_map.csv") -> pd.DataFrame:
    """Campaign cost, derived from marketing's per-event figures.

    Sums net_tta_cost (and gross_cost) from dim_event across the events each
    campaign's audiences map to. Chain: workbook Mapping sheet `campaign`
    -> audience_id -> event_audience_map.csv -> airtable_record_id ->
    dim_event. Each event is counted once per campaign however many
    audiences point at it.

    Replaces the hand-typed Campaigns sheet, which was the last place a
    second cost figure lived. Returns the same columns rollup_by_campaign
    expects: campaign, gross_cost, net_cost_to_tta -- plus cost_events and
    unrecorded_events so coverage is visible. A campaign whose events all
    have unrecorded cost gets NULL, never 0.
    """
    mp = pd.read_excel(path, sheet_name="Mapping", dtype={"audience_id": str})
    mp["campaign"] = mp.get("campaign", "").fillna("").astype(str).str.strip()
    mp = mp[mp["campaign"] != ""][["audience_id", "campaign"]]
    mp["audience_id"] = mp["audience_id"].astype(str).str.strip()

    empty = pd.DataFrame(columns=["campaign", "gross_cost", "net_cost_to_tta",
                                  "cost_events", "unrecorded_events"])
    mfile = Path(map_csv)
    if not mfile.exists():
        log.warning("%s not found; campaign costs unavailable", mfile)
        return empty
    amap = pd.read_csv(mfile, dtype=str).fillna("")
    amap = amap[amap["event_id"] != ""][["audience_id", "event_id"]]
    amap["airtable_record_id"] = amap["event_id"].str.rsplit("-", n=1).str[0]

    db = Path(db_path) if db_path else next(
        (c for c in (Path("../tta.duckdb"), Path("tta.duckdb")) if c.exists()), None)
    if db is None:
        log.warning("no source database found; campaign costs unavailable")
        return empty
    con = duckdb.connect(str(db), read_only=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info('dim_event')").fetchall()}
        if "airtable_record_id" not in cols or "net_tta_cost" not in cols:
            log.warning("dim_event has no cost columns; re-run events_ingest.py")
            return empty
        ev = con.execute("""
            SELECT airtable_record_id,
                   MIN(gross_cost)    AS gross_cost,
                   MIN(net_tta_cost)  AS net_tta_cost,
                   BOOL_OR(cost_recorded) AS cost_recorded
            FROM dim_event GROUP BY 1
        """).df()
    finally:
        con.close()

    j = (mp.merge(amap, on="audience_id", how="inner")
           .drop_duplicates(["campaign", "airtable_record_id"])
           .merge(ev, on="airtable_record_id", how="left"))
    if j.empty:
        return empty
    out = (j.groupby("campaign")
             .agg(gross_cost=("gross_cost", lambda s: s.sum(min_count=1)),
                  net_cost_to_tta=("net_tta_cost", lambda s: s.sum(min_count=1)),
                  cost_events=("cost_recorded", lambda s: int(s.fillna(False).sum())),
                  unrecorded_events=("cost_recorded",
                                     lambda s: int((~s.fillna(False).astype(bool)).sum())))
             .reset_index())
    part = out[(out.cost_events > 0) & (out.unrecorded_events > 0)]
    if len(part):
        log.info("%d campaign(s) have events with unrecorded cost; their cost is a floor: %s",
                 len(part), ", ".join(part["campaign"]))
    return out


def dedupe_events(cohorts: pd.DataFrame) -> pd.DataFrame:
    """Collapse audiences that map to the same event.

    Several audiences can describe one activation -- the 420 launch has both a
    guest list and a photobooth list, Clear for Takeoff has a sheet and a
    backfill. Reporting them as separate rows double-counts anyone captured
    twice: 706 + 80 for the 420 launch is 786, but the union is 762.

    Counting people once requires the member lists, so this is an upper bound
    corrected only where the same (event_date, store) pair repeats. Use
    dash_event_return for exact unioned counts; use this for the per-event
    view where the split by audience is itself informative.
    """
    key = ["event_date", "store", "event_name"]
    dupes = cohorts.duplicated(subset=["event_date", "store"], keep=False)
    if dupes.any():
        pairs = cohorts.loc[dupes, ["event_date", "store"]].drop_duplicates()
        log.warning(
            "%d event(s) have more than one audience; attendee counts across "
            "those rows overlap and must not be summed: %s",
            len(pairs),
            ", ".join(f"{r.event_date} {r.store}" for r in pairs.itertuples()),
        )
    return cohorts


def rollup_by_campaign(cohorts: pd.DataFrame, campaigns: pd.DataFrame) -> pd.DataFrame:
    """Aggregate cohorts to campaign level and attach cost per acquired customer.

    Costs are budgeted per campaign, not per event — Cali Sober Sips ran at three
    stores, All Things Go was a three-day festival, 420 covered both the OnlyNY
    launch and the four-store food drops. Dividing a campaign budget by a single
    event would invent an allocation nobody agreed to.
    """
    agg = (
        cohorts.groupby("campaign", dropna=False)
        .agg(
            events=("audience_id", "count"),
            attendees=("roster", "sum"),
            mean_pos_rate=("pos_record_rate", "mean"),
            new_customers=("new_customers", "sum"),
            same_day_buyers=("same_day_buyers", "sum"),
            returned_90d=("returned_90d", "sum"),
            revenue_90d=("revenue_90d", "sum"),
            new_returned_90d=("new_returned_90d", "sum"),
            new_revenue_90d=("new_revenue_90d", "sum"),
            existing_returned_90d=("existing_returned_90d", "sum"),
            existing_revenue_90d=("existing_revenue_90d", "sum"),
        )
        .reset_index()
    )
    cost = campaigns[["campaign", "gross_cost", "net_cost_to_tta"]]
    out = agg.merge(cost, on="campaign", how="left")

    out["cost_per_new_customer"] = (out["net_cost_to_tta"] / out["new_customers"]).round(2)
    out["cost_per_returner"] = (out["net_cost_to_tta"] / out["returned_90d"]).round(2)
    out["revenue_per_dollar"] = (out["revenue_90d"] / out["net_cost_to_tta"]).round(2)
    out.loc[out["new_customers"] == 0, "cost_per_new_customer"] = None
    out.loc[out["returned_90d"] == 0, "cost_per_returner"] = None

    out["cost_note"] = ""
    thin = out["mean_pos_rate"] < LOW_POS_RATE
    out.loc[thin, "cost_note"] = (
        "most attendees never transacted; cost is spread over a mostly non-purchasing audience"
    )
    return out.sort_values("campaign").reset_index(drop=True)


def build(db_path: Path, mapping_path: Path) -> pd.DataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    mapping = load_mapping(mapping_path)

    rows = []
    for r in mapping.itertuples():
        rows.append(
            compute_cohort(
                con,
                str(r.audience_id),
                r.event_date,
                event_name=str(getattr(r, "event_name", "") or ""),
                store=str(getattr(r, "store", "") or ""),
                event_type=str(getattr(r, "event_type", "") or ""),
                campaign=str(getattr(r, "campaign", "") or ""),
            ).as_row()
        )
    con.close()

    out = pd.DataFrame(rows).sort_values("event_date").reset_index(drop=True)
    out = dedupe_events(out)
    for w in out[out["warnings"] != ""].itertuples():
        log.warning("%s (%s): %s", w.event_name or w.audience_id, w.event_date, w.warnings)
    return out


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Compute event cohort metrics.")
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    df = build(args.db, args.mapping)
    roll = rollup_by_campaign(df, load_campaigns(args.mapping, args.db))

    if args.out:
        df.to_csv(args.out, index=False)
        roll.to_csv(args.out.with_name(args.out.stem + "_by_campaign.csv"), index=False)
        print(f"\nwrote {args.out} and the campaign rollup alongside it")

    cols = ["event_date", "event_name", "roster", "pos_record_rate", "new_customers",
            "same_day_buyers", "returned_90d", "revenue_90d", "date_check"]
    with pd.option_context("display.width", 220, "display.max_colwidth", 34):
        print("\nPER EVENT")
        print(df[cols].to_string(index=False))
        print("\nBY CAMPAIGN")
        print(roll.drop(columns=["cost_note"]).to_string(index=False))
