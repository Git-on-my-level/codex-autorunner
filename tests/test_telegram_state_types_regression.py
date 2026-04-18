from __future__ import annotations

import json

import pytest

from codex_autorunner.integrations.telegram.state_types import (
    STATE_VERSION,
    OutboxRecord,
    PendingApprovalRecord,
    PendingVoiceRecord,
    TelegramState,
    TelegramTopicRecord,
    ThreadSummary,
    normalize_approval_mode,
    parse_topic_key,
    topic_key,
)


class TestTopicKey:
    def test_root_topic(self) -> None:
        assert topic_key(100, None) == "100:root"

    def test_threaded_topic(self) -> None:
        assert topic_key(100, 200) == "100:200"

    def test_scoped_topic(self) -> None:
        assert topic_key(100, 200, scope="repo@/path") == "100:200:repo%40%2Fpath"

    def test_empty_scope_ignored(self) -> None:
        assert topic_key(100, 200, scope="") == "100:200"

    def test_whitespace_scope_ignored(self) -> None:
        assert topic_key(100, 200, scope="  ") == "100:200"

    def test_none_scope_ignored(self) -> None:
        assert topic_key(100, 200, scope=None) == "100:200"

    def test_raises_on_non_int_chat_id(self) -> None:
        with pytest.raises(TypeError, match="chat_id must be int"):
            topic_key("100", None)  # type: ignore[arg-type]


class TestParseTopicKey:
    def test_root_key(self) -> None:
        chat_id, thread_id, scope = parse_topic_key("100:root")
        assert chat_id == 100
        assert thread_id is None
        assert scope is None

    def test_threaded_key(self) -> None:
        chat_id, thread_id, scope = parse_topic_key("100:200")
        assert chat_id == 100
        assert thread_id == 200
        assert scope is None

    def test_scoped_key(self) -> None:
        chat_id, thread_id, scope = parse_topic_key("100:200:repo%40%2Fpath")
        assert chat_id == 100
        assert thread_id == 200
        assert scope == "repo@/path"

    def test_raises_on_single_part(self) -> None:
        with pytest.raises(ValueError, match="invalid topic key"):
            parse_topic_key("100")

    def test_raises_on_empty(self) -> None:
        with pytest.raises(ValueError, match="invalid topic key"):
            parse_topic_key("")

    def test_raises_on_invalid_chat_id(self) -> None:
        with pytest.raises(ValueError, match="invalid chat id"):
            parse_topic_key("abc:200")


class TestTopicKeyRoundTrip:
    @pytest.mark.parametrize(
        "chat_id, thread_id, scope",
        [
            (100, None, None),
            (100, 200, None),
            (100, 200, "repo@/path"),
            (-100, None, None),
            (0, 1, None),
        ],
    )
    def test_round_trip(
        self,
        chat_id: int,
        thread_id: int | None,
        scope: str | None,
    ) -> None:
        key = topic_key(chat_id, thread_id, scope=scope)
        parsed_chat, parsed_thread, parsed_scope = parse_topic_key(key)
        assert parsed_chat == chat_id
        assert parsed_thread == thread_id
        assert parsed_scope == scope


class TestThreadSummary:
    def test_round_trip(self) -> None:
        summary = ThreadSummary(
            user_preview="user text",
            assistant_preview="assistant text",
            last_used_at="2025-01-01T00:00:00Z",
            workspace_path="/ws",
            rollout_path="/rollout",
        )
        d = summary.to_dict()
        restored = ThreadSummary.from_dict(d)
        assert restored is not None
        assert restored.user_preview == "user text"
        assert restored.assistant_preview == "assistant text"
        assert restored.last_used_at == "2025-01-01T00:00:00Z"
        assert restored.workspace_path == "/ws"
        assert restored.rollout_path == "/rollout"

    def test_from_dict_returns_none_for_non_dict(self) -> None:
        assert ThreadSummary.from_dict("not a dict") is None  # type: ignore[arg-type]

    def test_from_dict_accepts_camel_case(self) -> None:
        payload = {
            "userPreview": "user",
            "assistantPreview": "assistant",
            "lastUsedAt": "2025-01-01",
            "workspacePath": "/ws",
        }
        restored = ThreadSummary.from_dict(payload)
        assert restored is not None
        assert restored.user_preview == "user"
        assert restored.assistant_preview == "assistant"
        assert restored.last_used_at == "2025-01-01"
        assert restored.workspace_path == "/ws"

    def test_from_dict_tolerates_none_values(self) -> None:
        restored = ThreadSummary.from_dict({})
        assert restored is not None
        assert restored.user_preview is None
        assert restored.assistant_preview is None


class TestTelegramTopicRecord:
    def test_round_trip(self) -> None:
        record = TelegramTopicRecord(
            repo_id="repo-1",
            workspace_path="/ws",
            workspace_id="ws-1",
            pma_enabled=True,
            active_thread_id="thread-1",
            thread_ids=["thread-1", "thread-2"],
            agent="codex",
            model="gpt-5.4",
            effort="high",
            approval_mode="yolo",
            last_active_at="2025-01-01T00:00:00Z",
        )
        record.thread_summaries["thread-1"] = ThreadSummary(
            user_preview="hello",
            assistant_preview="world",
        )
        d = record.to_dict()
        restored = TelegramTopicRecord.from_dict(d, default_approval_mode="yolo")
        assert restored.repo_id == "repo-1"
        assert restored.workspace_path == "/ws"
        assert restored.pma_enabled is True
        assert restored.active_thread_id == "thread-1"
        assert restored.thread_ids == ["thread-1", "thread-2"]
        assert restored.agent == "codex"
        assert restored.model == "gpt-5.4"
        assert restored.effort == "high"
        assert restored.approval_mode == "yolo"
        assert "thread-1" in restored.thread_summaries
        assert restored.thread_summaries["thread-1"].user_preview == "hello"

    def test_from_dict_accepts_camel_case_keys(self) -> None:
        payload = {
            "repoId": "repo-1",
            "workspacePath": "/ws",
            "activeThreadId": "t-1",
            "pmaEnabled": True,
        }
        restored = TelegramTopicRecord.from_dict(payload, default_approval_mode="yolo")
        assert restored.repo_id == "repo-1"
        assert restored.workspace_path == "/ws"
        assert restored.active_thread_id == "t-1"
        assert restored.pma_enabled is True

    def test_from_dict_defaults_approval_mode(self) -> None:
        restored = TelegramTopicRecord.from_dict({}, default_approval_mode="yolo")
        assert restored.approval_mode == "yolo"

    def test_from_dict_auto_inherits_thread_id(self) -> None:
        payload = {"activeThreadId": "thread-1"}
        restored = TelegramTopicRecord.from_dict(payload, default_approval_mode="yolo")
        assert restored.thread_ids == ["thread-1"]


class TestPendingApprovalRecord:
    def test_round_trip(self) -> None:
        record = PendingApprovalRecord(
            request_id="req-1",
            turn_id="turn-1",
            chat_id=100,
            thread_id=200,
            message_id=300,
            prompt="approve this",
            created_at="2025-01-01T00:00:00Z",
            topic_key="100:200",
        )
        d = record.to_dict()
        restored = PendingApprovalRecord.from_dict(d)
        assert restored is not None
        assert restored.request_id == "req-1"
        assert restored.turn_id == "turn-1"
        assert restored.chat_id == 100
        assert restored.thread_id == 200
        assert restored.message_id == 300
        assert restored.prompt == "approve this"
        assert restored.topic_key == "100:200"

    def test_from_dict_returns_none_for_missing_request_id(self) -> None:
        payload = {"turn_id": "t", "chat_id": 1, "created_at": "now"}
        assert PendingApprovalRecord.from_dict(payload) is None

    def test_from_dict_returns_none_for_missing_created_at(self) -> None:
        payload = {"request_id": "r", "turn_id": "t", "chat_id": 1}
        assert PendingApprovalRecord.from_dict(payload) is None

    def test_from_dict_returns_none_for_non_dict(self) -> None:
        assert PendingApprovalRecord.from_dict("bad") is None  # type: ignore[arg-type]

    def test_from_dict_tolerates_non_int_thread_id(self) -> None:
        payload = {
            "request_id": "r",
            "turn_id": "t",
            "chat_id": 1,
            "thread_id": "not-int",
            "created_at": "now",
        }
        restored = PendingApprovalRecord.from_dict(payload)
        assert restored is not None
        assert restored.thread_id is None


class TestOutboxRecord:
    def test_round_trip(self) -> None:
        record = OutboxRecord(
            record_id="rec-1",
            chat_id=100,
            thread_id=200,
            reply_to_message_id=300,
            placeholder_message_id=None,
            text="outbound message",
            created_at="2025-01-01T00:00:00Z",
            attempts=2,
            last_error="rate limited",
            operation="send",
            operation_id="op-1",
            message_id=400,
            outbox_key="100:200",
            overflow_mode_override=None,
        )
        d = record.to_dict()
        restored = OutboxRecord.from_dict(d)
        assert restored is not None
        assert restored.record_id == "rec-1"
        assert restored.chat_id == 100
        assert restored.attempts == 2
        assert restored.last_error == "rate limited"
        assert restored.operation == "send"

    def test_from_dict_returns_none_for_missing_record_id(self) -> None:
        assert (
            OutboxRecord.from_dict({"chat_id": 1, "text": "", "created_at": "now"})
            is None
        )

    def test_from_dict_defaults_attempts_to_zero(self) -> None:
        payload = {
            "record_id": "r",
            "chat_id": 1,
            "text": "",
            "created_at": "now",
            "attempts": -5,
        }
        restored = OutboxRecord.from_dict(payload)
        assert restored is not None
        assert restored.attempts == 0


class TestPendingVoiceRecord:
    def test_round_trip(self) -> None:
        record = PendingVoiceRecord(
            record_id="v-1",
            chat_id=100,
            thread_id=200,
            message_id=300,
            file_id="file-abc",
            file_name="voice.ogg",
            caption="note",
            file_size=1024,
            mime_type="audio/ogg",
            duration=30,
            workspace_path="/ws",
            created_at="2025-01-01T00:00:00Z",
        )
        d = record.to_dict()
        restored = PendingVoiceRecord.from_dict(d)
        assert restored is not None
        assert restored.record_id == "v-1"
        assert restored.file_id == "file-abc"
        assert restored.caption == "note"
        assert restored.duration == 30

    def test_from_dict_returns_none_for_missing_file_id(self) -> None:
        payload = {
            "record_id": "r",
            "chat_id": 1,
            "message_id": 1,
            "created_at": "now",
        }
        assert PendingVoiceRecord.from_dict(payload) is None

    def test_from_dict_returns_none_for_missing_message_id(self) -> None:
        payload = {
            "record_id": "r",
            "chat_id": 1,
            "file_id": "f",
            "created_at": "now",
        }
        assert PendingVoiceRecord.from_dict(payload) is None


class TestTelegramState:
    def test_to_json_produces_valid_json(self) -> None:
        state = TelegramState()
        state.topics["100:root"] = TelegramTopicRecord(
            repo_id="repo-1",
            agent="codex",
        )
        state.pending_approvals["req-1"] = PendingApprovalRecord(
            request_id="req-1",
            turn_id="turn-1",
            chat_id=100,
            thread_id=None,
            message_id=10,
            prompt="approve?",
            created_at="2025-01-01",
        )
        raw = state.to_json()
        parsed = json.loads(raw)
        assert parsed["version"] == STATE_VERSION
        assert "100:root" in parsed["topics"]
        assert parsed["topics"]["100:root"]["repo_id"] == "repo-1"
        assert "req-1" in parsed["pending_approvals"]

    def test_empty_state_serializes(self) -> None:
        state = TelegramState()
        raw = state.to_json()
        parsed = json.loads(raw)
        assert parsed["version"] == STATE_VERSION
        assert parsed["topics"] == {}
        assert parsed["pending_approvals"] == {}
        assert parsed["outbox"] == {}
        assert parsed["pending_voice"] == {}


class TestNormalizeApprovalMode:
    def test_yolo_default(self) -> None:
        assert normalize_approval_mode(None) == "yolo"

    def test_safe_mode(self) -> None:
        assert normalize_approval_mode("safe") == "safe"

    def test_invalid_falls_back(self) -> None:
        assert normalize_approval_mode("invalid") == "yolo"

    def test_custom_default(self) -> None:
        assert normalize_approval_mode("invalid", default="safe") == "safe"
