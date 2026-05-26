from __future__ import annotations

import pytest

from codex_autorunner.core.automation.models import AutomationRuntimeContract
from codex_autorunner.core.orchestration.turn_execution_contract import (
    TURN_EXECUTION_CONTRACT_VERSION,
)
from codex_autorunner.core.runtime_identity import (
    RUNTIME_IDENTITY_CONTRACT_VERSION,
    RUNTIME_STAGE_EFFECTIVE,
    RUNTIME_STAGE_PROJECTED,
    RUNTIME_STAGE_REQUESTED,
    RUNTIME_STAGE_RESOLVED,
    RuntimeIdentityContractError,
    RuntimeIdentityEnvelope,
    RuntimeIdentityStage,
)


def test_runtime_identity_envelope_round_trips_all_stages() -> None:
    envelope = RuntimeIdentityEnvelope(
        requested=RuntimeIdentityStage(
            stage=RUNTIME_STAGE_REQUESTED,
            logical_agent="opencode",
            canonical_model_label="zai-coding-plan/glm-5.1",
            profile="security",
            reasoning="high",
            approval_policy="never",
            sandbox_policy="danger-full-access",
            workspace_scope={"repo_id": "repo-1"},
            prompt_ref={"kind": "inline", "sha256": "prompt-sha"},
            input_ref={"kind": "automation_event", "event_id": "event-1"},
            source="automation_rule",
            provenance={"rule_id": "rule-1"},
        ),
        resolved=RuntimeIdentityStage(
            stage=RUNTIME_STAGE_RESOLVED,
            logical_agent="opencode",
            runtime_agent="opencode",
            provider_id="zai-coding-plan",
            canonical_model_label="zai-coding-plan/glm-5.1",
            provider_model_id="glm-5.1",
            source="runtime_resolver",
        ),
        effective=RuntimeIdentityStage(
            stage=RUNTIME_STAGE_EFFECTIVE,
            runtime_agent="opencode",
            backend_runtime_id="session-1",
            provider_payload={
                "providerID": "zai-coding-plan",
                "modelID": "glm-5.1",
            },
            observed_at="2026-05-26T01:02:03+00:00",
            metadata={"session_path": "session.json"},
        ),
        projected=RuntimeIdentityStage(
            stage=RUNTIME_STAGE_PROJECTED,
            canonical_model_label="zai-coding-plan/glm-5.1",
            source="chat_read_model",
        ),
        metadata={"owner": "core"},
    )

    payload = envelope.to_dict()
    restored = RuntimeIdentityEnvelope.from_dict(payload)
    restored_json = RuntimeIdentityEnvelope.from_json(envelope.to_json())

    assert payload["contract_version"] == RUNTIME_IDENTITY_CONTRACT_VERSION
    assert restored.to_dict() == payload
    assert restored_json.to_dict() == payload
    assert restored.effective is not None
    assert restored.effective.provider_id == "zai-coding-plan"
    assert restored.effective.provider_model_id == "glm-5.1"
    assert restored.effective.observed_at == "2026-05-26T01:02:03Z"


def test_runtime_identity_preserves_unknown_without_fallback_model() -> None:
    stage = RuntimeIdentityStage.from_dict(
        {
            "stage": RUNTIME_STAGE_PROJECTED,
            "logical_agent": "codex",
            "canonical_model_label": "",
            "provider_payload": {},
        }
    )

    assert stage.logical_agent == "codex"
    assert stage.canonical_model_label is None
    assert stage.provider_id is None
    assert stage.provider_model_id is None
    assert stage.to_dict()["canonical_model_label"] is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"contract_version": 999}, "unsupported runtime identity"),
        (
            {
                "contract_version": RUNTIME_IDENTITY_CONTRACT_VERSION,
                "requested": {"stage": "actual"},
            },
            "stage",
        ),
        (
            {
                "contract_version": RUNTIME_IDENTITY_CONTRACT_VERSION,
                "requested": {"stage": RUNTIME_STAGE_REQUESTED, "metadata": object()},
            },
            "JSON-safe",
        ),
    ],
)
def test_runtime_identity_rejects_invalid_payloads(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RuntimeIdentityContractError, match=message):
        RuntimeIdentityEnvelope.from_mapping(payload)


def test_automation_runtime_contract_wraps_runtime_identity_stage() -> None:
    runtime = AutomationRuntimeContract.from_dict(
        {
            "agent": "opencode",
            "model": "zai-coding-plan/glm-5.1",
            "profile": "security",
            "reasoning": "high",
            "approval_policy": "never",
            "sandbox_policy": "danger-full-access",
            "workspace_scope": {"repo_id": "repo-1"},
            "provider_payload": {
                "providerID": "zai-coding-plan",
                "modelID": "glm-5.1",
            },
        }
    )

    stage = runtime.to_runtime_stage()
    envelope = runtime.to_runtime_envelope()
    restored = AutomationRuntimeContract.from_runtime_stage(stage)

    assert runtime.to_dict() == restored.to_dict()
    assert stage.stage == RUNTIME_STAGE_REQUESTED
    assert stage.logical_agent == "opencode"
    assert stage.canonical_model_label == "zai-coding-plan/glm-5.1"
    assert stage.provider_id == "zai-coding-plan"
    assert stage.provider_model_id == "glm-5.1"
    assert envelope.requested == stage


def test_runtime_identity_stage_from_turn_execution_request_shape() -> None:
    turn_request = {
        "contract_version": TURN_EXECUTION_CONTRACT_VERSION,
        "request_id": "req-1",
        "target_id": "thread-1",
        "target_kind": "thread",
        "workspace_root": "/repo",
        "agent": "opencode",
        "profile": "code",
        "model": "zai-coding-plan/glm-5.1",
        "model_payload": {
            "providerID": "zai-coding-plan",
            "modelID": "glm-5.1",
        },
        "reasoning": "high",
        "approval_policy": "never",
        "sandbox_policy": {"type": "workspaceWrite"},
        "input_items": [{"type": "text", "text": "hello"}],
    }

    stage = RuntimeIdentityStage.from_turn_execution_request(turn_request)

    assert stage.stage == RUNTIME_STAGE_RESOLVED
    assert stage.runtime_agent == "opencode"
    assert stage.canonical_model_label == "zai-coding-plan/glm-5.1"
    assert stage.provider_id == "zai-coding-plan"
    assert stage.provider_model_id == "glm-5.1"
    assert stage.workspace_scope == {"workspace_root": "/repo"}
    assert stage.prompt_ref == {
        "kind": "turn_execution_request",
        "request_id": "req-1",
    }
    assert stage.input_ref == {"kind": "turn_execution_input_items"}
    assert stage.source == "turn_execution_request"
