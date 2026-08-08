"""
overnight_rebuild.py -- unattended full rebuild of tta.duckdb.

Rebuilds every period through tta_etl.py so the newly-seeded customer_xwalk
(363,629 added mappings) is applied to customer_key at load time. Then
re-ingests tiers and republishes.

ALWAYS run the check first:

    python overnight_rebuild.py --check

That verifies source folders exist for every period in load_log and prints a
GO / NO-GO. It touches nothing. If it says GO:

    python overnight_rebuild.py

Everything is logged to rebuild_<timestamp>.log. The database is backed up
before the first period runs, and restored automatically if any period fails.

    python overnight_rebuild.py --restore     put the backup back manually

WHY A REBUILD
-------------
tta_etl line 617 resolves identity as alpine_id -> crosswalk -> name_hash.
Hashes that previously fell through to the name_hash fallback now resolve via
the crosswalk and are written as real POS ids. Roughly 375k fragmented
identities should consolidate, which also fixes the inflated new-customer
counts and deflated repeat rates the ETL warns about at line 370.
"""

import argparse
import datetime as dt
import glob
import os
import re
import shutil
import subprocess
import sys
import time

import json

import duckdb

DB = "../tta.duckdb"
STATE = ".rebuild_state.json"
ETL = "tta_etl.py"
INGEST = "loyalty_ingest.py"
PUBLISH = "publish.py"

# A period folder must contain files that look like these exports.
SIGNATURES = [
    ("dispensations", ["dispensation"]),
    ("breakdown", ["breakdown", "detailed sales"]),
    ("pos", ["pos transaction", "transactions by register", "register"]),
]

# Where period folders might live.
SEARCH_ROOTS = [".", "..", "./history", "../history", "../inbox",
                "../archive", "../data", os.path.expanduser("~/Downloads")]

# Periods with no source folder by design. The scheduled period is written by
# the daily Actions run from the Drive inbox, which is emptied after each run.
# It is NOT deleted by skipping it -- tta_etl only deletes the period it is
# rebuilding -- so its rows survive untouched and the next scheduled run
# rewrites them against the seeded crosswalk anyway.
SKIP_PERIODS = {"scheduled", "unnamed"}


def load_state():
    """Periods already rebuilt in a prior --no-restore run."""
    if not os.path.exists(STATE):
        return {"done": [], "backup": None}
    try:
        with open(STATE) as f:
            d = json.load(f)
        d.setdefault("done", [])
        d.setdefault("backup", None)
        return d
    except Exception:
        return {"done": [], "backup": None}


def save_state(d):
    try:
        with open(STATE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def clear_state():
    if os.path.exists(STATE):
        os.remove(STATE)


def log(msg, fh=None):
    line = "%s  %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def periods_from_db(db):
    if not os.path.exists(db):
        return None, "database not found: %s" % db
    con = duckdb.connect(db, read_only=True)
    try:
        has = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name='load_log'"
        ).fetchone()[0]
        if not has:
            return None, "load_log table missing - cannot enumerate periods"
        rows = con.execute(
            "SELECT period, MIN(loaded_at) AS first_loaded, SUM(lines) AS lines "
            "FROM load_log GROUP BY 1 ORDER BY 2"
        ).fetchall()
        return rows, None
    finally:
        con.close()


def folder_looks_like_inbox(path):
    try:
        names = [n.lower() for n in os.listdir(path)]
    except Exception:
        return False
    hits = 0
    for _, pats in SIGNATURES:
        if any(any(p in n for p in pats) for n in names):
            hits += 1
    return hits >= 2


def find_candidate_folders():
    seen, out = set(), []
    for root in SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for path in [root] + glob.glob(os.path.join(root, "*")) + \
                glob.glob(os.path.join(root, "*", "*")):
            if not os.path.isdir(path):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            if folder_looks_like_inbox(path):
                out.append(path)
    return sorted(out)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def match_periods(periods, folders):
    """Pair each load_log period with a folder whose name contains it.

    Returns (pairs, unmatched, skipped). Skipped periods are expected to have
    no folder and do not block the run.
    """
    pairs, unmatched, skipped = [], [], []
    for period, loaded, lines in periods:
        if str(period).strip().lower() in SKIP_PERIODS:
            skipped.append((period, lines))
            continue
        want = norm(period)
        hit = None
        for f in folders:
            if want and want in norm(os.path.basename(f)):
                hit = f
                break
        if hit:
            pairs.append((period, hit, lines))
        else:
            unmatched.append((period, lines))
    return pairs, unmatched, skipped


def check(db):
    print("=" * 72)
    print("REBUILD FEASIBILITY CHECK")
    print("=" * 72)

    for f in (ETL, INGEST, PUBLISH):
        print("  %-24s %s" % (f, "found" if os.path.exists(f) else "MISSING"))
    if not os.path.exists(ETL):
        print("\nNO-GO: run this from the folder containing tta_etl.py")
        return 1

    periods, err = periods_from_db(db)
    if err:
        print("\nNO-GO: %s" % err)
        return 1

    print()
    print("  periods in load_log: %d" % len(periods))

    con = duckdb.connect(db, read_only=True)
    xw = con.execute("SELECT COUNT(*) FROM customer_xwalk").fetchone()[0]
    src = con.execute(
        "SELECT customer_source, COUNT(DISTINCT customer_key) "
        "FROM fact_line GROUP BY 1"
    ).fetchall()
    con.close()
    print("  customer_xwalk rows: %s" % "{:,}".format(xw))
    for s, n in src:
        print("    %-16s %s customers" % (s, "{:,}".format(n)))
    if xw < 100000:
        print()
        print("  !! crosswalk looks unseeded. Run loyalty_ingest.py first,")
        print("     otherwise the rebuild gains nothing.")

    folders = find_candidate_folders()
    print()
    print("  candidate source folders found: %d" % len(folders))
    for f in folders[:20]:
        print("    %s" % f)
    if len(folders) > 20:
        print("    ... and %d more" % (len(folders) - 20))

    pairs, unmatched, skipped = match_periods(periods, folders)
    print()
    print("  matched %d of %d periods" % (len(pairs), len(periods)))
    for p, f, n in pairs[:20]:
        print("    %-16s -> %s" % (p, f))
    if skipped:
        print()
        print("  SKIPPED by design (%d) - rows are left untouched:" % len(skipped))
        for p, n in skipped:
            print("    %-16s (%s lines)" % (p, "{:,}".format(int(n or 0))))
    if unmatched:
        print()
        print("  UNMATCHED - no source folder (%d):" % len(unmatched))
        for p, n in unmatched[:15]:
            print("    %-16s (%s lines)" % (p, "{:,}".format(int(n or 0))))

    print()
    print("=" * 72)
    if not pairs:
        print("NO-GO -- no period folders found on this machine.")
        print()
        print("The source exports are probably in the Drive archive folder")
        print("rather than on disk. Without them the ETL cannot re-derive")
        print("fact_line, and a rebuild is impossible tonight.")
        print()
        print("Nothing was changed. The dual-key tier map already in place")
        print("gives 78.3% coverage, so the Loyalty tab works as-is.")
        return 1
    if unmatched:
        n_lines = sum(int(n or 0) for _, n in unmatched)
        print("PARTIAL -- %d period(s) have no source folder, covering %s lines."
              % (len(unmatched), "{:,}".format(n_lines)))
        print()
        print("Those rows are NOT deleted -- tta_etl only rebuilds the periods")
        print("it is given. They simply keep their existing customer_key, so")
        print("identity stays fragmented for that slice and a person may count")
        print("twice across the rebuilt and non-rebuilt periods.")
        print()
        print("Add the missing folders for a clean result, or proceed anyway:")
        print("    python overnight_rebuild.py --partial")
        return 2
    print("GO -- all %d rebuildable periods have a source folder."
          % len(pairs))
    if skipped:
        print("     (%d period(s) skipped by design, rows left as-is)"
              % len(skipped))
    print()
    print("Run: python overnight_rebuild.py")
    return 0


def run(db, partial=False, resume=False, no_restore=False):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    logpath = "rebuild_%s.log" % stamp
    fh = open(logpath, "w", encoding="utf-8")
    t0 = time.time()

    log("rebuild starting", fh)
    periods, err = periods_from_db(db)
    if err:
        log("ABORT: %s" % err, fh)
        return 1

    folders = find_candidate_folders()
    pairs, unmatched, skipped = match_periods(periods, folders)
    for p, n in skipped:
        log("skipping period %s by design (%s lines, left untouched)"
            % (p, "{:,}".format(int(n or 0))), fh)
    if unmatched and not partial:
        log("ABORT: %d period(s) have no source folder. Run --check."
            % len(unmatched), fh)
        return 1
    for p, n in unmatched:
        log("WARNING: period %s has no folder - not rebuilt, rows kept as-is"
            % p, fh)
    if not pairs:
        log("ABORT: no source folders found.", fh)
        return 1

    state = load_state()
    done = set(state["done"]) if resume else set()
    if resume and done:
        log("resuming: %d period(s) already rebuilt (%s)"
            % (len(done), ", ".join(sorted(done))), fh)
    elif resume:
        log("resume requested but no completed periods recorded - "
            "starting from the beginning", fh)

    # Reuse the original backup when resuming, so a restore still returns the
    # database to its true pre-rebuild state rather than a half-rebuilt one.
    if resume and state.get("backup") and os.path.exists(state["backup"]):
        backup = state["backup"]
        log("reusing original backup: %s" % backup, fh)
    else:
        backup = "%s.prerebuild-%s.bak" % (db, stamp)
        shutil.copy2(db, backup)
        log("backup written: %s (%.0f MB)"
            % (backup, os.path.getsize(backup) / 1e6), fh)
    state["backup"] = backup
    save_state(state)

    def restore(why):
        log("FAILED: %s" % why, fh)
        if no_restore:
            log("--no-restore set: leaving the database as-is.", fh)
            log("%d period(s) rebuilt this session are KEPT."
                % len(state["done"]), fh)
            log("The database is now MIXED - some periods rebuilt, some not.", fh)
            log("Safe to leave, but finish the run before publishing.", fh)
            log("Resume with:  python overnight_rebuild.py --resume "
                "--no-restore", fh)
            log("Roll back with: python overnight_rebuild.py --restore", fh)
            fh.close()
            return 1
        log("restoring backup...", fh)
        shutil.copy2(backup, db)
        log("database restored. No changes kept.", fh)
        clear_state()
        fh.close()
        return 1

    todo = [x for x in pairs if x[0] not in done]
    if not todo:
        log("nothing to do - every period already rebuilt. "
            "Use --reset to start over.", fh)
    for i, (period, folder, _) in enumerate(todo, 1):
        log("[%d/%d] period %s  <-  %s" % (i, len(todo), period, folder), fh)
        cmd = [sys.executable, ETL, "--inbox", folder,
               "--period", period, "--db", db]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=7200)
        except subprocess.TimeoutExpired:
            return restore("period %s exceeded 2h" % period)
        # Full stdout: the ETL prints a check block per store, and truncating
        # hides which store failed.
        for ln in (r.stdout or "").splitlines():
            fh.write("      %s\n" % ln)
        if r.returncode != 0:
            fh.write((r.stderr or "") + "\n")
            fails = [ln.strip() for ln in (r.stdout or "").splitlines()
                     if "[FAIL]" in ln]
            for ln in fails:
                log("      %s" % ln, fh)
            return restore("tta_etl.py exit %d on period %s"
                           % (r.returncode, period))
        log("      ok", fh)
        state["done"] = sorted(set(state["done"]) | {period})
        save_state(state)

    log("all periods rebuilt (%.1f min)" % ((time.time() - t0) / 60), fh)

    # --- identity outcome --------------------------------------------------
    con = duckdb.connect(db, read_only=True)
    rows = con.execute(
        "SELECT customer_source, COUNT(DISTINCT customer_key) "
        "FROM fact_line GROUP BY 1 ORDER BY 2 DESC").fetchall()
    total = con.execute(
        "SELECT COUNT(DISTINCT customer_key) FROM fact_line").fetchone()[0]
    con.close()
    log("identity after rebuild: %s distinct customers" % "{:,}".format(total), fh)
    for s, n in rows:
        log("    %-16s %s (%.1f%%)"
            % (s, "{:,}".format(n), n / max(total, 1) * 100), fh)

    # --- tiers -------------------------------------------------------------
    # Events calendar, then tiers -- tiers must come after the rebuild
    # because customer_key values change when identity consolidates.
    if os.path.exists("events_ingest.py"):
        log("re-ingesting events", fh)
        re_ = subprocess.run([sys.executable, "events_ingest.py", "--db", db],
                             capture_output=True, text=True, timeout=1800)
        fh.write((re_.stdout or "")[-3000:] + "\n")
        if re_.returncode != 0:
            log("  events ingest failed (continuing)", fh)

    log("re-ingesting tiers", fh)
    r = subprocess.run([sys.executable, INGEST, "--db", db],
                       capture_output=True, text=True, timeout=3600)
    fh.write((r.stdout or "")[-6000:] + "\n")
    if r.returncode != 0:
        fh.write((r.stderr or "")[-4000:] + "\n")
        return restore("loyalty_ingest.py failed")
    for ln in (r.stdout or "").splitlines():
        if "customers :" in ln or "net sales :" in ln:
            log("   %s" % ln.strip(), fh)

    # --- publish -----------------------------------------------------------
    log("publishing", fh)
    # Pass --db explicitly: publish.py defaults to tta.duckdb in the working
    # directory, which is not necessarily the database just rebuilt.
    r = subprocess.run([sys.executable, PUBLISH, "--db", db],
                       capture_output=True, text=True, timeout=3600)
    fh.write((r.stdout or "")[-8000:] + "\n")
    if r.returncode != 0:
        fh.write((r.stderr or "")[-4000:] + "\n")
        log("publish failed -- database itself is FINE and rebuilt.", fh)
        log("Backup kept at %s if you want to roll back." % backup, fh)
        fh.close()
        return 1

    clear_state()
    log("DONE in %.1f min" % ((time.time() - t0) / 60), fh)
    log("backup kept: %s" % backup, fh)
    log("log: %s" % logpath, fh)
    fh.close()
    return 0


def restore_cmd(db):
    baks = sorted(glob.glob(db + ".prerebuild-*.bak"))
    if not baks:
        print("no pre-rebuild backup found")
        return 1
    shutil.copy2(baks[-1], db)
    print("restored %s from %s" % (db, baks[-1]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--partial", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="skip periods already rebuilt in a --no-restore run")
    ap.add_argument("--no-restore", action="store_true",
                    help="on failure, keep completed periods instead of "
                         "rolling back (debug loop; leaves a mixed database)")
    ap.add_argument("--reset", action="store_true",
                    help="forget recorded progress")
    a = ap.parse_args()

    if a.reset:
        clear_state()
        print("progress cleared")
        return 0
    if a.restore:
        clear_state()
        return restore_cmd(a.db)
    if a.check:
        return check(a.db)
    return run(a.db, a.partial, a.resume, a.no_restore)


if __name__ == "__main__":
    sys.exit(main())
