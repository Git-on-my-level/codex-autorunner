from __future__ import annotations

from codex_autorunner.core.pr_bindings import PrBinding
from codex_autorunner.core.scm_events import ScmEvent
from codex_autorunner.core.scm_reaction_router import route_scm_reactions
from codex_autorunner.core.scm_reaction_types import ScmReactionConfig


def _event(
    event_type: str,
    *,
    event_id: str = "github:event-1",
    repo_slug: str | None = "acme/widgets",
    repo_id: str | None = "repo-1",
    pr_number: int | None = 42,
    payload: dict[str, object] | None = None,
) -> ScmEvent:
    return ScmEvent(
        event_id=event_id,
        provider="github",
        event_type=event_type,
        occurred_at="2026-03-25T00:00:00Z",
        received_at="2026-03-25T00:00:01Z",
        created_at="2026-03-25T00:00:02Z",
        repo_slug=repo_slug,
        repo_id=repo_id,
        pr_number=pr_number,
        delivery_id="delivery-1",
        payload=payload or {},
        raw_payload=None,
    )


def _binding(
    *,
    thread_target_id: str | None = None,
    pr_state: str = "open",
) -> PrBinding:
    return PrBinding(
        binding_id="binding-1",
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        pr_state=pr_state,
        head_branch="feature/reactions",
        base_branch="main",
        thread_target_id=thread_target_id,
        created_at="2026-03-24T00:00:00Z",
        updated_at="2026-03-25T00:00:00Z",
        closed_at=None,
    )


def test_route_scm_reactions_returns_threaded_ci_failure_intent() -> None:
    event = _event(
        "check_run",
        payload={
            "action": "completed",
            "status": "completed",
            "conclusion": "failure",
            "name": "ci / test",
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-123")
    )

    assert len(intents) == 1
    assert intents[0].reaction_kind == "ci_failed"
    assert intents[0].operation_kind == "enqueue_managed_turn"
    assert intents[0].payload == {
        "correlation_id": "scm:github:event-1",
        "thread_target_id": "thread-123",
        "request": {
            "kind": "message",
            "message_text": (
                "CI failed for acme/widgets#42: ci / test (failure). "
                "Inspect the failing check and push a fix."
            ),
            "metadata": {
                "scm": {
                    "correlation_id": "scm:github:event-1",
                    "event_id": "github:event-1",
                    "provider": "github",
                    "event_type": "check_run",
                    "reaction_kind": "ci_failed",
                    "repo_slug": "acme/widgets",
                    "repo_id": "repo-1",
                    "pr_number": 42,
                    "binding_id": "binding-1",
                    "thread_target_id": "thread-123",
                }
            },
        },
    }


def test_route_scm_reactions_returns_notify_intent_for_changes_requested_without_thread() -> (
    None
):
    event = _event(
        "pull_request_review",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add coverage for the webhook branch handling.",
        },
    )

    intents = route_scm_reactions(event, binding=_binding())

    assert len(intents) == 1
    assert intents[0].reaction_kind == "changes_requested"
    assert intents[0].operation_kind == "notify_chat"
    assert intents[0].payload == {
        "correlation_id": "scm:github:event-1",
        "delivery": "primary_pma",
        "message": (
            "Changes requested on acme/widgets#42 by reviewer: "
            "Please add coverage for the webhook branch handling."
        ),
        "metadata": {
            "scm": {
                "correlation_id": "scm:github:event-1",
                "event_id": "github:event-1",
                "provider": "github",
                "event_type": "pull_request_review",
                "reaction_kind": "changes_requested",
                "repo_slug": "acme/widgets",
                "repo_id": "repo-1",
                "pr_number": 42,
                "binding_id": "binding-1",
            }
        },
        "repo_id": "repo-1",
    }


def test_route_scm_reactions_returns_notify_intent_for_approved_review_and_is_deterministic() -> (
    None
):
    event = _event(
        "pull_request_review",
        event_id="github:event-approved",
        payload={
            "action": "submitted",
            "review_state": "approved",
            "author_login": "approver",
        },
    )
    binding = _binding()

    first = route_scm_reactions(event, binding=binding)
    second = route_scm_reactions(event, binding=binding)

    assert [intent.to_dict() for intent in first] == [
        intent.to_dict() for intent in second
    ]
    assert first[0].reaction_kind == "approved_and_green"
    assert first[0].operation_kind == "notify_chat"
    assert (
        first[0].payload["message"]
        == "acme/widgets#42 is approved and ready to land (approver)."
    )


def test_route_scm_reactions_routes_commented_review_as_review_comment() -> None:
    event = _event(
        "pull_request_review",
        event_id="github:event-commented-review",
        payload={
            "action": "submitted",
            "review_state": "commented",
            "author_login": "chatgpt-codex-connector[bot]",
            "body": "Please extract the webhook normalization helper.",
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-commented-review")
    )

    assert len(intents) == 1
    assert intents[0].reaction_kind == "review_comment"
    assert intents[0].operation_kind == "enqueue_managed_turn"
    assert intents[0].payload["request"]["message_text"] == (
        "New PR comment on acme/widgets#42 from chatgpt-codex-connector[bot]: "
        "Please extract the webhook normalization helper. "
        "Address the feedback and reply on the PR after updating the branch."
    )


def test_route_scm_reactions_returns_notify_intent_for_merged_pr_even_with_thread() -> (
    None
):
    event = _event(
        "pull_request",
        event_id="github:event-merged",
        payload={
            "action": "closed",
            "state": "closed",
            "merged": True,
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-789", pr_state="merged")
    )

    assert len(intents) == 1
    assert intents[0].reaction_kind == "merged"
    assert intents[0].operation_kind == "notify_chat"
    assert intents[0].payload["message"] == "acme/widgets#42 was merged."
    assert intents[0].payload["repo_id"] == "repo-1"


def test_route_scm_reactions_returns_threaded_changes_requested_prompt_with_next_step() -> (
    None
):
    event = _event(
        "pull_request_review",
        event_id="github:event-thread-review",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Split the router text builders into a separate module and add tests.",
            "html_url": "https://example.invalid/review/1",
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-555")
    )

    assert len(intents) == 1
    assert intents[0].operation_kind == "enqueue_managed_turn"
    assert intents[0].payload["request"]["message_text"] == (
        "Changes requested on acme/widgets#42 by reviewer: "
        "Split the router text builders into a separate module and add tests. "
        "Address the feedback and reply after updating the PR."
    )
    assert (
        "https://example.invalid/review/1"
        not in intents[0].payload["request"]["message_text"]
    )


def test_route_scm_reactions_routes_pr_comment_to_managed_thread() -> None:
    event = _event(
        "issue_comment",
        event_id="github:event-comment-thread",
        payload={
            "action": "created",
            "comment_id": "2844",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please guard the wake-up path with a PR-comment filter.",
            "path": "src/codex_autorunner/core/scm_reaction_router.py",
            "line": 164,
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-900")
    )

    assert len(intents) == 1
    assert intents[0].reaction_kind == "review_comment"
    assert intents[0].operation_kind == "enqueue_managed_turn"
    assert intents[0].payload["request"]["message_text"] == (
        "New PR comment on acme/widgets#42 from reviewer at "
        "src/codex_autorunner/core/scm_reaction_router.py:164: "
        "Please guard the wake-up path with a PR-comment filter. "
        "Address the feedback and reply on the PR after updating the branch."
    )


def test_route_scm_reactions_routes_pr_comment_to_notify_without_thread() -> None:
    event = _event(
        "issue_comment",
        event_id="github:event-comment-notify",
        payload={
            "action": "created",
            "comment_id": "2844",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please add coverage for bot filtering.",
        },
    )

    intents = route_scm_reactions(event, binding=_binding())

    assert len(intents) == 1
    assert intents[0].reaction_kind == "review_comment"
    assert intents[0].operation_kind == "notify_chat"
    assert (
        intents[0].payload["message"]
        == "New PR comment on acme/widgets#42 from reviewer: Please add coverage for bot filtering."
    )


def test_route_scm_reactions_skips_self_pr_comments() -> None:
    self_comment = _event(
        "issue_comment",
        event_id="github:event-self-comment",
        payload={
            "action": "created",
            "author_login": "pr-author",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "I pushed a fix.",
        },
    )

    assert route_scm_reactions(self_comment, binding=_binding()) == []


def test_route_scm_reactions_skips_bot_issue_comments() -> None:
    bot_comment = _event(
        "issue_comment",
        event_id="github:event-bot-comment",
        payload={
            "action": "created",
            "author_login": "github-actions[bot]",
            "author_type": "Bot",
            "issue_author_login": "pr-author",
            "body": "Automated reminder.",
        },
    )

    assert route_scm_reactions(bot_comment, binding=_binding()) == []


def test_route_scm_reactions_routes_pull_request_review_comment() -> None:
    event = _event(
        "pull_request_review_comment",
        event_id="github:event-inline-comment",
        payload={
            "action": "created",
            "comment_id": "2844",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please cover the inline review-comment webhook path too.",
            "path": "src/codex_autorunner/integrations/github/webhooks.py",
            "line": 284,
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-inline")
    )

    assert len(intents) == 2
    assert intents[0].reaction_kind == "review_comment"
    assert intents[0].operation_kind == "enqueue_managed_turn"
    assert intents[1].reaction_kind == "review_comment"
    assert intents[1].operation_kind == "react_pr_review_comment"
    assert intents[1].payload == {
        "comment_id": "2844",
        "content": "eyes",
        "correlation_id": "scm:github:event-inline-comment",
        "repo_slug": "acme/widgets",
        "repo_id": "repo-1",
        "binding_id": "binding-1",
        "scm": {
            "correlation_id": "scm:github:event-inline-comment",
            "event_id": "github:event-inline-comment",
            "provider": "github",
            "event_type": "pull_request_review_comment",
            "reaction_kind": "review_comment",
            "repo_slug": "acme/widgets",
            "repo_id": "repo-1",
            "pr_number": 42,
            "binding_id": "binding-1",
            "thread_target_id": "thread-inline",
        },
    }


def test_route_scm_reactions_parses_review_comment_id_from_discussion_url() -> None:
    event = _event(
        "pull_request_review_comment",
        event_id="github:event-inline-comment-url-id",
        payload={
            "action": "created",
            "comment_id": "PRRC_kwDOAcmeNodeId",
            "html_url": "https://github.com/acme/widgets/pull/42#discussion_r99331",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please preserve inline reaction support.",
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-inline")
    )

    assert len(intents) == 2
    assert intents[1].operation_kind == "react_pr_review_comment"
    assert intents[1].payload["comment_id"] == "99331"


def test_route_scm_reactions_parses_review_comment_id_from_api_url() -> None:
    event = _event(
        "pull_request_review_comment",
        event_id="github:event-inline-comment-api-url-id",
        payload={
            "action": "created",
            "comment_id": "PRRC_kwDOAcmeNodeId",
            "url": "https://api.github.com/repos/acme/widgets/pulls/comments/77123",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please preserve inline reaction support.",
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-inline")
    )

    assert len(intents) == 2
    assert intents[1].operation_kind == "react_pr_review_comment"
    assert intents[1].payload["comment_id"] == "77123"


def test_route_scm_reactions_routes_bot_pull_request_review_comment() -> None:
    event = _event(
        "pull_request_review_comment",
        event_id="github:event-inline-bot-comment",
        payload={
            "action": "created",
            "comment_id": "2845",
            "author_login": "chatgpt-codex-connector[bot]",
            "author_type": "Bot",
            "issue_author_login": "pr-author",
            "body": "Please cover the bot-authored inline review path.",
            "path": "src/codex_autorunner/integrations/github/webhooks.py",
            "line": 305,
        },
    )

    intents = route_scm_reactions(
        event, binding=_binding(thread_target_id="thread-inline-bot")
    )

    assert len(intents) == 2
    assert intents[0].reaction_kind == "review_comment"
    assert intents[0].operation_kind == "enqueue_managed_turn"
    assert intents[1].reaction_kind == "review_comment"
    assert intents[1].operation_kind == "react_pr_review_comment"


def test_route_scm_reactions_still_reacts_to_review_comment_without_chat_target() -> (
    None
):
    event = _event(
        "pull_request_review_comment",
        event_id="github:event-inline-reaction-only",
        repo_id=None,
        payload={
            "action": "created",
            "comment_id": "777",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please handle the reaction side effect centrally.",
        },
    )

    intents = route_scm_reactions(event, binding=None)

    assert len(intents) == 1
    assert intents[0].operation_kind == "react_pr_review_comment"
    assert intents[0].payload["comment_id"] == "777"
    assert intents[0].payload["repo_slug"] == "acme/widgets"


def test_route_scm_reactions_returns_no_intents_for_irrelevant_events() -> None:
    opened = _event(
        "pull_request",
        event_id="github:event-opened",
        payload={"action": "opened", "state": "open", "merged": False},
    )
    issue_comment = _event("issue_comment", event_id="github:event-comment")

    assert (
        route_scm_reactions(opened, binding=_binding(thread_target_id="thread-1")) == []
    )
    assert route_scm_reactions(issue_comment, binding=_binding()) == []


def test_route_scm_reactions_respects_reaction_config_and_requires_notify_target() -> (
    None
):
    approved = _event(
        "pull_request_review",
        event_id="github:event-disabled",
        payload={"action": "submitted", "review_state": "approved"},
    )
    no_repo_target = _event(
        "pull_request_review",
        event_id="github:event-no-target",
        repo_id=None,
        payload={"action": "submitted", "review_state": "changes_requested"},
    )

    assert (
        route_scm_reactions(
            approved,
            binding=_binding(),
            config=ScmReactionConfig(approved_and_green=False),
        )
        == []
    )
    assert route_scm_reactions(no_repo_target, binding=None) == []
