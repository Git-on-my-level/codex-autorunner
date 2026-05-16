#!/usr/bin/env python3
"""Measure Web UI route ready times against a seeded or live hub."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import web_ui_screens
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.utils import atomic_write
from tests.chat_surface_lab.web_responsiveness_budgets import (
    DEFAULT_SEED,
    seed_large_web_hub,
)

DEFAULT_OUT_DIR = (
    REPO_ROOT / ".codex-autorunner" / "diagnostics" / "web-page-load-times"
)

DEFAULT_ROUTES = (
    ("hub", "/hub"),
    ("chats", "/chats"),
    ("chat-detail", "/chats/{chat_id}"),
    ("repos", "/repos"),
    ("worktrees", "/worktrees"),
    ("tickets", "/tickets"),
    ("settings", "/settings"),
)

PRIMARY_LOADING_MARKERS = (
    "Opening Web Hub...",
    "Opening PMA...",
    "Loading chats",
    "Loading PMA chats",
    "Loading active chat",
    "Loading repos",
    "Loading repositories",
    "Loading worktrees",
    "Loading tickets",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open Web UI routes in Chromium and record route-ready timings. "
            "By default this seeds a deterministic large fixture hub."
        )
    )
    parser.add_argument("--mode", choices=("fixture", "hub", "url"), default="fixture")
    parser.add_argument("--hub-root", type=Path, default=Path.home() / "car-workspace")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--base-path", default="/car")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--chat-id", default=None)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--viewport", default="1440x1000")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--chat-count", type=int, default=DEFAULT_SEED["chat_count"])
    parser.add_argument("--repo-count", type=int, default=DEFAULT_SEED["repo_count"])
    parser.add_argument(
        "--worktree-count", type=int, default=DEFAULT_SEED["worktree_count"]
    )
    parser.add_argument(
        "--ticket-run-group-count",
        type=int,
        default=DEFAULT_SEED["ticket_run_group_count"],
    )
    parser.add_argument(
        "--timeline-event-count",
        type=int,
        default=DEFAULT_SEED["timeline_event_count"],
    )
    parser.add_argument(
        "--journal-event-count",
        type=int,
        default=DEFAULT_SEED["journal_event_count"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repeats <= 0:
        print(
            "profile-web-page-load-times: --repeats must be positive", file=sys.stderr
        )
        return 2
    try:
        viewport = web_ui_screens.parse_viewport(args.viewport)
    except ValueError as exc:
        print(f"profile-web-page-load-times: {exc}", file=sys.stderr)
        return 2

    out_dir = args.out_dir.resolve()
    if out_dir.exists() and not args.no_clean:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_path = web_ui_screens.normalize_base_path(args.base_path)
    server = None
    thread = None
    hub_root: Path | None = None
    original_cwd: Path | None = None
    tempdir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.mode == "url":
            if not args.base_url:
                print(
                    "profile-web-page-load-times: --base-url is required with --mode url",
                    file=sys.stderr,
                )
                return 2
            base_url = args.base_url.rstrip("/")
        else:
            if args.mode == "fixture":
                tempdir = tempfile.TemporaryDirectory(prefix="car-web-page-load-")
                hub_root = Path(tempdir.name) / "hub"
                seed_hub_files(hub_root, force=True)
                seed_stats = seed_large_web_hub(
                    hub_root,
                    chat_count=args.chat_count,
                    repo_count=args.repo_count,
                    worktree_count=args.worktree_count,
                    ticket_run_group_count=args.ticket_run_group_count,
                    timeline_event_count=args.timeline_event_count,
                    journal_event_count=args.journal_event_count,
                )
                if not args.chat_id:
                    args.chat_id = str(seed_stats.get("detail_thread_id") or "")
            else:
                hub_root = args.hub_root.expanduser().resolve()
                if not hub_root.exists():
                    print(
                        f"profile-web-page-load-times: hub root does not exist: {hub_root}",
                        file=sys.stderr,
                    )
                    return 2
            port = args.port or web_ui_screens.find_free_port(args.host)
            base_url = f"http://{args.host}:{port}"
            original_cwd = Path.cwd()
            os.chdir(web_ui_screens.server_working_directory(hub_root))
            server, thread = web_ui_screens.start_server(
                hub_root, args.host, port, base_path or "/"
            )
            web_ui_screens.wait_for_server(base_url, base_path, args.timeout_seconds)

        chat_id = args.chat_id or _first_live_chat_id(
            base_url=base_url, base_path=base_path
        )
        routes = _build_routes(chat_id)
        report = run_profile(
            base_url=base_url,
            base_path=base_path,
            routes=routes,
            viewport=viewport,
            repeats=args.repeats,
            timeout_seconds=args.timeout_seconds,
            headed=args.headed,
            profile=args.profile,
            out_dir=out_dir,
        )
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if original_cwd is not None:
            os.chdir(original_cwd)
        if tempdir is not None:
            tempdir.cleanup()

    latest_path = out_dir / "latest.json"
    run_report_path = out_dir / "runs" / str(report["run_id"]) / "report.json"
    run_report_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    atomic_write(latest_path, serialized)
    atomic_write(run_report_path, serialized)
    print(_format_summary(report, latest_path, run_report_path))
    return 0


def run_profile(
    *,
    base_url: str,
    base_path: str,
    routes: list[tuple[str, str]],
    viewport: tuple[int, int],
    repeats: int,
    timeout_seconds: float,
    headed: bool,
    profile: str,
    out_dir: Path,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed. Use the project venv browser extra and install Chromium."
        ) from exc

    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    samples: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        for repeat in range(1, repeats + 1):
            for name, path in routes:
                context = browser.new_context(
                    viewport={"width": viewport[0], "height": viewport[1]},
                    device_scale_factor=1,
                )
                page = context.new_page()
                url = web_ui_screens.route_url(base_url, base_path, path)
                started = time.perf_counter()
                status: int | None = None
                error: str | None = None
                loading_timed_out = False
                try:
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_seconds * 1000,
                    )
                    status = response.status if response is not None else None
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
                except PlaywrightTimeoutError as exc:
                    loading_timed_out = True
                    error = str(exc)
                except Exception as exc:
                    error = str(exc)
                ready_ms = round((time.perf_counter() - started) * 1000, 3)
                metrics = page.evaluate(
                    """() => {
                        const nav = performance.getEntriesByType('navigation')[0];
                        return {
                            domContentLoadedMs: nav ? nav.domContentLoadedEventEnd : 0,
                            loadEventMs: nav ? nav.loadEventEnd : 0,
                            transferSize: nav ? nav.transferSize : 0,
                            decodedBodySize: nav ? nav.decodedBodySize : 0,
                            elements: document.querySelectorAll('*').length,
                            bodyTextChars: document.body?.innerText?.length || 0,
                        };
                    }"""
                )
                samples.append(
                    {
                        "route": name,
                        "path": path,
                        "url": url,
                        "repeat": repeat,
                        "status": status,
                        "error": error,
                        "loading_timed_out": loading_timed_out,
                        "ready_ms": ready_ms,
                        "metrics": metrics,
                    }
                )
                context.close()
        browser.close()

    ready_values = [sample["ready_ms"] for sample in samples if not sample.get("error")]
    route_summaries = []
    for name, path in routes:
        route_values = [
            sample["ready_ms"]
            for sample in samples
            if sample["route"] == name and not sample.get("error")
        ]
        route_summaries.append(
            {
                "route": name,
                "path": path,
                **_summary_stats(route_values),
            }
        )
    return {
        "version": 1,
        "suite": "web_page_load_times",
        "profile": profile,
        "run_id": run_id,
        "base_url": base_url,
        "base_path": base_path,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "repeats": repeats,
        "summary": _summary_stats(ready_values),
        "routes": route_summaries,
        "samples": samples,
        "signoff": {
            "route_count": len(routes),
            "sample_count": len(samples),
            "error_count": sum(1 for sample in samples if sample.get("error")),
        },
    }


def _summary_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median_ms": None, "p75_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p75_ms": round(_percentile(ordered, 0.75), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "max_ms": round(max(ordered), 3),
    }


def _percentile(ordered_values: list[float], percentile: float) -> float:
    if not ordered_values:
        return 0.0
    index = min(
        len(ordered_values) - 1,
        max(0, int(round((len(ordered_values) - 1) * percentile))),
    )
    return ordered_values[index]


def _build_routes(chat_id: str | None) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for name, path in DEFAULT_ROUTES:
        if "{chat_id}" in path:
            if not chat_id:
                continue
            path = path.format(chat_id=chat_id)
        routes.append((name, path))
    return routes


def _first_live_chat_id(*, base_url: str, base_path: str) -> str | None:
    url = web_ui_screens.route_url(base_url, base_path, "/hub/read-models/chats")
    try:
        with urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            return str(row["id"])
    return None


def _format_summary(report: dict[str, Any], latest: Path, run_report: Path) -> str:
    summary = report["summary"]
    lines = [
        "WEB PAGE LOAD TIMES",
        f"run_id={report['run_id']}",
        f"profile={report['profile']}",
        f"latest={latest}",
        f"run_report={run_report}",
        f"median_ms={summary['median_ms']}",
        f"p75_ms={summary['p75_ms']}",
        f"p95_ms={summary['p95_ms']}",
        f"max_ms={summary['max_ms']}",
        f"errors={report['signoff']['error_count']}",
        "routes:",
    ]
    for route in report["routes"]:
        lines.append(
            f"  {route['route']}: median={route['median_ms']} p75={route['p75_ms']} "
            f"p95={route['p95_ms']} max={route['max_ms']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
