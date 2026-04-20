"""Shared Discord durable delivery adapter logic for managed-thread records."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    SQLiteManagedThreadDeliveryEngine,
)
from ...integrations.chat.managed_thread_turns import (
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadSurfaceInfo,
    build_managed_thread_delivery_intent,
    render_managed_thread_delivery_record_text,
)
from .constants import DISCORD_MAX_MESSAGE_LENGTH
from .rendering import chunk_discord_message, format_discord_message


async def deliver_discord_managed_thread_record(
    service: Any,
    record: Any,
    *,
    claim: Any,
    channel_id_fallback: Optional[str],
    base_record_label: str,
    error_record_label: str,
    default_execution_error: str,
) -> ManagedThreadDeliveryAttemptResult:
    """Deliver a managed-thread delivery record to Discord (chunks ok-path; errors → message)."""
    _ = claim
    target_channel_id = _resolve_delivery_channel_id(
        record, fallback=channel_id_fallback
    )
    workspace_root = _resolve_delivery_workspace_root(record)
    if not target_channel_id:
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.ABANDONED,
            error="missing_discord_channel_id",
        )
    if record.envelope.final_status == "ok":
        assistant_text = render_managed_thread_delivery_record_text(record)
        formatted = (
            format_discord_message(assistant_text)
            if assistant_text
            else "(No response text returned.)"
        )
        chunks = chunk_discord_message(
            formatted,
            max_len=DISCORD_MAX_MESSAGE_LENGTH,
            with_numbering=False,
        )
        if not chunks:
            chunks = [formatted]
        base_record_id = (
            f"{base_record_label}:{record.managed_thread_id}:{record.managed_turn_id}"
        )
        try:
            for chunk_index, chunk in enumerate(chunks, start=1):
                record_id = (
                    f"{base_record_id}:chunk:{chunk_index}"
                    if len(chunks) > 1
                    else base_record_id
                )
                delivered = await service._send_channel_message_safe(
                    target_channel_id,
                    {"content": chunk},
                    record_id=record_id,
                )
                if not delivered:
                    return ManagedThreadDeliveryAttemptResult(
                        outcome=ManagedThreadDeliveryOutcome.FAILED,
                        error=f"discord_send_deferred:{record_id}",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error=str(exc) or exc.__class__.__name__,
            )
        try:
            await _flush_delivery_outbox_files(
                service,
                workspace_root=workspace_root,
                channel_id=target_channel_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # intentional: do not surface cleanup failures after reply
            logger = getattr(service, "_logger", None)
            if logger is not None:
                log_event(
                    logger,
                    logging.WARNING,
                    "discord.managed_thread.delivery_cleanup_failed",
                    channel_id=target_channel_id,
                    workspace_root=(
                        str(workspace_root) if workspace_root is not None else None
                    ),
                    managed_thread_id=record.managed_thread_id,
                    managed_turn_id=record.managed_turn_id,
                    exc=exc,
                )
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            adapter_cursor={"chunk_count": len(chunks)},
        )
    if record.envelope.final_status == "interrupted":
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.ABANDONED,
            error="interrupted_turn_has_no_terminal_delivery",
        )
    try:
        delivered = await service._send_channel_message_safe(
            target_channel_id,
            {
                "content": (
                    f"Turn failed: {record.envelope.error_text or default_execution_error}"
                )
            },
            record_id=(
                f"{error_record_label}:{record.managed_thread_id}:{record.managed_turn_id}"
            ),
        )
        if not delivered:
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error="discord_error_notice_deferred",
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.FAILED,
            error=str(exc) or exc.__class__.__name__,
        )
    try:
        await _flush_delivery_outbox_files(
            service,
            workspace_root=workspace_root,
            channel_id=target_channel_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # intentional: do not surface cleanup failures after reply
        logger = getattr(service, "_logger", None)
        if logger is not None:
            log_event(
                logger,
                logging.WARNING,
                "discord.managed_thread.delivery_cleanup_failed",
                channel_id=target_channel_id,
                workspace_root=(
                    str(workspace_root) if workspace_root is not None else None
                ),
                managed_thread_id=record.managed_thread_id,
                managed_turn_id=record.managed_turn_id,
                exc=exc,
            )
    return ManagedThreadDeliveryAttemptResult(
        outcome=ManagedThreadDeliveryOutcome.DELIVERED
    )


def _resolve_delivery_channel_id(record: Any, *, fallback: Optional[str]) -> str:
    tt = record.target.transport_target
    if fallback is not None:
        return str(tt.get("channel_id") or fallback).strip()
    return str(tt.get("channel_id", "")).strip()


def _resolve_delivery_workspace_root(record: Any) -> Optional[Path]:
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    raw_path = metadata.get("workspace_root")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    return Path(raw_path)


async def _flush_delivery_outbox_files(
    service: Any,
    *,
    workspace_root: Optional[Path],
    channel_id: str,
) -> None:
    if workspace_root is None:
        return
    flush = getattr(service, "_flush_outbox_files", None)
    if not callable(flush):
        return
    await flush(workspace_root=workspace_root, channel_id=channel_id)


def build_discord_managed_thread_durable_delivery_hooks(
    service: Any,
    *,
    channel_id: str,
    managed_thread_id: str,
    workspace_root: Path,
    public_execution_error: str,
) -> ManagedThreadDurableDeliveryHooks:
    state_root = Path(getattr(getattr(service, "_config", None), "root", Path.cwd()))
    engine = SQLiteManagedThreadDeliveryEngine(state_root)

    class _DiscordManagedThreadDeliveryAdapter:
        @property
        def adapter_key(self) -> str:
            return "discord"

        async def deliver_managed_thread_record(
            self, record: Any, *, claim: Any
        ) -> Any:
            return await deliver_discord_managed_thread_record(
                service,
                record,
                claim=claim,
                channel_id_fallback=channel_id,
                base_record_label="discord-queued",
                error_record_label="discord-queued-error",
                default_execution_error=public_execution_error,
            )

    return ManagedThreadDurableDeliveryHooks(
        engine=engine,
        adapter=_DiscordManagedThreadDeliveryAdapter(),
        build_delivery_intent=lambda finalized: (
            None
            if finalized.status == "interrupted"
            else build_managed_thread_delivery_intent(
                finalized,
                surface=ManagedThreadSurfaceInfo(
                    log_label="Discord",
                    surface_kind="discord",
                    surface_key=channel_id,
                ),
                transport_target={"channel_id": channel_id},
                metadata={
                    "managed_thread_id": managed_thread_id,
                    "workspace_root": str(workspace_root),
                },
            )
        ),
    )
