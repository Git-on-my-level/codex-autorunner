"""Discord interaction ingress runtime.

Single entry point for all Discord interaction types (slash commands, component
interactions, modal submits, autocomplete requests).  Ingress owns:

- interaction parsing and normalization
- collaboration/authz checks
- ack policy lookup and initial response/defer emission
- timing telemetry for ingress and acknowledgement

Ingress must not run command business logic.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ...core.logging_utils import log_event
from .interaction_registry import (
    DiscordAckPolicy,
    DiscordAckTiming,
    normalize_discord_command_path,
    slash_command_ack_metadata_for_path,
)
from .interactions import (
    extract_autocomplete_command_context,
    extract_channel_id,
    extract_command_path_and_options,
    extract_component_custom_id,
    extract_component_values,
    extract_guild_id,
    extract_interaction_created_at,
    extract_interaction_id,
    extract_interaction_token,
    extract_modal_custom_id,
    extract_modal_values,
    extract_user_id,
    is_autocomplete_interaction,
    is_component_interaction,
    is_modal_submit_interaction,
)


class InteractionKind(enum.Enum):
    SLASH_COMMAND = "slash_command"
    COMPONENT = "component"
    MODAL_SUBMIT = "modal_submit"
    AUTOCOMPLETE = "autocomplete"


@dataclass(frozen=True)
class IngressTiming:
    interaction_created_at: Optional[float] = None
    ingress_started_at: Optional[float] = None
    authz_finished_at: Optional[float] = None
    ack_finished_at: Optional[float] = None
    ingress_finished_at: Optional[float] = None
    execution_started_at: Optional[float] = None
    execution_finished_at: Optional[float] = None


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    options: dict[str, Any]
    ack_policy: Optional[DiscordAckPolicy]
    ack_timing: DiscordAckTiming
    requires_workspace: bool


@dataclass
class IngressContext:
    interaction_id: str
    interaction_token: str
    channel_id: str
    guild_id: Optional[str]
    user_id: Optional[str]
    kind: InteractionKind
    deferred: bool = False
    command_spec: Optional[CommandSpec] = None
    custom_id: Optional[str] = None
    values: Optional[list[str]] = None
    modal_values: Optional[dict[str, Any]] = None
    focused_name: Optional[str] = None
    focused_value: Optional[str] = None
    message_id: Optional[str] = None
    timing: IngressTiming = field(default_factory=IngressTiming)


@dataclass
class IngressResult:
    accepted: bool
    context: Optional[IngressContext] = None
    rejection_reason: Optional[str] = None


class InteractionIngress:
    def __init__(self, service: Any, *, logger: logging.Logger) -> None:
        self._service = service
        self._logger = logger

    async def process_raw_payload(
        self,
        payload: dict[str, Any],
    ) -> IngressResult:
        now = time.monotonic()
        ctx = self._normalize(payload)
        if ctx is None:
            return IngressResult(
                accepted=False,
                rejection_reason="normalization_failed",
            )
        self._service._ensure_interaction_session(
            ctx.interaction_id,
            ctx.interaction_token,
            kind=ctx.kind,
        )
        register_interaction = getattr(
            self._service, "_register_interaction_ingress", None
        )
        if callable(register_interaction):
            is_duplicate = await register_interaction(ctx)
            if is_duplicate:
                self._logger.debug(
                    "Skipping duplicate Discord interaction delivery: %s",
                    ctx.interaction_id,
                )
                return IngressResult(
                    accepted=False,
                    context=ctx,
                    rejection_reason="duplicate_interaction",
                )
        ctx.timing = IngressTiming(
            interaction_created_at=ctx.timing.interaction_created_at,
            ingress_started_at=now,
        )

        authz_ok = self._check_authorization(ctx)
        ctx.timing = IngressTiming(
            interaction_created_at=ctx.timing.interaction_created_at,
            ingress_started_at=ctx.timing.ingress_started_at,
            authz_finished_at=time.monotonic(),
        )
        if not authz_ok:
            await self._send_authz_rejection(ctx)
            return IngressResult(
                accepted=False,
                context=ctx,
                rejection_reason="unauthorized",
            )

        if ctx.kind == InteractionKind.SLASH_COMMAND:
            self._resolve_command_spec(ctx)
            ack_ok = await self._perform_ack(ctx)
            if not ack_ok and ctx.command_spec is not None:
                ack_policy = ctx.command_spec.ack_policy
                if ack_policy is not None and ack_policy not in (
                    "immediate",
                    None,
                ):
                    await self._service._respond_ephemeral(
                        ctx.interaction_id,
                        ctx.interaction_token,
                        "Discord interaction did not acknowledge. Please retry.",
                    )
                    ctx.timing = IngressTiming(
                        interaction_created_at=ctx.timing.interaction_created_at,
                        ingress_started_at=ctx.timing.ingress_started_at,
                        authz_finished_at=ctx.timing.authz_finished_at,
                        ack_finished_at=time.monotonic(),
                        ingress_finished_at=time.monotonic(),
                    )
                    return IngressResult(
                        accepted=False,
                        context=ctx,
                        rejection_reason="ack_failed",
                    )

        ctx.timing = IngressTiming(
            interaction_created_at=ctx.timing.interaction_created_at,
            ingress_started_at=ctx.timing.ingress_started_at,
            authz_finished_at=ctx.timing.authz_finished_at,
            ack_finished_at=ctx.timing.ack_finished_at or time.monotonic(),
            ingress_finished_at=time.monotonic(),
        )
        self._record_telemetry(ctx)
        return IngressResult(accepted=True, context=ctx)

    def _normalize(self, payload: dict[str, Any]) -> Optional[IngressContext]:
        interaction_id = extract_interaction_id(payload)
        interaction_token = extract_interaction_token(payload)
        channel_id = extract_channel_id(payload)

        if not interaction_id or not interaction_token or not channel_id:
            return None

        guild_id = extract_guild_id(payload)
        user_id = extract_user_id(payload)
        created_at = extract_interaction_created_at(payload)
        message_id: Optional[str] = None
        message_data = payload.get("message")
        if isinstance(message_data, dict):
            raw_mid = message_data.get("id")
            if isinstance(raw_mid, str) and raw_mid.strip():
                message_id = raw_mid.strip()

        if is_component_interaction(payload):
            custom_id = extract_component_custom_id(payload)
            values = extract_component_values(payload)
            return IngressContext(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
                kind=InteractionKind.COMPONENT,
                custom_id=custom_id,
                values=values,
                message_id=message_id,
                timing=IngressTiming(interaction_created_at=created_at),
            )

        if is_modal_submit_interaction(payload):
            custom_id = extract_modal_custom_id(payload)
            modal_values = extract_modal_values(payload)
            return IngressContext(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
                kind=InteractionKind.MODAL_SUBMIT,
                custom_id=custom_id,
                modal_values=modal_values,
                timing=IngressTiming(interaction_created_at=created_at),
            )

        if is_autocomplete_interaction(payload):
            (
                command_path,
                options,
                focused_name,
                focused_value,
            ) = extract_autocomplete_command_context(payload)
            return IngressContext(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
                kind=InteractionKind.AUTOCOMPLETE,
                command_spec=(
                    CommandSpec(
                        path=command_path,
                        options=options,
                        ack_policy=None,
                        ack_timing="dispatch",
                        requires_workspace=False,
                    )
                    if command_path
                    else None
                ),
                focused_name=focused_name,
                focused_value=focused_value,
                timing=IngressTiming(interaction_created_at=created_at),
            )

        command_path, options = extract_command_path_and_options(payload)
        return IngressContext(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            kind=InteractionKind.SLASH_COMMAND,
            command_spec=(
                CommandSpec(
                    path=command_path,
                    options=options,
                    ack_policy=None,
                    ack_timing="dispatch",
                    requires_workspace=False,
                )
                if command_path
                else None
            ),
            timing=IngressTiming(interaction_created_at=created_at),
        )

    def _check_authorization(self, ctx: IngressContext) -> bool:
        policy_result = self._service._evaluate_interaction_collaboration_policy(
            channel_id=ctx.channel_id,
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
        )
        if not policy_result.command_allowed:
            self._service._log_collaboration_policy_result(
                channel_id=ctx.channel_id,
                guild_id=ctx.guild_id,
                user_id=ctx.user_id,
                interaction_id=ctx.interaction_id,
                result=policy_result,
            )
            return False
        return True

    async def _send_authz_rejection(self, ctx: IngressContext) -> None:
        if ctx.kind == InteractionKind.AUTOCOMPLETE:
            await self._service._respond_autocomplete(
                ctx.interaction_id,
                ctx.interaction_token,
                choices=[],
            )
            return
        await self._service._respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "This Discord command is not authorized for this channel/user/guild.",
        )

    def _resolve_command_spec(self, ctx: IngressContext) -> None:
        if ctx.command_spec is None:
            return
        raw_path = ctx.command_spec.path
        normalized_path = normalize_discord_command_path(raw_path)
        ack_policy, ack_timing, requires_workspace = (
            slash_command_ack_metadata_for_path(normalized_path)
        )
        ctx.command_spec = CommandSpec(
            path=normalized_path,
            options=ctx.command_spec.options,
            ack_policy=ack_policy,
            ack_timing=ack_timing,
            requires_workspace=requires_workspace,
        )

    async def _perform_ack(self, ctx: IngressContext) -> bool:
        if ctx.command_spec is None:
            return True
        session = self._service._ensure_interaction_session(
            ctx.interaction_id,
            ctx.interaction_token,
            kind=ctx.kind,
        )
        policy = ctx.command_spec.ack_policy
        if policy is None or policy == "immediate":
            return True
        if ctx.command_spec.ack_timing != "dispatch":
            return True
        if session.has_initial_response():
            ctx.deferred = session.is_deferred()
            return True
        prepared: bool = await self._service._prepare_command_interaction(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            command_path=ctx.command_spec.path,
            timing="dispatch",
        )
        if prepared:
            ctx.deferred = True
        return prepared

    def _record_telemetry(self, ctx: IngressContext) -> None:
        t = ctx.timing
        if t.ingress_started_at is not None and t.ingress_finished_at is not None:
            elapsed_ms = (t.ingress_finished_at - t.ingress_started_at) * 1000
            ack_delta_ms: Optional[float] = None
            gateway_to_ingress_ms: Optional[float] = None
            if t.ingress_started_at is not None and t.ack_finished_at is not None:
                ack_delta_ms = (t.ack_finished_at - t.ingress_started_at) * 1000
            if t.interaction_created_at is not None:
                gateway_to_ingress_ms = max(
                    0.0,
                    (time.time() - t.interaction_created_at) * 1000,
                )
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.ingress.completed",
                interaction_id=ctx.interaction_id,
                kind=ctx.kind.value,
                ingress_elapsed_ms=round(elapsed_ms, 1),
                ack_delta_ms=(
                    round(ack_delta_ms, 1) if ack_delta_ms is not None else None
                ),
                gateway_to_ingress_ms=(
                    round(gateway_to_ingress_ms, 1)
                    if gateway_to_ingress_ms is not None
                    else None
                ),
                deferred=ctx.deferred,
                command_path=(
                    ":".join(ctx.command_spec.path) if ctx.command_spec else None
                ),
            )
