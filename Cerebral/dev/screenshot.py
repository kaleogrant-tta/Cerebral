#!/usr/bin/env python3
"""
screenshot.py -- start Cerebral on the fake data, click through every top-level
section in a headless Chromium and save one PNG per section.

    python dev/screenshot.py                    # -> dev/shots/<section>.png  (1440x900)
    python dev/screenshot.py --full             # also dev/shots/full/<section>.png (whole page height)
    python dev/screenshot.py --only loyalty,events
    python dev/screenshot.py --url http://localhost:8501   # use an app that is already running
    python dev/screenshot.py --width 390 --height 844 --out dev/shots/phone   # phone-sized

Works with both layouts: a row of st.tabs, or the newer pill strip. Section names
are read from the page itself, so nothing here needs updating when tabs change.

Needs:  pip install playwright   (the browser itself is already installed at
/opt/pw-browsers/chromium -- do NOT run `playwright install`).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent
FIXTURE = HERE / "fixture" / "cerebral_dash.duckdb"
CHROMIUM_FALLBACK = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "section"


# ------------------------------------------------------------------ app process
def install_fixture() -> tuple[bool, Path | None]:
    """Copy the fixture to where the app looks first. Returns (we_installed, backup)."""
    target = APP_DIR / "cerebral_dash.duckdb"
    if not FIXTURE.exists():
        subprocess.check_call([sys.executable, str(HERE / "make_fixture.py")])
    if target.exists() and target.read_bytes() == FIXTURE.read_bytes():
        return False, None
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".real-backup")
        if not backup.exists():
            shutil.move(str(target), str(backup))
    shutil.copyfile(FIXTURE, target)
    return True, backup


def uninstall_fixture(installed: bool, backup: Path | None) -> None:
    target = APP_DIR / "cerebral_dash.duckdb"
    if installed:
        target.unlink(missing_ok=True)
        if backup and backup.exists():
            shutil.move(str(backup), str(target))


def start_app(port: int, app: str):
    cmd = [sys.executable, "-m", "streamlit", "run", app, "--server.headless", "true",
           "--server.port", str(port), "--browser.gatherUsageStats", "false",
           "--server.fileWatcherType", "none"]
    log = open(HERE / "shots" / "streamlit.log", "w")
    proc = subprocess.Popen(cmd, cwd=APP_DIR, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://localhost:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise SystemExit(f"streamlit exited early; see {HERE / 'shots' / 'streamlit.log'}")
        try:
            if urllib.request.urlopen(url + "/_stcore/health", timeout=1).status == 200:
                return proc, url
        except Exception:
            time.sleep(.5)
    proc.terminate()
    raise SystemExit("streamlit did not come up within 60s")


# ------------------------------------------------------------------ page helpers
def settle(page, extra=0.8, timeout=60):
    """Wait until Streamlit has stopped running and charts have painted."""
    end = time.time() + timeout
    time.sleep(0.4)
    while time.time() < end:
        busy = page.locator('[data-testid="stStatusWidget"], [data-testid="stSpinner"]').count()
        if not busy:
            break
        time.sleep(0.3)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    # plotly/vega draw after the element appears; wait for the count to stop changing
    last, stable = -1, 0
    while time.time() < end and stable < 3:
        n = page.locator(".js-plotly-plot .main-svg, .vega-embed canvas, .vega-embed svg").count()
        stable = stable + 1 if n == last else 0
        last = n
        time.sleep(0.25)
    time.sleep(extra)


def find_sections(page):
    """Return (mode, labels). mode is 'tabs' or 'pills'."""
    tab_lists = page.locator('[data-baseweb="tab-list"]')
    if tab_lists.count():
        first = tab_lists.first
        labels = [t.inner_text().strip() for t in first.locator('button[role="tab"]').all()]
        if len(labels) >= 4:
            return "tabs", labels
    groups = page.locator('[data-testid="stButtonGroup"]')
    for i in range(groups.count()):
        btns = groups.nth(i).locator("button")
        labels = [b.inner_text().strip() for b in btns.all()]
        if len(labels) >= 4:
            return "pills", labels
    return "none", []


def click_section(page, mode, label, idx):
    if mode == "tabs":
        page.locator('[data-baseweb="tab-list"]').first.locator('button[role="tab"]').nth(idx).click()
    else:
        for i in range(page.locator('[data-testid="stButtonGroup"]').count()):
            g = page.locator('[data-testid="stButtonGroup"]').nth(i)
            b = g.locator("button", has_text=re.compile(rf"^\s*{re.escape(label)}\s*$"))
            if b.count():
                b.first.click()
                return
        raise RuntimeError(f"pill {label!r} not found")


def page_height(page) -> int:
    return page.evaluate("""() => {
        const m = document.querySelector('[data-testid="stMain"]') ||
                  document.querySelector('section.main') ||
                  document.querySelector('[data-testid="stAppViewContainer"]');
        return m ? m.scrollHeight : document.body.scrollHeight;
    }""")


def main() -> int:
    ap = argparse.ArgumentParser(description="Screenshot every Cerebral section")
    ap.add_argument("--out", type=Path, default=HERE / "shots")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--port", type=int, default=8599)
    ap.add_argument("--url", help="use an already-running app instead of starting one")
    ap.add_argument("--app", default="cerebral_public.py", help="script name inside Cerebral/")
    ap.add_argument("--only", help="comma-separated section names or slugs")
    ap.add_argument("--full", action="store_true", help="also save a full-page-height shot in <out>/full/")
    ap.add_argument("--dark", action="store_true", help="emulate prefers-color-scheme: dark")
    ap.add_argument("--keep", action="store_true", help="leave the fixture installed in Cerebral/ afterwards")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    a.out.mkdir(parents=True, exist_ok=True)
    if a.full:
        (a.out / "full").mkdir(exist_ok=True)

    proc = installed = backup = None
    installed = False
    if not a.url:
        installed, backup = install_fixture()
        proc, url = start_app(a.port, a.app)
    else:
        url = a.url.rstrip("/")

    problems: list[str] = []
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception as exc:
                print(f"(default chromium not found: {str(exc).splitlines()[0]}; using {CHROMIUM_FALLBACK})")
                browser = pw.chromium.launch(headless=True, executable_path=CHROMIUM_FALLBACK,
                                             args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": a.width, "height": a.height},
                                      color_scheme="dark" if a.dark else "light")
            page = ctx.new_page()
            page.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
            page.on("console", lambda m: problems.append(f"console error: {m.text[:200]}")
                    if m.type == "error" and "favicon" not in m.text and "Failed to load resource" not in m.text else None)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="stApp"]', timeout=60000)
            try:
                page.wait_for_selector("h1", timeout=60000)      # the app title: data has loaded
            except Exception:
                problems.append("page never showed a title (h1) within 60s")
            settle(page, extra=1.5)

            mode, labels = "none", []
            for _ in range(40):                                  # sections appear a moment after the title
                mode, labels = find_sections(page)
                if labels:
                    break
                time.sleep(0.5)
            if not labels:
                print("No tabs or pills found; taking a single shot of the page.")
                labels, mode = ["page"], "none"
            print(f"Found {len(labels)} sections ({mode}): {', '.join(labels)}")
            only = {s.strip().lower() for s in a.only.split(",")} if a.only else None

            saved = []
            for idx, label in enumerate(labels):
                if only and label.lower() not in only and slug(label) not in only:
                    continue
                if mode != "none":
                    click_section(page, mode, label, idx)
                settle(page)
                exc = page.locator('[data-testid="stException"]').count()
                if exc:
                    msg = page.locator('[data-testid="stException"]').first.inner_text()[:300].replace("\n", " ")
                    problems.append(f"[{label}] {exc} exception box(es) on screen: {msg}")
                f = a.out / f"{slug(label)}.png"
                page.screenshot(path=str(f))
                saved.append(f)
                if a.full:
                    h = min(max(page_height(page) + 80, a.height), 16000)
                    page.set_viewport_size({"width": a.width, "height": h})
                    time.sleep(0.8)
                    page.screenshot(path=str(a.out / "full" / f"{slug(label)}.png"))
                    page.set_viewport_size({"width": a.width, "height": a.height})
                    time.sleep(0.3)
                print(f"  saved {f.relative_to(HERE.parent)}", flush=True)
            browser.close()
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if not a.keep and not a.url:
            uninstall_fixture(installed, backup)

    print(f"\n{len(saved)} screenshots in {a.out}")
    if problems:
        print("Browser-side problems:")
        for p in dict.fromkeys(problems):
            print("  -", p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
