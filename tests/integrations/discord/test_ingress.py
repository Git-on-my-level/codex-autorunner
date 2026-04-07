from __future__ import annotations

import logging
from typing import Any, Optional

import pytest

from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationEvaluationResult,
)
from codex_autorunner.integrations.discord.ingress import (
    InteractionIngress,
    InteractionKind,
)


def _allowed_result() -> CollaborationEvaluationResult:
    return CollaborationEvaluationResult(
        outcome="active_destination",
        allowed=True,
        command_allowed=True,
        should_start_turn=True,
        actor_allowed=True,
        container_allowed=True,
        destination_allowed=True,
        destination_mode="active",
        plain_text_trigger="always",
        reason="allowed",
    )


def _denied_result() -> CollaborationEvaluationResult:
    return CollaborationEvaluationResult(
        outcome="denied_destination",
        allowed=False,
        command_allowed=False,
        should_start_turn=False,
        actor_allowed=True,
        container_allowed=True,
        destination_allowed=False,
        destination_mode="denied",
        plain_text_trigger="disabled",
        reason="denied",
    )


class _FakeService:
    def __init__(
        self,
        *,
        command_allowed: bool = True,
        prepared_policy: Optional[str] = None,
        ack_succeeds: bool = True,
    ) -> None:
        self._command_allowed = command_allowed
        self._prepared_policy = prepared_policy
        self._ack_succeeds = ack_succeeds
        self._logger = logging.getLogger("test.ingress")
        self.respond_ephemeral_calls: list[dict[str, Any]] = []
        self.respond_autocomplete_calls: list[dict[str, Any]] = []
        self.prepare_command_calls: list[dict[str, Any]] = []
        self.log_collaboration_calls: list[dict[str, Any]] = []
        self.normalize_path_calls: list[tuple[str, ...]] = []

    def _evaluate_interaction_collaboration_policy(
        self,
        *,
        channel_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
    ) -> CollaborationEvaluationResult:
        if self._command_allowed:
            return _allowed_result()
        return _denied_result()

    def _log_collaboration_policy_result(self, **kwargs: Any) -> None:
        self.log_collaboration_calls.append(kwargs)

    async def _respond_ephemeral(
        self, interaction_id: str, interaction_token: str, text: str
    ) -> None:
        self.respond_ephemeral_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "text": text,
            }
        )

    async def _respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        self.respond_autocomplete_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "choices": choices,
            }
        )

    def _prepared_interaction_policy(self, token: str) -> Optional[str]:
        return self._prepared_policy

    async def _prepare_command_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        command_path: tuple[str, ...],
        timing: str,
    ) -> bool:
        self.prepare_command_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "command_path": command_path,
                "timing": timing,
            }
        )
        return self._ack_succeeds

    @staticmethod
    def _normalize_discord_command_path(
        command_path: tuple[str, ...],
    ) -> tuple[str, ...]:
        if command_path[:1] == ("flow",):
            return ("car", "flow", *command_path[1:])
        return command_path


def _slash_command_payload(
    *,
    interaction_id: str = "inter-1",
    command_name: str = "car",
    subcommand_name: str = "status",
    options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    sub_options = [
        {"type": 3, "name": k, "value": v} for k, v in (options or {}).items()
    ]
    return {
        "id": interaction_id,
        "token": "token-1",
        "channel_id": "chan-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": command_name,
            "options": [
                {
                    "type": 1,
                    "name": subcommand_name,
                    "options": sub_options,
                }
            ],
        },
    }


def _component_payload(
    *,
    custom_id: str = "bind_select",
    values: Optional[list[str]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "inter-2",
        "token": "token-2",
        "channel_id": "chan-2",
        "guild_id": "guild-2",
        "type": 3,
        "member": {"user": {"id": "user-2"}},
        "data": {
            "custom_id": custom_id,
        },
        "message": {"id": "msg-1"},
    }
    if values is not None:
        payload["data"]["values"] = values
    return payload


def _modal_payload(
    *,
    custom_id: str = "tickets_modal:abc",
) -> dict[str, Any]:
    return {
        "id": "inter-3",
        "token": "token-3",
        "channel_id": "chan-3",
        "guild_id": "guild-3",
        "type": 5,
        "member": {"user": {"id": "user-3"}},
        "data": {
            "custom_id": custom_id,
            "components": [
                {
                    "type": 18,
                    "label": "Ticket",
                    "component": {
                        "type": 4,
                        "custom_id": "ticket_body",
                        "value": "body text",
                    },
                }
            ],
        },
    }


def _autocomplete_payload(
    *,
    command_name: str = "car",
    subcommand_name: str = "bind",
    focused_name: str = "workspace",
    focused_value: str = "codex",
) -> dict[str, Any]:
    return {
        "id": "inter-4",
        "token": "token-4",
        "channel_id": "chan-4",
        "guild_id": "guild-4",
        "type": 4,
        "member": {"user": {"id": "user-4"}},
        "data": {
            "name": command_name,
            "options": [
                {
                    "type": 1,
                    "name": subcommand_name,
                    "options": [
                        {
                            "type": 3,
                            "name": focused_name,
                            "value": focused_value,
                            "focused": True,
                        }
                    ],
                }
            ],
        },
    }


@pytest.mark.anyio
async def test_normalize_slash_command() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    ctx = result.context
    assert ctx.kind == InteractionKind.SLASH_COMMAND
    assert ctx.interaction_id == "inter-1"
    assert ctx.interaction_token == "token-1"
    assert ctx.channel_id == "chan-1"
    assert ctx.guild_id == "guild-1"
    assert ctx.user_id == "user-1"
    assert ctx.command_spec is not None
    assert ctx.command_spec.path == ("car", "status")


@pytest.mark.anyio
async def test_normalize_component() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _component_payload(custom_id="flow:run-1:resume", values=["val-1"])
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    ctx = result.context
    assert ctx.kind == InteractionKind.COMPONENT
    assert ctx.interaction_id == "inter-2"
    assert ctx.custom_id == "flow:run-1:resume"
    assert ctx.values == ["val-1"]
    assert ctx.message_id == "msg-1"


@pytest.mark.anyio
async def test_normalize_modal_submit() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _modal_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    ctx = result.context
    assert ctx.kind == InteractionKind.MODAL_SUBMIT
    assert ctx.custom_id == "tickets_modal:abc"
    assert ctx.modal_values is not None
    assert ctx.modal_values.get("ticket_body") == "body text"


@pytest.mark.anyio
async def test_normalize_autocomplete() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _autocomplete_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    ctx = result.context
    assert ctx.kind == InteractionKind.AUTOCOMPLETE
    assert ctx.focused_name == "workspace"
    assert ctx.focused_value == "codex"
    assert ctx.command_spec is not None
    assert ctx.command_spec.path == ("car", "bind")


@pytest.mark.anyio
async def test_normalization_returns_none_for_missing_ids() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)

    result = await ingress.process_raw_payload({})
    assert result.accepted is False
    assert result.rejection_reason == "normalization_failed"

    result = await ingress.process_raw_payload({"id": "inter-1"})
    assert result.accepted is False
    assert result.rejection_reason == "normalization_failed"

    result = await ingress.process_raw_payload({"id": "inter-1", "token": "token-1"})
    assert result.accepted is False
    assert result.rejection_reason == "normalization_failed"


@pytest.mark.anyio
async def test_authz_rejection_sends_ephemeral() -> None:
    service = _FakeService(command_allowed=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is False
    assert result.rejection_reason == "unauthorized"
    assert result.context is not None
    assert len(service.respond_ephemeral_calls) == 1
    call = service.respond_ephemeral_calls[0]
    assert call["interaction_id"] == "inter-1"
    assert "not authorized" in call["text"]


@pytest.mark.anyio
async def test_authz_rejection_autocomplete_sends_empty_choices() -> None:
    service = _FakeService(command_allowed=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _autocomplete_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is False
    assert result.rejection_reason == "unauthorized"
    assert len(service.respond_autocomplete_calls) == 1
    assert service.respond_autocomplete_calls[0]["choices"] == []
    assert len(service.respond_ephemeral_calls) == 0


@pytest.mark.anyio
async def test_authz_rejection_component_sends_ephemeral() -> None:
    service = _FakeService(command_allowed=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _component_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is False
    assert result.rejection_reason == "unauthorized"
    assert len(service.respond_ephemeral_calls) == 1


@pytest.mark.anyio
async def test_command_spec_resolved_for_slash_command() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="status")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    spec = result.context.command_spec
    assert spec is not None
    assert spec.path == ("car", "status")
    assert spec.ack_policy == "defer_ephemeral"
    assert spec.requires_workspace is False


@pytest.mark.anyio
async def test_command_spec_resolved_for_car_bind() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="bind")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    spec = result.context.command_spec
    assert spec is not None
    assert spec.path == ("car", "bind")
    assert spec.ack_policy == "defer_ephemeral"


@pytest.mark.anyio
async def test_command_spec_resolved_for_car_new() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="new")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    spec = result.context.command_spec
    assert spec is not None
    assert spec.path == ("car", "new")
    assert spec.ack_policy == "defer_public"
    assert spec.requires_workspace is True


@pytest.mark.anyio
async def test_command_spec_none_for_unknown_command() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="nonexistent")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    spec = result.context.command_spec
    assert spec is not None
    assert spec.ack_policy is None


@pytest.mark.anyio
async def test_ack_performed_for_deferred_command() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="status")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is True
    assert len(service.prepare_command_calls) == 1
    call = service.prepare_command_calls[0]
    assert call["command_path"] == ("car", "status")
    assert call["timing"] == "dispatch"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command_name", "subcommand_name", "expected_path"),
    [
        ("flow", "status", ("car", "flow", "status")),
        ("flow", "start", ("car", "flow", "start")),
    ],
)
async def test_flow_commands_ack_on_dispatch(
    command_name: str,
    subcommand_name: str,
    expected_path: tuple[str, ...],
) -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(
        command_name=command_name,
        subcommand_name=subcommand_name,
    )
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is True
    assert len(service.prepare_command_calls) == 1
    call = service.prepare_command_calls[0]
    assert call["command_path"] == expected_path
    assert call["timing"] == "dispatch"


@pytest.mark.anyio
async def test_ack_skipped_for_immediate_command() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="agent")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is False
    assert len(service.prepare_command_calls) == 0


@pytest.mark.anyio
async def test_ack_skipped_when_already_prepared() -> None:
    service = _FakeService(prepared_policy="defer_ephemeral")
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="status")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is True
    assert len(service.prepare_command_calls) == 0


@pytest.mark.anyio
async def test_ack_failure_sends_retry_message() -> None:
    service = _FakeService(ack_succeeds=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="car", subcommand_name="status")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is False
    assert result.rejection_reason == "ack_failed"
    assert len(service.respond_ephemeral_calls) == 1
    assert "retry" in service.respond_ephemeral_calls[0]["text"].lower()


@pytest.mark.anyio
async def test_no_ack_for_component() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _component_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is False
    assert len(service.prepare_command_calls) == 0


@pytest.mark.anyio
async def test_no_ack_for_modal() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _modal_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is False
    assert len(service.prepare_command_calls) == 0


@pytest.mark.anyio
async def test_no_ack_for_autocomplete() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _autocomplete_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.deferred is False
    assert len(service.prepare_command_calls) == 0


@pytest.mark.anyio
async def test_timing_recorded_on_success() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    t = result.context.timing
    assert t.ingress_started_at is not None
    assert t.authz_finished_at is not None
    assert t.ack_finished_at is not None
    assert t.ingress_finished_at is not None
    assert t.ingress_started_at <= t.authz_finished_at
    assert t.authz_finished_at <= t.ack_finished_at
    assert t.ack_finished_at <= t.ingress_finished_at


@pytest.mark.anyio
async def test_timing_records_created_timestamp_from_snowflake() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    created_at_ms = 1_700_000_000_000
    snowflake = str((created_at_ms - 1420070400000) << 22)
    payload = _slash_command_payload(interaction_id=snowflake)

    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.timing.interaction_created_at == pytest.approx(
        created_at_ms / 1000.0,
        abs=0.001,
    )


@pytest.mark.anyio
async def test_timing_recorded_on_authz_rejection() -> None:
    service = _FakeService(command_allowed=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is False
    assert result.context is not None
    t = result.context.timing
    assert t.ingress_started_at is not None
    assert t.authz_finished_at is not None
    assert t.ingress_finished_at is None


@pytest.mark.anyio
async def test_flow_command_normalized_to_car_flow() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload(command_name="flow", subcommand_name="status")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    spec = result.context.command_spec
    assert spec is not None
    assert spec.path[:2] == ("car", "flow")


@pytest.mark.anyio
async def test_collaboration_policy_logged_on_rejection() -> None:
    service = _FakeService(command_allowed=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_command_payload()
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is False
    assert len(service.log_collaboration_calls) == 1
    call = service.log_collaboration_calls[0]
    assert call["interaction_id"] == "inter-1"
    assert call["channel_id"] == "chan-1"


@pytest.mark.anyio
async def test_component_without_custom_id_passes_normalization() -> None:
    service = _FakeService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _component_payload(custom_id="   ")
    result = await ingress.process_raw_payload(payload)

    assert result.accepted is True
    assert result.context is not None
    assert result.context.custom_id is None
