#!/usr/bin/env python3
"""Profile browser responsiveness while an opened Web UI chat sits idle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import web_ui_screens
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.utils import atomic_write
from pma_live_hub_smoke import SMOKE_FIXTURE_MANIFEST
from tests.chat_surface_lab.web_responsiveness_budgets import (
    DEFAULT_SEED,
    seed_large_web_hub,
)

DEFAULT_OUT_DIR = (
    REPO_ROOT / ".codex-autorunner" / "diagnostics" / "web-chats-idle-profile"
)

PRIMARY_LOADING_MARKERS = (
    "Opening Web Hub...",
    "Opening PMA...",
    "Loading chats",
    "Loading PMA chats",
    "Loading active chat",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start a seeded hub, open the chats page and a chat in Chromium, "
            "idle for at least 60 seconds, and write responsiveness evidence."
        )
    )
    parser.add_argument("--mode", choices=("fixture", "hub", "url"), default="fixture")
    parser.add_argument(
        "--fixture-kind",
        choices=("smoke", "large"),
        default="large",
        help="Fixture to seed when --mode fixture. Default: large.",
    )
    parser.add_argument("--hub-root", type=Path, default=Path.home() / "car-workspace")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--base-path", default="/car")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--chat-id", default=None)
    parser.add_argument("--idle-seconds", type=float, default=75.0)
    parser.add_argument("--sample-ms", type=int, default=250)
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
    parser.add_argument("--viewport", default="1440x1000")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-clean", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.idle_seconds < 60:
        print("profile-web-chats-idle: --idle-seconds must be at least 60", file=sys.stderr)
        return 2
    try:
        viewport = web_ui_screens.parse_viewport(args.viewport)
    except ValueError as exc:
        print(f"profile-web-chats-idle: {exc}", file=sys.stderr)
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
    try:
        if args.mode == "url":
            if not args.base_url:
                print("profile-web-chats-idle: --base-url is required with --mode url", file=sys.stderr)
                return 2
            base_url = args.base_url.rstrip("/")
        else:
            if args.mode == "fixture":
                if args.fixture_kind == "large":
                    hub_root = _seed_large_fixture_hub(out_dir / "fixture", args)
                    seeded_chat_id = _large_fixture_chat_id(out_dir / "fixture")
                    if seeded_chat_id and not args.chat_id:
                        args.chat_id = seeded_chat_id
                else:
                    hub_root = web_ui_screens.seed_fixture_hub(out_dir / "fixture")
            else:
                hub_root = args.hub_root.expanduser().resolve()
                if not hub_root.exists():
                    print(f"profile-web-chats-idle: hub root does not exist: {hub_root}", file=sys.stderr)
                    return 2
            port = args.port or web_ui_screens.find_free_port(args.host)
            base_url = f"http://{args.host}:{port}"
            original_cwd = Path.cwd()
            os.chdir(web_ui_screens.server_working_directory(hub_root))
            server, thread = web_ui_screens.start_server(
                hub_root, args.host, port, base_path or "/"
            )
            web_ui_screens.wait_for_server(base_url, base_path, args.timeout_seconds)

        chat_id = args.chat_id or _fixture_chat_id(hub_root)
        if not chat_id:
            chat_id = _first_live_chat_id(base_url=base_url, base_path=base_path)
        report = run_browser_profile(
            base_url=base_url,
            base_path=base_path,
            chat_id=chat_id,
            viewport=viewport,
            idle_seconds=args.idle_seconds,
            sample_ms=args.sample_ms,
            timeout_seconds=args.timeout_seconds,
            headed=args.headed,
            out_dir=out_dir,
            hub_root=hub_root,
            mode=args.mode,
        )
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if original_cwd is not None:
            os.chdir(original_cwd)

    latest_path = out_dir / "latest.json"
    report_path = out_dir / "runs" / str(report["run_id"]) / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    atomic_write(latest_path, serialized)
    atomic_write(report_path, serialized)

    print(_format_summary(report, latest_path, report_path))
    return 0 if report["signoff"]["reproduced_freeze"] is False else 1


def run_browser_profile(
    *,
    base_url: str,
    base_path: str,
    chat_id: str | None,
    viewport: tuple[int, int],
    idle_seconds: float,
    sample_ms: int,
    timeout_seconds: float,
    headed: bool,
    out_dir: Path,
    hub_root: Path | None,
    mode: str,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed for this Python. Use the project venv "
            "or install Playwright Chromium."
        ) from exc

    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    screenshot_path = out_dir / "runs" / run_id / "chat-idle.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(
            viewport={"width": viewport[0], "height": viewport[1]},
            device_scale_factor=1,
        )
        page.on(
            "console",
            lambda message: console_messages.append(
                {
                    "type": message.type,
                    "text": message.text,
                    "location": message.location,
                }
            )
            if message.type in {"error", "warning"}
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": request.failure,
                }
            ),
        )
        session = page.context.new_cdp_session(page)
        session.send("Performance.enable")

        index_url = web_ui_screens.route_url(base_url, base_path, "/chats")
        detail_url = (
            web_ui_screens.route_url(base_url, base_path, f"/chats/{chat_id}")
            if chat_id
            else None
        )
        navigation = []
        navigation.append(_goto_and_wait(page, index_url, timeout_seconds))
        if detail_url:
            navigation.append(_goto_and_wait(page, detail_url, timeout_seconds))
        else:
            clicked = _click_first_chat_link(page, base_path, timeout_seconds)
            navigation.append({"url": page.url, "clicked_first_chat_link": clicked})

        page.evaluate(
            """({ sampleMs }) => {
                const state = {
                    sampleMs,
                    startedAt: performance.now(),
                    lastAt: performance.now(),
                    tickCount: 0,
                    rafCount: 0,
                    maxIntervalGapMs: 0,
                    intervalGaps: [],
                    longTasks: [],
                    observerError: null,
                };
                window.__carIdleProfile = state;
                window.__carIdleProfileInterval = window.setInterval(() => {
                    const now = performance.now();
                    const gap = now - state.lastAt;
                    state.tickCount += 1;
                    state.lastAt = now;
                    state.maxIntervalGapMs = Math.max(state.maxIntervalGapMs, gap);
                    state.intervalGaps.push({
                        atMs: Math.round((now - state.startedAt) * 1000) / 1000,
                        gapMs: Math.round(gap * 1000) / 1000,
                    });
                    if (state.intervalGaps.length > 2000) state.intervalGaps.shift();
                }, sampleMs);
                const raf = () => {
                    state.rafCount += 1;
                    window.__carIdleProfileRaf = requestAnimationFrame(raf);
                };
                window.__carIdleProfileRaf = requestAnimationFrame(raf);
                try {
                    const observer = new PerformanceObserver((list) => {
                        for (const entry of list.getEntries()) {
                            state.longTasks.push({
                                name: entry.name,
                                startTime: Math.round(entry.startTime * 1000) / 1000,
                                duration: Math.round(entry.duration * 1000) / 1000,
                            });
                        }
                    });
                    observer.observe({ entryTypes: ['longtask'] });
                    window.__carIdleProfileObserver = observer;
                } catch (error) {
                    state.observerError = String(error);
                }
            }""",
            {"sampleMs": sample_ms},
        )

        page.wait_for_timeout(idle_seconds * 1000)
        idle_metrics = page.evaluate(
            """() => {
                const state = window.__carIdleProfile || {};
                const gapEntries = Array.isArray(state.intervalGaps) ? state.intervalGaps : [];
                const gaps = gapEntries.map((entry) => typeof entry === 'number' ? entry : entry.gapMs || 0);
                const post60GapEntries = gapEntries.filter((entry) => {
                    if (typeof entry === 'number') return false;
                    return (entry.atMs || 0) >= 60000;
                });
                const post60Gaps = post60GapEntries.map((entry) => entry.gapMs || 0);
                const sorted = [...gaps].sort((a, b) => a - b);
                const percentile = (values, p) => {
                    const sortedValues = [...values].sort((a, b) => a - b);
                    if (!sortedValues.length) return 0;
                    const index = Math.min(sortedValues.length - 1, Math.max(0, Math.ceil(sortedValues.length * p) - 1));
                    return sortedValues[index] || 0;
                };
                const longTasks = state.longTasks || [];
                const post60LongTasks = longTasks.filter((entry) => (entry.startTime || 0) >= 60000);
                return {
                    measuredMs: Math.round((performance.now() - (state.startedAt || performance.now())) * 1000) / 1000,
                    sampleMs: state.sampleMs || null,
                    tickCount: state.tickCount || 0,
                    rafCount: state.rafCount || 0,
                    maxIntervalGapMs: state.maxIntervalGapMs || 0,
                    intervalGapP95Ms: percentile(gaps, 0.95),
                    intervalGapP99Ms: percentile(gaps, 0.99),
                    intervalGapOver1000Ms: gaps.filter((gap) => gap > 1000).length,
                    intervalGapOver5000Ms: gaps.filter((gap) => gap > 5000).length,
                    maxPost60IntervalGapMs: Math.max(0, ...post60Gaps),
                    post60IntervalGapP99Ms: percentile(post60Gaps, 0.99),
                    post60IntervalGapOver1000Ms: post60Gaps.filter((gap) => gap > 1000).length,
                    post60IntervalGapOver5000Ms: post60Gaps.filter((gap) => gap > 5000).length,
                    intervalGaps: gapEntries.slice(-50),
                    longTaskCount: Array.isArray(state.longTasks) ? state.longTasks.length : 0,
                    maxLongTaskMs: Math.max(0, ...longTasks.map((entry) => entry.duration || 0)),
                    post60LongTaskCount: post60LongTasks.length,
                    maxPost60LongTaskMs: Math.max(0, ...post60LongTasks.map((entry) => entry.duration || 0)),
                    longTasks: longTasks.slice(-50),
                    observerError: state.observerError || null,
                    dom: {
                        elements: document.querySelectorAll('*').length,
                        links: document.querySelectorAll('a[href]').length,
                        buttons: document.querySelectorAll('button,[role="button"]').length,
                        inputs: document.querySelectorAll('input,textarea,select').length,
                        bodyTextChars: document.body?.innerText?.length || 0,
                    },
                    page: {
                        title: document.title,
                        url: window.location.href,
                        visibilityState: document.visibilityState,
                    },
                };
            }"""
        )
        perf_metrics = _performance_metrics(session)
        page.screenshot(path=str(screenshot_path), full_page=False)
        browser.close()

    actionable_failed_requests = [
        request
        for request in failed_requests
        if request.get("failure") != "net::ERR_ABORTED"
    ]
    reproduced = (
        idle_metrics["maxPost60IntervalGapMs"] >= 5000
        or idle_metrics["post60IntervalGapOver5000Ms"] > 0
        or idle_metrics["maxPost60LongTaskMs"] >= 5000
    )
    laggy = (
        reproduced
        or idle_metrics["maxPost60IntervalGapMs"] >= 1000
        or idle_metrics["post60IntervalGapOver1000Ms"] > 0
        or idle_metrics["maxPost60LongTaskMs"] >= 1000
    )
    return {
        "version": 1,
        "suite": "web_chats_idle_profile",
        "run_id": run_id,
        "mode": mode,
        "base_url": base_url,
        "base_path": base_path,
        "hub_root": str(hub_root) if hub_root else None,
        "chat_id": chat_id,
        "idle_seconds_requested": idle_seconds,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "navigation": navigation,
        "idle_metrics": idle_metrics,
        "chrome_performance_metrics": perf_metrics,
        "console": {
            "errors": [item for item in console_messages if item["type"] == "error"],
            "warnings": [item for item in console_messages if item["type"] == "warning"],
        },
        "page_errors": page_errors,
        "failed_requests": actionable_failed_requests,
        "screenshot": {"path": str(screenshot_path), "size_bytes": screenshot_path.stat().st_size},
        "signoff": {
            "reproduced_freeze": reproduced,
            "observed_lag": laggy,
            "message": (
                "freeze reproduced"
                if reproduced
                else "no freeze reproduced; lag observed"
                if laggy
                else "no freeze or severe lag reproduced"
            ),
        },
    }


def _goto_and_wait(page: Any, url: str, timeout_seconds: float) -> dict[str, Any]:
    response_status = None
    error = None
    loading_timed_out = False
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        response_status = response.status if response is not None else None
    except Exception as exc:
        error = str(exc)
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
    except Exception:
        loading_timed_out = True
    return {
        "url": url,
        "final_url": page.url,
        "status": response_status,
        "error": error,
        "loading_timed_out": loading_timed_out,
    }


def _click_first_chat_link(page: Any, base_path: str, timeout_seconds: float) -> bool:
    href = page.evaluate(
        """(basePath) => {
            const prefix = `${basePath || ''}/chats/`;
            const link = Array.from(document.querySelectorAll('a[href]'))
                .find((node) => {
                    const href = node.getAttribute('href') || '';
                    return href.startsWith(prefix) || href.startsWith('/chats/');
                });
            return link ? link.getAttribute('href') : null;
        }""",
        base_path,
    )
    if not href:
        return False
    page.evaluate(
        """(targetHref) => {
            const link = Array.from(document.querySelectorAll('a[href]'))
                .find((node) => node.getAttribute('href') === targetHref);
            if (!link) throw new Error(`chat link disappeared: ${targetHref}`);
            link.click();
        }""",
        href,
    )
    page.wait_for_load_state("domcontentloaded", timeout=timeout_seconds * 1000)
    return True


def _fixture_chat_id(hub_root: Path | None) -> str | None:
    if hub_root is None:
        return None
    manifest_path = hub_root / SMOKE_FIXTURE_MANIFEST
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    value = payload.get("final_thread_id") or payload.get("running_thread_id")
    return str(value) if value else None


def _seed_large_fixture_hub(evidence_dir: Path, args: argparse.Namespace) -> Path:
    hub_root = evidence_dir / "hub"
    if hub_root.exists():
        shutil.rmtree(hub_root)
    hub_root.mkdir(parents=True)
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
    (evidence_dir / "large-fixture-manifest.json").write_text(
        json.dumps(seed_stats, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return hub_root


def _large_fixture_chat_id(evidence_dir: Path) -> str | None:
    manifest_path = evidence_dir / "large-fixture-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    value = payload.get("detail_thread_id")
    return str(value) if value else None


def _first_live_chat_id(*, base_url: str, base_path: str) -> str | None:
    url = f"{base_url.rstrip('/')}{base_path}/hub/read-models/chats?limit=25"
    try:
        with urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        lifecycle = str(row.get("lifecycle") or "").strip().lower()
        chat_id = row.get("chatId")
        if chat_id and status != "archived" and lifecycle != "archived":
            return str(chat_id)
    for row in rows:
        if isinstance(row, dict) and row.get("chatId"):
            return str(row["chatId"])
    return None


def _performance_metrics(session: Any) -> dict[str, float]:
    payload = session.send("Performance.getMetrics")
    return {
        str(item["name"]): float(item["value"])
        for item in payload.get("metrics", [])
        if "name" in item and "value" in item
    }


def _format_summary(report: dict[str, Any], latest_path: Path, report_path: Path) -> str:
    metrics = report["idle_metrics"]
    signoff = report["signoff"]
    return "\n".join(
        [
            "WEB CHATS IDLE PROFILE",
            f"run_id={report['run_id']}",
            f"status={signoff['message']}",
            f"latest={latest_path}",
            f"run_report={report_path}",
            f"url={metrics['page']['url']}",
            f"measured_ms={metrics['measuredMs']}",
            f"max_interval_gap_ms={round(metrics['maxIntervalGapMs'], 3)}",
            f"interval_gap_p99_ms={round(metrics['intervalGapP99Ms'], 3)}",
            f"long_task_count={metrics['longTaskCount']}",
            f"max_long_task_ms={round(metrics['maxLongTaskMs'], 3)}",
            f"max_post60_interval_gap_ms={round(metrics['maxPost60IntervalGapMs'], 3)}",
            f"post60_interval_gap_p99_ms={round(metrics['post60IntervalGapP99Ms'], 3)}",
            f"post60_long_task_count={metrics['post60LongTaskCount']}",
            f"max_post60_long_task_ms={round(metrics['maxPost60LongTaskMs'], 3)}",
            f"dom_elements={metrics['dom']['elements']}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
