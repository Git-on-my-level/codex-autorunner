"""Tests for OpenCode protocol-payload drift adapters.

Covers session id extraction, message/part alias normalization,
status type / idle classification, error text, usage field aliases,
part-and-delta extraction, delta text extraction, and message phase
extraction.  Every compatibility path tested here corresponds to a known
schema drift alias documented in the protocol-payload owner.
"""

import pytest

from codex_autorunner.agents.opencode.protocol_payload import (
    OPENCODE_IDLE_STATUS_VALUES,
    extract_delta_text_value,
    extract_error_text,
    extract_message_phase,
    extract_part_and_delta,
    extract_session_id,
    extract_status_type,
    extract_total_tokens,
    extract_usage_details,
    normalize_message_phase,
    parse_message_response,
    prompt_echo_matches,
    recover_last_assistant_message,
    status_is_idle,
)


class TestExtractSessionId:
    """Session-id alias paths exercised by the protocol-payload owner."""

    def test_direct_session_id_aliases(self) -> None:
        assert extract_session_id({"sessionID": "abc"}) == "abc"
        assert extract_session_id({"sessionId": "abc"}) == "abc"
        assert extract_session_id({"session_id": "abc"}) == "abc"

    def test_fallback_id_key(self) -> None:
        assert extract_session_id({"id": "abc"}, allow_fallback_id=True) == "abc"
        assert extract_session_id({"id": "abc"}, allow_fallback_id=False) is None

    def test_nested_info_session(self) -> None:
        payload = {"info": {"sessionID": "s1"}}
        assert extract_session_id(payload) == "s1"

    def test_nested_info_session_with_id_fallback(self) -> None:
        payload = {"info": {"session": {"id": "s2"}}}
        assert extract_session_id(payload) == "s2"

    def test_properties_direct_session_id(self) -> None:
        payload = {"properties": {"sessionID": "s3"}}
        assert extract_session_id(payload) == "s3"

    def test_properties_info_nested_session(self) -> None:
        payload = {"properties": {"info": {"session": {"id": "s4"}}}}
        assert extract_session_id(payload) == "s4"

    def test_properties_item_nested_session(self) -> None:
        payload = {"properties": {"item": {"session": {"id": "s5"}}}}
        assert extract_session_id(payload) == "s5"

    def test_properties_part_nested_session(self) -> None:
        payload = {"properties": {"part": {"session": {"id": "s6"}}}}
        assert extract_session_id(payload) == "s6"

    def test_top_level_session_dict(self) -> None:
        payload = {"session": {"id": "s7"}}
        assert extract_session_id(payload) == "s7"

    def test_top_level_session_dict_with_session_id(self) -> None:
        payload = {"session": {"sessionID": "s8"}}
        assert extract_session_id(payload) == "s8"

    def test_item_nested_session_id(self) -> None:
        payload = {"item": {"sessionID": "s9"}}
        assert extract_session_id(payload) == "s9"

    def test_direct_session_id_takes_precedence_over_nested(self) -> None:
        payload = {"sessionID": "top", "session": {"id": "nested"}}
        assert extract_session_id(payload) == "top"

    def test_non_dict_returns_none(self) -> None:
        assert extract_session_id("not a dict") is None
        assert extract_session_id(None) is None
        assert extract_session_id(42) is None

    def test_whitespace_only_accepted(self) -> None:
        assert extract_session_id({"sessionID": "   "}) == "   "

    def test_non_string_value_ignored(self) -> None:
        assert extract_session_id({"sessionID": 123}) is None
        assert extract_session_id({"sessionID": None}) is None


class TestExtractStatusType:
    """Status-type extraction from various payload shapes."""

    def test_direct_status_dict(self) -> None:
        assert extract_status_type({"status": {"type": "busy"}}) == "busy"

    def test_direct_status_string(self) -> None:
        assert extract_status_type({"status": "idle"}) == "idle"

    def test_properties_status_dict(self) -> None:
        payload = {"properties": {"status": {"type": "idle"}}}
        assert extract_status_type(payload) == "idle"

    def test_properties_status_with_status_key(self) -> None:
        payload = {"properties": {"status": {"status": "done"}}}
        assert extract_status_type(payload) == "done"

    def test_info_as_status(self) -> None:
        payload = {"info": {"type": "running"}}
        assert extract_status_type(payload) == "running"

    def test_non_dict_returns_none(self) -> None:
        assert extract_status_type(None) is None
        assert extract_status_type("string") is None

    def test_empty_payload_returns_none(self) -> None:
        assert extract_status_type({}) is None

    def test_non_string_status_ignored(self) -> None:
        assert extract_status_type({"status": 42}) is None


class TestStatusIsIdle:
    """Idle classification for known status values."""

    @pytest.mark.parametrize("value", sorted(OPENCODE_IDLE_STATUS_VALUES))
    def test_known_idle_values(self, value: str) -> None:
        assert status_is_idle(value) is True

    def test_case_insensitive(self) -> None:
        assert status_is_idle("IDLE") is True
        assert status_is_idle("Done") is True

    def test_non_idle_values(self) -> None:
        assert status_is_idle("busy") is False
        assert status_is_idle("running") is False

    def test_none_returns_false(self) -> None:
        assert status_is_idle(None) is False

    def test_empty_returns_false(self) -> None:
        assert status_is_idle("") is False


class TestExtractPartAndDelta:
    """Part and delta extraction from message-part event payloads."""

    def test_properties_nesting(self) -> None:
        payload = {
            "properties": {
                "part": {"type": "text", "text": "hi"},
                "delta": {"text": "hi"},
            }
        }
        part, delta = extract_part_and_delta(payload)
        assert part == {"type": "text", "text": "hi"}
        assert delta == {"text": "hi"}

    def test_direct_keys(self) -> None:
        payload = {
            "part": {"type": "text", "text": "hello"},
            "delta": "hello",
        }
        part, delta = extract_part_and_delta(payload)
        assert part == {"type": "text", "text": "hello"}
        assert delta == "hello"

    def test_properties_without_part(self) -> None:
        payload = {"properties": {"delta": {"text": "x"}}}
        part, delta = extract_part_and_delta(payload)
        assert part is None
        assert delta == {"text": "x"}

    def test_properties_without_delta(self) -> None:
        payload = {"properties": {"part": {"type": "text"}}}
        part, delta = extract_part_and_delta(payload)
        assert part == {"type": "text"}
        assert delta is None

    def test_non_dict_returns_none(self) -> None:
        part, delta = extract_part_and_delta("not a dict")
        assert part is None
        assert delta is None

    def test_none_returns_none(self) -> None:
        part, delta = extract_part_and_delta(None)
        assert part is None
        assert delta is None

    def test_empty_payload(self) -> None:
        part, delta = extract_part_and_delta({})
        assert part is None
        assert delta is None

    def test_properties_preferred_over_direct(self) -> None:
        payload = {
            "properties": {"part": {"type": "a"}, "delta": "x"},
            "part": {"type": "b"},
            "delta": "y",
        }
        part, delta = extract_part_and_delta(payload)
        assert part == {"type": "a"}
        assert delta == "x"

    def test_non_dict_part_returns_none_part(self) -> None:
        payload = {"properties": {"part": "not-a-dict", "delta": "d"}}
        part, delta = extract_part_and_delta(payload)
        assert part is None
        assert delta == "d"


class TestExtractDeltaTextValue:
    """Delta text extraction from various delta shapes."""

    def test_dict_with_text(self) -> None:
        assert extract_delta_text_value({"text": "hello"}) == "hello"

    def test_plain_string(self) -> None:
        assert extract_delta_text_value("hello") == "hello"

    def test_dict_without_text(self) -> None:
        assert extract_delta_text_value({"content": "hi"}) is None

    def test_none(self) -> None:
        assert extract_delta_text_value(None) is None

    def test_int(self) -> None:
        assert extract_delta_text_value(42) is None

    def test_empty_string(self) -> None:
        assert extract_delta_text_value("") == ""

    def test_empty_dict(self) -> None:
        assert extract_delta_text_value({}) is None


class TestExtractErrorText:
    """Error text extraction from various payload shapes."""

    def test_error_dict_with_message(self) -> None:
        assert extract_error_text({"error": {"message": "fail"}}) == "fail"

    def test_error_dict_with_detail(self) -> None:
        assert extract_error_text({"error": {"detail": "fail"}}) == "fail"

    def test_error_string(self) -> None:
        assert extract_error_text({"error": "fail"}) == "fail"

    def test_top_level_detail(self) -> None:
        assert extract_error_text({"detail": "fail"}) == "fail"

    def test_top_level_message(self) -> None:
        assert extract_error_text({"message": "fail"}) == "fail"

    def test_top_level_reason(self) -> None:
        assert extract_error_text({"reason": "fail"}) == "fail"

    def test_error_dict_takes_precedence(self) -> None:
        payload = {"error": {"message": "err"}, "detail": "top"}
        assert extract_error_text(payload) == "err"

    def test_non_dict_returns_none(self) -> None:
        assert extract_error_text(None) is None
        assert extract_error_text("string") is None

    def test_empty_returns_none(self) -> None:
        assert extract_error_text({}) is None


class TestExtractMessagePhase:
    """Message phase extraction from various nesting levels."""

    def test_direct_phase(self) -> None:
        assert extract_message_phase({"phase": "commentary"}) == "commentary"

    def test_info_phase(self) -> None:
        assert (
            extract_message_phase({"info": {"phase": "final_answer"}}) == "final_answer"
        )

    def test_properties_phase(self) -> None:
        assert (
            extract_message_phase({"properties": {"phase": "commentary"}})
            == "commentary"
        )

    def test_properties_message_phase(self) -> None:
        payload = {"properties": {"message": {"phase": "final_answer"}}}
        assert extract_message_phase(payload) == "final_answer"

    def test_properties_item_phase(self) -> None:
        payload = {"properties": {"item": {"phase": "commentary"}}}
        assert extract_message_phase(payload) == "commentary"

    def test_message_phase(self) -> None:
        assert (
            extract_message_phase({"message": {"phase": "commentary"}}) == "commentary"
        )

    def test_item_phase(self) -> None:
        assert (
            extract_message_phase({"item": {"phase": "final_answer"}}) == "final_answer"
        )

    def test_invalid_phase_returns_none(self) -> None:
        assert extract_message_phase({"phase": "unknown"}) is None

    def test_non_dict_returns_none(self) -> None:
        assert extract_message_phase(None) is None
        assert extract_message_phase("string") is None

    def test_empty_returns_none(self) -> None:
        assert extract_message_phase({}) is None

    def test_case_insensitive(self) -> None:
        assert extract_message_phase({"phase": "Commentary"}) == "commentary"
        assert extract_message_phase({"phase": "FINAL_ANSWER"}) == "final_answer"


class TestNormalizeMessagePhase:
    def test_valid_values(self) -> None:
        assert normalize_message_phase("commentary") == "commentary"
        assert normalize_message_phase("final_answer") == "final_answer"

    def test_case_insensitive(self) -> None:
        assert normalize_message_phase("Commentary") == "commentary"

    def test_invalid_returns_none(self) -> None:
        assert normalize_message_phase("unknown") is None
        assert normalize_message_phase("") is None
        assert normalize_message_phase(None) is None


class TestParseMessageResponse:
    """Message response parsing with text/error extraction."""

    def test_text_from_text_field(self) -> None:
        result = parse_message_response({"text": "hello"})
        assert result.text == "hello"
        assert result.error is None

    def test_text_from_message_field(self) -> None:
        result = parse_message_response({"message": "hello"})
        assert result.text == "hello"

    def test_text_from_content_list(self) -> None:
        result = parse_message_response({"content": [{"type": "text", "text": "hi"}]})
        assert result.text == "hi"

    def test_text_from_content_string(self) -> None:
        result = parse_message_response({"content": "hi"})
        assert result.text == "hi"

    def test_error_field(self) -> None:
        result = parse_message_response({"text": "ok", "error": "fail"})
        assert result.text == "ok"
        assert result.error == "fail"

    def test_non_dict_returns_empty(self) -> None:
        result = parse_message_response(None)
        assert result.text == ""
        assert result.error is None

    def test_parts_extraction(self) -> None:
        result = parse_message_response(
            {"parts": [{"type": "text", "text": "part text"}]}
        )
        assert result.text == "part text"

    def test_info_error_fallback(self) -> None:
        result = parse_message_response({"info": {"error": {"message": "info error"}}})
        assert result.error == "info error"


class TestRecoverLastAssistantMessage:
    """Last assistant message recovery from message lists."""

    def test_recovers_last_assistant_text(self) -> None:
        messages = [
            {"role": "user", "text": "hello"},
            {"info": {"role": "assistant"}, "text": "response"},
        ]
        result = recover_last_assistant_message(messages)
        assert result.text == "response"

    def test_skips_user_messages(self) -> None:
        messages = [
            {"info": {"role": "user"}, "text": "question"},
            {"info": {"role": "assistant"}, "text": "answer"},
        ]
        result = recover_last_assistant_message(messages)
        assert result.text == "answer"

    def test_data_key_list(self) -> None:
        result = recover_last_assistant_message(
            {"data": [{"info": {"role": "assistant"}, "text": "hi"}]}
        )
        assert result.text == "hi"

    def test_empty_returns_empty(self) -> None:
        result = recover_last_assistant_message([])
        assert result.text == ""

    def test_prompt_echo_skipped(self) -> None:
        messages = [
            {"info": {"role": "assistant"}, "text": "prompt text"},
        ]
        result = recover_last_assistant_message(messages, prompt="prompt text")
        assert result.text == ""

    def test_prompt_echo_not_matching_included(self) -> None:
        messages = [
            {"info": {"role": "assistant"}, "text": "actual response"},
        ]
        result = recover_last_assistant_message(messages, prompt="prompt text")
        assert result.text == "actual response"


class TestPromptEchoMatches:
    def test_exact_match(self) -> None:
        assert prompt_echo_matches("hello", prompt="hello") is True

    def test_whitespace_normalized(self) -> None:
        assert prompt_echo_matches("  hello  ", prompt="hello") is True

    def test_no_match(self) -> None:
        assert prompt_echo_matches("hello", prompt="world") is False

    def test_none_prompt(self) -> None:
        assert prompt_echo_matches("hello", prompt=None) is False

    def test_non_string_text(self) -> None:
        assert prompt_echo_matches(42, prompt="hello") is False


class TestExtractTotalTokens:
    """Usage total-token extraction with alias keys."""

    def test_total_tokens_key(self) -> None:
        assert extract_total_tokens({"totalTokens": 100}) == 100

    def test_total_tokens_snake(self) -> None:
        assert extract_total_tokens({"total_tokens": 100}) == 100

    def test_total_key(self) -> None:
        assert extract_total_tokens({"total": 100}) == 100

    def test_computed_from_components(self) -> None:
        usage = {"inputTokens": 60, "outputTokens": 40}
        assert extract_total_tokens(usage) == 100

    def test_empty_returns_none(self) -> None:
        assert extract_total_tokens({}) is None


class TestExtractUsageDetails:
    """Usage detail extraction with alias keys."""

    def test_all_fields(self) -> None:
        usage = {
            "inputTokens": 10,
            "outputTokens": 20,
            "reasoningTokens": 5,
            "cachedInputTokens": 3,
        }
        details = extract_usage_details(usage)
        assert details["inputTokens"] == 10
        assert details["outputTokens"] == 20
        assert details["reasoningTokens"] == 5
        assert details["cachedInputTokens"] == 3

    def test_partial_fields(self) -> None:
        details = extract_usage_details({"inputTokens": 10})
        assert details["inputTokens"] == 10
        assert "outputTokens" not in details

    def test_empty(self) -> None:
        assert extract_usage_details({}) == {}
