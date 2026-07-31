"""
apply_twocharts.py — one-time upgrade for Cerebral's Insights drill-down.

In "Go deeper on a pair", the top-brands ranking covers BOTH categories in
one list, so the bigger category (e.g. Flower) fills the whole top 10 and
the smaller one (e.g. Concentrate) never appears. This patch replaces that
single ranking with TWO side-by-side charts — one per category, each with
its own top 10 brands — with Net $ and Change % on hover.

Run it in the folder that contains cerebral_public.py:
    python apply_twocharts.py
"""

import re
import sys
from pathlib import Path

app = Path(__file__).with_name("cerebral_public.py")
if not app.exists():
    sys.exit("ERROR: cerebral_public.py not found. "
             "Put apply_twocharts.py in the same folder as the app.")

src = app.read_text(encoding="utf-8")
lines = src.splitlines(keepends=True)

if "topbrands_" in src:
    sys.exit("Already patched — the two per-category charts are present. "
             "No changes made.")

# --- locate the block -------------------------------------------------------
# Anchor inside the pair drill-down: the brand-trend query.
anchor = next((i for i, l in enumerate(lines) if "FROM dash_brand_trend" in l),
              None)
if anchor is None:
    sys.exit("ERROR: could not find the brand-trend query (dash_brand_trend). "
             "No changes made — send me this message.")

# Block start: the heading above the current top-brands rendering.
head_candidates = ("top brands of the categories", "brands in these categories",
                   "top brands")
start = next((i for i in range(anchor, len(lines))
              if any(c in lines[i].lower() for c in head_candidates)), None)
if start is None:
    sys.exit("ERROR: could not find the top-brands heading after the query. "
             "No changes made — send me this message.")

# Block end: the brand-pairs section that follows it.
end = next((i for i in range(start + 1, len(lines))
            if "# --- brand pairs" in lines[i] or "bp = q(" in lines[i]), None)
if end is None:
    sys.exit("ERROR: found the heading but not where the block ends. "
             "No changes made — send me this message.")

indent = re.match(r"\s*", lines[start]).group(0)

# --- replacement block (written at zero indent, re-indented below) ----------
block = '''
st.markdown("**Top brands in each category**")
st.markdown('<p class="note">One chart per category. In a single shared '
            'ranking the bigger category fills the whole top 10 and the '
            'smaller one never appears.</p>', unsafe_allow_html=True)
_cd = ["change"] if "change" in bt.columns else None
_ht = ("%{y}<br>Net $%{x:,.0f}<br>Change %{customdata[0]:+.1f}%<extra></extra>"
       if _cd else "%{y}<br>Net $%{x:,.0f}<extra></extra>")
tc1, tc2 = st.columns(2)
for _col, _cat in zip((tc1, tc2), (ca, cb)):
    _tb = (bt[bt.category == _cat]
           .sort_values("net_total", ascending=False).head(10))
    with _col:
        st.markdown(f"##### {_cat}")
        if _tb.empty:
            st.info(f"No brand-level volume in {_cat} in this window.")
            continue
        _fig = px.bar(_tb.sort_values("net_total"),
                      x="net_total", y="brand", orientation="h",
                      color_discrete_sequence=[CAT_COLORS.get(_cat, ACCENT)],
                      custom_data=_cd)
        _fig.update_traces(hovertemplate=_ht)
        _fig.update_layout(height=max(260, 36 * len(_tb)),
                           margin=dict(l=0, r=0, t=8, b=0),
                           xaxis=dict(title="", tickformat="$~s",
                                      gridcolor="rgba(0,0,0,.06)"),
                           yaxis=dict(title=""),
                           showlegend=False,
                           plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(_fig, use_container_width=True,
                        key=f"topbrands_{_cat}")
'''.lstrip("\n")

new_lines = [(indent + l if l.strip() else l) + "\n"
             for l in block.rstrip("\n").splitlines()]
new_src = "".join(lines[:start] + new_lines + ["\n"] + lines[end:])

# --- safety: compile before writing ----------------------------------------
import py_compile, tempfile, os
tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
tmp.write(new_src)
tmp.close()
try:
    py_compile.compile(tmp.name, doraise=True)
except py_compile.PyCompileError as e:
    os.unlink(tmp.name)
    sys.exit(f"ERROR: patched file would not compile, nothing was written.\n{e}")
os.unlink(tmp.name)

backup = app.with_name("cerebral_public_backup_twocharts.py")
backup.write_text(src, encoding="utf-8")
app.write_text(new_src, encoding="utf-8")

print(f"Done. Replaced lines {start + 1}-{end} with two per-category charts.")
print(f"Backup saved as: {backup.name}")
print()
print("Now refresh the app (or relaunch: python -m streamlit run cerebral_public.py)")
print("Then: Insights > Which categories get bought together > pick a pair.")
