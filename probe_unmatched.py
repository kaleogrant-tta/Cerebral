import duckdb
con = duckdb.connect(r"tta.duckdb", read_only=True)
rows = con.execute("""
    SELECT offer_name,
           COUNT(*) AS redemptions,
           ROUND(SUM(redeem_amt), 2) AS dollars,
           MIN(date_key) AS first_seen,
           MAX(date_key) AS last_seen
    FROM fact_redemption
    WHERE matched_brand IS NULL
    GROUP BY 1
    ORDER BY dollars DESC
    LIMIT 25
""").fetchall()
for r in rows:
    print(r)
print("TOTAL UNMATCHED:", con.execute("SELECT COUNT(*), ROUND(SUM(redeem_amt),2) FROM fact_redemption WHERE matched_brand IS NULL").fetchone())
con.close()
