#!/usr/bin/env python3
"""Manual PMA live hub smoke test.

Starts a disposable local hub with the packaged PMA Svelte app mounted under
``/car``, drives a real browser through primary PMA routes, and writes route
evidence plus screenshots to a deterministic evidence directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_EVIDENCE_DIR = (
    REPO_ROOT / ".codex-autorunner" / "runs" / "pma-live-hub-smoke" / "latest"
)
SMOKE_FIXTURE_MANIFEST = "smoke-fixture-manifest.json"

SMOKE_ROUTES = (
    ("/car/chats", ("Chats", "PMA Memory")),
    ("/car/repos", ("Current work", "smoke-repo")),
    ("/car/tickets", ("Cross-workspace ticket queue", "Tickets")),
    ("/car/contextspace/local", ("Documents", "active_context.md")),
    ("/car/settings", ("PMA agent/model", "Hub")),
)

PRIMARY_LOADING_MARKERS = (
    "Opening PMA...",
    "Loading PMA chats",
    "Loading active chat",
    "Loading workspace state",
    "Loading tickets",
    "Loading contextspace docs",
    "Loading settings",
)

CONTROL_PROMPT = """<CAR_TICKET_FLOW_PROMPT>
<CAR_CURRENT_TICKET_FILE>
PATH: .codex-autorunner/tickets/TICKET-350-smoke-fixture.md
---
ticket_id: "tkt_pma_live_hub_smoke_fixture"
agent: "codex"
done: false
title: "PMA live hub smoke fixture"
---
</CAR_CURRENT_TICKET_FILE>
</CAR_TICKET_FLOW_PROMPT>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual/local PMA smoke against a real uvicorn hub and browser. "
            "Requires the browser optional dependency and a Playwright browser."
        )
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help=f"Evidence output directory. Default: {DEFAULT_EVIDENCE_DIR}",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium headed for interactive local debugging.",
    )
    return parser.parse_args()


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def write_fixture_ticket(root: Path) -> None:
    tickets_dir = root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-350-smoke-fixture.md").write_text(
        """---
ticket_id: "tkt_pma_live_hub_smoke_fixture"
agent: "codex"
done: false
title: "PMA live hub smoke fixture"
repo_id: "smoke-repo"
worktree_id: "smoke-repo--review"
---
## Goal
Fixture ticket used by `scripts/pma_live_hub_smoke.py`.

## Evidence
- Exercises repo cards, worktree detail pages, ticket detail pages, and PMA artifacts.
""",
        encoding="utf-8",
    )


def seed_smoke_hub(evidence_dir: Path) -> Path:
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
    from codex_autorunner.core.config import load_hub_config
    from codex_autorunner.core.managed_thread_store import ManagedThreadStore
    from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
    from codex_autorunner.core.ports.run_event import ApprovalRequested
    from codex_autorunner.manifest import load_manifest, save_manifest

    hub_root = evidence_dir / "hub"
    if hub_root.exists():
        shutil.rmtree(hub_root)
    hub_root.mkdir(parents=True)

    seed_hub_files(hub_root, force=True)
    write_fixture_ticket(hub_root)

    repo_id = "smoke-repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    write_fixture_ticket(repo_root)

    worktree_id = "smoke-repo--review"
    worktree_root = hub_root / "worktrees" / worktree_id
    worktree_root.mkdir(parents=True)
    (worktree_root / ".git").mkdir()
    seed_repo_files(worktree_root, git_required=False)
    write_fixture_ticket(worktree_root)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    manifest.ensure_repo(
        hub_root,
        worktree_root,
        repo_id=worktree_id,
        display_name="Review worktree",
        kind="worktree",
        worktree_of=repo_id,
        branch="review/pma-screens",
    )
    save_manifest(hub_config.manifest_path, manifest, hub_root)

    store = ManagedThreadStore(hub_root)
    thread = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id=repo_id,
        resource_kind="repo",
        resource_id=repo_id,
        name="Ticket flow readability fixture",
        metadata={"ticket_id": "TICKET-350-smoke-fixture"},
    )
    turn = store.create_turn(
        str(thread["managed_thread_id"]),
        prompt="Review the attached fixture preview before continuing.",
        metadata={
            "ticket_id": "TICKET-350-smoke-fixture",
            "attachments": [
                {
                    "id": "fixture-preview-link",
                    "kind": "link",
                    "title": "Fixture preview",
                    "url": "https://example.test/pma-fixture-preview",
                }
            ],
        },
    )
    store.mark_turn_finished(str(turn["managed_turn_id"]), status="ok")
    running_thread = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id=repo_id,
        resource_kind="repo",
        resource_id=repo_id,
        name="Running queue smoke fixture",
        metadata={"ticket_id": "TICKET-350-smoke-fixture"},
    )
    running_turn = store.create_turn(
        str(running_thread["managed_thread_id"]),
        prompt="Run the queued state fixture.",
        metadata={"ticket_id": "TICKET-350-smoke-fixture"},
    )
    queued_turn = store.create_turn(
        str(running_thread["managed_thread_id"]),
        prompt="Queued follow-up from the smoke fixture.",
        busy_policy="queue",
        force_queue=True,
        metadata={"ticket_id": "TICKET-350-smoke-fixture"},
    )
    with store._write_conn() as conn:  # Keep queued timeline state without a worker.
        with conn:
            conn.execute(
                """
                DELETE FROM orch_queue_items
                 WHERE source_key = ?
                """,
                (str(queued_turn["managed_turn_id"]),),
            )
    persist_turn_timeline(
        hub_root,
        execution_id=str(running_turn["managed_turn_id"]),
        target_kind="thread_target",
        target_id=str(running_thread["managed_thread_id"]),
        events=[
            ApprovalRequested(
                timestamp="2026-05-06T10:00:04Z",
                request_id="approval-smoke-1",
                description="Smoke approval fixture",
                context={"scope": "workspace"},
            )
        ],
    )
    worktree_thread = store.create_thread(
        "codex",
        worktree_root.resolve(),
        repo_id=repo_id,
        resource_kind="worktree",
        resource_id=worktree_id,
        name="Review worktree fixture",
        metadata={"ticket_id": "TICKET-350-smoke-fixture"},
    )
    worktree_turn = store.create_turn(
        str(worktree_thread["managed_thread_id"]),
        prompt="Capture degraded-state visual QA for the review worktree.",
        metadata={
            "ticket_id": "TICKET-350-smoke-fixture",
            "artifacts": [
                {
                    "id": "fixture-error-summary",
                    "kind": "error",
                    "title": "Fixture degraded state",
                    "summary": "Synthetic stale request marker for visual QA.",
                }
            ],
        },
    )
    store.mark_turn_finished(str(worktree_turn["managed_turn_id"]), status="failed")
    (hub_root / SMOKE_FIXTURE_MANIFEST).write_text(
        json.dumps(
            {
                "repo_id": repo_id,
                "worktree_id": worktree_id,
                "ticket_id": "TICKET-350-smoke-fixture",
                "final_thread_id": str(thread["managed_thread_id"]),
                "running_thread_id": str(running_thread["managed_thread_id"]),
                "running_turn_id": str(running_turn["managed_turn_id"]),
                "queued_turn_id": str(queued_turn["managed_turn_id"]),
                "failed_thread_id": str(worktree_thread["managed_thread_id"]),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return hub_root


def server_working_directory(hub_root: Path) -> Path:
    fixture_repo = hub_root / "worktrees" / "smoke-repo"
    if (fixture_repo / ".git").exists():
        return fixture_repo
    return hub_root


def start_server(hub_root: Path, host: str, port: int):
    import uvicorn

    from codex_autorunner.server import create_hub_app

    app = create_hub_app(
        hub_root, base_path="/car", endpoint_host=host, endpoint_port=port
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="pma-smoke-hub", daemon=True)
    thread.start()
    return server, thread


def wait_for_server(base_url: str, timeout_seconds: float) -> None:
    import httpx

    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/car/health", timeout=1.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # intentional: startup can race socket binding
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(
        f"Hub did not become healthy within {timeout_seconds}s: {last_error}"
    )


def body_excerpt(text: str, limit: int = 1200) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def run_browser_smoke(
    *,
    base_url: str,
    evidence_dir: Path,
    timeout_seconds: float,
    headed: bool,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed for this Python. Run this command with the "
            "project venv or install the browser extra: `.venv/bin/python -m "
            "playwright install chromium`."
        ) from exc

    screenshots_dir = evidence_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    route_evidence: list[dict[str, Any]] = []
    request_paths: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on(
            "request",
            lambda request: request_paths.append(request.url.replace(base_url, "")),
        )
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("requestfailed", lambda request: failed_requests.append(request.url))

        for route, required_text in SMOKE_ROUTES:
            page.goto(f"{base_url}{route}", wait_until="domcontentloaded")
            page.wait_for_function(
                """(loadingMarkers) => {
                    const text = document.body?.innerText || '';
                    if (!text.trim()) return false;
                    if (text.includes('Could not load')) return true;
                    return !loadingMarkers.some((marker) => text.includes(marker));
                }""",
                arg=list(PRIMARY_LOADING_MARKERS),
                timeout=timeout_seconds * 1000,
            )
            text = page.locator("body").inner_text(timeout=timeout_seconds * 1000)
            missing = [marker for marker in required_text if marker not in text]
            loading = [marker for marker in PRIMARY_LOADING_MARKERS if marker in text]
            errors = []
            if "Could not load" in text:
                errors.append("route rendered an error state")
            if missing:
                errors.append(f"missing expected text: {', '.join(missing)}")
            if loading:
                errors.append(
                    f"still showing primary loading text: {', '.join(loading)}"
                )
            if route == "/car/chats" and "<CAR_TICKET_FLOW_PROMPT>" in text:
                errors.append("raw CAR_TICKET_FLOW_PROMPT is visible in PMA chat")

            screenshot_name = route.strip("/").replace("/", "__") + ".png"
            screenshot_path = screenshots_dir / screenshot_name
            page.screenshot(path=str(screenshot_path), full_page=True)
            route_evidence.append(
                {
                    "route": route,
                    "url": page.url,
                    "screenshot": str(screenshot_path.relative_to(evidence_dir)),
                    "required_text": list(required_text),
                    "errors": errors,
                    "excerpt": body_excerpt(text),
                }
            )

        browser.close()

    unscoped_base_path_requests = [
        path
        for path in request_paths
        if path.startswith(("/hub/", "/api/", "/_app/", "/repos/"))
    ]
    pma_tail_event_paths = [
        path
        for path in request_paths
        if path.startswith("/car/hub/pma/threads/") and path.endswith("/tail/events")
    ]
    route_evidence.append(
        {
            "route": "__browser_diagnostics__",
            "base_path_request_errors": unscoped_base_path_requests,
            "pma_tail_event_paths": pma_tail_event_paths,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "failed_requests": failed_requests,
        }
    )
    return route_evidence


def main() -> int:
    args = parse_args()
    evidence_dir = args.evidence_dir.resolve()
    if evidence_dir.exists():
        shutil.rmtree(evidence_dir)
    evidence_dir.mkdir(parents=True)

    hub_root = seed_smoke_hub(evidence_dir)
    port = args.port or find_free_port(args.host)
    base_url = f"http://{args.host}:{port}"
    original_cwd = Path.cwd()
    os.chdir(server_working_directory(hub_root))
    server, thread = start_server(hub_root, args.host, port)
    evidence: dict[str, Any] = {
        "base_url": base_url,
        "hub_root": str(hub_root),
        "routes": [],
    }
    try:
        wait_for_server(base_url, args.timeout_seconds)
        route_evidence = run_browser_smoke(
            base_url=base_url,
            evidence_dir=evidence_dir,
            timeout_seconds=args.timeout_seconds,
            headed=args.headed,
        )
        evidence["routes"] = route_evidence
        failures = [
            f"{item['route']}: {'; '.join(item['errors'])}"
            for item in route_evidence
            if item.get("errors")
        ]
        diagnostics = route_evidence[-1] if route_evidence else {}
        failures.extend(
            f"unscoped base-path request: {path}"
            for path in diagnostics.get("base_path_request_errors", [])
        )
        if not diagnostics.get("pma_tail_event_paths"):
            failures.append("PMA tail EventSource request was not observed")
        failures.extend(
            f"browser page error: {error}"
            for error in diagnostics.get("page_errors", [])
        )
        if failures:
            evidence["status"] = "failed"
            evidence["failures"] = failures
            return 1
        evidence["status"] = "passed"
        return 0
    finally:
        (evidence_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        server.should_exit = True
        thread.join(timeout=5)
        os.chdir(original_cwd)
        print(f"PMA live hub smoke evidence: {evidence_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
