import duckdb
for p in [r"cerebral_dash.duckdb", r"Cerebral\cerebral_dash.duckdb"]:
    print("="*30, p)
    try:
        con = duckdb.connect(p, read_only=True)
        cols = [c[1] for c in con.execute("PRAGMA table_info(dash_offer_performance)").fetchall()]
        print("has product col:", "product" in cols)
        print("total redemptions:", con.execute("SELECT SUM(redemptions) FROM dash_offer_performance").fetchone()[0])
        print("ruby rows:", con.execute("SELECT COUNT(*) FROM dash_offer_performance WHERE lower(brand) LIKE '%ruby%'").fetchone()[0])
        con.close()
    except Exception as e:
        print("ERR", e)
