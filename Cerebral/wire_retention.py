"""
wire_retention.py -- patch publish.py and cerebral_public.py to add the
Retention tab. Run it once; it edits the two files in place.

    python wire_retention.py --dry-run    show what would change
    python wire_retention.py              apply
    python wire_retention.py --revert     restore the latest backups

Assumes the Loyalty tab is already wired (t_loyalty present). Timestamped
backups are written before anything changes, and every edit is skipped if
already applied, so re-running is safe.
"""

import argparse
import ast
import datetime as dt
import glob
import os
import re
import shutil
import sys

PUBLISH = "publish.py"
PUBLIC = "cerebral_public.py"
NEW_TEXT_COLS = ("first_channel", "seq_label", "gap_bucket")


class Patcher:
    def __init__(self, path, dry):
        self.path, self.dry = path, dry
        self.text = open(path, encoding="utf-8").read()
        self.orig = self.text
        self.log = []

    def has(self, needle):
        return needle in self.text

    def skip(self, label):
        self.log.append(("SKIP", label, "already present"))

    def ok(self, label):
        self.log.append(("OK", label, ""))

    def fail(self, label, why):
        self.log.append(("FAIL", label, why))

    def after_line(self, anchor, insert, label):
        m = re.search(anchor, self.text, re.M)
        if not m:
            return self.fail(label, "anchor not found")
        end = self.text.index("\n", m.end()) + 1
        self.text = self.text[:end] + insert + self.text[end:]
        self.ok(label)

    def before_line(self, anchor, insert, label):
        m = re.search(anchor, self.text, re.M)
        if not m:
            return self.fail(label, "anchor not found")
        start = self.text.rfind("\n", 0, m.start()) + 1
        line_end = self.text.index("\n", m.start())
        indent = re.match(r"[ \t]*", self.text[start:line_end]).group(0)
        block = "".join((indent + ln if ln.strip() else "") + "\n"
                        for ln in insert.splitlines())
        self.text = self.text[:start] + block + self.text[start:]
        self.ok(label)

    def sub(self, pattern, repl, label, flags=re.M | re.S):
        new, n = re.subn(pattern, repl, self.text, count=1, flags=flags)
        if not n:
            self.fail(label, "pattern not found")
            return False
        self.text = new
        self.ok(label)
        return True

    def append(self, block, label):
        if not self.text.endswith("\n"):
            self.text += "\n"
        self.text += block
        self.ok(label)

    def save(self, stamp):
        if self.text == self.orig:
            return None
        if self.dry:
            return "(dry run)"
        bak = "%s.retfix-%s.bak" % (self.path, stamp)
        shutil.copy2(self.path, bak)
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(self.text)
        return bak


def patch_publish(dry):
    if not os.path.exists(PUBLISH):
        print("!! %s not found" % PUBLISH)
        return None
    p = Patcher(PUBLISH, dry)

    if p.has("from publish_retention import build_retention"):
        p.skip("import build_retention")
    else:
        p.after_line(r"^import duckdb\b.*$",
                     "\nfrom publish_retention import build_retention\n",
                     "import build_retention")

    if re.search(r'ALLOWED_TEXT\s*=\s*\{[^}]*"first_channel"', p.text, re.S):
        p.skip("add retention columns to ALLOWED_TEXT")
    else:
        add = ", ".join('"%s"' % c for c in NEW_TEXT_COLS)
        done = False
        # Anchor on whatever currently sits last in the set.
        for anchor in ('"tier"', '"canonical"', '"product_sku"'):
            if re.search(r'ALLOWED_TEXT\s*=\s*\{[^}]*' + re.escape(anchor),
                         p.text, re.S):
                done = p.sub(r"(%s)(\s*\})" % re.escape(anchor),
                             r"\1, " + add + r"\2",
                             "add retention columns to ALLOWED_TEXT")
                break
        if not done:
            p.fail("add retention columns to ALLOWED_TEXT",
                   "could not locate the end of ALLOWED_TEXT")

    if p.has("build_retention(con)"):
        p.skip("call build_retention(con)")
    else:
        p.before_line(r'^[ \t]*con\.execute\("DETACH src"\)',
                      "# --- retention: cohorts, first three baskets, gaps ---\n"
                      "build_retention(con)\n",
                      "call build_retention(con)")
    return p


def patch_public(dry):
    if not os.path.exists(PUBLIC):
        print("!! %s not found" % PUBLIC)
        return None
    p = Patcher(PUBLIC, dry)

    if p.has("from retention_tab import render_retention"):
        p.skip("import render_retention")
    else:
        anchor = (r"^from loyalty_tab import .*$" if p.has("from loyalty_tab")
                  else r"^from glossary import .*$")
        p.after_line(anchor, "from retention_tab import render_retention\n",
                     "import render_retention")

    if "t_retention" in p.text:
        p.skip("add t_retention to the tab tuple")
    else:
        if not p.has("t_loyalty"):
            p.fail("add t_retention to the tab tuple",
                   "t_loyalty not found - wire the Loyalty tab first")
        else:
            p.sub(r"(t_loyalty)(\s*,)", r"\1, t_retention\2",
                  "add t_retention to the tab tuple")
            p.sub(r'("Loyalty"\s*,)', r'\1 "Retention",',
                  'add the "Retention" label')

    if "with t_retention:" in p.text:
        p.skip("render_retention block")
    else:
        p.append(
            "\n\n# ------------------------------------------------ retention\n"
            "with t_retention:\n"
            "    render_retention(q=q, keys=keys, stores=STORES,\n"
            "                     heading=heading, table_exists=table_exists)\n",
            "render_retention block")
    return p


def revert():
    n = 0
    for target in (PUBLISH, PUBLIC):
        baks = sorted(glob.glob(target + ".retfix-*.bak"))
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

    missing = [f for f in ("publish_retention.py", "retention_tab.py")
               if not os.path.exists(f)]
    if missing:
        print("!! missing here: %s" % ", ".join(missing))
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = False
    for p in (patch_publish(a.dry_run), patch_public(a.dry_run)):
        if p is None:
            failed = True
            continue
        print()
        print(p.path)
        for status, label, note in p.log:
            print("  %s %-38s %s"
                  % ({"OK": " + ", "SKIP": " = ", "FAIL": " ! "}[status],
                     label, note))
            if status == "FAIL":
                failed = True
        bak = p.save(stamp)
        print("  backup: %s" % bak if bak else "  no changes needed")

    print()
    if failed:
        print("Some edits could not be applied. Each file is either fully")
        print("patched or untouched. Paste this output back to adjust.")
        return 1
    if a.dry_run:
        print("Dry run. Re-run without --dry-run to apply.")
        return 0

    for f in (PUBLISH, PUBLIC):
        try:
            ast.parse(open(f, encoding="utf-8").read())
            print("  %s parses cleanly" % f)
        except SyntaxError as e:
            print("  !! %s line %s: %s" % (f, e.lineno, e.msg))
            print("     python wire_retention.py --revert")
            return 1

    print()
    print("Done. Next:")
    print("  python publish.py")
    print("  python -m streamlit run cerebral_public.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
