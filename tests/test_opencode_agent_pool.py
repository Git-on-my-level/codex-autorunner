import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import (
    ConversationRef,
    RuntimeCapability,
    TerminalTurnResult,
    TurnRef,
)
from codex_autorunner.core.config import TicketFlowConfig
from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.orchestration import ColdTraceStore
from codex_autorunner.core.orchestration.turn_timeline import list_turn_timeline
from codex_autorunner.integrations.agents.agent_pool_impl import DefaultAgentPool
from codex_autorunner.tickets.agent_pool import AgentTurnRequest


@dataclass
class _HarnessScript:
    assistant_text: str
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    errors: list[str] = field(default_factory=list)
    release_event: Optional[asyncio.Event] = None
    started_event: Optional[asyncio.Event] = None
    streamed_raw_events: Optional[list[dict[str, Any]]] = None
    stream_pause_after: Optional[int] = None
    stream_release_event: Optional[asyncio.Event] = None
    stream_started_event: Optional[asyncio.Event] = None


class _FakeHarness:
    agent_id = "fake"
    display_name = "Fake Harness"
    capabilities = frozenset(
        [
            RuntimeCapability("durable_threads"),
            RuntimeCapability("message_turns"),
            RuntimeCapability("event_streaming"),
        ]
    )

    def __init__(
        self,
        scripts: list[_HarnessScript],
        *,
        allow_parallel_event_stream: bool = True,
    ) -> None:
        self._scripts = list(scripts)
        self._turns: dict[tuple[str, str], _HarnessScript] = {}
        self.calls: list[dict[str, object]] = []
        self.resume_calls: list[str] = []
        self._session_counter = 0
        self._turn_counter = 0
        self._allow_parallel_event_stream = allow_parallel_event_stream
        self.stream_event_calls = 0

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    def supports(self, capability: str) -> bool:
        return RuntimeCapability(capability) in self.capabilities

    def allows_parallel_event_stream(self) -> bool:
        return self._allow_parallel_event_stream

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        _ = workspace_root, title
        self._session_counter += 1
        return ConversationRef(
            agent=self.agent_id, id=f"session-{self._session_counter}"
        )

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        _ = workspace_root
        self.resume_calls.append(conversation_id)
        return ConversationRef(agent=self.agent_id, id=conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        _ = workspace_root, input_items
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "prompt": prompt,
                "model": model,
                "reasoning": reasoning,
                "approval_mode": approval_mode,
                "sandbox_policy": sandbox_policy,
            }
        )
        script = self._scripts.pop(0)
        self._turns[(conversation_id, turn_id)] = script
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, timeout
        if turn_id is None:
            raise ValueError("turn_id is required")
        script = self._turns[(conversation_id, turn_id)]
        if script.started_event is not None:
            script.started_event.set()
        if script.stream_started_event is not None:
            await script.stream_started_event.wait()
        if script.release_event is not None:
            await script.release_event.wait()
        return TerminalTurnResult(
            status=script.status,
            assistant_text=script.assistant_text,
            errors=list(script.errors),
            raw_events=list(script.raw_events),
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id, turn_id

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root
        self.stream_event_calls += 1
        script = self._turns[(conversation_id, turn_id)]
        streamed_raw_events = script.streamed_raw_events or script.raw_events
        for index, payload in enumerate(streamed_raw_events, start=1):
            yield payload
            if index == 1 and script.stream_started_event is not None:
                script.stream_started_event.set()
            if (
                script.stream_pause_after is not None
                and index >= script.stream_pause_after
                and script.stream_release_event is not None
            ):
                await script.stream_release_event.wait()


class _FakeCloser:
    def __init__(self) -> None:
        self.closed = False

    async def close_all(self) -> None:
        self.closed = True


@dataclass
class _ContextualHarnessFactory:
    harnesses: dict[tuple[str, Optional[str]], _FakeHarness]
    calls: list[tuple[str, Optional[str]]] = field(default_factory=list)

    def make_harness(self, ctx: Any):
        requested_agent = getattr(ctx, "_requested_agent_id", None)
        requested_profile = getattr(ctx, "_requested_agent_profile", None)
        key = (
            str(requested_agent or "").strip().lower(),
            (
                str(requested_profile).strip().lower()
                if isinstance(requested_profile, str) and requested_profile.strip()
                else None
            ),
        )
        self.calls.append(key)
        harness = self.harnesses.get(key)
        if harness is None:
            raise AssertionError(f"Unexpected harness resolution request: {key!r}")
        return harness


def _build_descriptor(
    agent_id: str,
    *,
    capabilities: Optional[frozenset[RuntimeCapability]] = None,
) -> AgentDescriptor:
    return AgentDescriptor(
        id=agent_id,
        name=agent_id.title(),
        capabilities=capabilities
        or frozenset(
            {
                RuntimeCapability("durable_threads"),
                RuntimeCapability("message_turns"),
                RuntimeCapability("event_streaming"),
            }
        ),
        make_harness=lambda ctx: ctx.fake_harness,
    )


def _build_contextual_descriptor(
    agent_id: str,
    factory: _ContextualHarnessFactory,
    *,
    runtime_kind: Optional[str] = None,
    capabilities: Optional[frozenset[RuntimeCapability]] = None,
) -> AgentDescriptor:
    return AgentDescriptor(
        id=agent_id,
        name=agent_id.title(),
        capabilities=capabilities
        or frozenset(
            {
                RuntimeCapability("durable_threads"),
                RuntimeCapability("message_turns"),
                RuntimeCapability("event_streaming"),
            }
        ),
        make_harness=factory.make_harness,
        runtime_kind=runtime_kind,
    )


def _make_pool(
    tmp_path: Path,
    harness: _FakeHarness,
    *,
    approval_mode: str,
    descriptors: Optional[dict[str, AgentDescriptor]] = None,
) -> DefaultAgentPool:
    cfg = SimpleNamespace(
        root=tmp_path,
        ticket_flow=TicketFlowConfig(
            approval_mode=approval_mode,
            default_approval_decision="accept",
            include_previous_ticket_context=False,
        ),
    )
    pool = DefaultAgentPool(cfg)  # type: ignore[arg-type]
    pool._agent_descriptors_override = descriptors or {  # type: ignore[attr-defined]
        "codex": _build_descriptor("codex"),
        "opencode": _build_descriptor("opencode"),
    }
    pool._harness_context_override = SimpleNamespace(fake_harness=harness)  # type: ignore[attr-defined]
    return pool


def _message(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"message": {"method": method, "params": params}}


@pytest.mark.asyncio
async def test_run_turn_maps_runtime_events_to_result_and_emits(tmp_path: Path):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="hello",
                raw_events=[
                    _message(
                        "outputDelta",
                        {
                            "turnId": "turn-1",
                            "delta": "hello",
                            "deltaType": "assistant_stream",
                        },
                    ),
                    _message(
                        "outputDelta",
                        {
                            "turnId": "turn-1",
                            "delta": "log line",
                            "deltaType": "log_line",
                        },
                    ),
                    _message(
                        "thread/tokenUsage/updated",
                        {
                            "turnId": "turn-1",
                            "tokenUsage": {"input": 3, "output": 5},
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "hello"
    assert result.error is None
    assert result.conversation_id
    assert result.turn_id == "turn-1"
    assert result.raw["final_status"] == "completed"
    assert result.raw["log_lines"] == ["log line"]
    assert result.raw["token_usage"] == {"input": 3, "output": 5}
    assert isinstance(result.raw["execution_id"], str)
    assert result.raw["backend_thread_id"] == "session-1"

    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.AGENT_STREAM_DELTA,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.TOKEN_USAGE,
    ]
    assert emitted[0][1]["message"]["method"] == "outputDelta"
    assert emitted[1][1]["delta"] == "hello"


@pytest.mark.asyncio
async def test_run_turn_replays_final_events_when_parallel_streaming_is_disabled(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="hello",
                raw_events=[
                    _message(
                        "outputDelta",
                        {
                            "turnId": "turn-1",
                            "delta": "hello",
                            "deltaType": "assistant_stream",
                        },
                    )
                ],
            )
        ],
        allow_parallel_event_stream=False,
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "hello"
    assert harness.stream_event_calls == 0
    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.AGENT_STREAM_DELTA,
    ]


@pytest.mark.asyncio
async def test_run_turn_skips_live_stream_when_parallel_streaming_is_disabled(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="hello",
                raw_events=[
                    _message(
                        "outputDelta",
                        {
                            "turnId": "turn-1",
                            "delta": "hello",
                            "deltaType": "assistant_stream",
                        },
                    )
                ],
            )
        ],
        allow_parallel_event_stream=False,
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "hello"
    assert harness.stream_event_calls == 0
    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.AGENT_STREAM_DELTA,
    ]


@pytest.mark.asyncio
async def test_run_turn_mirrors_opencode_text_parts_after_assistant_role_resolution(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="hello",
                raw_events=[
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-1",
                                    "messageID": "assistant-1",
                                    "type": "text",
                                    "text": "hello",
                                },
                                "delta": "hello",
                            }
                        },
                    ),
                    _message(
                        "message.updated",
                        {
                            "properties": {
                                "info": {
                                    "id": "assistant-1",
                                    "role": "assistant",
                                }
                            }
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "hello"
    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.AGENT_STREAM_DELTA,
    ]
    assert emitted[2][1]["delta"] == "hello"


@pytest.mark.asyncio
async def test_run_turn_mirrors_opencode_text_part_deltas_with_direct_ids(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="Hello world",
                raw_events=[
                    _message(
                        "message.updated",
                        {
                            "properties": {
                                "info": {
                                    "id": "assistant-1",
                                    "role": "assistant",
                                }
                            }
                        },
                    ),
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-1",
                                    "messageID": "assistant-1",
                                    "type": "text",
                                    "text": "Hello",
                                },
                                "delta": "Hello",
                            }
                        },
                    ),
                    _message(
                        "message.part.delta",
                        {
                            "properties": {
                                "partID": "part-1",
                                "messageID": "assistant-1",
                                "delta": " world",
                            }
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "Hello world"
    mirrored = [
        payload["delta"]
        for event_type, payload in emitted
        if event_type == FlowEventType.AGENT_STREAM_DELTA
    ]
    assert mirrored == ["Hello", " world"]


@pytest.mark.asyncio
async def test_run_turn_preserves_whitespace_in_opencode_text_part_deltas(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="Debug of OpenCode  \n",
                raw_events=[
                    _message(
                        "message.updated",
                        {
                            "properties": {
                                "info": {
                                    "id": "assistant-1",
                                    "role": "assistant",
                                }
                            }
                        },
                    ),
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-1",
                                    "messageID": "assistant-1",
                                    "type": "text",
                                    "text": "Debug",
                                },
                                "delta": "Debug",
                            }
                        },
                    ),
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-1",
                                    "messageID": "assistant-1",
                                    "type": "text",
                                    "text": "Debug of",
                                },
                                "delta": " of",
                            }
                        },
                    ),
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-1",
                                    "messageID": "assistant-1",
                                    "type": "text",
                                    "text": "Debug of OpenCode  \n",
                                },
                                "delta": " OpenCode  \n",
                            }
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    mirrored = [
        payload["delta"]
        for event_type, payload in emitted
        if event_type == FlowEventType.AGENT_STREAM_DELTA
    ]
    assert mirrored == ["Debug", " of", " OpenCode  \n"]


@pytest.mark.asyncio
async def test_run_turn_does_not_mirror_opencode_user_text_parts(tmp_path: Path):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="",
                raw_events=[
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-user-1",
                                    "messageID": "user-1",
                                    "type": "text",
                                    "text": "user prompt",
                                },
                                "delta": "user prompt",
                            }
                        },
                    ),
                    _message(
                        "message.updated",
                        {
                            "properties": {
                                "info": {
                                    "id": "user-1",
                                    "role": "user",
                                }
                            }
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
    ]


@pytest.mark.asyncio
async def test_run_turn_does_not_mirror_reasoning_part_deltas_with_missing_part_type(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="",
                raw_events=[
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "reason-1",
                                    "type": "reasoning",
                                },
                                "delta": "thinking",
                            }
                        },
                    ),
                    _message(
                        "message.part.delta",
                        {
                            "properties": {
                                "partID": "reason-1",
                                "delta": " more",
                            }
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
    ]


@pytest.mark.asyncio
async def test_run_turn_does_not_mirror_cumulative_opencode_text_snapshots_without_delta(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="hello",
                raw_events=[
                    _message(
                        "message.updated",
                        {
                            "properties": {
                                "info": {
                                    "id": "assistant-1",
                                    "role": "assistant",
                                }
                            }
                        },
                    ),
                    _message(
                        "message.part.updated",
                        {
                            "properties": {
                                "part": {
                                    "id": "part-1",
                                    "messageID": "assistant-1",
                                    "type": "text",
                                    "text": "hello",
                                }
                            }
                        },
                    ),
                ],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    emitted = []

    def _emit(event_type: FlowEventType, payload: dict):
        emitted.append((event_type, payload))

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main prompt",
            workspace_root=tmp_path,
            emit_event=_emit,
        )
    )

    assert result.text == "hello"
    assert [event_type for event_type, _ in emitted] == [
        FlowEventType.APP_SERVER_EVENT,
        FlowEventType.APP_SERVER_EVENT,
    ]


@pytest.mark.asyncio
async def test_run_turn_handles_failure_and_returns_error(tmp_path: Path):
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="",
                status="error",
                errors=["boom"],
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="review")

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main",
            workspace_root=tmp_path,
        )
    )

    assert result.error == "boom"
    assert result.turn_id == "turn-1"
    assert result.raw["final_status"] == "failed"
    assert result.raw["log_lines"] == []
    assert result.raw["token_usage"] is None
    assert isinstance(result.raw["execution_id"], str)
    assert result.raw["backend_thread_id"] == "session-1"


@pytest.mark.asyncio
async def test_run_turn_persists_full_timeline_from_raw_events_after_partial_live_stream(
    tmp_path: Path,
):
    streamed_raw_events = [
        _message(
            "item/toolCall/start",
            {"item": {"toolCall": {"name": "shell", "input": {"cmd": "pwd"}}}},
        ),
        _message(
            "item/toolCall/end",
            {"name": "shell", "result": {"stdout": str(tmp_path)}},
        ),
    ]
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="done",
                raw_events=streamed_raw_events[1:],
                streamed_raw_events=streamed_raw_events,
                stream_pause_after=1,
                stream_release_event=asyncio.Event(),
                stream_started_event=asyncio.Event(),
            )
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="codex",
            prompt="main",
            workspace_root=tmp_path,
        )
    )

    timeline = list_turn_timeline(
        tmp_path,
        execution_id=str(result.raw["execution_id"]),
    )

    assert [entry["event_type"] for entry in timeline] == [
        "tool_call",
        "tool_result",
        "turn_completed",
    ]
    assert timeline[1]["event"]["result"] == {"stdout": str(tmp_path)}
    checkpoint = ColdTraceStore(tmp_path).load_checkpoint(
        str(result.raw["execution_id"])
    )
    assert checkpoint is not None
    assert checkpoint.trace_manifest_id
    manifest = ColdTraceStore(tmp_path).get_manifest_by_trace_id(
        checkpoint.trace_manifest_id
    )
    assert manifest is not None
    assert manifest.event_count == 3


@pytest.mark.asyncio
async def test_run_turn_passes_model_reasoning_and_reuses_thread_target(
    tmp_path: Path,
):
    harness = _FakeHarness(
        [
            _HarnessScript(assistant_text="first"),
            _HarnessScript(assistant_text="second"),
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="review")

    first = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main",
            workspace_root=tmp_path,
        )
    )
    second = await pool.run_turn(
        AgentTurnRequest(
            agent_id="opencode",
            prompt="main",
            workspace_root=tmp_path,
            conversation_id=first.conversation_id,
            options={
                "model": {"providerID": "provider", "modelID": "model-x"},
                "reasoning": "high",
            },
            additional_messages=[
                {"text": "more"},
                {"text": "  "},
                {"text": "end"},
            ],
        )
    )

    assert first.conversation_id == second.conversation_id
    assert harness.resume_calls == ["session-1"]
    assert harness.calls[1]["conversation_id"] == "session-1"
    assert harness.calls[1]["model"] == "provider/model-x"
    assert harness.calls[1]["reasoning"] == "high"
    assert harness.calls[1]["prompt"] == "main\n\nmore\n\nend"
    assert harness.calls[1]["approval_mode"] == "on-request"
    assert harness.calls[1]["sandbox_policy"] == "workspaceWrite"


@pytest.mark.asyncio
async def test_run_turn_supports_runtime_backed_hermes_agent(tmp_path: Path):
    harness = _FakeHarness([_HarnessScript(assistant_text="hermes response")])
    pool = _make_pool(
        tmp_path,
        harness,
        approval_mode="review",
        descriptors={
            "codex": _build_descriptor("codex"),
            "opencode": _build_descriptor("opencode"),
            "hermes": _build_descriptor("hermes"),
        },
    )

    result = await pool.run_turn(
        AgentTurnRequest(
            agent_id="hermes",
            prompt="main prompt",
            workspace_root=tmp_path,
        )
    )

    assert result.text == "hermes response"
    assert result.error is None
    assert result.raw["backend_thread_id"] == "session-1"

    threads = pool._thread_store.list_threads(agent="hermes", limit=1)
    assert len(threads) == 1
    assert threads[0]["name"] == "ticket-flow:hermes"
    assert harness.calls[0]["conversation_id"] == "session-1"


@pytest.mark.asyncio
async def test_run_turn_uses_profile_aware_runtime_resolution_for_hermes_queue_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    alias_harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="done-1",
                started_event=first_started,
                release_event=release_first,
            ),
            _HarnessScript(assistant_text="done-2"),
        ]
    )
    factory = _ContextualHarnessFactory(
        harnesses={("hermes-m4-pma", None): alias_harness}
    )
    pool = _make_pool(
        tmp_path,
        alias_harness,
        approval_mode="review",
        descriptors={
            "hermes": _build_contextual_descriptor("hermes", factory),
            "hermes-m4-pma": _build_contextual_descriptor(
                "hermes-m4-pma",
                factory,
                runtime_kind="hermes",
            ),
        },
    )

    monkeypatch.setattr(
        "codex_autorunner.integrations.agents.agent_pool_impl.resolve_agent_runtime",
        lambda agent_id, profile=None, context=None: (
            SimpleNamespace(
                logical_agent_id="hermes",
                logical_profile="m4-pma",
                runtime_agent_id="hermes-m4-pma",
                runtime_profile=None,
                resolution_kind="alias_profile",
            )
            if agent_id == "hermes" and profile == "m4-pma"
            else SimpleNamespace(
                logical_agent_id=agent_id,
                logical_profile=profile,
                runtime_agent_id=agent_id,
                runtime_profile=profile,
                resolution_kind="passthrough",
            )
        ),
    )

    options = {
        "profile": "m4-pma",
        "ticket_flow_run_id": "run-123",
        "ticket_id": "tkt-123",
        "ticket_path": ".codex-autorunner/tickets/TICKET-123.md",
    }

    first_task = asyncio.create_task(
        pool.run_turn(
            AgentTurnRequest(
                agent_id="hermes",
                prompt="first",
                workspace_root=tmp_path,
                options=options,
            )
        )
    )
    await first_started.wait()

    thread = pool._thread_store.list_threads(agent="hermes", limit=1)[0]
    thread_id = str(thread["managed_thread_id"])
    assert thread["name"] == "ticket-flow:hermes@m4-pma"
    assert thread["metadata"]["agent_profile"] == "m4-pma"
    assert thread["metadata"]["run_id"] == "run-123"
    assert thread["metadata"]["ticket_id"] == "tkt-123"
    assert thread["metadata"]["ticket_path"] == options["ticket_path"]

    second_task = asyncio.create_task(
        pool.run_turn(
            AgentTurnRequest(
                agent_id="hermes",
                prompt="second",
                workspace_root=tmp_path,
                conversation_id=thread_id,
                options=options,
            )
        )
    )
    await asyncio.sleep(0)

    service = pool._get_orchestration_service()  # type: ignore[attr-defined]
    assert service.get_running_execution(thread_id) is not None
    assert len(service.list_queued_executions(thread_id)) == 1

    release_first.set()
    first_result = await first_task
    second_result = await second_task

    assert first_result.text == "done-1"
    assert second_result.text == "done-2"
    assert first_result.conversation_id == second_result.conversation_id == thread_id
    assert factory.calls == [
        ("hermes-m4-pma", None),
        ("hermes-m4-pma", None),
        ("hermes-m4-pma", None),
    ]
    assert alias_harness.calls[0]["conversation_id"] == "session-1"
    assert alias_harness.calls[1]["conversation_id"] == "session-1"


@pytest.mark.asyncio
async def test_run_turn_fails_early_when_ticket_flow_capability_is_missing(
    tmp_path: Path,
):
    harness = _FakeHarness([_HarnessScript(assistant_text="should not run")])
    pool = _make_pool(
        tmp_path,
        harness,
        approval_mode="review",
        descriptors={
            "broken": _build_descriptor(
                "broken",
                capabilities=frozenset({RuntimeCapability("durable_threads")}),
            )
        },
    )

    with pytest.raises(ValueError) as excinfo:
        await pool.run_turn(
            AgentTurnRequest(
                agent_id="broken",
                prompt="main prompt",
                workspace_root=tmp_path,
            )
        )

    assert (
        str(excinfo.value)
        == "Agent 'broken' does not support ticket-flow execution (missing capability: message_turns)"
    )
    assert harness.calls == []


@pytest.mark.asyncio
async def test_run_turn_queues_busy_delegated_thread_and_shows_active_work(
    tmp_path: Path,
):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    harness = _FakeHarness(
        [
            _HarnessScript(
                assistant_text="done-1",
                started_event=first_started,
                release_event=release_first,
            ),
            _HarnessScript(assistant_text="done-2"),
        ]
    )
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")

    first_task = asyncio.create_task(
        pool.run_turn(
            AgentTurnRequest(
                agent_id="codex",
                prompt="first",
                workspace_root=tmp_path,
            )
        )
    )
    await first_started.wait()

    thread = pool._thread_store.list_threads(agent="codex", limit=1)[0]
    thread_id = str(thread["managed_thread_id"])

    second_task = asyncio.create_task(
        pool.run_turn(
            AgentTurnRequest(
                agent_id="codex",
                prompt="second",
                workspace_root=tmp_path,
                conversation_id=thread_id,
            )
        )
    )
    await asyncio.sleep(0)

    service = pool._get_orchestration_service()  # type: ignore[attr-defined]
    assert service.get_running_execution(thread_id) is not None
    assert len(service.list_queued_executions(thread_id)) == 1
    assert harness.calls[0]["conversation_id"] == "session-1"

    release_first.set()
    first_result = await first_task
    second_result = await second_task

    assert first_result.conversation_id == thread_id
    assert second_result.conversation_id == thread_id
    assert first_result.text == "done-1"
    assert second_result.text == "done-2"
    assert len(harness.calls) == 2
    assert harness.calls[1]["conversation_id"] == "session-1"


@pytest.mark.asyncio
async def test_close_all_closes_runtime_supervisors(tmp_path: Path):
    harness = _FakeHarness([_HarnessScript(assistant_text="done")])
    pool = _make_pool(tmp_path, harness, approval_mode="yolo")
    codex_supervisor = _FakeCloser()
    opencode_supervisor = _FakeCloser()
    hermes_supervisor = _FakeCloser()
    pool._runtime_context = SimpleNamespace(  # type: ignore[attr-defined]
        app_server_supervisor=codex_supervisor,
        opencode_supervisor=opencode_supervisor,
        hermes_supervisor=hermes_supervisor,
        zeroclaw_supervisor=None,
    )

    await pool.close_all()

    assert codex_supervisor.closed is True
    assert opencode_supervisor.closed is True
    assert hermes_supervisor.closed is True
