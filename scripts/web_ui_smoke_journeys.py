#!/usr/bin/env python3
"""Lightweight Playwright smoke journeys for the Web Hub.

This is the top of the Web UI test pyramid: it proves a few critical browser
interactions against seeded disposable hub state. Broad route and layout
diagnostics stay in ``scripts/web_ui_screens.py`` and fast scenario contracts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
SRC_ROOT = REPO_ROOT / "src"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pma_live_hub_smoke import (  # noqa: E402
    PRIMARY_LOADING_MARKERS,
    SMOKE_FIXTURE_MANIFEST,
    find_free_port,
    seed_smoke_hub,
    server_working_directory,
    start_server,
    wait_for_server,
)

DEFAULT_OUT_DIR = REPO_ROOT / ".codex-autorunner" / "render" / "web_ui_smoke" / "latest"
SMOKE_LOADING_MARKERS = tuple(
    dict.fromkeys(
        (
            *PRIMARY_LOADING_MARKERS,
            "Opening Web Hub...",
            "Loading chats",
            "Loading active chat",
            "Loading workspace state",
            "Loading tickets",
            "Loading contextspace docs",
            "Loading settings",
            "Loading models",
        )
    )
)
SMOKE_ROUTES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("hub", "/car/hub", ("Chats",)),
    ("repos", "/car/repos", ("Repos", "smoke-repo")),
    ("repo-detail", "/car/repos/smoke-repo", ("smoke-repo", "Repo tickets")),
    (
        "worktree-detail",
        "/car/repos/smoke-repo/worktrees/smoke-repo--review",
        ("Review worktree", "Worktree tickets"),
    ),
    (
        "ticket-detail",
        "/car/repos/smoke-repo/tickets/TICKET-350-smoke-fixture",
        ("Goal", "Open chat"),
    ),
    (
        "contextspace",
        "/car/repos/smoke-repo/worktrees/smoke-repo--review/contextspace",
        ("Contextspace", "active_context.md"),
    ),
)


@dataclass
class BrowserDiagnostics:
    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    bad_api_responses: list[dict[str, Any]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run seeded Web Hub Playwright smoke journeys."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def _install_browser_diagnostics(
    page, diagnostics: BrowserDiagnostics, base_url: str
) -> None:
    def _route_path(url: str) -> str:
        return url.replace(base_url, "")

    page.on(
        "console",
        lambda message: (
            diagnostics.console_errors.append(
                {
                    "type": message.type,
                    "text": message.text,
                    "location": message.location,
                    "url": page.url,
                }
            )
            if message.type == "error"
            and "Failed to load resource:" not in message.text
            else None
        ),
    )
    page.on("pageerror", lambda error: diagnostics.page_errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: (
            diagnostics.failed_requests.append(
                {
                    "url": _route_path(request.url),
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": request.failure,
                    "page_url": page.url,
                }
            )
            if request.failure != "net::ERR_ABORTED"
            else None
        ),
    )
    page.on(
        "response",
        lambda response: (
            diagnostics.bad_api_responses.append(
                {
                    "url": _route_path(response.url),
                    "status": response.status,
                    "page_url": page.url,
                }
            )
            if response.status >= 500
            and any(part in response.url for part in ("/hub/", "/api/", "/repos/"))
            else None
        ),
    )


def _wait_for_hub_page(page, timeout_ms: float) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_function(
        """(loadingMarkers) => {
            const text = document.body?.innerText || '';
            if (!text.trim()) return false;
            if (text.includes('Could not load')) return true;
            return !loadingMarkers.some((marker) => text.includes(marker));
        }""",
        arg=list(SMOKE_LOADING_MARKERS),
        timeout=timeout_ms,
    )
    page.wait_for_timeout(300)


def _assert_page_health(
    page, *, route_name: str, required_text: tuple[str, ...]
) -> None:
    body_text = page.locator("body").inner_text(timeout=2000)
    if not body_text.strip():
        raise AssertionError(f"{route_name}: route rendered a blank body")
    if "Could not load" in body_text:
        raise AssertionError(f"{route_name}: route rendered an error state")
    remaining_loading = [
        marker for marker in SMOKE_LOADING_MARKERS if marker in body_text
    ]
    if remaining_loading:
        raise AssertionError(
            f"{route_name}: persistent loading marker(s): {', '.join(remaining_loading)}"
        )
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
) -> dict[str, Any]:
    response = page.goto(
        f"{base_url}{path}", wait_until="domcontentloaded", timeout=timeout_ms
    )
    if response is None or response.status >= 400:
        status = None if response is None else response.status
        raise AssertionError(f"{route_name}: navigation failed with status {status}")
    _wait_for_hub_page(page, timeout_ms)
    _assert_page_health(page, route_name=route_name, required_text=required_text)
    return {
        "route": route_name,
        "path": path,
        "final_url": page.url,
        "status": response.status,
    }


def _click_nav_and_check(
    page, *, base_url: str, label: str, expected_path: str, timeout_ms: float
) -> None:
    page.get_by_role("link", name=label).first.click(timeout=timeout_ms)
    page.wait_for_url(f"{base_url}{expected_path}", timeout=timeout_ms)
    _wait_for_hub_page(page, timeout_ms)


def _run_journeys(
    page, *, base_url: str, fixture: dict[str, Any], timeout_ms: float
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    evidence.append(
        _goto_and_check(
            page,
            base_url=base_url,
            route_name="home-chat",
            path="/car/chats",
            required_text=("Chats",),
            timeout_ms=timeout_ms,
        )
    )
    _click_nav_and_check(
        page,
        base_url=base_url,
        label="Repos",
        expected_path="/car/repos",
        timeout_ms=timeout_ms,
    )
    _assert_page_health(
        page, route_name="nav-repos", required_text=("Repos", "smoke-repo")
    )
    _click_nav_and_check(
        page,
        base_url=base_url,
        label="Settings",
        expected_path="/car/settings",
        timeout_ms=timeout_ms,
    )
    _assert_page_health(page, route_name="nav-settings", required_text=("Settings",))
    _click_nav_and_check(
        page,
        base_url=base_url,
        label="Chats",
        expected_path="/car/chats",
        timeout_ms=timeout_ms,
    )

    for route_name, path, required_text in SMOKE_ROUTES:
        evidence.append(
            _goto_and_check(
                page,
                base_url=base_url,
                route_name=route_name,
                path=path,
                required_text=required_text,
                timeout_ms=timeout_ms,
            )
        )

    running_thread_id = fixture["running_thread_id"]
    evidence.append(
        _goto_and_check(
            page,
            base_url=base_url,
            route_name="running-chat",
            path=f"/car/chats/{running_thread_id}",
            required_text=(
                "Running queue smoke fixture",
                "Queued follow-up from the smoke fixture.",
                "waiting",
            ),
            timeout_ms=timeout_ms,
        )
    )
    page.locator("textarea[aria-label^='Message']").fill(
        "Smoke fixture follow-up.",
        timeout=timeout_ms,
    )
    page.get_by_role("button", name="Send").wait_for(timeout=timeout_ms)

    final_thread_id = fixture["final_thread_id"]
    evidence.append(
        _goto_and_check(
            page,
            base_url=base_url,
            route_name="final-chat",
            path=f"/car/chats/{final_thread_id}",
            required_text=(
                "Ticket flow readability fixture",
                "Review the attached fixture preview before continuing.",
            ),
            timeout_ms=timeout_ms,
        )
    )
    return evidence


def _failure_messages(diagnostics: BrowserDiagnostics) -> list[str]:
    failures: list[str] = []
    failures.extend(
        f"console error: {item['text']}" for item in diagnostics.console_errors
    )
    failures.extend(
        f"page error: {error}"
        for error in diagnostics.page_errors
        if "Failed to execute 'structuredClone'" not in error
    )
    failures.extend(
        f"failed request: {item['method']} {item['url']} ({item['failure']})"
        for item in diagnostics.failed_requests
    )
    failures.extend(
        f"API response {item['status']}: {item['url']}"
        for item in diagnostics.bad_api_responses
    )
    return failures


def run_smoke(
    *,
    base_url: str,
    out_dir: Path,
    fixture: dict[str, Any],
    timeout_seconds: float,
    headed: bool,
) -> tuple[
    str,
    list[dict[str, Any]],
    BrowserDiagnostics,
    dict[str, str | None],
    list[str],
]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed for this Python. Install the browser "
            "extra and run `.venv/bin/python -m playwright install chromium`."
        ) from exc

    artifacts_dir = out_dir / "artifacts"
    video_dir = artifacts_dir / "video"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = BrowserDiagnostics()
    artifact_paths: dict[str, str | None] = {
        "trace": None,
        "screenshot": None,
        "video": None,
    }
    evidence: list[dict[str, Any]] = []
    status = "passed"
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1440, "height": 960},
            record_video_dir=str(video_dir),
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        _install_browser_diagnostics(page, diagnostics, base_url)
        try:
            evidence = _run_journeys(
                page,
                base_url=base_url,
                fixture=fixture,
                timeout_ms=timeout_seconds * 1000,
            )
            failures = _failure_messages(diagnostics)
            if failures:
                raise AssertionError("; ".join(failures))
        except BaseException as exc:
            status = "failed"
            failures.append(str(exc))
            screenshot_path = artifacts_dir / "failure.png"
            trace_path = artifacts_dir / "trace.zip"
            page.screenshot(path=str(screenshot_path), full_page=True)
            context.tracing.stop(path=str(trace_path))
            artifact_paths["screenshot"] = str(screenshot_path)
            artifact_paths["trace"] = str(trace_path)
        else:
            context.tracing.stop()
        finally:
            page.close()
            context.close()
            browser.close()

    videos = sorted(video_dir.glob("*.webm"))
    if status == "failed" and videos:
        artifact_paths["video"] = str(videos[0])
    elif status == "passed" and video_dir.exists():
        shutil.rmtree(video_dir)
    return status, evidence, diagnostics, artifact_paths, failures


def main() -> int:
    args = parse_args()
    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    hub_root = seed_smoke_hub(out_dir / "fixture")
    fixture = json.loads(
        (hub_root / SMOKE_FIXTURE_MANIFEST).read_text(encoding="utf-8")
    )
    port = args.port or find_free_port(args.host)
    base_url = f"http://{args.host}:{port}"
    original_cwd = Path.cwd()
    server = None
    thread: threading.Thread | None = None
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "base_url": base_url,
        "hub_root": str(hub_root),
        "status": "failed",
        "journeys": [],
        "diagnostics": {},
        "artifacts": {},
    }
    status = 1
    try:
        os.chdir(server_working_directory(hub_root))
        server, thread = start_server(hub_root, args.host, port)
        wait_for_server(base_url, args.timeout_seconds)
        run_status, journeys, diagnostics, artifacts, failures = run_smoke(
            base_url=base_url,
            out_dir=out_dir,
            fixture=fixture,
            timeout_seconds=args.timeout_seconds,
            headed=args.headed,
        )
        manifest.update(
            {
                "status": run_status,
                "journeys": journeys,
                "diagnostics": {
                    "console_errors": diagnostics.console_errors,
                    "page_errors": diagnostics.page_errors,
                    "failed_requests": diagnostics.failed_requests,
                    "bad_api_responses": diagnostics.bad_api_responses,
                },
                "artifacts": artifacts,
            }
        )
        if failures:
            manifest["failures"] = failures
        if run_status == "passed":
            status = 0
        else:
            status = 1
            if artifacts.get("trace"):
                manifest["failure_hint"] = (
                    f"Open Playwright trace: {artifacts['trace']}"
                )
            else:
                manifest["failure_hint"] = (
                    f"Inspect smoke manifest: {out_dir / 'manifest.json'}"
                )
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["failures"] = [str(exc)]
        artifact_hint = manifest.get("artifacts") or {}
        if isinstance(artifact_hint, dict) and artifact_hint.get("trace"):
            manifest["failure_hint"] = (
                f"Open Playwright trace: {artifact_hint['trace']}"
            )
        else:
            manifest["failure_hint"] = (
                f"Inspect smoke manifest: {out_dir / 'manifest.json'}"
            )
        status = 1
    finally:
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        os.chdir(original_cwd)

    print(f"Web UI smoke manifest: {out_dir / 'manifest.json'}")
    if manifest["status"] != "passed":
        print(
            manifest.get("failure_hint", "Inspect smoke manifest for failure details.")
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
