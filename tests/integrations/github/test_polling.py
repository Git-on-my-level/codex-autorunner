from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.core.scm_events import ScmEventStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore
from codex_autorunner.integrations.github.polling import GitHubScmPollingService


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
    ) -> None:
        self.repo_root = repo_root
        self.raw_config = raw_config or {}
        self._pr_view_payload = pr_view_payload
        self._reviews_payload = reviews_payload
        self._checks_payload = checks_payload
        self._issue_comments_payload = issue_comments_payload or []
        self._review_threads_payload = review_threads_payload or []

    def pr_view(self, *, number: int, cwd=None, repo_slug=None) -> dict[str, object]:
        _ = number, cwd, repo_slug
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


def _polling_config(*, profile: str | None = None) -> dict[str, object]:
    reactions: dict[str, object] = {}
    if profile is not None:
        reactions["profile"] = profile
    return {
        "github": {
            "automation": {
                "polling": {
                    "enabled": True,
                    "watch_window_minutes": 30,
                    "interval_seconds": 90,
                },
                "reactions": reactions,
            }
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
