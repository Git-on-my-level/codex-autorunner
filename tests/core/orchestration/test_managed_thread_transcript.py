from __future__ import annotations

from codex_autorunner.core.orchestration.managed_thread_transcript import (
    transcript_rows_from_timeline_items,
)


def _user_item(payload: dict) -> dict:
    return {
        "kind": "user_message",
        "item_id": "turn:1:user",
        "order_key": "001",
        "timestamp": "2026-05-10T12:00:00Z",
        "managed_thread_id": "thread-1",
        "managed_turn_id": "turn-1",
        "payload": payload,
        "identity": {"timeline_item_id": "turn:1:user"},
    }


def test_transcript_projects_legacy_injected_prompt_as_model_context() -> None:
    rows = transcript_rows_from_timeline_items(
        [
            _user_item(
                {
                    "text": "<injected context>\nrepo guidance\n</injected context>\n\nFix login",
                    "raw_model_prompt": (
                        "<injected context>\nrepo guidance\n</injected context>\n\nFix login"
                    ),
                }
            )
        ]
    )

    row = rows[0]
    assert row["visible_text"] == "Fix login"
    assert row["model_context_text"] == "repo guidance"
    assert row["raw_model_prompt"].startswith("<injected context>")
    assert row["message"]["text"].startswith("<injected context>")
    assert row["message"]["visible_text"] == "Fix login"
    assert row["message"]["model_context_text"] == "repo guidance"


def test_transcript_projects_capsule_only_model_context_refs() -> None:
    capsule_ref = {
        "capsule_id": "car.repo_basics",
        "capsule_version": "1",
        "visibility": "model_only",
        "scope": "repo",
        "source_digest": "sha256:repo",
    }
    rows = transcript_rows_from_timeline_items(
        [_user_item({"text": "Fix login", "capsule_refs": [capsule_ref]})]
    )

    row = rows[0]
    assert row["visible_text"] == "Fix login"
    assert row["model_context_text"] is None
    assert row["model_context_refs"] == [capsule_ref]
    assert row["message"]["model_context_refs"] == [capsule_ref]


def test_transcript_projects_structured_refs_and_model_context_text() -> None:
    capsule_ref = {
        "capsule_id": "car.issue",
        "capsule_version": "2",
        "visibility": "model_only",
        "scope": "ticket",
        "source_digest": "sha256:ticket",
        "reason": "ticket_context",
    }
    rows = transcript_rows_from_timeline_items(
        [
            _user_item(
                {
                    "text": "Fix login",
                    "user_visible_text": "Fix login",
                    "raw_model_prompt": (
                        "<injected context>\nticket notes\n</injected context>\n\nFix login"
                    ),
                    "capsule_refs": [capsule_ref],
                }
            )
        ]
    )

    row = rows[0]
    assert row["visible_text"] == "Fix login"
    assert row["model_context_text"] == "ticket notes"
    assert row["model_context_refs"] == [capsule_ref]
