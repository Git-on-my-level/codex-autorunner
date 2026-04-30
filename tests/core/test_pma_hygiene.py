from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.filebox import ensure_structure, inbox_dir
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.orchestration.bindings import OrchestrationBindingStore
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_automation_store import PmaAutomationStore
from codex_autorunner.core.pma_dispatches import ensure_pma_dispatches_dir
from codex_autorunner.core.pma_hygiene import (
    apply_pma_hygiene_report,
    build_pma_hygiene_report,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.integrations.agents.backend_orchestrator import (
    build_backend_orchestrator,
)
from codex_autorunner.integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from codex_autorunner.manifest import load_manifest
from codex_autorunner.surfaces.cli.pma_cli import pma_app
from tests.conftest import write_test_config
from tests.support.git_test_helpers import init_git_repo as _init_git_repo


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _set_file_mtime(path: Path, when: datetime) -> None:
    timestamp = when.timestamp()
    path.touch()
    os.utime(path, (timestamp, timestamp))


def _write_dispatch(
    hub_root: Path,
    *,
    dispatch_id: str,
    title: str,
    created_at: str,
    resolved_at: str | None = None,
) -> Path:
    dispatch_dir = ensure_pma_dispatches_dir(hub_root)
    resolved_line = f"resolved_at: {resolved_at}\n" if resolved_at else ""
    path = dispatch_dir / f"{dispatch_id}.md"
    path.write_text(
        "---\n"
        f"title: {title}\n"
        "priority: warn\n"
        f"created_at: {created_at}\n"
        f"{resolved_line}"
        "source_turn_id: turn-1\n"
        "---\n\n"
        "Dispatch body.\n",
        encoding="utf-8",
    )
    return path


def _reviewed_thread_cleanup_report(managed_thread_id: str) -> dict[str, object]:
    return {
        "groups": {
            "safe": [],
            "protected": [],
            "needs-confirmation": [
                {
                    "candidate_id": f"threads:{managed_thread_id}",
                    "group": "needs-confirmation",
                    "category": "threads",
                    "label": managed_thread_id,
                    "action": "archive_managed_thread",
                    "reason": "review-approved cleanup",
                    "target": {"managed_thread_id": managed_thread_id},
                    "evidence": {"freshness": {"is_stale": True}},
                }
            ],
        }
    }


def _build_supervisor(hub_root: Path) -> HubSupervisor:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    return HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )


def test_build_pma_hygiene_report_groups_candidates(hub_env) -> None:
    hub_root = hub_env.hub_root
    base_now = datetime.now(timezone.utc)
    report_now = base_now + timedelta(hours=2)
    stale_at = base_now - timedelta(hours=2)

    ensure_structure(hub_root)
    stale_file = inbox_dir(hub_root) / "forgotten.txt"
    stale_file.write_text("leftover", encoding="utf-8")
    _set_file_mtime(stale_file, stale_at)

    thread_store = PmaThreadStore(hub_root)
    unbound = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="stale-unbound",
    )
    bound = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="stale-bound",
    )
    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="discord",
        surface_key="channel-1",
        thread_target_id=bound["managed_thread_id"],
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )

    automation_store = PmaAutomationStore(hub_root)
    active_sub = automation_store.create_subscription(
        {"event_types": ["flow_completed"], "repo_id": hub_env.repo_id}
    )["subscription"]
    inactive_sub = automation_store.create_subscription(
        {"event_types": ["flow_failed"], "repo_id": hub_env.repo_id}
    )["subscription"]
    assert automation_store.cancel_subscription(inactive_sub["subscription_id"]) is True

    pending_timer = automation_store.create_timer(
        {"timer_type": "one_shot", "delay_seconds": 60, "repo_id": hub_env.repo_id}
    )["timer"]
    cancelled_timer = automation_store.create_timer(
        {"timer_type": "one_shot", "delay_seconds": 0, "repo_id": hub_env.repo_id}
    )["timer"]
    assert automation_store.cancel_timer(cancelled_timer["timer_id"]) is True

    pending_wakeup, _ = automation_store.enqueue_wakeup(
        source="transition",
        repo_id=hub_env.repo_id,
        reason="pending",
        timestamp=_iso(stale_at),
    )
    dispatched_wakeup, _ = automation_store.enqueue_wakeup(
        source="transition",
        repo_id=hub_env.repo_id,
        reason="done",
        timestamp=_iso(stale_at),
    )
    assert automation_store.mark_wakeup_dispatched(dispatched_wakeup.wakeup_id) is True

    resolved_dispatch = _write_dispatch(
        hub_root,
        dispatch_id="resolved-alert",
        title="Resolved alert",
        created_at=_iso(stale_at),
        resolved_at=_iso(stale_at),
    )
    _write_dispatch(
        hub_root,
        dispatch_id="open-alert",
        title="Open alert",
        created_at=_iso(stale_at),
        resolved_at=None,
    )

    report = build_pma_hygiene_report(
        hub_root,
        generated_at=_iso(report_now),
        stale_threshold_seconds=60,
    )

    safe_ids = {
        item["candidate_id"]
        for item in report["groups"]["safe"]
        if isinstance(item, dict)
    }
    protected_ids = {
        item["candidate_id"]
        for item in report["groups"]["protected"]
        if isinstance(item, dict)
    }
    needs_confirmation_ids = {
        item["candidate_id"]
        for item in report["groups"]["needs-confirmation"]
        if isinstance(item, dict)
    }

    assert f"automation:subscription:{inactive_sub['subscription_id']}" in safe_ids
    assert f"automation:timer:{cancelled_timer['timer_id']}" in safe_ids
    assert f"automation:wakeup:{dispatched_wakeup.wakeup_id}" in safe_ids
    assert "alerts:resolved-alert" in safe_ids

    assert f"threads:{bound['managed_thread_id']}" in protected_ids
    assert f"automation:subscription:{active_sub['subscription_id']}" in protected_ids
    assert f"automation:timer:{pending_timer['timer_id']}" in protected_ids
    assert f"automation:wakeup:{pending_wakeup.wakeup_id}" in protected_ids
    assert "alerts:open-alert" in protected_ids

    assert "files:inbox:forgotten.txt" in needs_confirmation_ids
    assert f"threads:{unbound['managed_thread_id']}" in needs_confirmation_ids
    assert resolved_dispatch.exists()
    assert report["summary"]["safe_apply_count"] >= 4


def test_apply_pma_hygiene_report_only_removes_safe_items(hub_env) -> None:
    hub_root = hub_env.hub_root
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    stale_at = now - timedelta(hours=2)

    ensure_structure(hub_root)
    stale_file = inbox_dir(hub_root) / "stale.txt"
    stale_file.write_text("stale", encoding="utf-8")
    _set_file_mtime(stale_file, stale_at)

    automation_store = PmaAutomationStore(hub_root)
    inactive_sub = automation_store.create_subscription(
        {"event_types": ["flow_failed"], "repo_id": hub_env.repo_id}
    )["subscription"]
    assert automation_store.cancel_subscription(inactive_sub["subscription_id"]) is True

    inactive_timer = automation_store.create_timer(
        {"timer_type": "one_shot", "delay_seconds": 0, "repo_id": hub_env.repo_id}
    )["timer"]
    assert automation_store.cancel_timer(inactive_timer["timer_id"]) is True

    dispatched_wakeup, _ = automation_store.enqueue_wakeup(
        source="transition",
        repo_id=hub_env.repo_id,
        reason="done",
        timestamp=_iso(stale_at),
    )
    assert automation_store.mark_wakeup_dispatched(dispatched_wakeup.wakeup_id) is True

    resolved_dispatch = _write_dispatch(
        hub_root,
        dispatch_id="resolved-only",
        title="Resolved only",
        created_at=_iso(stale_at),
        resolved_at=_iso(stale_at),
    )

    report = build_pma_hygiene_report(
        hub_root,
        generated_at=_iso(now),
        stale_threshold_seconds=60,
        categories=["files", "automation", "alerts"],
    )
    apply_result = apply_pma_hygiene_report(hub_root, report)

    assert apply_result["failed"] == 0
    assert apply_result["applied"] == apply_result["attempted"]
    assert stale_file.exists()
    assert not resolved_dispatch.exists()
    assert automation_store.list_subscriptions(include_inactive=True) == []
    assert automation_store.list_timers(include_inactive=True) == []
    assert automation_store.list_wakeups() == []


def test_apply_pma_hygiene_report_can_include_reviewed_thread_cleanup(hub_env) -> None:
    hub_root = hub_env.hub_root
    thread_store = PmaThreadStore(hub_root)
    thread = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="reviewed-cleanup-thread",
    )
    managed_thread_id = thread["managed_thread_id"]
    report = _reviewed_thread_cleanup_report(managed_thread_id)

    blocked = apply_pma_hygiene_report(hub_root, report)
    assert blocked["attempted"] == 0
    assert thread_store.get_thread(managed_thread_id)["lifecycle_status"] == "active"

    applied = apply_pma_hygiene_report(
        hub_root, report, include_needs_confirmation=True
    )
    assert applied["attempted"] == 1
    assert applied["safe_attempted"] == 0
    assert applied["reviewed_attempted"] == 1
    assert applied["applied"] == 1
    assert applied["failed"] == 0
    assert thread_store.get_thread(managed_thread_id)["lifecycle_status"] == "archived"


def test_apply_pma_hygiene_report_revalidates_reviewed_thread_binding(hub_env) -> None:
    hub_root = hub_env.hub_root
    thread_store = PmaThreadStore(hub_root)
    thread = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="reviewed-thread-now-bound",
    )
    managed_thread_id = thread["managed_thread_id"]
    report = _reviewed_thread_cleanup_report(managed_thread_id)

    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="github_pr",
        surface_key="Git-on-my-level/codex-autorunner#1302",
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )

    applied = apply_pma_hygiene_report(
        hub_root, report, include_needs_confirmation=True
    )

    assert applied["attempted"] == 1
    assert applied["applied"] == 0
    assert applied["failed"] == 1
    assert (
        applied["results"][0]["error"]
        == "Managed thread cleanup no longer safe: managed thread has an active binding"
    )
    assert thread_store.get_thread(managed_thread_id)["lifecycle_status"] == "active"


def test_apply_pma_hygiene_report_revalidates_reviewed_thread_busy_state(
    hub_env,
) -> None:
    hub_root = hub_env.hub_root
    thread_store = PmaThreadStore(hub_root)
    thread = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="reviewed-thread-now-busy",
    )
    managed_thread_id = thread["managed_thread_id"]
    report = _reviewed_thread_cleanup_report(managed_thread_id)

    thread_store.create_turn(managed_thread_id, prompt="still running")

    applied = apply_pma_hygiene_report(
        hub_root, report, include_needs_confirmation=True
    )

    assert applied["attempted"] == 1
    assert applied["applied"] == 0
    assert applied["failed"] == 1
    assert (
        applied["results"][0]["error"]
        == "Managed thread cleanup no longer safe: managed thread has running work"
    )
    assert thread_store.get_thread(managed_thread_id)["lifecycle_status"] == "active"


def test_apply_pma_hygiene_report_revalidates_reviewed_thread_lifecycle(
    hub_env,
) -> None:
    hub_root = hub_env.hub_root
    thread_store = PmaThreadStore(hub_root)
    thread = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="reviewed-thread-now-archived",
    )
    managed_thread_id = thread["managed_thread_id"]
    report = _reviewed_thread_cleanup_report(managed_thread_id)

    thread_store.archive_thread(managed_thread_id)

    applied = apply_pma_hygiene_report(
        hub_root, report, include_needs_confirmation=True
    )

    assert applied["attempted"] == 1
    assert applied["applied"] == 0
    assert applied["failed"] == 1
    assert (
        applied["results"][0]["error"]
        == "Managed thread cleanup no longer safe: managed thread lifecycle is archived"
    )
    assert thread_store.get_thread(managed_thread_id)["lifecycle_status"] == "archived"


def _bump_thread_status_timestamps(
    hub_root: Path, managed_thread_id: str, when: datetime
) -> None:
    ts = _iso(when)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        conn.execute(
            """
            UPDATE orch_thread_targets
               SET status_updated_at = ?,
                   updated_at = ?
             WHERE thread_target_id = ?
            """,
            (ts, ts, managed_thread_id),
        )
        conn.commit()


def test_build_pma_hygiene_report_blocks_worktree_purge_when_repo_has_fresh_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/mixed-fresh-stale",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    stale_thread = store.create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="stale-only",
    )
    store.create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="still-fresh",
    )
    now = datetime.now(timezone.utc)
    _bump_thread_status_timestamps(
        hub_root, stale_thread["managed_thread_id"], now - timedelta(hours=2)
    )
    # Keep the second thread recent so only one thread is stale for this report.
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        cur = conn.execute(
            """
            SELECT thread_target_id
              FROM orch_thread_targets
             WHERE display_name = 'still-fresh'
            """
        )
        row = cur.fetchone()
        assert row is not None
        _bump_thread_status_timestamps(
            hub_root, str(row[0]), now - timedelta(seconds=30)
        )

    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now),
        stale_threshold_seconds=60,
    )
    protected = [
        item
        for item in report["groups"]["protected"]
        if isinstance(item, dict)
        and item.get("candidate_id") == f"threads:worktree:{worktree.id}"
    ]
    assert len(protected) == 1
    assert "non-stale" in str(protected[0].get("reason") or "").lower()


def test_build_pma_hygiene_report_marks_mixed_idle_followup_stale_worktree_needs_confirmation(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/mixed-followup-states",
        start_point="HEAD",
    )
    store = PmaThreadStore(hub_root)
    idleish = store.create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="idleish",
    )
    failedish = store.create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="failedish",
    )
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    ts = _iso(old)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        conn.execute(
            """
            UPDATE orch_thread_targets
               SET status_updated_at = ?,
                   updated_at = ?
             WHERE thread_target_id = ?
            """,
            (ts, ts, idleish["managed_thread_id"]),
        )
        conn.execute(
            """
            UPDATE orch_thread_targets
               SET runtime_status = 'failed',
                   status_reason = 'test',
                   status_updated_at = ?,
                   updated_at = ?,
                   status_terminal = 0
             WHERE thread_target_id = ?
            """,
            (ts, ts, failedish["managed_thread_id"]),
        )
        conn.commit()

    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now + timedelta(hours=2)),
        stale_threshold_seconds=60,
    )
    needs_confirmation = [
        item
        for item in report["groups"]["needs-confirmation"]
        if isinstance(item, dict)
        and item.get("candidate_id") == f"threads:worktree:{worktree.id}"
    ]
    assert len(needs_confirmation) == 1
    assert (
        "not all stale threads"
        in str(needs_confirmation[0].get("reason") or "").lower()
    )


def test_build_pma_hygiene_report_marks_clean_stale_worktree_safe(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/stale-clean-worktree",
        start_point="HEAD",
    )
    PmaThreadStore(hub_root).create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="stale-clean-thread",
    )

    now = datetime.now(timezone.utc)
    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now + timedelta(hours=2)),
        stale_threshold_seconds=60,
    )

    safe_items = [item for item in report["groups"]["safe"] if isinstance(item, dict)]
    assert {item["candidate_id"] for item in safe_items} == {
        f"threads:worktree:{worktree.id}"
    }
    assert safe_items[0]["action"] == "purge_worktree"
    assert safe_items[0]["target"]["archive_requested"] is True


def test_build_pma_hygiene_report_respects_cleanup_require_archive_false(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    supervisor = HubSupervisor(
        load_hub_config(hub_root),
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
    )
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/no-archive-policy",
        start_point="HEAD",
    )
    PmaThreadStore(hub_root).create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="stale-thread",
    )

    now = datetime.now(timezone.utc)
    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now + timedelta(hours=2)),
        stale_threshold_seconds=60,
    )
    safe_items = [item for item in report["groups"]["safe"] if isinstance(item, dict)]
    assert {item["candidate_id"] for item in safe_items} == {
        f"threads:worktree:{worktree.id}"
    }
    assert safe_items[0]["target"]["archive_requested"] is False


def test_build_pma_hygiene_report_includes_chat_bound_stale_worktree_as_protected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/chat-bound-worktree",
        start_point="HEAD",
    )
    PmaThreadStore(hub_root).create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="stale-chat-bound",
    )

    monkeypatch.setattr(
        "codex_autorunner.core.pma_hygiene.repo_has_active_non_pma_chat_binding",
        lambda **kwargs: True,
    )

    now = datetime.now(timezone.utc)
    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now + timedelta(hours=2)),
        stale_threshold_seconds=60,
    )
    protected = [
        item
        for item in report["groups"]["protected"]
        if isinstance(item, dict)
        and item.get("candidate_id") == f"threads:worktree:{worktree.id}"
    ]
    assert len(protected) == 1
    assert "Chat-bound worktree" in str(protected[0].get("reason") or "")


def test_apply_pma_hygiene_report_purge_worktree_fails_on_error_status_payload(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    report = {
        "groups": {
            "safe": [
                {
                    "candidate_id": "threads:worktree:wt-1",
                    "group": "safe",
                    "category": "threads",
                    "label": "wt-1",
                    "action": "purge_worktree",
                    "reason": "test",
                    "target": {
                        "worktree_repo_id": "wt-1",
                        "archive_requested": False,
                    },
                }
            ],
            "protected": [],
            "needs-confirmation": [],
        }
    }

    def _fake_cleanup(_repo_id: str, _archive: bool) -> dict[str, object]:
        return {"status": "error", "message": "cleanup refused"}

    apply_result = apply_pma_hygiene_report(
        hub_root, report, cleanup_worktree=_fake_cleanup
    )
    assert apply_result["applied"] == 0
    assert apply_result["failed"] == 1
    assert apply_result["results"][0]["status"] == "failed"
    assert apply_result["results"][0]["error"] == "cleanup refused"


def test_build_pma_hygiene_report_marks_dirty_stale_worktree_needs_confirmation(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/stale-dirty-worktree",
        start_point="HEAD",
    )
    PmaThreadStore(hub_root).create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="stale-dirty-thread",
    )
    (worktree.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    now = datetime.now(timezone.utc)
    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now + timedelta(hours=2)),
        stale_threshold_seconds=60,
    )

    needs_confirmation = [
        item
        for item in report["groups"]["needs-confirmation"]
        if isinstance(item, dict)
    ]
    assert {item["candidate_id"] for item in needs_confirmation} == {
        f"threads:worktree:{worktree.id}"
    }
    assert needs_confirmation[0]["action"] == "purge_worktree"


def test_apply_pma_hygiene_report_purges_safe_worktree_candidate(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = _build_supervisor(hub_root)
    base = supervisor.create_repo("base")
    _init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/purge-safe-worktree",
        start_point="HEAD",
    )
    thread_store = PmaThreadStore(hub_root)
    created = thread_store.create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="safe-worktree-thread",
    )

    now = datetime.now(timezone.utc)
    report = build_pma_hygiene_report(
        hub_root,
        categories=["threads"],
        generated_at=_iso(now + timedelta(hours=2)),
        stale_threshold_seconds=60,
    )
    apply_result = apply_pma_hygiene_report(
        hub_root,
        report,
        cleanup_worktree=lambda repo_id, archive: supervisor.cleanup_worktree(
            worktree_repo_id=repo_id,
            archive=archive,
        ),
    )

    assert apply_result["attempted"] == 1
    assert apply_result["applied"] == 1
    assert apply_result["failed"] == 0
    assert not worktree.path.exists()
    assert (
        load_manifest(
            load_hub_config(hub_root).manifest_path,
            hub_root,
        ).get(worktree.id)
        is None
    )
    assert thread_store.get_thread(created["managed_thread_id"])[
        "lifecycle_status"
    ] == ("archived")


def test_build_pma_hygiene_report_canonicalizes_category_order(hub_env) -> None:
    report = build_pma_hygiene_report(
        hub_env.hub_root,
        categories=["automation", "threads"],
    )

    assert report["categories"] == ["threads", "automation"]


def test_pma_hygiene_cli_outputs_json_report(hub_env) -> None:
    ensure_structure(hub_env.hub_root)
    stale_file = inbox_dir(hub_env.hub_root) / "cli-stale.txt"
    stale_file.write_text("stale", encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _set_file_mtime(stale_file, old)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "hygiene",
            "--path",
            str(hub_env.hub_root),
            "--category",
            "files",
            "--stale-threshold-seconds",
            "60",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "files:inbox:cli-stale.txt" in result.stdout


def test_pma_hygiene_cli_summary_only_output(hub_env) -> None:
    ensure_structure(hub_env.hub_root)
    stale_file = inbox_dir(hub_env.hub_root) / "summary-stale.txt"
    stale_file.write_text("stale", encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _set_file_mtime(stale_file, old)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "hygiene",
            "--path",
            str(hub_env.hub_root),
            "--category",
            "files",
            "--stale-threshold-seconds",
            "60",
            "--summary",
        ],
    )

    assert result.exit_code == 0
    assert "Summary:" in result.stdout
    assert "Category counts:" in result.stdout
    assert "files:inbox:summary-stale.txt" not in result.stdout
