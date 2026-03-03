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


def _read_queue_items(
    hub_root: Path, lane_id: str = "pma:default"
) -> list[dict[str, object]]:
    safe_lane_id = lane_id.replace(":", "__COLON__").replace("/", "__SLASH__")
    queue_path = (
        hub_root / ".codex-autorunner" / "pma" / "queue" / f"{safe_lane_id}.jsonl"
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


def test_lifecycle_processing_failures_retry_then_quarantine(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=[
            "  lifecycle_retry_max_attempts: 2",
            "  lifecycle_retry_initial_backoff_seconds: 0",
            "  lifecycle_retry_max_backoff_seconds: 0",
        ],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-2")

        store = LifecycleEventStore(hub_root)
        unprocessed = store.get_unprocessed()
        assert len(unprocessed) == 2
        failing_event_id = unprocessed[0].event_id
        passing_event_id = unprocessed[1].event_id

        original_process = supervisor._process_lifecycle_event
        attempts: dict[str, int] = {}

        def _flaky_process(event) -> None:
            attempts[event.event_id] = attempts.get(event.event_id, 0) + 1
            if event.event_id == failing_event_id:
                raise RuntimeError("characterization failure")
            original_process(event)

        monkeypatch.setattr(supervisor, "_process_lifecycle_event", _flaky_process)

        supervisor.process_lifecycle_events()
        remaining = store.get_unprocessed()
        assert [event.event_id for event in remaining] == [failing_event_id]
        assert attempts[failing_event_id] == 1
        assert attempts[passing_event_id] == 1
        persisted = store.load()
        failing = next(
            event for event in persisted if event.event_id == failing_event_id
        )
        retry_meta = failing.data.get("lifecycle_retry") or {}
        assert retry_meta.get("attempts") == 1
        assert retry_meta.get("status") == "retry_scheduled"
        assert retry_meta.get("quarantined") is False

        supervisor.process_lifecycle_events()
        remaining = store.get_unprocessed()
        assert remaining == []
        assert attempts[failing_event_id] == 2
        persisted = store.load()
        failing = next(
            event for event in persisted if event.event_id == failing_event_id
        )
        retry_meta = failing.data.get("lifecycle_retry") or {}
        assert retry_meta.get("attempts") == 2
        assert retry_meta.get("status") == "quarantined"
        assert retry_meta.get("quarantine_reason") == "max_attempts_exceeded"
        assert retry_meta.get("dead_lettered_at")
        assert failing.processed is True
    finally:
        supervisor.shutdown()


def test_lifecycle_subscription_enqueues_wakeup_payload(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        store = supervisor.get_pma_automation_store()
        _, deduped = store.upsert_subscription(
            event_types=["flow_failed"],
            repo_id="repo-1",
            run_id="run-1",
            from_state="running",
            to_state="failed",
            idempotency_key="sub-flow-failed-1",
        )
        assert deduped is False

        supervisor.lifecycle_emitter.emit_flow_failed(
            "repo-1",
            "run-1",
            data={"from_state": "running", "reason": "flow_error"},
        )
        supervisor.process_lifecycle_events()

        items = _read_queue_items(hub_root)
        assert len(items) == 1
        payload = items[0].get("payload") or {}
        wake_up = payload.get("wake_up") or {}
        assert wake_up.get("repo_id") == "repo-1"
        assert wake_up.get("run_id") == "run-1"
        assert wake_up.get("from_state") == "running"
        assert wake_up.get("to_state") == "failed"
        assert wake_up.get("reason") == "flow_error"
        assert isinstance(wake_up.get("timestamp"), str)
        assert wake_up.get("source") == "lifecycle_subscription"
        assert wake_up.get("event_type") == "flow_failed"
        message = str(payload.get("message") or "")
        assert "source: lifecycle_subscription" in message
        assert "event_type: flow_failed" in message
        assert "suggested_next_action:" in message
    finally:
        supervisor.shutdown()


def test_automation_timer_due_drains_to_pma_queue(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        store = supervisor.get_pma_automation_store()
        _, deduped = store.upsert_timer(
            due_at="2000-01-01T00:00:00+00:00",
            thread_id="thread-123",
            from_state="idle",
            to_state="follow_up",
            reason="timer_due",
            idempotency_key="timer-thread-123",
        )
        assert deduped is False

        created = supervisor.process_pma_automation_timers()
        assert created == 1
        drained = supervisor.drain_pma_automation_wakeups()
        assert drained == 1

        assert supervisor.process_pma_automation_timers() == 0
        supervisor.drain_pma_automation_wakeups()

        items = _read_queue_items(hub_root)
        assert len(items) == 1
        payload = items[0].get("payload") or {}
        wake_up = payload.get("wake_up") or {}
        assert wake_up.get("thread_id") == "thread-123"
        assert wake_up.get("repo_id") is None
        assert wake_up.get("run_id") is None
        assert wake_up.get("from_state") == "idle"
        assert wake_up.get("to_state") == "follow_up"
        assert wake_up.get("reason") == "timer_due"
        assert isinstance(wake_up.get("timestamp"), str)
        assert wake_up.get("source") == "timer"
        assert wake_up.get("timer_id")
        message = str(payload.get("message") or "")
        assert "source: timer" in message
        assert "timer_id:" in message
        assert "/hub/pma/timers/{timer_id}/touch" in message
    finally:
        supervisor.shutdown()


def test_flow_completion_subscription_can_trigger_next_lane(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        store = supervisor.get_pma_automation_store()
        created = store.create_subscription(
            {
                "event_types": ["flow_completed"],
                "repo_id": "repo-1",
                "run_id": "run-1",
                "from_state": "running",
                "to_state": "completed",
                "lane_id": "pma:lane-next",
                "idempotency_key": "sub-next-lane",
            }
        )
        assert created.get("deduped") is False

        supervisor.lifecycle_emitter.emit_flow_completed(
            "repo-1",
            "run-1",
            data={"from_state": "running"},
        )
        supervisor.process_lifecycle_events()

        default_items = _read_queue_items(hub_root, "pma:default")
        next_lane_items = _read_queue_items(hub_root, "pma:lane-next")
        assert default_items == []
        assert len(next_lane_items) == 1
        wake_up = (next_lane_items[0].get("payload") or {}).get("wake_up") or {}
        assert wake_up.get("lane_id") == "pma:lane-next"
        assert wake_up.get("to_state") == "completed"
    finally:
        supervisor.shutdown()


def test_drain_automation_wakeups_requests_lane_worker_start(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root))
    supervisor._stop_lifecycle_event_processor()

    try:
        started_lanes: list[str] = []
        supervisor.set_pma_lane_worker_starter(started_lanes.append)
        store = supervisor.get_pma_automation_store()
        _, deduped = store.enqueue_wakeup(
            source="lifecycle_subscription",
            lane_id="pma:lane-next",
            repo_id="repo-1",
            run_id="run-1",
            from_state="running",
            to_state="completed",
            reason="flow_completed",
            timestamp="2026-01-01T00:00:00Z",
            idempotency_key="wakeup-next-lane",
        )
        assert deduped is False

        drained = supervisor.drain_pma_automation_wakeups()
        assert drained == 1
        assert started_lanes == ["pma:lane-next"]
    finally:
        supervisor.shutdown()
