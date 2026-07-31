"""
apply_holo.py — one-time visual upgrade for Cerebral.

What it does:
  1. Backs up cerebral_public.py -> cerebral_public_backup_holo.py
  2. Adds a loader that injects theme.css into the app
  3. Replaces the plain st.title("Cerebral") header with the
     holographic foil wordmark

How to run (in the folder that contains cerebral_public.py and theme.css):
    python apply_holo.py
"""

import re
import sys
from pathlib import Path

app = Path(__file__).with_name("cerebral_public.py")
css = Path(__file__).with_name("theme.css")

if not app.exists():
    sys.exit("ERROR: cerebral_public.py not found. "
             "Put apply_holo.py in the same folder as the app (C:\\Users\\User\\cerebral).")
if not css.exists():
    sys.exit("ERROR: theme.css not found. Download it into the same folder first.")

src = app.read_text(encoding="utf-8")
new = src
steps = []

# --- step 1: theme.css loader ----------------------------------------------
if "theme.css" in new:
    steps.append("CSS loader: already present, skipped")
else:
    loader = (
        "\n# --- theme stylesheet --------------------------------------------------\n"
        "from pathlib import Path as _Path\n"
        "_css = _Path(__file__).with_name('theme.css')\n"
        "if _css.exists():\n"
        "    st.markdown(\n"
        "        f'<style>{_css.read_text(encoding=\"utf-8\")}</style>',\n"
        "        unsafe_allow_html=True,\n"
        "    )\n"
        "# --------------------------------------------------------------------------\n"
    )
    m = re.search(r"st\.set_page_config\([^)]*\)", new, flags=re.DOTALL)
    if not m:
        sys.exit("ERROR: could not find st.set_page_config(...) — no changes were made.")
    new = new[:m.end()] + "\n" + loader + new[m.end():]
    steps.append("CSS loader: added after st.set_page_config")

# --- step 2: holographic wordmark ------------------------------------------
if "holo-wordmark" in new:
    steps.append("Wordmark: already present, skipped")
else:
    pat = re.compile(
        r'^[ \t]*st\.title\("Cerebral"\)[ \t]*\r?\n'
        r'[ \t]*label\s*=\s*"All stores"[^\n]*\r?\n'
        r'[ \t]*st\.caption\(f"Category analytics[^\n]*\{label\}"\)[ \t]*$',
        flags=re.MULTILINE,
    )
    m = pat.search(new)
    if not m:
        sys.exit("ERROR: could not find the st.title(\"Cerebral\") header block — "
                 "no changes were made. Send me the error and I'll adjust.")
    repl = (
        'label = ("All stores" if len(keys) == len(STORES)\n'
        '         else ", ".join(STORES[k] for k in keys))\n'
        '\n'
        'st.markdown(f"""\n'
        '<div class="holo-wordmark">\n'
        '  <div class="holo-eyebrow">The Travel Agency</div>\n'
        '  <h1 class="holo-title" data-text="Cerebral">Cerebral</h1>\n'
        '  <div class="holo-sub">Category analytics &middot; <b>{label}</b></div>\n'
        '</div>\n'
        '""", unsafe_allow_html=True)'
    )
    new = new[:m.start()] + repl + new[m.end():]
    steps.append("Wordmark: holographic title installed")

# --- save --------------------------------------------------------------------
if new != src:
    backup = app.with_name("cerebral_public_backup_holo.py")
    backup.write_text(src, encoding="utf-8")
    app.write_text(new, encoding="utf-8")
    steps.append(f"Backup saved as: {backup.name}")

print("Done:")
for s in steps:
    print("  -", s)
print()
print("Now relaunch the app:")
print("  python -m streamlit run cerebral_public.py")
