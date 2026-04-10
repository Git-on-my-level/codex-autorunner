from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from tests.conftest import write_test_config

import codex_autorunner.core.scm_polling_watches as scm_polling_watches
import codex_autorunner.integrations.github.polling as github_polling
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.pr_binding_runtime import upsert_pr_binding
from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.core.publish_executor import PublishOperationProcessor
from codex_autorunner.core.publish_journal import PublishJournalStore
from codex_autorunner.core.publish_operation_executors import (
    build_enqueue_managed_turn_executor,
    build_notify_chat_executor,
)
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEventStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.github.polling import (
    GitHubPollingConfig,
    GitHubScmPollingService,
)
from codex_autorunner.integrations.github.publisher import (
    build_react_pr_review_comment_executor,
)
from codex_autorunner.integrations.github.service import GitHubError, RepoInfo


class _GitHubServiceStub:
    def __init__(
        self,
        repo_root: Path,
        raw_config: dict | None = None,
        *,
        pr_view_payload: dict[str, object],
        reviews_payload: list[dict[str, object]],
        checks_payload: list[dict[str, object]],
        issue_comments_payload: list[dict[str, object]] | None = None,
        review_threads_payload: list[dict[str, object]] | None = None,
        rate_limit_payload: dict[str, object] | None = None,
        pr_view_exception: Exception | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.raw_config = raw_config or {}
        self._pr_view_payload = pr_view_payload
        self._reviews_payload = reviews_payload
        self._checks_payload = checks_payload
        self._issue_comments_payload = issue_comments_payload or []
        self._review_threads_payload = review_threads_payload or []
        self._rate_limit_payload = rate_limit_payload or {}
        self._pr_view_exception = pr_view_exception

    def pr_view(self, *, number: int, cwd=None, repo_slug=None) -> dict[str, object]:
        _ = number, cwd, repo_slug
        if self._pr_view_exception is not None:
            raise self._pr_view_exception
        return dict(self._pr_view_payload)

    def pr_reviews(self, *, owner: str, repo: str, number: int, cwd=None):
        _ = owner, repo, number, cwd
        return list(self._reviews_payload)

    def pr_checks(self, *, number: int, cwd=None):
        _ = number, cwd
        return list(self._checks_payload)

    def issue_comments(
        self,
        *,
        owner: str,
        repo: str,
        number: int | None = None,
        since=None,
        limit: int = 100,
        cwd=None,
    ):
        _ = owner, repo, number, since, limit, cwd
        return list(self._issue_comments_payload)

    def pr_review_threads(self, *, owner: str, repo: str, number: int, cwd=None):
        _ = owner, repo, number, cwd
        return list(self._review_threads_payload)

    def rate_limit_status(self) -> dict[str, object]:
        return dict(self._rate_limit_payload)


class _AutomationServiceFake:
    ingested_events: list[tuple[str, dict[str, object], dict[str, object]]] = []
    process_calls = 0

    def __init__(self, hub_root: Path, *, reaction_config=None, **kwargs) -> None:
        _ = hub_root, kwargs
        self._reaction_config = dict(reaction_config or {})

    def ingest_event(self, event) -> None:
        self.ingested_events.append(
            (event.event_type, dict(event.payload), dict(self._reaction_config))
        )

    def process_now(self, limit: int = 10):
        _ = limit
        type(self).process_calls += 1
        return []


class _DiscoveringGitHubServiceStub(_GitHubServiceStub):
    def __init__(
        self,
        repo_root: Path,
        raw_config: dict | None = None,
        *,
        hub_root: Path,
        repo_id: str | None,
        repo_slug: str,
        pr_number: int,
        head_branch: str,
        base_branch: str = "main",
        pr_state: str = "open",
        discover: bool = True,
    ) -> None:
        super().__init__(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": pr_state == "draft",
                "headRefOid": "abc123",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
        )
        self._hub_root = hub_root
        self._repo_id = repo_id
        self._repo_slug = repo_slug
        self._pr_number = pr_number
        self._head_branch = head_branch
        self._base_branch = base_branch
        self._pr_state = pr_state
        self._discover = discover

    def repo_info(self) -> RepoInfo:
        return RepoInfo(
            name_with_owner=self._repo_slug,
            url=f"https://github.com/{self._repo_slug}",
        )

    def discover_pr_binding(self, *, branch=None, cwd=None):
        _ = branch, cwd
        if not self._discover:
            return None
        return PrBindingStore(self._hub_root).upsert_binding(
            provider="github",
            repo_slug=self._repo_slug,
            repo_id=self._repo_id,
            pr_number=self._pr_number,
            pr_state=self._pr_state,
            head_branch=self._head_branch,
            base_branch=self._base_branch,
        )


def _write_discord_binding(
    db_path: Path,
    *,
    channel_id: str,
    workspace_path: str,
    repo_id: str | None = None,
    updated_at: str = "2026-03-30T00:00:00Z",
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    workspace_path TEXT,
                    repo_id TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO channel_bindings (
                    channel_id, workspace_path, repo_id, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    workspace_path=excluded.workspace_path,
                    repo_id=excluded.repo_id,
                    updated_at=excluded.updated_at
                """,
                (channel_id, workspace_path, repo_id, updated_at),
            )
    finally:
        conn.close()


def _write_manifest(hub_root: Path, *, repo_rel: str, repo_id: str = "repo-1") -> None:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos:",
                f"  - id: {repo_id}",
                f"    path: {repo_rel}",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _polling_config(
    *,
    profile: str | None = None,
    discovery_interval_seconds: int = 360,
    discovery_workspace_limit: int = 1,
) -> dict[str, object]:
    reactions: dict[str, object] = {}
    if profile is not None:
        reactions["profile"] = profile
    return {
        "github": {
            "automation": {
                "polling": {
                    "enabled": True,
                    "discovery_interval_seconds": discovery_interval_seconds,
                    "discovery_workspace_limit": discovery_workspace_limit,
                    "watch_window_minutes": 30,
                    "interval_seconds": 90,
                },
                "reactions": reactions,
            }
        }
    }


def _rate_limit_payload(
    *,
    graphql_remaining: int,
    core_remaining: int = 5000,
    limit: int = 5000,
    reset_epoch: int = 2147483647,
) -> dict[str, object]:
    return {
        "resources": {
            "graphql": {
                "remaining": graphql_remaining,
                "limit": limit,
                "reset": reset_epoch,
            },
            "core": {
                "remaining": core_remaining,
                "limit": limit,
                "reset": reset_epoch,
            },
        }
    }


def test_arm_watch_captures_baseline_and_minimal_noise_profile(
    tmp_path: Path,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "abc123",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[
                {
                    "review_id": "rev-1",
                    "review_state": "CHANGES_REQUESTED",
                    "author_login": "reviewer",
                    "body": "Please tighten the polling scope.",
                    "submitted_at": "2026-03-30T01:00:00Z",
                }
            ],
            checks_payload=[
                {
                    "name": "unit-tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "details_url": "https://example.invalid/checks/1",
                }
            ],
            issue_comments_payload=[
                {
                    "comment_id": "comment-1",
                    "body": "Please wire PR comments into polling too.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T01:05:00Z",
                }
            ],
            review_threads_payload=[
                {
                    "thread_id": "thread-1",
                    "isResolved": False,
                    "comments": [
                        {
                            "comment_id": "review-comment-1",
                            "body": "Please cover inline review comments.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 140,
                            "updated_at": "2026-03-30T01:06:00Z",
                        }
                    ],
                }
            ],
        )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(profile="minimal_noise"),
        github_service_factory=_factory,
    )

    watch = service.arm_watch(binding=binding, workspace_root=tmp_path / "repo")

    assert watch is not None
    assert watch.snapshot["head_sha"] == "abc123"
    assert "baseline_pending" not in watch.snapshot
    assert sorted(watch.snapshot["changes_requested_reviews"]) == ["rev-1"]
    assert len(watch.snapshot["failed_checks"]) == 1
    assert sorted(watch.snapshot["issue_comments"]) == ["comment-1"]
    assert sorted(watch.snapshot["review_thread_comments"]) == ["review-comment-1"]
    assert watch.reaction_config["ci_failed"] is True
    assert watch.reaction_config["changes_requested"] is True
    assert watch.reaction_config["review_comment"] is True
    assert watch.reaction_config["approved_and_green"] is False
    assert watch.reaction_config["merged"] is False


def test_arm_watch_backfills_recent_review_comments_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "abc123",
                "createdAt": "2026-03-30T00:45:00Z",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            issue_comments_payload=[
                {
                    "comment_id": "comment-old",
                    "body": "Old comment should stay in the baseline.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:20:00Z",
                },
                {
                    "comment_id": "comment-new",
                    "body": "Recent PR conversation comment should backfill.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:55:00Z",
                },
            ],
            review_threads_payload=[
                {
                    "thread_id": "thread-1",
                    "isResolved": False,
                    "comments": [
                        {
                            "comment_id": "review-comment-new",
                            "body": "Recent inline review comment should backfill.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 140,
                            "updated_at": "2026-03-30T00:56:00Z",
                        }
                    ],
                }
            ],
        )

    monkeypatch.setattr(github_polling, "now_iso", lambda: "2026-03-30T01:00:00Z")
    monkeypatch.setattr(scm_polling_watches, "now_iso", lambda: "2026-03-30T01:00:00Z")
    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        event_store=ScmEventStore(tmp_path),
    )

    watch = service.arm_watch(binding=binding, workspace_root=tmp_path / "repo")

    assert watch is not None
    assert sorted(watch.snapshot["issue_comments"]) == ["comment-new", "comment-old"]
    assert sorted(watch.snapshot["review_thread_comments"]) == ["review-comment-new"]
    assert watch.snapshot["pr_created_at"] == "2026-03-30T00:45:00Z"
    assert [item[0] for item in _AutomationServiceFake.ingested_events] == [
        "issue_comment",
        "pull_request_review_comment",
    ]
    assert _AutomationServiceFake.process_calls == 1
    events = ScmEventStore(tmp_path).list_events(limit=10)
    assert len(events) == 2
    assert {event.payload["comment_id"] for event in events} == {
        "comment-new",
        "review-comment-new",
    }


def test_arm_watch_backfill_uses_current_arm_time_for_reactivated_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    monkeypatch.setattr(scm_polling_watches, "now_iso", lambda: "2026-03-30T00:00:00Z")
    existing_watch = watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:01:30Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"head_sha": "oldsha"},
    )
    watch_store.close_watch(watch_id=existing_watch.watch_id, state="closed")

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "abc123",
                "createdAt": "2026-03-30T00:00:00Z",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            issue_comments_payload=[
                {
                    "comment_id": "comment-old",
                    "body": "Comment from the previous watch window.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:20:00Z",
                },
                {
                    "comment_id": "comment-new",
                    "body": "Comment from the current arm window.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:55:00Z",
                },
            ],
        )

    monkeypatch.setattr(github_polling, "now_iso", lambda: "2026-03-30T01:00:00Z")
    monkeypatch.setattr(scm_polling_watches, "now_iso", lambda: "2026-03-30T01:00:00Z")
    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    watch = service.arm_watch(binding=binding, workspace_root=tmp_path / "repo")

    assert watch is not None
    assert watch.started_at == "2026-03-30T00:00:00Z"
    assert [item[0] for item in _AutomationServiceFake.ingested_events] == [
        "issue_comment"
    ]
    events = ScmEventStore(tmp_path).list_events(limit=10)
    assert len(events) == 1
    assert events[0].payload["comment_id"] == "comment-new"


def test_backfill_binding_thread_targets_claims_active_matching_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True)
    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        metadata={"head_branch": "feature/backfill"},
    )
    PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/backfill",
        base_branch="main",
    )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
    )

    counts = service.backfill_binding_thread_targets(limit=10)
    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
    )

    assert counts["threads_scanned"] == 1
    assert counts["bindings_updated"] == 1
    assert binding is not None
    assert binding.thread_target_id == thread["managed_thread_id"]


def test_backfill_binding_thread_targets_skips_already_bound_binding(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True)
    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        metadata={"head_branch": "feature/backfill"},
    )
    original = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/backfill",
        base_branch="main",
        thread_target_id=str(thread["managed_thread_id"]),
    )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
    )

    counts = service.backfill_binding_thread_targets(limit=10)
    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
    )

    assert counts["threads_scanned"] == 1
    assert counts["bindings_matched"] == 1
    assert counts["bindings_updated"] == 0
    assert binding is not None
    assert binding.thread_target_id == thread["managed_thread_id"]
    assert binding.updated_at == original.updated_at


def test_process_due_watches_emits_only_new_review_and_check_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={
            "head_sha": "oldsha",
            "pr_state": "open",
            "changes_requested_reviews": {
                "rev-1": {
                    "action": "submitted",
                    "review_id": "rev-1",
                    "review_state": "CHANGES_REQUESTED",
                }
            },
            "failed_checks": {
                "oldsha:unit-tests:failure:https://example.invalid/checks/1": {
                    "action": "completed",
                    "name": "unit-tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": "oldsha",
                    "details_url": "https://example.invalid/checks/1",
                }
            },
        },
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
            },
            reviews_payload=[
                {
                    "review_id": "rev-1",
                    "review_state": "CHANGES_REQUESTED",
                    "author_login": "reviewer",
                    "body": "Original feedback",
                    "submitted_at": "2026-03-30T00:05:00Z",
                },
                {
                    "review_id": "rev-2",
                    "review_state": "CHANGES_REQUESTED",
                    "author_login": "reviewer",
                    "body": "Please add dedupe coverage.",
                    "submitted_at": "2026-03-30T00:10:00Z",
                },
            ],
            checks_payload=[
                {
                    "name": "unit-tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "details_url": "https://example.invalid/checks/2",
                }
            ],
        )

    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    result = service.process_due_watches(limit=10)

    assert result["due"] == 1
    assert result["polled"] == 1
    assert result["events_emitted"] == 2
    assert _AutomationServiceFake.process_calls == 1
    assert [item[0] for item in _AutomationServiceFake.ingested_events] == [
        "pull_request_review",
        "check_run",
    ]

    events = ScmEventStore(tmp_path).list_events(limit=10)
    assert len(events) == 2
    assert {event.event_type for event in events} == {
        "pull_request_review",
        "check_run",
    }


def test_process_due_watches_emits_new_pr_comment_and_inline_review_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={
            "head_sha": "oldsha",
            "pr_state": "open",
            "issue_comments": {
                "comment-1": {
                    "action": "created",
                    "comment_id": "comment-1",
                    "author_login": "reviewer",
                    "issue_author_login": "pr-author",
                    "body": "Existing PR conversation comment.",
                    "updated_at": "2026-03-30T00:01:00Z",
                }
            },
            "review_thread_comments": {
                "review-comment-1": {
                    "action": "created",
                    "comment_id": "review-comment-1",
                    "author_login": "reviewer",
                    "issue_author_login": "pr-author",
                    "body": "Existing inline thread comment.",
                    "path": "src/codex_autorunner/integrations/github/polling.py",
                    "line": 140,
                    "updated_at": "2026-03-30T00:02:00Z",
                }
            },
        },
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            issue_comments_payload=[
                {
                    "comment_id": "comment-1",
                    "body": "Existing PR conversation comment.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:01:00Z",
                },
                {
                    "comment_id": "comment-2",
                    "body": "Please wake up on PR comments from polling too.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:03:00Z",
                },
            ],
            review_threads_payload=[
                {
                    "thread_id": "thread-1",
                    "isResolved": False,
                    "comments": [
                        {
                            "comment_id": "review-comment-1",
                            "body": "Existing inline thread comment.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 140,
                            "updated_at": "2026-03-30T00:02:00Z",
                        },
                        {
                            "comment_id": "review-comment-2",
                            "body": "Please also wake up on new inline comments.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 196,
                            "updated_at": "2026-03-30T00:04:00Z",
                        },
                    ],
                },
                {
                    "thread_id": "thread-2",
                    "isResolved": True,
                    "comments": [
                        {
                            "comment_id": "review-comment-resolved",
                            "body": "Resolved thread should not retrigger polling.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 210,
                            "updated_at": "2026-03-30T00:05:00Z",
                        }
                    ],
                },
            ],
        )

    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    result = service.process_due_watches(limit=10)

    assert result["events_emitted"] == 2
    assert _AutomationServiceFake.process_calls == 1
    assert [item[0] for item in _AutomationServiceFake.ingested_events] == [
        "issue_comment",
        "pull_request_review_comment",
    ]

    events = ScmEventStore(tmp_path).list_events(limit=10)
    assert len(events) == 2
    assert {event.event_type for event in events} == {
        "issue_comment",
        "pull_request_review_comment",
    }


def test_process_due_watches_reacts_then_wakes_thread_and_notifies_bound_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = hub_root / "repo"
    workspace_root.mkdir(parents=True)
    write_test_config(
        hub_root / CONFIG_FILENAME,
        DEFAULT_HUB_CONFIG,
    )
    _write_manifest(hub_root, repo_rel="repo")

    async def _bind_discord() -> None:
        store = DiscordStateStore(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3"
        )
        try:
            await store.initialize()
            await store.upsert_binding(
                channel_id="repo-discord",
                guild_id=None,
                workspace_path=str(workspace_root),
                repo_id="repo-1",
            )
        finally:
            await store.close()

    asyncio.run(_bind_discord())
    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        metadata={"head_branch": "feature/scm-polling"},
    )
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
        thread_target_id=str(thread["managed_thread_id"]),
    )
    watch_store = ScmPollingWatchStore(hub_root)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        repo_id=binding.repo_id,
        pr_number=binding.pr_number,
        workspace_root=str(workspace_root.resolve()),
        thread_target_id=str(thread["managed_thread_id"]),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={
            "head_sha": "oldsha",
            "pr_state": "open",
            "review_thread_comments": {},
        },
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            issue_comments_payload=[],
            review_threads_payload=[
                {
                    "thread_id": "thread-1",
                    "isResolved": False,
                    "comments": [
                        {
                            "comment_id": "2844",
                            "body": "Please cover the inline review wakeup path too.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 196,
                            "updated_at": "2026-03-30T00:04:00Z",
                        }
                    ],
                }
            ],
        )

    automation_config = _polling_config()
    automation_config["github"]["automation"]["policy"] = {
        "react_pr_review_comment": "allow"
    }
    reaction_calls: list[tuple[str, str, str, int, str]] = []

    class _ReactionGitHubService:
        def __init__(self, repo_root: Path, raw_config=None) -> None:
            _ = raw_config
            self.repo_root = repo_root

        def create_pull_request_review_comment_reaction(
            self,
            *,
            owner: str,
            repo: str,
            comment_id: int,
            content: str,
            cwd=None,
        ) -> dict[str, object]:
            reaction_calls.append((owner, repo, str(cwd), comment_id, content))
            return {
                "id": 88,
                "content": content,
                "url": "https://api.github.com/reactions/88",
            }

    journal = PublishJournalStore(hub_root)
    processor = PublishOperationProcessor(
        journal,
        executors={
            "react_pr_review_comment": build_react_pr_review_comment_executor(
                repo_root=hub_root,
                raw_config=automation_config,
                github_service_factory=_ReactionGitHubService,
            ),
            "enqueue_managed_turn": build_enqueue_managed_turn_executor(
                hub_root=hub_root
            ),
            "notify_chat": build_notify_chat_executor(hub_root=hub_root),
        },
    )
    processed_operations = []
    automation = ScmAutomationService(
        hub_root,
        reaction_config=automation_config,
        publish_processor=processor,
    )

    class _AutomationWrapper:
        def ingest_event(self, event) -> None:
            automation.ingest_event(event)

        def process_now(self, limit: int = 10):
            result = automation.process_now(limit=limit)
            processed_operations.extend(result)
            return result

    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationWrapper(),  # type: ignore[misc]
    )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=automation_config,
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process_due_watches(limit=10)

    assert result["events_emitted"] == 1
    assert [operation.operation_kind for operation in processed_operations] == [
        "react_pr_review_comment",
        "enqueue_managed_turn",
        "notify_chat",
    ]
    notify_result = processed_operations[2]
    assert notify_result.state == "succeeded"
    assert notify_result.response["delivery"] == "bound"
    assert notify_result.response["repo_id"] == "repo-1"
    assert notify_result.response["route"] == "bound"
    assert notify_result.response["targets"] == 1
    assert notify_result.response["published"] == 1
    assert reaction_calls == [("acme", "widgets", str(hub_root), 2844, "eyes")]

    turns = PmaThreadStore(hub_root).list_turns(thread["managed_thread_id"], limit=10)
    assert len(turns) == 1

    async def _load_outbox() -> list[object]:
        store = DiscordStateStore(
            hub_root / ".codex-autorunner" / "discord_state.sqlite3"
        )
        try:
            return await store.list_outbox()
        finally:
            await store.close()

    outbox = asyncio.run(_load_outbox())
    assert any(
        record.channel_id == "repo-discord"
        and "taking a look" in str(record.payload_json.get("content", "")).lower()
        and "inline review wakeup path"
        in str(record.payload_json.get("content", "")).lower()
        for record in outbox
    )


def test_process_due_watches_does_not_reemit_when_thread_is_reopened_without_new_comments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={
            "head_sha": "oldsha",
            "pr_state": "open",
            "review_thread_comments": {
                "review-comment-1": {
                    "action": "created",
                    "comment_id": "review-comment-1",
                    "author_login": "reviewer",
                    "issue_author_login": "pr-author",
                    "body": "Existing inline thread comment.",
                    "path": "src/codex_autorunner/integrations/github/polling.py",
                    "line": 140,
                    "thread_resolved": True,
                    "updated_at": "2026-03-30T00:02:00Z",
                }
            },
        },
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            review_threads_payload=[
                {
                    "thread_id": "thread-1",
                    "isResolved": False,
                    "comments": [
                        {
                            "comment_id": "review-comment-1",
                            "body": "Existing inline thread comment.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 140,
                            "updated_at": "2026-03-30T00:02:00Z",
                        }
                    ],
                }
            ],
        )

    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    result = service.process_due_watches(limit=10)

    assert result["events_emitted"] == 0
    assert _AutomationServiceFake.ingested_events == []


def test_process_due_watches_uses_first_successful_poll_as_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"baseline_pending": True},
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
            },
            reviews_payload=[
                {
                    "review_id": "rev-2",
                    "review_state": "CHANGES_REQUESTED",
                    "author_login": "reviewer",
                    "body": "Please add dedupe coverage.",
                    "submitted_at": "2026-03-30T00:10:00Z",
                },
            ],
            checks_payload=[],
        )

    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    result = service.process_due_watches(limit=10)

    assert result["events_emitted"] == 0
    assert _AutomationServiceFake.ingested_events == []
    refreshed = watch_store.list_due_watches(limit=10)
    assert refreshed == []


def test_process_due_watches_backfills_recent_comments_when_baseline_was_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    monkeypatch.setattr(scm_polling_watches, "now_iso", lambda: "2026-03-30T01:00:00Z")
    watch_store = ScmPollingWatchStore(tmp_path)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"baseline_pending": True},
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
                "createdAt": "2026-03-30T00:45:00Z",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            issue_comments_payload=[
                {
                    "comment_id": "comment-old",
                    "body": "Old comment should stay in the baseline.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:20:00Z",
                },
                {
                    "comment_id": "comment-new",
                    "body": "Recent PR conversation comment should backfill.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T01:05:00Z",
                },
            ],
            review_threads_payload=[
                {
                    "thread_id": "thread-1",
                    "isResolved": False,
                    "comments": [
                        {
                            "comment_id": "review-comment-new",
                            "body": "Recent inline review comment should backfill.",
                            "author_login": "reviewer",
                            "author_type": "User",
                            "path": "src/codex_autorunner/integrations/github/polling.py",
                            "line": 140,
                            "updated_at": "2026-03-30T01:06:00Z",
                        }
                    ],
                }
            ],
        )

    monkeypatch.setattr(github_polling, "now_iso", lambda: "2026-03-30T01:10:00Z")
    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    result = service.process_due_watches(limit=10)

    assert result["events_emitted"] == 2
    assert [item[0] for item in _AutomationServiceFake.ingested_events] == [
        "issue_comment",
        "pull_request_review_comment",
    ]
    assert _AutomationServiceFake.process_calls == 1
    events = ScmEventStore(tmp_path).list_events(limit=10)
    assert len(events) == 2
    assert {event.payload["comment_id"] for event in events} == {
        "comment-new",
        "review-comment-new",
    }


def test_process_due_watches_uses_current_poll_time_for_deferred_baseline_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    monkeypatch.setattr(scm_polling_watches, "now_iso", lambda: "2026-03-30T00:00:00Z")
    existing_watch = watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"baseline_pending": True},
    )
    watch_store.close_watch(watch_id=existing_watch.watch_id, state="closed")
    monkeypatch.setattr(scm_polling_watches, "now_iso", lambda: "2026-03-30T01:00:00Z")
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"baseline_pending": True},
    )

    def _factory(repo_root: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
                "createdAt": "2026-03-30T00:00:00Z",
                "author": {"login": "pr-author"},
            },
            reviews_payload=[],
            checks_payload=[],
            issue_comments_payload=[
                {
                    "comment_id": "comment-old",
                    "body": "Comment from the previous watch window.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:20:00Z",
                },
                {
                    "comment_id": "comment-new",
                    "body": "Comment from the current poll window.",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "updated_at": "2026-03-30T00:55:00Z",
                },
            ],
        )

    monkeypatch.setattr(github_polling, "now_iso", lambda: "2026-03-30T01:00:00Z")
    _AutomationServiceFake.ingested_events = []
    _AutomationServiceFake.process_calls = 0
    monkeypatch.setattr(
        GitHubScmPollingService,
        "_build_automation_service",
        lambda self, reaction_config=None: _AutomationServiceFake(  # type: ignore[misc]
            tmp_path,
            reaction_config=reaction_config,
        ),
    )

    service = GitHubScmPollingService(
        tmp_path,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(tmp_path),
    )

    result = service.process_due_watches(limit=10)

    assert result["events_emitted"] == 1
    assert [item[0] for item in _AutomationServiceFake.ingested_events] == [
        "issue_comment"
    ]
    events = ScmEventStore(tmp_path).list_events(limit=10)
    assert len(events) == 1
    assert events[0].payload["comment_id"] == "comment-new"


def test_claim_due_watches_prevents_duplicate_claims(
    tmp_path: Path,
) -> None:
    binding = PrBindingStore(tmp_path).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
        pr_state="open",
        head_branch="feature/scm-polling",
        base_branch="main",
    )
    watch_store = ScmPollingWatchStore(tmp_path)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        workspace_root=str((tmp_path / "repo").resolve()),
        poll_interval_seconds=90,
        next_poll_at="2026-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"baseline_pending": True},
    )

    claimed = watch_store.claim_due_watches(
        provider="github",
        limit=10,
        now_timestamp="2026-03-30T00:00:00Z",
    )

    assert len(claimed) == 1
    assert claimed[0].next_poll_at == "2026-03-30T00:01:30Z"
    assert (
        watch_store.claim_due_watches(
            provider="github",
            limit=10,
            now_timestamp="2026-03-30T00:00:00Z",
        )
        == []
    )


def test_process_discovers_external_pr_binding_and_arms_watch_from_manifest_workspace(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=42,
            head_branch="feature/external-pr",
            discover=True,
        )

    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["candidate_workspaces"] == 1
    assert result["bindings_discovered"] == 1
    assert result["watches_armed"] == 1
    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=42,
    )
    assert binding is not None
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None
    assert watch.state == "active"
    assert watch.workspace_root == str(repo_root.resolve())


def test_process_arms_watch_for_existing_binding_without_discovery(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=43,
        pr_state="open",
        head_branch="feature/missing-watch",
        base_branch="main",
    )

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=43,
            head_branch="feature/missing-watch",
            discover=False,
        )

    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["bindings_discovered"] == 0
    assert result["watches_armed"] == 1
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None
    assert watch.state == "active"
    assert watch.workspace_root == str(repo_root.resolve())


def test_process_discovers_external_pr_binding_from_discord_bound_unregistered_workspace(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "worktrees" / "repo-1--discord-1"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/base", repo_id="repo-1")
    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-1",
        workspace_path=str(repo_root.resolve()),
        repo_id=None,
    )

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id=None,
            repo_slug="acme/widgets",
            pr_number=43,
            head_branch="feature/missing-watch",
            discover=repo_root_arg == repo_root,
        )

    raw_config = _polling_config()
    raw_config["discord_bot"] = {"enabled": True}
    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=raw_config,
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["candidate_workspaces"] == 1
    assert result["bindings_discovered"] == 1
    assert result["watches_armed"] == 1
    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=43,
    )
    assert binding is not None
    assert binding.repo_id is None
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None
    assert watch.state == "active"
    assert watch.workspace_root == str(repo_root.resolve())


def test_process_uses_managed_thread_head_branch_hint_for_external_pr_discovery(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "worktrees" / "repo-1--codex-1"
    repo_root.mkdir(parents=True)
    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        repo_root,
        repo_id="repo-1",
        metadata={"head_branch": "feature/non-ticket-flow"},
    )

    class _BranchHintGitHubServiceStub(_DiscoveringGitHubServiceStub):
        def discover_pr_binding(self, *, branch=None, cwd=None):
            _ = cwd
            if branch != self._head_branch:
                return None
            return upsert_pr_binding(
                self._hub_root,
                provider="github",
                repo_slug=self._repo_slug,
                repo_id=self._repo_id,
                pr_number=self._pr_number,
                pr_state=self._pr_state,
                head_branch=self._head_branch,
                base_branch=self._base_branch,
            )

    def _factory(repo_root_arg: Path, raw_config=None) -> _BranchHintGitHubServiceStub:
        return _BranchHintGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=44,
            head_branch="feature/non-ticket-flow",
            discover=True,
        )

    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["candidate_workspaces"] == 1
    assert result["bindings_discovered"] == 1
    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=44,
    )
    assert binding is not None
    assert binding.thread_target_id == thread["managed_thread_id"]
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None
    assert watch.state == "active"
    assert watch.workspace_root == str(repo_root.resolve())


def test_process_repairs_active_watch_workspace_root_without_resetting_snapshot(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=44,
        pr_state="open",
        head_branch="feature/repair-watch",
        base_branch="main",
    )
    stale_root = hub_root / "workspace" / "missing"

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=44,
            head_branch="feature/repair-watch",
            discover=False,
        )

    watch_store = ScmPollingWatchStore(hub_root)
    watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        repo_id=binding.repo_id,
        pr_number=binding.pr_number,
        workspace_root=str(stale_root),
        thread_target_id=binding.thread_target_id,
        poll_interval_seconds=90,
        next_poll_at="2099-03-30T00:00:00Z",
        expires_at="2099-03-30T01:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"head_sha": "abc123", "pr_state": "open"},
    )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["watches_armed"] == 1
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None
    assert watch.workspace_root == str(repo_root.resolve())
    assert watch.snapshot == {"head_sha": "abc123", "pr_state": "open"}


def test_process_skips_malformed_active_binding_rows_without_crashing(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=45,
        pr_state="open",
        head_branch="feature/valid-binding",
        base_branch="main",
    )
    with open_orchestration_sqlite(hub_root) as conn:
        conn.execute(
            """
            INSERT INTO orch_pr_bindings (
                binding_id,
                provider,
                repo_slug,
                repo_id,
                pr_number,
                pr_state,
                head_branch,
                base_branch,
                thread_target_id,
                created_at,
                updated_at,
                closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bad-binding",
                "github",
                "acme/bad",
                "repo-bad",
                "not-a-number",
                "open",
                "feature/bad",
                "main",
                None,
                "2026-03-30T00:00:00Z",
                "2026-03-30T00:00:00Z",
                None,
            ),
        )

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=45,
            head_branch="feature/valid-binding",
            discover=False,
        )

    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["invalid_bindings_skipped"] == 1
    assert result["watches_armed"] == 1
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None


def test_process_defers_baseline_when_rate_limit_budget_is_low(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=46,
        pr_state="open",
        head_branch="feature/rate-limited",
        base_branch="main",
    )

    def _factory(repo_root_arg: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root_arg,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "abc123",
            },
            reviews_payload=[],
            checks_payload=[],
            rate_limit_payload=_rate_limit_payload(graphql_remaining=0),
        )

    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process(limit=10)

    assert result["watches_armed"] == 1
    assert result["rate_limited_skipped"] == 1
    watch = watch_store.get_watch(provider="github", binding_id=binding.binding_id)
    assert watch is not None
    assert watch.snapshot == {"baseline_pending": True}
    assert watch.last_error_text == "GitHub rate-limit budget low; baseline deferred"


def test_quota_state_cache_persists_across_poll_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)

    def _factory(repo_root_arg: Path, raw_config=None) -> _GitHubServiceStub:
        calls["count"] += 1
        return _GitHubServiceStub(
            repo_root_arg,
            raw_config,
            pr_view_payload={},
            reviews_payload=[],
            checks_payload=[],
            rate_limit_payload=_rate_limit_payload(graphql_remaining=5000),
        )

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
    )
    first = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=ScmPollingWatchStore(hub_root),
        event_store=ScmEventStore(hub_root),
    )._quota_state_for_workspace(workspace_root=repo_root, cache={})

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2026, 4, 7, 10, 1, 30, tzinfo=timezone.utc),
    )
    second = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=ScmPollingWatchStore(hub_root),
        event_store=ScmEventStore(hub_root),
    )._quota_state_for_workspace(workspace_root=repo_root, cache={})

    assert first is not None
    assert second == first
    assert calls["count"] == 1


def test_rate_limit_error_invalidates_cached_quota_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    binding = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=49,
        pr_state="open",
        head_branch="feature/rate-limit-reset",
        base_branch="main",
    )

    watch_store = ScmPollingWatchStore(hub_root)
    watch = watch_store.upsert_watch(
        provider="github",
        binding_id=binding.binding_id,
        repo_slug=binding.repo_slug,
        repo_id=binding.repo_id,
        pr_number=binding.pr_number,
        workspace_root=str(repo_root.resolve()),
        thread_target_id=binding.thread_target_id,
        poll_interval_seconds=90,
        next_poll_at="2026-04-07T10:00:00Z",
        expires_at="2099-04-07T11:00:00Z",
        reaction_config={"enabled": True},
        snapshot={"baseline_pending": True},
    )
    assert watch is not None

    calls = {"rate_limit_status": 0}
    should_raise = {"value": True}

    class _CountingGitHubServiceStub(_GitHubServiceStub):
        def rate_limit_status(self) -> dict[str, object]:
            calls["rate_limit_status"] += 1
            return super().rate_limit_status()

    def _factory(repo_root_arg: Path, raw_config=None) -> _GitHubServiceStub:
        pr_view_exception = None
        if should_raise["value"]:
            pr_view_exception = GitHubError(
                "Command failed: gh pr view 49 --json ...: API rate limit exceeded",
                status_code=429,
            )
        return _CountingGitHubServiceStub(
            repo_root_arg,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "abc123",
            },
            reviews_payload=[],
            checks_payload=[],
            rate_limit_payload=_rate_limit_payload(graphql_remaining=5000),
            pr_view_exception=pr_view_exception,
        )

    first_service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
    )
    first = first_service.process_due_watches(limit=10)

    watch_store.refresh_watch(
        watch_id=watch.watch_id,
        next_poll_at="2026-04-07T10:01:30Z",
    )
    should_raise["value"] = False

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2026, 4, 7, 10, 1, 30, tzinfo=timezone.utc),
    )
    second = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    ).process_due_watches(limit=10)

    assert first["rate_limited_skipped"] == 1
    assert second["polled"] == 1
    assert calls["rate_limit_status"] == 2


def test_quota_cache_persistence_preserves_discovery_cycle_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)

    def _factory(repo_root_arg: Path, raw_config=None) -> _GitHubServiceStub:
        return _GitHubServiceStub(
            repo_root_arg,
            raw_config,
            pr_view_payload={},
            reviews_payload=[],
            checks_payload=[],
            rate_limit_payload=_rate_limit_payload(graphql_remaining=5000),
        )

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
    )
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=ScmPollingWatchStore(hub_root),
        event_store=ScmEventStore(hub_root),
    )

    assert service._claim_discovery_cycle(
        polling_config=GitHubPollingConfig.from_mapping(_polling_config())
    )
    assert (
        service._quota_state_for_workspace(workspace_root=repo_root, cache={})
        is not None
    )

    state = github_polling.read_json(
        hub_root / ".codex-autorunner" / "github_polling_state.json"
    )
    assert isinstance(state, dict)
    assert isinstance(state.get("last_discovery_cycle_slot"), int)
    assert state.get("quota_state_cache") is not None


def test_process_rotates_discovery_across_candidate_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    roots = [hub_root / "workspace" / f"repo-{index}" for index in range(4)]
    for root in roots:
        root.mkdir(parents=True)

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-4",
            repo_slug="acme/rotated",
            pr_number=64,
            head_branch="feature/rotated-discovery",
            discover=repo_root_arg == roots[3],
        )

    watch_store = ScmPollingWatchStore(hub_root)
    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(
            discovery_interval_seconds=180,
            discovery_workspace_limit=2,
        ),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    monkeypatch.setattr(
        service,
        "_candidate_workspace_roots",
        lambda: (list(roots), {}, {}, {}),
    )
    monkeypatch.setattr(
        service,
        "_thread_activity",
        lambda: ({}, {}),
    )
    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2099, 4, 5, 8, 0, 0, tzinfo=timezone.utc),
    )
    first = service.process(limit=2)

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2099, 4, 5, 8, 1, 0, tzinfo=timezone.utc),
    )
    skipped = service.process(limit=2)

    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2099, 4, 5, 8, 3, 0, tzinfo=timezone.utc),
    )
    second = service.process(limit=2)

    assert first["candidate_workspaces_scanned"] == 2
    assert first["bindings_discovered"] == 0
    assert skipped["candidate_workspaces_scanned"] == 0
    assert second["candidate_workspaces_scanned"] == 2
    assert second["bindings_discovered"] == 1
    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/rotated",
        pr_number=64,
    )
    assert binding is not None


def test_process_due_watches_continues_after_rate_limited_watch(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root_ok = hub_root / "workspace" / "repo-ok"
    repo_root_rl = hub_root / "workspace" / "repo-rate-limit"
    repo_root_ok.mkdir(parents=True)
    repo_root_rl.mkdir(parents=True)

    binding_ok = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-ok",
        pr_number=47,
        pr_state="open",
        head_branch="feature/ok",
        base_branch="main",
    )
    binding_rl = PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/rate-limit",
        repo_id="repo-rl",
        pr_number=48,
        pr_state="open",
        head_branch="feature/rate-limit",
        base_branch="main",
    )

    watch_store = ScmPollingWatchStore(hub_root)
    for binding, workspace_root in (
        (binding_ok, repo_root_ok),
        (binding_rl, repo_root_rl),
    ):
        watch_store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug=binding.repo_slug,
            repo_id=binding.repo_id,
            pr_number=binding.pr_number,
            workspace_root=str(workspace_root.resolve()),
            thread_target_id=binding.thread_target_id,
            poll_interval_seconds=90,
            next_poll_at="2026-03-30T00:00:00Z",
            expires_at="2099-03-30T01:00:00Z",
            reaction_config={"enabled": True},
            snapshot={"baseline_pending": True},
        )

    def _factory(repo_root_arg: Path, raw_config=None) -> _GitHubServiceStub:
        if repo_root_arg == repo_root_rl:
            return _GitHubServiceStub(
                repo_root_arg,
                raw_config,
                pr_view_payload={},
                reviews_payload=[],
                checks_payload=[],
                rate_limit_payload=_rate_limit_payload(graphql_remaining=5000),
                pr_view_exception=GitHubError(
                    "Command failed: gh pr view 48 --json ...: GraphQL: API rate limit already exceeded for user ID 9387252.",
                    status_code=429,
                ),
            )
        return _GitHubServiceStub(
            repo_root_arg,
            raw_config,
            pr_view_payload={
                "state": "OPEN",
                "isDraft": False,
                "headRefOid": "newsha",
            },
            reviews_payload=[],
            checks_payload=[],
            rate_limit_payload=_rate_limit_payload(graphql_remaining=5000),
        )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(),
        github_service_factory=_factory,
        watch_store=watch_store,
        event_store=ScmEventStore(hub_root),
    )

    result = service.process_due_watches(limit=10)

    assert result["due"] == 2
    assert result["polled"] == 1
    assert result["errors"] == 0
    assert result["rate_limited_skipped"] == 1


def test_process_throttles_discovery_to_one_workspace_per_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    roots = [hub_root / "workspace" / f"repo-{index}" for index in range(3)]
    for root in roots:
        root.mkdir(parents=True)

    scanned_roots: list[Path] = []

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        scanned_roots.append(repo_root_arg)
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=51,
            head_branch="feature/discovery-budget",
            discover=False,
        )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(
            discovery_interval_seconds=360,
            discovery_workspace_limit=1,
        ),
        github_service_factory=_factory,
        watch_store=ScmPollingWatchStore(hub_root),
        event_store=ScmEventStore(hub_root),
    )

    monkeypatch.setattr(
        service,
        "_candidate_workspace_roots",
        lambda: (list(roots), {}, {}, {}),
    )
    monkeypatch.setattr(service, "_thread_activity", lambda: ({}, {}))
    monkeypatch.setattr(
        github_polling,
        "_utc_now",
        lambda: datetime(2026, 4, 5, 9, 0, 0, tzinfo=timezone.utc),
    )

    first = service.process(limit=10)
    second = service.process(limit=10)

    assert first["candidate_workspaces"] == 3
    assert first["candidate_workspaces_scanned"] == 1
    assert second["candidate_workspaces_scanned"] == 0
    assert scanned_roots == [roots[0]]


def test_process_rotates_single_workspace_discovery_across_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    roots = [hub_root / "workspace" / f"repo-{index}" for index in range(4)]
    for root in roots:
        root.mkdir(parents=True)

    scanned_roots: list[Path] = []

    def _factory(repo_root_arg: Path, raw_config=None) -> _DiscoveringGitHubServiceStub:
        scanned_roots.append(repo_root_arg)
        return _DiscoveringGitHubServiceStub(
            repo_root_arg,
            raw_config,
            hub_root=hub_root,
            repo_id="repo-1",
            repo_slug="acme/widgets",
            pr_number=51,
            head_branch="feature/discovery-budget",
            discover=False,
        )

    service = GitHubScmPollingService(
        hub_root,
        raw_config=_polling_config(
            discovery_interval_seconds=360,
            discovery_workspace_limit=1,
        ),
        github_service_factory=_factory,
        watch_store=ScmPollingWatchStore(hub_root),
        event_store=ScmEventStore(hub_root),
    )

    monkeypatch.setattr(
        service, "_candidate_workspace_roots", lambda: (list(roots), {}, {}, {})
    )
    monkeypatch.setattr(service, "_thread_activity", lambda: ({}, {}))

    for minute in (0, 6, 12, 18):
        monkeypatch.setattr(
            github_polling,
            "_utc_now",
            lambda minute=minute: datetime(
                2026, 4, 5, 9, minute, 0, tzinfo=timezone.utc
            ),
        )
        result = service.process(limit=10)
        assert result["candidate_workspaces_scanned"] == 1

    assert len(scanned_roots) == 4
    assert len(set(scanned_roots)) == 4
    assert set(scanned_roots) == set(roots)
