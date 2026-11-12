"""Capture dashboard screenshots (and fail on any rendered exception).

    streamlit run streamlit_app.py &   # then:
    python scripts/screenshot_dashboard.py --out docs/images
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

PAGES = {"overview": "", "discovery": "discovery", "topic": "topic", "regional": "regional",
         "channels": "channels", "videos": "videos", "control_center": "control-center"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8501")
    ap.add_argument("--out", default="docs/images")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=args.scale)
        for name, path in PAGES.items():
            page.goto(f"{args.base}/{path}", wait_until="networkidle")
            page.wait_for_timeout(7000)
            errors = page.locator("[data-testid='stException']").all_inner_texts()
            if errors:
                failures += 1
                print(f"{name}: EXCEPTION {errors[0][:500]}")
            page.screenshot(path=f"{args.out}/{name}.png")
            print(f"{name}: ok")
        browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
