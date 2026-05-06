from __future__ import annotations

import pytest

from codex_autorunner.core.interaction_inbox import (
    InteractionInboxError,
    InteractionInboxStore,
    InteractionOption,
    InteractionPrompt,
    apply_response,
    create_prompt,
)


def _prompt(
    kind: str,
    *,
    requester_user_id: str | None = "user-1",
    expires_at: str | None = None,
) -> InteractionPrompt:
    options = ()
    if kind != "custom_text_input":
        options = (
            InteractionOption(id="one", label="One"),
            InteractionOption(id="two", label="Two"),
        )
    if kind == "approval":
        options = (
            InteractionOption(id="approve", label="Approve"),
            InteractionOption(id="decline", label="Decline"),
        )
    return InteractionPrompt(
        id=f"prompt-{kind}",
        kind=kind,  # type: ignore[arg-type]
        title="Prompt",
        message="Choose",
        owner={"kind": "repo", "id": "repo"},
        target_scope={"kind": "thread", "key": "thread:1"},
        requester_user_id=requester_user_id,
        options=options,
        expires_at=expires_at,
        source={"surface": "test"},
    )


def test_approval_approve_and_decline() -> None:
    approved = apply_response(
        create_prompt(_prompt("approval")),
        actor_user_id="user-1",
        response={"decision": "approve"},
    )
    declined = apply_response(
        create_prompt(_prompt("approval")),
        actor_user_id="user-1",
        response={"decision": "decline"},
    )

    assert approved.status == "answered"
    assert approved.response["decision"] == "approve"
    assert declined.response["decision"] == "decline"


def test_question_option_selection() -> None:
    answered = apply_response(
        create_prompt(_prompt("single_choice_question")),
        actor_user_id="user-1",
        response={"option_id": "two"},
    )

    assert answered.response["option_id"] == "two"


def test_multi_choice_selection() -> None:
    answered = apply_response(
        create_prompt(_prompt("multi_choice_question")),
        actor_user_id="user-1",
        response={"option_ids": ["one", "two"]},
    )

    assert answered.response["option_ids"] == ["one", "two"]


def test_custom_input() -> None:
    answered = apply_response(
        create_prompt(_prompt("custom_text_input")),
        actor_user_id="user-1",
        response={"text": "custom value"},
    )

    assert answered.response["text"] == "custom value"


def test_unauthorized_actor() -> None:
    with pytest.raises(InteractionInboxError) as exc:
        apply_response(
            create_prompt(_prompt("selection")),
            actor_user_id="user-2",
            response={"option_id": "one"},
        )

    assert exc.value.code == "unauthorized_actor"


def test_expired_prompt() -> None:
    with pytest.raises(InteractionInboxError) as exc:
        apply_response(
            create_prompt(_prompt("selection", expires_at="2026-01-01T00:00:00Z")),
            actor_user_id="user-1",
            response={"option_id": "one"},
            now="2026-01-02T00:00:00Z",
        )

    assert exc.value.code == "expired"


def test_duplicate_response() -> None:
    answered = apply_response(
        create_prompt(_prompt("selection")),
        actor_user_id="user-1",
        response={"option_id": "one"},
    )

    with pytest.raises(InteractionInboxError) as exc:
        apply_response(
            answered,
            actor_user_id="user-1",
            response={"option_id": "two"},
        )

    assert exc.value.code == "already_answered"


def test_store_persists_prompt_response(tmp_path) -> None:
    store = InteractionInboxStore(tmp_path / "interaction_inbox.json")
    store.upsert_prompt(_prompt("approval"))

    reopened = InteractionInboxStore(tmp_path / "interaction_inbox.json")
    decided = reopened.respond(
        "prompt-approval",
        actor_user_id="user-1",
        response={"decision": "approve"},
    )

    assert decided.status == "answered"
    assert reopened.get_prompt("prompt-approval").response["decision"] == "approve"
