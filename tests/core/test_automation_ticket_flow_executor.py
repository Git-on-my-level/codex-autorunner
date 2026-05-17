from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.automation import (
    EXECUTOR_TICKET_FLOW,
    AutomationEvent,
    AutomationExecutorRegistry,
    AutomationJob,
    AutomationJobWorker,
    AutomationRule,
    AutomationStore,
)
from codex_autorunner.core.automation.models import (
    JOB_RUNNING,
    TARGET_POLICY_AUTO_WORKTREE,
    TARGET_POLICY_EXISTING_REPO,
    TARGET_POLICY_EXISTING_WORKTREE,
    TARGET_POLICY_HUB,
    TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
    TARGET_POLICY_PR_WORKTREE,
    TRIGGER_KIND_EVENT,
)
from codex_autorunner.core.automation.ticket_flow_executor import (
    TicketFlowAutomationExecutor,
    automation_branch_name,
    automation_repo_id,
)
from codex_autorunner.core.hub_topology import (
    HubTopologyRepository,
    LockStatus,
    RepoSnapshot,
    RepoStatus,
)
from codex_autorunner.manifest import MANIFEST_VERSION, Manifest, save_manifest


def _ticket(ticket_id: str = "tkt_ticket_001") -> str:
    return f"""---
ticket_id: "{ticket_id}"
agent: "codex"
done: false
title: "Do work"
---

## Goal
Do the work.
"""


def _job(*, target: dict, executor: dict | None = None) -> AutomationJob:
    return AutomationJob.create(
        job_id="job-abcdef1234567890",
        rule_id="Rule One",
        event_id="event-1",
        target=target,
        executor={"kind": EXECUTOR_TICKET_FLOW, **(executor or {})},
        payload={"event": {"payload": {"pr": {"number": 42}}}},
    )


def _snapshot(repo_id: str, path: Path, *, branch: str) -> RepoSnapshot:
    return RepoSnapshot(
        id=repo_id,
        path=path,
        display_name=repo_id,
        enabled=True,
        auto_run=False,
        worktree_setup_commands=None,
        kind="worktree",
        worktree_of="base",
        branch=branch,
        exists_on_disk=True,
        is_clean=True,
        initialized=True,
        init_error=None,
        status=RepoStatus.IDLE,
        lock_status=LockStatus.UNLOCKED,
        last_run_id=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        last_exit_code=None,
        runner_pid=None,
    )


class _FakeWorktreeManager:
    def __init__(self, hub_root: Path) -> None:
        self.hub_root = hub_root
        self.calls: list[dict] = []

    def create_worktree(
        self, *, base_repo_id: str, branch: str, **_kwargs
    ) -> RepoSnapshot:
        self.calls.append({"base_repo_id": base_repo_id, "branch": branch})
        repo_id = automation_repo_id(base_repo_id, branch)
        path = self.hub_root / "worktrees" / repo_id
        base_path = self.hub_root / "repos" / base_repo_id
        if base_path.exists():
            shutil.copytree(base_path, path, dirs_exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
        manifest = _load_manifest(self.hub_root)
        manifest.ensure_repo(
            self.hub_root,
            path,
            repo_id=repo_id,
            kind="worktree",
            worktree_of=base_repo_id,
            branch=branch,
        )
        save_manifest(
            self.hub_root / ".codex-autorunner" / "manifest.yml",
            manifest,
            self.hub_root,
        )
        return _snapshot(repo_id, path, branch=branch)


async def _fake_start(repo_root: Path, **_kwargs):
    assert repo_root.exists()
    return SimpleNamespace(id="run-1")


def _run(coro):
    return asyncio.run(coro)


def _load_manifest(hub_root: Path) -> Manifest:
    from codex_autorunner.manifest import load_manifest

    return load_manifest(hub_root / ".codex-autorunner" / "manifest.yml", hub_root)


def _executor(
    hub_root: Path, fake_wm: _FakeWorktreeManager
) -> TicketFlowAutomationExecutor:
    return TicketFlowAutomationExecutor(
        hub_root=hub_root,
        topology_repository=HubTopologyRepository(
            hub_root=hub_root,
            manifest_path=hub_root / ".codex-autorunner" / "manifest.yml",
        ),
        worktree_manager=fake_wm,  # type: ignore[arg-type]
        start_flow_run_fn=_fake_start,
        run_coroutine_fn=_run,
    )


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    base = hub_root / "repos" / "base"
    base.mkdir(parents=True)
    manifest = Manifest(version=MANIFEST_VERSION, repos=[])
    manifest.ensure_repo(hub_root, base, repo_id="base", kind="base")
    save_manifest(hub_root / ".codex-autorunner" / "manifest.yml", manifest, hub_root)
    return hub_root


@pytest.mark.parametrize(
    "policy",
    [
        TARGET_POLICY_EXISTING_REPO,
        TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
        TARGET_POLICY_AUTO_WORKTREE,
    ],
)
def test_ticket_flow_executor_creates_isolated_worktree_for_base_targets(
    hub: Path, policy: str
) -> None:
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": policy, "repo_id": "base"},
        executor={
            "ticket_pack": {
                "source": "inline",
                "tickets": [{"path": "TICKET-001.md", "content": _ticket()}],
            }
        },
    )

    result = executor.execute(job)

    assert result.status == JOB_RUNNING
    assert result.data["execution_phase"] == "running"
    assert result.execution_refs["ticket_flow_repo_id"].startswith("base--automation-")
    worktree = Path(result.data["worktree"]["path"])
    assert (worktree / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert not (hub / "repos" / "base" / ".codex-autorunner" / "tickets").exists()
    assert fake_wm.calls == [
        {
            "base_repo_id": "base",
            "branch": automation_branch_name(job, target=job.target, policy=policy),
        }
    ]


def test_ticket_flow_executor_uses_existing_worktree_as_base_selector(
    hub: Path,
) -> None:
    manifest = _load_manifest(hub)
    existing_path = hub / "worktrees" / "base--feature"
    existing_path.mkdir(parents=True)
    manifest.ensure_repo(
        hub,
        existing_path,
        repo_id="base--feature",
        kind="worktree",
        worktree_of="base",
        branch="feature",
    )
    save_manifest(hub / ".codex-autorunner" / "manifest.yml", manifest, hub)
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": TARGET_POLICY_EXISTING_WORKTREE, "repo_id": "base--feature"},
        executor={
            "ticket_pack": {
                "source": "inline",
                "tickets": [{"path": "TICKET-001.md", "content": _ticket()}],
            }
        },
    )

    result = executor.execute(job)

    assert result.data["worktree"]["base_repo_id"] == "base"
    assert fake_wm.calls[0]["base_repo_id"] == "base"
    assert Path(result.data["worktree"]["path"]) != existing_path


def test_pr_worktree_branch_is_pr_specific(hub: Path) -> None:
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": TARGET_POLICY_PR_WORKTREE, "repo_id": "base"},
        executor={
            "ticket_pack": {
                "source": "inline",
                "tickets": [{"path": "TICKET-001.md", "content": _ticket()}],
            }
        },
    )

    executor.execute(job)

    assert fake_wm.calls[0]["branch"] == "automation/pr-42/rule-one"


def test_ticket_flow_executor_rejects_hub_target_before_materializing(
    hub: Path,
) -> None:
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": TARGET_POLICY_HUB},
        executor={
            "ticket_pack": {
                "source": "inline",
                "tickets": [{"path": "TICKET-001.md", "content": _ticket()}],
            }
        },
    )

    with pytest.raises(ValueError, match="requires a repo/worktree target"):
        executor.execute(job)

    assert fake_wm.calls == []


def test_ticket_flow_executor_rejects_unsafe_materialized_paths(hub: Path) -> None:
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "base"},
        executor={
            "ticket_pack": {
                "source": "inline",
                "tickets": [{"path": "../TICKET-001.md", "content": _ticket()}],
            }
        },
    )

    with pytest.raises(ValueError, match="unsafe materialized path"):
        executor.execute(job)


def test_ticket_flow_executor_rejects_duplicate_ticket_ids(hub: Path) -> None:
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "base"},
        executor={
            "ticket_pack": {
                "source": "inline",
                "tickets": [
                    {"path": "TICKET-001-a.md", "content": _ticket("tkt_duplicate")},
                    {"path": "TICKET-002-b.md", "content": _ticket("tkt_duplicate")},
                ],
            }
        },
    )

    with pytest.raises(ValueError, match="Duplicate ticket_id"):
        executor.execute(job)


def test_ticket_flow_executor_supports_repo_relative_pack_path(hub: Path) -> None:
    pack_dir = hub / "repos" / "base" / "packs" / "demo" / ".codex-autorunner"
    (pack_dir / "tickets").mkdir(parents=True)
    (pack_dir / "contextspace").mkdir(parents=True)
    (pack_dir / "tickets" / "TICKET-001.md").write_text(_ticket(), encoding="utf-8")
    (pack_dir / "contextspace" / "spec.md").write_text("# Spec\n", encoding="utf-8")
    fake_wm = _FakeWorktreeManager(hub)
    executor = _executor(hub, fake_wm)
    job = _job(
        target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "base"},
        executor={
            "ticket_pack": {
                "source": "repo_relative_path",
                "path": "packs/demo",
            }
        },
    )

    result = executor.execute(job)

    worktree = Path(result.data["worktree"]["path"])
    assert (worktree / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert (worktree / ".codex-autorunner" / "contextspace" / "spec.md").exists()
    assert result.execution_refs == {
        "ticket_flow_repo_id": result.data["worktree"]["repo_id"],
        "ticket_flow_worktree_id": result.data["worktree"]["repo_id"],
        "ticket_flow_run_id": "run-1",
    }


def test_ticket_flow_worker_keeps_job_running_after_child_run_start(
    hub: Path,
) -> None:
    store = AutomationStore(hub)
    store.upsert_rule(
        AutomationRule.create(
            rule_id="rule-1",
            name="Ticket flow",
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": ["manual.run"]},
            target_policy=TARGET_POLICY_EXISTING_REPO,
            executor_kind=EXECUTOR_TICKET_FLOW,
        )
    )
    store.record_event(
        AutomationEvent.create(event_id="event-1", event_type="manual.run")
    )
    store.enqueue_job(
        AutomationJob.create(
            job_id="job-1",
            rule_id="rule-1",
            event_id="event-1",
            target={"policy": TARGET_POLICY_EXISTING_REPO, "repo_id": "base"},
            executor={
                "kind": EXECUTOR_TICKET_FLOW,
                "ticket_pack": {
                    "source": "inline",
                    "tickets": [{"path": "TICKET-001.md", "content": _ticket()}],
                },
            },
            available_at="2026-01-01T00:00:00Z",
        )
    )
    fake_wm = _FakeWorktreeManager(hub)
    registry = AutomationExecutorRegistry()
    registry.register(EXECUTOR_TICKET_FLOW, _executor(hub, fake_wm))

    result = AutomationJobWorker(store, registry).process_once(
        now="2026-01-01T00:00:00Z"
    )

    saved = store.get_job("job-1")
    attempt = store.list_attempts("job-1")[0]
    assert result.running == 1
    assert result.succeeded == 0
    assert saved.state == JOB_RUNNING
    assert saved.lock_key is None
    assert saved.claimed_at is None
    assert saved.finished_at is None
    assert saved.ticket_flow_run_id == "run-1"
    assert saved.ticket_flow_repo_id == saved.ticket_flow_worktree_id
    assert attempt.status == JOB_RUNNING
    assert attempt.executor_result["execution_phase"] == "running"

    released = store.release_stale_claims(
        stale_before="2026-01-01T00:05:00Z", now="2026-01-01T00:10:00Z"
    )
    assert released == 0
    assert store.get_job("job-1").state == JOB_RUNNING
