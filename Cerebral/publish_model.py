"""
publish_model.py -- the Model Lab tables (dash_model_*) and the TL;DR feed
(dash_insights) in cerebral_dash.duckdb.

Runs from Cerebral/ AFTER publish.py, publish_vm.py and publish_promo.py.
It reads the dash file ONLY (no source DB needed), so it also runs on the
dev fixture and on any downloaded copy of the live file:

    python publish_model.py                          # cerebral_dash.duckdb next to this file
    python publish_model.py --dash path/to/file.duckdb
    python publish_model.py --check                  # row counts only
    python publish_model.py --explain Flower 4       # print a Stata-style table for one fit

What it does, in the order the Model Lab tab shows it
  1. dash_model_week      one row per store x category x ISO week: the units
                          sold plus everything we can use to explain them
                          (SKUs in stock, promo days, takeover days, holiday,
                          days open, season, trend). store_key 0 = all stores.
  2. dash_model_coef      the regression for each store x category:
                          units = a + b*skus_in_stock + c*promo + d*takeover + ...
                          one row per term with its coefficient and standard
                          error. "Holding everything else in the model fixed,
                          one more SKU on the shelf is worth b units a week."
  3. dash_model_fit       actual vs predicted per week, the residual (what the
                          model cannot explain), and how much of the gap to a
                          normal week is down to stock.
  4. dash_model_backtest  the honest test: fit on data up to 8 weeks ago, then
                          forecast those 8 weeks and compare with what
                          happened and with "same as last week".
  5. dash_model_forecast  the next 4 weeks, with a typical range.
  6. dash_model_brand     brand-level "demand after stock" for Brand Efficiency.
  7. dash_insights        plain-English statements, suggestions and questions
                          built from all of the above (and a couple of cheap
                          checks on other tables), for the TL;DR block.

Maths
  Ordinary least squares in numpy (no statsmodels): the coefficients are
  (X'X)^-1 X'y, the standard errors come from the residual variance, R-squared
  is 1 - SSE/SST. "Likely range" everywhere means coefficient +/- 2 standard
  errors (roughly 95%). A term whose likely range crosses zero is shown but
  flagged as "not clearly different from zero".

Honest limits (the tab repeats these)
  * A regression finds patterns, not causes. Stock and sales move together
    partly because buyers restock what sells; a takeover coefficient says
    takeover weeks sell more, not that the takeover made it happen.
  * With ~20-190 weeks per store the fit supports a handful of terms. Fine
    for "did the promo move the needle" and "what should next month look
    like", not for SKU-level questions.
  * The partial last week is left out of every fit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from promos import PROMOS, promo_stores, _d as _pdate  # noqa: E402

try:                                   # TAKEOVERS lives in the app file;
    from takeover_promos import TAKEOVERS as _TK  # noqa: E402
except Exception:                      # fall back to parsing cerebral_public.py
    _TK = None

STORES = {1: "DTBK", 2: "5th Avenue", 3: "Soho", 4: "Union Square"}
STORE_LABEL = {0: "All stores", **STORES}

MIN_WEEKS = 20          # fewest complete weeks a fit will run on
GOOD_WEEKS = 40         # below this the tab says "thin history"
BACKTEST_WEEKS = 8      # held-out weeks for the honest test
FORECAST_WEEKS = 4
MIN_SHARE = 0.03        # categories under 3% of a store's units get no insights
LONG_PROMO_DAYS = 120   # an always-on "promo" (Boarding Pass) is not a promo week
Z_FLAG = 1.5            # residual z-score that earns an insight
Z_RUN = 1.0             # z for the "3 weeks in a row" rule
BRAND_MIN_WEEKS = 16
BRAND_TOP_N = 12        # brands per store x category with insights potential


# --------------------------------------------------------------- takeovers
def _takeovers() -> list[dict]:
    """The TAKEOVERS calendar from cerebral_public.py, without importing the
    app (which would start Streamlit). Parsed with ast like checks.py does."""
    if _TK:
        return _TK
    import ast
    src = (HERE / "cerebral_public.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "TAKEOVERS":
                    return ast.literal_eval(node.value)
    return []


# ---------------------------------------------------------------- holidays
def _holiday_dates(years) -> dict[dt.date, str]:
    """Retail-relevant days: 4/20, Green Wednesday + Thanksgiving, Christmas
    week, New Year, July 4, Memorial Day, Labor Day."""
    out = {}
    for y in years:
        out[dt.date(y, 4, 20)] = "4/20"
        out[dt.date(y, 7, 4)] = "July 4"
        out[dt.date(y, 12, 24)] = "Christmas"
        out[dt.date(y, 12, 25)] = "Christmas"
        out[dt.date(y, 12, 31)] = "New Year's Eve"
        out[dt.date(y, 1, 1)] = "New Year"
        # Thanksgiving: 4th Thursday of November
        nov1 = dt.date(y, 11, 1)
        first_thu = nov1 + dt.timedelta(days=(3 - nov1.weekday()) % 7)
        tg = first_thu + dt.timedelta(days=21)
        out[tg] = "Thanksgiving"
        out[tg - dt.timedelta(days=1)] = "Green Wednesday"
        # Memorial Day: last Monday of May; Labor Day: first Monday of Sept
        may31 = dt.date(y, 5, 31)
        out[may31 - dt.timedelta(days=may31.weekday())] = "Memorial Day"
        sep1 = dt.date(y, 9, 1)
        out[sep1 + dt.timedelta(days=(0 - sep1.weekday()) % 7)] = "Labor Day"
    return out


def _week_start(y: int, w: int) -> dt.date:
    return dt.date.fromisocalendar(int(y), int(w), 1)


def _days_in_week(ws: dt.date, start: dt.date, end: dt.date) -> int:
    """How many of the 7 days from ws fall inside [start, end]."""
    we = ws + dt.timedelta(days=6)
    lo, hi = max(ws, start), min(we, end)
    return max(0, (hi - lo).days + 1)


# ------------------------------------------------------------------- OLS
def ols(X: np.ndarray, y: np.ndarray):
    """Plain least squares. Returns coef, se, r2, sigma, resid, n."""
    n, k = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)
    coef = XtX_inv @ X.T @ y
    resid = y - X @ coef
    dof = max(1, n - k)
    sigma2 = float(resid @ resid) / dof
    se = np.sqrt(np.maximum(np.diag(XtX_inv) * sigma2, 0))
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / sst if sst > 0 else 0.0
    return coef, se, r2, math.sqrt(sigma2), resid, n


TERM_LABEL = {
    "const":    "Baseline",
    "stock":    "SKUs in stock (each)",
    "lag1":     "Last week's units (momentum, per unit)",
    "promo":    "Promo week (full week)",
    "takeover": "Takeover week (full week)",
    "holiday":  "Holiday week",
    "days_open": "Days open (each)",
    "sin":      "Season (sine)",
    "cos":      "Season (cosine)",
    "trend":    "Trend (per week)",
}


def _design(d: pd.DataFrame, use_stock: bool) -> tuple[np.ndarray, list[str]]:
    cols = ["const"]
    mats = [np.ones(len(d))]
    if use_stock:
        cols.append("stock"); mats.append(d.skus_in_stock.to_numpy(float))
    # a yearly cycle cannot be estimated from less than a year of weeks, and
    # extrapolating half a sine wave is how a short history produces a wild
    # forecast - the season terms only enter once the history spans 52 weeks
    span = (d.week_start.max() - d.week_start.min()).days / 7.0 + 1
    terms = ["lag1", "promo", "takeover", "holiday", "days_open", "trend"]
    if span >= 52:
        terms += ["sin", "cos"]
    for c in terms:
        v = d["units_lag1" if c == "lag1" else c].to_numpy(float)
        if np.nanstd(v) > 1e-9:            # a constant column adds nothing
            cols.append(c); mats.append(v)
    return np.column_stack(mats), cols


# ------------------------------------------------------ 1. the model table
def build_model_week(con) -> pd.DataFrame:
    meta = con.execute("SELECT last_txn FROM dash_meta").fetchone()
    last_txn = pd.Timestamp(meta[0]).date()
    cw = con.execute("""
        SELECT store_key, iso_year, iso_week, category,
               SUM(units) AS units, SUM(net) AS net, SUM(gm) AS gm,
               SUM(baskets_with) AS baskets_with,
               MAX(days_open) AS days_open,
               MAX(skus_in_stock) AS skus_in_stock,
               MAX(skus_out) AS skus_out,
               MAX(floor_units_end) AS floor_units_end
        FROM dash_category_week
        WHERE category IS NOT NULL
        GROUP BY 1,2,3,4
    """).df()
    if cw.empty:
        raise SystemExit("dash_category_week is empty")

    # chain rows (store 0): sum units/net, sum SKUs in stock across stores
    chain = (cw.groupby(["iso_year", "iso_week", "category"], as_index=False)
               .agg(units=("units", "sum"), net=("net", "sum"), gm=("gm", "sum"),
                    baskets_with=("baskets_with", "sum"),
                    days_open=("days_open", "max"),
                    skus_in_stock=("skus_in_stock", lambda s: s.sum(min_count=1)),
                    skus_out=("skus_out", lambda s: s.sum(min_count=1)),
                    floor_units_end=("floor_units_end", lambda s: s.sum(min_count=1))))
    chain["store_key"] = 0
    mw = pd.concat([cw, chain], ignore_index=True)

    mw["week_start"] = [_week_start(y, w) for y, w in zip(mw.iso_year, mw.iso_week)]
    mw["complete"] = [ws + dt.timedelta(days=6) <= last_txn for ws in mw.week_start]

    # promo days: any promo in the calendar that covers this store and this
    # category (or every category), excluding always-on mechanics
    tk = _takeovers()
    years = sorted({int(y) for y in mw.iso_year} | {last_txn.year, last_txn.year + 1})
    hol = _holiday_dates(years)
    promo_days, tko_days, hol_name = [], [], []
    for r in mw.itertuples(index=False):
        ws = r.week_start
        pdays = 0
        for p in PROMOS:
            s, e = _pdate(p["start"]), _pdate(p["end"])
            if (e - s).days > LONG_PROMO_DAYS:
                continue
            if p.get("category") not in (None, r.category):
                continue
            if r.store_key != 0 and r.store_key not in promo_stores(p):
                continue
            pdays = max(pdays, _days_in_week(ws, s, e))
        promo_days.append(pdays)
        tdays = 0
        for t in tk:
            tdays = max(tdays, _days_in_week(ws, _pdate(t["start"]), _pdate(t["end"])))
        tko_days.append(tdays)
        names = [hol[ws + dt.timedelta(days=i)] for i in range(7)
                 if ws + dt.timedelta(days=i) in hol]
        hol_name.append(names[0] if names else None)
    mw["promo_days"] = promo_days
    mw["takeover_days"] = tko_days
    mw["holiday_name"] = hol_name
    mw["promo"] = mw.promo_days / 7.0
    mw["takeover"] = mw.takeover_days / 7.0
    mw["holiday"] = mw.holiday_name.notna().astype(float)
    woy = mw.iso_week.astype(float)
    mw["sin"] = np.sin(2 * np.pi * woy / 52.18)
    mw["cos"] = np.cos(2 * np.pi * woy / 52.18)
    # trend counts weeks from the first week in the file, per store x category
    first = mw.week_start.min()
    mw["trend"] = [(ws - first).days / 7.0 for ws in mw.week_start]
    mw["days_open"] = pd.to_numeric(mw.days_open, errors="coerce").fillna(7).clip(1, 7)
    mw = mw.sort_values(["store_key", "category", "week_start"]).reset_index(drop=True)
    # last week's units: the momentum term. Weekly sales carry over - a store
    # that sold 2,000 last week will sell near 2,000 this week whatever else is
    # going on - and a model without this loses to "same as last week" every
    # time. With it in, every other coefficient reads "beyond what last week
    # already told us".
    mw["units_lag1"] = mw.groupby(["store_key", "category"]).units.shift(1)
    _ws = pd.to_datetime(mw.week_start)
    gap = (_ws - _ws.groupby([mw.store_key, mw.category]).shift(1)).dt.days
    mw.loc[gap != 7, "units_lag1"] = np.nan          # a missing week breaks the chain
    return mw, last_txn, hol


# ------------------------------------------------------- 2-5. the fits
def _predict(bmap: dict, row) -> float:
    """One week's expected units from a coefficient map and a row that carries
    the driver columns (skus_in_stock for 'stock', units_lag1 for 'lag1')."""
    v = bmap.get("const", 0.0)
    for c, b in bmap.items():
        if c == "const":
            continue
        x = row["skus_in_stock"] if c == "stock" else row["units_lag1"] if c == "lag1" else row[c]
        v += b * float(x)
    return float(v)


def _walk(bmap: dict, frame: pd.DataFrame, lag_seed: float) -> np.ndarray:
    """Forecast a run of weeks in order, feeding each forecast in as the next
    week's 'last week's units'. That is the honest multi-week forecast: at the
    cutoff you do not know the actuals of the weeks you are forecasting."""
    out, lag = [], lag_seed
    for _, r in frame.iterrows():
        r = r.copy(); r["units_lag1"] = lag
        v = _predict(bmap, r)
        out.append(v); lag = v
    return np.array(out)


def _fit_unit(d: pd.DataFrame, forecast_x: pd.DataFrame | None):
    """Fit one store x category. Returns dicts/frames for coef, fit, backtest,
    forecast, plus a summary dict. d is complete weeks only, sorted."""
    n_all = len(d)
    d = d[d.units_lag1.notna()].reset_index(drop=True)      # first week has no "last week"
    for c in ("skus_in_stock", "skus_out", "floor_units_end", "units", "net"):
        d[c] = pd.to_numeric(d[c], errors="coerce").astype(float)   # nullable ints -> float
    have_stock = d.skus_in_stock.notna()
    stock_ok = have_stock.sum() >= MIN_WEEKS and d.skus_in_stock.dropna().std() > 1e-9
    stock_imputed = None
    if stock_ok:
        # the inventory ledger can lag sales by a few weeks (it is exported
        # monthly). Weeks AFTER the last stock week are kept in the fit with the
        # shelf assumed at its last known 4-week mean, and flagged, so the fit
        # still reaches the last complete week; weeks BEFORE the first stock
        # week are dropped.
        last_known = d.index[have_stock].max()
        first_known = d.index[have_stock].min()
        fill = float(d.loc[have_stock, "skus_in_stock"].tail(4).mean())
        d = d.loc[first_known:].copy()
        stock_imputed = d.skus_in_stock.isna()
        d.loc[stock_imputed, "skus_in_stock"] = fill
        fit_d = d
    else:
        fit_d = d
    if len(fit_d) < MIN_WEEKS:
        return None
    y = fit_d.units.to_numpy(float)
    X, cols = _design(fit_d, stock_ok)
    coef, se, r2, sigma, resid, n = ols(X, y)
    kind = "stock" if stock_ok else "no_stock"
    bmap = dict(zip(cols, coef))

    coef_rows = [dict(term=c, label=TERM_LABEL.get(c, c), coef=float(b), se=float(s),
                      lo=float(b - 2 * s), hi=float(b + 2 * s),
                      clear=bool(abs(b) > 2 * s))
                 for c, b, s in zip(cols, coef, se)]

    # fitted values: what a week "should" have sold given its stock / season...
    pred = X @ coef
    fit = fit_d[["store_key", "iso_year", "iso_week", "category", "week_start",
                 "units", "net", "skus_in_stock", "skus_out", "promo_days",
                 "takeover_days", "holiday_name"]].copy()
    fit["predicted"] = pred
    fit["residual"] = y - pred
    fit["resid_z"] = fit.residual / sigma if sigma > 0 else 0.0
    fit["stock_imputed"] = stock_imputed.to_numpy() if stock_imputed is not None else False
    if stock_ok:
        b_stock = coef[cols.index("stock")]
        # the part of this week's gap to a normal shelf that stock explains
        norm = fit_d.skus_in_stock.rolling(8, min_periods=3).median().shift(1)
        fit["stock_vs_norm"] = fit_d.skus_in_stock - norm
        fit["explained_by_stock"] = b_stock * fit.stock_vs_norm
        # an assumed shelf explains nothing: blank those weeks
        fit.loc[fit.stock_imputed, ["stock_vs_norm", "explained_by_stock"]] = np.nan
        fit.loc[fit.stock_imputed, "skus_in_stock"] = np.nan
    else:
        fit["stock_vs_norm"] = np.nan
        fit["explained_by_stock"] = np.nan

    # ---- backtest: fit through cutoff, walk forward over the held-out weeks
    bt_rows, bt_sum = [], None
    if len(fit_d) >= MIN_WEEKS + BACKTEST_WEEKS:
        tr, ho = fit_d.iloc[:-BACKTEST_WEEKS], fit_d.iloc[-BACKTEST_WEEKS:]
        Xtr, ctr = _design(tr, stock_ok)
        ctr_coef, *_ = ols(Xtr, tr.units.to_numpy(float))
        seed = float(tr.units.iloc[-1])
        f_model = _walk(dict(zip(ctr, ctr_coef)), ho, seed)
        # "no model" rivals: same as the last training week; mean of last 4
        naive_last = seed
        naive_4 = float(tr.units.tail(4).mean())
        # no-stock rival: same terms minus stock (only differs when stock is in)
        if stock_ok:
            Xtr2, ctr2 = _design(tr, False)
            c2, *_ = ols(Xtr2, tr.units.to_numpy(float))
            f_nostock = _walk(dict(zip(ctr2, c2)), ho, seed)
        else:
            f_nostock = f_model
        act = ho.units.to_numpy(float)
        for i, r in enumerate(ho.itertuples(index=False)):
            bt_rows.append(dict(store_key=r.store_key, category=r.category,
                                iso_year=r.iso_year, iso_week=r.iso_week,
                                week_start=r.week_start, actual=float(act[i]),
                                fcast_model=float(f_model[i]), fcast_nostock=float(f_nostock[i]),
                                fcast_last=naive_last, fcast_last4=naive_4))
        mae = lambda f: float(np.mean(np.abs(act - f)))
        m_model, m_ns, m_last, m_4 = mae(f_model), mae(f_nostock), mae(naive_last), mae(naive_4)
        best_naive = min(m_last, m_4)
        gain = (1 - m_model / best_naive) if best_naive > 0 else 0.0
        verdict = ("beats" if gain > 0.10 else "ties" if gain > -0.10 else "loses")
        bt_sum = dict(mae_model=m_model, mae_nostock=m_ns, mae_last=m_last,
                      mae_last4=m_4, gain_vs_naive=gain, verdict=verdict,
                      cutoff_week=f"{int(tr.iloc[-1].iso_year)}-W{int(tr.iloc[-1].iso_week):02d}")

    # ---- forecast the next weeks from the full fit, walking forward
    fc_rows = []
    if forecast_x is not None and not forecast_x.empty:
        fx = forecast_x.copy()
        if stock_ok:
            fx["skus_in_stock"] = float(fit_d.skus_in_stock.tail(4).mean())
        f = _walk(bmap, fx, float(fit_d.units.iloc[-1]))
        for i, r in enumerate(fx.itertuples(index=False)):
            v = f[i]
            # the band widens with the horizon: errors compound week on week
            band = 1.645 * sigma * math.sqrt(i + 1) if "lag1" in bmap else 1.645 * sigma
            fc_rows.append(dict(store_key=r.store_key, category=r.category,
                                iso_year=r.iso_year, iso_week=r.iso_week,
                                week_start=r.week_start, forecast=float(v),
                                lo=float(v - band), hi=float(v + band),
                                promo_days=int(r.promo_days), takeover_days=int(r.takeover_days),
                                holiday_name=r.holiday_name,
                                stock_assumed=(float(fx.skus_in_stock.iloc[0]) if stock_ok else None)))

    summary = dict(model_kind=kind, n_weeks=int(n), n_weeks_all=int(n_all), r2=float(r2),
                   sigma=float(sigma), thin=bool(n < GOOD_WEEKS),
                   stock_imputed_weeks=int(stock_imputed.sum()) if stock_imputed is not None else 0,
                   mean_units=float(y.mean()),
                   unit_price=float(fit_d.net.sum() / max(1.0, fit_d.units.sum())))
    return dict(coef=coef_rows, fit=fit, backtest=bt_rows, backtest_summary=bt_sum,
                forecast=fc_rows, summary=summary)


def _future_weeks(mw: pd.DataFrame, last_txn: dt.date, hol: dict) -> pd.DataFrame:
    """Design rows for the FORECAST_WEEKS weeks after the last complete week,
    for every store x category: promo/takeover days from the calendar (future
    promos are known), holidays, season, trend; days_open = 7."""
    comp = mw[mw.complete]
    if comp.empty:
        return pd.DataFrame()
    last_ws = comp.week_start.max()
    tk = _takeovers()
    first = mw.week_start.min()
    rows = []
    for (sk, cat), g in comp.groupby(["store_key", "category"]):
        for i in range(1, FORECAST_WEEKS + 1):
            ws = last_ws + dt.timedelta(days=7 * i)
            y, w, _ = ws.isocalendar()
            pdays = 0
            for p in PROMOS:
                s, e = _pdate(p["start"]), _pdate(p["end"])
                if (e - s).days > LONG_PROMO_DAYS or p.get("category") not in (None, cat):
                    continue
                if sk != 0 and sk not in promo_stores(p):
                    continue
                pdays = max(pdays, _days_in_week(ws, s, e))
            tdays = max([_days_in_week(ws, _pdate(t["start"]), _pdate(t["end"])) for t in tk] + [0])
            names = [hol[ws + dt.timedelta(days=j)] for j in range(7) if ws + dt.timedelta(days=j) in hol]
            rows.append(dict(store_key=sk, category=cat, iso_year=y, iso_week=w, week_start=ws,
                             promo_days=pdays, takeover_days=tdays, holiday_name=names[0] if names else None,
                             promo=pdays / 7.0, takeover=tdays / 7.0, holiday=float(bool(names)),
                             days_open=7.0, sin=math.sin(2 * math.pi * w / 52.18),
                             cos=math.cos(2 * math.pi * w / 52.18),
                             trend=(ws - first).days / 7.0, skus_in_stock=np.nan,
                             units_lag1=np.nan))
    return pd.DataFrame(rows)


# ---------------------------------------------------------- 6. brands
def _canon_map(con) -> dict:
    try:
        a = con.execute("SELECT alias, canonical FROM dash_brand_alias").df()
        return dict(zip(a.alias, a.canonical))
    except Exception:
        return {}


def build_brand(con, mw: pd.DataFrame, last_txn: dt.date, cat_coef: dict) -> pd.DataFrame:
    """Per store x brand: units ~ stock (products on the floor at week end,
    from dash_vm_stock_week when present) + season + own-takeover + trend.
    Brands without stock history get the no-stock fit and say so."""
    bw = con.execute("""
        SELECT store_key, iso_year, iso_week, brand, category,
               SUM(units) AS units, SUM(net) AS net
        FROM dash_brand_week WHERE brand IS NOT NULL
        GROUP BY 1,2,3,4,5
    """).df()
    if bw.empty:
        return pd.DataFrame()
    cm = _canon_map(con)
    bw["brand"] = bw.brand.map(lambda b: cm.get(b, b))
    bw = bw.groupby(["store_key", "iso_year", "iso_week", "brand", "category"], as_index=False).sum()
    # a brand's home category = where most of its units go
    home = (bw.groupby(["brand", "category"]).units.sum().reset_index()
              .sort_values("units", ascending=False).drop_duplicates("brand")
              .set_index("brand").category)
    bw = bw[bw.category == bw.brand.map(home)]
    bw = bw.groupby(["store_key", "iso_year", "iso_week", "brand"], as_index=False) \
           .agg(units=("units", "sum"), net=("net", "sum"), category=("category", "first"))
    chain = bw.groupby(["iso_year", "iso_week", "brand"], as_index=False) \
              .agg(units=("units", "sum"), net=("net", "sum"), category=("category", "first"))
    chain["store_key"] = 0
    bw = pd.concat([bw, chain], ignore_index=True)

    stock = pd.DataFrame()
    try:
        stock = con.execute("""
            SELECT store_key, iso_year, iso_week, brand,
                   products_on_floor_end AS on_floor, products_stocked_out AS stocked_out
            FROM dash_vm_stock_week WHERE brand IS NOT NULL
        """).df()
    except Exception:
        pass
    if not stock.empty:
        stock["brand"] = stock.brand.map(lambda b: cm.get(b, b))
        schain = stock.groupby(["iso_year", "iso_week", "brand"], as_index=False)[["on_floor", "stocked_out"]].sum()
        schain["store_key"] = 0
        stock = pd.concat([stock, schain], ignore_index=True)
        bw = bw.merge(stock, on=["store_key", "iso_year", "iso_week", "brand"], how="left")
    else:
        bw["on_floor"] = np.nan; bw["stocked_out"] = np.nan

    # the same calendar columns the category model uses (takeover = own brand only)
    tk = _takeovers()
    first = mw.week_start.min()
    bw["week_start"] = [_week_start(y, w) for y, w in zip(bw.iso_year, bw.iso_week)]
    bw = bw[[ws + dt.timedelta(days=6) <= last_txn for ws in bw.week_start]]
    bw["sin"] = np.sin(2 * np.pi * bw.iso_week / 52.18)
    bw["cos"] = np.cos(2 * np.pi * bw.iso_week / 52.18)
    bw["trend"] = [(ws - first).days / 7.0 for ws in bw.week_start]
    own = []
    for r in bw.itertuples(index=False):
        b = str(r.brand).lower()
        d = 0
        for t in tk:
            if any(p in b for p in t["patterns"]):
                d = max(d, _days_in_week(r.week_start, _pdate(t["start"]), _pdate(t["end"])))
        own.append(d / 7.0)
    bw["takeover"] = own
    bw["promo"] = 0.0; bw["holiday"] = 0.0; bw["days_open"] = 7.0
    bw = bw.sort_values(["store_key", "brand", "week_start"])
    bw["units_lag1"] = bw.groupby(["store_key", "brand"]).units.shift(1)
    bw = bw[bw.units_lag1.notna()]

    out = []
    for (sk, brand), g in bw.groupby(["store_key", "brand"]):
        if len(g) < BRAND_MIN_WEEKS:
            continue
        stock_ok = g.on_floor.notna().sum() >= BRAND_MIN_WEEKS and g.on_floor.dropna().std() > 1e-9
        d = g[g.on_floor.notna()] if stock_ok else g
        if len(d) < BRAND_MIN_WEEKS:
            continue
        d = d.assign(skus_in_stock=d.on_floor)
        X, cols = _design(d, stock_ok)
        y = d.units.to_numpy(float)
        coef, se, r2, sigma, resid, n = ols(X, y)
        last4 = d.tail(4)
        pred4 = (X[-4:] @ coef)
        act4 = y[-4:]
        gap = float(act4.sum() - pred4.sum())
        gap_pct = gap / pred4.sum() * 100 if pred4.sum() > 0 else np.nan
        z = gap / (sigma * 2) if sigma > 0 else 0.0   # 4-week sum: sigma*sqrt(4)
        b_stock = s_stock = np.nan
        if stock_ok:
            i = cols.index("stock"); b_stock, s_stock = float(coef[i]), float(se[i])
        price = float(d.net.sum() / max(1.0, d.units.sum()))
        avg_out = float(d.stocked_out.tail(8).mean()) if stock_ok else np.nan
        cost = (b_stock * avg_out * price) if stock_ok and np.isfinite(avg_out) else np.nan
        out.append(dict(store_key=int(sk), brand=brand, category=g.category.iloc[0],
                        n_weeks=int(n), r2=float(r2), model_kind="stock" if stock_ok else "no_stock",
                        units_last4=float(act4.sum()), predicted_last4=float(pred4.sum()),
                        gap_units=gap, gap_pct=float(gap_pct) if np.isfinite(gap_pct) else None,
                        gap_z=float(z), stock_coef=b_stock, stock_se=s_stock,
                        stock_clear=bool(stock_ok and abs(b_stock) > 2 * s_stock),
                        avg_products_out=avg_out, unit_price=price,
                        stockout_cost_week=float(cost) if np.isfinite(cost) else None,
                        thin=bool(n < GOOD_WEEKS),
                        last_week=f"{int(last4.iloc[-1].iso_year)}-W{int(last4.iloc[-1].iso_week):02d}"))
    return pd.DataFrame(out)


# -------------------------------------------------------- 7. insights
def _fmt_units(x: float) -> str:
    return f"{int(round(x)):,}"


def _money(x: float) -> str:
    return f"${int(round(x)):,}"


def build_insights(con, mw, results: dict, brand: pd.DataFrame, last_txn: dt.date) -> pd.DataFrame:
    """Plain-English statements for the TL;DR. Each row: store_key, kind
    (finding / suggestion / question), severity (bad / ok / info), section
    (slug of the tab to open), headline, detail, magnitude (for ordering)."""
    rows = []
    comp = mw[mw.complete]
    last_ws = comp.week_start.max()
    y, w, _ = last_ws.isocalendar()
    week_lbl = f"{y}-W{w:02d}"

    def add(sk, kind, sev, section, headline, detail="", mag=0.0, category=None, brand_=None):
        rows.append(dict(store_key=int(sk), store=STORE_LABEL[int(sk)], kind=kind,
                         severity=sev, section=section, category=category, brand=brand_,
                         headline=headline, detail=detail, magnitude=float(mag),
                         week=week_lbl))

    # unit share, so tiny categories stay quiet
    share = (comp[comp.week_start == last_ws].groupby(["store_key", "category"]).units.sum()
             / comp[comp.week_start == last_ws].groupby("store_key").units.sum())

    verdicts: dict[str, list] = {}
    for (sk, cat), res in results.items():
        if res is None or share.get((sk, cat), 0) < MIN_SHARE:
            continue
        fit = res["fit"]; sm = res["summary"]; store = STORE_LABEL[sk]
        price = sm["unit_price"]
        last = fit[fit.week_start == last_ws]
        coefs = {c["term"]: c for c in res["coef"]}

        # ---- last week's gap
        if not last.empty:
            r = last.iloc[0]
            if abs(r.resid_z) >= Z_FLAG:
                gap = r.residual
                stock_part = r.explained_by_stock if pd.notna(r.explained_by_stock) else 0.0
                direction = "fewer" if gap < 0 else "more"
                base = (f"{cat} at {store} sold about {_fmt_units(abs(gap))} {direction} units "
                        f"than expected in {week_lbl} ({_money(abs(gap) * price)} of sales)")
                if gap < 0 and stock_part < 0 and abs(stock_part) >= 0.5 * abs(gap):
                    add(sk, "finding", "warn", "model-lab",
                        base + " — mostly a thin shelf.",
                        f"The shelf held {abs(int(r.stock_vs_norm))} fewer SKUs than its recent norm, "
                        f"which accounts for roughly {_fmt_units(abs(stock_part))} of the missing units. "
                        f"Supply, not demand: check receiving and the floor set before changing the pitch.",
                        mag=abs(r.resid_z), category=cat)
                elif gap < 0 and pd.isna(r.stock_vs_norm):
                    add(sk, "finding", "warn", "model-lab",
                        base + " — shelf unknown for this week.",
                        "The inventory ledger has not been loaded this far, so this cannot yet be "
                        "split into supply and demand. Load the latest monthly ledger to find out.",
                        mag=abs(r.resid_z) * 0.9, category=cat)
                elif gap < 0:
                    add(sk, "finding", "bad", "model-lab",
                        base + " with a normal shelf.",
                        "Stock, season, promos and days open are all accounted for, so this reads as "
                        "a demand dip. Worth asking the floor what changed.",
                        mag=abs(r.resid_z), category=cat)
                else:
                    add(sk, "finding", "ok", "model-lab",
                        base + ".",
                        "More than stock, season or the promo calendar explain — something worked. "
                        "Worth finding out what so it can be repeated.",
                        mag=abs(r.resid_z) * 0.8, category=cat)

        # ---- a run of weeks below expectation
        tail = fit.tail(3)
        if len(tail) == 3 and (tail.resid_z <= -Z_RUN).all():
            add(sk, "finding", "bad", "model-lab",
                f"{cat} at {store} has run below expectation for three weeks in a row.",
                f"Short of about {_fmt_units(-tail.residual.sum())} units over the three weeks after "
                f"allowing for stock and season. One soft week is noise; three is a trend.",
                mag=float(-tail.resid_z.mean()) + 0.5, category=cat)
        elif len(tail) == 3 and (tail.resid_z >= Z_RUN).all():
            add(sk, "finding", "ok", "model-lab",
                f"{cat} at {store} has beaten expectation three weeks running.",
                f"About {_fmt_units(tail.residual.sum())} units above the model over three weeks.",
                mag=float(tail.resid_z.mean()), category=cat)

        # ---- what a stockout costs (store-level only; chain repeats it)
        st_ = coefs.get("stock")
        if st_ and st_["clear"] and st_["coef"] > 0 and sk != 0:
            add(sk, "finding", "info", "brand-efficiency",
                f"Each {cat} SKU missing from the shelf for a week costs {store} about "
                f"{_money(st_['coef'] * price)} in sales.",
                f"Likely range {_money(max(0, st_['lo']) * price)}–{_money(st_['hi'] * price)}, from "
                f"{sm['n_weeks']} weeks of history. Use it to price a reorder against a markdown.",
                mag=0.6, category=cat)

        # ---- promo effect
        pr = coefs.get("promo")
        if pr and pr["clear"]:
            if pr["coef"] > 0:
                add(sk, "finding", "ok", "promo-lab",
                    f"A full promo week lifts {cat} at {store} by about {_fmt_units(pr['coef'])} units "
                    f"({_money(pr['coef'] * price)}), stock and season held fixed.",
                    f"Likely range {_fmt_units(pr['lo'])}–{_fmt_units(pr['hi'])} units. "
                    "That is the promo's own effect, not the week it happened to land in.",
                    mag=0.7, category=cat)
            else:
                add(sk, "question", "warn", "promo-lab",
                    f"Promo weeks have not lifted {cat} at {store} — the measured effect is "
                    f"{_fmt_units(pr['coef'])} units.",
                    "Either the offers are moving units the model already expected, or discounting "
                    "is trading margin for nothing. Worth checking the applied share in Promo Lab.",
                    mag=0.8, category=cat)

        # ---- backtest verdict: collected per chain category, written once below
        bt = res["backtest_summary"]
        if bt and sk == 0:
            verdicts.setdefault(bt["verdict"], []).append(cat)

        # ---- next-week forecast (chain only)
        fc = res["forecast"]
        if fc and sk == 0 and not last.empty:
            f0 = fc[0]
            lastu = float(last.iloc[0].units)
            chg = (f0["forecast"] / lastu - 1) * 100 if lastu > 0 else 0
            if abs(chg) >= 8:
                flag = " — a " + ("promo" if f0["promo_days"] else "takeover" if f0["takeover_days"]
                                 else (f0["holiday_name"] or "seasonal")) + " week" \
                    if (f0["promo_days"] or f0["takeover_days"] or f0["holiday_name"]) else ""
                add(sk, "suggestion", "info", "model-lab",
                    f"Expect {cat} around {_fmt_units(f0['forecast'])} units next week, "
                    f"{'up' if chg > 0 else 'down'} {abs(chg):.0f}% on last week{flag}.",
                    f"Typical range {_fmt_units(f0['lo'])}–{_fmt_units(f0['hi'])}. Plan floor stock to the top of it.",
                    mag=min(1.0, abs(chg) / 20), category=cat)

        # ---- stock thinning while demand holds
        if sm["model_kind"] == "stock" and len(fit) >= 6 and sk != 0:
            s = fit.skus_in_stock.tail(5)
            if s.is_monotonic_decreasing and s.iloc[0] > 0 and s.iloc[-1] / s.iloc[0] < 0.85 \
                    and fit.resid_z.tail(3).mean() > -0.5:
                add(sk, "question", "warn", "brand-efficiency",
                    f"{cat} stock at {store} has thinned {100 - s.iloc[-1] / s.iloc[0] * 100:.0f}% "
                    f"over five weeks while sales held up — is a reorder overdue?",
                    "Demand after stock is fine, so the next step down in SKUs will start to cost sales.",
                    mag=0.7, category=cat)

    # ---- one line on how trustworthy the forecasts are right now
    if verdicts.get("beats"):
        cats = ", ".join(sorted(verdicts["beats"]))
        add(0, "finding", "info", "model-lab",
            f"Forecasts beat 'same as last week' over the last {BACKTEST_WEEKS} weeks for: {cats}.",
            "Those are worth planning against. The Model Lab tab shows the held-out weeks.", mag=0.4)
    if verdicts.get("loses"):
        cats = ", ".join(sorted(verdicts["loses"]))
        add(0, "question", "warn", "model-lab",
            f"Forecasts did worse than 'same as last week' recently for: {cats}.",
            "Treat those as rough guides. Usually a new promo, a menu change, a short history or a "
            "data problem explains it; the backtest chart in Model Lab shows which weeks missed.", mag=0.5)

    # ---- brands: largest demand gaps with a trustworthy fit
    if not brand.empty:
        for sk, g in brand.groupby("store_key"):
            g = g[(~g.thin) & g.gap_z.abs().ge(1.5)].copy()
            g["abs"] = g.gap_z.abs()
            for r in g.sort_values("abs", ascending=False).head(3).itertuples(index=False):
                stock_txt = ("with the shelf accounted for" if r.model_kind == "stock"
                             else "(no stock history for this brand yet)")
                if r.gap_units < 0:
                    add(sk, "finding", "bad", "brand-efficiency",
                        f"{r.brand} ({r.category}) at {STORE_LABEL[sk]} is {abs(r.gap_pct):.0f}% below "
                        f"expectation over four weeks {stock_txt}.",
                        f"About {_fmt_units(abs(r.gap_units))} units short of the model. "
                        "Demand, not availability — check pricing, placement and the budtender pitch.",
                        mag=abs(r.gap_z) * 0.7, category=r.category, brand_=r.brand)
                else:
                    add(sk, "finding", "ok", "brand-efficiency",
                        f"{r.brand} ({r.category}) at {STORE_LABEL[sk]} is running {r.gap_pct:.0f}% above "
                        f"expectation over four weeks {stock_txt}.",
                        f"About {_fmt_units(r.gap_units)} units above the model.",
                        mag=abs(r.gap_z) * 0.5, category=r.category, brand_=r.brand)
            # brands where empty shelf is clearly costing money
            cost = brand[(brand.store_key == sk) & brand.stock_clear & (brand.stockout_cost_week > 0)]
            for r in cost.sort_values("stockout_cost_week", ascending=False).head(2).itertuples(index=False):
                if r.avg_products_out >= 1:
                    add(sk, "suggestion", "warn", "brand-efficiency",
                        f"{r.brand} has averaged {r.avg_products_out:.1f} products out of stock a week at "
                        f"{STORE_LABEL[sk]}, costing about {_money(r.stockout_cost_week)} a week.",
                        f"Each {r.brand} product on the floor is worth about {r.stock_coef:.1f} units a week "
                        f"here. Deepening the reorder looks cheaper than the lost sales.",
                        mag=min(1.5, r.stockout_cost_week / 1000), category=r.category, brand_=r.brand)

    # ---- two cheap non-model checks so the TL;DR is not only about the model
    try:
        bwk = con.execute("""
            SELECT store_key, iso_year, iso_week, SUM(redeem_value) AS rv, SUM(net) AS net
            FROM dash_basket_week GROUP BY 1,2,3
        """).df()
        bwk["week_start"] = [_week_start(a, b) for a, b in zip(bwk.iso_year, bwk.iso_week)]
        bwk = bwk[[ws + dt.timedelta(days=6) <= last_txn for ws in bwk.week_start]]
        for sk, g in bwk.groupby("store_key"):
            g = g.sort_values("week_start")
            if len(g) < 6:
                continue
            cur, base = g.rv.iloc[-1], g.rv.iloc[-5:-1].mean()
            if base > 0 and cur / base < 0.6:
                add(sk, "question", "warn", "redemptions",
                    f"Loyalty redemptions at {STORE_LABEL[sk]} were {100 - cur / base * 100:.0f}% below "
                    f"their four-week average in {week_lbl}.",
                    "Either points are being saved up, a competing promo replaced them, or the "
                    "Alpine IQ export was short (check it covered all four stores).",
                    mag=0.9)
    except Exception:
        pass

    ins = pd.DataFrame(rows)
    if ins.empty:
        return ins
    sev_rank = {"bad": 0, "warn": 1, "ok": 2, "info": 3}
    ins["rank"] = ins.severity.map(sev_rank)
    ins = ins.sort_values(["store_key", "rank", "magnitude"], ascending=[True, True, False])
    ins["position"] = ins.groupby("store_key").cumcount() + 1
    return ins.reset_index(drop=True)


# -------------------------------------------------------------- driver
def build_model(con) -> dict:
    mw, last_txn, hol = build_model_week(con)
    comp = mw[mw.complete]
    fut = _future_weeks(mw, last_txn, hol)
    results, coef_rows, fit_frames, bt_rows, bt_sums, fc_rows, sums = {}, [], [], [], [], [], []
    for (sk, cat), g in comp.groupby(["store_key", "category"]):
        fx = fut[(fut.store_key == sk) & (fut.category == cat)]
        res = _fit_unit(g.reset_index(drop=True), fx)
        results[(sk, cat)] = res
        if res is None:
            sums.append(dict(store_key=int(sk), category=cat, model_kind="none",
                             n_weeks=int(len(g)), n_weeks_all=int(len(g)), r2=None, sigma=None,
                             thin=True, mean_units=float(g.units.mean()),
                             unit_price=float(g.net.sum() / max(1.0, g.units.sum()))))
            continue
        for c in res["coef"]:
            coef_rows.append(dict(store_key=int(sk), category=cat, **c))
        fit_frames.append(res["fit"])
        bt_rows += res["backtest"]
        if res["backtest_summary"]:
            bt_sums.append(dict(store_key=int(sk), category=cat, **res["backtest_summary"]))
        fc_rows += res["forecast"]
        sums.append(dict(store_key=int(sk), category=cat, **res["summary"]))

    brand = build_brand(con, mw, last_txn, {})
    ins = build_insights(con, mw, results, brand, last_txn)

    def put(name, frame: pd.DataFrame):
        con.execute(f"DROP TABLE IF EXISTS {name}")
        if frame is None or frame.empty:
            con.execute(f"CREATE TABLE {name} AS SELECT 1 AS _empty WHERE FALSE")
            return 0
        con.register("_f", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _f")
        con.unregister("_f")
        return len(frame)

    counts = {
        "dash_model_week": put("dash_model_week", mw.drop(columns=["sin", "cos"])),
        "dash_model_summary": put("dash_model_summary", pd.DataFrame(sums)),
        "dash_model_coef": put("dash_model_coef", pd.DataFrame(coef_rows)),
        "dash_model_fit": put("dash_model_fit", pd.concat(fit_frames, ignore_index=True) if fit_frames else pd.DataFrame()),
        "dash_model_backtest": put("dash_model_backtest", pd.DataFrame(bt_rows)),
        "dash_model_backtest_summary": put("dash_model_backtest_summary", pd.DataFrame(bt_sums)),
        "dash_model_forecast": put("dash_model_forecast", pd.DataFrame(fc_rows)),
        "dash_model_brand": put("dash_model_brand", brand),
        "dash_insights": put("dash_insights", ins),
    }
    con.execute("DROP TABLE IF EXISTS dash_model_meta")
    con.execute("CREATE TABLE dash_model_meta AS SELECT ? AS built_at, ? AS last_complete_week, "
                "? AS min_weeks, ? AS good_weeks, ? AS backtest_weeks, ? AS forecast_weeks",
                [dt.datetime.now(), str(comp.week_start.max()), MIN_WEEKS, GOOD_WEEKS,
                 BACKTEST_WEEKS, FORECAST_WEEKS])
    return counts


def explain(con, category: str, store_key: int) -> None:
    """A Stata-style readout for one fit, for learning and for checking."""
    mw, last_txn, hol = build_model_week(con)
    g = mw[(mw.complete) & (mw.store_key == store_key) & (mw.category == category)].reset_index(drop=True)
    res = _fit_unit(g, None)
    if res is None:
        print(f"not enough complete weeks ({len(g)}) for {category} at {STORE_LABEL[store_key]}")
        return
    sm = res["summary"]
    print(f"\n{category} at {STORE_LABEL[store_key]}  ·  units per week  ·  "
          f"{sm['n_weeks']} weeks  ·  R² = {sm['r2']:.3f}  ·  residual sd = {sm['sigma']:.1f}\n")
    print(f"{'term':<26}{'coef':>12}{'std err':>12}{'likely low':>12}{'likely high':>12}  clear?")
    for c in res["coef"]:
        print(f"{c['label']:<26}{c['coef']:>12.2f}{c['se']:>12.2f}{c['lo']:>12.2f}{c['hi']:>12.2f}  "
              f"{'yes' if c['clear'] else 'no'}")
    bt = res["backtest_summary"]
    if bt:
        print(f"\nBacktest (fit through {bt['cutoff_week']}, forecast {BACKTEST_WEEKS} weeks): "
              f"model MAE {bt['mae_model']:.1f} vs same-as-last-week {bt['mae_last']:.1f}, "
              f"last-4-mean {bt['mae_last4']:.1f}  →  model {bt['verdict']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dash", default=str(HERE / "cerebral_dash.duckdb"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--explain", nargs=2, metavar=("CATEGORY", "STORE_KEY"))
    a = ap.parse_args()
    if a.check:
        con = duckdb.connect(a.dash, read_only=True)
        for t in ("dash_model_week", "dash_model_coef", "dash_model_fit", "dash_model_backtest",
                  "dash_model_forecast", "dash_model_brand", "dash_insights"):
            try:
                print(f"  {t:<30}{con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>8,}")
            except Exception as e:
                print(f"  {t:<30} missing ({e})")
        return
    if a.explain:
        con = duckdb.connect(a.dash, read_only=True)
        explain(con, a.explain[0], int(a.explain[1]))
        return
    con = duckdb.connect(a.dash)
    counts = build_model(con)
    con.close()
    for k, v in counts.items():
        print(f"  {k:<30}{v:>8,} rows")


if __name__ == "__main__":
    main()
