from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from codex_autorunner.core.automation import (
    AutomationRuleEngine,
    AutomationStore,
    ensure_builtin_pma_reactive_rule,
)
from codex_autorunner.core.config import HubConfig
from codex_autorunner.core.hub_lifecycle_routing import LifecycleEventRouter
from codex_autorunner.core.hub_topology import LockStatus, RepoSnapshot, RepoStatus
from codex_autorunner.core.lifecycle_events import (
    LifecycleEvent,
    LifecycleEventType,
)


def _make_hub_config(tmp_path: Path, *, pma_enabled: bool = True) -> HubConfig:
    config_dir = tmp_path / ".codex-autorunner"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yml").write_text(
        f"version: 2\nmode: hub\npma:\n  enabled: {str(pma_enabled).lower()}\n",
        encoding="utf-8",
    )
    from codex_autorunner.core.config import load_hub_config

    return load_hub_config(tmp_path)


def _make_snapshot(repo_id: str, *, exists_on_disk: bool = True) -> RepoSnapshot:
    return RepoSnapshot(
        id=repo_id,
        path=Path(f"/tmp/{repo_id}"),
        display_name=repo_id,
        enabled=True,
        auto_run=False,
        worktree_setup_commands=None,
        kind="base",
        worktree_of=None,
        branch=None,
        exists_on_disk=exists_on_disk,
        is_clean=None,
        initialized=False,
        init_error=None,
        status=RepoStatus.UNINITIALIZED,
        lock_status=LockStatus.UNLOCKED,
        last_run_id=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        last_exit_code=None,
        runner_pid=None,
    )


class _StubStore:
    def __init__(self) -> None:
        self.marked_processed_ids: list[str] = []
        self.prune_calls: int = 0

    def mark_processed(self, event_id: str) -> None:
        self.marked_processed_ids.append(event_id)

    def prune_processed(self, *, keep_last: int = 100) -> None:
        self.prune_calls += 1


def _make_router(
    tmp_path: Path,
    *,
    hub_config: Optional[HubConfig] = None,
    list_repos_fn=None,
    pma_enabled: bool = True,
    automation_rule_engine: Optional[AutomationRuleEngine] = None,
) -> tuple[LifecycleEventRouter, _StubStore]:
    config = hub_config or _make_hub_config(tmp_path, pma_enabled=pma_enabled)
    store = _StubStore()

    def _run_coro(coro):
        return (
            asyncio.get_event_loop().run_until_complete(coro)
            if asyncio.get_event_loop().is_running()
            else asyncio.run(coro)
        )

    router = LifecycleEventRouter(
        hub_config=config,
        lifecycle_store=store,
        list_repos_fn=list_repos_fn or (lambda: []),
        run_coroutine_fn=_run_coro,
        automation_rule_engine=automation_rule_engine,
        logger=logging.getLogger("test.hub_lifecycle_routing"),
    )
    return router, store


def test_route_event_skips_processed_events(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_COMPLETED,
        repo_id="repo-1",
        run_id="run-1",
        processed=True,
    )
    router.route_event(event)
    assert store.marked_processed_ids == []


def test_route_event_marks_dispatch_pma_disabled(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path, pma_enabled=False)
    event = LifecycleEvent(
        event_type=LifecycleEventType.DISPATCH_CREATED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_marks_dispatch_repo_missing(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.DISPATCH_CREATED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_marks_dispatch_repo_not_on_disk(tmp_path: Path) -> None:
    snapshot = _make_snapshot("repo-1", exists_on_disk=False)
    router, store = _make_router(tmp_path, list_repos_fn=lambda: [snapshot])
    router, store = _make_router(tmp_path, list_repos_fn=lambda: [snapshot])
    event = LifecycleEvent(
        event_type=LifecycleEventType.DISPATCH_CREATED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_flow_completed_enqueues_pma(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_COMPLETED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_flow_completed_records_unified_event_and_job(
    tmp_path: Path,
) -> None:
    config = _make_hub_config(tmp_path)
    automation_store = AutomationStore(tmp_path)
    ensure_builtin_pma_reactive_rule(automation_store, pma_config=config.pma)
    router, store = _make_router(
        tmp_path,
        hub_config=config,
        automation_rule_engine=AutomationRuleEngine(automation_store),
    )
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_COMPLETED,
        repo_id="repo-1",
        run_id="run-1",
    )

    router.route_event(event)

    assert event.event_id in store.marked_processed_ids
    saved_event = automation_store.get_event(f"lifecycle:{event.event_id}")
    assert saved_event is not None
    assert saved_event.event_type == "lifecycle.flow_completed"
    job = automation_store.list_jobs()[0]
    assert job.executor["kind"] == "pma_turn"
    assert job.dedupe_key == f"lifecycle:{event.event_id}"
    assert job.policy["reactive_debounce_key"].endswith(":repo-1:run-1")


def test_route_event_records_unified_event_when_pma_disabled(
    tmp_path: Path,
) -> None:
    config = _make_hub_config(tmp_path, pma_enabled=False)
    automation_store = AutomationStore(tmp_path)
    ensure_builtin_pma_reactive_rule(automation_store, pma_config=config.pma)
    router, store = _make_router(
        tmp_path,
        hub_config=config,
        automation_rule_engine=AutomationRuleEngine(automation_store),
    )
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_FAILED,
        repo_id="repo-1",
        run_id="run-1",
    )

    router.route_event(event)

    assert event.event_id in store.marked_processed_ids
    saved_event = automation_store.get_event(f"lifecycle:{event.event_id}")
    assert saved_event is not None
    assert saved_event.event_type == "lifecycle.flow_failed"
    assert automation_store.list_jobs() == []


def test_route_event_flow_failed_marks_pma_disabled(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path, pma_enabled=False)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_FAILED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_flow_paused_enqueues_pma(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_PAUSED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_flow_stopped_enqueues_pma(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_STOPPED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_flow_started_enqueues_pma(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_STARTED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_flow_resumed_enqueues_pma(tmp_path: Path) -> None:
    router, store = _make_router(tmp_path)
    event = LifecycleEvent(
        event_type=LifecycleEventType.FLOW_RESUMED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_build_transition_payload_defaults_for_flow_started_and_resumed(
    tmp_path: Path,
) -> None:
    router, _ = _make_router(tmp_path)

    started_payload = router._build_transition_payload(
        LifecycleEvent(
            event_type=LifecycleEventType.FLOW_STARTED,
            repo_id="repo-1",
            run_id="run-1",
        )
    )
    resumed_payload = router._build_transition_payload(
        LifecycleEvent(
            event_type=LifecycleEventType.FLOW_RESUMED,
            repo_id="repo-1",
            run_id="run-1",
        )
    )

    assert started_payload["from_state"] == "pending"
    assert started_payload["to_state"] == "running"
    assert resumed_payload["from_state"] == "paused"
    assert resumed_payload["to_state"] == "running"


def test_list_repos_failure_does_not_crash_dispatch(tmp_path: Path) -> None:
    def _fail_list():
        raise RuntimeError("listing failed")

    router, store = _make_router(tmp_path, list_repos_fn=_fail_list)
    event = LifecycleEvent(
        event_type=LifecycleEventType.DISPATCH_CREATED,
        repo_id="repo-1",
        run_id="run-1",
    )
    router.route_event(event)
    assert event.event_id in store.marked_processed_ids


def test_route_event_creates_dispatch_interceptor_lazily(tmp_path: Path) -> None:
    config = _make_hub_config(tmp_path)
    config.pma.dispatch_interception_enabled = True
    router, _ = _make_router(tmp_path, hub_config=config)
    assert router._dispatch_interceptor is None
    result = router._ensure_dispatch_interceptor()
    assert result is not None
    assert router._dispatch_interceptor is result


def test_route_event_no_dispatch_interceptor_when_disabled(tmp_path: Path) -> None:
    config = _make_hub_config(tmp_path)
    config.pma.dispatch_interception_enabled = False
    router, _ = _make_router(tmp_path, hub_config=config)
    assert router._ensure_dispatch_interceptor() is None
