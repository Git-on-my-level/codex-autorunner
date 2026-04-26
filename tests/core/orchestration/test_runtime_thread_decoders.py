from __future__ import annotations

from typing import Any

from codex_autorunner.core.acp_lifecycle import analyze_acp_lifecycle_message
from codex_autorunner.core.orchestration.runtime_thread_decoders import (
    ACPPromptTurnDecoder,
    CodexItemDecoder,
    DecoderContext,
    ErrorDecoder,
    LifecycleBoundaryDecoder,
    MessageDecoder,
    OpenCodeMessageDecoder,
    PermissionDecoder,
    SessionUpdateDecoder,
    StreamDeltaDecoder,
    UsageDecoder,
    build_default_decoder_registry,
)
from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
)
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Failed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
)


def _ctx(
    method: str,
    params: dict[str, Any],
    *,
    state: RuntimeThreadRunEventState | None = None,
) -> tuple[RuntimeThreadRunEventState, DecoderContext]:
    if state is None:
        state = RuntimeThreadRunEventState()
    raw_message = {"method": method, "params": params}
    acp_lifecycle = analyze_acp_lifecycle_message(raw_message)
    ctx = DecoderContext(
        timestamp="2026-01-01T00:00:00Z",
        raw_message=raw_message,
        acp_lifecycle=acp_lifecycle,
    )
    return state, ctx


class TestDecoderRegistry:
    def test_exact_method_lookup(self) -> None:
        registry = build_default_decoder_registry()
        assert registry._exact.get("token/usage") is not None
        assert registry._exact.get("item/completed") is not None
        assert registry._exact.get("session/update") is not None
        assert registry._exact.get("turn/started") is not None
        assert registry._exact.get("session/created") is not None

    def test_unknown_method_returns_empty(self) -> None:
        registry = build_default_decoder_registry()
        state = RuntimeThreadRunEventState()
        _, ctx = _ctx("nonexistent/method", {})
        assert registry.decode("nonexistent/method", {}, state, ctx) == []

    def test_fallback_match_for_outputdelta(self) -> None:
        registry = build_default_decoder_registry()
        state = RuntimeThreadRunEventState()
        _, ctx = _ctx("some/outputdelta/event", {"delta": "text"})
        events = registry.decode(
            "some/outputdelta/event", {"delta": "text"}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)

    def test_can_register_additional_decoder(self) -> None:
        class CustomDecoder(MessageDecoder):
            def methods(self):
                return frozenset({"custom/event"})

            def decode(self, method, params, state, ctx):
                return [RunNotice(timestamp=ctx.timestamp, kind="custom", message="hi")]

        registry = build_default_decoder_registry()
        registry.register(CustomDecoder())
        state = RuntimeThreadRunEventState()
        _, ctx = _ctx("custom/event", {})
        events = registry.decode("custom/event", {}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "custom"

    def test_all_decoders_registered(self) -> None:
        registry = build_default_decoder_registry()
        assert len(registry._decoders) == 9


class TestLifecycleBoundaryDecoder:
    def setup_method(self) -> None:
        self.decoder = LifecycleBoundaryDecoder()

    def test_methods_cover_acp_lifecycle_boundaries(self) -> None:
        assert self.decoder.methods() == frozenset(
            {
                "turn/started",
                "prompt/started",
                "session/created",
                "session/loaded",
            }
        )

    def test_decode_silently_consumes_lifecycle_boundary_events(self) -> None:
        state, ctx = _ctx("turn/started", {"turnId": "turn-1", "promptId": "prompt-1"})
        events = self.decoder.decode(
            "turn/started",
            {"turnId": "turn-1", "promptId": "prompt-1"},
            state,
            ctx,
        )
        assert events == []


class TestCodexItemDecoder:
    def setup_method(self) -> None:
        self.decoder = CodexItemDecoder()

    def test_methods_covers_item_family(self) -> None:
        methods = self.decoder.methods()
        assert "item/started" in methods
        assert "item/reasoning/summaryTextDelta" in methods
        assert "item/completed" in methods
        assert "item/agentMessage/delta" in methods
        assert "item/toolCall/start" in methods
        assert "item/toolCall/end" in methods
        assert "item/commandExecution/requestApproval" in methods
        assert "item/fileChange/requestApproval" in methods

    def test_item_started_structural_event_returns_empty(self) -> None:
        state, ctx = _ctx(
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "reasoning"},
            },
        )
        events = self.decoder.decode(
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "reasoning"},
            },
            state,
            ctx,
        )
        assert events == []

    def test_item_started_review_mode_returns_progress_notice(self) -> None:
        state, ctx = _ctx(
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"enteredReviewMode": True},
            },
        )
        events = self.decoder.decode(
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"enteredReviewMode": True},
            },
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "progress"
        assert events[0].message == "entered review mode"

    def test_reasoning_delta(self) -> None:
        state, ctx = _ctx(
            "item/reasoning/summaryTextDelta",
            {"itemId": "r1", "delta": "thinking"},
        )
        events = self.decoder.decode(
            "item/reasoning/summaryTextDelta",
            {"itemId": "r1", "delta": "thinking"},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "thinking"
        assert events[0].message == "thinking"

    def test_item_completed_agent_message(self) -> None:
        state, ctx = _ctx(
            "item/completed",
            {"item": {"type": "agentMessage", "text": "hello"}},
        )
        events = self.decoder.decode(
            "item/completed",
            {"item": {"type": "agentMessage", "text": "hello"}},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "hello"
        assert state.best_assistant_text() == "hello"

    def test_item_completed_commentary_agent_message(self) -> None:
        state, ctx = _ctx(
            "item/completed",
            {"item": {"type": "agentMessage", "phase": "commentary", "text": "note"}},
        )
        events = self.decoder.decode(
            "item/completed",
            {"item": {"type": "agentMessage", "phase": "commentary", "text": "note"}},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "commentary"
        assert state.best_assistant_text() == ""

    def test_item_completed_reasoning_clears_buffer(self) -> None:
        state = RuntimeThreadRunEventState(
            reasoning_buffers={"r1": "accumulated thought"}
        )
        _, ctx = _ctx(
            "item/completed",
            {"item": {"type": "reasoning"}, "itemId": "r1"},
            state=state,
        )
        events = self.decoder.decode(
            "item/completed",
            {"item": {"type": "reasoning"}, "itemId": "r1"},
            state,
            ctx,
        )
        assert events == []
        assert "r1" not in state.reasoning_buffers

    def test_item_completed_tool_result(self) -> None:
        state, ctx = _ctx(
            "item/completed",
            {
                "item": {
                    "type": "commandExecution",
                    "exitCode": 0,
                    "result": "ok",
                },
            },
        )
        events = self.decoder.decode(
            "item/completed",
            {
                "item": {
                    "type": "commandExecution",
                    "exitCode": 0,
                    "result": "ok",
                },
            },
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], ToolResult)
        assert events[0].status == "completed"

    def test_agent_message_delta_streams(self) -> None:
        state, ctx = _ctx(
            "item/agentMessage/delta",
            {"delta": "partial"},
        )
        events = self.decoder.decode(
            "item/agentMessage/delta",
            {"delta": "partial"},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "partial"

    def test_tool_call_start(self) -> None:
        state, ctx = _ctx(
            "item/toolCall/start",
            {"item": {"toolCall": {"name": "shell", "input": {"cmd": "ls"}}}},
        )
        events = self.decoder.decode(
            "item/toolCall/start",
            {"item": {"toolCall": {"name": "shell", "input": {"cmd": "ls"}}}},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], ToolCall)
        assert events[0].tool_name == "shell"

    def test_tool_call_end(self) -> None:
        state, ctx = _ctx(
            "item/toolCall/end",
            {"name": "shell", "result": {"stdout": "/tmp"}},
        )
        events = self.decoder.decode(
            "item/toolCall/end",
            {"name": "shell", "result": {"stdout": "/tmp"}},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], ToolResult)
        assert events[0].status == "completed"

    def test_command_approval(self) -> None:
        state, ctx = _ctx(
            "item/commandExecution/requestApproval",
            {"command": ["rm", "-rf", "/tmp/test"]},
        )
        events = self.decoder.decode(
            "item/commandExecution/requestApproval",
            {"command": ["rm", "-rf", "/tmp/test"]},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], ApprovalRequested)
        assert "rm -rf /tmp/test" in events[0].description

    def test_file_change_approval(self) -> None:
        state, ctx = _ctx(
            "item/fileChange/requestApproval",
            {"files": ["src/main.py"]},
        )
        events = self.decoder.decode(
            "item/fileChange/requestApproval",
            {"files": ["src/main.py"]},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], ApprovalRequested)
        assert "src/main.py" in events[0].description

    def test_empty_delta_returns_empty(self) -> None:
        state, ctx = _ctx(
            "item/reasoning/summaryTextDelta",
            {"itemId": "r1", "delta": ""},
        )
        events = self.decoder.decode(
            "item/reasoning/summaryTextDelta",
            {"itemId": "r1", "delta": ""},
            state,
            ctx,
        )
        assert events == []

    def test_item_completed_no_item_returns_empty(self) -> None:
        state, ctx = _ctx("item/completed", {})
        events = self.decoder.decode("item/completed", {}, state, ctx)
        assert events == []


class TestACPPromptTurnDecoder:
    def setup_method(self) -> None:
        self.decoder = ACPPromptTurnDecoder()

    def test_methods_covers_prompt_turn_family(self) -> None:
        methods = self.decoder.methods()
        for m in [
            "prompt/output",
            "prompt/delta",
            "prompt/progress",
            "turn/progress",
            "prompt/message",
            "turn/message",
            "prompt/failed",
            "turn/failed",
            "prompt/completed",
            "turn/completed",
            "prompt/cancelled",
            "turn/cancelled",
        ]:
            assert m in methods, f"{m} not in methods"

    def test_prompt_output_streams_text(self) -> None:
        state, ctx = _ctx("prompt/output", {"delta": "hello"})
        events = self.decoder.decode("prompt/output", {"delta": "hello"}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "hello"

    def test_prompt_progress_with_message(self) -> None:
        state, ctx = _ctx("prompt/progress", {"message": "thinking"})
        events = self.decoder.decode(
            "prompt/progress", {"message": "thinking"}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].message == "thinking"

    def test_prompt_message(self) -> None:
        state, ctx = _ctx("prompt/message", {"text": "final"})
        events = self.decoder.decode("prompt/message", {"text": "final"}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "final"

    def test_prompt_failed(self) -> None:
        state, ctx = _ctx("prompt/failed", {"message": "boom"})
        events = self.decoder.decode("prompt/failed", {"message": "boom"}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], Failed)
        assert events[0].error_message == "boom"

    def test_prompt_completed(self) -> None:
        state, ctx = _ctx(
            "prompt/completed",
            {"status": "completed", "finalOutput": "done"},
        )
        events = self.decoder.decode(
            "prompt/completed",
            {"status": "completed", "finalOutput": "done"},
            state,
            ctx,
        )
        assert state.completed_seen is True
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "done"

    def test_prompt_cancelled_no_completed(self) -> None:
        state, ctx = _ctx("prompt/cancelled", {"status": "cancelled"})
        self.decoder.decode("prompt/cancelled", {"status": "cancelled"}, state, ctx)
        assert state.completed_seen is False

    def test_prompt_output_with_usage(self) -> None:
        state, ctx = _ctx("prompt/output", {"usage": {"totalTokens": 42}})
        events = self.decoder.decode(
            "prompt/output", {"usage": {"totalTokens": 42}}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert state.token_usage == {"totalTokens": 42}

    def test_empty_params_returns_empty(self) -> None:
        state, ctx = _ctx("prompt/output", {})
        events = self.decoder.decode("prompt/output", {}, state, ctx)
        assert events == []

    def test_commentary_message_becomes_notice(self) -> None:
        state, ctx = _ctx(
            "prompt/message",
            {"text": "internal note", "phase": "commentary"},
        )
        events = self.decoder.decode(
            "prompt/message",
            {"text": "internal note", "phase": "commentary"},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "commentary"
        assert state.best_assistant_text() == ""


class TestPermissionDecoder:
    def setup_method(self) -> None:
        self.decoder = PermissionDecoder()

    def test_methods(self) -> None:
        methods = self.decoder.methods()
        assert "permission/requested" in methods
        assert "session/request_permission" in methods
        assert "permission/decision" in methods
        assert "permission" in methods
        assert "question" in methods

    def test_permission_requested(self) -> None:
        state, ctx = _ctx(
            "permission/requested",
            {"requestId": "p1", "description": "Need approval"},
        )
        events = self.decoder.decode(
            "permission/requested",
            {"requestId": "p1", "description": "Need approval"},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], ApprovalRequested)
        assert events[0].request_id == "p1"

    def test_permission_decision_declined(self) -> None:
        state, ctx = _ctx(
            "permission/decision",
            {"decision": "decline", "description": "unsafe"},
        )
        events = self.decoder.decode(
            "permission/decision",
            {"decision": "decline", "description": "unsafe"},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "approval"
        assert "declined" in events[0].message.lower()

    def test_generic_permission(self) -> None:
        state, ctx = _ctx("permission", {"reason": "Needs check"})
        events = self.decoder.decode(
            "permission", {"reason": "Needs check"}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], ApprovalRequested)
        assert events[0].description == "Needs check"

    def test_question(self) -> None:
        state, ctx = _ctx("question", {"question": "Continue?"})
        events = self.decoder.decode("question", {"question": "Continue?"}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], ApprovalRequested)
        assert events[0].description == "Continue?"


class TestUsageDecoder:
    def setup_method(self) -> None:
        self.decoder = UsageDecoder()

    def test_methods(self) -> None:
        methods = self.decoder.methods()
        for m in [
            "token/usage",
            "usage",
            "turn/tokenUsage",
            "turn/usage",
            "thread/tokenUsage/updated",
        ]:
            assert m in methods

    def test_token_usage(self) -> None:
        state, ctx = _ctx("token/usage", {"usage": {"totalTokens": 42}})
        events = self.decoder.decode(
            "token/usage", {"usage": {"totalTokens": 42}}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)
        assert state.token_usage == {"totalTokens": 42}

    def test_usage_alias(self) -> None:
        state, ctx = _ctx("usage", {"usage": {"totalTokens": 10}})
        events = self.decoder.decode(
            "usage", {"usage": {"totalTokens": 10}}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)

    def test_empty_usage_returns_empty(self) -> None:
        state, ctx = _ctx("token/usage", {})
        events = self.decoder.decode("token/usage", {}, state, ctx)
        assert events == []

    def test_thread_token_usage_updated(self) -> None:
        state, ctx = _ctx(
            "thread/tokenUsage/updated",
            {"tokenUsage": {"totalTokens": 5}},
        )
        events = self.decoder.decode(
            "thread/tokenUsage/updated",
            {"tokenUsage": {"totalTokens": 5}},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)


class TestSessionUpdateDecoder:
    def setup_method(self) -> None:
        self.decoder = SessionUpdateDecoder()

    def test_methods(self) -> None:
        methods = self.decoder.methods()
        for m in ["session/update", "session.idle", "session.status", "session/status"]:
            assert m in methods

    def test_session_update_message_chunk(self) -> None:
        state, ctx = _ctx(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"delta": "hello"},
                },
            },
        )
        events = self.decoder.decode(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"delta": "hello"},
                },
            },
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "hello"

    def test_session_update_thought_chunk(self) -> None:
        state, ctx = _ctx(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"text": "thinking"},
                },
            },
        )
        events = self.decoder.decode(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"text": "thinking"},
                },
            },
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].kind == "progress"

    def test_session_update_usage(self) -> None:
        state, ctx = _ctx(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "usage_update",
                    "usage": {"totalTokens": 10},
                },
            },
        )
        events = self.decoder.decode(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "usage_update",
                    "usage": {"totalTokens": 10},
                },
            },
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], TokenUsage)

    def test_session_idle_marks_completed(self) -> None:
        state, ctx = _ctx("session.idle", {})
        events = self.decoder.decode("session.idle", {}, state, ctx)
        assert events == []
        assert state.completed_seen is True

    def test_session_status_progress(self) -> None:
        state, ctx = _ctx("session.status", {"status": {"type": "running"}})
        events = self.decoder.decode(
            "session.status", {"status": {"type": "running"}}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], RunNotice)
        assert events[0].message == "agent running"

    def test_session_status_idle_marks_completed(self) -> None:
        state, ctx = _ctx("session.status", {"status": {"type": "idle"}})
        events = self.decoder.decode(
            "session.status", {"status": {"type": "idle"}}, state, ctx
        )
        assert events == []
        assert state.completed_seen is True


class TestErrorDecoder:
    def setup_method(self) -> None:
        self.decoder = ErrorDecoder()

    def test_methods(self) -> None:
        assert "turn/error" in self.decoder.methods()
        assert "error" in self.decoder.methods()

    def test_turn_error(self) -> None:
        state, ctx = _ctx("turn/error", {"message": "crash"})
        events = self.decoder.decode("turn/error", {"message": "crash"}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], Failed)
        assert events[0].error_message == "crash"
        assert state.last_error_message == "crash"

    def test_generic_error(self) -> None:
        state, ctx = _ctx("error", {"error": {"message": "Auth required"}})
        events = self.decoder.decode(
            "error", {"error": {"message": "Auth required"}}, state, ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], Failed)
        assert events[0].error_message == "Auth required"

    def test_turn_error_default_message(self) -> None:
        state, ctx = _ctx("turn/error", {})
        events = self.decoder.decode("turn/error", {}, state, ctx)
        assert len(events) == 1
        assert events[0].error_message == "Turn error"


class TestStreamDeltaDecoder:
    def setup_method(self) -> None:
        self.decoder = StreamDeltaDecoder()

    def test_exact_method(self) -> None:
        assert "turn/streamDelta" in self.decoder.methods()

    def test_can_decode_outputdelta_variant(self) -> None:
        assert self.decoder.can_decode("some/outputdelta/method")

    def test_cannot_decode_normal_method(self) -> None:
        assert not self.decoder.can_decode("token/usage")

    def test_decode_streams(self) -> None:
        state, ctx = _ctx("turn/streamDelta", {"delta": "chunk"})
        events = self.decoder.decode("turn/streamDelta", {"delta": "chunk"}, state, ctx)
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "chunk"


class TestOpenCodeMessageDecoder:
    def setup_method(self) -> None:
        self.decoder = OpenCodeMessageDecoder()

    def test_methods(self) -> None:
        methods = self.decoder.methods()
        for m in [
            "message.part.updated",
            "message.part.delta",
            "message.updated",
            "message.completed",
            "message.delta",
        ]:
            assert m in methods

    def test_message_delta_with_text(self) -> None:
        state, ctx = _ctx(
            "message.delta",
            {"properties": {"delta": {"text": "hello"}}},
        )
        events = self.decoder.decode(
            "message.delta",
            {"properties": {"delta": {"text": "hello"}}},
            state,
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], OutputDelta)
        assert events[0].content == "hello"

    def test_message_delta_with_reasoning_part_dropped(self) -> None:
        state, ctx = _ctx(
            "message.delta",
            {
                "properties": {
                    "delta": {"text": "thinking"},
                    "part": {"id": "r1", "type": "reasoning"},
                }
            },
        )
        events = self.decoder.decode(
            "message.delta",
            {
                "properties": {
                    "delta": {"text": "thinking"},
                    "part": {"id": "r1", "type": "reasoning"},
                }
            },
            state,
            ctx,
        )
        assert events == []

    def test_message_updated_assistant_role(self) -> None:
        state, ctx = _ctx(
            "message.updated",
            {
                "info": {"id": "msg-1", "role": "assistant"},
                "text": "final answer",
            },
        )
        events = self.decoder.decode(
            "message.updated",
            {
                "info": {"id": "msg-1", "role": "assistant"},
                "text": "final answer",
            },
            state,
            ctx,
        )
        assert len(events) >= 1
        assert any(isinstance(e, OutputDelta) for e in events)

    def test_message_completed_user_role_no_output(self) -> None:
        state, ctx = _ctx(
            "message.completed",
            {
                "info": {"id": "msg-1", "role": "user"},
                "text": "user prompt",
            },
        )
        events = self.decoder.decode(
            "message.completed",
            {
                "info": {"id": "msg-1", "role": "user"},
                "text": "user prompt",
            },
            state,
            ctx,
        )
        assert all(not isinstance(e, OutputDelta) for e in events)
