import ast
p = r"C:\Users\User\cerebral\Cerebral\cerebral_public.py"
lines = open(p, encoding="utf-8-sig").read().splitlines(keepends=False)
idx = next(i for i, l in enumerate(lines) if l.startswith("def render_promo_lab"))
head = lines[:idx]

func = '''
def render_promo_lab():
    """Promo Lab: churn by category/store/brand + ROI estimates.
    Uses fact_line locally, or the privacy-safe dash_promo_* tables online."""
    import os
    import numpy as np
    import pandas as pd

    st.subheader("Promo Lab - Discount Intelligence & ROI")
    st.caption("Where churn concentrates, and the projected return on fixing it with discounts. "
               "Uses your real margins from the data, not a guess.")
    st.markdown('<p class="note"><b>How to read this.</b> This tab answers one question: where is it worth spending discount dollars? A customer counts as <b>churned</b> when they have not bought anything within the lapse window you set below. Every table is ranked by <b>Net gain</b> - the money a win-back promo is projected to make after paying for the discount itself - not by churn rate. That way small, noisy segments can never outrank big, reliable ones. Set the assumptions to your own guesses; nothing here is final until a real campaign measures a real response rate.</p>', unsafe_allow_html=True)

    # ---------- data: full detail locally, privacy-safe aggregates online ----------
    pub_cat, pub_brand = pd.DataFrame(), pd.DataFrame()
    df = q("""SELECT customer_key, store_key, txn_ts, category, brand,
                     basket_id, net_sales, gross_margin
              FROM fact_line WHERE customer_key IS NOT NULL""")
    if df.empty:
        try:
            import duckdb
            for cand in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "tta.duckdb"),
                         r"C:\\Users\\User\\cerebral\\Cerebral\\tta.duckdb"]:
                if os.path.exists(cand):
                    try:
                        con = duckdb.connect(cand, read_only=True)
                        df = con.execute("""SELECT customer_key, store_key, txn_ts, category, brand,
                                                   basket_id, net_sales, gross_margin
                                            FROM fact_line WHERE customer_key IS NOT NULL""").df()
                        con.close()
                    except Exception:
                        pass
                if not df.empty:
                    break
        except Exception:
            pass

    if not df.empty:
        work = df.copy()
        work["txn_ts"] = pd.to_datetime(work["txn_ts"], errors="coerce")
        work = work.dropna(subset=["txn_ts", "customer_key"])
        today = work["txn_ts"].max()

        def build_pub(dim):
            pc = (work.groupby(["store_key", dim, "customer_key"])
                      .agg(last=("txn_ts", "max"), n=("basket_id", "nunique"),
                           spend=("net_sales", "sum"), gm=("gross_margin", "sum"))
                      .reset_index())
            days = (today - pc["last"]).dt.days
            pc["repeat"] = (pc["n"] > 1).astype(int)
            for wdays in (30, 45, 60, 90):
                pc[f"churned_{wdays}"] = (days > wdays).astype(int)
                pc[f"lapsed_spend_{wdays}"] = pc["spend"] * pc[f"churned_{wdays}"]
            return (pc.groupby(["store_key", dim])
                      .agg(customers=("customer_key", "count"),
                           repeat_buyers=("repeat", "sum"),
                           spend_sum=("spend", "sum"), gm_sum=("gm", "sum"),
                           **{f"churned_{w}": (f"churned_{w}", "sum") for w in (30, 45, 60, 90)},
                           **{f"lapsed_spend_{w}": (f"lapsed_spend_{w}", "sum") for w in (30, 45, 60, 90)})
                      .reset_index())

        pub_cat = build_pub("category")
        pub_brand = build_pub("brand") if work["brand"].notna().any() else pd.DataFrame()
    else:
        pub_cat = q("SELECT * FROM dash_promo_category")
        pub_brand = q("SELECT * FROM dash_promo_brand")

    if pub_cat.empty:
        st.warning("No customer-level data found. The published file needs a rebuild "
                   "(publish.py) that includes the dash_promo tables.")
        return

    # ---------- assumptions ----------
    a1, a2, a3, a4 = st.columns(4)
    LAPSE = a1.selectbox("Lapse window (days)", [30, 45, 60, 90], index=0,
                         help="No purchase within this window = churned")
    WINBACK = a2.slider("Expected win-back rate", 1, 40, 10) / 100
    DISCOUNT = a3.slider("Discount depth", 5, 50, 20) / 100
    MIN_CUST = a4.slider("Min customers per segment", 5, 100, 30)

    CH, LS = f"churned_{LAPSE}", f"lapsed_spend_{LAPSE}"

    def roi_table(pub, group_cols):
        g = pub.groupby(group_cols).agg(
            customers=("customers", "sum"), repeat_buyers=("repeat_buyers", "sum"),
            spend_sum=("spend_sum", "sum"), gm_sum=("gm_sum", "sum"),
            churned=(CH, "sum"), lapsed_spend=(LS, "sum")).reset_index()
        g["churn_rate"] = (g["churned"] / g["customers"].clip(lower=1) * 100).round(1)
        g["repeat_rate"] = (g["repeat_buyers"] / g["customers"].clip(lower=1) * 100).round(1)
        g["real_margin"] = np.where(g["spend_sum"] > 0, g["gm_sum"] / g["spend_sum"], 0.5)
        g["avg_lapsed_spend"] = (g["lapsed_spend"] / g["churned"].clip(lower=1)).round(0)
        g["reachable"] = g["churned"].astype(int)
        g["expected_winbacks"] = (g["reachable"] * WINBACK).round(1)
        g["incr_revenue"] = (g["expected_winbacks"] * g["avg_lapsed_spend"]).round(0)
        g["promo_cost"] = (g["incr_revenue"] * DISCOUNT).round(0)
        g["incr_profit"] = (g["incr_revenue"] * g["real_margin"]).round(0)
        g["net_gain"] = (g["incr_profit"] - g["promo_cost"]).round(0)
        g["roi_pct"] = np.where(g["promo_cost"] > 0,
                                (g["net_gain"] / g["promo_cost"] * 100).round(0), np.nan)
        g = g[(g["customers"] >= MIN_CUST) & (g["reachable"] >= 5)]
        return g.sort_values("net_gain", ascending=False)

    RENAME = {"category": "Segment", "customers": "Customers", "churned": "Lapsed",
              "churn_rate": "Churn %", "repeat_rate": "Repeat %",
              "avg_lapsed_spend": "Avg lapsed spend $", "reachable": "Targetable",
              "expected_winbacks": "Expected win-backs", "incr_revenue": "Incr. revenue $",
              "promo_cost": "Promo cost $", "net_gain": "Net gain $", "roi_pct": "ROI %",
              "real_margin": "Real margin", "store_key": "Store", "brand": "Brand"}
    MONEY_FMT = {"Avg lapsed spend $": "${:,.0f}", "Incr. revenue $": "${:,.0f}",
                 "Promo cost $": "${:,.0f}", "Net gain $": "${:,.0f}",
                 "ROI %": "{:,.0f}%", "Real margin": "{:.0%}"}

    tab1, tab2, tab3 = st.tabs(["Churn Map (Categories)", "Store Opportunities", "Brand Promos"])

    with tab1:
        st.markdown('<p class="note"><b>What you are looking at.</b> Each row is a product category across all stores. <b>Churn %</b> is the share of that category customers who have not come back within the lapse window. <b>Real margin</b> comes straight from your sales data, not a guess. <b>Targetable</b> is how many lapsed customers you could actually send an offer to. The greener the Net gain column, the more sense a discount makes there.</p>', unsafe_allow_html=True)
        seg = roi_table(pub_cat, ["category"])
        show = seg[["category", "customers", "churned", "churn_rate", "repeat_rate",
                    "real_margin", "avg_lapsed_spend", "expected_winbacks",
                    "incr_revenue", "promo_cost", "net_gain", "roi_pct"]].rename(columns=RENAME)
        st.markdown("**Segments ranked by net gain from a win-back promo**")
        st.dataframe(show.style.format(MONEY_FMT)
                     .background_gradient(subset=["Net gain $"], cmap="Greens"),
                     use_container_width=True, hide_index=True)

    with tab2:
        st.markdown('<p class="note"><b>How to use this.</b> The first table picks the single best promo for each store - start there. The dropdown below it shows every category inside one store. The verdict table at the bottom tells you whether a store needs one targeted offer (churn concentrated in a few categories) or a store-wide event like a double-points week (churn spread across nearly everything).</p>', unsafe_allow_html=True)
        store_seg = roi_table(pub_cat, ["store_key", "category"])
        store_seg["store_key"] = store_seg["store_key"].map(STORES).fillna(store_seg["store_key"].astype(str))
        st.markdown("**Each store's single best promo (highest net gain)**")
        if not store_seg.empty:
            idx = store_seg.groupby("store_key")["net_gain"].idxmax()
            best = store_seg.loc[idx][["store_key", "category", "reachable", "churn_rate",
                                       "expected_winbacks", "incr_revenue",
                                       "promo_cost", "net_gain", "roi_pct"]].rename(columns=RENAME)
            st.dataframe(best.style.format(MONEY_FMT), use_container_width=True, hide_index=True)

        st.markdown("**Drill into a store**")
        pick = st.selectbox("Store", sorted(store_seg["store_key"].unique()), key="promo_store_pick")
        drill = store_seg[store_seg["store_key"] == pick][
            ["category", "customers", "churned", "churn_rate", "reachable",
             "expected_winbacks", "incr_revenue", "promo_cost", "net_gain", "roi_pct"]
        ].rename(columns=RENAME)
        st.dataframe(drill.style.format(MONEY_FMT), use_container_width=True, hide_index=True)

        st.markdown("**Store-wide promo signal**")
        sw = store_seg.groupby("store_key").agg(
            segments=("category", "count"),
            high_churn_segs=("churn_rate", lambda s: (s > 50).sum()),
            total_net=("net_gain", "sum")).reset_index()
        sw["verdict"] = np.where(
            sw["high_churn_segs"] >= sw["segments"] * 0.6,
            "Store-wide event (e.g., double-points week)",
            "Targeted segment discounts")
        st.dataframe(sw.rename(columns={"store_key": "Store", "segments": "Segments",
                                        "high_churn_segs": "High-churn segments",
                                        "total_net": "Total net gain $"})
                     .style.format({"Total net gain $": "${:,.0f}"}),
                     use_container_width=True, hide_index=True)

    with tab3:
        st.markdown('<p class="note"><b>What this means.</b> Brands ranked by the return on winning back their lapsed buyers. A <b>positive ROI</b> means the promo pays for itself under your assumptions. A negative one means the discount would cost more than it brings back - protect those brands and use them as traffic drivers in marketing instead of discounting them. High ROI with tiny dollar figures means interesting but not a priority.</p>', unsafe_allow_html=True)
        st.markdown("**Brands ranked by win-back ROI**")
        if pub_brand.empty:
            st.warning("No brand data in this build.")
        else:
            brand = roi_table(pub_brand, ["brand"])
            show = brand[["brand", "customers", "churn_rate", "repeat_rate", "real_margin",
                          "reachable", "expected_winbacks", "incr_revenue", "promo_cost",
                          "net_gain", "roi_pct"]].rename(columns=RENAME)
            st.dataframe(show.style.format(MONEY_FMT)
                         .background_gradient(subset=["Net gain $"], cmap="Greens"),
                         use_container_width=True, hide_index=True)

    positive = roi_table(pub_cat, ["store_key", "category"])
    positive = positive[positive["net_gain"] > 0]
    st.divider()
    m1, m2, m3 = st.columns(3)
    m1.metric("Promos with positive ROI", f"{len(positive):,}")
    m2.metric("Total customers to target", f"{int(positive['reachable'].sum()):,}")
    m3.metric("Total projected net gain", f"${positive['net_gain'].sum():,.0f}")

    st.download_button("Download full ROI table (CSV)",
                       positive.rename(columns=RENAME).to_csv(index=False),
                       "promo_lab_roi.csv", "text/csv", key="promo_download")


with t_promo:
    render_promo_lab()
'''

s = "\n".join(head) + func
ast.parse(s)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("Promo Lab v4 installed - syntax OK")