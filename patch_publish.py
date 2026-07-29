p = r"C:\Users\User\cerebral\Cerebral\publish.py"
s = open(p, encoding="utf-8-sig").read()

anchor = "    # --- inventory, most recent snapshot only ----------------------------"
new_table = '''
    # --- promo lab: privacy-safe churn aggregates --------------------------
    # Per-customer behaviour reduced to counts per store x category / brand.
    # No customer keys, no basket keys - sums and counts only.
    for _name, _dim in [("dash_promo_category", "category"),
                        ("dash_promo_brand", "brand")]:
        con.execute(f"""
            CREATE TABLE {_name} AS
            WITH mx AS (SELECT MAX(txn_ts) AS t1 FROM src.fact_line WHERE NOT is_return),
            pc AS (
                SELECT l.store_key, l.{_dim} AS dim, l.customer_key,
                       MAX(l.txn_ts)               AS last_ts,
                       COUNT(DISTINCT l.basket_id) AS n,
                       SUM(l.net_sales)            AS spend,
                       SUM(l.gross_margin)         AS gm
                FROM src.fact_line l
                WHERE NOT l.is_return AND l.customer_key IS NOT NULL
                  AND l.{_dim} IS NOT NULL
                GROUP BY 1,2,3
            )
            SELECT store_key, dim AS {_dim},
                   COUNT(*)                AS customers,
                   COUNT(*) FILTER (WHERE n > 1) AS repeat_buyers,
                   SUM(spend)              AS spend_sum,
                   SUM(gm)                 AS gm_sum,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 30) AS churned_30,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 30) AS lapsed_spend_30,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 45) AS churned_45,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 45) AS lapsed_spend_45,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 60) AS churned_60,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 60) AS lapsed_spend_60,
                   COUNT(*)    FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 90) AS churned_90,
                   SUM(spend)  FILTER (WHERE date_diff('day', last_ts, (SELECT t1 FROM mx)) > 90) AS lapsed_spend_90
            FROM pc GROUP BY 1,2
        """)

'''
if anchor in s and "dash_promo_category" not in s:
    s = s.replace(anchor, new_table + anchor, 1)
    open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("publish.py patched")
elif "dash_promo_category" in s:
    print("already patched")
else:
    print("ANCHOR NOT FOUND - paste this to chat")