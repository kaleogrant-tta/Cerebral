"""
apply_twotables.py — one-time upgrade for Cerebral's Insights drill-down.

The "Top products in these categories" table ranks BOTH categories together,
so the bigger category (e.g. Flower) fills the whole list and the smaller
one never appears. This patch:
  1. Removes the overall LIMIT in the product query (it could cut the
     smaller category's products before the table is even built).
  2. Replaces the single table with TWO side-by-side tables — one per
     category, top 10 products each.

Run it in the folder that contains cerebral_public.py:
    python apply_twotables.py
"""

import re
import sys
from pathlib import Path

app = Path(__file__).with_name("cerebral_public.py")
if not app.exists():
    sys.exit("ERROR: cerebral_public.py not found. "
             "Put apply_twotables.py in the same folder as the app.")

src = app.read_text(encoding="utf-8")
lines = src.splitlines(keepends=True)

if "Top products in each category" in src:
    sys.exit("Already patched — the two per-category tables are present. "
             "No changes made.")

# --- 1) lift the LIMIT in the product query ---------------------------------
q = next((i for i, l in enumerate(lines) if "FROM dash_product_trend" in l),
         None)
if q is None:
    sys.exit("ERROR: could not find the product query (dash_product_trend). "
             "No changes made — send me this message.")
for i in range(q, min(q + 8, len(lines))):
    lines[i] = re.sub(r"ORDER BY net_total DESC LIMIT \d+",
                      "ORDER BY net_total DESC", lines[i])

# --- 2) locate the single-table rendering -----------------------------------
start = next((i for i, l in enumerate(lines)
              if "Top products in these categories" in l), None)
if start is None:
    sys.exit("ERROR: could not find the 'Top products in these categories' "
             "heading. No changes made — send me this message.")

# Block ends where the drill-down section dedents back out (e.g. "inv = q(").
end = next((i for i in range(start + 1, min(start + 80, len(lines)))
            if re.match(r"^    \S", lines[i])), None)
if end is None:
    sys.exit("ERROR: found the heading but not where the block ends. "
             "No changes made — send me this message.")

indent = re.match(r"\s*", lines[start]).group(0)

# --- replacement block (zero indent, re-indented below) ---------------------
block = '''
st.markdown("**Top products in each category**")
pt1, pt2 = st.columns(2)
for _col, _cat in zip((pt1, pt2), (ca, cb)):
    _tp = (pt[pt.category == _cat]
           .sort_values("net_total", ascending=False).head(10))
    with _col:
        st.markdown(f"##### {_cat}")
        if _tp.empty:
            st.info(f"No product-level volume in {_cat} in this window.")
            continue
        st.dataframe(pd.DataFrame({
            "Brand": _tp.brand,
            "Product": _tp["product"],
            "Net $": _tp.net_total.round(0),
            "Change %": pd.to_numeric(_tp.change, errors="coerce").round(1),
        }), use_container_width=True, hide_index=True, column_config={
            "Net $": st.column_config.NumberColumn(format="$%d"),
            "Change %": st.column_config.NumberColumn(
                help="Second half versus first half of the period.",
                format="%.1f%%"),
        })
st.markdown('<p class="note">Product names change often as SKUs turn over, '
            'so read these as examples of where the movement sits rather '
            'than a stable ranking.</p>', unsafe_allow_html=True)
'''.lstrip("\n")

new_lines = [(indent + l if l.strip() else l) + "\n"
             for l in block.rstrip("\n").splitlines()]
new_src = "".join(lines[:start] + new_lines + ["\n"] + lines[end:])

# --- safety: compile before writing -----------------------------------------
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

backup = app.with_name("cerebral_public_backup_twotables.py")
backup.write_text(src, encoding="utf-8")
app.write_text(new_src, encoding="utf-8")

print(f"Done. Replaced lines {start + 1}-{end} with two per-category tables.")
print(f"Backup saved as: {backup.name}")
print()
print("Refresh the app, then: Insights > pick a pair > 'Top products in each category'.")
