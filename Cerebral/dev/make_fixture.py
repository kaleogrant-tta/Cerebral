#!/usr/bin/env python3
"""
make_fixture.py -- build a SMALL, fully synthetic cerebral_dash.duckdb.

Writes  Cerebral/dev/fixture/cerebral_dash.duckdb  with every dash_* table the
dashboard (cerebral_public.py and the *_tab.py / promo_calendar / channel_promo
/ brand_roi modules) reads, using the column lists in publish.py and the
publish_*.py modules. Nothing here is real: every number comes from a seeded
random generator, so the file is the same on every run.

    python dev/make_fixture.py            # from the Cerebral/ folder
    python dev/make_fixture.py --out /some/other/path.duckdb

Shape of the fake world
  * 4 stores (1 DTBK, 2 5th Avenue, 3 Soho, 4 Union Square)
  * 32 ISO weeks of 2026 (W10 - W41, Mar 2 - Oct 11), full weeks only
  * 6 categories, ~12 brands, the 7 takeover brands from TAKEOVERS get a sales
    lift inside their windows so the Takeover tab has something to show
  * loyalty / retention / events / audiences / VM / discounting tables are
    produced by the same maths the real publish_* modules use where practical
    (publish_events and publish_vm are imported and run for real).
  * Promo Lab: a fake fact_line / fact_basket source (W23-W42) with the promo
    behaviour from dev/PROMO_LAB_SPEC.md injected, then publish_promo.build_promo
    runs on it for real (section 8).
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import math
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent                       # Cerebral/
DEFAULT_OUT = HERE / "fixture" / "cerebral_dash.duckdb"
sys.path.insert(0, str(APP_DIR))

RNG = np.random.default_rng(2026)

# ---------------------------------------------------------------- universe
YEAR = 2026
WEEK_NUMS = list(range(10, 42))                 # W10 .. W41  (32 weeks)
WEEKS = [(YEAR, w) for w in WEEK_NUMS]
WK_START = {w: dt.date.fromisocalendar(YEAR, w, 1) for w in WEEK_NUMS}
FIRST_DAY = WK_START[WEEK_NUMS[0]]
LAST_DAY = WK_START[WEEK_NUMS[-1]] + dt.timedelta(days=6)
ALL_DAYS = [FIRST_DAY + dt.timedelta(days=i)
            for i in range((LAST_DAY - FIRST_DAY).days + 1)]
STORE_NAMES = {1: "DTBK", 2: "5th Avenue", 3: "Soho", 4: "Union Square"}
VM_STORE = {1: "DTBK", 2: "FIFTH", 3: "SOHO", 4: "USQ"}
STORE_WEEKLY_NET = {1: 150_000, 2: 210_000, 3: 95_000, 4: 175_000}
CHAN_SHARE = {1: {"In-Store": .70, "Non-Stop": .20, "Delivery": .10},
              2: {"In-Store": .68, "Non-Stop": .20, "Delivery": .12},
              3: {"In-Store": .85, "Non-Stop": .15},
              4: {"In-Store": .72, "Non-Stop": .20, "Delivery": .08}}
CHAN_AOV = {"In-Store": 58.0, "Non-Stop": 66.0, "Delivery": 88.0}
CHANNELS = ["In-Store", "Non-Stop", "Delivery"]
DOW_W = np.array([0.82, 0.86, 0.92, 0.98, 1.20, 1.30, 0.92])   # Mon..Sun
DOW_W = DOW_W / DOW_W.sum() * 7

CATS = {   # share of net, avg unit price, gross margin, basket penetration
    "Flower":      dict(share=.31, price=38., gm=.52, pen=.52),
    "Pre-Roll":    dict(share=.14, price=9.5, gm=.50, pen=.28),
    "Vape":        dict(share=.20, price=36., gm=.48, pen=.30),
    "Edible":      dict(share=.14, price=17., gm=.55, pen=.24),
    "Concentrate": dict(share=.09, price=42., gm=.50, pen=.10),
    "Accessory":   dict(share=.05, price=14., gm=.62, pen=.12),
}
BRANDS = {
    "Flower": ["Rythm", "Ruby", "Wyld", "Woodstock", "Timeless", "Select",
               "Dark Heart", "Find.", "Mini Mart", "Good Green",
               "Harbor Farms", "Kings Road"],
    "Pre-Roll": ["Rythm", "Ruby", "Wyld", "Woodstock", "Select", "Find.",
                 "Mini Mart", "Good Green", "Metro Gold"],
    "Vape": ["Select", "Dark Heart", "Rythm", "Wyld", "Timeless",
             "Cloud Nine", "Sunday Goods", "Old Port"],
    "Edible": ["Rythm", "Wyld", "Woodstock", "Bodega Botanicals",
               "Sunday Goods", "Cloud Nine", "Alpine Leaf"],
    "Concentrate": ["Rythm", "Dark Heart", "Select", "Timeless", "Old Port",
                    "Harbor Farms"],
    "Accessory": ["The Travel Agency", "Cloud Nine", "Raw Rolls",
                  "Metro Gold", "Glass House"],
}
STRAINS = ["Blue Dream", "Gelato 33", "Sour Diesel", "Purple Punch",
           "Wedding Cake", "Jack Herer", "Zkittlez", "Lemon Haze", "Mimosa",
           "Runtz", "OG Kush", "GMO Cookies"]
SIZES = {"Flower": ["3.5g", "7g", "14g"], "Pre-Roll": ["1g", "5pk", "2pk"],
         "Vape": ["0.5g Cartridge", "1g All-In-One", "0.3g Pod"],
         "Edible": ["10pk Gummies", "100mg Chocolate", "5mg Beverage"],
         "Concentrate": ["1g Live Resin", "1g Badder", "0.5g Rosin"]}
ACC_ITEMS = ["Rolling Papers", "Glass Pipe", "Lighter", "Grinder",
             "Rolling Tray", "Torch", "Storage Jar", "Hemp Wick"]

TAKEOVERS_FALLBACK = [
    {"name": "Rythm (GTI)", "start": "2026-03-01", "end": "2026-03-31", "patterns": ["rythm", "rhythm"]},
    {"name": "Select (Curaleaf)", "start": "2026-04-01", "end": "2026-04-15", "patterns": ["select"]},
    {"name": "Dark Heart (Curaleaf)", "start": "2026-04-16", "end": "2026-04-30", "patterns": ["dark heart", "darkheart"]},
    {"name": "Woodstock", "start": "2026-05-01", "end": "2026-05-14", "patterns": ["woodstock"]},
    {"name": "Timeless", "start": "2026-05-14", "end": "2026-06-07", "patterns": ["timeless"]},
    {"name": "Ruby Farms", "start": "2026-07-16", "end": "2026-08-17", "patterns": ["ruby farms", "ruby"]},
    {"name": "Wyld", "start": "2026-09-15", "end": "2026-10-18", "patterns": ["wyld"]},
]
# brand -> (start, end, share multiplier) for the sales lift
LIFT = {"Rythm": ("2026-03-01", "2026-03-31", 1.9),
        "Select": ("2026-04-01", "2026-04-15", 2.2),
        "Dark Heart": ("2026-04-16", "2026-04-30", 2.0),
        "Woodstock": ("2026-05-01", "2026-05-14", 1.8),
        "Timeless": ("2026-05-14", "2026-06-07", 1.7),
        "Ruby": ("2026-07-16", "2026-08-17", 2.1),
        "Wyld": ("2026-09-15", "2026-10-18", 2.0)}
LIFT = {b: (dt.date.fromisoformat(s), dt.date.fromisoformat(e), m)
        for b, (s, e, m) in LIFT.items()}


def day_lift(brand: str, d: dt.date) -> float:
    t = LIFT.get(brand)
    return t[2] if t and t[0] <= d <= t[1] else 1.0


def week_lift(brand: str, w: int) -> float:
    s = WK_START[w]
    return float(np.mean([day_lift(brand, s + dt.timedelta(days=i)) for i in range(7)]))


def sql_ident_list(vals):
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in vals)


def put(con, name: str, df: pd.DataFrame):
    """Create table `name` from a DataFrame (replacing any old one)."""
    con.execute(f"DROP TABLE IF EXISTS {name}")
    con.register("_tmp_df", df)
    con.execute(f"CREATE TABLE {name} AS SELECT * FROM _tmp_df")
    con.unregister("_tmp_df")
    print(f"  {name:<32}{len(df):>9,} rows")


def ints(s):
    return np.round(s).astype("int64")


# ====================================================================
# 1. sales spine: product x store x week, then everything rolls up from it
# ====================================================================
def build_products() -> pd.DataFrame:
    rows = []
    for cat, brands in BRANDS.items():
        base = RNG.lognormal(0, .65, len(brands))
        base = base / base.sum()
        for bi, brand in enumerate(brands):
            if cat == "Accessory":
                items = ACC_ITEMS
            else:
                items = [f"{STRAINS[(bi * 3 + k) % len(STRAINS)]} {SIZES[cat][k % 3]}"
                         for k in range(3)]
            psh = RNG.lognormal(0, 1.1 if cat == "Accessory" else .5, len(items))
            psh = psh / psh.sum()
            for k, item in enumerate(items):
                rows.append(dict(category=cat, brand=brand,
                                 product=f"{brand} {item}",
                                 base_brand=base[bi], pshare=psh[k],
                                 price=CATS[cat]["price"] * RNG.uniform(.8, 1.3)
                                 * (1 + .18 * k if cat != "Accessory" else 1)))
    return pd.DataFrame(rows)


def build_spine():
    prods = build_products()
    pref = {(s, b): RNG.lognormal(0, .2)
            for s in STORE_NAMES for b in {b for bl in BRANDS.values() for b in bl}}
    wf = {}
    for w in WEEK_NUMS:
        wf[w] = 1 + .0035 * (w - 25) + .04 * math.sin(w / 4) + RNG.normal(0, .025)
    rows = []
    for s in STORE_NAMES:
        snoise = {w: RNG.normal(1, .03) for w in WEEK_NUMS}
        for w in WEEK_NUMS:
            for cat, c in CATS.items():
                boost = 1.35 if (w == 17 and cat in ("Flower", "Pre-Roll", "Vape")) else 1.0
                cat_net = (STORE_WEEKLY_NET[s] * wf[w] * snoise[w] * c["share"]
                           * boost * RNG.normal(1, .04))
                sub = prods[prods.category == cat].copy()
                sub["w"] = (sub.base_brand * np.array([pref[(s, b)] for b in sub.brand])
                            * np.array([week_lift(b, w) for b in sub.brand])
                            * RNG.normal(1, .06, len(sub)))
                # brand share = weight of brand, split across the brand's products
                bw = sub.groupby("brand")["w"].transform("sum")
                tot = sub.w.sum() / sub.groupby("brand").ngroups
                # weight per brand normalised across brands, then product share inside brand
                bsum = sub.groupby("brand")["w"].first()
                sub["bshare"] = (sub.base_brand * np.array([pref[(s, b)] for b in sub.brand])
                                 * np.array([week_lift(b, w) for b in sub.brand]))
                bt = sub.groupby("brand")["bshare"].transform("first")
                brand_tot = sub.drop_duplicates("brand").bshare.sum()
                sub["share"] = (bt / brand_tot) * sub.pshare * RNG.normal(1, .08, len(sub))
                sub["net"] = cat_net * .965 * sub.share
                sub["store_key"], sub["iso_year"], sub["iso_week"] = s, YEAR, w
                rows.append(sub[["store_key", "iso_year", "iso_week", "category",
                                 "brand", "product", "price", "net"]])
    p = pd.concat(rows, ignore_index=True)
    p["units"] = ints(p.net / p.price)
    p = p[p.units >= 1].copy()
    p["gm"] = p.net * p.category.map({k: v["gm"] for k, v in CATS.items()}) \
        * RNG.normal(1, .02, len(p))
    p["baskets"] = np.maximum(1, ints(p.units * RNG.uniform(.74, .9, len(p))))
    return p


def build_core(con, P: pd.DataFrame) -> dict:
    out = {}
    # ---- category x channel x week -----------------------------------
    cat_all = (P.groupby(["store_key", "iso_year", "iso_week", "category"], as_index=False)
                 .agg(net=("net", "sum"), gm=("gm", "sum"), units=("units", "sum")))
    for c in ("net", "gm", "units"):
        cat_all[c] = cat_all[c] / .965
    rows = []
    for _, r in cat_all.iterrows():
        for ch, sh in CHAN_SHARE[r.store_key].items():
            n = RNG.normal(1, .03)
            rows.append((r.store_key, r.iso_year, r.iso_week, ch, r.category,
                         r.net * sh * n, r.gm * sh * n, r.units * sh * n))
    cw = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "channel",
                                     "category", "net", "gm", "units"])
    cw["units"] = ints(cw.units)
    # basket totals per store x week x channel
    bw = (cw.groupby(["store_key", "iso_year", "iso_week", "channel"], as_index=False)
            .agg(net_all=("net", "sum")))
    bw["net_all"] = bw.net_all / .98
    bw["baskets"] = ints(bw.net_all / bw.channel.map(CHAN_AOV) * RNG.normal(1, .02, len(bw)))
    bw["days_open"] = 7
    bw["avg_lines"] = RNG.normal(2.3, .08, len(bw))
    cw = cw.merge(bw[["store_key", "iso_year", "iso_week", "channel", "baskets",
                      "days_open", "net_all", "avg_lines"]],
                  on=["store_key", "iso_year", "iso_week", "channel"])
    pen = cw.category.map({k: v["pen"] for k, v in CATS.items()})
    cw["baskets_with"] = np.minimum(cw.baskets, ints(cw.baskets * pen * RNG.normal(1, .04, len(cw))))
    sk = {"Flower": 140, "Pre-Roll": 80, "Vape": 90, "Edible": 110, "Concentrate": 45, "Accessory": 60}
    key = cw[["store_key", "iso_year", "iso_week", "category"]].drop_duplicates().reset_index(drop=True)
    key["skus_in_stock"] = ints(key.category.map(sk) * RNG.normal(1, .06, len(key)))
    key["skus_out"] = ints(key.skus_in_stock * RNG.uniform(.02, .09, len(key)))
    key["floor_units_end"] = ints(key.skus_in_stock * RNG.uniform(10, 18, len(key)))
    cw = cw.merge(key, on=["store_key", "iso_year", "iso_week", "category"])
    cw = cw[["store_key", "iso_year", "iso_week", "channel", "category", "net", "gm",
             "units", "baskets_with", "baskets", "days_open", "net_all", "avg_lines",
             "skus_in_stock", "skus_out", "floor_units_end"]]
    put(con, "dash_category_week", cw)

    b = bw.copy()
    b["net"] = b.net_all
    b["avg_net"] = b.net / b.baskets
    b["single_line"] = ints(b.baskets * .28)
    b["redeem_baskets"] = ints(b.baskets * RNG.uniform(.09, .14, len(b)))
    b["redeem_value"] = b.redeem_baskets * RNG.uniform(5.5, 7.5, len(b))
    b = b[["store_key", "iso_year", "iso_week", "channel", "baskets", "net", "avg_net",
           "avg_lines", "single_line", "redeem_baskets", "redeem_value", "days_open"]]
    put(con, "dash_basket_week", b)
    out["bw"] = b
    out["cw"] = cw

    # ---- brand / product weekly -------------------------------------
    key = ["store_key", "iso_year", "iso_week", "brand", "category"]
    bwk = P.groupby(key, as_index=False).agg(net=("net", "sum"), gm=("gm", "sum"),
                                             units=("units", "sum"), baskets=("baskets", "sum"))
    bwk["baskets"] = ints(bwk.baskets * .85)
    bwk["first_basket_customers"] = ints(bwk.baskets * RNG.uniform(.04, .09, len(bwk)))
    bwk["established_customers"] = ints(bwk.baskets * RNG.uniform(.35, .55, len(bwk)))
    put(con, "dash_brand_week", bwk)
    out["bwk"] = bwk

    bpw = P[["store_key", "iso_year", "iso_week", "brand", "category", "product",
             "net", "gm", "units", "baskets"]]
    put(con, "dash_brand_product_week", bpw)

    pw = P.copy()
    pw["rank_in_cat"] = (pw.groupby(["store_key", "iso_year", "iso_week", "category"])["net"]
                           .rank(method="first", ascending=False).astype(int))
    pw = pw[pw.rank_in_cat <= 10][["store_key", "iso_year", "iso_week", "category", "brand",
                                   "product", "units", "net", "gm", "baskets", "rank_in_cat"]]
    put(con, "dash_product_week", pw)

    acc = P[P.category == "Accessory"][["store_key", "iso_year", "iso_week", "product", "units", "net"]]
    put(con, "dash_acc_product_week", acc)

    # ---- trends: first half vs second half of the loaded window -------
    cut = WEEK_NUMS[len(WEEK_NUMS) // 2]
    P2 = P.assign(half=np.where(P.iso_week < cut, "early", "late"))

    def halves(keys):
        g = P2.groupby(keys + ["half"], as_index=False).agg(net=("net", "sum"), units=("units", "sum"))
        pv = g.pivot_table(index=keys, columns="half", values=["net", "units"],
                           aggfunc="sum", fill_value=0)
        pv.columns = [f"{a}_{b}" for a, b in pv.columns]
        pv = pv.reset_index()
        pv["net_total"] = pv.net_early + pv.net_late
        return pv
    bt = halves(["store_key", "category", "brand"])
    bt = bt[bt.net_total >= 2000][["store_key", "category", "brand", "net_early", "net_late",
                                   "units_early", "units_late", "net_total"]]
    put(con, "dash_brand_trend", bt)
    pt = halves(["store_key", "category", "brand", "product"])
    pt = pt[pt.net_total >= 1000].copy()
    pt["rk"] = pt.groupby(["store_key", "category"])["net_total"].rank(method="first", ascending=False).astype(int)
    pt = pt[pt.rk <= 25][["store_key", "category", "brand", "product", "net_early", "net_late",
                          "units_early", "units_late", "net_total", "rk"]]
    put(con, "dash_product_trend", pt)

    # ---- brand scorecard (whole file) --------------------------------
    sc = P.groupby(["store_key", "brand"], as_index=False).agg(
        primary_category=("category", "min"), categories=("category", "nunique"),
        skus=("product", "nunique"), net=("net", "sum"), gm=("gm", "sum"),
        units=("units", "sum"), baskets=("baskets", "sum"))
    sc["baskets"] = ints(sc.baskets * .85)
    sc["customers"] = ints(sc.baskets * RNG.uniform(.45, .6, len(sc)))
    sc["first_basket_customers"] = ints(sc.customers * RNG.uniform(.08, .2, len(sc)))
    sc["established_customers"] = ints(sc.customers * RNG.uniform(.3, .55, len(sc)))
    sc = sc[sc.net >= 1000]
    put(con, "dash_brand_scorecard", sc)
    out["sc"] = sc

    put(con, "dash_customer_totals", pd.DataFrame(
        [dict(total_customers=61_240, new_last_180d=17_980)]))

    # ---- category pairs ----------------------------------------------
    cats = sorted(CATS)
    pc = {"Flower": .60, "Pre-Roll": .30, "Vape": .35, "Edible": .30, "Concentrate": .15, "Accessory": .12}
    pairlift = {("Accessory", "Flower"): 1.5, ("Accessory", "Pre-Roll"): 1.3,
                ("Concentrate", "Vape"): .55, ("Flower", "Pre-Roll"): .7,
                ("Edible", "Flower"): 1.15}
    pb, pbase, bp = [], [], []
    for s in STORE_NAMES:
        total_b = int(b[b.store_key == s].baskets.sum())
        multi = int(total_b * .45)
        for c in cats:
            pbase.append((s, c, int(multi * pc[c]), multi))
        for i, a in enumerate(cats):
            for z in cats[i + 1:]:
                lift = pairlift.get((a, z), RNG.uniform(.85, 1.2))
                joint = int(min(multi * pc[a] * pc[z] * lift, multi * min(pc[a], pc[z]) * .9))
                pb.append((s, a, z, joint))
                ba = [x for x in BRANDS[a]][:6]
                bz = [x for x in BRANDS[z]][:6]
                for x in ba:
                    for y in bz:
                        j = int(joint * RNG.uniform(.01, .09) * RNG.uniform(.6, 1.6))
                        if j >= 15:
                            bp.append((s, a, z, x, y, j))
    put(con, "dash_pairs", pd.DataFrame(pb, columns=["store_key", "cat_a", "cat_b", "joint_baskets"]))
    put(con, "dash_pair_base", pd.DataFrame(pbase, columns=["store_key", "category", "cat_baskets", "multi_baskets"]))
    put(con, "dash_brand_pairs", pd.DataFrame(bp, columns=["store_key", "cat_a", "cat_b", "brand_a", "brand_b", "joint_baskets"]))

    # ---- inventory ------------------------------------------------------
    snap = LAST_DAY
    last4 = P[P.iso_week > WEEK_NUMS[-1] - 4]
    inv_rows, bei_rows = [], []
    sales = last4.groupby(["store_key", "category", "brand"], as_index=False).agg(
        net=("net", "sum"), gm=("gm", "sum"), units=("units", "sum"))
    sales["inv_cost"] = sales.net * RNG.uniform(.35, 1.9, len(sales))
    sales["inv_retail"] = sales.inv_cost * 2.1
    price = sales.category.map({k: v["price"] for k, v in CATS.items()})
    sales["qoh"] = ints(sales.inv_retail / price)
    sales["skus"] = ints(np.maximum(1, sales.units / 18 * RNG.uniform(.6, 1.4, len(sales))))
    # a few brands sold out (stock ~0) and a few with stock but no sales
    sales.loc[RNG.random(len(sales)) < .03, ["inv_cost", "inv_retail", "qoh", "skus"]] = 0
    inv = sales.groupby(["store_key", "category"], as_index=False).agg(
        inv_cost=("inv_cost", "sum"), inv_retail=("inv_retail", "sum"),
        qoh=("qoh", "sum"), skus=("skus", "sum"))
    inv["snapshot_date"] = snap
    put(con, "dash_inventory", inv)
    acc_inv = (P[P.category == "Accessory"][["store_key", "product"]].drop_duplicates())
    acc_inv["qoh"] = ints(RNG.gamma(1.2, 9, len(acc_inv)))
    acc_inv.loc[RNG.random(len(acc_inv)) < .12, "qoh"] = 0
    acc_inv["snapshot_date"] = snap
    put(con, "dash_acc_product_inv", acc_inv)

    j = sales.copy()
    g = j.groupby(["store_key", "category"])
    for src, dst in [("net", "cat_net"), ("gm", "cat_gm"), ("units", "cat_units"),
                     ("inv_cost", "cat_inv_cost"), ("qoh", "cat_qoh"), ("skus", "cat_skus")]:
        j[dst] = g[src].transform("sum")
    nz = lambda s: s.replace(0, np.nan)
    j["sales_share"] = j.net / nz(j.cat_net)
    j["gm_share"] = j.gm / nz(j.cat_gm)
    j["capital_share"] = j.inv_cost / nz(j.cat_inv_cost)
    j["unit_share"] = j.qoh / nz(j.cat_qoh)
    j["assort_share"] = j.skus / nz(j.cat_skus)
    ok = j.inv_cost >= 200
    j["bei_capital"] = np.where(ok, j.sales_share / nz(j.capital_share), np.nan)
    j["bei_margin"] = np.where(ok, j.gm_share / nz(j.capital_share), np.nan)
    j["bei_unit"] = np.where(j.qoh >= 5, (j.units / nz(j.cat_units)) / nz(j.unit_share), np.nan)
    j["bei_assort"] = np.where(j.skus >= 1, j.sales_share / nz(j.assort_share), np.nan)
    j["dos"] = np.where(j.units > 0, j.qoh / (j.units / 28.0), np.nan)
    j["stocked_out"] = (j.net > 0) & (j.inv_cost < 200)
    j["snapshot_date"] = snap
    j["window_days"] = 28
    bei = j[["store_key", "category", "brand", "net", "gm", "units", "inv_cost", "inv_retail",
             "qoh", "skus", "sales_share", "gm_share", "capital_share", "unit_share",
             "assort_share", "bei_capital", "bei_margin", "bei_unit", "bei_assort", "dos",
             "stocked_out", "snapshot_date", "window_days"]].round(4)
    put(con, "dash_bei", bei)
    cov = inv.groupby("category", as_index=False).agg(inv_cost=("inv_cost", "sum"), skus=("skus", "sum"))
    cov["inv_rows"] = ints(cov.skus * 1.6)
    cov["unmatched_rows"] = ints(cov.inv_rows * RNG.uniform(.01, .05, len(cov)))
    cov["cost_coverage"] = RNG.uniform(.93, .99, len(cov)).round(4)
    cov.loc[cov.category == "Accessory", "cost_coverage"] = .62   # below BEI_MIN_COVERAGE on purpose
    put(con, "dash_bei_coverage", cov[["category", "inv_rows", "unmatched_rows", "cost_coverage", "inv_cost"]])

    # ---- churn / promo lab -------------------------------------------
    def promo(dim):
        g = P.groupby(["store_key", dim], as_index=False).agg(spend_sum=("net", "sum"),
                                                              gm_sum=("gm", "sum"),
                                                              baskets=("baskets", "sum"))
        g["customers"] = ints(np.maximum(30, g.baskets * RNG.uniform(.12, .25, len(g))))
        g["repeat_buyers"] = ints(g.customers * RNG.uniform(.35, .6, len(g)))
        for d, f in ((30, .42), (45, .33), (60, .27), (90, .19)):
            g[f"churned_{d}"] = ints(g.customers * f * RNG.uniform(.85, 1.15, len(g)))
            g[f"lapsed_spend_{d}"] = g[f"churned_{d}"] * (g.spend_sum / g.customers) * RNG.uniform(.5, .8, len(g))
        cols = ["store_key", dim, "customers", "repeat_buyers", "spend_sum", "gm_sum"] + \
            [f"{k}_{d}" for d in (30, 45, 60, 90) for k in ("churned", "lapsed_spend")]
        return g[cols]
    put(con, "dash_promo_category", promo("category"))
    put(con, "dash_promo_brand", promo("brand"))

    # ---- brand_day (whole window; takeover precision) ------------------
    bd_week = P.groupby(["store_key", "iso_week", "brand"], as_index=False).agg(
        net=("net", "sum"), units=("units", "sum"), baskets=("baskets", "sum"), gm=("gm", "sum"))
    recs = []
    for r in bd_week.itertuples():
        days = [WK_START[r.iso_week] + dt.timedelta(days=i) for i in range(7)]
        f = np.array([day_lift(r.brand, d) * DOW_W[i] for i, d in enumerate(days)])
        f = f / f.sum()
        for d, fi in zip(days, f):
            recs.append((d, r.store_key, r.brand, r.net * fi, ints(np.array([r.units * fi]))[0],
                         max(1, int(r.baskets * fi * .85)), r.gm * fi))
    bd = pd.DataFrame(recs, columns=["day", "store_key", "brand", "net", "units", "baskets", "gm"])
    bd["day"] = pd.to_datetime(bd.day)
    put(con, "dash_brand_day", bd)
    out["bd"] = bd
    return out


# ====================================================================
# 2. incomplete days, alias, meta, load log
# ====================================================================
def build_misc(con, core):
    put(con, "dash_incomplete_days", pd.DataFrame(
        [(3, pd.Timestamp(LAST_DAY).date(), 41, 780, 1290.0, 510.0)],
        columns=["store_key", "day", "baskets", "close_min", "typical_close", "short_by_min"]
    ).iloc[0:0])
    put(con, "dash_brand_alias", pd.DataFrame(
        [("Ruby Farms", "Ruby", 4120, 128_400.0), ("RUBY FARMS", "Ruby", 310, 9_800.0)],
        columns=["alias", "canonical", "lines", "net"]))
    b = core["bw"]
    n_b = int(b.baskets.sum())
    put(con, "dash_meta", pd.DataFrame([dict(
        n_lines=int(n_b * 2.3), n_baskets=n_b,
        first_txn=pd.Timestamp(FIRST_DAY) + pd.Timedelta(hours=9),
        last_txn=pd.Timestamp(LAST_DAY) + pd.Timedelta(hours=21, minutes=48),
        built_at=pd.Timestamp.now().floor("s"))]))
    ll = []
    for s in STORE_NAMES:
        for p in ("2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"):
            ll.append((p, s, 52_000, 21_000, 0, "fixture-1", pd.Timestamp.now().floor("s")))
    put(con, "dash_load_log", pd.DataFrame(ll, columns=["period", "store_key", "lines", "baskets",
                                                         "warnings", "config_version", "loaded_at"]))


# ====================================================================
# 3. redemptions / offers / GWP / discounting
# ====================================================================
OFFERS = [  # offer_name, brand, category, product, weight, method
    ("Travel Club 100 Points Reward - Travel Agency Merch", "The Travel Agency", "Accessory", "The Travel Agency Rolling Tray", 5, "brand+product"),
    ("Travel Club 200 Points Reward - Metro Gold Pre-Roll", "Metro Gold", "Pre-Roll", None, 4, "brand"),
    ("Travel Club 500 Points Reward - Rythm Pre-Roll", "Rythm", "Pre-Roll", "Rythm Blue Dream 1g", 9, "brand+product"),
    ("Travel Club 500 Points Reward - Rythm Edible", "Rythm", "Edible", "Rythm Purple Punch 100mg Chocolate", 5, "brand+product"),
    ("Travel Club 1000 Points Reward - Rythm Flower 3.5g", "Rythm", "Flower", "Rythm Gelato 33 3.5g", 7, "brand+product"),
    ("Travel Club 1000 Points Reward - Dark Heart Flower 3.5g", "Dark Heart", "Flower", "Dark Heart Sour Diesel 3.5g", 4, "brand+product"),
    ("Travel Club 1500 Points Reward - Select Vape", "Select", "Vape", "Select Wedding Cake 1g All-In-One", 4, "brand+product"),
    ("Travel Club 3000 Points Reward - Rythm Flower 28g", "Rythm", "Flower", "Rythm Gelato 33 14g", 2, "brand+product"),
    ("Loyalty 500 Points Reward - Rythm Pre-Roll", "Rythm", "Pre-Roll", "Rythm Blue Dream 1g", 3, "brand+product"),
    ("Loytaly 500 Points Reward - Rythm Pre-Roll", "Rythm", "Pre-Roll", "Rythm Blue Dream 1g", 1, "brand+product"),
    ("Secret Drop - April Mystery Bag", "Secret Drops", "Promo", "Mystery Bag", 4, "brand"),
    ("Ruby Farms Takeover - Free Doobie With Purchase", "Ruby", "Pre-Roll", "Ruby Gelato 33 1g", 6, "brand+product"),
    ("Wyld Takeover - Free Gummies With Purchase", "Wyld", "Edible", "Wyld Gelato 33 10pk Gummies", 6, "brand+product"),
    ("Timeless Takeover 15% Off", "Timeless", "Vape", "Timeless Jack Herer 1g All-In-One", 3, "brand+product"),
    ("Woodstock Takeover 15% Off", "Woodstock", "Flower", "Woodstock Wedding Cake 7g", 3, "brand+product"),
]
SUB_TIERS = [500, 1000, 1500]
SUB_PICKS = [("Rythm", "Pre-Roll", "Rythm Blue Dream 1g"), ("Rythm", "Edible", "Rythm Purple Punch 100mg Chocolate"),
             ("Wyld", "Flower", "Wyld Gelato 33 3.5g"), ("Select", "Vape", "Select Wedding Cake 1g All-In-One"),
             ("Good Green", "Flower", "Good Green Runtz 3.5g"), ("Find.", "Flower", "Find. OG Kush 7g"),
             ("Sunday Goods", "Edible", "Sunday Goods Mimosa 5mg Beverage"), ("Old Port", "Concentrate", "Old Port Jack Herer 1g Badder")]


def build_offers(con, core):
    bw = core["bw"]
    totals = bw.groupby("store_key").redeem_baskets.sum()
    recs = []   # per store offer rows
    wsum = sum(o[4] for o in OFFERS) + len(SUB_TIERS) * 3 + 2
    for s in STORE_NAMES:
        tot = totals[s]
        for name, brand, cat, prod, w, meth in OFFERS:
            n = int(tot * w / wsum * RNG.uniform(.85, 1.15))
            val = n * RNG.uniform(7, 14) if "Takeover" not in name else n * RNG.uniform(4, 6)
            fam = brand
            recs.append((s, name, prod, fam, cat, meth, n, val, RNG.uniform(70, 95),
                         pd.Timestamp("2026-03-02") + pd.Timedelta(days=int(RNG.integers(0, 40))),
                         pd.Timestamp(LAST_DAY)))
        for t in SUB_TIERS:
            for (pb, pc, pp) in SUB_PICKS:
                n = int(tot * 3 / wsum / len(SUB_PICKS) * RNG.uniform(.3, 1.8))
                if n:
                    recs.append((s, f"Travel Club {t} Points Substitution", pp,
                                 f"Travel Club Substitution — {t} pts", pc, "substituted-line", n,
                                 n * t / 100 * RNG.uniform(.9, 1.1), RNG.uniform(65, 90),
                                 pd.Timestamp("2026-03-16"), pd.Timestamp(LAST_DAY)))
            n = int(tot * 1 / wsum * RNG.uniform(.5, 1.2))
            recs.append((s, f"Travel Club {t} Points Substitution", None,
                         f"Travel Club Substitution — {t} pts", None, "unmatched", n,
                         n * t / 100, RNG.uniform(60, 80), pd.Timestamp("2026-03-16"), pd.Timestamp(LAST_DAY)))
    op = pd.DataFrame(recs, columns=["store_key", "offer_name", "product", "brand", "category",
                                     "match_method", "redemptions", "redeem_value", "avg_basket",
                                     "first_seen", "last_seen"])
    put(con, "dash_offer_performance", op)

    br = op.groupby(["store_key", "brand", "category", "match_method"], dropna=False, as_index=False).agg(
        redemptions=("redemptions", "sum"), redeem_value=("redeem_value", "sum"),
        avg_basket=("avg_basket", "mean"))
    br["avg_redeem"] = br.redeem_value / br.redemptions.replace(0, np.nan)
    br["redeemers"] = ints(br.redemptions * RNG.uniform(.55, .8, len(br)))
    br["first_visit_redeemers"] = ints(br.redeemers * RNG.uniform(.12, .35, len(br)))
    br["established_redeemers"] = ints(br.redeemers * RNG.uniform(.3, .5, len(br)))
    br = br[["store_key", "brand", "category", "match_method", "redemptions", "redeem_value",
             "avg_redeem", "avg_basket", "redeemers", "first_visit_redeemers", "established_redeemers"]]
    put(con, "dash_brand_redemption", br)

    # daily: allocate each offer-level total over days (takeover brands spike in their windows)
    brand_tot = op.groupby(["store_key", "brand"], as_index=False)[["redemptions", "redeem_value"]].sum()
    recs = []
    for r in brand_tot.itertuples():
        wts = np.array([day_lift(r.brand, d) * DOW_W[d.weekday()] * (4 if day_lift(r.brand, d) > 1 and r.brand in LIFT else 1)
                        for d in ALL_DAYS])
        wts = wts / wts.sum()
        for ch, chs in CHAN_SHARE[r.store_key].items():
            n = RNG.poisson(np.maximum(r.redemptions * chs * wts, 0))
            for d, k in zip(ALL_DAYS, n):
                if k:
                    recs.append((r.store_key, d, r.brand, ch, int(k),
                                 float(k * r.redeem_value / max(r.redemptions, 1))))
    rd = pd.DataFrame(recs, columns=["store_key", "day", "brand", "channel", "redemptions", "redeem_value"])
    rd["day"] = pd.to_datetime(rd.day)
    put(con, "dash_redemption_day", rd)

    # tier x week
    rd["iso_week"] = rd.day.dt.isocalendar().week.astype(int)
    rd["iso_year"] = YEAR
    wk = rd.groupby(["store_key", "iso_year", "iso_week", "brand"], as_index=False)[["redemptions", "redeem_value"]].sum()
    parts = []
    for tier, f in (("Frequent Flyer", .45), ("Travel Club", .5), ("Unknown", .05)):
        t = wk.copy()
        t["tier"] = tier
        t["redemptions"] = ints(t.redemptions * f)
        t["redeem_value"] = t.redeem_value * f
        t["customers"] = ints(t.redemptions * .75)
        parts.append(t[t.redemptions > 0])
    put(con, "dash_redemption_tier", pd.concat(parts)[["store_key", "iso_year", "iso_week", "tier", "brand",
                                                       "redemptions", "redeem_value", "customers"]])


def build_discounting(con, core, P):
    bw = core["bw"]
    # day-level, spread from the weekly baskets
    recs = []
    for r in bw.itertuples():
        for i in range(7):
            d = WK_START[r.iso_week] + dt.timedelta(days=i)
            f = DOW_W[i] / 7
            base = r.baskets * f
            n = int(base * RNG.normal(1, .04))
            net = r.net * f * RNG.normal(1, .04)
            disc_rate = RNG.uniform(.07, .11)
            gross = net / (1 - disc_rate)
            recs.append((r.store_key, d, r.channel, n, int(n * RNG.uniform(.5, .66)), gross,
                         gross - net, net, net * RNG.uniform(.47, .53),
                         r.redeem_value * f * RNG.normal(1, .05)))
    dd = pd.DataFrame(recs, columns=["store_key", "day", "channel", "baskets", "discounted_baskets",
                                     "gross", "discount", "net", "margin", "loyalty_redeem"])
    dd["day"] = pd.to_datetime(dd.day)
    put(con, "dash_discount_day", dd)

    g = P.groupby(["store_key", "brand", "category"], as_index=False).agg(
        baskets=("baskets", "sum"), units=("units", "sum"), net=("net", "sum"), margin=("gm", "sum"))
    for c in ("baskets", "units", "net", "margin"):
        g[c] = g[c] * .38
    g["baskets"] = ints(g.baskets)
    g["discount"] = g.net * RNG.uniform(.09, .2, len(g))
    put(con, "dash_discount_brand", g[["store_key", "brand", "category", "baskets", "units", "net", "discount", "margin"]])

    groups = [("Frequent Flyer", "Loyalty tier"), ("Travel Club", "Loyalty tier"),
              ("Veteran", "Community"), ("Senior 65+", "Community"), ("Medical Patient", "Community"),
              ("Retail Worker Discount", "Industry"), ("High Neighbor Rate", "Community"),
              ("Staff", "Staff / friends & family"), ("Friends & Family", "Staff / friends & family"),
              ("Industry Partner", "Industry")]
    rows = []
    for s in STORE_NAMES:
        for name, kind in groups:
            baskets = int(RNG.integers(300, 4200) * STORE_WEEKLY_NET[s] / 150_000)
            net = baskets * RNG.uniform(48, 75)
            disc = net * RNG.uniform(.12, .28)
            loy = disc * (RNG.uniform(.4, .7) if kind == "Loyalty tier" else RNG.uniform(.0, .15))
            rows.append((s, name, kind, int(baskets * .22), baskets, net, disc, loy, disc - loy,
                         int(baskets * .6), (disc - loy) * .6))
    put(con, "dash_discount_group", pd.DataFrame(rows, columns=[
        "store_key", "group_name", "group_kind", "members", "baskets", "net", "discount", "loyalty",
        "other_discount", "windowed_baskets", "windowed_other"]))

    gl = []
    for name, kind in groups:
        if kind == "Loyalty tier":
            continue
        pre = RNG.uniform(180, 320)
        post = pre * RNG.uniform(.9, 1.3)
        med = (post - pre) * RNG.uniform(.2, .8)
        lo, hi = med - RNG.uniform(8, 40), med + RNG.uniform(8, 40)
        gl.append(dict(group_name=name, group_kind=kind, window_days=90,
                       interpretable=kind != "Staff / friends & family", members=int(RNG.integers(35, 420)),
                       spend_pre=pre, spend_post=post, median_change=med, ci_lo=lo, ci_hi=hi,
                       excludes_zero=bool(lo > 0 or hi < 0), pct_increased=RNG.uniform(42, 61),
                       visits_pre=RNG.uniform(2, 4), visits_post=RNG.uniform(2, 5),
                       discount_pre=RNG.uniform(10, 30), discount_post=RNG.uniform(14, 40),
                       margin_pre=pre * .5, margin_post=post * .48, median_margin_change=med * .45))
    put(con, "dash_group_lift", pd.DataFrame(gl))
    put(con, "dash_group_lift_meta", pd.DataFrame([dict(
        window_days=90, min_members=30, min_join_date=dt.date(2020, 1, 1),
        members_evaluated=int(sum(r["members"] for r in gl)), groups_published=len(gl),
        built_at=pd.Timestamp.now().floor("s"))]))


def build_gwp(con):
    gw = [("Woodstock", "2026-05-01", ["Woodstock Rolling Tray (GWP)", "Woodstock Lighter (GWP)"]),
          ("Timeless", "2026-05-14", ["Timeless Grinder (GWP)", "Timeless Tote (GWP)"]),
          ("Ruby", "2026-07-16", ["Ruby Doobies Tin (GWP)", "Ruby Lighter (GWP)"]),
          ("Wyld", "2026-09-15", ["Wyld Hat (GWP)", "Wyld Rolling Papers (GWP)"])]
    rec, day_rows, sus = [], [], []
    sku = 480100
    for brand, start, items in gw:
        st_ = dt.date.fromisoformat(start)
        for it in items:
            sku += 7
            for s in STORE_NAMES:
                for off, qty in ((-6, 120), (14, 60)):
                    d = st_ + dt.timedelta(days=off)
                    if d <= LAST_DAY:
                        rec.append((s, d, brand, it, str(sku), float(qty)))
                for d in ALL_DAYS:
                    if st_ <= d <= st_ + dt.timedelta(days=33):
                        for ch, chs in CHAN_SHARE[s].items():
                            k = RNG.poisson(2.4 * chs * DOW_W[d.weekday()] * (STORE_WEEKLY_NET[s] / 150_000))
                            if k:
                                day_rows.append((s, d, brand, it, ch, int(k), 0.01 * k))
                        if RNG.random() < .1:
                            sus.append((s, d, str(sku), int(RNG.integers(1, 3)), int(RNG.integers(1, 4)), RNG.uniform(0, 12)))
    put(con, "dash_gwp_receipt", pd.DataFrame(rec, columns=["store_key", "day", "brand", "product", "product_sku", "units_received"]).assign(day=lambda d: pd.to_datetime(d.day)))
    put(con, "dash_gwp_day", pd.DataFrame(day_rows, columns=["store_key", "day", "brand", "product", "channel", "units", "net"]).assign(day=lambda d: pd.to_datetime(d.day)))
    s2 = pd.DataFrame(sus, columns=["store_key", "day", "product", "lines", "units", "net"])
    s2["day"] = pd.to_datetime(s2.day)
    s2 = s2.groupby(["store_key", "day", "product"], as_index=False).sum()
    put(con, "dash_suspect_lines", s2)


# ====================================================================
# 4. channel / promo-lab tables
# ====================================================================
def build_channel(con, core):
    cw = core["cw"]
    last13 = cw[cw.iso_week > WEEK_NUMS[-1] - 13]
    # channel x dow (store level)
    rows, daysd = [], []
    for s in STORE_NAMES:
        for ch, chs in CHAN_SHARE[s].items():
            sub = last13[(last13.store_key == s) & (last13.channel == ch)]
            wk_net = sub.groupby("iso_week").net.sum().mean()
            wk_b = sub.drop_duplicates("iso_week").baskets.mean()
            for dow in range(1, 8):
                f = DOW_W[dow - 1] / 7
                nd = 13
                rows.append((s, ch, dow, int(wk_b * f * nd), wk_net * f * nd,
                             wk_net * f * nd * .5, ints(np.array([wk_net * f * nd / 22]))[0],
                             wk_net * f * nd * .09, nd, 91,
                             ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][dow - 1]))
                daysd.append((s, ch, dow, nd, wk_net * f, wk_net * f * RNG.uniform(.12, .3),
                              wk_b * f, wk_b * f * RNG.uniform(.1, .25)))
    dow_df = pd.DataFrame(rows, columns=["store_key", "channel", "dow", "baskets", "net", "gm", "units",
                                         "discount", "days", "window_days", "dow_name"])
    put(con, "dash_channel_dow", dow_df)
    put(con, "dash_channel_daysd", pd.DataFrame(daysd, columns=["store_key", "channel", "dow", "days", "mean_net", "sd_net", "mean_baskets", "sd_baskets"]))

    pair = []
    for s in [0] + list(STORE_NAMES):
        chans = list(CHAN_SHARE[s]) if s else CHANNELS
        for a in chans:
            for b in chans:
                if a != b:
                    n = int(RNG.integers(60, 400) * (4 if s == 0 else 1))
                    pair.append((s, a, b, n, CHAN_AOV[a], CHAN_AOV[b] * RNG.uniform(.95, 1.1),
                                 CHAN_AOV[b] / CHAN_AOV[a] * RNG.uniform(.92, 1.08)))
    put(con, "dash_channel_pair", pd.DataFrame(pair, columns=["store_key", "ch_from", "ch_to", "customers", "aov_from", "aov_to", "median_ratio"]))
    st = []
    for s in [0] + list(STORE_NAMES):
        chans = [c for c in (CHAN_SHARE[s] if s else CHANNELS) if c != "In-Store"]
        for ch in chans:
            st.append((s, ch, int(RNG.integers(80, 500) * (4 if s == 0 else 1)),
                       RNG.uniform(.35, .6), RNG.uniform(1.2, 3.4)))
    put(con, "dash_channel_stick", pd.DataFrame(st, columns=["store_key", "channel", "first_timers", "repeat_rate", "avg_later_baskets"]))


# ====================================================================
# 5. loyalty, new vs returning, retention
# ====================================================================
TIERS = {"Frequent Flyer": (.12, 1.35), "Travel Club": (.28, 1.05), "Non-Loyalty": (.60, .88)}


def build_loyalty(con, core):
    bw, cw = core["bw"], core["cw"]
    wk = bw.groupby(["store_key", "iso_year", "iso_week"], as_index=False).agg(
        baskets=("baskets", "sum"), net=("net", "sum"), rb=("redeem_baskets", "sum"), rv=("redeem_value", "sum"))
    def with_chain(df, keys):
        chain = df.drop(columns="store_key").groupby(keys, as_index=False).sum(numeric_only=True)
        chain.insert(0, "store_key", 0)
        return pd.concat([chain, df], ignore_index=True)
    # week x tier
    rows = []
    for r in wk.itertuples():
        for t, (share, aov) in TIERS.items():
            bk = r.baskets * share * RNG.normal(1, .03)
            net = bk * (r.net / r.baskets) * aov
            rb = r.rb * (share * (2.6 if t != "Non-Loyalty" else .0) + (.02 if t == "Non-Loyalty" else 0))
            rows.append((r.store_key, r.iso_year, r.iso_week, t, int(bk * .72), int(bk), net,
                         rb * 6.8, int(rb)))
    lw = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "tier", "customers", "baskets", "net", "redeem_value", "redeem_baskets"])
    chain = lw.groupby(["iso_year", "iso_week", "tier"], as_index=False).agg(
        customers=("customers", lambda s: int(s.sum() * .78)), baskets=("baskets", "sum"), net=("net", "sum"),
        redeem_value=("redeem_value", "sum"), redeem_baskets=("redeem_baskets", "sum"))
    chain.insert(0, "store_key", 0)
    put(con, "dash_loyalty_week", pd.concat([chain, lw], ignore_index=True)[lw.columns])

    # channel x tier
    rows = []
    for r in bw.itertuples():
        for t, (share, aov) in TIERS.items():
            bk = r.baskets * share
            rows.append((r.store_key, r.iso_year, r.iso_week, r.channel, t, int(bk * .72), int(bk),
                         bk * (r.net / r.baskets) * aov, int(r.redeem_baskets * share * (2.6 if t != "Non-Loyalty" else .05))))
    lc = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "channel", "tier", "customers", "baskets", "net", "redeem_baskets"])
    chain = lc.groupby(["iso_year", "iso_week", "channel", "tier"], as_index=False).sum(numeric_only=True).drop(columns="store_key", errors="ignore")
    chain.insert(0, "store_key", 0)
    put(con, "dash_loyalty_channel_week", pd.concat([chain, lc], ignore_index=True)[lc.columns])

    # category x tier
    rows = []
    cat = cw.groupby(["store_key", "iso_year", "iso_week", "category"], as_index=False).agg(net=("net", "sum"), units=("units", "sum"), bw=("baskets_with", "sum"))
    for r in cat.itertuples():
        for t, (share, aov) in TIERS.items():
            rows.append((r.store_key, r.iso_year, r.iso_week, r.category, t, int(r.bw * share * .72),
                         r.net * share * aov, int(r.units * share * aov)))
    lcat = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "category", "tier", "customers", "net", "units"])
    chain = lcat.groupby(["iso_year", "iso_week", "category", "tier"], as_index=False).sum(numeric_only=True).drop(columns="store_key", errors="ignore")
    chain.insert(0, "store_key", 0)
    put(con, "dash_loyalty_category_week", pd.concat([chain, lcat], ignore_index=True)[lcat.columns])

    # offer x tier
    offs = ["Travel Club 500 Points Reward - Rythm Pre-Roll", "Travel Club 1000 Points Reward - Rythm Flower 3.5g",
            "Travel Club 100 Points Reward - Travel Agency Merch", "Travel Club 1500 Points Reward - Select Vape",
            "Travel Club 500 Points Reward - Rythm Edible"]
    rows = []
    for r in wk.itertuples():
        for o in offs:
            for t, f in (("Frequent Flyer", .5), ("Travel Club", .5)):
                n = r.rb * RNG.uniform(.05, .22) * f
                rows.append((r.store_key, r.iso_year, r.iso_week, o, t, int(n * .8), int(n), n * RNG.uniform(7, 14), RNG.uniform(70, 95)))
    lo = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "offer_name", "tier", "customers", "redemptions", "redeem_value", "avg_basket"])
    chain = lo.groupby(["iso_year", "iso_week", "offer_name", "tier"], as_index=False).agg(
        customers=("customers", "sum"), redemptions=("redemptions", "sum"), redeem_value=("redeem_value", "sum"), avg_basket=("avg_basket", "mean"))
    chain.insert(0, "store_key", 0)
    put(con, "dash_loyalty_offer", pd.concat([chain, lo], ignore_index=True)[lo.columns])

    # bins
    rows = []
    bins = [("At or above $125", 1, .2), ("Between $100 and $124", 2, .17), ("Below $100", 3, .63)]
    for r in lw.itertuples():
        for lab, o, f in bins:
            ff = f * (1.6 if (r.tier == "Frequent Flyer" and o < 3) else 1.0) * (.6 if (r.tier == "Non-Loyalty" and o < 3) else 1.0)
            ff = min(ff, .9)
            rows.append((r.store_key, r.iso_year, r.iso_week, r.tier, lab, o, int(r.baskets * ff),
                         r.baskets * ff * (140 if o == 1 else 110 if o == 2 else 52), int(r.customers * ff)))
    lb = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "tier", "bin_label", "bin_order", "baskets", "net", "customers"])
    chain = lb.groupby(["iso_year", "iso_week", "tier", "bin_label", "bin_order"], as_index=False).agg(
        baskets=("baskets", "sum"), net=("net", "sum"), customers=("customers", lambda s: int(s.sum() * .78)))
    chain.insert(0, "store_key", 0)
    put(con, "dash_loyalty_bins", pd.concat([chain, lb], ignore_index=True)[lb.columns])

    put(con, "dash_loyalty_roster", pd.DataFrame([("Frequent Flyer", 4_100, 3_700), ("Travel Club", 21_500, 14_300), ("Non-Loyalty", 36_000, 36_000)],
                                                   columns=["tier", "roster", "transacted"]))
    months = pd.date_range("2025-09-01", "2026-10-01", freq="MS")
    put(con, "dash_loyalty_enrollment", pd.DataFrame({"month": months.date, "enrollments": ints(RNG.normal(300, 60, len(months)) + np.arange(len(months)) * 8)}))
    put(con, "dash_loyalty_meta", pd.DataFrame([dict(matched_customers=35_600, transacting_customers=54_000,
                                                       tiers_built_at=pd.Timestamp.now().floor("s"), built_at=pd.Timestamp.now().floor("s"))]))


def build_newret(con, core):
    bw = core["bw"]
    wk = bw.groupby(["store_key", "iso_year", "iso_week"], as_index=False).agg(baskets=("baskets", "sum"), net=("net", "sum"))
    rows = []
    for r in wk.itertuples():
        i = r.iso_week - WEEK_NUMS[0]
        new_frac = .62 * math.exp(-i / 2.0) + .085 + RNG.normal(0, .006)
        for scope, cov in (("resolved", .83), ("all", 1.0)):
            cust = r.baskets * .78 * cov
            for seg, f in (("New", new_frac), ("Returning", 1 - new_frac)):
                bk = r.baskets * cov * (f if seg == "New" else f * 1.12)
                rows.append((r.store_key, r.iso_year, r.iso_week, scope, seg, int(cust * f), int(bk),
                             bk * (r.net / r.baskets) * (.9 if seg == "New" else 1.04)))
    nr = pd.DataFrame(rows, columns=["store_key", "iso_year", "iso_week", "id_scope", "segment", "customers", "baskets", "net"])
    chain = nr.groupby(["iso_year", "iso_week", "id_scope", "segment"], as_index=False).sum(numeric_only=True).drop(columns="store_key", errors="ignore")
    chain.insert(0, "store_key", 0)
    put(con, "dash_newret_week", pd.concat([chain, nr], ignore_index=True)[nr.columns])
    put(con, "dash_newret_meta", pd.DataFrame([dict(first_txn=pd.Timestamp(FIRST_DAY), last_txn=pd.Timestamp(LAST_DAY), burn_in_weeks=4)]))


def build_retention(con):
    asof = pd.Timestamp(LAST_DAY)
    months = pd.date_range("2026-03-01", "2026-10-01", freq="MS")
    scopes = {"resolved": 1.0, "bounded": 1.14}
    coh, seq, gaps, summ, roll = [], [], [], [], []
    for scope, sf in scopes.items():
        for ch in CHANNELS:
            chsh = {"In-Store": 1.0, "Non-Stop": .32, "Delivery": .15}[ch]
            for store in range(5):
                ssh = 1.0 if store == 0 else STORE_WEEKLY_NET[store] / 630_000
                if store and ch == "Delivery" and store == 3:
                    continue
                for m in months:
                    base = int(2400 * sf * chsh * ssh * RNG.uniform(.85, 1.15))
                    if base < 25:
                        continue
                    d_obs = (asof - (m + pd.Timedelta(days=14))).days
                    p_rep = .62 * (1 - math.exp(-d_obs / 55)) * (.8 if ch != "In-Store" else 1) * RNG.uniform(.95, 1.05)
                    rep = int(base * p_rep)
                    orders = int(base * (1 + p_rep * 2.6 * (1 - math.exp(-d_obs / 150))))
                    coh.append((scope, m.date(), ch, store, base, orders, orders * 61.0, rep,
                                int(rep * .38), int(rep * min(d_obs, 240) * .45), float(d_obs)))
    put(con, "dash_retention_cohort", pd.DataFrame(coh, columns=[
        "id_scope", "cohort_month", "first_channel", "first_store", "customers", "orders", "revenue",
        "repeaters", "sticky", "lifespan_days", "days_observed"]))
    for scope, sf in scopes.items():
        for ch in CHANNELS:
            chsh = {"In-Store": 1.0, "Non-Stop": .32, "Delivery": .15}[ch]
            n = int(14000 * sf * chsh)
            av = [58, 61, 63]
            gap = [18, 24, 31]
            for i in range(3):
                cust = int(n * (1, .52, .36)[i] * (1 if scope == "resolved" else .95))
                seq.append((scope, ch, i + 1, f"Basket {i + 1}", cust, av[i] * (1 if ch == "In-Store" else 1.12),
                            av[i] * .88, cust * av[i], None if i == 0 else gap[i] * RNG.uniform(.9, 1.1),
                            None if i == 0 else gap[i] * .7))
            for k, (lab, o, f) in enumerate([("0-7 days", 1, .17), ("8-14 days", 2, .2), ("15-30 days", 3, .27),
                                              ("31-60 days", 4, .2), ("61-90 days", 5, .09), ("90+ days", 6, .07)]):
                gaps.append((scope, ch, lab, o, int(n * 2.1 * f * RNG.uniform(.95, 1.05))))
            rep = int(n * .52)
            summ.append((scope, ch, n, int(n * 2.9), n * 2.9 * 61.0, rep, int(rep * .4), 96.0, 27.0, 11.0, 22.0, 41.0))
            for m in months:
                mm = int(1900 * sf * chsh * (1 + .05 * (m.month - 3)))
                roll.append((m.date(), scope, ch, mm, int(mm * 2.4), int(mm * .58), int(mm * .17), min(6, m.month - 2)))
    put(con, "dash_retention_sequence", pd.DataFrame(seq, columns=[
        "id_scope", "first_channel", "seq", "seq_label", "customers", "avg_value", "median_value",
        "revenue", "avg_days_since_prev", "median_days_since_prev"]))
    put(con, "dash_retention_gaps", pd.DataFrame(gaps, columns=["id_scope", "first_channel", "gap_bucket", "bucket_order", "gaps"]))
    put(con, "dash_retention_summary", pd.DataFrame(summ, columns=[
        "id_scope", "first_channel", "customers", "orders", "revenue", "repeaters", "sticky",
        "avg_lifespan_days", "avg_gap_days", "gap_p25", "gap_p50", "gap_p75"]))
    put(con, "dash_retention_rolling", pd.DataFrame(roll, columns=[
        "month", "id_scope", "first_channel", "customers", "visits", "repeat_customers", "heavy_customers", "window_months"]))
    put(con, "dash_retention_meta", pd.DataFrame([
        ("resolved", 61_000, 74_000, 61_000, 9_500, 3_500, FIRST_DAY, LAST_DAY, pd.Timestamp.now().floor("s")),
        ("bounded", 70_500, 74_000, 61_000, 9_500, 3_500, FIRST_DAY, LAST_DAY, pd.Timestamp.now().floor("s"))],
        columns=["id_scope", "customers", "all_customers", "resolved_customers", "singleton_customers",
                 "excluded_multi_hash", "cov_start", "cov_end", "built_at"]))


# ====================================================================
# 6. events, event return/tracker, audiences
# ====================================================================
EVENTS = [  # name, date, type, series, partners, int/ext, store, offsite, gross_cost, brand_offset
    ("Rythm Launch Party", "2026-03-12", "TTA Brand Event", "Brand Takeover", "Rythm", "Internal", 4, False, 4200, 1500),
    ("420 Block Party", "2026-04-20", "TTA Brand Event", "One-off", "Select|Dark Heart", "Internal", 0, True, 18500, 6000),
    ("Select Budtender Day", "2026-04-08", "In-Store Activation", "Budtender Series", "Select", "Internal", 2, False, 900, 300),
    ("Woodstock Pop-Up", "2026-05-09", "In-Store Activation", "Pop-Up Series", "Woodstock", "Internal", 1, False, 1800, 600),
    ("Timeless Glass Night", "2026-05-22", "In-Store Activation", "Pop-Up Series", "Timeless", "Internal", 3, False, 1500, 400),
    ("Pride Weekend Activation", "2026-06-27", "Community", "One-off", "", "Internal", 0, True, 9600, 2000),
    ("Ruby Farms Doobie Drop", "2026-07-18", "In-Store Activation", "Brand Takeover", "Ruby", "Internal", 4, False, 2100, 900),
    ("Ruby Farms Doobie Drop", "2026-07-18", "In-Store Activation", "Brand Takeover", "Ruby", "Internal", 2, False, 2100, 900),
    ("Summer Budtender Showcase", "2026-07-30", "In-Store Activation", "Budtender Series", "", "Internal", 4, False, 1100, 0),
    ("Rooftop Customer Appreciation", "2026-08-15", "TTA Brand Event", "One-off", "", "Internal", 0, True, 12400, 3000),
    ("Soho Gallery Takeover", "2026-08-21", "Community", "Pop-Up Series", "", "Internal", 3, False, 2600, 0),
    ("DTBK Anniversary", "2026-09-05", "TTA Brand Event", "One-off", "", "Internal", 1, False, 3300, 800),
    ("Wyld Tasting Night", "2026-09-19", "In-Store Activation", "Brand Takeover", "Wyld", "Internal", 4, False, 1400, 600),
    ("Fall Kickoff Market", "2026-10-03", "Community", "One-off", "", "Internal", 0, True, 7800, 1500),
]


def build_events(con, core):
    import publish_events as pe
    rows = []
    for i, e in enumerate(EVENTS):
        name, d, typ, series, partners, ie, sk, off, gross, offset = e
        rid = f"recFIX{i:04d}"
        net = gross - offset
        rows.append(dict(
            event_id=f"{rid}-{sk}", airtable_record_id=rid, event_name=name, event_date=pd.Timestamp(d),
            event_type=typ, series=series, brand_partners=partners, internal_external=ie, store_key=sk,
            is_offsite=off, n_stores=1, has_control=(not off), cost_state="recorded" if i != 9 else "no_budget_linked",
            cost_recorded=(i != 9), direct_cost=gross * .8, shared_cost_allocated=gross * .2, gross_cost=float(gross),
            brand_offset=float(offset), net_tta_cost=float(net), shared_lines=0,
            expected_attendance=int(RNG.integers(60, 400)), net_tta_cost_export=float(net), realloc_delta=0.0))
    ev = pd.DataFrame(rows)
    # same-day / same-name rows collapse: give the 2nd Ruby row a different record id
    ev.loc[7, "airtable_record_id"] = ev.loc[6, "airtable_record_id"]
    ev["event_id"] = [f"{r}-{k}" for r, k in zip(ev.airtable_record_id, ev.store_key)]
    store_day = {"net": {s: STORE_WEEKLY_NET[s] / 7 for s in STORE_NAMES},
                 "baskets": {s: STORE_WEEKLY_NET[s] / 7 / 60 for s in STORE_NAMES},
                 "new_customers": {s: 14 * STORE_WEEKLY_NET[s] / 150_000 for s in STORE_NAMES}}
    det = []
    for m in pe.METRICS:
        for e in ev.itertuples():
            sk = 0 if e.is_offsite else int(e.store_key)
            for off in pe.OFFSETS:
                base = sum(store_day[m].values()) if sk == 0 else store_day[m][sk]
                peak = {0: 1.0, 1: .35, -1: .08, 2: .12, -2: .03}[off]
                drift = RNG.normal(0, .03) if e.has_control else np.nan
                own = RNG.normal(.04 + (0.30 * peak if m != "net" else .22 * peak), .06)
                if e.is_offsite:
                    own = own + .05
                obs = base * (1 + own)
                cf = base * (1 + (drift if np.isfinite(drift) else 0))
                did = own - drift if np.isfinite(drift) else np.nan
                det.append(dict(metric=m, event_id=e.event_id, coincident=1, airtable_record_id=e.airtable_record_id,
                                event_name=e.event_name, event_date=e.event_date, event_type=e.event_type,
                                series=e.series, brand_partners=e.brand_partners,
                                store_name=pe.STORE_NAME[sk], store_key=sk, is_offsite=bool(e.is_offsite),
                                has_control=bool(e.has_control), offset=off, own_lift=own, control_drift=drift,
                                did=did, n_controls=int(RNG.integers(4, 7)),
                                controls_used=int(RNG.integers(3, 7)) if e.has_control else 0,
                                measured=bool(np.isfinite(obs - cf)),
                                observed=obs, baseline=base,
                                counterfactual=cf, incremental=obs - cf))
    detail = pd.DataFrame(det)
    detail["coincident"] = detail.groupby(["metric", "store_key", "event_date", "offset"])["event_id"].transform("nunique")
    detail["incremental_share"] = detail.incremental / detail.coincident
    detail["scope"] = detail.apply(pe._scope, axis=1)
    summary = pe._summaries(detail)
    cost = pe._cost_table(ev, detail)
    meta = pd.DataFrame([dict(
        events=int(ev.airtable_record_id.nunique()), cost_recorded=int(cost.cost_recorded.sum()),
        cost_unrecorded=int((~cost.cost_recorded).sum()), net_tta_cost_total=float(cost.net_tta_cost.sum()),
        event_rows=len(ev), single_store=int(ev[ev.has_control].airtable_record_id.nunique()),
        offsite=int(ev[ev.is_offsite].airtable_record_id.nunique()),
        coincident_store_days=int(detail[(detail.offset == 0) & (detail.metric == "net") & (detail.coincident > 1)]
                                  .drop_duplicates(["store_key", "event_date"]).shape[0]),
        cov_start=FIRST_DAY, cov_end=LAST_DAY, control_window_days=pe.CONTROL_WINDOW_DAYS,
        min_controls=pe.MIN_CONTROLS, trust_n=pe.TRUST_N, built_at=pd.Timestamp.now().floor("s"))])
    for name, df in (("dash_events_detail", detail), ("dash_events_summary", summary),
                     ("dash_events_cost", cost), ("dash_events_meta", meta)):
        put(con, name, df)
    return ev


def build_event_tables(con, ev):
    one = ev.drop_duplicates("airtable_record_id")
    rr = []
    for e in one.itertuples():
        mature = pd.Timestamp(e.event_date) + pd.Timedelta(days=90) <= pd.Timestamp(LAST_DAY)
        roster = int(RNG.integers(80, 420))
        matched = int(roster * RNG.uniform(.3, .6))
        for seg, f in (("New", .3), ("Regular", .7)):
            buyers = int(matched * f * RNG.uniform(.6, 1))
            ret = int(buyers * (RNG.uniform(.25, .45) if seg == "New" else RNG.uniform(.6, .85)))
            rr.append((e.event_id, e.event_name, pd.Timestamp(e.event_date).date(), e.event_type,
                       e.store_key, e.is_offsite, roster, matched, seg, buyers, ret, bool(mature)))
    put(con, "dash_event_return", pd.DataFrame(rr, columns=["event_id", "event_name", "event_date", "event_type", "store_key", "is_offsite",
                                                            "roster_members", "pos_matched", "segment", "buyers", "returned", "mature"]))
    put(con, "dash_event_return_meta", pd.DataFrame([dict(window_days=90, min_measurable=10, data_through=LAST_DAY,
                                                           events_mapped=len(one), events_immature=int(sum(not r[-1] for r in rr) / 2))]))
    tr = []
    for e in one.itertuples():
        ed = pd.Timestamp(e.event_date)
        for bucket, f in (("new", .5), ("active", .3), ("lapsed", .2)):
            su = int(RNG.integers(60, 320) * f * 2)
            sm = int(su * .7)
            row = [e.airtable_record_id, e.event_name, ed.date(), e.event_type, e.series, bool(e.is_offsite),
                   e.cost_state, bool(e.cost_recorded), e.net_tta_cost, e.gross_cost, e.brand_offset, bucket,
                   su, sm, su - sm, int(sm * .8)]
            for w, frac in ((0, .1), (30, .22), (90, .3)):
                b = int(sm * frac * (1.5 if bucket != "new" else 1) * RNG.uniform(.8, 1.1))
                row += [b, b * RNG.uniform(60, 90), bool(ed + pd.Timedelta(days=w) <= pd.Timestamp(LAST_DAY))]
            tr.append(row)
    cols = ["airtable_record_id", "event_name", "event_date", "event_type", "series", "is_offsite", "cost_state",
            "cost_recorded", "net_tta_cost", "gross_cost", "brand_offset", "bucket", "signups", "signups_matchable",
            "signups_unmatchable", "resolvable", "buyers_d0", "revenue_d0", "mature_d0", "buyers_d30", "revenue_d30",
            "mature_d30", "buyers_d90", "revenue_d90", "mature_d90"]
    t = pd.DataFrame(tr, columns=cols)
    put(con, "dash_event_tracker", t)
    put(con, "dash_event_tracker_meta", pd.DataFrame([dict(
        lapse_days=90, data_through=LAST_DAY, events=len(one), events_costed=int(one.cost_recorded.sum()),
        events_mature_90=int(t[t.mature_d90].airtable_record_id.nunique()), has_attendance=False,
        signups_unmatchable_total=int(t.signups_unmatchable.sum()), signups_total=int(t.signups.sum()))]))

    # audiences (mirrors config/audience_snapshot/*.csv)
    coh, ret = [], []
    camp = ["(none)", "420", "Ruby Farms", "Summer"]
    for i, e in enumerate(one.itertuples()):
        roster = int(RNG.integers(120, 600))
        pos = int(roster * RNG.uniform(.25, .75))
        newc = int(pos * RNG.uniform(.05, .2))
        same = int(pos * RNG.uniform(.1, .3))
        r30, r60, r90 = int(pos * .5), int(pos * .6), int(pos * .66)
        rev = r90 * RNG.uniform(70, 110)
        aid = str(300000 + i)
        coh.append((aid, e.event_name, pd.Timestamp(e.event_date).date(), "Not In Store" if e.is_offsite else STORE_NAMES[e.store_key],
                    e.event_type, camp[i % 4], roster, pos, round(pos / roster, 3), int(pos * .97), newc, pos - newc, same,
                    int(same * 1.1), same * 85.0, r30, r60, r90, rev, int(newc * .4), newc * 60.0, r90 - int(newc * .4),
                    rev - newc * 60.0, pd.Timestamp(e.event_date).date(), int(same * 1.3), "confirmed", ""))
        cum = np.cumsum(RNG.poisson(max(r90 / 60, .5), 90) + (np.arange(90) < 14) * RNG.poisson(r90 / 40, 90))
        for d in range(1, 91):
            ret.append((d, int(min(cum[d - 1], r90)), aid, e.event_name))
    cohorts = pd.DataFrame(coh, columns=[
        "audience_id", "event_name", "event_date", "store", "event_type", "campaign", "roster", "with_pos_record",
        "pos_record_rate", "ever_purchased", "new_customers", "existing_customers", "same_day_buyers", "same_day_receipts",
        "same_day_revenue", "returned_30d", "returned_60d", "returned_90d", "revenue_90d", "new_returned_90d",
        "new_revenue_90d", "existing_returned_90d", "existing_revenue_90d", "detected_peak_date", "peak_customers",
        "date_check", "warnings"])
    put(con, "dash_audience_cohorts", cohorts)
    cg = cohorts.groupby("campaign", as_index=False).agg(
        events=("audience_id", "count"), attendees=("roster", "sum"), mean_pos_rate=("pos_record_rate", "mean"),
        new_customers=("new_customers", "sum"), same_day_buyers=("same_day_buyers", "sum"),
        returned_90d=("returned_90d", "sum"), revenue_90d=("revenue_90d", "sum"),
        new_returned_90d=("new_returned_90d", "sum"), new_revenue_90d=("new_revenue_90d", "sum"),
        existing_returned_90d=("existing_returned_90d", "sum"), existing_revenue_90d=("existing_revenue_90d", "sum"))
    cg["gross_cost"] = np.where(cg.campaign == "(none)", np.nan, cg.events * 5200.0)
    cg["net_cost_to_tta"] = cg.gross_cost * .7
    cg["cost_per_new_customer"] = cg.net_cost_to_tta / cg.new_customers.replace(0, np.nan)
    cg["cost_per_returner"] = cg.net_cost_to_tta / cg.returned_90d.replace(0, np.nan)
    cg["revenue_per_dollar"] = cg.revenue_90d / cg.net_cost_to_tta
    cg["cost_note"] = np.where(cg.campaign == "(none)", "", "cost is spread over a mostly non-purchasing audience")
    put(con, "dash_audience_campaigns", cg)
    put(con, "dash_audience_returns", pd.DataFrame(ret, columns=["day_offset", "cum_returners", "audience_id", "event_name"]))
    put(con, "dash_audience_meta", pd.DataFrame([dict(
        generated_at=str(pd.Timestamp.now().floor("s")), events_mapped=len(cohorts), audiences_total=len(cohorts) + 4,
        attendees=int(cohorts.roster.sum()), attendees_with_pos_record=int(cohorts.with_pos_record.sum()),
        pct_never_transacted=round(100 * (1 - cohorts.with_pos_record.sum() / cohorts.roster.sum()), 1),
        new_customers=int(cohorts.new_customers.sum()), revenue_90d=float(cohorts.revenue_90d.sum()),
        campaigns_costed=int((cg.gross_cost.notna()).sum()), date_min=cohorts.event_date.min(), date_max=cohorts.event_date.max())]))


# ====================================================================
# 7. visual merchandising -- run the real publish_vm.build on a fake src
# ====================================================================
def build_vm(dash_path: Path):
    import publish_vm as pv
    tmp = Path(tempfile.mkdtemp(prefix="cerebral_vm_"))
    src = tmp / "src.duckdb"
    s = duckdb.connect(str(src))
    s.execute("""CREATE TABLE fact_vm_placement (store VARCHAR, month INTEGER, week_n INTEGER,
        bay_raw VARCHAR, bay_type VARCHAR, shelf_tier VARCHAR, position_label VARCHAR, impact_note VARCHAR,
        brand_raw VARCHAR, brand VARCHAR, brand_key VARCHAR, product_hint VARCHAR, is_brand BOOLEAN, comment VARCHAR)""")
    s.execute("""CREATE TABLE dim_vm_week (store VARCHAR, store_key INTEGER, month INTEGER, week_n INTEGER,
        week_start DATE, week_end DATE, iso_year INTEGER, iso_week INTEGER, source VARCHAR)""")
    s.execute("""CREATE TABLE dim_takeover (brand VARCHAR, start_date DATE, end_date DATE, stores VARCHAR,
        surface VARCHAR, source VARCHAR, airtable_record_id VARCHAR, notes VARCHAR)""")
    s.execute("""CREATE TABLE vm_ingest_log (sheet VARCHAR, store VARCHAR, month INTEGER, placements INTEGER,
        notes INTEGER, weeks VARCHAR, warnings VARCHAR, ingested_at VARCHAR)""")
    s.execute("""CREATE TABLE fact_inventory_week (store_key INTEGER, iso_year INTEGER, iso_week INTEGER,
        brand VARCHAR, product VARCHAR, category VARCHAR, floor_start DOUBLE, floor_end DOUBLE, total_end DOUBLE,
        received DOUBLE, sold_units DOUBLE, moved_to_floor DOUBLE, stockout_floor BOOLEAN, days_of_supply_floor DOUBLE)""")

    bays = [("Flower Wall A", "Flower"), ("Flower Wall B", "Flower"), ("Pre-Roll Case", "Pre-Roll"),
            ("Vape Wall", "Vape"), ("Edibles Case", "Edible"), ("Spotlight Bay", "Spotlight")]
    cat_brands = {"Flower": BRANDS["Flower"], "Pre-Roll": BRANDS["Pre-Roll"], "Vape": BRANDS["Vape"],
                  "Edible": BRANDS["Edible"], "Spotlight": ["Rythm", "Select", "Dark Heart", "Woodstock", "Timeless", "Ruby", "Wyld", "Find."]}
    place, weeks, log = [], [], []
    for sk, sname in VM_STORE.items():
        for month in range(3, 11):
            first = dt.date(YEAR, month, 1)
            first_mon = first + dt.timedelta(days=(7 - first.weekday()) % 7)
            wk_list = []
            for n in range(1, 6):
                ws = first_mon + dt.timedelta(days=7 * (n - 1))
                if ws.month != month or ws > LAST_DAY:
                    continue
                iso = ws.isocalendar()
                wk_list.append(n)
                weeks.append((sname, sk, month, n, ws, ws + dt.timedelta(days=6), iso[0], iso[1], "rule"))
                for bay, btype in bays:
                    pool = cat_brands[btype]
                    for tier in ("Top", "Middle", "Bottom"):
                        for pos in (1, 2):
                            idx = (month * 3 + n // 3 + pos + len(tier) + sk) % len(pool)
                            brand = pool[idx]
                            if btype == "Spotlight":
                                for tb, (a, z, _) in LIFT.items():
                                    if a <= ws + dt.timedelta(days=3) <= z + dt.timedelta(days=6):
                                        brand = tb
                            raw = brand if RNG.random() > .08 else brand + " " + btype
                            if RNG.random() < .03:
                                raw = ""
                            place.append((sname, month, n, bay, btype, tier, f"{tier[0]}{pos}", "",
                                          raw, raw, "", "", bool(raw), ""))
            if wk_list:
                log.append((f"{sname} {month:02d}", sname, month, len(bays) * 6 * len(wk_list), 0,
                            ",".join(map(str, wk_list)), "", dt.datetime.now().isoformat(timespec="seconds")))
    # a few VM names that will not resolve, so the "unmatched" panel has content
    for sname in VM_STORE.values():
        place.append((sname, 9, 1, "Accessories Wall", "Accessory", "Top", "T1", "", "Glass House Bongs", "Glass House Bongs", "", "", True, ""))
        place.append((sname, 9, 1, "Spotlight Bay", "Spotlight", "Top", "T9", "", "Mystery Brand XYZ", "Mystery Brand XYZ", "", "", True, ""))
    s.executemany("INSERT INTO fact_vm_placement VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", place)
    s.executemany("INSERT INTO dim_vm_week VALUES (?,?,?,?,?,?,?,?,?)", weeks)
    s.executemany("INSERT INTO vm_ingest_log VALUES (?,?,?,?,?,?,?,?)", log)
    s.execute("INSERT INTO dim_takeover VALUES ('Good Green', DATE '2026-06-08', DATE '2026-06-21', 'USQ|SOHO', 'window', 'takeovers.csv', '', 'fixture')")

    # fake week-level inventory for resolved brands (floor stock)
    d = duckdb.connect(str(dash_path))
    bwk = d.execute("""SELECT store_key, iso_year, iso_week, brand, category, units
                       FROM dash_brand_week""").df()
    d.close()
    prods = bwk.sample(frac=.5, random_state=3)
    inv = pd.DataFrame({
        "store_key": prods.store_key, "iso_year": prods.iso_year, "iso_week": prods.iso_week,
        "brand": prods.brand, "product": prods.brand + " item", "category": prods.category,
        "floor_start": ints(prods.units * RNG.uniform(.8, 2.0, len(prods))).astype(float),
        "sold_units": prods.units.astype(float)})
    inv["received"] = ints(inv.sold_units * RNG.uniform(.7, 1.3, len(inv))).astype(float)
    inv["moved_to_floor"] = ints(inv.sold_units * RNG.uniform(.2, 1.0, len(inv))).astype(float)
    inv["floor_end"] = np.maximum(0, inv.floor_start + inv.moved_to_floor - inv.sold_units)
    inv["total_end"] = inv.floor_end * 2.2
    inv["stockout_floor"] = (inv.floor_end <= 0).astype(object)
    inv.loc[RNG.random(len(inv)) < .04, "stockout_floor"] = None
    inv["days_of_supply_floor"] = np.where(inv.sold_units > 0, inv.floor_end / (inv.sold_units / 7), np.nan)
    s.register("_inv", inv[["store_key", "iso_year", "iso_week", "brand", "product", "category", "floor_start", "floor_end",
                            "total_end", "received", "sold_units", "moved_to_floor", "stockout_floor", "days_of_supply_floor"]])
    s.execute("INSERT INTO fact_inventory_week SELECT * FROM _inv")
    s.close()

    app_copy = tmp / "app_takeovers.py"
    try:     # prefer the live TAKEOVERS list from the app, fall back to the copy above
        ast.parse((APP_DIR / "cerebral_public.py").read_text(encoding="utf-8"))
        app_copy = APP_DIR / "cerebral_public.py"
        pv.load_takeovers(app_copy)
    except Exception as exc:
        print(f"  (using built-in TAKEOVERS copy: {exc.__class__.__name__})")
        app_copy = tmp / "app_takeovers.py"
        app_copy.write_text("TAKEOVERS = " + repr(TAKEOVERS_FALLBACK) + "\n", encoding="utf-8")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pv.build(src, dash_path, app_copy)
    for ln in buf.getvalue().splitlines():
        print("  [vm]", ln)
    shutil.rmtree(tmp, ignore_errors=True)


# ====================================================================
# 8. promo lab -- fake fact_line / fact_basket, then the real publish_promo
# ====================================================================
# Its own generator so the sections above stay byte-identical run to run.
PROMO_RNG = np.random.default_rng(4242)
P_FIRST = dt.date(2026, 6, 1)      # 2026-W23 Monday
P_LAST = dt.date(2026, 10, 18)     # 2026-W42 Sunday (Wyld B2G1 ends this day)
P_DAILY_BASKETS = {1: 300, 2: 420, 3: 190, 4: 350}   # before the weekday curve
WYLD_GUMMY_PRICE = 25.0            # regular shelf price in the fake world
EIGHTH_PRICE = 40.0
# Spotlight items ("Fall Finds Under $40") -- brand, product, category, regular price.
# They sell at the regular price here: the fixture ends before that window opens.
SPOTLIGHT_ITEMS = [
    ("Clea", "Clea Midlife Beverage Booster", "Edible", 36.0),
    ("Good Green", "Good Green Runtz 3.5g", "Flower", EIGHTH_PRICE),
    ("MFNY", "MFNY Blue Dream 0.5g Cartridge", "Vape", 45.0),
    ("Papa & Barkley", "Papa & Barkley Releaf Gummies", "Edible", 34.0),
    ("Deep Black", "Deep Black Ashtray", "Accessory", 24.0),
    ("Stone Road", "Stone Road Hash 1g", "Concentrate", 42.0),
]
HOUR_W = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 5, 7, 8, 8, 8, 9, 10, 11, 10, 7, 4, 0, 0], dtype=float)
HOUR_W = HOUR_W / HOUR_W.sum()


class _PromoWorld:
    """Collects fake fact_line / fact_basket rows."""

    def __init__(self, rng):
        self.rng = rng
        self.lines, self.baskets = [], []
        self.bid = 5_000_000
        self.lid = 0
        self.cat_p = np.array([CATS[c]["pen"] for c in CATS]); self.cat_p /= self.cat_p.sum()
        self.cats = list(CATS)

    def ordinary_lines(self, n=None):
        rng = self.rng
        n = n or min(5, int(rng.geometric(.55)))
        out = []
        for _ in range(n):
            if rng.random() < .012:                                  # a spotlight item at shelf price
                brand, prod, cat, price = SPOTLIGHT_ITEMS[int(rng.integers(len(SPOTLIGHT_ITEMS)))]
                out.append((cat, brand, prod, 1.0, price * rng.uniform(.97, 1.03)))
                continue
            cat = self.cats[int(rng.choice(len(self.cats), p=self.cat_p))]
            brand = BRANDS[cat][int(rng.integers(len(BRANDS[cat])))]
            if cat == "Accessory":
                prod = f"{brand} {ACC_ITEMS[int(rng.integers(len(ACC_ITEMS)))]}"
            else:
                size = SIZES[cat][int(rng.integers(3))]
                prod = f"{brand} {STRAINS[int(rng.integers(len(STRAINS)))]} {size}"
            price = CATS[cat]["price"] * rng.uniform(.8, 1.3)
            if cat == "Edible" and brand == "Wyld":
                prod = f"Wyld {STRAINS[int(rng.integers(len(STRAINS)))]} 10pk Gummies"
                price = WYLD_GUMMY_PRICE * rng.uniform(.96, 1.04)
            elif cat == "Flower" and "3.5g" in prod:
                price = EIGHTH_PRICE * rng.uniform(.97, 1.03)
            units = 2.0 if rng.random() < .12 else 1.0
            out.append((cat, brand, prod, units, price))
        return out

    def add_basket(self, store, day, specs, discount=0.0, cust=None, known_p=.65, is_return=False):
        """specs: list of (category, brand, product, units, unit_price[, gm_pct])."""
        rng = self.rng
        self.bid += 1
        hour = int(rng.choice(24, p=HOUR_W))
        ts = dt.datetime(day.year, day.month, day.day, hour, int(rng.integers(60)), int(rng.integers(60)))
        chans = list(CHAN_SHARE[store]); probs = np.array([CHAN_SHARE[store][c] for c in chans])
        channel = chans[int(rng.choice(len(chans), p=probs / probs.sum()))]
        if cust is None and rng.random() < known_p:
            pool = 5000 + 45 * (day - P_FIRST).days          # the pool grows, so first visits keep happening
            cust = f"F{store}-{int(rng.integers(pool)):06d}"
        iso = day.isocalendar()
        sign = -1.0 if is_return else 1.0
        net_sum = gm_sum = units_sum = 0.0
        cats = set()
        for spec in specs:
            cat, brand, prod, units, price = spec[:5]
            gm_pct = spec[5] if len(spec) > 5 else CATS.get(cat, {"gm": .5})["gm"]
            net = sign * units * price
            cogs = sign * units * price * (1 - gm_pct) if price > 0 else sign * units * 4.0
            self.lid += 1
            self.lines.append((f"L{self.lid}", self.bid, store, ts, int(day.strftime("%Y%m%d")), iso[0], iso[1],
                               day.weekday() + 1, channel, cust, "dutchie" if cust else None, prod,
                               f"SKU{zlib.crc32(prod.encode()) % 900000 + 100000}", cat, cat, brand, sign * units,
                               net, cogs / units if units else 0.0, cogs, net - cogs, is_return))
            net_sum += net; gm_sum += net - cogs; units_sum += sign * units; cats.add(cat)
        self.baskets.append((self.bid, store, ts, int(day.strftime("%Y%m%d")), iso[0], iso[1], day.weekday() + 1,
                             channel, cust, cust is not None, net_sum, gm_sum, units_sum, len(specs), len(cats),
                             0.0, -abs(discount), False, is_return))

    def frames(self):
        lcols = ["line_id", "basket_id", "store_key", "txn_ts", "date_key", "iso_year", "iso_week", "day_of_week",
                 "channel", "customer_key", "customer_source", "product", "sku", "raw_category", "category", "brand",
                 "units", "net_sales", "unit_cost", "cogs", "gross_margin", "is_return"]
        bcols = ["basket_id", "store_key", "txn_ts", "date_key", "iso_year", "iso_week", "day_of_week", "channel",
                 "customer_key", "customer_known", "basket_net", "basket_margin", "basket_units", "basket_lines",
                 "distinct_cats", "loyalty_redeem", "discount_amt", "used_redemption", "is_return"]
        return pd.DataFrame(self.lines, columns=lcols), pd.DataFrame(self.baskets, columns=bcols)


def _wyld(rng, price, n=1):
    return ("Edible", "Wyld", f"Wyld {STRAINS[int(rng.integers(len(STRAINS)))]} 10pk Gummies", float(n), price)


def _eighth(rng, brand, price):
    return ("Flower", brand, f"{brand} {STRAINS[int(rng.integers(len(STRAINS)))]} 3.5g", 1.0, price)


def build_promo_src(src_path: Path):
    """Fake sales for W23-W42 with the promo behaviour from PROMO_LAB_SPEC injected."""
    rng = PROMO_RNG
    w = _PromoWorld(rng)
    d = dt.date
    windows = dict(wyld_3for60=(d(2026, 9, 15), d(2026, 9, 28)), wyld_spend65=(d(2026, 9, 29), d(2026, 10, 12)),
                   wyld_b2g1=(d(2026, 10, 13), d(2026, 10, 18)), gg_rythm=(d(2026, 10, 1), d(2026, 10, 31)),
                   ruby=(d(2026, 7, 16), d(2026, 8, 17)))
    inwin = lambda k, day: windows[k][0] <= day <= windows[k][1]
    day = P_FIRST
    while day <= P_LAST:
        for s in STORE_NAMES:
            scale = P_DAILY_BASKETS[s] / 300
            n = int(rng.poisson(P_DAILY_BASKETS[s] * DOW_W[day.weekday()]))
            for _ in range(n):
                disc = 0.0
                specs = w.ordinary_lines()
                if rng.random() < .08:
                    disc = .1 * sum(u * p for *_, u, p in specs)
                w.add_basket(s, day, specs, discount=disc)
            # -- 3 for $60: three gummies at $20 (plus a few at shelf price = qualifying, not applied)
            if inwin("wyld_3for60", day):
                for _ in range(int(rng.poisson(11 * scale))):
                    extra = w.ordinary_lines(1) if rng.random() < .5 else []
                    if rng.random() < .5:
                        specs = [_wyld(rng, 20.0, 3)] + extra
                    else:
                        specs = [_wyld(rng, 20.0) for _ in range(3)] + extra
                    w.add_basket(s, day, specs, discount=3 * (WYLD_GUMMY_PRICE - 20.0))
                for _ in range(int(rng.poisson(1.0 * scale))):            # rung without the deal
                    w.add_basket(s, day, [_wyld(rng, WYLD_GUMMY_PRICE) for _ in range(3)])
                # as the real export shows it: line prices are the period average (the
                # deal is NOT visible in the unit price), only discount_amt carries it
                for _ in range(int(rng.poisson(3 * scale))):
                    w.add_basket(s, day, [_wyld(rng, 22.5) for _ in range(3)], discount=3 * (WYLD_GUMMY_PRICE - 20.0))
                for _ in range(int(rng.poisson(4 * scale))):              # one gummy short
                    w.add_basket(s, day, [_wyld(rng, WYLD_GUMMY_PRICE) for _ in range(2)] + w.ordinary_lines(1))
            # -- spend $65, gummy for $10
            if inwin("wyld_spend65", day):
                for _ in range(int(rng.poisson(10 * scale))):
                    big = w.ordinary_lines(2)
                    while sum(u * p for *_, u, p in big) < 65:
                        big += w.ordinary_lines(1)
                    w.add_basket(s, day, big + [_wyld(rng, 10.0)], discount=WYLD_GUMMY_PRICE - 10.0)
                for _ in range(int(rng.poisson(3 * scale))):              # period-average priced add-on line
                    big = w.ordinary_lines(2)
                    while sum(u * p for *_, u, p in big) < 65:
                        big += w.ordinary_lines(1)
                    w.add_basket(s, day, big + [_wyld(rng, 21.0)], discount=WYLD_GUMMY_PRICE - 10.0)
            # -- buy 2 get 1: three gummies, one at $0
            if inwin("wyld_b2g1", day):
                for _ in range(int(rng.poisson(12 * scale))):
                    specs = [_wyld(rng, WYLD_GUMMY_PRICE), _wyld(rng, WYLD_GUMMY_PRICE), _wyld(rng, 0.0)]
                    if rng.random() < .4:
                        specs += w.ordinary_lines(1)
                    w.add_basket(s, day, specs, discount=WYLD_GUMMY_PRICE)
                for _ in range(int(rng.poisson(4 * scale))):              # bought two, no third
                    w.add_basket(s, day, [_wyld(rng, WYLD_GUMMY_PRICE) for _ in range(2)])
            # -- 4 eighths 25% off, Good Green or Rythm (one brand)
            if inwin("gg_rythm", day):
                for _ in range(int(rng.poisson(8 * scale))):
                    brand = "Good Green" if rng.random() < .55 else "Rythm"
                    specs = [_eighth(rng, brand, EIGHTH_PRICE * .75) for _ in range(4)]
                    w.add_basket(s, day, specs, discount=4 * EIGHTH_PRICE * .25)
                for _ in range(int(rng.poisson(2 * scale))):              # period-average priced, deal in discount_amt
                    brand = "Good Green" if rng.random() < .55 else "Rythm"
                    w.add_basket(s, day, [_eighth(rng, brand, EIGHTH_PRICE * .93) for _ in range(4)],
                                 discount=4 * EIGHTH_PRICE * .25)
                for _ in range(int(rng.poisson(3 * scale))):              # three eighths
                    brand = "Good Green" if rng.random() < .5 else "Rythm"
                    w.add_basket(s, day, [_eighth(rng, brand, EIGHTH_PRICE) for _ in range(3)])
            # -- Ruby takeover: two eighths plus the free doobie
            if inwin("ruby", day):
                for _ in range(int(rng.poisson(6 * scale))):
                    specs = [_eighth(rng, "Ruby", EIGHTH_PRICE), _eighth(rng, "Ruby", EIGHTH_PRICE),
                             ("Pre-Roll", "Ruby", "Ruby Doobies Pre-Roll 1g (GWP)", 1.0, 0.01)]
                    w.add_basket(s, day, specs)
            # -- Boarding Pass redemptions (ghost SKU at $0) and a few returns
            for _ in range(int(rng.poisson(1.5 * scale))):
                specs = w.ordinary_lines(1) + [("Pre-Roll", "The Travel Agency", "Boarding Pass - Free Pre-Roll", 1.0, 0.0)]
                w.add_basket(s, day, specs, known_p=.9)
            if rng.random() < .3:
                w.add_basket(s, day, w.ordinary_lines(1), is_return=True)
        day += dt.timedelta(days=1)
    fl, fb = w.frames()
    con = duckdb.connect(str(src_path))
    con.register("_fl", fl); con.register("_fb", fb)
    con.execute("CREATE TABLE fact_line AS SELECT * FROM _fl")
    con.execute("CREATE TABLE fact_basket AS SELECT * FROM _fb")
    # a tier table so the tier:<tier> segment rows get exercised
    con.execute("""CREATE TABLE dim_customer_tier AS
                   SELECT customer_key,
                          CASE WHEN hash(customer_key) % 10 < 2 THEN 'Frequent Flyer'
                               WHEN hash(customer_key) % 10 < 6 THEN 'Travel Club' ELSE 'Non-Loyalty' END AS tier
                   FROM (SELECT DISTINCT customer_key FROM fact_basket WHERE customer_key IS NOT NULL)""")
    con.close()
    return len(fl), len(fb)


def build_promo(dash_path: Path):
    import publish_promo as pp
    tmp = Path(tempfile.mkdtemp(prefix="cerebral_promo_"))
    src = tmp / "src.duckdb"
    nl, nb = build_promo_src(src)
    print(f"  [promo] fake src: {nl:,} lines, {nb:,} baskets, {P_FIRST} -> {P_LAST}")
    con = duckdb.connect(str(dash_path))
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        counts = pp.build_promo(con, src_attached=True)
    con.execute("DETACH src")
    con.close()
    for ln in buf.getvalue().splitlines():
        print("  [promo]", ln)
    for t, n in counts.items():
        print(f"  {t:<32}{n:>9,} rows")
    shutil.rmtree(tmp, ignore_errors=True)


# ====================================================================
# ---------------------------------------------------------------------------
# 9. model lab -- the real publish_model on the finished fixture (it reads only
#    dash_* tables, so nothing to fake). The fixture's numbers are random, so
#    expect low R-squared and a backtest that loses to "same as last week";
#    the point is that every table exists and the tab renders.
def build_model(dash_path: Path):
    import publish_model as pm
    con = duckdb.connect(str(dash_path))
    counts = pm.build_model(con)
    con.close()
    print("  [model] " + ", ".join(f"{k.replace('dash_', '')}: {v:,}" for k, v in counts.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.out.exists():
        a.out.unlink()
    print(f"Building fixture -> {a.out}")
    con = duckdb.connect(str(a.out))
    P = build_spine()
    core = build_core(con, P)
    build_misc(con, core)
    build_offers(con, core)
    build_discounting(con, core, P)
    build_gwp(con)
    build_channel(con, core)
    build_loyalty(con, core)
    build_newret(con, core)
    build_retention(con)
    ev = build_events(con, core)
    build_event_tables(con, ev)
    con.close()
    build_vm(a.out)
    build_promo(a.out)
    build_model(a.out)
    con = duckdb.connect(str(a.out))
    n = con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name LIKE 'dash_%'").fetchone()[0]
    con.close()
    print(f"Done: {n} dash_* tables, {a.out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
