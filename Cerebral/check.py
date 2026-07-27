import duckdb
d = duckdb.connect("cerebral_dash.duckdb", read_only=True)
tables = [r[0] for r in d.execute("SHOW TABLES").fetchall()]
red = [t for t in tables if "redem" in t or "offer" in t]
print(f"dashboard file: {len(tables)} tables")
print("redemption tables:", red if red else "NONE")
for t in red:
    n = d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"   {t}: {n} rows")
s = duckdb.connect("tta.duckdb", read_only=True)
has = s.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = 'fact_redemption'").fetchone()[0]
print("\nfact_redemption in tta.duckdb:", "yes" if has else "no — run rebuild.py")
if has:
    print("   rows:", s.execute("SELECT COUNT(*) FROM fact_redemption").fetchone()[0])
