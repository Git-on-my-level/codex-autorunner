from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.automation import (
    EXECUTOR_PMA_OPERATOR_TURN,
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from codex_autorunner.core.automation.builtins import _normalize_reactive_event_types
from codex_autorunner.core.automation.models import (
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.lifecycle_events import LifecycleEventStore
from codex_autorunner.core.pma_automation_records import (
    PmaAutomationTimer,
    PmaLifecycleSubscription,
)
from codex_autorunner.manifest import load_manifest, save_manifest


def _write_hub_config(
    hub_root: Path,
    *,
    dispatch_interception: bool,
    extra_lines: list[str] | None = None,
) -> None:
    hub_root.mkdir(parents=True, exist_ok=True)
    config_dir = hub_root / ".codex-autorunner"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yml"
    lines = [
        "version: 2",
        "mode: hub",
        "pma:",
        "  enabled: true",
        f"  dispatch_interception_enabled: {'true' if dispatch_interception else 'false'}",
    ]
    if extra_lines:
        lines.extend(extra_lines)
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


def _automation_jobs(hub_root: Path) -> list[dict[str, object]]:
    return [job.to_dict() for job in AutomationStore(hub_root).list_jobs()]


def _upsert_subscription_rule(
    hub_root: Path, subscription: PmaLifecycleSubscription
) -> None:
    subscription_id = subscription.subscription_id
    filters = {
        path: value
        for path, value in (
            ("event.repo_id", subscription.repo_id),
            ("event.payload.run_id", subscription.run_id),
            ("event.payload.thread_id", subscription.thread_id),
            ("event.payload.from_state", subscription.from_state),
            ("event.payload.to_state", subscription.to_state),
        )
        if value is not None
    }
    AutomationStore(hub_root).upsert_rule(
        AutomationRule.create(
            rule_id=f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}",
            name=f"PMA subscription {subscription_id}",
            enabled=subscription.state == "active",
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={
                "event_types": _normalize_reactive_event_types(subscription.event_types)
            },
            filters=filters,
            target_policy=TARGET_POLICY_HUB,
            target={
                "repo_id": subscription.repo_id,
                "run_id": subscription.run_id,
                "thread_id": subscription.thread_id,
            },
            executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
            executor={
                "wake_up_kind": "pma_subscription",
                "source": "transition",
                "subscription_id": subscription_id,
                "lane_id": subscription.lane_id,
                "event_type": "{{ event.payload.event_type }}",
                "repo_id": "{{ event.repo_id }}",
                "run_id": "{{ event.payload.run_id }}",
                "thread_id": "{{ event.payload.thread_id }}",
                "from_state": "{{ event.payload.from_state }}",
                "to_state": "{{ event.payload.to_state }}",
                "reason": "{{ event.payload.reason }}",
                "timestamp": "{{ event.raw_payload.timestamp }}",
                "message_text": (
                    "Automation wake-up received.\n"
                    "source: transition\n"
                    "event_type: {{ event.payload.event_type }}\n"
                    f"subscription_id: {subscription_id}\n"
                    "repo_id: {{ event.repo_id }}\n"
                    "run_id: {{ event.payload.run_id }}\n"
                    "thread_id: {{ event.payload.thread_id }}\n"
                    "from_state: {{ event.payload.from_state }}\n"
                    "to_state: {{ event.payload.to_state }}\n"
                    "reason: {{ event.payload.reason }}\n"
                    "timestamp: {{ event.raw_payload.timestamp }}\n"
                    "suggested_next_action: inspect the transition and adjust "
                    "the generalized automation rule or schedule as needed."
                ),
                **dict(subscription.metadata or {}),
            },
            policy={
                "dedupe_key": f"pma-subscription:{subscription_id}:{{{{ event.event_id }}}}",
                "approval_mode": "pause_and_request_user",
                "max_attempts": 3,
                "max_concurrent_per_rule": 1,
                "max_concurrent_per_target": 1,
            },
            metadata={
                "builtin": True,
                "purpose": "pma_lifecycle_subscription",
                "subscription_id": subscription_id,
                "idempotency_key": subscription.idempotency_key,
                "reason": subscription.reason,
                "max_matches": subscription.max_matches,
                "match_count": subscription.match_count,
                "metadata": dict(subscription.metadata or {}),
            },
            created_at=subscription.created_at,
            updated_at=subscription.updated_at,
        )
    )


def _upsert_timer_schedule(hub_root: Path, timer: PmaAutomationTimer) -> None:
    timer_id = timer.timer_id
    store = AutomationStore(hub_root)
    rule = store.upsert_rule(
        AutomationRule.create(
            rule_id=f"{PMA_TIMER_RULE_PREFIX}{timer_id}",
            name=f"PMA timer {timer_id}",
            enabled=timer.state == "pending",
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["schedule.fire"]},
            filters={"schedule.rule_id": f"{PMA_TIMER_RULE_PREFIX}{timer_id}"},
            target_policy=TARGET_POLICY_HUB,
            target={
                "repo_id": timer.repo_id,
                "run_id": timer.run_id,
                "thread_id": timer.thread_id,
            },
            executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
            executor={
                "message_text": (
                    "Automation wake-up received.\n"
                    "source: timer\n"
                    f"timer_id: {timer_id}\n"
                    "repo_id: {{ schedule.payload.repo_id }}\n"
                    "run_id: {{ schedule.payload.run_id }}\n"
                    "thread_id: {{ schedule.payload.thread_id }}\n"
                    "suggested_next_action: verify progress, then inspect or pause "
                    "the generalized automation schedule as needed."
                ),
                "wake_up_kind": "pma_timer",
            },
            policy={
                "dedupe_key": f"pma-timer:{timer_id}:{{{{ schedule.next_fire_at }}}}",
                "approval_mode": "pause_and_request_user",
                "max_attempts": 3,
                "max_concurrent_per_rule": 1,
                "max_concurrent_per_target": 1,
            },
            metadata={
                "builtin": True,
                "purpose": "pma_timer",
                "timer_id": timer_id,
                "idempotency_key": timer.idempotency_key,
            },
            created_at=timer.created_at,
            updated_at=timer.updated_at,
        )
    )
    store.upsert_schedule(
        AutomationSchedule.create(
            schedule_id=f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}",
            rule_id=rule.rule_id,
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at=timer.due_at if timer.state == "pending" else None,
            last_fire_at=timer.fired_at,
            schedule={
                "timer_id": timer_id,
                "timer_kind": timer.timer_type,
                "payload": {
                    "timer_id": timer_id,
                    "timer_type": timer.timer_type,
                    "repo_id": timer.repo_id,
                    "run_id": timer.run_id,
                    "thread_id": timer.thread_id,
                    "lane_id": timer.lane_id,
                    "from_state": timer.from_state,
                    "to_state": timer.to_state,
                    "reason": timer.reason,
                    "metadata": dict(timer.metadata or {}),
                },
            },
            state="active" if timer.state == "pending" else timer.state,
            created_at=timer.created_at,
            updated_at=timer.updated_at,
        )
    )


def test_lifecycle_dispatch_auto_resolve_does_not_enqueue_pma(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root, dispatch_interception=True)
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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

        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        event = (jobs[0].get("payload") or {}).get("event") or {}
        event_payload = event.get("payload") or {}
        assert event_payload.get("event_type") == "dispatch_created"
        assert event_payload.get("repo_id") == "repo-1"
    finally:
        supervisor.shutdown()


def test_lifecycle_flow_failed_enqueues_pma(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root, dispatch_interception=False)
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        event = (jobs[0].get("payload") or {}).get("event") or {}
        event_payload = event.get("payload") or {}
        assert event_payload.get("event_type") == "flow_failed"
        assert event_payload.get("repo_id") == "repo-1"
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_disabled_skips_enqueue(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        event = (jobs[0].get("payload") or {}).get("event") or {}
        event_payload = event.get("payload") or {}
        assert event_payload.get("event_type") == "flow_failed"
    finally:
        supervisor.shutdown()


def test_lifecycle_reactive_origin_blocklist(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_origin_blocklist:", "    - pma"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-1")
        supervisor.lifecycle_emitter.emit_flow_failed("repo-1", "run-2")
        supervisor.process_lifecycle_events()

        store = LifecycleEventStore(hub_root)
        assert store.get_unprocessed() == []

        assert len(_automation_jobs(hub_root)) == 2
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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        subscription = PmaLifecycleSubscription.create(
            event_types=["flow_failed"],
            repo_id="repo-1",
            run_id="run-1",
            from_state="running",
            to_state="failed",
            idempotency_key="sub-flow-failed-1",
        )
        _upsert_subscription_rule(hub_root, subscription)

        supervisor.lifecycle_emitter.emit_flow_failed(
            "repo-1",
            "run-1",
            data={"from_state": "running", "reason": "flow_error"},
        )
        supervisor.process_lifecycle_events()

        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        executor = jobs[0].get("executor") or {}
        assert executor.get("repo_id") == "repo-1"
        assert executor.get("run_id") == "run-1"
        assert executor.get("from_state") == "running"
        assert executor.get("to_state") == "failed"
        assert executor.get("reason") == "flow_error"
        assert isinstance(executor.get("timestamp"), str)
        assert executor.get("source") == "transition"
        assert executor.get("event_type") == "flow_failed"
        message = str(executor.get("message_text") or "")
        assert "source: transition" in message
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
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        timer = PmaAutomationTimer.create(
            due_at="2000-01-01T00:00:00+00:00",
            thread_id="thread-123",
            from_state="idle",
            to_state="follow_up",
            reason="timer_due",
            idempotency_key="timer-thread-123",
        )
        _upsert_timer_schedule(hub_root, timer)

        supervisor.process_automation_timers()
        assert len(_automation_jobs(hub_root)) == 1

        assert supervisor.process_automation_timers() == 0

        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        executor = jobs[0].get("executor") or {}
        target = jobs[0].get("target") or {}
        assert target.get("thread_id") == "thread-123"
        assert target.get("repo_id") is None
        assert target.get("run_id") is None
        message = str(executor.get("message_text") or "")
        assert "thread_id: thread-123" in message
        assert "source: timer" in message
        assert "timer_id:" in message
        assert "generalized automation schedule" in message
    finally:
        supervisor.shutdown()


def test_flow_completion_subscription_can_trigger_next_lane(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        subscription = PmaLifecycleSubscription.create(
            event_types=["flow_completed"],
            repo_id="repo-1",
            run_id="run-1",
            from_state="running",
            to_state="completed",
            lane_id="pma:lane-next",
            idempotency_key="sub-next-lane",
        )
        _upsert_subscription_rule(hub_root, subscription)

        supervisor.lifecycle_emitter.emit_flow_completed(
            "repo-1",
            "run-1",
            data={"from_state": "running"},
        )
        supervisor.process_lifecycle_events()

        assert _read_queue_items(hub_root, "pma:default") == []
        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        executor = jobs[0].get("executor") or {}
        assert executor.get("to_state") == "completed"
    finally:
        supervisor.shutdown()


def test_drain_automation_wakeup_copies_delivery_target_from_subscription(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(
        hub_root,
        dispatch_interception=False,
        extra_lines=["  reactive_enabled: false"],
    )
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        subscription = PmaLifecycleSubscription.create(
            event_types=["flow_completed"],
            repo_id="repo-1",
            run_id="run-1",
            metadata={
                "delivery_target": {
                    "surface_kind": "discord",
                    "surface_key": "discord:channel-1",
                }
            },
            idempotency_key="sub-delivery-target",
        )
        _upsert_subscription_rule(hub_root, subscription)

        supervisor.lifecycle_emitter.emit_flow_completed("repo-1", "run-1")
        supervisor.process_lifecycle_events()

        jobs = _automation_jobs(hub_root)
        assert len(jobs) == 1
        executor = jobs[0].get("executor") or {}
        assert executor.get("delivery_target") == {
            "surface_kind": "discord",
            "surface_key": "discord:channel-1",
        }
    finally:
        supervisor.shutdown()
