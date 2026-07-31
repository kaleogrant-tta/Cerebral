import duckdb
con = duckdb.connect(r"C:\Users\User\cerebral\Cerebral\tta.duckdb", read_only=True)
print(con.execute("""
    SELECT period, store_key, lines, baskets, passed, warnings, loaded_at
    FROM load_log WHERE period = '2026-07'
    ORDER BY loaded_at DESC
""").df().to_string())