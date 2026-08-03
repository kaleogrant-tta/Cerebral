import subprocess, sys, glob, os
folders = sorted(f for f in glob.glob("history/*") if os.path.isdir(f)
                 and any(x.endswith((".xlsx",".xls")) for x in os.listdir(f)))
print(f"\n{len(folders)} period(s) to load, two passes.\n")
for p in (1, 2):
    print(f"{'='*60}\nPASS {p} of 2 — {'building the identity crosswalk' if p==1 else 'applying it retroactively'}\n{'='*60}")
    for i, f in enumerate(folders, 1):
        period = os.path.basename(f)
        print(f"  [{i}/{len(folders)}] {period}", flush=True)
        r = subprocess.run([sys.executable, "tta_etl.py", "--inbox", f,
                            "--db", "tta.duckdb", "--period", period],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if any(k in line for k in ("FAIL", "WARN", "ERROR")):
                print("      " + line.strip())
print(f"\n{'='*60}\nRebuilding dashboard file\n{'='*60}")
subprocess.run([sys.executable, "publish.py"])
print("\nDone. Run:  python -m streamlit run cerebral_public.py\n")
