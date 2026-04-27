"""Post-ingress scheduling pipeline for Discord interactions.

This module owns the scheduling path that runs after the ingress normalizer
has accepted an interaction.  ``schedule_ingressed_interaction`` encapsulates:

1. Build the runtime envelope (resource keys, ack policies)
2. Dispatch acknowledgement (defer)
3. Duplicate detection after ack
4. Persistence to the interaction ledger
5. Ingress timing finalization
6. Submission to the CommandRunner

The service object is passed as ``Any`` because the protocol surface is large
and still evolving; the required methods are documented on the function.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from ...core.logging_utils import log_event
from .ingress import IngressContext


@dataclass
class ScheduleResult:
    submitted: bool


async def schedule_ingressed_interaction(
    service: Any,
    ctx: IngressContext,
    payload: dict[str, Any],
    *,
    submission_order: Optional[int] = None,
    dispatch_started_at: float,
) -> ScheduleResult:
    """Schedule an ingressed interaction for background execution.

    This function encapsulates the post-normalization scheduling pipeline:

    1. Build the runtime envelope (resource keys, ack policies)
    2. Dispatch acknowledgement (defer)
    3. Duplicate detection after ack
    4. Persistence to the interaction ledger
    5. Ingress timing finalization
    6. Submission to the CommandRunner

    Required service methods:
    - ``_build_runtime_interaction_envelope(ctx)`` → RuntimeInteractionEnvelope
    - ``_interaction_telemetry_fields(ctx, *, now, envelope)`` → dict
    - ``_register_chat_operation_received(ctx, *, conversation_id)`` → None
    - ``_acknowledge_runtime_envelope(envelope, *, stage)`` → bool
    - ``_dispatch_ack_failure_confirms_expiry(ctx, envelope)`` → bool
    - ``_respond_ephemeral(interaction_id, interaction_token, text)`` → None
    - ``_register_interaction_ingress(ctx)`` → bool
    - ``_persist_runtime_interaction(envelope, payload, *, scheduler_state)`` → None
    - ``_ingress.finalize_success(ctx)`` → None
    - ``_command_runner.submit(...)`` → None
    - ``_initial_ack_budget_seconds()`` → float
    - ``_logger`` → logging.Logger
    """
    envelope = await service._build_runtime_interaction_envelope(ctx)
    log_event(
        service._logger,
        logging.INFO,
        "discord.interaction.admitted",
        **service._interaction_telemetry_fields(
            ctx, now=dispatch_started_at, envelope=envelope
        ),
    )
    registration = await service._register_chat_operation_received(
        ctx, conversation_id=envelope.conversation_id
    )
    if registration is not None and not registration.inserted:
        duplicate_suppressed = await service._suppress_registered_duplicate_interaction(
            interaction_id=ctx.interaction_id,
            snapshot=registration.snapshot,
        )
        if duplicate_suppressed:
            log_event(
                service._logger,
                logging.INFO,
                "discord.interaction.rejected",
                rejection_reason="duplicate_interaction",
                **service._interaction_telemetry_fields(
                    ctx, now=dispatch_started_at, envelope=envelope
                ),
            )
            ctx.timing = replace(
                ctx.timing,
                ingress_finished_at=time.monotonic(),
            )
            return ScheduleResult(submitted=False)
    acked = await service._acknowledge_runtime_envelope(envelope, stage="dispatch")
    if not acked and service._dispatch_ack_failure_confirms_expiry(ctx, envelope):
        log_event(
            service._logger,
            logging.WARNING,
            "discord.interaction.delivery_expired_before_dispatch",
            expired_before_ack=True,
            ack_budget_seconds=service._initial_ack_budget_seconds(),
            **service._interaction_telemetry_fields(ctx, envelope=envelope),
        )
        ctx.timing = replace(
            ctx.timing,
            ack_finished_at=time.monotonic(),
            ingress_finished_at=time.monotonic(),
        )
        return ScheduleResult(submitted=False)
    if not acked and envelope.dispatch_ack_policy not in (None, "immediate"):
        await service._respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Discord interaction did not acknowledge. Please retry.",
        )
        ctx.timing = replace(
            ctx.timing,
            ack_finished_at=time.monotonic(),
            ingress_finished_at=time.monotonic(),
        )
        return ScheduleResult(submitted=False)

    duplicate_after_ack = await service._register_interaction_ingress(ctx)
    if duplicate_after_ack:
        return ScheduleResult(submitted=False)
    await service._persist_runtime_interaction(
        envelope, payload, scheduler_state="acknowledged"
    )
    service._ingress.finalize_success(ctx)

    ingress_elapsed_ms = None
    if (
        ctx.timing.ingress_started_at is not None
        and ctx.timing.ingress_finished_at is not None
    ):
        ingress_elapsed_ms = round(
            (ctx.timing.ingress_finished_at - ctx.timing.ingress_started_at) * 1000,
            1,
        )
    log_event(
        service._logger,
        logging.INFO,
        "discord.interaction.enqueued",
        ingress_elapsed_ms=ingress_elapsed_ms,
        **service._interaction_telemetry_fields(ctx, envelope=envelope),
    )
    service._command_runner.submit(
        envelope.context,
        payload,
        resource_keys=envelope.resource_keys,
        conversation_id=envelope.conversation_id,
        queue_wait_ack_policy=envelope.queue_wait_ack_policy,
        submission_order=submission_order,
    )
    await asyncio.sleep(0)
    return ScheduleResult(submitted=True)
