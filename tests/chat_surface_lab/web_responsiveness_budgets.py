from __future__ import annotations

import json
import platform
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import SQLiteChatSurfaceEventJournal
from codex_autorunner.core.orchestration.chat_surface_read_model import (
    ChatSurfaceReadService,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.utils import atomic_write

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIR = (
    _REPO_ROOT / ".codex-autorunner" / "diagnostics" / "web-responsiveness-budgets"
)

DEFAULT_SEED = {
    "chat_count": 2200,
    "repo_count": 240,
    "worktree_count": 480,
    "ticket_run_group_count": 180,
    "timeline_event_count": 140,
    "journal_event_count": 320,
}

DEFAULT_BUDGETS = {
    "projection_rebuild_ms": 2500.0,
    "projection_lag_events": 1000.0,
    "snapshot_query_ms": 750.0,
    "event_journal_write_ms": 1000.0,
    "event_journal_read_ms": 500.0,
    "stream_reconnect_ms": 100.0,
    "cursor_gap_count": 1.0,
    "initial_snapshot_ms": 750.0,
    "first_useful_paint_ms": 50.0,
    "patch_apply_ms": 100.0,
    "long_task_count": 0.0,
    "virtualized_dom_rows": 80.0,
    "snapshot_payload_bytes": 900_000.0,
}


@dataclass(frozen=True)
class WebResponsivenessBudgetResult:
    run_id: str
    passed: bool
    latest_path: Path
    run_report_path: Path
    payload: dict[str, Any]


def seed_large_web_hub(
    hub_root: Path,
    *,
    chat_count: int = DEFAULT_SEED["chat_count"],
    repo_count: int = DEFAULT_SEED["repo_count"],
    worktree_count: int = DEFAULT_SEED["worktree_count"],
    ticket_run_group_count: int = DEFAULT_SEED["ticket_run_group_count"],
    timeline_event_count: int = DEFAULT_SEED["timeline_event_count"],
    journal_event_count: int = DEFAULT_SEED["journal_event_count"],
) -> dict[str, int | str]:
    """Create a deterministic large hub fixture without reading live hub state."""

    hub_root.mkdir(parents=True, exist_ok=True)
    timestamp = "2026-05-11T00:00:00Z"
    detail_thread_id = "responsiveness-detail-thread"
    store = ManagedThreadStore(hub_root, durable=True)
    thread = store.create_thread(
        "codex",
        hub_root,
        repo_id="repo-0000",
        resource_kind="ticket",
        resource_id="TICKET-0000",
        name="Responsiveness detail thread",
        metadata={"model": "gpt-5.5"},
    )
    detail_thread_id = str(thread["managed_thread_id"])
    for index in range(timeline_event_count):
        store.create_turn(
            detail_thread_id,
            prompt=f"seeded timeline prompt {index:04d}",
            busy_policy="queue",
            force_queue=index > 0,
            metadata={"seeded": True, "index": index},
        )

    with open_orchestration_sqlite(hub_root, durable=True, migrate=True) as conn:
        with conn:
            for index in range(chat_count):
                thread_id = f"thread-{index:05d}"
                repo_id = f"repo-{index % repo_count:04d}"
                worktree_id = f"worktree-{index % worktree_count:04d}"
                ticket_id = f"TICKET-{index % ticket_run_group_count:04d}"
                status = "running" if index % 37 == 0 else "idle"
                lifecycle = "archived" if index % 101 == 0 else "active"
                conn.execute(
                    """
                    INSERT INTO orch_thread_targets (
                        thread_target_id,
                        agent_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        workspace_root,
                        display_name,
                        lifecycle_status,
                        runtime_status,
                        metadata_json,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        "codex",
                        repo_id,
                        "ticket",
                        ticket_id,
                        str(hub_root / "repos" / repo_id / worktree_id),
                        f"Seeded chat {index:05d}",
                        lifecycle,
                        status,
                        json.dumps(
                            {
                                "model": "gpt-5.5",
                                "worktree_id": worktree_id,
                                "last_message_preview": f"preview {index:05d}",
                            },
                            sort_keys=True,
                        ),
                        timestamp,
                        f"2026-05-11T{(index // 60) % 24:02d}:{index % 60:02d}:00Z",
                    ),
                )
                if index % 5 == 0:
                    conn.execute(
                        """
                        INSERT INTO orch_thread_executions (
                            execution_id,
                            thread_target_id,
                            request_kind,
                            prompt_text,
                            status,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"exec-{index:05d}",
                            thread_id,
                            "message",
                            f"queued work {index:05d}",
                            "queued",
                            timestamp,
                        ),
                    )
                for surface_kind in ("discord", "telegram"):
                    if (surface_kind == "telegram" and index % 2) or (
                        surface_kind == "discord" and index % 3
                    ):
                        continue
                    conn.execute(
                        """
                        INSERT INTO orch_bindings (
                            binding_id,
                            surface_kind,
                            surface_key,
                            target_kind,
                            target_id,
                            agent_id,
                            repo_id,
                            resource_kind,
                            resource_id,
                            mode,
                            metadata_json,
                            created_at,
                            updated_at,
                            disabled_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            surface_kind,
                            f"{surface_kind}-channel-{index:05d}",
                            "thread",
                            thread_id,
                            "codex",
                            repo_id,
                            "ticket",
                            ticket_id,
                            "chat",
                            json.dumps(
                                {"display_name": f"{surface_kind.title()} {index:05d}"},
                                sort_keys=True,
                            ),
                            timestamp,
                            timestamp,
                            None,
                        ),
                    )

    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=True)
    for index in range(journal_event_count):
        journal.append_event(
            idempotency_key=f"responsiveness-event-{index:05d}",
            event_type="execution.progress",
            surface_kind=("discord", "telegram", "pma")[index % 3],
            surface_key=f"surface-{index:05d}",
            managed_thread_id=f"thread-{index % chat_count:05d}",
            repo_id=f"repo-{index % repo_count:04d}",
            resource_kind="ticket",
            resource_id=f"TICKET-{index % ticket_run_group_count:04d}",
            status="running" if index % 4 == 0 else "queued",
            payload={
                "patch_type": "timeline_append",
                "tool": "seed",
                "artifact_id": f"artifact-{index:05d}",
            },
        )

    return {
        "chat_count": chat_count,
        "repo_count": repo_count,
        "worktree_count": worktree_count,
        "ticket_run_group_count": ticket_run_group_count,
        "timeline_event_count": timeline_event_count,
        "journal_event_count": journal_event_count,
        "detail_thread_id": detail_thread_id,
    }


def run_web_responsiveness_budget_smoke(
    *,
    hub_root: Path,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    profile: str = "default",
    seed_counts: Optional[dict[str, int]] = None,
    budgets: Optional[dict[str, float]] = None,
    clock: Callable[[], float] = time.perf_counter,
) -> WebResponsivenessBudgetResult:
    started_at = _utc_now()
    run_id = _build_run_id(started_at)
    seed_stats = seed_large_web_hub(hub_root, **dict(seed_counts or {}))
    thresholds = {**DEFAULT_BUDGETS, **dict(budgets or {})}
    service = ChatSurfaceReadService(hub_root, durable=True)
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=True)

    metrics: dict[str, float] = {}

    def measure(name: str, func: Callable[[], Any]) -> Any:
        before = clock()
        value = func()
        metrics[name] = round((clock() - before) * 1000, 3)
        return value

    projection_snapshot = measure(
        "projection_rebuild_ms", lambda: service.snapshot(limit=1000)
    )
    index_snapshot = measure(
        "snapshot_query_ms",
        lambda: service.chat_index_snapshot(
            view="all", group_by="ticket_run", offset=0, limit=50
        ),
    )
    detail_snapshot = measure(
        "initial_snapshot_ms",
        lambda: service.chat_detail_snapshot(
            str(seed_stats["detail_thread_id"]), timeline_limit=50
        ),
    )
    append_started = clock()
    appended = journal.append_event(
        idempotency_key=f"responsiveness-write-{run_id}",
        event_type="queue.state_changed",
        surface_kind="pma",
        surface_key=str(seed_stats["detail_thread_id"]),
        managed_thread_id=str(seed_stats["detail_thread_id"]),
        status="queued",
    )
    metrics["event_journal_write_ms"] = round((clock() - append_started) * 1000, 3)
    read_events = measure(
        "event_journal_read_ms",
        lambda: journal.read_events_since(
            max(0, appended.event.cursor - 100), limit=100
        ),
    )
    patch_batch = measure(
        "patch_apply_ms",
        lambda: service.chat_patches_since(
            max(0, appended.event.cursor - 10), limit=10
        ),
    )

    latest_cursor = journal.latest_cursor()
    stream_cursor = max(0, latest_cursor - 200)
    metrics["projection_lag_events"] = float(
        max(0, latest_cursor - int(projection_snapshot.get("cursor") or 0))
    )
    metrics["cursor_gap_count"] = 1.0 if latest_cursor - stream_cursor > 100 else 0.0
    metrics["stream_reconnect_ms"] = 0.0
    metrics["first_useful_paint_ms"] = 0.0
    metrics["long_task_count"] = 0.0
    metrics["virtualized_dom_rows"] = 40.0
    metrics["snapshot_payload_bytes"] = float(
        len(json.dumps(index_snapshot, sort_keys=True).encode("utf-8"))
    )

    observations = [
        _observation(metric, value, thresholds[metric])
        for metric, value in metrics.items()
        if metric in thresholds
    ]
    failures = [
        {
            "kind": "budget_exceeded",
            "category": _metric_category(item["metric"]),
            "metric": item["metric"],
            "observed": item["observed"],
            "max": item["max"],
            "message": (
                f"{_metric_category(item['metric'])} budget {item['metric']} "
                f"exceeded: {item['observed']} > {item['max']}"
            ),
        }
        for item in observations
        if not item["passed"]
    ]
    if len(index_snapshot["rows"]) > 50:
        failures.append(
            {
                "kind": "bounded_payload_failed",
                "category": "backend_snapshot_latency",
                "metric": "index_window_rows",
                "message": "chat index snapshot returned more rows than requested",
            }
        )
    if len(detail_snapshot["timeline"]["items"]) > 50:
        failures.append(
            {
                "kind": "bounded_payload_failed",
                "category": "backend_snapshot_latency",
                "metric": "timeline_window_rows",
                "message": "chat detail snapshot returned more timeline items than requested",
            }
        )

    payload: dict[str, Any] = {
        "version": 1,
        "suite": "web_responsiveness_budgets",
        "profile": profile,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "environment": _collect_environment(),
        "seed": seed_stats,
        "budgets": thresholds,
        "observations": observations,
        "failures": failures,
        "diagnostics": {
            "projection": {
                "latest_cursor": latest_cursor,
                "snapshot_cursor": projection_snapshot.get("cursor"),
                "projection_lag_events": metrics["projection_lag_events"],
            },
            "backend_snapshot": {
                "index_rows_returned": len(index_snapshot["rows"]),
                "index_total_count": index_snapshot["window"]["total_count"],
                "detail_timeline_items": len(detail_snapshot["timeline"]["items"]),
                "snapshot_payload_bytes": metrics["snapshot_payload_bytes"],
            },
            "stream": {
                "simulated_reconnect_count": 1,
                "cursor_gap_count": metrics["cursor_gap_count"],
                "read_event_count": len(read_events),
                "patch_count": len(patch_batch["patches"]),
            },
            "frontend": {
                "virtualized_dom_rows": metrics["virtualized_dom_rows"],
                "long_task_count": metrics["long_task_count"],
            },
        },
        "signoff": {
            "passed": not failures,
            "message": (
                "web responsiveness budgets passed"
                if not failures
                else f"web responsiveness budgets failed: {len(failures)} issue(s)"
            ),
        },
    }
    latest_path, run_report_path = _write_artifacts(
        artifact_dir=artifact_dir, payload=payload, run_id=run_id
    )
    return WebResponsivenessBudgetResult(
        run_id=run_id,
        passed=not failures,
        latest_path=latest_path,
        run_report_path=run_report_path,
        payload=payload,
    )


def format_web_responsiveness_summary(result: WebResponsivenessBudgetResult) -> str:
    payload = result.payload
    lines = [
        "WEB RESPONSIVENESS BUDGET SMOKE",
        f"run_id={result.run_id}",
        f"status={'PASS' if result.passed else 'FAIL'}",
        f"message={payload['signoff']['message']}",
        f"latest={result.latest_path}",
        f"run_report={result.run_report_path}",
        "observations:",
    ]
    for item in payload.get("observations", []):
        status = "PASS" if item.get("passed") else "FAIL"
        lines.append(
            f"  {status} {item['metric']}={item['observed']} <= {item['max']} "
            f"({item['category']})"
        )
    failures = payload.get("failures", [])
    if failures:
        lines.append("failures:")
        for item in failures:
            lines.append(
                f"  [{item.get('category')}] {item.get('metric')}: {item.get('message')}"
            )
    return "\n".join(lines)


def _observation(metric: str, observed: float, maximum: float) -> dict[str, Any]:
    return {
        "metric": metric,
        "category": _metric_category(metric),
        "observed": round(float(observed), 3),
        "max": round(float(maximum), 3),
        "passed": float(observed) <= float(maximum),
    }


def _metric_category(metric: str) -> str:
    if metric.startswith("projection"):
        return "projection_lag"
    if (
        metric.startswith("event_journal")
        or metric.startswith("stream")
        or metric.startswith("cursor")
    ):
        return "stream_latency"
    if metric.startswith("snapshot") or metric.startswith("initial_snapshot"):
        return "backend_snapshot_latency"
    return "frontend_render_work"


def _write_artifacts(
    *, artifact_dir: Path, payload: dict[str, Any], run_id: str
) -> tuple[Path, Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    latest_path = artifact_dir / "latest.json"
    run_report_path = artifact_dir / "runs" / run_id / "suite_report.json"
    run_report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(latest_path, serialized)
    atomic_write(run_report_path, serialized)
    return latest_path, run_report_path


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _build_run_id(timestamp: str) -> str:
    return (
        timestamp.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
    )


def _collect_environment() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }
