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
    ) -> None:
        self.repo_root = repo_root
        self.raw_config = raw_config or {}
        self._pr_view_payload = pr_view_payload
        self._reviews_payload = reviews_payload
        self._checks_payload = checks_payload

    def pr_view(self, *, number: int, cwd=None, repo_slug=None) -> dict[str, object]:
        _ = number, cwd, repo_slug
        return dict(self._pr_view_payload)

    def pr_reviews(self, *, owner: str, repo: str, number: int, cwd=None):
        _ = owner, repo, number, cwd
        return list(self._reviews_payload)

    def pr_checks(self, *, number: int, cwd=None):
        _ = number, cwd
        return list(self._checks_payload)


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
    assert watch.reaction_config["ci_failed"] is True
    assert watch.reaction_config["changes_requested"] is True
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
