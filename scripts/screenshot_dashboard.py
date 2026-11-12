"""Capture dashboard screenshots (and fail on any rendered exception).

    streamlit run streamlit_app.py &   # then:
    python scripts/screenshot_dashboard.py --out docs/images
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

PAGES = {"platform_overview": "", "pipeline_orchestrator": "orchestrator", "data_lakehouse": "lakehouse",
         "data_quality": "quality", "lineage_observability": "lineage", "execution_history": "executions",
         "trend_intelligence": "intelligence"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8501")
    ap.add_argument("--out", default="docs/images")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--full", action="store_true", help="capture the full scrollable page")
    ap.add_argument("--extra", action="append", default=[],
                    help="additional captures as name=path?query, e.g. orchestrator_failed=orchestrator?run=<id>")
    args = ap.parse_args()
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=args.scale)
        pages = dict(PAGES)
        pages.update(dict(e.split("=", 1) for e in args.extra))
        for name, path in pages.items():
            page.goto(f"{args.base}/{path}", wait_until="networkidle")
            page.wait_for_timeout(7000)
            errors = page.locator("[data-testid='stException']").all_inner_texts()
            if errors:
                failures += 1
                print(f"{name}: EXCEPTION {errors[0][:500]}")
            page.screenshot(path=f"{args.out}/{name}.png", full_page=args.full)
            print(f"{name}: ok")
        browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
