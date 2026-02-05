from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.lifecycle_events import LifecycleEventStore
from codex_autorunner.manifest import load_manifest, save_manifest


def _write_hub_config(
    hub_root: Path,
    *,
    dispatch_interception: bool,
    extra_lines: list[str] | None = None,
) -> None:
    hub_root.mkdir(parents=True, exist_ok=True)
    config_path = hub_root / "codex-autorunner.yml"
    lines = [
        "pma:",
        "  enabled: true",
        f"  dispatch_interception_enabled: {'true' if dispatch_interception else 'false'}",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")


def _register_repo(hub_root: Path, repo_root: Path, repo_id: str) -> None:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(manifest_path, manifest, hub_root)


def _write_paused_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={})
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def _write_dispatch_history(repo_root: Path, run_id: str, body: str) -> None:
    dispatch_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nmode: pause\n---\n\n{body}\n"
    (dispatch_dir / "DISPATCH.md").write_text(content, encoding="utf-8")


def _read_queue_items(hub_root: Path) -> list[dict[str, object]]:
    queue_path = (
        hub_root / ".codex-autorunner" / "pma" / "queue" / "pma__COLON__default.jsonl"
    )
    if not queue_path.exists():
        return []
    items: list[dict[str, object]] = []
    for line in queue_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def test_lifecycle_dispatch_auto_resolve_does_not_enqueue_pma(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root, dispatch_interception=True)
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        repo_root = hub_root / "repo-1"
        repo_root.mkdir(parents=True, exist_ok=True)
        _register_repo(hub_root, repo_root, "repo-1")

        run_id = "run-1"
        _write_paused_run(repo_root, run_id)
        _write_dispatch_history(repo_root, run_id, "ok")

        supervisor.lifecycle_emitter.emit_dispatch_created("repo-1", run_id)
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []
        assert _read_queue_items(hub_root) == []
    finally:
        supervisor.shutdown()


def test_lifecycle_dispatch_escalate_enqueues_pma(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root, dispatch_interception=True)
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        repo_root = hub_root / "repo-1"
        repo_root.mkdir(parents=True, exist_ok=True)
        _register_repo(hub_root, repo_root, "repo-1")

        run_id = "run-1"
        _write_paused_run(repo_root, run_id)
        _write_dispatch_history(repo_root, run_id, "please help")

        supervisor.lifecycle_emitter.emit_dispatch_created("repo-1", run_id)
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        items = _read_queue_items(hub_root)
        assert len(items) == 1
        payload = items[0].get("payload") or {}
        lifecycle_event = payload.get("lifecycle_event") or {}
        assert lifecycle_event.get("event_type") == "dispatch_created"
        assert lifecycle_event.get("repo_id") == "repo-1"
    finally:
        supervisor.shutdown()


def test_lifecycle_flow_failed_enqueues_pma(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root, dispatch_interception=False)
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        items = _read_queue_items(hub_root)
        assert len(items) == 1
        payload = items[0].get("payload") or {}
        lifecycle_event = payload.get("lifecycle_event") or {}
        assert lifecycle_event.get("event_type") == "flow_failed"
        assert lifecycle_event.get("repo_id") == "repo-1"
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_disabled_skips_enqueue(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []
        assert _read_queue_items(hub_root) == []
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_allowlist_filters(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=[
            "  reactive_event_types:",
            "    - flow_failed",
        ],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_completed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []
        assert _read_queue_items(hub_root) == []
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_debounce(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_debounce_seconds: 3600"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        items = _read_queue_items(hub_root)
        assert len(items) == 1
        payload = items[0].get("payload") or {}
        lifecycle_event = payload.get("lifecycle_event") or {}
        assert lifecycle_event.get("event_type") == "flow_failed"
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_origin_blocklist(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_origin_blocklist:", "    - pma"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1", origin="pma")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []
        assert _read_queue_items(hub_root) == []
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_rate_limit(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=[
            "  reactive_debounce_seconds: 0",
            "  rate_limit_window_seconds: 3600",
            "  max_actions_per_window: 1",
        ],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-2")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        items = _read_queue_items(hub_root)
        assert len(items) == 1
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_circuit_breaker(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=[
            "  circuit_breaker_threshold: 2",
            "  circuit_breaker_cooldown_seconds: 3600",
        ],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        checker = supervisor.get_pma_safety_checker()
        checker.record_reactive_result(status="error", error="boom")
        checker.record_reactive_result(status="error", error="boom again")

        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []
        assert _read_queue_items(hub_root) == []
    finally:
        supervisor.shutdown()
