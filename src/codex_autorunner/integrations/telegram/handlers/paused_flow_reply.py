from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ....core.orchestration import (
    FlowTarget,
    PausedFlowTarget,
)


def paused_flow_status(run_record: Any) -> str:
    status = getattr(run_record, "status", None)
    if status is None:
        return "paused"
    return str(getattr(status, "value", status))


async def resolve_paused_flow_core(
    paused: Optional[tuple[str, Any]],
    workspace_root: Optional[Path],
) -> Optional[PausedFlowTarget]:
    if paused is None:
        return None
    run_id, run_record = paused
    ws = workspace_root or Path(".")
    return PausedFlowTarget(
        flow_target=FlowTarget(
            flow_target_id="ticket_flow",
            flow_type="ticket_flow",
            display_name="ticket_flow",
            workspace_root=str(ws),
        ),
        run_id=run_id,
        status=paused_flow_status(run_record),
        workspace_root=ws,
    )


async def submit_flow_reply_core(
    handlers: Any,
    message: Any,
    paused: Optional[tuple[str, Any]],
    workspace_root: Path,
    reply_text: str,
    files: Optional[list[tuple[str, bytes]]] = None,
) -> None:
    if paused is None:
        return
    run_id, run_record = paused
    expected_media = bool(
        message.photos or message.document or message.audio or message.voice
    )
    if expected_media and not files:
        await handlers._send_message(
            message.chat_id,
            "Failed to download media for paused flow reply. Please retry.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    success, result = await handlers._write_user_reply_from_telegram(
        workspace_root,
        run_id,
        run_record,
        message,
        reply_text,
        files,
    )
    await handlers._send_message(
        message.chat_id,
        result,
        thread_id=message.thread_id,
        reply_to=message.message_id,
    )
    if success:
        auto_resume = getattr(handlers, "_auto_resume_ticket_flow_run", None)
        if callable(auto_resume):
            await auto_resume(workspace_root, run_id)
            return
        await handlers._ticket_flow_bridge.auto_resume_run(workspace_root, run_id)
