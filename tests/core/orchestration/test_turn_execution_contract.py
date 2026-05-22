from __future__ import annotations

import pytest

from codex_autorunner.core.hub_control_plane import ExecutionCreateRequest
from codex_autorunner.core.orchestration.models import QueuedExecutionRequest
from codex_autorunner.core.orchestration.turn_assistant_output import (
    TurnAssistantOutput,
)
from codex_autorunner.core.orchestration.turn_execution_contract import (
    TURN_EXECUTION_CONTRACT_VERSION,
    DeliveryIntentRef,
    TurnExecutionContractError,
    TurnExecutionOrigin,
    TurnExecutionRecord,
    TurnExecutionRequest,
)


def test_contract_symbols_are_exported_from_orchestration_package() -> None:
    from codex_autorunner.core import orchestration

    assert orchestration.TurnExecutionRequest is TurnExecutionRequest
    assert orchestration.TurnExecutionRecord is TurnExecutionRecord
    assert orchestration.TurnExecutionContractError is TurnExecutionContractError


def _origin() -> TurnExecutionOrigin:
    return TurnExecutionOrigin(
        kind="surface",
        source_id="discord:channel-1:message-1",
        surface_kind="discord",
        surface_key="channel-1",
        metadata={"message_id": "message-1"},
    )


def _request(**overrides: object) -> TurnExecutionRequest:
    values: dict[str, object] = {
        "request_id": "req-1",
        "target_id": "thread-1",
        "target_kind": "thread",
        "workspace_root": "/repo",
        "request_kind": "message",
        "busy_policy": "queue",
        "prompt_text": "hello",
        "input_items": ({"type": "text", "text": "hello"},),
        "context_profile": "car_core",
        "agent": "opencode",
        "profile": "code",
        "model": "zai-coding-plan/glm-5.1",
        "model_payload": {
            "providerID": "zai-coding-plan",
            "modelID": "glm-5.1",
        },
        "reasoning": "high",
        "approval_policy": "never",
        "approval_mode": "strict",
        "sandbox_policy": {
            "type": "workspaceWrite",
            "writableRoots": ["/repo"],
            "networkAccess": False,
        },
        "client_request_id": "client-1",
        "idempotency_key": "idem-1",
        "correlation_id": "corr-1",
        "origin": _origin(),
        "metadata": {"rule": {"name": "nightly"}},
        "delivery_intents": (
            DeliveryIntentRef(
                kind="discord_reply",
                intent_id="delivery-1",
                metadata={"surface_key": "channel-1"},
            ),
        ),
    }
    values.update(overrides)
    return TurnExecutionRequest(**values)  # type: ignore[arg-type]


def test_turn_execution_request_round_trips_without_dropping_runtime_fields() -> None:
    request = _request()

    payload = request.to_dict()
    restored = TurnExecutionRequest.from_mapping(payload)
    restored_json = TurnExecutionRequest.from_json(request.to_json())

    assert payload["contract_version"] == TURN_EXECUTION_CONTRACT_VERSION
    assert restored.to_dict() == payload
    assert restored_json.to_dict() == payload
    assert restored.model == "zai-coding-plan/glm-5.1"
    assert restored.reasoning == "high"
    assert restored.approval_policy == "never"
    assert restored.approval_mode == "strict"
    assert restored.sandbox_policy == {
        "type": "workspaceWrite",
        "writableRoots": ["/repo"],
        "networkAccess": False,
    }
    assert restored.client_request_id == "client-1"
    assert restored.request_kind == "message"
    assert restored.input_items == ({"type": "text", "text": "hello"},)
    assert restored.context_profile == "car_core"
    assert request.to_json() == restored.to_json()


def test_turn_execution_record_round_trips_with_terminal_references() -> None:
    assistant_output = TurnAssistantOutput(
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="conversation-1",
        backend_turn_id="turn-1",
        text="done",
        ownership="trimmed_from_cumulative",
        source="reducer",
        provenance={
            "reducer_scope": "cumulative_transcript_trimmed",
            "matched_prior_text": "previous sensitive output",
        },
    )
    record = TurnExecutionRecord(
        request=_request(),
        execution_id="exec-1",
        status="completed",
        queued_at="2026-05-21T01:00:00Z",
        claimed_at="2026-05-21T01:00:01Z",
        started_at="2026-05-21T01:00:02Z",
        terminal_at="2026-05-21T01:00:03Z",
        backend_conversation_id="conversation-1",
        backend_turn_id="turn-1",
        assistant_text="done",
        assistant_output=assistant_output,
        transcript_ref="transcript://exec-1",
        timeline_ref="timeline://exec-1",
        cold_trace_ref="cold-trace://exec-1",
        conflict_evidence={"duplicate_terminal": False},
    )

    payload = record.to_dict()
    restored = TurnExecutionRecord.from_mapping(payload)

    assert restored.to_dict() == payload
    assert TurnExecutionRecord.from_json(record.to_json()).to_dict() == payload
    assert restored.request_id == "req-1"
    assert restored.backend_conversation_id == "conversation-1"
    assert restored.backend_turn_id == "turn-1"
    assert restored.assistant_text == "done"
    assert restored.assistant_output is not None
    assert restored.assistant_output.text == "done"
    assert restored.assistant_output.ownership == "trimmed_from_cumulative"
    assert restored.assistant_output.provenance == {
        "reducer_scope": "cumulative_transcript_trimmed",
        "matched_prior_chars": len("previous sensitive output"),
    }
    assert "previous sensitive output" not in record.to_json()
    assert restored.conflict_evidence == {"duplicate_terminal": False}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target_id": ""}, "target_id is required"),
        ({"prompt_text": "", "input_items": ()}, "prompt_text or input_items"),
        ({"request_kind": "unknown"}, "request_kind must be one of"),
        ({"busy_policy": "wait"}, "busy_policy must be one of"),
        (
            {
                "target_kind": "flow",
                "origin": TurnExecutionOrigin(
                    kind="surface",
                    source_id="web:1",
                    surface_kind="pma",
                    surface_key="thread-1",
                ),
            },
            "surface origin cannot target a flow",
        ),
    ],
)
def test_turn_execution_request_rejects_invalid_required_fields(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(TurnExecutionContractError, match=message):
        _request(**overrides)


def test_turn_execution_request_preserves_literal_prompt_whitespace() -> None:
    request = _request(prompt_text="  keep literal text\n")

    assert request.prompt_text == "  keep literal text\n"
    assert TurnExecutionRequest.from_mapping(request.to_dict()).prompt_text == (
        "  keep literal text\n"
    )


def test_surface_origin_requires_surface_identity() -> None:
    with pytest.raises(TurnExecutionContractError, match="surface_kind"):
        TurnExecutionOrigin(kind="surface", source_id="message-1")


def test_opencode_request_requires_resolved_provider_model_payload() -> None:
    with pytest.raises(TurnExecutionContractError, match="provider/model"):
        _request(model=None, model_payload={})

    with pytest.raises(TurnExecutionContractError, match="provider/model"):
        _request(model="glm-5.1", model_payload={})

    with pytest.raises(TurnExecutionContractError, match="providerID"):
        _request(model_payload={})

    with pytest.raises(TurnExecutionContractError, match="must match"):
        _request(
            model_payload={
                "providerID": "zai-coding-plan",
                "modelID": "glm-4.6",
            }
        )


def test_opencode_request_accepts_explicit_resolved_provider_model_payload() -> None:
    request = _request()

    assert request.model_payload == {
        "providerID": "zai-coding-plan",
        "modelID": "glm-5.1",
    }


def test_contract_rejects_non_json_safe_metadata() -> None:
    with pytest.raises(TurnExecutionContractError, match="JSON-safe"):
        _request(metadata={"bad": object()})


def test_live_execution_create_request_rejects_legacy_partial_shape() -> None:
    with pytest.raises(ValueError, match="turn_request is required"):
        ExecutionCreateRequest.from_mapping(
            {
                "thread_target_id": "thread-1",
                "prompt": "hello",
                "request_kind": "message",
            }
        )


def test_queued_execution_payload_rejects_legacy_request_shape() -> None:
    legacy_payload = {
        "request": {
            "target_id": "thread-1",
            "message_text": "hello",
        },
        "client_request_id": "client-1",
    }

    with pytest.raises(ValueError, match="canonical turn_request"):
        QueuedExecutionRequest.from_payload(
            legacy_payload,
            thread_target_id="thread-1",
        )


def test_queued_execution_payload_uses_canonical_turn_request_fields() -> None:
    request = _request(target_id="thread-1")

    queued = QueuedExecutionRequest.from_payload(
        {
            "turn_request": request.to_dict(),
            "client_request_id": "client-1",
        },
        thread_target_id="thread-1",
    )

    assert queued.request.model == "zai-coding-plan/glm-5.1"
    assert queued.request.reasoning == "high"
    assert queued.request.approval_mode == "strict"
    assert queued.sandbox_policy == {
        "type": "workspaceWrite",
        "writableRoots": ["/repo"],
        "networkAccess": False,
    }
