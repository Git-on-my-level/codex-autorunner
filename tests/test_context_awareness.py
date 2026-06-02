from __future__ import annotations

from codex_autorunner.core.context_awareness import (
    CAR_AWARENESS_BLOCK,
    maybe_inject_filebox_hint,
    maybe_inject_planned_car_awareness,
    maybe_inject_planned_prompt_writing_hint,
    maybe_inject_worktree_pr_hint,
)


def test_filebox_hint_injected_for_raw_user_keyword() -> None:
    prompt, injected = maybe_inject_filebox_hint(
        "please handle this",
        hint_text="<injected context>\nInbox: /tmp/inbox\n</injected context>",
        user_input_texts=["check outbox"],
    )

    assert injected is True
    assert "Inbox: /tmp/inbox" in prompt


def test_filebox_hint_not_injected_from_car_context_keyword_only() -> None:
    prompt, injected = maybe_inject_filebox_hint(
        CAR_AWARENESS_BLOCK,
        hint_text="<injected context>\nInbox: /tmp/inbox\n</injected context>",
    )

    assert injected is False
    assert prompt == CAR_AWARENESS_BLOCK


def test_worktree_pr_hint_injected_for_raw_user_keyword() -> None:
    prompt, injected = maybe_inject_worktree_pr_hint(
        "please create a worktree for this PR",
        user_input_texts=["please create a worktree for this PR"],
    )

    assert injected is True
    assert "car pma thread spawn --agent <agent_id>" in prompt
    assert "--pr --name <label> --path <hub_root>" in prompt
    assert "git worktree add ... main" in prompt


def test_worktree_pr_hint_not_injected_from_existing_context_only() -> None:
    prompt, injected = maybe_inject_worktree_pr_hint(
        CAR_AWARENESS_BLOCK,
        user_input_texts=["please summarize"],
    )

    assert injected is False
    assert prompt == CAR_AWARENESS_BLOCK


def test_planned_car_awareness_dedupes_by_thread_scope(tmp_path) -> None:
    first, first_injected = maybe_inject_planned_car_awareness(
        "please check our car board",
        hub_root=tmp_path,
        surface_kind="discord",
        surface_key="channel-1",
        managed_thread_id="thread-1",
        declared_profile="car_ambient",
    )
    second, second_injected = maybe_inject_planned_car_awareness(
        "please check our car board",
        hub_root=tmp_path,
        surface_kind="discord",
        surface_key="channel-1",
        managed_thread_id="thread-1",
        declared_profile="car_ambient",
    )

    assert first_injected is True
    assert "You are operating inside a Codex Autorunner" in first
    assert second_injected is False
    assert second == "please check our car board"


def test_planned_prompt_writing_hint_dedupes_by_thread_scope(tmp_path) -> None:
    first, first_injected = maybe_inject_planned_prompt_writing_hint(
        "write a prompt for this",
        hub_root=tmp_path,
        surface_kind="telegram",
        surface_key="topic-1",
        managed_thread_id="topic-1",
        trigger_text="write a prompt for this",
    )
    second, second_injected = maybe_inject_planned_prompt_writing_hint(
        "write a prompt for this",
        hub_root=tmp_path,
        surface_kind="telegram",
        surface_key="topic-1",
        managed_thread_id="topic-1",
        trigger_text="write a prompt for this",
    )

    assert first_injected is True
    assert "put the prompt in a ```code block```" in first
    assert second_injected is False
    assert second == "write a prompt for this"
