from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...core.logging_utils import log_event
from ...integrations.chat.managed_thread_turns import (
    ManagedThreadFinalizationResult,
    handoff_managed_thread_final_delivery,
)
from .managed_thread_delivery import build_discord_managed_thread_durable_delivery_hooks
from .message_turns import build_discord_thread_orchestration_service


async def recover_managed_thread_executions_on_startup(service: Any) -> None:
    orchestration_service = build_discord_thread_orchestration_service(service)
    thread_store = getattr(orchestration_service, "thread_store", None)
    list_running = getattr(
        thread_store, "list_thread_ids_with_running_executions", None
    )
    if not callable(list_running):
        return

    recovered = 0
    failed = 0
    for managed_thread_id in list_running(limit=None):
        try:
            thread = orchestration_service.get_thread_target(managed_thread_id)
            execution = orchestration_service.get_running_execution(managed_thread_id)
            if thread is None or execution is None:
                continue
            bindings = orchestration_service.list_bindings(
                thread_target_id=managed_thread_id,
                surface_kind="discord",
                include_disabled=False,
                limit=5,
            )
            if not bindings:
                continue
            channel_id = next(
                (
                    surface_key
                    for binding in bindings
                    if isinstance(
                        surface_key := getattr(binding, "surface_key", None), str
                    )
                    and surface_key.strip()
                ),
                None,
            )
            if channel_id is None or not thread.workspace_root:
                continue

            recovered_execution = (
                await orchestration_service.recover_running_execution_from_harness(
                    managed_thread_id,
                    default_error="Discord PMA turn failed",
                )
            )
            if recovered_execution is None:
                recovered_execution = (
                    orchestration_service.recover_running_execution_after_restart(
                        managed_thread_id
                    )
                )
            if recovered_execution is None:
                continue

            finalized = ManagedThreadFinalizationResult(
                status=recovered_execution.status,
                assistant_text=recovered_execution.output_text or "",
                error=recovered_execution.error,
                managed_thread_id=managed_thread_id,
                managed_turn_id=recovered_execution.execution_id,
                backend_thread_id=thread.backend_thread_id,
                token_usage=None,
            )
            delivery = build_discord_managed_thread_durable_delivery_hooks(
                service,
                channel_id=channel_id,
                managed_thread_id=managed_thread_id,
                workspace_root=Path(thread.workspace_root),
                public_execution_error="Discord PMA turn failed",
            )
            await handoff_managed_thread_final_delivery(
                finalized,
                delivery=delivery,
                logger=service._logger,
            )
            recovered += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            log_event(
                service._logger,
                logging.WARNING,
                "discord.turn.startup_execution_recovery_failed",
                managed_thread_id=managed_thread_id,
                exc=exc,
            )
    if recovered or failed:
        log_event(
            service._logger,
            logging.INFO,
            "discord.turn.startup_execution_recovery_finished",
            recovered=recovered,
            failed=failed,
        )
