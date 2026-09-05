#!/usr/bin/env python3
"""Exercise generated *real view source* fixtures in Chromium.

Install Python playwright separately for this development-only check. Produce
fixtures with CAR_UI_ARTIFACT_DIR=... node scripts/verify-portable.cjs first.
These fixtures use the documented test JSX renderer, not Hono HTTP. The real
Hono route/auth/schema tests remain in `bun test` and must pass separately.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import re
from playwright.sync_api import sync_playwright

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--chromium", default=shutil.which("chromium") or shutil.which("chromium-browser"))
    args = parser.parse_args()
    root = args.fixtures.resolve()
    if not (root / "decision.html").is_file():
        parser.error("Generate CAR_UI_ARTIFACT_DIR fixtures first")
    def inline_fixture(name: str) -> str:
        # No network required: inline the identical deferred enhancement at body end.
        html = (root / name).read_text()
        html = re.sub(r'<script[^>]*src="/ui/live-refresh.js"[^>]*></script>', '', html)
        script = (root / "ui/live-refresh.js").read_text()
        return html.replace('</body>', '<script data-refresh-ms="15000">'+script+'</script></body>')
    checks: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, args=["--no-sandbox"])
        for scheme in ("light", "dark"):
            for width in (320, 390, 1440):
                context = browser.new_context(viewport={"width": width, "height": 1100}, color_scheme=scheme)
                # Capture the refresh timer; trigger it deterministically instead of sleeping.

                page = context.new_page(); errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.evaluate("window.__carTimers=[]; window.setTimeout=(fn,ms)=>{window.__carTimers.push(fn);return window.__carTimers.length}")
                page.set_content(inline_fixture("decision.html"), wait_until="load")
                assert not errors, errors
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Horizontal overflow at {width} {scheme}"
                assert page.get_by_role("heading", name="Preserve API v1 for one more release?").count() == 1
                for area in page.locator("textarea").all():
                    identifier = area.get_attribute("id")
                    assert identifier and page.locator(f'label[for="{identifier}"]').count() == 1
                assert page.locator(".option-card").count() == 2
                assert page.locator(".option-card .option-answer").first.inner_text().startswith("Preserve API v1 compatibility")
                assert page.locator(".decision-uncertainty").is_visible()
                if width < 600:
                    page.locator(".mobile-primary > summary").click()
                    assert page.get_by_role("navigation", name="Mobile primary navigation").is_visible()
                    page.locator(".mobile-primary > summary").click()
                # An input must stay on the page after blur and even after closing all disclosures.
                editor = page.locator('textarea[name="text"]')
                editor.fill("Preserve compatibility; check usage before removal.")
                page.locator("h1").click()
                page.evaluate("document.querySelectorAll('details').forEach(d=>d.open=false); window.__draftMarker=42; window.__carTimers.find(fn=>typeof fn==='function'&&fn.name==='refresh')()")
                assert editor.input_value() == "Preserve compatibility; check usage before removal."
                assert page.evaluate("window.__draftMarker") == 42
                assert page.locator("#refresh-paused").is_visible()
                page.once("dialog", lambda dialog: dialog.dismiss())
                page.get_by_role("link", name="Refresh", exact=True).click()
                assert editor.input_value().startswith("Preserve compatibility")
                page.locator(".decision-composer > summary").click()
                page.screenshot(path=str(root / f"decision-{width}-{scheme}.png"), full_page=True)
                assert not errors, errors
                checks.append(f"{width}px {scheme}: layout, labels, options, uncertainty, menu, draft after blur, refresh cancel")
                context.close()
        page = browser.new_page(viewport={"width": 390, "height": 1000})
        page.set_content(inline_fixture("expired.html"), wait_until="load")
        assert page.get_by_role("button", name="Acknowledge missed decision").is_visible()
        assert page.locator('form[action$="/answer"]').count() == 0
        assert page.get_by_text("Deadline missed · not approved", exact=True).count() == 1
        checks.append("Expired decision: review visible, no actionable stale answer")
        browser.close()
    (root / "browser-results.json").write_text(json.dumps({"fixture_renderer":"test JSX seam (not Hono routes)","checks":checks,"passed":len(checks)},indent=2)+"\n")
    print(json.dumps({"browser_checks_passed":len(checks),"fixtures":str(root)},indent=2))

if __name__ == "__main__":
    main()
