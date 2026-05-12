#!/usr/bin/env python3
"""Capture Web Hub UI screenshots for development and QA.

The default mode starts a seeded disposable hub so screenshots stay useful even
when the developer machine has no configured repos. Use ``--mode hub`` to point
at a real hub root, or ``--mode url --base-url ...`` to capture an already
running server.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_OUT_DIR = (
    REPO_ROOT / ".codex-autorunner" / "render" / "web_ui_samples" / "latest"
)
DEFAULT_OUTBOX_DIR = REPO_ROOT / ".codex-autorunner" / "filebox" / "outbox"

DEFAULT_ROUTES: tuple[tuple[str, str], ...] = (
    ("chat", "/chats"),
    ("hub", "/hub"),
    ("repos", "/repos"),
    ("repo-detail", "/repos/smoke-repo"),
    ("repo-tickets", "/repos/smoke-repo/tickets"),
    ("repo-ticket-detail", "/repos/smoke-repo/tickets/TICKET-350-smoke-fixture"),
    ("worktree-detail", "/repos/smoke-repo/worktrees/smoke-repo--review"),
    (
        "worktree-contextspace",
        "/repos/smoke-repo/worktrees/smoke-repo--review/contextspace",
    ),
    (
        "worktree-tickets",
        "/repos/smoke-repo/worktrees/smoke-repo--review/tickets",
    ),
    ("tickets", "/tickets"),
    ("ticket-detail", "/tickets/TICKET-350-smoke-fixture"),
    ("worktrees", "/worktrees"),
    ("contextspace", "/contextspace/local"),
    ("settings", "/settings"),
)

DEFAULT_VIEWPORTS: tuple[tuple[int, int], ...] = ((1440, 1000), (390, 844))

PRIMARY_LOADING_MARKERS = (
    "Opening Web Hub...",
    "Loading chats",
    "Loading active chat",
    "Loading workspace state",
    "Loading tickets",
    "Loading contextspace docs",
    "Loading settings",
    "Loading models",
)


@dataclass(frozen=True)
class CaptureRoute:
    name: str
    path: str


@dataclass(frozen=True)
class BrowserConsoleMessage:
    type: str
    text: str
    location: dict[str, Any]
    url: str


@dataclass(frozen=True)
class FailedNetworkRequest:
    url: str
    method: str
    resource_type: str
    failure: str | None
    route_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a repeatable screenshot pack for the Web Hub UI."
    )
    parser.add_argument(
        "--mode",
        choices=("fixture", "hub", "url"),
        default="fixture",
        help=(
            "fixture seeds a disposable hub, hub serves --hub-root, url captures "
            "an already-running server. Default: fixture."
        ),
    )
    parser.add_argument(
        "--hub-root",
        type=Path,
        default=Path.home() / "car-workspace",
        help="Hub root used with --mode hub. Default: ~/car-workspace.",
    )
    parser.add_argument(
        "--base-url",
        help=(
            "Server origin for --mode url, for example http://127.0.0.1:4180. "
            "Do not include the /car base path unless --base-path is ''."
        ),
    )
    parser.add_argument("--base-path", default="/car", help="Mounted UI base path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Screenshot output directory. Default: {DEFAULT_OUT_DIR}",
    )
    parser.add_argument(
        "--outbox",
        action="store_true",
        help="Copy captured PNGs into the CAR filebox outbox after capture.",
    )
    parser.add_argument(
        "--outbox-dir",
        type=Path,
        default=DEFAULT_OUTBOX_DIR,
        help=f"Outbox directory used with --outbox. Default: {DEFAULT_OUTBOX_DIR}",
    )
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "Capture one route. Repeatable. If omitted, captures the default PMA "
            "route pack."
        ),
    )
    parser.add_argument(
        "--viewport",
        action="append",
        default=[],
        help=(
            "Viewport in WIDTHxHEIGHT format. Repeatable. Default: "
            "1440x1000 and 390x844."
        ),
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=1500,
        help="Extra settle delay after primary loading markers clear. Default: 1500.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Server and page readiness timeout. Default: 20.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium headed for interactive local debugging.",
    )
    parser.add_argument(
        "--viewport-only",
        action="store_true",
        help="Capture only the viewport instead of full-page screenshots.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not remove the output directory before capture.",
    )
    return parser.parse_args()


def parse_viewport(raw: str) -> tuple[int, int]:
    parts = raw.lower().split("x", 1)
    if len(parts) != 2:
        raise ValueError("viewport must use WIDTHxHEIGHT format")
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise ValueError("viewport width and height must be integers") from exc
    if width < 320 or height < 320:
        raise ValueError("viewport width and height must be at least 320")
    return width, height


def parse_viewports(raw_viewports: list[str]) -> list[tuple[int, int]]:
    if not raw_viewports:
        return list(DEFAULT_VIEWPORTS)
    return [parse_viewport(raw) for raw in raw_viewports]


def viewport_label(viewport: tuple[int, int]) -> str:
    return f"{viewport[0]}x{viewport[1]}"


def parse_routes(raw_routes: list[str]) -> list[CaptureRoute]:
    if not raw_routes:
        return [CaptureRoute(name, path) for name, path in DEFAULT_ROUTES]
    routes: list[CaptureRoute] = []
    for raw in raw_routes:
        if "=" not in raw:
            raise ValueError(f"invalid route {raw!r}; expected NAME=PATH")
        name, path = raw.split("=", 1)
        name = name.strip()
        path = path.strip()
        if not name:
            raise ValueError(f"invalid route {raw!r}; route name is empty")
        if "/" in name or "\\" in name:
            raise ValueError(f"invalid route name {name!r}; use a filename stem")
        if not path.startswith("/"):
            raise ValueError(f"invalid route path {path!r}; path must start with /")
        routes.append(CaptureRoute(name, path))
    return routes


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def start_server(hub_root: Path, host: str, port: int, base_path: str):
    import uvicorn

    from codex_autorunner.server import create_hub_app

    app = create_hub_app(
        hub_root,
        base_path=base_path,
        endpoint_host=host,
        endpoint_port=port,
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="web-ui-screens-hub", daemon=True)
    thread.start()
    return server, thread


def wait_for_server(base_url: str, base_path: str, timeout_seconds: float) -> None:
    import httpx

    deadline = time.monotonic() + timeout_seconds
    health_path = f"{base_path.rstrip('/')}/health" if base_path else "/health"
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}{health_path}", timeout=1.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # startup can race socket binding
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(
        f"Hub did not become healthy within {timeout_seconds}s: {last_error}"
    )


def seed_fixture_hub(out_dir: Path) -> Path:
    from pma_live_hub_smoke import seed_smoke_hub

    return seed_smoke_hub(out_dir)


def server_working_directory(hub_root: Path) -> Path:
    fixture_repo = hub_root / "worktrees" / "smoke-repo"
    if (fixture_repo / ".git").exists():
        return fixture_repo
    return hub_root


def normalize_base_path(raw: str) -> str:
    value = raw.strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


def route_url(base_url: str, base_path: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{base_path}{path}"


def _relative_artifact(path: Path, out_dir: Path) -> str:
    try:
        return str(path.relative_to(out_dir))
    except ValueError:
        return str(path)


def build_loading_marker_status(
    *,
    timed_out: bool,
    body_text: str,
    markers: tuple[str, ...] = PRIMARY_LOADING_MARKERS,
) -> dict[str, Any]:
    remaining = [marker for marker in markers if marker in body_text]
    if timed_out:
        state = "timeout"
    elif remaining:
        state = "present"
    else:
        state = "cleared"
    return {
        "state": state,
        "remaining_markers": remaining,
        "rendered_error_state": "Could not load" in body_text,
    }


def classify_capture_failures(record: dict[str, Any]) -> list[str]:
    failures: set[str] = set()
    navigation = record.get("navigation", {})
    if navigation.get("ok") is False:
        failures.add("navigation")
    if [
        error
        for error in record.get("console", {}).get("errors", [])
        if "Failed to load resource:" not in str(error.get("text", ""))
    ]:
        failures.add("console")
    if [
        request
        for request in record.get("network", {}).get("failed_requests", [])
        if is_actionable_failed_request(request)
    ]:
        failures.add("network")
    loading = record.get("loading_marker", {})
    if loading.get("state") in {"timeout", "present"} or loading.get(
        "rendered_error_state"
    ):
        failures.add("loading")
    layout = record.get("layout", {})
    if layout.get("has_horizontal_overflow"):
        failures.add("layout")
    screenshot = record.get("screenshot", {})
    if not screenshot.get("path") or screenshot.get("size_bytes", 0) <= 0:
        failures.add("screenshot")
    accessibility = record.get("accessibility", {})
    if accessibility.get("error"):
        failures.add("accessibility capture")
    return sorted(failures)


def is_actionable_failed_request(request: dict[str, Any]) -> bool:
    return request.get("failure") != "net::ERR_ABORTED"


def build_manifest(
    *,
    mode: str,
    base_url: str,
    base_path: str,
    hub_root: Path | None,
    out_dir: Path,
    viewports: list[tuple[int, int]],
    full_page: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "generated_at_unix": time.time(),
        "mode": mode,
        "base_url": base_url,
        "base_path": base_path,
        "hub_root": str(hub_root) if hub_root is not None else None,
        "out_dir": str(out_dir),
        "viewports": [
            {"width": viewport[0], "height": viewport[1]} for viewport in viewports
        ],
        "full_page": full_page,
        "captures": [],
        "diagnostics": {
            "console_errors": [],
            "console_warnings": [],
            "page_errors": [],
            "failed_requests": [],
        },
        "outbox": [],
    }


def summarize_dom(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const bodyText = document.body?.innerText || '';
            const visibleText = bodyText
                .split(/\\n+/)
                .map((line) => line.trim())
                .filter(Boolean)
                .slice(0, 80);
            const collect = (selector) => Array.from(document.querySelectorAll(selector))
                .map((node) => node.innerText?.trim() || node.getAttribute('aria-label') || '')
                .filter(Boolean)
                .slice(0, 40);
            return {
                title: document.title,
                url: window.location.href,
                body_text_chars: bodyText.length,
                visible_text: visibleText,
                headings: collect('h1,h2,h3,[role="heading"]'),
                buttons: collect('button,[role="button"]'),
                links: Array.from(document.querySelectorAll('a[href]'))
                    .map((node) => ({
                        text: node.innerText?.trim() || node.getAttribute('aria-label') || '',
                        href: node.getAttribute('href'),
                    }))
                    .filter((item) => item.text || item.href)
                    .slice(0, 40),
                counts: {
                    elements: document.querySelectorAll('*').length,
                    buttons: document.querySelectorAll('button,[role="button"]').length,
                    links: document.querySelectorAll('a[href]').length,
                    inputs: document.querySelectorAll('input,textarea,select').length,
                },
            };
        }"""
    )


def capture_accessibility_snapshot(page) -> tuple[dict[str, Any] | None, str | None]:
    try:
        accessibility = getattr(page, "accessibility", None)
        if accessibility is not None:
            snapshot = accessibility.snapshot()
            return snapshot, None
    except Exception as exc:
        return None, str(exc)
    try:
        session = page.context.new_cdp_session(page)
        return session.send("Accessibility.getFullAXTree"), None
    except Exception as exc:
        return None, str(exc)


def capture_layout_diagnostics(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const root = document.documentElement;
            const viewportWidth = root.clientWidth;
            const viewportHeight = root.clientHeight;
            const describe = (node, rect) => ({
                tag: node.tagName.toLowerCase(),
                id: node.id || '',
                class_name: String(node.className || '').slice(0, 120),
                text: (node.innerText || node.getAttribute('aria-label') || '').trim().slice(0, 160),
                rect: {
                    left: Math.round(rect.left),
                    right: Math.round(rect.right),
                    top: Math.round(rect.top),
                    bottom: Math.round(rect.bottom),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
            });
            const overflowing = [];
            const clipped = [];
            for (const node of document.querySelectorAll('body *')) {
                const rect = node.getBoundingClientRect();
                if (!rect.width || !rect.height) continue;
                if (rect.right > viewportWidth + 1 || rect.left < -1) {
                    overflowing.push(describe(node, rect));
                }
                const style = window.getComputedStyle(node);
                const clipsText = (
                    (style.overflowX === 'hidden' || style.textOverflow === 'ellipsis') &&
                    node.scrollWidth > node.clientWidth + 1
                );
                const clipsY = style.overflowY === 'hidden' && node.scrollHeight > node.clientHeight + 1;
                if (clipsText || clipsY) {
                    clipped.push({ ...describe(node, rect), reason: clipsText ? 'text-x' : 'content-y' });
                }
            }
            return {
                viewport: { width: viewportWidth, height: viewportHeight },
                document: {
                    scroll_width: root.scrollWidth,
                    client_width: root.clientWidth,
                    scroll_height: root.scrollHeight,
                    client_height: root.clientHeight,
                },
                has_horizontal_overflow: root.scrollWidth > root.clientWidth + 1,
                overflowing_elements: overflowing.slice(0, 25),
                clipped_elements: clipped.slice(0, 25),
            };
        }"""
    )


def capture_screenshots(
    *,
    base_url: str,
    base_path: str,
    out_dir: Path,
    routes: list[CaptureRoute],
    viewport: tuple[int, int],
    wait_ms: int,
    timeout_seconds: float,
    headed: bool,
    full_page: bool,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed for this Python. Use the project venv "
            "or install the browser extra, then run `.venv/bin/python -m "
            "playwright install chromium` if Chromium is missing."
        ) from exc

    captures: list[dict[str, Any]] = []
    console_messages: list[BrowserConsoleMessage] = []
    page_errors: list[str] = []
    failed_requests: list[FailedNetworkRequest] = []
    screenshots_dir = out_dir / viewport_label(viewport)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(
            viewport={"width": viewport[0], "height": viewport[1]},
            device_scale_factor=1,
        )
        current_route_url = {"value": ""}

        def _record_console(message) -> None:
            if message.type not in {"error", "warning"}:
                return
            console_messages.append(
                BrowserConsoleMessage(
                    type=message.type,
                    text=message.text,
                    location=message.location,
                    url=current_route_url["value"],
                )
            )

        def _record_failed_request(request) -> None:
            failed_requests.append(
                FailedNetworkRequest(
                    url=request.url,
                    method=request.method,
                    resource_type=request.resource_type,
                    failure=request.failure,
                    route_url=current_route_url["value"],
                )
            )

        page.on("console", _record_console)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("requestfailed", _record_failed_request)

        for item in routes:
            started_at = time.monotonic()
            url = route_url(base_url, base_path, item.path)
            current_route_url["value"] = url
            console_start = len(console_messages)
            failed_request_start = len(failed_requests)
            screenshot_path = screenshots_dir / f"{item.name}.png"
            dom_summary_path = screenshots_dir / f"{item.name}.dom_summary.json"
            accessibility_path = screenshots_dir / f"{item.name}.a11y_snapshot.json"
            layout_path = screenshots_dir / f"{item.name}.layout_diagnostics.json"
            final_url = url
            navigation: dict[str, Any] = {"ok": True, "status": None, "error": None}
            body_text = ""
            loading_timed_out = False
            screenshot_error: str | None = None
            accessibility_error: str | None = None
            print(
                f"capturing {viewport_label(viewport)} {item.name}: {url}",
                flush=True,
            )
            try:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000
                )
                if response is None:
                    navigation = {
                        "ok": False,
                        "status": None,
                        "error": "route did not return a browser navigation response",
                    }
                elif response.status >= 400:
                    navigation = {
                        "ok": False,
                        "status": response.status,
                        "error": f"route returned HTTP {response.status}",
                    }
                else:
                    navigation["status"] = response.status
            except Exception as exc:
                navigation = {"ok": False, "status": None, "error": str(exc)}
            try:
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
            except PlaywrightTimeoutError:
                loading_timed_out = True
            page.wait_for_timeout(wait_ms)
            try:
                body_text = page.locator("body").inner_text(
                    timeout=timeout_seconds * 1000
                )
            except Exception as exc:
                body_text = ""
                navigation = {
                    "ok": False,
                    "status": navigation.get("status"),
                    "error": str(exc),
                }
            final_url = page.url
            dom_summary = summarize_dom(page)
            dom_summary_path.write_text(
                json.dumps(dom_summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            accessibility_snapshot, accessibility_error = (
                capture_accessibility_snapshot(page)
            )
            if accessibility_snapshot is not None:
                accessibility_path.write_text(
                    json.dumps(accessibility_snapshot, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            layout_diagnostics = capture_layout_diagnostics(page)
            layout_path.write_text(
                json.dumps(layout_diagnostics, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                page.screenshot(path=str(screenshot_path), full_page=full_page)
            except Exception as exc:
                screenshot_error = str(exc)
            route_console = console_messages[console_start:]
            route_failed_requests = failed_requests[failed_request_start:]
            loading_marker = build_loading_marker_status(
                timed_out=loading_timed_out,
                body_text=body_text,
            )
            capture = {
                "name": item.name,
                "path": item.path,
                "url": url,
                "final_url": final_url,
                "viewport": {"width": viewport[0], "height": viewport[1]},
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "navigation": navigation,
                "loading_marker": loading_marker,
                "screenshot": {
                    "path": str(screenshot_path),
                    "relative_path": _relative_artifact(screenshot_path, out_dir),
                    "size_bytes": (
                        screenshot_path.stat().st_size
                        if screenshot_path.exists()
                        else 0
                    ),
                    "error": screenshot_error,
                },
                "accessibility": {
                    "path": (
                        str(accessibility_path) if accessibility_path.exists() else None
                    ),
                    "relative_path": (
                        _relative_artifact(accessibility_path, out_dir)
                        if accessibility_path.exists()
                        else None
                    ),
                    "error": accessibility_error,
                },
                "dom_summary": {
                    "path": str(dom_summary_path),
                    "relative_path": _relative_artifact(dom_summary_path, out_dir),
                },
                "console": {
                    "errors": [
                        message.__dict__
                        for message in route_console
                        if message.type == "error"
                    ],
                    "warnings": [
                        message.__dict__
                        for message in route_console
                        if message.type == "warning"
                    ],
                },
                "network": {
                    "failed_requests": [
                        request.__dict__ for request in route_failed_requests
                    ],
                },
                "layout": {
                    "path": str(layout_path),
                    "relative_path": _relative_artifact(layout_path, out_dir),
                    "has_horizontal_overflow": layout_diagnostics.get(
                        "has_horizontal_overflow", False
                    ),
                    "overflowing_elements": layout_diagnostics.get(
                        "overflowing_elements", []
                    ),
                    "clipped_elements": layout_diagnostics.get("clipped_elements", []),
                },
            }
            capture["failure_subsystems"] = classify_capture_failures(capture)
            capture["errors"] = capture["failure_subsystems"]
            captures.append(capture)

        browser.close()

    captures.append(
        {
            "name": "__browser_diagnostics__",
            "console_errors": [
                message.__dict__
                for message in console_messages
                if message.type == "error"
            ],
            "console_warnings": [
                message.__dict__
                for message in console_messages
                if message.type == "warning"
            ],
            "page_errors": page_errors,
            "failed_requests": [request.__dict__ for request in failed_requests],
        }
    )
    return captures


def copy_to_outbox(captures: list[dict[str, Any]], outbox_dir: Path) -> list[str]:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for capture in captures:
        screenshot_record = capture.get("screenshot")
        screenshot = (
            screenshot_record.get("path")
            if isinstance(screenshot_record, dict)
            else screenshot_record
        )
        if not screenshot:
            continue
        source = Path(str(screenshot))
        if source.suffix.lower() != ".png" or not source.is_file():
            continue
        viewport = source.parent.name
        destination_name = (
            f"{viewport}-{source.name}" if "x" in viewport else source.name
        )
        destination = outbox_dir / destination_name
        shutil.copy2(source, destination)
        copied.append(str(destination))
    return copied


def main() -> int:
    args = parse_args()
    try:
        viewports = parse_viewports(args.viewport)
        routes = parse_routes(args.route)
    except ValueError as exc:
        print(f"web-ui-screens: {exc}", file=sys.stderr)
        return 2

    out_dir = args.out_dir.resolve()
    outbox_dir = args.outbox_dir.expanduser().resolve()
    if out_dir.exists() and not args.no_clean:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_path = normalize_base_path(args.base_path)
    server = None
    thread = None
    hub_root: Path | None = None
    original_cwd: Path | None = None
    if args.mode == "url":
        if not args.base_url:
            print(
                "web-ui-screens: --base-url is required with --mode url",
                file=sys.stderr,
            )
            return 2
        base_url = args.base_url.rstrip("/")
    else:
        if args.mode == "fixture":
            hub_root = seed_fixture_hub(out_dir / "fixture")
        else:
            hub_root = args.hub_root.expanduser().resolve()
            if not hub_root.exists():
                print(
                    f"web-ui-screens: hub root does not exist: {hub_root}",
                    file=sys.stderr,
                )
                return 2
        port = args.port or find_free_port(args.host)
        base_url = f"http://{args.host}:{port}"
        original_cwd = Path.cwd()
        os.chdir(server_working_directory(hub_root))
        server, thread = start_server(hub_root, args.host, port, base_path or "/")

    evidence = build_manifest(
        mode=args.mode,
        base_url=base_url,
        base_path=base_path,
        hub_root=hub_root,
        out_dir=out_dir,
        viewports=viewports,
        full_page=not args.viewport_only,
    )
    status = 0
    try:
        if args.mode != "url":
            wait_for_server(base_url, base_path, args.timeout_seconds)
        captures: list[dict[str, Any]] = []
        diagnostics = evidence["diagnostics"]
        for viewport in viewports:
            viewport_captures = capture_screenshots(
                base_url=base_url,
                base_path=base_path,
                out_dir=out_dir,
                routes=routes,
                viewport=viewport,
                wait_ms=args.wait_ms,
                timeout_seconds=args.timeout_seconds,
                headed=args.headed,
                full_page=not args.viewport_only,
            )
            viewport_diagnostics = viewport_captures[-1] if viewport_captures else {}
            diagnostics["console_errors"].extend(
                viewport_diagnostics.get("console_errors", [])
            )
            diagnostics["console_warnings"].extend(
                viewport_diagnostics.get("console_warnings", [])
            )
            diagnostics["page_errors"].extend(
                viewport_diagnostics.get("page_errors", [])
            )
            diagnostics["failed_requests"].extend(
                viewport_diagnostics.get("failed_requests", [])
            )
            captures.extend(
                capture
                for capture in viewport_captures
                if capture.get("name") != "__browser_diagnostics__"
            )
        evidence["captures"] = captures
        failures = [
            (
                f"{capture['name']} {capture['viewport']['width']}x"
                f"{capture['viewport']['height']}: "
                f"{', '.join(capture['failure_subsystems'])}"
            )
            for capture in captures
            if capture.get("failure_subsystems")
        ]
        failures.extend(
            f"browser page error: {error}"
            for error in diagnostics.get("page_errors", [])
        )
        failures.extend(
            f"browser failed request: {request}"
            for request in diagnostics.get("failed_requests", [])
            if is_actionable_failed_request(request)
        )
        if failures:
            evidence["status"] = "failed"
            evidence["failures"] = failures
            status = 1
        else:
            evidence["status"] = "passed"
        if args.outbox:
            copied = copy_to_outbox(captures, outbox_dir)
            evidence["outbox"] = copied
    finally:
        manifest_text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        (out_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
        (out_dir / "latest.json").write_text(manifest_text, encoding="utf-8")
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if original_cwd is not None:
            os.chdir(original_cwd)

    print(f"Web UI screenshots: {out_dir}")
    if evidence["outbox"]:
        print(f"Outboxed {len(evidence['outbox'])} screenshot(s).")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
