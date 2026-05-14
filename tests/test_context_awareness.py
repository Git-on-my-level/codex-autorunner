from __future__ import annotations

from codex_autorunner.core.context_awareness import (
    CAR_AWARENESS_BLOCK,
    format_artifact_delivery_hint,
    maybe_inject_filebox_hint,
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


def test_artifact_delivery_hint_labels_legacy_paths_as_scoped(tmp_path) -> None:
    hint = format_artifact_delivery_hint(
        root=tmp_path,
        target_surface="discord",
        target_conversation_key="channel:123",
        workspace_scope=f"repo:{tmp_path}",
        scope_label="repo/worktree FileBox",
    )

    assert "car artifacts send <file> --to current" in hint
    assert "channel:123" in hint
    assert "Compatibility FileBox paths for this active target only" in hint
    assert "different hub/repo FileBox outbox" in hint
