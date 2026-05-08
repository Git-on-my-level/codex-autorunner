from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any, Optional

from ....core.logging_utils import log_event
from ....core.orchestration import (
    PausedFlowTarget,
    SurfaceThreadMessageRequest,
    build_surface_orchestration_ingress,
)
from ....core.pma_notification_store import notification_surface_key
from .commands.execution import _build_telegram_thread_orchestration_service
from .media_ingress import (
    submit_thread_message_core,
)
from .message_policy import event_logger
from .paused_flow_reply import resolve_paused_flow_core, submit_flow_reply_core


class TelegramSurfaceTurnDispatch:
    __slots__ = (
        "handlers",
        "message",
        "runtime",
        "record",
        "topic_key",
        "workspace_root",
        "prompt_text",
        "flow_reply_text",
        "pma_enabled",
        "paused",
        "notification_reply",
        "placeholder_id",
    )

    def __init__(
        self,
        *,
        handlers: Any,
        message: Any,
        runtime: Any,
        record: Any,
        topic_key: str,
        workspace_root: Path,
        prompt_text: str,
        flow_reply_text: str,
        pma_enabled: bool,
        paused: Optional[tuple[str, Any]],
        notification_reply: Any,
        placeholder_id: Optional[int],
    ) -> None:
        self.handlers = handlers
        self.message = message
        self.runtime = runtime
        self.record = record
        self.topic_key = topic_key
        self.workspace_root = workspace_root
        self.prompt_text = prompt_text
        self.flow_reply_text = flow_reply_text
        self.pma_enabled = pma_enabled
        self.paused = paused
        self.notification_reply = notification_reply
        self.placeholder_id = placeholder_id

    def build_request(self) -> SurfaceThreadMessageRequest:
        return SurfaceThreadMessageRequest(
            surface_kind="telegram",
            workspace_root=self.workspace_root,
            prompt_text=self.prompt_text,
            agent_id=getattr(self.record, "agent", None),
            pma_enabled=self.pma_enabled,
        )


def build_telegram_surface_ingress(
    handlers: Any,
    *,
    topic_key: str,
    message: Any,
) -> Any:
    evt_logger = event_logger(handlers)
    return build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: log_event(
            evt_logger,
            logging.INFO,
            f"telegram.{orchestration_event.event_type}",
            topic_key=topic_key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            surface_kind=orchestration_event.surface_kind,
            target_kind=orchestration_event.target_kind,
            target_id=orchestration_event.target_id,
            status=orchestration_event.status,
            **orchestration_event.metadata,
        )
    )


async def resolve_telegram_paused_flow(
    _request: SurfaceThreadMessageRequest,
    *,
    dispatch: TelegramSurfaceTurnDispatch,
) -> Optional[PausedFlowTarget]:
    return await resolve_paused_flow_core(dispatch.paused, dispatch.workspace_root)


async def submit_telegram_flow_reply(
    _request: SurfaceThreadMessageRequest,
    flow_target: PausedFlowTarget,
    *,
    dispatch: TelegramSurfaceTurnDispatch,
) -> None:
    await submit_flow_reply_core(
        dispatch.handlers,
        dispatch.message,
        dispatch.paused,
        dispatch.workspace_root,
        dispatch.flow_reply_text,
    )


async def submit_telegram_thread_message(
    _request: SurfaceThreadMessageRequest,
    *,
    dispatch: TelegramSurfaceTurnDispatch,
) -> None:
    await submit_thread_message_core(
        dispatch.handlers,
        dispatch.message,
        dispatch.runtime,
        dispatch.record,
        text_override=dispatch.prompt_text,
        placeholder_id=dispatch.placeholder_id,
        notification_reply=dispatch.notification_reply,
    )


async def bind_telegram_notification_continuation(
    dispatch: TelegramSurfaceTurnDispatch,
) -> None:
    notification_reply = dispatch.notification_reply
    if notification_reply is None:
        return
    orch_service = _build_telegram_thread_orchestration_service(dispatch.handlers)
    orch_binding = (
        orch_service.get_binding(
            surface_kind="telegram",
            surface_key=notification_surface_key(notification_reply.notification_id),
        )
        if orch_service is not None
        else None
    )
    if orch_binding is not None:
        hub_client = getattr(dispatch.handlers, "_hub_client", None)
        if hub_client is not None:
            from ....core.hub_control_plane import (
                NotificationContinuationBindRequest as _CPContinuationRequest,
            )

            try:
                await hub_client.bind_notification_continuation(
                    _CPContinuationRequest(
                        notification_id=notification_reply.notification_id,
                        thread_target_id=orch_binding.thread_target_id,
                    )
                )
                return
            except Exception as exc:
                log_event(
                    event_logger(dispatch.handlers),
                    logging.WARNING,
                    "telegram.notification.continuation_bind.control_plane_failed",
                    notification_id=notification_reply.notification_id,
                    exc=exc,
                )
        else:
            log_event(
                event_logger(dispatch.handlers),
                logging.WARNING,
                "telegram.notification.continuation_bind.hub_client_unavailable",
                notification_id=notification_reply.notification_id,
            )


async def submit_telegram_surface_turn(
    dispatch: TelegramSurfaceTurnDispatch,
) -> None:
    ingress = build_telegram_surface_ingress(
        dispatch.handlers,
        topic_key=dispatch.topic_key,
        message=dispatch.message,
    )
    request = dispatch.build_request()
    if dispatch.notification_reply is not None:
        await submit_telegram_thread_message(request, dispatch=dispatch)
        await bind_telegram_notification_continuation(dispatch)
        return
    await ingress.submit_message(
        request,
        resolve_paused_flow_target=partial(
            resolve_telegram_paused_flow, dispatch=dispatch
        ),
        submit_flow_reply=partial(submit_telegram_flow_reply, dispatch=dispatch),
        submit_thread_message=partial(
            submit_telegram_thread_message, dispatch=dispatch
        ),
    )
