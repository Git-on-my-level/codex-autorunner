#!/usr/bin/env python3
"""Lightweight Playwright smoke for the Vite dev server (`make serve`).

This complements ``scripts/web_ui_smoke_journeys.py``, which validates the built
static bundle against a seeded disposable hub. Dev smoke catches SvelteKit client
routing regressions that only appear through the Vite proxy (for example invalid
``+page.ts`` exports that render the generic 500 page).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_VITE_URL = "http://127.0.0.1:5173"
DEFAULT_HUB_URL = "http://127.0.0.1:4173"
FAILURE_MARKERS = ("Internal Error", "Invalid export")
LOADING_MARKERS = (
    "Loading workspace state",
    "Loading tickets",
    "Loading contextspace docs",
)


@dataclass
class DevSmokeDiagnostics:
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Web Hub Vite dev-server Playwright smoke checks."
    )
    parser.add_argument("--vite-url", default=DEFAULT_VITE_URL)
    parser.add_argument("--hub-url", default=DEFAULT_HUB_URL)
    parser.add_argument("--repo-id", default="")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    return parser.parse_args()


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def _resolve_repo_id(hub_url: str, explicit_repo_id: str) -> str:
    if explicit_repo_id.strip():
        return explicit_repo_id.strip()
    payload = _fetch_json(f"{hub_url.rstrip('/')}/hub/read-models/repo-worktree/topology?limit=5")
    repos = payload.get("repos") or []
    if not repos:
        raise RuntimeError("Hub topology returned no repos; pass --repo-id explicitly.")
    repo_id = str(repos[0].get("repoId") or repos[0].get("id") or "").strip()
    if not repo_id:
        raise RuntimeError("Could not resolve repo id from hub topology.")
    return repo_id


def _wait_for_page(page, timeout_ms: float) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_function(
        """(loadingMarkers) => {
            const text = document.body?.innerText || '';
            if (!text.trim()) return false;
            if (text.includes('Internal Error')) return true;
            if (document.querySelector('[aria-busy="true"]')) return false;
            return !loadingMarkers.some((marker) => text.includes(marker));
        }""",
        arg=list(LOADING_MARKERS),
        timeout=timeout_ms,
    )
    page.wait_for_timeout(300)


def _wait_for_required_text(page, required_text: tuple[str, ...], timeout_ms: float) -> None:
    for text in required_text:
        page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms)


def _assert_route_health(page, *, route_name: str, required_text: tuple[str, ...]) -> None:
    body_text = page.locator("body").inner_text(timeout=2000)
    if not body_text.strip():
        raise AssertionError(f"{route_name}: route rendered a blank body")
    for marker in FAILURE_MARKERS:
        if marker in body_text:
            raise AssertionError(f"{route_name}: route rendered failure marker {marker!r}")
    missing = [text for text in required_text if text not in body_text]
    if missing:
        raise AssertionError(f"{route_name}: missing text: {', '.join(missing)}")
    if page.locator("main").count() < 1:
        raise AssertionError(f"{route_name}: missing main landmark")


def _goto_and_check(
    page,
    *,
    base_url: str,
    route_name: str,
    path: str,
    required_text: tuple[str, ...],
    timeout_ms: float,
) -> None:
    response = page.goto(
        f"{base_url.rstrip('/')}{path}",
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    if response is None or response.status >= 400:
        status = None if response is None else response.status
        raise AssertionError(f"{route_name}: navigation failed with status {status}")
    _wait_for_page(page, timeout_ms)
    _wait_for_required_text(page, required_text, timeout_ms)
    _assert_route_health(page, route_name=route_name, required_text=required_text)


def _install_diagnostics(page, diagnostics: DevSmokeDiagnostics) -> None:
    page.on(
        "console",
        lambda message: (
            diagnostics.console_errors.append(message.text)
            if message.type == "error"
            and "Failed to load resource:" not in message.text
            else None
        ),
    )
    page.on("pageerror", lambda error: diagnostics.page_errors.append(str(error)))


def _assert_diagnostics(diagnostics: DevSmokeDiagnostics) -> None:
    failures: list[str] = []
    failures.extend(f"console error: {text}" for text in diagnostics.console_errors)
    failures.extend(f"page error: {text}" for text in diagnostics.page_errors)
    if failures:
        raise AssertionError("\n".join(failures))


def main() -> int:
    args = parse_args()
    timeout_ms = args.timeout_seconds * 1000

    try:
        repo_id = _resolve_repo_id(args.hub_url, args.repo_id)
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        print(f"Could not resolve repo id from hub at {args.hub_url}: {exc}", file=sys.stderr)
        print("Start `make serve` first or pass --repo-id.", file=sys.stderr)
        return 1

    routes: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("repos-index", "/repos", ("Repos",)),
        ("repo-detail", f"/repos/{repo_id}", (repo_id, "Repo tickets")),
        ("repo-tickets", f"/repos/{repo_id}/tickets", ("Tickets", "New ticket")),
    )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is required. Install project dev deps and run `playwright install chromium`.", file=sys.stderr)
        return 1

    diagnostics = DevSmokeDiagnostics()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        _install_diagnostics(page, diagnostics)
        try:
            for route_name, path, required_text in routes:
                _goto_and_check(
                    page,
                    base_url=args.vite_url,
                    route_name=route_name,
                    path=path,
                    required_text=required_text,
                    timeout_ms=timeout_ms,
                )
            _assert_diagnostics(diagnostics)
        finally:
            browser.close()

    print(f"web-ui-dev-smoke ok (repo={repo_id}, vite={args.vite_url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
