"""
publish_audiences.py -- write slim audience/event tables into cerebral_dash.duckdb.

Reads:
    data/cerebral_audiences.duckdb      (built by audience_ingest.py)
    config/audience_event_mapping.xlsx  (event dates, campaigns, costs)

Writes into the published dashboard database:
    dash_audience_cohorts    one row per mapped event
    dash_audience_campaigns  one row per campaign
    dash_audience_returns    return curve, one row per event x day offset
    dash_audience_meta       coverage and caveats for the tab header

Snapshot fallback
-----------------
The scheduled refresh runs publish.py from a clean checkout, where neither the
audiences database nor its source exports exist -- both are local-only and hold
PII. So every successful local run also writes a PII-free CSV snapshot to
config/audience_snapshot/, which IS committed. When the source database is
absent, publish() loads that snapshot instead, and the scheduled refresh
publishes the same tables it would have built.

The snapshot is aggregate-only: no contact ids, no phones, no emails, no POS
ids. Refresh it by re-running this script locally after ingesting new events.

NO PII is published. Contact ids, phones, emails and POS ids stay in the local
audiences database and never reach the dashboard. The published tables are
aggregates only.

Usage:
    python publish_audiences.py --src data/cerebral_audiences.duckdb \
        --mapping config/audience_event_mapping.xlsx --dash cerebral_dash.duckdb
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

import audience_metrics as am

log = logging.getLogger(__name__)

RETURN_DAYS = 90
SNAPSHOT_DIR = Path("config/audience_snapshot")
TABLES = ("cohorts", "campaigns", "returns", "meta")


def build_returns(con: duckdb.DuckDBPyConnection, mapping: pd.DataFrame) -> pd.DataFrame:
    """Cumulative distinct returners by day offset, per event."""
    frames = []
    for r in mapping.itertuples():
        aid, ev = str(r.audience_id), r.event_date
        df = con.execute(
            """
            SELECT CAST(sold_at AS DATE) AS d, contact_id
            FROM audience_sales
            WHERE audience_id = ? AND sold_at IS NOT NULL
              AND CAST(sold_at AS DATE) > ? AND CAST(sold_at AS DATE) <= ?
            """,
            [aid, ev, ev + timedelta(days=RETURN_DAYS)],
        ).df()
        if df.empty:
            continue
        df["offset"] = (pd.to_datetime(df["d"]).dt.date - ev).map(lambda x: x.days)
        first = df.groupby("contact_id")["offset"].min().reset_index()
        curve = (
            first.groupby("offset")["contact_id"].count().reindex(
                range(1, RETURN_DAYS + 1), fill_value=0
            ).cumsum().reset_index()
        )
        curve.columns = ["day_offset", "cum_returners"]
        curve["audience_id"] = aid
        curve["event_name"] = str(getattr(r, "event_name", "") or "")
        frames.append(curve)
    if not frames:
        return pd.DataFrame(
            columns=["audience_id", "event_name", "day_offset", "cum_returners"]
        )
    return pd.concat(frames, ignore_index=True)


def _write_snapshot(frames: dict[str, pd.DataFrame], where: Path = SNAPSHOT_DIR) -> None:
    where.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        df.to_csv(where / f"{name}.csv", index=False)
    log.info("snapshot refreshed at %s", where)


def _read_snapshot(where: Path = SNAPSHOT_DIR) -> dict[str, pd.DataFrame] | None:
    if not all((where / f"{n}.csv").exists() for n in TABLES):
        return None
    out = {n: pd.read_csv(where / f"{n}.csv") for n in TABLES}
    for col in ("event_date",):
        if col in out["cohorts"]:
            out["cohorts"][col] = pd.to_datetime(out["cohorts"][col]).dt.date
    log.info("using committed snapshot from %s", where)
    return out


def _write(dash: Path, frames: dict[str, pd.DataFrame]) -> None:
    out = duckdb.connect(str(dash))
    for short, df in frames.items():
        name = f"dash_audience_{short}"
        out.register("_stage", df)
        out.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _stage")
        out.unregister("_stage")
        log.info("wrote %s (%d rows)", name, len(df))
    out.close()


def publish(src: Path, mapping_path: Path, dash: Path) -> dict:
    if not Path(src).exists() or not Path(mapping_path).exists():
        snap = _read_snapshot()
        if snap is None:
            raise FileNotFoundError(
                f"no source at {src} and no snapshot at {SNAPSHOT_DIR}"
            )
        _write(dash, snap)
        return {k: len(v) for k, v in snap.items()}

    cohorts = am.build(src, mapping_path)
    campaigns = am.rollup_by_campaign(cohorts, am.load_campaigns(mapping_path, src))

    con = duckdb.connect(str(src), read_only=True)
    mapping = am.load_mapping(mapping_path)
    returns = build_returns(con, mapping)
    con.close()

    total_roster = int(cohorts["roster"].sum())
    total_pos = int(cohorts["with_pos_record"].sum())
    meta = pd.DataFrame([{
        "generated_at": pd.Timestamp.now(),
        "events_mapped": len(cohorts),
        "audiences_total": len(pd.read_excel(mapping_path, sheet_name="Mapping")),
        "attendees": total_roster,
        "attendees_with_pos_record": total_pos,
        "pct_never_transacted": round(100 * (1 - total_pos / total_roster), 1) if total_roster else None,
        "new_customers": int(cohorts["new_customers"].sum()),
        "revenue_90d": float(cohorts["revenue_90d"].sum()),
        "campaigns_costed": int(campaigns["net_cost_to_tta"].notna().sum()),
        "date_min": str(cohorts["event_date"].min()),
        "date_max": str(cohorts["event_date"].max()),
    }])

    frames = {"cohorts": cohorts, "campaigns": campaigns,
              "returns": returns, "meta": meta}
    _write(dash, frames)
    _write_snapshot(frames)

    return {k: len(v) for k, v in frames.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path("data/cerebral_audiences.duckdb"))
    ap.add_argument("--mapping", type=Path, default=Path("config/audience_event_mapping.xlsx"))
    ap.add_argument("--dash", type=Path, default=Path("cerebral_dash.duckdb"))
    a = ap.parse_args()

    res = publish(a.src, a.mapping, a.dash)
    print(f"\npublished to {a.dash}")
    for k, v in res.items():
        print(f"  {k:12s} {v:>5} rows")
