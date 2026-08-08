"""
wire_loyalty.py -- patch publish.py and cerebral_public.py to add the
Loyalty tab. Run it once; it edits the two files in place.

    python wire_loyalty.py

Timestamped backups are written beside each file before anything changes.
Safe to run twice -- every edit checks whether it has already been applied.

    python wire_loyalty.py --dry-run     show what would change, touch nothing
    python wire_loyalty.py --revert      restore the most recent backups
"""

import argparse
import glob
import os
import re
import shutil
import sys
import datetime as dt

PUBLISH = "publish.py"
PUBLIC = "cerebral_public.py"


class Patcher:
    def __init__(self, path, dry):
        self.path = path
        self.dry = dry
        self.text = open(path, encoding="utf-8").read()
        self.orig = self.text
        self.log = []

    def already(self, needle):
        return needle in self.text

    def after_line(self, anchor_re, insert, label):
        """Insert a line immediately after the first line matching anchor_re."""
        m = re.search(anchor_re, self.text, re.M)
        if not m:
            self.log.append(("FAIL", label, "anchor not found: %s" % anchor_re))
            return False
        end = self.text.index("\n", m.end()) + 1
        self.text = self.text[:end] + insert + self.text[end:]
        self.log.append(("OK", label, ""))
        return True

    def before_line(self, anchor_re, insert, label):
        m = re.search(anchor_re, self.text, re.M)
        if not m:
            self.log.append(("FAIL", label, "anchor not found: %s" % anchor_re))
            return False
        start = self.text.rfind("\n", 0, m.start()) + 1
        line_end = self.text.index("\n", m.start())
        indent = re.match(r"[ \t]*", self.text[start:line_end]).group(0)
        block = "".join((indent + ln if ln.strip() else "") + "\n"
                        for ln in insert.splitlines())
        self.text = self.text[:start] + block + self.text[start:]
        self.log.append(("OK", label, ""))
        return True

    def sub(self, pattern, repl, label, count=1):
        new, n = re.subn(pattern, repl, self.text, count=count, flags=re.M | re.S)
        if not n:
            self.log.append(("FAIL", label, "pattern not found"))
            return False
        self.text = new
        self.log.append(("OK", label, ""))
        return True

    def append(self, block, label):
        if not self.text.endswith("\n"):
            self.text += "\n"
        self.text += block
        self.log.append(("OK", label, ""))
        return True

    def save(self, stamp):
        if self.text == self.orig:
            return None
        if self.dry:
            return "(dry run)"
        bak = "%s.%s.bak" % (self.path, stamp)
        shutil.copy2(self.path, bak)
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(self.text)
        return bak


def patch_publish(dry):
    if not os.path.exists(PUBLISH):
        print("!! %s not found in this folder" % PUBLISH)
        return None
    p = Patcher(PUBLISH, dry)

    # 1. import
    if p.already("from publish_loyalty import build_loyalty"):
        p.log.append(("SKIP", "import build_loyalty", "already present"))
    else:
        p.after_line(r"^import duckdb\b.*$",
                     "\nfrom publish_loyalty import build_loyalty\n",
                     "import build_loyalty")

    # 2. tier in ALLOWED_TEXT
    if re.search(r'ALLOWED_TEXT\s*=\s*\{[^}]*"tier"', p.text, re.S):
        p.log.append(("SKIP", 'add "tier" to ALLOWED_TEXT', "already present"))
    else:
        p.sub(r'("canonical")(\s*\})', r'\1, "tier", "bin_label"\2',
              'add "tier"/"bin_label" to ALLOWED_TEXT')

    # 3. call the builder before DETACH
    if p.already("build_loyalty(con)"):
        p.log.append(("SKIP", "call build_loyalty(con)", "already present"))
    else:
        p.before_line(r'^[ \t]*con\.execute\("DETACH src"\)',
                      "# --- loyalty tiers x channel x store ---\n"
                      "build_loyalty(con)\n",
                      "call build_loyalty(con)")
    return p


def patch_public(dry):
    if not os.path.exists(PUBLIC):
        print("!! %s not found in this folder" % PUBLIC)
        return None
    p = Patcher(PUBLIC, dry)

    # 1. import
    if p.already("from loyalty_tab import render_loyalty"):
        p.log.append(("SKIP", "import render_loyalty", "already present"))
    else:
        p.after_line(r"^from glossary import .*$",
                     "from loyalty_tab import render_loyalty\n",
                     "import render_loyalty")

    # 2. add the tab to st.tabs(...)
    if "t_loyalty" in p.text:
        p.log.append(("SKIP", "add Loyalty tab", "already present"))
    else:
        ok = p.sub(
            r"t_charts, t_insights, t_brands, t_acc, t_redeem, t_takeover, "
            r"t_projections, t_promo, t_gloss = st\.tabs\(\s*\n?\s*\[.*?\]\)",
            't_charts, t_insights, t_brands, t_acc, t_redeem, t_loyalty, \\\n'
            '    t_takeover, t_projections, t_promo, t_gloss = st.tabs(\n'
            '    ["Charts", "Insights", "Brands", "Accessories", "Redemptions", "Loyalty",\n'
            '     "Takeovers", "Projections", "Promo Lab",\n'
            '     "What the terms mean"])',
            "add Loyalty tab")
        if not ok:
            return p

    # 3. render block, appended at end of file
    #    Tab ORDER comes from st.tabs(); where the `with` block sits does not
    #    matter, so appending avoids disturbing existing tab bodies.
    if "with t_loyalty:" in p.text:
        p.log.append(("SKIP", "render_loyalty block", "already present"))
    else:
        p.append(
            '\n\n# ---------------------------------------------------------------- loyalty\n'
            'with t_loyalty:\n'
            '    render_loyalty(q=q, keys=keys, keep=keep, stores=STORES,\n'
            '                   heading=heading, table_exists=table_exists,\n'
            '                   partial_week=PARTIAL_WEEK)\n',
            "render_loyalty block")
    return p


def revert():
    n = 0
    for target in (PUBLISH, PUBLIC):
        baks = sorted(glob.glob(target + ".*.bak"))
        if not baks:
            print("  no backup for %s" % target)
            continue
        shutil.copy2(baks[-1], target)
        print("  restored %s from %s" % (target, os.path.basename(baks[-1])))
        n += 1
    print("reverted %d file(s)" % n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()

    if a.revert:
        revert()
        return 0

    missing = [f for f in ("publish_loyalty.py", "loyalty_tab.py")
               if not os.path.exists(f)]
    if missing:
        print("!! missing in this folder: %s" % ", ".join(missing))
        print("   Copy them here first, then re-run.")
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = False

    for patcher in (patch_publish(a.dry_run), patch_public(a.dry_run)):
        if patcher is None:
            failed = True
            continue
        print()
        print(patcher.path)
        for status, label, note in patcher.log:
            mark = {"OK": " + ", "SKIP": " = ", "FAIL": " ! "}[status]
            print("  %s %-34s %s" % (mark, label, note))
            if status == "FAIL":
                failed = True
        bak = patcher.save(stamp)
        if bak:
            print("  backup: %s" % bak)
        else:
            print("  no changes needed")

    print()
    if failed:
        print("Some edits could not be applied. Nothing was half-written --")
        print("each file is either fully patched or untouched. Paste this")
        print("output back and the anchors can be adjusted.")
        return 1

    if a.dry_run:
        print("Dry run only. Re-run without --dry-run to apply.")
        return 0

    # syntax check
    import ast
    for f in (PUBLISH, PUBLIC):
        try:
            ast.parse(open(f, encoding="utf-8").read())
            print("  %s parses cleanly" % f)
        except SyntaxError as e:
            print("  !! %s has a syntax error at line %s: %s" % (f, e.lineno, e.msg))
            print("     Run: python wire_loyalty.py --revert")
            return 1

    print()
    print("Done. Next:")
    print("  python loyalty_ingest.py --db ..\\tta.duckdb")
    print("  python publish.py")
    print("  python -m streamlit run cerebral_public.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
