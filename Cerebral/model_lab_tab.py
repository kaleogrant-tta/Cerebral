"""
model_lab_tab.py -- the Model Lab tab, the TL;DR block at the top of Insights,
the supply-vs-demand note on the scorecard alerts, and the "demand after
stock" block on Brand Efficiency.

Everything here reads the dash_model_* / dash_insights tables that
publish_model.py writes. Nothing is fitted in the app; the app only draws.

The idea, in one paragraph (it is the same worksheet Justin Wolfers used for
consumer sentiment, applied to a cannabis store): a regression draws the best
line through weekly units sold, explaining them with what we can measure --
SKUs in stock, promo and takeover weeks, holidays, days open, the season and a
slow trend. Each coefficient reads "holding everything else in the model
fixed, one more of this is worth so many units a week". Whatever the line
cannot explain is the residual, and a big residual is the interesting part:
a dip the shelf does not account for is a demand problem; a dip the shelf
does account for is a supply problem. Then the honest test: fit on data up to
eight weeks ago, forecast those eight weeks, and see whether the model beat
"same as last week". If it did not, its forecasts are a rough guide only.

Entry points (all keyword-only, same style as the other *_tab modules)
    render_model_lab(q, keys, stores, heading, table_exists, accent, warn, bad, series)
    render_tldr(q, keys, stores, table_exists, howto)        # top of Insights
    model_note(q, store_key, category, iso_year, iso_week)   # one sentence for an alert
    render_brand_block(q, keys, stores, heading, table_exists, accent)
"""
from __future__ import annotations

import html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import charts

STORE_LABEL = {0: "All stores", 1: "DTBK", 2: "5th Avenue", 3: "Soho", 4: "Union Square"}
SECTION_LABEL = {"model-lab": "Model Lab", "brand-efficiency": "Brand Efficiency",
                 "promo-lab": "Promo Lab", "redemptions": "Redemptions", "insights": "Insights"}
TLDR_SHOW = 8


# ------------------------------------------------------------------ helpers
def _scope_key(keys: list[int], stores: dict) -> int:
    """One store when exactly one is picked in the sidebar, otherwise the
    chain (store_key 0). The fits are per store, so a two-store pick cannot be
    summed; the chain fit is the honest fallback and the caption says so."""
    return int(keys[0]) if len(keys) == 1 else 0


def _tables_ok(table_exists) -> bool:
    return all(table_exists(t) for t in ("dash_model_summary", "dash_model_coef", "dash_model_fit"))


def _wk_date(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df.week_start)


def _units(x) -> str:
    try:
        return f"{int(round(float(x))):,}"
    except (TypeError, ValueError):
        return "–"


def _money(x) -> str:
    try:
        return f"${int(round(float(x))):,}"
    except (TypeError, ValueError):
        return "–"


# ---------------------------------------------------------------- TL;DR
def render_tldr(*, q, keys, stores, table_exists, howto=None) -> None:
    """The plain-English feed. Chain lines first when every store is selected,
    then the selected stores' own lines; bad before warn before good."""
    st.markdown("#### TL;DR")
    if not table_exists("dash_insights"):
        st.info("The plain-English summary needs a newer data file than the one "
                "currently published. It appears after the next refresh.")
        return
    if howto:
        howto("tldr")
    ins = q("SELECT * FROM dash_insights")
    if ins.empty or "headline" not in ins.columns:
        st.markdown('<div class="alert a-ok">Nothing stood out this week: every category '
                    'and brand with enough history sold about what the model expected.</div>',
                    unsafe_allow_html=True)
        return
    want = set(int(k) for k in keys)
    if len(keys) == len(stores):
        want.add(0)
    ins = ins[ins.store_key.isin(want)].copy()
    sev_rank = {"bad": 0, "warn": 1, "ok": 2, "info": 3}
    ins["rank"] = ins.severity.map(sev_rank).fillna(4)
    ins["store_rank"] = (ins.store_key != 0).astype(int)
    ins = ins.sort_values(["rank", "store_rank", "magnitude"], ascending=[True, True, False])
    if ins.empty:
        st.markdown('<div class="alert a-ok">Nothing stood out for the selected store(s) this week.</div>',
                    unsafe_allow_html=True)
        return
    wk = ins.week.iloc[0]
    st.markdown(f'<p class="note">Assessed on the last complete week ({wk}). Each line is a '
                f'statement, a suggestion or a question the numbers raised; the tab named on '
                f'the right has the detail. Lines are prompts to look, not conclusions.</p>',
                unsafe_allow_html=True)

    def line(r):
        cls = {"bad": "a-bad", "warn": "a-warn", "ok": "a-ok", "info": "a-ok"}.get(r.severity, "a-ok")
        kind = {"finding": "", "suggestion": "Suggestion · ", "question": "Question · "}.get(r.kind, "")
        where = SECTION_LABEL.get(r.section, r.section)
        slug = html.escape(str(r.section))
        store = "" if r.store_key == 0 and len(keys) == len(stores) else f"{html.escape(str(r.store))} · "
        detail = html.escape(str(r.detail)) if isinstance(r.detail, str) and r.detail else ""
        st.markdown(
            f'<div class="alert {cls}"><span style="opacity:.65;font-size:.8rem">{store}{kind}'
            f'<a href="?tab={slug}" target="_self" style="text-decoration:none">{where} →</a></span><br>'
            f'<b>{html.escape(str(r.headline))}</b>'
            + (f'<br><span style="font-size:.86rem;opacity:.85">{detail}</span>' if detail else "")
            + '</div>', unsafe_allow_html=True)

    head, rest = ins.head(TLDR_SHOW), ins.iloc[TLDR_SHOW:]
    for r in head.itertuples(index=False):
        line(r)
    if not rest.empty:
        with st.expander(f"{len(rest)} more"):
            for r in rest.itertuples(index=False):
                line(r)


# ------------------------------------------------------ scorecard note
def model_note(*, q, table_exists, store_key: int, category: str,
               iso_year: int, iso_week: int) -> str:
    """One sentence to append to a penetration alert: was the shelf behind it?"""
    if not table_exists("dash_model_fit"):
        return ""
    f = q(f"""
        SELECT residual, resid_z, stock_vs_norm, explained_by_stock, skus_in_stock
        FROM dash_model_fit
        WHERE store_key = {int(store_key)} AND category = '{category.replace("'", "''")}'
          AND iso_year = {int(iso_year)} AND iso_week = {int(iso_week)}
    """)
    if f.empty:
        return ""
    r = f.iloc[0]
    if pd.isna(r.stock_vs_norm):
        return " Model: no stock history for this category yet, so supply vs demand can't be told apart."
    if r.residual < 0 and r.explained_by_stock < 0 and abs(r.explained_by_stock) >= 0.5 * abs(r.residual):
        return (f" Model: the shelf held {abs(int(r.stock_vs_norm))} fewer SKUs than its recent norm, "
                f"which explains most of the shortfall — supply-driven.")
    if r.residual < 0:
        return (f" Model: the shelf was {'normal' if abs(r.stock_vs_norm) < 3 else 'about as full as usual'} "
                f"and units still ran {_units(-r.residual)} below expectation — demand-driven.")
    return ""


# --------------------------------------------------------- Brand block
def render_brand_block(*, q, keys, stores, heading, table_exists, accent="#2F6F4F") -> None:
    """Appended to Brand Efficiency: each brand's last four weeks against what
    its own stock, season and takeover history said to expect."""
    st.divider()
    heading("Demand after stock")
    if not table_exists("dash_model_brand"):
        st.info("Brand-level models appear after the next refresh.")
        return
    sk = _scope_key(keys, stores)
    b = q(f"SELECT * FROM dash_model_brand WHERE store_key = {sk}")
    if b.empty or "brand" not in b.columns:
        st.info("No brand has enough weekly history yet for a model.")
        return
    st.markdown(
        f'<p class="note">For {STORE_LABEL[sk]}. Each brand\'s units over the last four weeks '
        f'against what a model of its own history expected, with the shelf (products on the '
        f'floor at week end, from the inventory ledgers) held fixed where that history exists. '
        f'A brand that sold modestly while half its products were out is not weak — this view '
        f'separates the two. "Stock worth" is how many extra units a week one more product '
        f'on the floor has been worth; "Stockout cost" is that times the products out times '
        f'the brand\'s price.'
        + (" Pick one store in the sidebar for the store-level fits." if sk == 0 and len(keys) > 1 else "")
        + '</p>', unsafe_allow_html=True)
    cats = sorted(b.category.dropna().unique().tolist())
    pick = st.selectbox("Category", ["All"] + cats, key="mb_cat")
    if pick != "All":
        b = b[b.category == pick]
    b = b.sort_values("gap_pct")
    conf = np.where(b.thin, "thin history", np.where(b.model_kind == "stock", "ok, with stock", "ok, no stock data"))
    st.dataframe(pd.DataFrame({
        "Brand": b.brand, "Category": b.category,
        "Units (4 wk)": b.units_last4.round(0), "Expected": b.predicted_last4.round(0),
        "Gap %": pd.to_numeric(b.gap_pct, errors="coerce").round(0),
        "Stock worth (units/wk per product)": pd.to_numeric(b.stock_coef, errors="coerce").round(1),
        "Products out /wk": pd.to_numeric(b.avg_products_out, errors="coerce").round(1),
        "Stockout cost /wk": pd.to_numeric(b.stockout_cost_week, errors="coerce").round(0),
        "Confidence": conf, "Weeks": b.n_weeks,
    }), hide_index=True, use_container_width=True, column_config={
        "Gap %": st.column_config.NumberColumn(format="%+.0f%%",
            help="Actual minus expected, as a share of expected. Negative = demand below the model."),
        "Stockout cost /wk": st.column_config.NumberColumn(format="$%d",
            help="Blank when the stock effect is not clearly different from zero."),
        "Stock worth (units/wk per product)": st.column_config.NumberColumn(format="%.1f",
            help="Regression coefficient on products-on-floor. Blank without stock history."),
    })
    st.caption("A regression finds patterns, not causes: 'stock worth' means weeks with more of "
               "the brand on the floor sold more of it, which is partly buyers restocking what "
               "sells. Read it as 'associated with', and trust it more as the weeks column grows.")


# ---------------------------------------------------------- Model Lab
def render_model_lab(*, q, keys, stores, heading, table_exists, accent="#2F6F4F",
                     warn="#B4632B", bad="#B33218", series=None) -> None:
    st.markdown("#### Model Lab")
    if not _tables_ok(table_exists):
        st.info("Model Lab needs a newer data file than the one currently published. "
                "It appears after the next refresh.")
        return
    meta = q("SELECT * FROM dash_model_meta").iloc[0] if table_exists("dash_model_meta") else None
    sk = _scope_key(keys, stores)
    summ = q(f"SELECT * FROM dash_model_summary WHERE store_key = {sk} ORDER BY mean_units DESC")
    if summ.empty:
        st.info("No category at this store has enough complete weeks for a model yet.")
        return
    st.markdown(
        '<p class="note">A regression explains weekly <b>units sold</b> per category with what '
        'we can measure — SKUs in stock, promo and takeover weeks, holidays, days open, season '
        'and a slow trend — then shows what it <i>cannot</i> explain, which is usually the '
        'interesting part. Same method as an economist explaining consumer sentiment with '
        'inflation and unemployment: the leftover is the thing to go and find out about.'
        + (" <b>Showing the chain fit</b> — pick one store in the sidebar for its own model." if sk == 0 and len(keys) > 1 else f" Showing <b>{STORE_LABEL[sk]}</b>.")
        + '</p>', unsafe_allow_html=True)

    cats = summ.category.tolist()
    default = cats.index("Flower") if "Flower" in cats else 0
    c1, c2 = st.columns([2, 3])
    with c1:
        cat = st.selectbox("Category", cats, index=default, key="ml_cat")
    s = summ[summ.category == cat].iloc[0]
    with c2:
        if s.model_kind == "none":
            st.warning(f"Only {int(s.n_weeks)} complete weeks — the fit needs "
                       f"{int(meta.min_weeks) if meta is not None else 20}.")
            return
        kind_txt = ("with SKUs-in-stock as a control" if s.model_kind == "stock"
                    else "without stock (no inventory history for this category)")
        thin_txt = " · thin history, read with care" if s.thin else ""
        st.markdown(f'<p class="note" style="margin-top:1.6rem">{int(s.n_weeks)} complete weeks, '
                    f'{kind_txt}{thin_txt}.</p>', unsafe_allow_html=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Weeks fitted", int(s.n_weeks))
    m2.metric("R²", f"{float(s.r2):.2f}", help="Share of the week-to-week ups and downs the model "
              "explains. 0.6 = 60%. Low is not wrong — it means most of the movement is things we "
              "don't measure.")
    m3.metric("Typical miss (units)", _units(s.sigma), help="Residual standard deviation: how far "
              "a normal week lands from the line. Gaps beyond ~1.5× this are flagged.")
    m4.metric("Avg units / week", _units(s.mean_units))

    t_drv, t_gap, t_fc, t_how = st.tabs(["Drivers", "The gap", "Forecast & backtest", "How to read this"])

    # ------------------------------------------------------------ drivers
    with t_drv:
        coef = q(f"SELECT * FROM dash_model_coef WHERE store_key = {sk} AND category = '{cat}'")
        price = float(s.unit_price) if pd.notna(s.unit_price) else 0.0
        rows = coef[coef.term != "const"].copy()
        heading("What moves weekly units")
        st.markdown('<p class="note">Each line: holding the other drivers fixed, one more of this '
                    'is worth this many units a week. The bar is the estimate, the whisker its '
                    'likely range (±2 standard errors). A whisker that crosses zero means the '
                    'data cannot tell the effect from nothing — shown grey.</p>',
                    unsafe_allow_html=True)
        if rows.empty:
            st.info("Only a baseline could be fitted.")
        else:
            fig = charts.fig_base(kind="hbar", height=max(220, 44 * len(rows) + 60))
            colours = [accent if c else charts._rgba(accent, 0.3) for c in rows.clear]
            fig.add_trace(go.Bar(
                y=rows.label, x=rows.coef, orientation="h", marker_color=colours,
                error_x=dict(type="data", symmetric=False,
                             array=(rows.hi - rows.coef), arrayminus=(rows.coef - rows.lo),
                             color="rgba(120,120,120,.6)", thickness=1.2, width=4),
                hovertemplate="%{y}<br>%{x:.1f} units/week<extra></extra>"))
            fig.add_vline(x=0, line_color="rgba(120,120,120,.5)", line_width=1)
            fig.update_xaxes(title_text="units per week")
            charts.show(fig, key=f"ml_coef_{sk}_{cat}", kind="hbar")

            def words(r):
                if r.term == "stock":
                    return (f"One more SKU on the shelf ≈ {r.coef:+.1f} units/wk "
                            f"({_money(r.coef * price)}); a SKU out for a week costs about that.")
                if r.term == "promo":
                    return f"A full promo week ≈ {r.coef:+.0f} units ({_money(r.coef * price)}) over a normal week."
                if r.term == "takeover":
                    return f"A full takeover week ≈ {r.coef:+.0f} units ({_money(r.coef * price)})."
                if r.term == "holiday":
                    return f"A holiday week (4/20, Green Wednesday, Christmas…) ≈ {r.coef:+.0f} units."
                if r.term == "days_open":
                    return f"Each trading day ≈ {r.coef:+.0f} units."
                if r.term == "trend":
                    return f"Underlying drift of {r.coef:+.1f} units per week, ≈ {r.coef * 52:+.0f} a year."
                return "Part of the yearly cycle (read the two season terms together)."
            st.dataframe(pd.DataFrame({
                "Driver": rows.label,
                "Units / week": rows.coef.round(1),
                "Likely range": [f"{lo:.0f} to {hi:.0f}" for lo, hi in zip(rows.lo, rows.hi)],
                "Clear?": np.where(rows.clear, "yes", "no — could be zero"),
                "In words": [words(r) for r in rows.itertuples(index=False)],
            }), hide_index=True, use_container_width=True)
            st.caption("A regression finds patterns, not causes. 'Takeover weeks sell more' is "
                       "true; 'the takeover caused it' needs more than this — brands pick takeover "
                       "windows when they are stocked and fresh.")

    # ------------------------------------------------------------- the gap
    with t_gap:
        fit = q(f"SELECT * FROM dash_model_fit WHERE store_key = {sk} AND category = '{cat}' "
                f"ORDER BY week_start")
        heading("Actual vs what the model expected")
        if fit.empty:
            st.info("No fitted weeks.")
        else:
            show = fit.tail(26).copy()
            show["d"] = _wk_date(show)
            fig = charts.fig_base(kind="line")
            fig.add_trace(go.Scatter(x=show.d, y=show.predicted, name="Expected",
                                     line=dict(color=charts._rgba(accent, 0.55), dash="dot", width=2)))
            fig.add_trace(go.Scatter(x=show.d, y=show.units, name="Actual",
                                     line=dict(color=accent, width=2.5)))
            fig.update_yaxes(title_text="units")
            charts.show(fig, key=f"ml_fit_{sk}_{cat}", kind="line")

            heading("The gap — what the model could not explain")
            st.markdown('<p class="note">Actual minus expected, each week. Red weeks fell short, '
                        'green beat the line. The hatched part of a bar is how much of the gap '
                        'the shelf accounts for (fewer or more SKUs than the recent norm); the '
                        'rest is demand the model cannot see.</p>', unsafe_allow_html=True)
            fig2 = charts.fig_base(kind="bar")
            col = [bad if v < 0 else accent for v in show.residual]
            fig2.add_trace(go.Bar(x=show.d, y=show.residual, name="Gap", marker_color=col,
                                  hovertemplate="%{x|%d %b}<br>gap %{y:.0f} units<extra></extra>"))
            if show.explained_by_stock.notna().any():
                fig2.add_trace(go.Bar(x=show.d, y=show.explained_by_stock, name="Explained by shelf",
                                      marker_color="rgba(120,120,120,.45)",
                                      marker_pattern_shape="/",
                                      hovertemplate="%{x|%d %b}<br>shelf part %{y:.0f} units<extra></extra>"))
            band = float(s.sigma) * 1.5
            fig2.add_hline(y=band, line_dash="dot", line_color="rgba(120,120,120,.5)")
            fig2.add_hline(y=-band, line_dash="dot", line_color="rgba(120,120,120,.5)")
            fig2.update_layout(barmode="overlay")
            fig2.update_yaxes(title_text="units vs expected")
            charts.show(fig2, key=f"ml_gap_{sk}_{cat}", kind="bar")

            last = fit.iloc[-1]
            wk = f"{int(last.iso_year)}-W{int(last.iso_week):02d}"
            if abs(last.resid_z) >= 1.5:
                shelf = ""
                if pd.notna(last.stock_vs_norm):
                    part = last.explained_by_stock
                    if last.residual < 0 and part < 0 and abs(part) >= 0.5 * abs(last.residual):
                        shelf = (f" The shelf held {abs(int(last.stock_vs_norm))} fewer SKUs than normal, which "
                                 f"explains about {_units(abs(part))} of it — a supply gap.")
                    else:
                        shelf = " The shelf was about normal, so this is demand, not supply."
                cls = "a-bad" if last.residual < 0 else "a-ok"
                st.markdown(f'<div class="alert {cls}"><b>{wk}</b> — {cat} sold {_units(abs(last.residual))} '
                            f'{"fewer" if last.residual < 0 else "more"} units than expected '
                            f'({_money(abs(last.residual) * float(s.unit_price))}).{shelf}</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="alert a-ok"><b>{wk}</b> — within the normal range of the model.</div>',
                            unsafe_allow_html=True)
            with st.expander("Weekly table"):
                st.dataframe(pd.DataFrame({
                    "Week": [f"{int(y)}-W{int(w):02d}" for y, w in zip(fit.iso_year, fit.iso_week)],
                    "Units": fit.units.round(0), "Expected": fit.predicted.round(0),
                    "Gap": fit.residual.round(0), "Gap (sd)": fit.resid_z.round(2),
                    "SKUs in stock": fit.skus_in_stock, "vs norm": fit.stock_vs_norm.round(0),
                    "Promo days": fit.promo_days, "Takeover days": fit.takeover_days,
                    "Holiday": fit.holiday_name,
                }).iloc[::-1], hide_index=True, use_container_width=True)

    # ------------------------------------------------- forecast & backtest
    with t_fc:
        heading("Could we have seen it coming? (backtest)")
        bts = q(f"SELECT * FROM dash_model_backtest_summary WHERE store_key = {sk} AND category = '{cat}'")
        bt = q(f"SELECT * FROM dash_model_backtest WHERE store_key = {sk} AND category = '{cat}' ORDER BY week_start")
        if bts.empty or bt.empty:
            st.info("Not enough history for a held-out test yet (needs about 28 complete weeks).")
        else:
            b = bts.iloc[0]
            st.markdown(f'<p class="note">The model was refitted on data through <b>{b.cutoff_week}</b> '
                        f'only, then asked to forecast the {len(bt)} weeks after. Compare with the '
                        f'simplest rival: "same as last week". If the model cannot beat that, its '
                        f'forecasts are a rough guide, not a plan.</p>', unsafe_allow_html=True)
            bt["d"] = _wk_date(bt)
            fig = charts.fig_base(kind="line")
            fig.add_trace(go.Scatter(x=bt.d, y=bt.fcast_last, name="Same as last week",
                                     line=dict(color="rgba(120,120,120,.6)", dash="dash")))
            if (bt.fcast_nostock != bt.fcast_model).any():
                fig.add_trace(go.Scatter(x=bt.d, y=bt.fcast_nostock, name="Model without stock",
                                         line=dict(color=charts._rgba(warn, 0.8), dash="dot")))
            fig.add_trace(go.Scatter(x=bt.d, y=bt.fcast_model, name="Model forecast",
                                     line=dict(color=charts._rgba(accent, 0.7), dash="dot", width=2)))
            fig.add_trace(go.Scatter(x=bt.d, y=bt.actual, name="What happened",
                                     line=dict(color=accent, width=2.5)))
            fig.update_yaxes(title_text="units")
            charts.show(fig, key=f"ml_bt_{sk}_{cat}", kind="line")
            k1, k2, k3 = st.columns(3)
            k1.metric("Model avg miss", _units(b.mae_model), help="Mean absolute error over the held-out weeks.")
            k2.metric("'Same as last week' miss", _units(b.mae_last))
            k3.metric("Last-4-week-average miss", _units(b.mae_last4))
            v = b.verdict
            msg = {"beats": f"The model beat the simple rivals by {b.gain_vs_naive * 100:.0f}% — worth planning against.",
                   "ties":  "The model and 'same as last week' are about as good — use the model for the shape of promo and holiday weeks, not for precision.",
                   "loses": "The model did worse than 'same as last week' over this stretch — something changed it doesn't know about. Treat the forecast below as a rough guide."}[v]
            st.markdown(f'<div class="alert {"a-ok" if v == "beats" else "a-warn" if v == "ties" else "a-bad"}">{msg}</div>',
                        unsafe_allow_html=True)

        heading("Next four weeks")
        fc = q(f"SELECT * FROM dash_model_forecast WHERE store_key = {sk} AND category = '{cat}' ORDER BY week_start")
        fit = q(f"SELECT week_start, units FROM dash_model_fit WHERE store_key = {sk} AND category = '{cat}' "
                f"ORDER BY week_start")
        if fc.empty:
            st.info("No forecast rows.")
        else:
            fc["d"] = _wk_date(fc)
            hist = fit.tail(12).copy(); hist["d"] = _wk_date(hist)
            fig = charts.fig_base(kind="line")
            fig.add_trace(go.Scatter(x=hist.d, y=hist.units, name="Actual", line=dict(color=accent, width=2.5)))
            fig.add_trace(go.Scatter(x=pd.concat([fc.d, fc.d[::-1]]), y=pd.concat([fc.hi, fc.lo[::-1]]),
                                     fill="toself", fillcolor=charts._rgba(accent, 0.12), line_width=0,
                                     name="Typical range", hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=fc.d, y=fc.forecast, name="Forecast",
                                     line=dict(color=charts._rgba(accent, 0.8), dash="dot", width=2)))
            fig.update_yaxes(title_text="units")
            charts.show(fig, key=f"ml_fc_{sk}_{cat}", kind="line")
            why = []
            for r in fc.itertuples(index=False):
                tags = []
                if r.promo_days: tags.append(f"promo {int(r.promo_days)}d")
                if r.takeover_days: tags.append(f"takeover {int(r.takeover_days)}d")
                if isinstance(r.holiday_name, str): tags.append(r.holiday_name)
                why.append(", ".join(tags) or "ordinary week")
            st.dataframe(pd.DataFrame({
                "Week": [f"{int(y)}-W{int(w):02d}" for y, w in zip(fc.iso_year, fc.iso_week)],
                "Forecast": fc.forecast.round(0), "Low": fc.lo.round(0), "High": fc.hi.round(0),
                "Why": why,
            }), hide_index=True, use_container_width=True)
            stock_note = (f" Stock is assumed to stay at its recent level ({_units(fc.stock_assumed.iloc[0])} SKUs)."
                          if pd.notna(fc.stock_assumed.iloc[0]) else "")
            st.caption(f"The range is where about nine weeks in ten would land if the pattern holds. "
                       f"Promo and takeover days come from the calendar in promos.py and the "
                       f"Takeovers list, so a promo added there changes the forecast.{stock_note}")

    # ---------------------------------------------------------- how to read
    with t_how:
        st.markdown("""
**The worksheet, in store terms.** An economist asking "why is sentiment low?" first
checks how much inflation and unemployment explain, then looks at the leftover. We do the
same with weekly units:

1. **Fit a line.** `units = baseline + b₁·SKUs in stock + b₂·promo week + b₃·takeover week
   + b₄·holiday + b₅·days open + season + trend`. Least squares picks the b's that make the
   weekly misses as small as possible.
2. **Read the coefficients** (Drivers). Each is "one more of this, everything else held fixed".
   The likely range matters as much as the number: a promo effect of +80 with a range of
   −40 to +200 is not yet a result.
3. **Look at the residual** (The gap). A week far below the line with a normal shelf is a
   demand problem. Far below with a thin shelf is a supply problem. That is the "stock as
   a control" idea: stock is in the model so that what's left over is comparable week to week.
4. **Test it honestly** (Forecast & backtest). Refit on data up to eight weeks ago and
   forecast forward. Beating "same as last week" is the bar; a model that only fits the past
   is a story, not a tool.
5. **Remember what it is not.** Correlation, not cause. Buyers restock what sells, so stock
   and sales move together for reasons other than shelf-driving-demand. Treat every
   coefficient as "associated with" and let the backtest earn trust.

**Where the numbers come from.** `dash_category_week` (units, SKUs in stock, days open),
`promos.py` and the Takeovers list (promo/takeover days per week), a short holiday list
(4/20, Green Wednesday & Thanksgiving, Memorial Day, July 4, Labor Day, Christmas, New
Year). The partial current week is never fitted. Season terms switch on once a category
has a full year of history. `publish_model.py --explain Flower 4` prints the same fit as a
Stata-style table.
""")


__all__ = ["render_model_lab", "render_tldr", "model_note", "render_brand_block"]
