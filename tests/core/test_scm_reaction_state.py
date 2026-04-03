from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.pr_bindings import PrBinding
from codex_autorunner.core.scm_events import ScmEvent
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore
from codex_autorunner.core.scm_reaction_types import ReactionIntent


def _event(
    *,
    event_id: str = "github:event-1",
    payload: dict[str, object] | None = None,
) -> ScmEvent:
    return ScmEvent(
        event_id=event_id,
        provider="github",
        event_type="pull_request_review",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload=payload
        or {
            "action": "submitted",
            "review_state": "changes_requested",
            "review_id": "review-1",
            "author_login": "reviewer",
            "body": "Please add webhook coverage.",
        },
        raw_payload=None,
    )


def _binding() -> PrBinding:
    return PrBinding(
        binding_id="binding-1",
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        pr_state="open",
        head_branch="feature/reactions",
        base_branch="main",
        thread_target_id="thread-123",
        created_at="2026-03-25T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
        closed_at=None,
    )


def _intent() -> ReactionIntent:
    return ReactionIntent(
        reaction_kind="changes_requested",
        operation_kind="enqueue_managed_turn",
        operation_key="scm:key-1",
        payload={
            "thread_target_id": "thread-123",
            "request": {
                "kind": "message",
                "message_text": "Changes requested on acme/widgets#42 by reviewer.",
            },
        },
        event_id="github:event-1",
        binding_id="binding-1",
    )


def test_compute_reaction_fingerprint_is_deterministic_and_ignores_event_identity() -> (
    None
):
    binding = _binding()
    intent = _intent()
    first = _event(event_id="github:event-1")
    second = _event(
        event_id="github:event-2",
        payload={
            "body": "Please add webhook coverage.",
            "review_state": "changes_requested",
            "review_id": "review-1",
            "author_login": "reviewer",
            "action": "submitted",
        },
    )
    store = ScmReactionStateStore(Path("/tmp/unused"))

    first_fingerprint = store.compute_reaction_fingerprint(
        first,
        binding=binding,
        intent=intent,
    )
    second_fingerprint = store.compute_reaction_fingerprint(
        second,
        binding=binding,
        intent=intent,
    )

    assert first_fingerprint == second_fingerprint


def test_compute_reaction_fingerprint_distinguishes_distinct_review_submissions() -> (
    None
):
    binding = _binding()
    first = _event(payload={"review_state": "approved", "review_id": "review-1"})
    second = _event(
        event_id="github:event-2",
        payload={"review_state": "approved", "review_id": "review-2"},
    )
    store = ScmReactionStateStore(Path("/tmp/unused"))

    first_fingerprint = store.compute_reaction_fingerprint(
        first,
        binding=binding,
        intent=ReactionIntent(
            reaction_kind="approved_and_green",
            operation_kind="notify_chat",
            operation_key="scm:key-2",
            payload={"repo_id": "repo-1", "message": "Approved"},
            event_id="github:event-1",
            binding_id="binding-1",
        ),
    )
    second_fingerprint = store.compute_reaction_fingerprint(
        second,
        binding=binding,
        intent=ReactionIntent(
            reaction_kind="approved_and_green",
            operation_kind="notify_chat",
            operation_key="scm:key-3",
            payload={"repo_id": "repo-1", "message": "Approved"},
            event_id="github:event-2",
            binding_id="binding-1",
        ),
    )

    assert first_fingerprint != second_fingerprint


def test_compute_reaction_fingerprint_distinguishes_review_comments() -> None:
    binding = _binding()
    first = ScmEvent(
        event_id="github:event-comment-1",
        provider="github",
        event_type="issue_comment",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "created",
            "author_login": "reviewer",
            "author_type": "User",
            "body": "Please move this helper.",
            "comment_id": "comment-1",
            "issue_author_login": "pr-author",
            "path": "src/codex_autorunner/core/scm_reaction_router.py",
            "line": 164,
        },
        raw_payload=None,
    )
    second = ScmEvent(
        event_id="github:event-comment-2",
        provider="github",
        event_type="issue_comment",
        occurred_at="2026-03-26T00:01:00Z",
        received_at="2026-03-26T00:01:01Z",
        created_at="2026-03-26T00:01:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-2",
        payload={
            "action": "created",
            "author_login": "reviewer",
            "author_type": "User",
            "body": "Please move this helper.",
            "comment_id": "comment-2",
            "issue_author_login": "pr-author",
            "path": "src/codex_autorunner/core/scm_reaction_router.py",
            "line": 164,
        },
        raw_payload=None,
    )
    store = ScmReactionStateStore(Path("/tmp/unused"))

    first_fingerprint = store.compute_reaction_fingerprint(
        first,
        binding=binding,
        intent=ReactionIntent(
            reaction_kind="review_comment",
            operation_kind="enqueue_managed_turn",
            operation_key="scm:key-comment-1",
            payload={"thread_target_id": "thread-123"},
            event_id="github:event-comment-1",
            binding_id="binding-1",
        ),
    )
    second_fingerprint = store.compute_reaction_fingerprint(
        second,
        binding=binding,
        intent=ReactionIntent(
            reaction_kind="review_comment",
            operation_kind="enqueue_managed_turn",
            operation_key="scm:key-comment-2",
            payload={"thread_target_id": "thread-123"},
            event_id="github:event-comment-2",
            binding_id="binding-1",
        ),
    )

    assert first_fingerprint != second_fingerprint


def test_compute_reaction_fingerprint_distinguishes_commented_reviews() -> None:
    binding = _binding()
    first = ScmEvent(
        event_id="github:event-review-comment-1",
        provider="github",
        event_type="pull_request_review",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "submitted",
            "author_login": "chatgpt-codex-connector[bot]",
            "body": "Please extract the webhook normalization helper.",
            "review_id": "review-1",
            "review_state": "commented",
        },
        raw_payload=None,
    )
    second = ScmEvent(
        event_id="github:event-review-comment-2",
        provider="github",
        event_type="pull_request_review",
        occurred_at="2026-03-26T00:01:00Z",
        received_at="2026-03-26T00:01:01Z",
        created_at="2026-03-26T00:01:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-2",
        payload={
            "action": "submitted",
            "author_login": "chatgpt-codex-connector[bot]",
            "body": "Please extract the webhook normalization helper.",
            "review_id": "review-2",
            "review_state": "commented",
        },
        raw_payload=None,
    )
    store = ScmReactionStateStore(Path("/tmp/unused"))

    first_fingerprint = store.compute_reaction_fingerprint(
        first,
        binding=binding,
        intent=ReactionIntent(
            reaction_kind="review_comment",
            operation_kind="enqueue_managed_turn",
            operation_key="scm:key-review-comment-1",
            payload={"thread_target_id": "thread-123"},
            event_id="github:event-review-comment-1",
            binding_id="binding-1",
        ),
    )
    second_fingerprint = store.compute_reaction_fingerprint(
        second,
        binding=binding,
        intent=ReactionIntent(
            reaction_kind="review_comment",
            operation_kind="enqueue_managed_turn",
            operation_key="scm:key-review-comment-2",
            payload={"thread_target_id": "thread-123"},
            event_id="github:event-review-comment-2",
            binding_id="binding-1",
        ),
    )

    assert first_fingerprint != second_fingerprint


def test_reaction_state_store_suppresses_emitted_reactions_and_allows_new_fingerprints(
    tmp_path: Path,
) -> None:
    store = ScmReactionStateStore(tmp_path)
    fingerprint = "fp-1"

    assert (
        store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint=fingerprint,
        )
        is True
    )

    emitted = store.mark_reaction_emitted(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-1",
        operation_key="scm:key-1",
        metadata={"provider": "github"},
    )

    assert emitted.state == "emitted"
    assert emitted.attempt_count == 1
    assert emitted.delivery_failure_count == 0
    assert emitted.last_operation_key == "scm:key-1"
    assert emitted.metadata["provider"] == "github"
    assert (
        store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint=fingerprint,
        )
        is False
    )
    assert (
        store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint="fp-2",
        )
        is True
    )


def test_reaction_state_store_tracks_failure_and_resolution_transitions(
    tmp_path: Path,
) -> None:
    store = ScmReactionStateStore(tmp_path)
    fingerprint = "fp-1"

    failed = store.mark_reaction_delivery_failed(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-1",
        error_text="delivery failed",
    )

    assert failed.state == "delivery_failed"
    assert failed.attempt_count == 0
    assert failed.delivery_failure_count == 1
    assert failed.last_error_text == "delivery failed"
    assert (
        store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint=fingerprint,
        )
        is False
    )

    suppressed = store.mark_reaction_suppressed(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-1b",
    )
    assert suppressed.attempt_count == 1

    recovered = store.mark_reaction_delivery_succeeded(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-2",
        operation_key="scm:key-2",
    )

    assert recovered.state == "emitted"
    assert recovered.attempt_count == 1
    assert recovered.delivery_failure_count == 1
    assert recovered.last_error_text is None

    escalated = store.mark_reaction_escalated(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-2b",
        operation_key="scm:escalation",
    )
    assert escalated.escalated_at is not None
    assert escalated.attempt_count == 2

    resolved = store.mark_reaction_resolved(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-3",
    )

    assert resolved.state == "resolved"
    assert resolved.resolved_at is not None
    assert (
        store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint=fingerprint,
        )
        is True
    )

    re_emitted = store.mark_reaction_emitted(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
        event_id="github:event-4",
        operation_key="scm:key-3",
    )

    assert re_emitted.state == "emitted"
    assert re_emitted.attempt_count == 3
    assert re_emitted.last_event_id == "github:event-4"
    assert re_emitted.last_operation_key == "scm:key-3"
    assert re_emitted.escalated_at is None


def test_reaction_state_store_resolves_other_active_fingerprints(
    tmp_path: Path,
) -> None:
    store = ScmReactionStateStore(tmp_path)
    first = store.mark_reaction_emitted(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint="fp-1",
        event_id="github:event-1",
        operation_key="scm:key-1",
    )
    second = store.mark_reaction_emitted(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint="fp-2",
        event_id="github:event-2",
        operation_key="scm:key-2",
    )

    assert first.state == "emitted"
    assert second.state == "emitted"

    resolved = store.resolve_other_active_reactions(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        keep_fingerprint="fp-2",
        event_id="github:event-3",
    )

    assert resolved == 1
    resolved_first = store.get_reaction_state(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint="fp-1",
    )
    kept_second = store.get_reaction_state(
        binding_id="binding-1",
        reaction_kind="changes_requested",
        fingerprint="fp-2",
    )

    assert resolved_first is not None
    assert resolved_first.state == "resolved"
    assert kept_second is not None
    assert kept_second.state == "emitted"
