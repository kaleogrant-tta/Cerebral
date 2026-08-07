"""
loyalty_ingest.py -- write customer tier assignments into the Cerebral DB.

Reads the AIQ persona export and the Dutchie discount-group audit, resolves a
single tier per POS customer, and writes `dim_customer_tier` into the main
DuckDB. Run this locally whenever you pull a fresh persona export; publish.py
then works from the table with no access to the source files.

    python loyalty_ingest.py --db tta.duckdb

Paths are auto-discovered if not given:

    python loyalty_ingest.py --db tta.duckdb --persona "C:\\Users\\User\\Downloads\\2260-...-rp.csv" --audit "C:\\Users\\User\\Downloads\\Customer Discount Group Audit 8_1_2024-8_5_2026.xlsx"

PRIVACY
-------
The persona export contains birth dates, driver's licence numbers, addresses
and phone numbers. NONE of that is read into the database. The written table
holds only: POS customer id, tier, enrolment date, points, order count, spend.
The source CSV must never be placed in the Drive inbox the ETL sweeps.
"""

import os
import re
import sys
import glob
import hashlib
import argparse
import datetime as dt

import duckdb
import pandas as pd

FF_DG_EXACT = "Travel Club Frequent Flyer"
TOGGLE_WINDOW_MIN = 60

PERSONA_GLOB = ["*-rp.csv", "*_rp.csv", "2260-*.csv"]
AUDIT_GLOB = ["*Discount*Group*Audit*.xlsx", "*discount*group*audit*.xlsx"]


def customer_hash(name):
    """Byte-identical to tta_etl.customer_hash.

    Verified against live customer_xwalk rows: 5/5 reproduced. Any change to
    the ETL's version must be mirrored here or the seed silently stops
    matching.
    """
    if not name or pd.isna(name):
        return None
    norm = re.sub(r"\s+", " ", str(name).strip().lower())
    if not norm:
        return None
    return "H" + hashlib.sha1(norm.encode()).hexdigest()[:15]


def usable_name(name):
    """Mirrors tta_etl._learn_identities' usable() filter.

    Rejects single tokens and all-initials, which collide catastrophically.
    """
    if not name or pd.isna(name):
        return False
    parts = str(name).split()
    if len(parts) < 2:
        return False
    return not all(len(p.strip(".")) <= 1 for p in parts)


def _find(globs):
    roots = [".", os.path.expanduser("~/Downloads"), os.path.expanduser("~")]
    hits = []
    for r in roots:
        for g in globs:
            hits.extend(glob.glob(os.path.join(r, "**", g), recursive=True))
    hits = sorted(set(hits), key=os.path.getsize, reverse=True)
    return hits[0] if hits else None


def read_personas(path):
    """Project the persona export down to tier-relevant columns only."""
    con = duckdb.connect()
    safe = os.path.abspath(path).replace("'", "''")
    con.execute("CREATE VIEW p AS SELECT * FROM read_csv_auto('%s', "
                "ignore_errors=true, sample_size=200000)" % safe)
    cols = {r[0] for r in con.execute("DESCRIBE p").fetchall()}

    def c(name, default="NULL"):
        return name if name in cols else default

    dg = '"Discount Groups"' if "Discount Groups" in cols else "NULL"
    fn = "first_name" if "first_name" in cols else "NULL"
    ln = "last_name" if "last_name" in cols else "NULL"
    q = """
    WITH x AS (
      SELECT regexp_extract(src_ids, 'leaflogix:::([0-9]+):::', 1) AS customer_key,
             trim(coalesce(%s,'') || ' ' || coalesce(%s,'')) AS full_name,
             CASE WHEN %s = ? THEN 3
                  WHEN coalesce(loyalty, false) THEN 2 ELSE 1 END AS rank,
             %s AS loyalty_points, %s AS aiq_orders,
             %s AS aiq_spend, %s AS signup_time
      FROM p
    )
    SELECT customer_key,
           max(rank) AS rank,
           any_value(full_name)  AS full_name,
           sum(loyalty_points) AS loyalty_points,
           sum(aiq_orders)     AS aiq_orders,
           sum(aiq_spend)      AS aiq_spend,
           min(signup_time)    AS signup_time
    FROM x
    WHERE customer_key IS NOT NULL AND customer_key <> ''
    GROUP BY 1
    """ % (fn, ln, dg, c("loyalty_points"), c("count_of_sales"),
           c("total_spent_as_member"), c("loyalty_signup_time"))
    df = con.execute(q, [FF_DG_EXACT]).df()
    con.close()

    df["tier"] = df["rank"].map({3: "Frequent Flyer", 2: "Travel Club"}) \
                            .fillna("Non-Loyalty")
    df["customer_key"] = df["customer_key"].astype(str)
    return df.drop(columns=["rank"])


def read_audit(path):
    """FF roster from the Dutchie audit, with register toggles stripped."""
    df = None
    for h in (3, 4, 0):
        head = pd.read_excel(path, header=h, nrows=1)
        if "Customer ID" in head.columns:
            df = pd.read_excel(path, header=h)
            break
    if df is None:
        raise ValueError("no header row found in %s" % path)

    df["ts"] = pd.to_datetime(
        df["Time"].astype(str).str.replace(r"\s+", " ", regex=True),
        format="%b %d %Y %I:%M%p", errors="coerce")
    ff = df[df["Discount Description"] == FF_DG_EXACT].dropna(subset=["ts"])
    ff = ff.sort_values(["Customer ID", "ts"])

    keep = []
    for _, grp in ff.groupby("Customer ID", sort=False):
        rows = grp.to_dict("records")
        pending, drop = None, set()
        for i, r in enumerate(rows):
            if r["Action"] == "Added":
                pending = i
            elif r["Action"] == "Removed" and pending is not None:
                gap = (r["ts"] - rows[pending]["ts"]).total_seconds() / 60.0
                if gap <= TOGGLE_WINDOW_MIN:
                    drop.update({i, pending})
                pending = None
        keep.extend([r for i, r in enumerate(rows) if i not in drop])

    clean = pd.DataFrame(keep)
    if clean.empty:
        return pd.DataFrame(columns=["customer_key", "ff_enrolled_at"])
    clean = clean.sort_values(["Customer ID", "ts"])
    first = clean[clean.Action == "Added"].groupby("Customer ID")["ts"].min()
    last = clean.groupby("Customer ID")["Action"].last()
    out = pd.concat([first.rename("ff_enrolled_at"),
                     last.rename("last_action")], axis=1).reset_index()
    out = out[out.last_action == "Added"]
    out["customer_key"] = out["Customer ID"].astype("int64").astype(str)
    return out[["customer_key", "ff_enrolled_at"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--persona", default=None)
    ap.add_argument("--audit", default=None)
    ap.add_argument("--no-seed", action="store_true",
                    help="skip writing to customer_xwalk")
    a = ap.parse_args()

    persona = a.persona or _find(PERSONA_GLOB)
    audit = a.audit or _find(AUDIT_GLOB)

    if not os.path.exists(a.db):
        print("Database not found: %s" % a.db)
        sys.exit(1)

    print("db      : %s" % a.db)
    print("persona : %s" % (persona or "-- none --"))
    print("audit   : %s" % (audit or "-- none --"))

    if not persona and not audit:
        print("Nothing to ingest.")
        sys.exit(1)

    frames = []
    if persona:
        p = read_personas(persona)
        p["source"] = "aiq_persona"
        print("  personas -> %s POS ids" % "{:,}".format(len(p)))
        print("  " + str(p.tier.value_counts().to_dict()))
        frames.append(p)

    ff = read_audit(audit) if audit else pd.DataFrame()
    if not ff.empty:
        print("  audit    -> %s currently enrolled FF" % "{:,}".format(len(ff)))

    base = frames[0] if frames else pd.DataFrame(
        columns=["customer_key", "tier", "loyalty_points", "aiq_orders",
                 "aiq_spend", "signup_time", "source"])

    if not ff.empty:
        base = base.merge(ff, on="customer_key", how="outer")
        # Register-side enrolment wins even if AIQ has not synced it yet.
        promoted = base.ff_enrolled_at.notna() & base.tier.ne("Frequent Flyer")
        print("  promoted from audit: %s" % "{:,}".format(int(promoted.sum())))
        base.loc[base.ff_enrolled_at.notna(), "tier"] = "Frequent Flyer"
        base["source"] = base["source"].fillna("dutchie_audit")
    else:
        base["ff_enrolled_at"] = pd.NaT

    base["tier"] = base["tier"].fillna("Non-Loyalty")
    base["built_at"] = dt.datetime.now()

    con = duckdb.connect(a.db)

    # --- name-hash expansion ----------------------------------------------
    # fact_line keys most customers by name_hash, not Dutchie id, because
    # Alpine only names a customer on redemption baskets. Tiers keyed solely
    # on Dutchie ids therefore reach a small fraction of transacting rows and
    # everything unmatched silently falls into Non-Loyalty.
    #
    # Two fixes, both applied:
    #   1. seed customer_xwalk so a future ETL rebuild consolidates identity
    #   2. emit tier rows under BOTH keys so the CURRENT fact_line matches
    #      with no rebuild required
    hash_rows = pd.DataFrame()
    if "full_name" in base.columns:
        nm = base[["customer_key", "tier", "full_name"]].dropna(
            subset=["full_name"]).copy()
        nm = nm[nm.full_name.map(usable_name)]
        nm["name_hash"] = nm.full_name.map(customer_hash)
        nm = nm.dropna(subset=["name_hash"])

        # A name mapping to several Dutchie ids is a genuine duplicate-name
        # collision, not a hash collision. Flag, never assign.
        ids_per_hash = nm.groupby("name_hash")["customer_key"].nunique()
        ambiguous = set(ids_per_hash[ids_per_hash > 1].index)
        clean = nm[~nm.name_hash.isin(ambiguous)].drop_duplicates("name_hash")

        print()
        print("name-hash expansion")
        print("  usable names        : %s" % "{:,}".format(len(nm)))
        print("  distinct hashes     : %s" % "{:,}".format(nm.name_hash.nunique()))
        print("  ambiguous (dropped) : %s (%.1f%%)"
              % ("{:,}".format(len(ambiguous)),
                 len(ambiguous) / max(nm.name_hash.nunique(), 1) * 100))
        print("  usable mappings     : %s" % "{:,}".format(len(clean)))

        if not a.no_seed:
            seed = clean.rename(columns={"customer_key": "alpine_id",
                                         "full_name": "display_name"})
            seed = seed[["name_hash", "alpine_id", "display_name"]].copy()
            seed["first_seen"] = dt.date.today()
            seed["last_seen"] = dt.date.today()
            seed["sightings"] = 1
            seed["ambiguous"] = False
            con.execute("""
                CREATE TABLE IF NOT EXISTS customer_xwalk (
                    name_hash VARCHAR PRIMARY KEY, alpine_id VARCHAR,
                    display_name VARCHAR, first_seen DATE, last_seen DATE,
                    sightings INTEGER, ambiguous BOOLEAN)
            """)
            before = con.execute(
                "SELECT COUNT(*) FROM customer_xwalk").fetchone()[0]
            con.register("xw_seed", seed)
            # Same shape as tta_etl._learn_identities: insert only unseen
            # hashes, then latch ambiguity where a conflicting id appears.
            con.execute("""
                INSERT INTO customer_xwalk
                SELECT n.name_hash, n.alpine_id, n.display_name,
                       n.first_seen, n.last_seen, n.sightings, n.ambiguous
                FROM xw_seed n
                LEFT JOIN customer_xwalk c USING (name_hash)
                WHERE c.name_hash IS NULL
            """)
            con.execute("""
                UPDATE customer_xwalk AS c
                SET ambiguous = c.ambiguous OR c.alpine_id <> n.alpine_id
                FROM xw_seed n WHERE n.name_hash = c.name_hash
            """)
            con.unregister("xw_seed")
            after = con.execute(
                "SELECT COUNT(*) FROM customer_xwalk").fetchone()[0]
            conflicts = con.execute(
                "SELECT COUNT(*) FROM customer_xwalk WHERE ambiguous"
            ).fetchone()[0]
            print("  customer_xwalk      : %s -> %s rows (+%s), %s ambiguous"
                  % ("{:,}".format(before), "{:,}".format(after),
                     "{:,}".format(after - before), "{:,}".format(conflicts)))

        # tier rows under the hash key as well
        hash_rows = clean[["name_hash", "tier", "customer_key"]].rename(
            columns={"customer_key": "person_key",
                     "name_hash": "customer_key"})
        hash_rows["ff_enrolled_at"] = pd.NaT
        for c_ in ("loyalty_points", "aiq_orders", "aiq_spend"):
            hash_rows[c_] = float("nan")
        hash_rows["signup_time"] = None
        hash_rows["source"] = "name_hash"
        hash_rows["key_type"] = "name_hash"
        hash_rows["built_at"] = dt.datetime.now()

    if "key_type" not in base.columns:
        base["key_type"] = "pos_id"
    if "person_key" not in base.columns:
        base["person_key"] = base["customer_key"]
    base["person_key"] = base["person_key"].fillna(base["customer_key"])
    if not hash_rows.empty:
        base = base.drop(columns=["full_name"], errors="ignore")
        base = pd.concat([base, hash_rows], ignore_index=True, sort=False)
        base = base.drop_duplicates("customer_key", keep="first")
    else:
        base = base.drop(columns=["full_name"], errors="ignore")

    con.execute("DROP TABLE IF EXISTS dim_customer_tier")
    con.execute("""
        CREATE TABLE dim_customer_tier AS
        SELECT CAST(customer_key AS VARCHAR)   AS customer_key,
               CAST(tier AS VARCHAR)           AS tier,
               TRY_CAST(ff_enrolled_at AS TIMESTAMP) AS ff_enrolled_at,
               TRY_CAST(loyalty_points AS DOUBLE)    AS loyalty_points,
               TRY_CAST(aiq_orders AS DOUBLE)        AS aiq_orders,
               TRY_CAST(aiq_spend AS DOUBLE)         AS aiq_spend,
               TRY_CAST(signup_time AS VARCHAR)      AS signup_time,
               CAST(source AS VARCHAR)         AS source,
               CAST(key_type AS VARCHAR)       AS key_type,
               CAST(person_key AS VARCHAR)     AS person_key,
               CAST(built_at AS TIMESTAMP)     AS built_at
        FROM base
    """)
    n = con.execute("SELECT count(*) FROM dim_customer_tier").fetchone()[0]
    print()
    print("wrote dim_customer_tier: %s rows" % "{:,}".format(n))
    print(con.execute(
        "SELECT tier, "
        "SUM(CASE WHEN key_type='pos_id' THEN 1 ELSE 0 END) AS people, "
        "SUM(CASE WHEN key_type='name_hash' THEN 1 ELSE 0 END) AS hash_keys "
        "FROM dim_customer_tier GROUP BY 1 ORDER BY 2 DESC"
    ).df().to_string(index=False))
    print("  (people = real roster; hash_keys are additional join keys for")
    print("   the same people, so total rows exceed the roster by design)")

    cov = con.execute("""
        SELECT COUNT(DISTINCT b.customer_key)                       AS total,
               COUNT(DISTINCT CASE WHEN d.customer_key IS NOT NULL
                                   THEN b.customer_key END)         AS matched,
               SUM(b.basket_net)                                    AS net,
               SUM(CASE WHEN d.customer_key IS NOT NULL
                        THEN b.basket_net ELSE 0 END)               AS net_matched
        FROM fact_basket b
        LEFT JOIN dim_customer_tier d ON d.customer_key = b.customer_key
        WHERE NOT b.is_return
    """).df().iloc[0]
    print()
    print("COVERAGE against fact_basket")
    print("  customers : %s of %s (%.1f%%)"
          % ("{:,}".format(int(cov.matched)), "{:,}".format(int(cov.total)),
             cov.matched / max(cov.total, 1) * 100))
    print("  net sales : $%s of $%s (%.1f%%)"
          % ("{:,.0f}".format(cov.net_matched), "{:,.0f}".format(cov.net),
             cov.net_matched / max(cov.net, 1) * 100))
    if cov.matched / max(cov.total, 1) < 0.5:
        print()
        print("  !! Under half of transacting customers resolve to a tier.")
        print("     Everything unmatched falls into Non-Loyalty, which will")
        print("     overstate that tier. Do not publish until this improves.")
    con.close()
    print()
    print("Next: python publish.py   (loyalty tables build automatically)")


if __name__ == "__main__":
    main()
