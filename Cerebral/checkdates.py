import duckdb
con = duckdb.connect(r"C:\Users\User\cerebral\tta.duckdb", read_only=True)
print(con.execute("""
    SELECT CAST(txn_ts AS DATE) d, COUNT(*) n
    FROM fact_line WHERE txn_ts >= DATE '2026-07-20'
    GROUP BY 1 ORDER BY 1
""").df().to_string())
