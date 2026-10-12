#!/usr/bin/env python3
"""
smoke_test.py -- run the whole Cerebral app headlessly against the fake data
and fail if anything throws.

    python dev/smoke_test.py                 # every section, all four stores
    python dev/smoke_test.py --each-store    # ...then again for each store alone
    python dev/smoke_test.py --only Loyalty,Events
    python dev/smoke_test.py -v              # also print info/warning text and empty queries

How it works
  * Streamlit's AppTest runs cerebral_public.py in-process (no browser).
  * duckdb.connect is wrapped so every query the app sends is logged, and any
    request for cerebral_dash.duckdb is pointed at the fixture, so a real data
    file lying next to the app can never leak into a test run.
  * It copes with both layouts of the app: the old row of st.tabs (everything
    renders in one pass) and the newer pill strip (one section renders at a
    time; the script picks each pill in turn).

Exit code 0 = no exceptions and no st.error boxes. 1 = something broke.
"""
from __future__ import annotations

import os
os.environ.setdefault("STREAMLIT_LOGGER_LEVEL", "error")   # silence deprecation chatter
import argparse
import collections
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent
DEFAULT_FIXTURE = HERE / "fixture" / "cerebral_dash.duckdb"

logging.disable(logging.CRITICAL)           # streamlit's deprecation/"bare mode" chatter


# ------------------------------------------------------------------ query log
QUERIES: list[dict] = []          # {section, sql, rows, error}
CURRENT = {"section": "(startup)"}


def install_duckdb_spy(fixture: Path) -> None:
    import duckdb
    real_connect = duckdb.connect

    class Result:
        def __init__(self, res, sql):
            self._r, self._sql = res, sql

        def df(self, *a, **k):
            d = self._r.df(*a, **k)
            QUERIES.append(dict(section=CURRENT["section"], sql=self._sql, rows=len(d), error=None))
            return d

        def __getattr__(self, item):
            return getattr(self._r, item)

    class Conn:
        def __init__(self, con):
            self._c = con

        def execute(self, sql, *a, **k):
            try:
                res = self._c.execute(sql, *a, **k)
            except Exception as exc:
                QUERIES.append(dict(section=CURRENT["section"], sql=str(sql), rows=None,
                                    error=f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"))
                raise
            return Result(res, str(sql))

        def __getattr__(self, item):
            return getattr(self._c, item)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._c.close()

    def connect(database=":memory:", *a, **k):
        db = str(database)
        if db.endswith("cerebral_dash.duckdb"):
            db = str(fixture)
            k.setdefault("read_only", True)
            return Conn(real_connect(db, *a, **k))
        return real_connect(database, *a, **k)

    duckdb.connect = connect


# ------------------------------------------------------------------ helpers
def walk_elements(container):
    """Every element under a container, depth first."""
    out = []

    def walk(e):
        for c in e.children.values():
            out.append(c)
            if getattr(c, "children", None):
                walk(c)
    walk(container)
    return out


def count_elements(container) -> collections.Counter:
    return collections.Counter(getattr(c, "type", type(c).__name__) for c in walk_elements(container))


def clean(v) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(v))).strip()


def texts(elems) -> list[str]:
    return [clean(e.value) for e in elems]


EMPTY_RX = re.compile(r"(no data|not published|no [a-z\- ]{0,40}(yet|found|available|published)|predates|"
                      r"nothing to show|not built|rebuild|isn't available|is not available|needs a refresh)", re.I)


def find_pills(at):
    groups = at.get("button_group")
    for g in groups:
        opts = list(getattr(g, "options", []) or [])
        if len(opts) >= 5:
            return g
    return None


def run(at):
    CURRENT_CACHE_CLEAR()
    at.run()


def CURRENT_CACHE_CLEAR():
    import streamlit as st
    try:
        st.cache_data.clear()
    except Exception:
        pass


def summarise(label, at, container, verbose, rows):
    els = walk_elements(container)
    by = collections.defaultdict(list)
    for e in els:
        by[getattr(e, "type", type(e).__name__)].append(e)
    exc = texts(at.exception)
    err = [clean(e.value) for e in by["error"]] or texts(at.error)
    warn = [clean(e.value) for e in by["warning"]]
    info = [clean(e.value) for e in by["info"]]
    md = [clean(e.value) for e in by["markdown"] + by["caption"]]
    heads = sum(1 for e in by["markdown"] if str(e.value).lstrip().startswith("#"))
    empties = [t for t in warn + info + md if EMPTY_RX.search(t) and len(t) < 400]
    status = "FAIL" if exc or err else "ok"
    rows.append(dict(label=label, status=status, metric=len(by["metric"]),
                     frames=len(by["dataframe"]) + len(by["arrow_data_frame"]) + len(by["table"]),
                     charts=sum(len(v) for k, v in by.items() if "chart" in k and k != "button_group") + len(by["pyplot"]),
                     headings=heads, warn=len(warn), info=len(info), exc=exc, err=err, empties=empties))
    if verbose:
        for w in warn:
            print(f"      warning: {w[:140]}")
        for i in info:
            print(f"      info:    {i[:140]}")


def print_table(rows):
    w = max(len(r["label"]) for r in rows)
    print(f"\n{'Section'.ljust(w)}  status  metrics  tables  charts  heads  warn  info")
    print("-" * (w + 56))
    for r in rows:
        print(f"{r['label'].ljust(w)}  {r['status']:<6}  {r['metric']:>7}  {r['frames']:>6}  "
              f"{r['charts']:>6}  {r['headings']:>5}  {r['warn']:>4}  {r['info']:>4}")
    notes = [(r["label"], t) for r in rows for t in dict.fromkeys(r["empties"])]
    if notes:
        print("\nText that looks like an empty-state notice (check these are intended):")
        for lab, t in notes:
            print(f"   [{lab}] {t[:150]}")
    for r in rows:
        for e in r["exc"]:
            print(f"\n!! EXCEPTION in {r['label']}:\n   {e[:600]}")
        for e in r["err"]:
            print(f"\n!! st.error in {r['label']}:\n   {e[:400]}")


def query_report(verbose):
    missing = collections.Counter()
    probes = collections.Counter()
    errors = []
    empty = collections.defaultdict(list)
    for q in QUERIES:
        tbls = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z_0-9]*)", q["sql"], re.I)
        t = tbls[0] if tbls else "?"
        if q["error"]:
            if "Catalog" in q["error"] or "does not exist" in q["error"]:
                if t.startswith("dash_"):
                    missing[t] += 1
                    errors.append((q["section"], t, q["error"]))
                else:
                    probes[t] += 1      # optional local-only table; the app handles its absence
                continue
            errors.append((q["section"], t, q["error"]))
        elif q["rows"] == 0:
            empty[t].append((q["section"], q["sql"]))
    print(f"\nQueries seen: {len(QUERIES)}")
    if errors:
        print(f"Query ERRORS ({len(errors)}):")
        for sec, t, e in sorted(set(errors)):
            print(f"   [{sec}] {t}: {e}")
    if missing:
        print("Tables the app asked for that the fixture lacks:",
              ", ".join(f"{t} (x{n})" for t, n in missing.items()))
    if probes:
        print("Optional non-dash tables the app probed and handled:",
              ", ".join(f"{t} (x{n})" for t, n in probes.items()))
    if empty:
        print("Queries that came back with 0 rows (may be fine -- a filter can legitimately match nothing):")
        for t, lst in sorted(empty.items()):
            secs = sorted({s for s, _ in lst})
            print(f"   {t:<30} x{len(lst):<3} in {', '.join(secs)}")
            if verbose:
                for _, sql in lst[:2]:
                    print("      " + re.sub(r"\s+", " ", sql)[:170])
    return bool(errors)


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="Headless smoke test for the Cerebral dashboard")
    ap.add_argument("--app", type=Path, default=APP_DIR / "cerebral_public.py",
                    help="app script to test (default: ../cerebral_public.py)")
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--only", help="comma-separated section names to run (default: all)")
    ap.add_argument("--each-store", action="store_true", help="repeat every section with a single store selected")
    ap.add_argument("--timeout", type=int, default=300, help="seconds allowed per page run")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if not a.fixture.exists():
        print(f"Fixture not found at {a.fixture} -- building it first ...")
        subprocess.check_call([sys.executable, str(HERE / "make_fixture.py"), "--out", str(a.fixture)])

    app = a.app.resolve()
    sys.path.insert(0, str(app.parent))
    os.chdir(a.fixture.parent)              # app also looks in the launch directory
    install_duckdb_spy(a.fixture)

    from streamlit.testing.v1 import AppTest
    t0 = time.time()
    at = AppTest.from_file(str(app), default_timeout=a.timeout)
    CURRENT["section"] = "(startup)"
    run(at)
    print(f"App script ran in {time.time() - t0:.1f}s")

    failed = False
    all_rows: list[dict] = []

    def one_pass(tag: str):
        nonlocal failed
        rows: list[dict] = []
        pills = find_pills(at)
        if at.tabs and not pills:
            # ---- legacy layout: every tab renders on every run
            print(f"Layout: st.tabs ({len(at.tabs)} tabs)")
            for t in at.tabs:
                summarise(t.label, at, t, a.verbose, rows)
            # exceptions are page level in this layout; attribute them to the first row
            pg_exc, pg_err = texts(at.exception), texts(at.error)
            for r in rows:
                r["exc"], r["err"] = [], []
            if rows and (pg_exc or pg_err):
                rows[0]["exc"], rows[0]["err"], rows[0]["status"] = pg_exc, pg_err, "FAIL"
        elif pills:
            labels = list(pills.options)
            key = pills.key
            print(f"Layout: pill navigation ({len(labels)} sections), key={key!r}")
            only = {s.strip().lower() for s in a.only.split(",")} if a.only else None
            for lab in labels:
                if only and lab.lower() not in only:
                    continue
                CURRENT["section"] = f"{tag}{lab}"
                at.session_state[key] = lab
                t1 = time.time()
                run(at)
                print(f"  ran {lab:<22} {time.time() - t1:5.1f}s", flush=True)
                summarise(lab, at, at.main, a.verbose, rows)
        else:
            print("Could not find st.tabs or a section pill strip -- showing the page as one block.")
            summarise("(page)", at, at.main, a.verbose, rows)
        if tag:
            for r in rows:
                r["label"] = tag + r["label"]
        all_rows.extend(rows)
        print_table(rows)
        if any(r["status"] == "FAIL" for r in rows):
            failed = True

    one_pass("")

    if a.each_store:
        ms = at.sidebar.multiselect
        if not ms:
            print("No store picker found in the sidebar; skipping --each-store")
        else:
            names = list(ms[0].options)
            for nm in names:
                print(f"\n===== only {nm} =====")
                ms[0].set_value([nm])
                CURRENT["section"] = f"[{nm}] "
                one_pass(f"[{nm}] ")
            ms[0].set_value(names)

    qerr = query_report(a.verbose)
    bad = [r for r in all_rows if r["status"] == "FAIL"]
    print("\nRESULT:", "FAIL" if (failed or bad) else "PASS",
          f"({len(all_rows)} section runs, {len(bad)} failing, {len(QUERIES)} queries, "
          f"{time.time() - t0:.0f}s total)")
    return 1 if (failed or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
