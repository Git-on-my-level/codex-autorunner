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


def _intermediate_item(payload: dict) -> dict:
    return {
        "kind": "intermediate",
        "item_id": f"turn:1:intermediate:{payload.get('intermediate_kind', 'notice')}",
        "order_key": "002",
        "timestamp": "2026-05-10T12:00:01Z",
        "managed_thread_id": "thread-1",
        "managed_turn_id": "turn-1",
        "payload": payload,
        "identity": {"timeline_item_id": "turn:1:intermediate"},
    }


def _lifecycle_item(payload: dict) -> dict:
    return {
        "kind": "lifecycle",
        "item_id": "action:1:compact",
        "order_key": "003",
        "timestamp": "2026-05-10T12:00:02Z",
        "managed_thread_id": "thread-1",
        "managed_turn_id": None,
        "payload": payload,
        "identity": {"timeline_item_id": "action:1:compact"},
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


def test_transcript_omits_intermediate_progress_rows() -> None:
    rows = transcript_rows_from_timeline_items(
        [
            _intermediate_item(
                {
                    "intermediate_kind": "chat_execution_journal",
                    "text": "terminal=3977ms",
                    "event_type": "run_notice",
                    "event": {"kind": "chat_execution_journal"},
                }
            ),
            _intermediate_item(
                {
                    "intermediate_kind": "notice",
                    "text": "Compacted hot timeline rows.",
                    "event_type": "run_notice",
                    "event": {"kind": "compaction_summary"},
                }
            ),
            _intermediate_item(
                {
                    "intermediate_kind": "notice",
                    "text": "Decoder missed event shape.",
                    "event_type": "run_notice",
                    "event": {"kind": "decode_failure"},
                }
            ),
            _intermediate_item(
                {
                    "intermediate_kind": "thinking",
                    "title": "Thinking",
                    "text": "Reading files",
                    "event_type": "assistant_update",
                }
            ),
        ]
    )

    assert rows == []


def test_transcript_projects_commentary_intermediate_rows() -> None:
    rows = transcript_rows_from_timeline_items(
        [
            _intermediate_item(
                {
                    "intermediate_kind": "commentary",
                    "title": "commentary",
                    "text": "I am checking the renderer.",
                    "event_ids": ["journal:1"],
                    "progress_source_ids": ["progress:commentary:0001"],
                    "event": {
                        "kind": "commentary",
                        "summary": "I am checking the renderer.",
                    },
                }
            )
        ]
    )

    assert rows == [
        {
            "kind": "intermediate",
            "id": "turn:1:intermediate:commentary",
            "title": "commentary",
            "text": "I am checking the renderer.",
            "event_ids": ["journal:1"],
            "progress_source_ids": ["progress:commentary:0001"],
            "detail": '{\n  "kind": "commentary",\n  "summary": "I am checking the renderer."\n}',
            "turn_id": "turn-1",
            "order_key": "002",
            "timestamp": "2026-05-10T12:00:01Z",
        }
    ]


def test_transcript_projects_context_compaction_card() -> None:
    rows = transcript_rows_from_timeline_items(
        [
            _lifecycle_item(
                {
                    "lifecycle_kind": "context_compaction",
                    "title": "Context compacted by CAR",
                    "text": "Earlier conversation was summarized.",
                    "context_compaction": {
                        "source": "car",
                        "provider": None,
                        "summary": "Keep the current goal.",
                        "preview": "Keep the current goal.",
                        "scope": "managed_thread",
                        "started_fresh_session": True,
                        "stored_by_car": True,
                    },
                }
            )
        ]
    )

    assert len(rows) == 1
    assert rows[0]["kind"] == "context_compaction"
    assert rows[0]["id"] == "action:1:compact"
    assert rows[0]["title"] == "Context compacted by CAR"
    assert rows[0]["text"] == "Earlier conversation was summarized."
    assert rows[0]["context_compaction"] == {
        "source": "car",
        "provider": None,
        "summary": "Keep the current goal.",
        "preview": "Keep the current goal.",
        "scope": "managed_thread",
        "started_fresh_session": True,
        "stored_by_car": True,
    }


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
