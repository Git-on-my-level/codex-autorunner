#!/usr/bin/env python3
"""Capture PMA Hub UI screenshots for development and QA.

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
    REPO_ROOT / ".codex-autorunner" / "render" / "pma_ui_samples" / "latest"
)
DEFAULT_OUTBOX_DIR = REPO_ROOT / ".codex-autorunner" / "filebox" / "outbox"

DEFAULT_ROUTES: tuple[tuple[str, str], ...] = (
    ("pma-chat", "/pma"),
    ("dashboard", "/dashboard"),
    ("repos", "/repos"),
    ("repo-detail", "/repos/smoke-repo"),
    ("worktree-detail", "/worktrees/smoke-repo--review"),
    ("tickets", "/tickets"),
    ("ticket-detail", "/tickets/350"),
    ("worktrees", "/worktrees"),
    ("contextspace", "/contextspace/local"),
    ("settings", "/settings"),
)

DEFAULT_VIEWPORTS: tuple[tuple[int, int], ...] = ((1440, 1000), (390, 844))

PRIMARY_LOADING_MARKERS = (
    "Opening PMA...",
    "Loading PMA chats",
    "Loading active chat",
    "Loading workspace state",
    "Loading tickets",
    "Loading contextspace docs",
    "Loading settings",
    "Loading dashboard",
    "Loading models",
)


@dataclass(frozen=True)
class CaptureRoute:
    name: str
    path: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a repeatable screenshot pack for the PMA Hub UI."
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
    thread = threading.Thread(target=server.run, name="pma-ui-screens-hub", daemon=True)
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
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    screenshots_dir = out_dir / viewport_label(viewport)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(
            viewport={"width": viewport[0], "height": viewport[1]},
            device_scale_factor=1,
        )
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("requestfailed", lambda request: failed_requests.append(request.url))

        for item in routes:
            url = route_url(base_url, base_path, item.path)
            screenshot_path = screenshots_dir / f"{item.name}.png"
            errors: list[str] = []
            print(
                f"capturing {viewport_label(viewport)} {item.name}: {url}",
                flush=True,
            )
            page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000
            )
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
                errors.append("primary loading markers did not clear before timeout")
            page.wait_for_timeout(wait_ms)
            text = page.locator("body").inner_text(timeout=timeout_seconds * 1000)
            if "Could not load" in text:
                errors.append("route rendered an error state")
            page.screenshot(path=str(screenshot_path), full_page=full_page)
            captures.append(
                {
                    "name": item.name,
                    "path": item.path,
                    "url": url,
                    "viewport": {"width": viewport[0], "height": viewport[1]},
                    "screenshot": str(screenshot_path),
                    "size_bytes": screenshot_path.stat().st_size,
                    "errors": errors,
                }
            )

        browser.close()

    captures.append(
        {
            "name": "__browser_diagnostics__",
            "console_errors": console_errors,
            "page_errors": page_errors,
            "failed_requests": failed_requests,
        }
    )
    return captures


def copy_to_outbox(captures: list[dict[str, Any]], outbox_dir: Path) -> list[str]:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for capture in captures:
        screenshot = capture.get("screenshot")
        if not screenshot:
            continue
        source = Path(str(screenshot))
        if source.suffix.lower() != ".png" or not source.is_file():
            continue
        destination = outbox_dir / source.name
        shutil.copy2(source, destination)
        copied.append(str(destination))
    return copied


def main() -> int:
    args = parse_args()
    try:
        viewports = parse_viewports(args.viewport)
        routes = parse_routes(args.route)
    except ValueError as exc:
        print(f"pma-ui-screens: {exc}", file=sys.stderr)
        return 2

    out_dir = args.out_dir.resolve()
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
                "pma-ui-screens: --base-url is required with --mode url",
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
                    f"pma-ui-screens: hub root does not exist: {hub_root}",
                    file=sys.stderr,
                )
                return 2
        port = args.port or find_free_port(args.host)
        base_url = f"http://{args.host}:{port}"
        original_cwd = Path.cwd()
        os.chdir(server_working_directory(hub_root))
        server, thread = start_server(hub_root, args.host, port, base_path or "/")

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "mode": args.mode,
        "base_url": base_url,
        "base_path": base_path,
        "hub_root": str(hub_root) if hub_root is not None else None,
        "out_dir": str(out_dir),
        "viewports": [
            {"width": viewport[0], "height": viewport[1]} for viewport in viewports
        ],
        "full_page": not args.viewport_only,
        "captures": [],
        "diagnostics": {
            "console_errors": [],
            "page_errors": [],
            "failed_requests": [],
        },
        "outbox": [],
    }
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
            diagnostics["page_errors"].extend(viewport_diagnostics.get("page_errors", []))
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
            f"{capture['name']}: {'; '.join(capture['errors'])}"
            for capture in captures
            if capture.get("errors")
        ]
        failures.extend(
            f"browser page error: {error}"
            for error in diagnostics.get("page_errors", [])
        )
        failures.extend(
            f"browser failed request: {url}"
            for url in diagnostics.get("failed_requests", [])
        )
        if failures:
            evidence["status"] = "failed"
            evidence["failures"] = failures
            status = 1
        else:
            evidence["status"] = "passed"
        if args.outbox:
            copied = copy_to_outbox(captures, args.outbox_dir.resolve())
            evidence["outbox"] = copied
    finally:
        (out_dir / "manifest.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if original_cwd is not None:
            os.chdir(original_cwd)

    print(f"PMA UI screenshots: {out_dir}")
    if evidence["outbox"]:
        print(f"Outboxed {len(evidence['outbox'])} screenshot(s).")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
