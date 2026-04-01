"""Characterization tests for event_decoder canonical contract."""

import json

from codex_autorunner.agents.opencode.event_decoder import (
    _extract_delta_content,
    _extract_message_id,
    _extract_permission_id,
    _extract_question_id,
    _extract_role,
    _extract_usage_from_payload,
    decode_sse_event,
    parse_message_response,
)
from codex_autorunner.agents.opencode.protocol_types import (
    MessageEvent,
    PermissionEvent,
    QuestionEvent,
    UsageEvent,
)
from codex_autorunner.core.sse import SSEEvent


class TestDecodeSSEEvent:
    def test_decodes_message_delta_event(self) -> None:
        event = SSEEvent(
            event="message.delta",
            data=json.dumps(
                {
                    "info": {"id": "msg-1", "role": "assistant"},
                    "delta": {"text": "Hello"},
                }
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, MessageEvent)
        assert result.event_type == "message.delta"
        assert result.message_id == "msg-1"
        assert result.role == "assistant"
        assert result.content == "Hello"

    def test_decodes_message_completed_event(self) -> None:
        event = SSEEvent(
            event="message.completed",
            data=json.dumps({"info": {"id": "msg-2", "role": "assistant"}}),
        )
        result = decode_sse_event(event)
        assert isinstance(result, MessageEvent)
        assert result.event_type == "message.completed"

    def test_decodes_usage_event(self) -> None:
        event = SSEEvent(
            event="usage",
            data=json.dumps(
                {
                    "usage": {"total": 100},
                    "info": {"providerID": "openai", "modelID": "gpt-4"},
                }
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, UsageEvent)
        assert result.event_type == "usage"
        assert result.usage == {"total": 100}
        assert result.provider_id == "openai"
        assert result.model_id == "gpt-4"

    def test_decodes_permission_event(self) -> None:
        event = SSEEvent(
            event="permission",
            data=json.dumps(
                {"id": "perm-1", "permission": "shell", "reason": "Run command"}
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, PermissionEvent)
        assert result.event_type == "permission"
        assert result.permission_id == "perm-1"
        assert result.permission == "shell"
        assert result.reason == "Run command"

    def test_decodes_question_event(self) -> None:
        event = SSEEvent(
            event="question",
            data=json.dumps(
                {"id": "q-1", "question": "Continue?", "context": [["Yes", "No"]]}
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, QuestionEvent)
        assert result.event_type == "question"
        assert result.question_id == "q-1"
        assert result.question == "Continue?"
        assert result.context == [["Yes", "No"]]

    def test_returns_none_for_unknown_event_type(self) -> None:
        event = SSEEvent(event="unknown.event", data=json.dumps({"foo": "bar"}))
        assert decode_sse_event(event) is None

    def test_returns_none_for_invalid_json(self) -> None:
        event = SSEEvent(event="message.delta", data="not valid json")
        assert decode_sse_event(event) is None

    def test_returns_none_for_non_dict_payload(self) -> None:
        event = SSEEvent(event="message.delta", data=json.dumps(["not", "a", "dict"]))
        assert decode_sse_event(event) is None


class TestExtractMessageId:
    def test_extracts_from_info_id(self) -> None:
        payload = {"info": {"id": "msg-123"}}
        assert _extract_message_id(payload) == "msg-123"

    def test_extracts_from_info_messageID_camelcase(self) -> None:
        payload = {"info": {"messageID": "msg-456"}}
        assert _extract_message_id(payload) == "msg-456"

    def test_extracts_from_info_messageId_mixedcase(self) -> None:
        payload = {"info": {"messageId": "msg-789"}}
        assert _extract_message_id(payload) == "msg-789"

    def test_extracts_from_info_message_id_snakecase(self) -> None:
        payload = {"info": {"message_id": "msg-abc"}}
        assert _extract_message_id(payload) == "msg-abc"

    def test_extracts_from_top_level_messageID(self) -> None:
        payload = {"messageID": "msg-top"}
        assert _extract_message_id(payload) == "msg-top"

    def test_extracts_from_top_level_message_id(self) -> None:
        payload = {"message_id": "msg-top2"}
        assert _extract_message_id(payload) == "msg-top2"

    def test_prefers_info_over_top_level(self) -> None:
        payload = {"info": {"id": "info-id"}, "messageID": "top-id"}
        assert _extract_message_id(payload) == "info-id"

    def test_returns_none_when_not_found(self) -> None:
        assert _extract_message_id({}) is None
        assert _extract_message_id({"info": {}}) is None

    def test_ignores_empty_string(self) -> None:
        payload = {"info": {"id": ""}}
        assert _extract_message_id(payload) is None


class TestExtractRole:
    def test_extracts_from_info_role(self) -> None:
        payload = {"info": {"role": "assistant"}}
        assert _extract_role(payload) == "assistant"

    def test_extracts_from_top_level_role(self) -> None:
        payload = {"role": "user"}
        assert _extract_role(payload) == "user"

    def test_prefers_info_over_top_level(self) -> None:
        payload = {"info": {"role": "assistant"}, "role": "user"}
        assert _extract_role(payload) == "assistant"

    def test_returns_none_when_not_found(self) -> None:
        assert _extract_role({}) is None


class TestExtractDeltaContent:
    def test_extracts_string_delta(self) -> None:
        payload = {"delta": "Hello world"}
        assert _extract_delta_content(payload) == "Hello world"

    def test_extracts_text_from_delta_dict(self) -> None:
        payload = {"delta": {"text": "Hello"}}
        assert _extract_delta_content(payload) == "Hello"

    def test_returns_none_for_missing_delta(self) -> None:
        assert _extract_delta_content({}) is None

    def test_returns_none_for_non_text_delta_dict(self) -> None:
        payload = {"delta": {"type": "tool"}}
        assert _extract_delta_content(payload) is None


class TestExtractUsageFromPayload:
    def test_extracts_usage_key(self) -> None:
        payload = {"usage": {"total": 100}}
        assert _extract_usage_from_payload(payload) == {"total": 100}

    def test_extracts_token_usage_key(self) -> None:
        payload = {"token_usage": {"input": 50}}
        assert _extract_usage_from_payload(payload) == {"input": 50}

    def test_extracts_usage_stats_key(self) -> None:
        payload = {"usage_stats": {"output": 25}}
        assert _extract_usage_from_payload(payload) == {"output": 25}

    def test_extracts_usageStats_camelcase(self) -> None:
        payload = {"usageStats": {"total": 200}}
        assert _extract_usage_from_payload(payload) == {"total": 200}

    def test_extracts_from_info_nested(self) -> None:
        payload = {"info": {"usage": {"total": 300}}}
        assert _extract_usage_from_payload(payload) == {"total": 300}

    def test_prefers_top_level_over_info(self) -> None:
        payload = {"usage": {"total": 100}, "info": {"usage": {"total": 200}}}
        assert _extract_usage_from_payload(payload) == {"total": 100}

    def test_returns_none_when_not_found(self) -> None:
        assert _extract_usage_from_payload({}) is None


class TestExtractPermissionId:
    def test_extracts_id_key(self) -> None:
        payload = {"id": "perm-1"}
        assert _extract_permission_id(payload) == "perm-1"

    def test_extracts_permissionID_camelcase(self) -> None:
        payload = {"permissionID": "perm-2"}
        assert _extract_permission_id(payload) == "perm-2"

    def test_extracts_permissionId_mixedcase(self) -> None:
        payload = {"permissionId": "perm-3"}
        assert _extract_permission_id(payload) == "perm-3"

    def test_extracts_permission_id_snakecase(self) -> None:
        payload = {"permission_id": "perm-4"}
        assert _extract_permission_id(payload) == "perm-4"

    def test_returns_none_when_not_found(self) -> None:
        assert _extract_permission_id({}) is None

    def test_ignores_empty_string(self) -> None:
        assert _extract_permission_id({"id": ""}) is None


class TestExtractQuestionId:
    def test_extracts_id_key(self) -> None:
        payload = {"id": "q-1"}
        assert _extract_question_id(payload) == "q-1"

    def test_extracts_questionID_camelcase(self) -> None:
        payload = {"questionID": "q-2"}
        assert _extract_question_id(payload) == "q-2"

    def test_extracts_questionId_mixedcase(self) -> None:
        payload = {"questionId": "q-3"}
        assert _extract_question_id(payload) == "q-3"

    def test_extracts_question_id_snakecase(self) -> None:
        payload = {"question_id": "q-4"}
        assert _extract_question_id(payload) == "q-4"

    def test_returns_none_when_not_found(self) -> None:
        assert _extract_question_id({}) is None


class TestParseMessageResponse:
    def test_extracts_text_key(self) -> None:
        payload = {"text": "Hello world"}
        result = parse_message_response(payload)
        assert result["text"] == "Hello world"
        assert result["error"] is None

    def test_falls_back_to_message_key(self) -> None:
        payload = {"message": "Fallback text"}
        result = parse_message_response(payload)
        assert result["text"] == "Fallback text"

    def test_extracts_from_content_list(self) -> None:
        payload = {"content": [{"text": "Part 1"}, {"text": " Part 2"}]}
        result = parse_message_response(payload)
        assert result["text"] == "Part 1 Part 2"

    def test_extracts_from_content_string(self) -> None:
        payload = {"content": "Direct content"}
        result = parse_message_response(payload)
        assert result["text"] == "Direct content"

    def test_extracts_error(self) -> None:
        payload = {"error": "Something went wrong"}
        result = parse_message_response(payload)
        assert result["error"] == "Something went wrong"

    def test_returns_none_for_non_dict(self) -> None:
        result = parse_message_response("not a dict")
        assert result["text"] is None
        assert result["error"] is None

    def test_handles_missing_keys(self) -> None:
        result = parse_message_response({})
        assert result["text"] is None
        assert result["error"] is None


class TestDecodeUsageEventAliasHandling:
    def test_provider_id_alias_providerID(self) -> None:
        event = SSEEvent(
            event="usage",
            data=json.dumps(
                {"usage": {"total": 100}, "info": {"providerID": "anthropic"}}
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, UsageEvent)
        assert result.provider_id == "anthropic"

    def test_provider_id_alias_provider_id(self) -> None:
        event = SSEEvent(
            event="usage",
            data=json.dumps(
                {"usage": {"total": 100}, "info": {"provider_id": "openai"}}
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, UsageEvent)
        assert result.provider_id == "openai"

    def test_model_id_alias_modelID(self) -> None:
        event = SSEEvent(
            event="usage",
            data=json.dumps({"usage": {"total": 100}, "info": {"modelID": "claude-3"}}),
        )
        result = decode_sse_event(event)
        assert isinstance(result, UsageEvent)
        assert result.model_id == "claude-3"

    def test_model_id_alias_model_id(self) -> None:
        event = SSEEvent(
            event="usage",
            data=json.dumps({"usage": {"total": 100}, "info": {"model_id": "gpt-4"}}),
        )
        result = decode_sse_event(event)
        assert isinstance(result, UsageEvent)
        assert result.model_id == "gpt-4"


class TestDecodeMessageEventAliasHandling:
    def test_message_delta_with_delta_at_top_level(self) -> None:
        event = SSEEvent(
            event="message.delta",
            data=json.dumps(
                {
                    "delta": {"text": "Streaming text"},
                    "info": {"messageID": "msg-legacy", "role": "assistant"},
                }
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, MessageEvent)
        assert result.content == "Streaming text"

    def test_message_delta_with_string_delta(self) -> None:
        event = SSEEvent(
            event="message.delta",
            data=json.dumps(
                {
                    "delta": "Plain string delta",
                    "info": {"id": "msg-str"},
                }
            ),
        )
        result = decode_sse_event(event)
        assert isinstance(result, MessageEvent)
        assert result.content == "Plain string delta"
