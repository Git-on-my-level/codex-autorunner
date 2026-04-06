from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....core.logging_utils import log_event
from .....core.pma_chat_delivery import deliver_pma_notification

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

PMA_DISCORD_MESSAGE_MAX_LEN = 1900
PMA_PUBLISH_RETRY_DELAYS_SECONDS = (0.0, 0.25, 0.75)


def normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = value if isinstance(value, str) else str(value)
    return normalized.strip() or None


def resolve_chat_state_path(
    request: Request, *, section: str, default_state_file: str
) -> Path:
    hub_root = request.app.state.config.root
    raw = getattr(request.app.state.config, "raw", {})
    section_cfg = raw.get(section) if isinstance(raw, dict) else {}
    if not isinstance(section_cfg, dict):
        section_cfg = {}
    state_file = section_cfg.get("state_file")
    if not isinstance(state_file, str) or not state_file.strip():
        state_file = default_state_file
    state_path = Path(state_file)
    if not state_path.is_absolute():
        state_path = (hub_root / state_path).resolve()
    return state_path


def resolve_publish_repo_id(
    *,
    request: Request,
    lifecycle_event: Optional[dict[str, Any]],
    wake_up: Optional[dict[str, Any]],
) -> Optional[str]:
    from .....core.pma_thread_store import PmaThreadStore

    for candidate in (
        lifecycle_event.get("repo_id") if lifecycle_event else None,
        wake_up.get("repo_id") if wake_up else None,
    ):
        normalized = normalize_optional_text(candidate)
        if normalized:
            return normalized

    thread_id = (
        normalize_optional_text(wake_up.get("thread_id"))
        if isinstance(wake_up, dict)
        else None
    )
    if not thread_id:
        return None
    try:
        thread = PmaThreadStore(request.app.state.config.root).get_thread(thread_id)
    except (OSError, ValueError, RuntimeError):
        logger.exception(
            "Failed resolving managed thread repo for publish thread_id=%s",
            thread_id,
        )
        return None
    if not isinstance(thread, dict):
        return None
    return normalize_optional_text(thread.get("repo_id"))


def build_publish_correlation_id(
    *,
    result: dict[str, Any],
    client_turn_id: Optional[str],
    wake_up: Optional[dict[str, Any]],
) -> str:
    for candidate in (
        client_turn_id,
        result.get("client_turn_id"),
        result.get("turn_id"),
        wake_up.get("wakeup_id") if isinstance(wake_up, dict) else None,
    ):
        normalized = normalize_optional_text(candidate)
        if normalized:
            return normalized
    return f"pma-{uuid.uuid4().hex[:12]}"


def build_publish_message(
    *,
    result: dict[str, Any],
    lifecycle_event: Optional[dict[str, Any]],
    wake_up: Optional[dict[str, Any]],
    correlation_id: str,
) -> str:
    trigger = (
        normalize_optional_text(lifecycle_event.get("event_type"))
        if isinstance(lifecycle_event, dict)
        else None
    )
    if not trigger and isinstance(wake_up, dict):
        trigger = normalize_optional_text(wake_up.get("event_type")) or (
            normalize_optional_text(wake_up.get("source")) or "automation"
        )
    trigger = trigger or "automation"

    repo_id = (
        normalize_optional_text(lifecycle_event.get("repo_id"))
        if isinstance(lifecycle_event, dict)
        else None
    )
    if not repo_id and isinstance(wake_up, dict):
        repo_id = normalize_optional_text(wake_up.get("repo_id"))

    run_id = (
        normalize_optional_text(lifecycle_event.get("run_id"))
        if isinstance(lifecycle_event, dict)
        else None
    )
    if not run_id and isinstance(wake_up, dict):
        run_id = normalize_optional_text(wake_up.get("run_id"))

    thread_id = (
        normalize_optional_text(wake_up.get("thread_id"))
        if isinstance(wake_up, dict)
        else None
    )
    status = normalize_optional_text(result.get("status")) or "error"
    detail = normalize_optional_text(result.get("detail"))
    output = normalize_optional_text(result.get("message"))

    lines: list[str] = [f"PMA update ({trigger})"]
    if repo_id:
        lines.append(f"repo_id: {repo_id}")
    if run_id:
        lines.append(f"run_id: {run_id}")
    if thread_id:
        lines.append(f"thread_id: {thread_id}")
    lines.append(f"correlation_id: {correlation_id}")
    lines.append("")

    if status == "ok":
        lines.append(output or "Turn completed with no assistant output.")
    else:
        lines.append(f"status: {status}")
        lines.append(f"error: {detail or 'Turn failed without detail.'}")
        lines.append("next_action: run /pma status and inspect PMA history if needed.")
    return "\n".join(lines).strip()


async def enqueue_with_retry(enqueue_call: Any) -> None:
    last_error: Optional[Exception] = None
    for delay_seconds in PMA_PUBLISH_RETRY_DELAYS_SECONDS:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        try:
            await enqueue_call()
            return
        except (
            Exception
        ) as exc:  # intentional: enqueue callback - exception types depend on queue backend
            last_error = exc
    if last_error is not None:
        raise last_error


async def publish_automation_result(
    *,
    request: Request,
    result: dict[str, Any],
    client_turn_id: Any,
    lifecycle_event: Any,
    wake_up: Any,
) -> dict[str, Any]:
    hub_root = request.app.state.config.root
    lifecycle_event_dict = (
        lifecycle_event if isinstance(lifecycle_event, dict) else None
    )
    wake_up_dict = wake_up if isinstance(wake_up, dict) else None
    client_turn_id_str = normalize_optional_text(client_turn_id)
    correlation_id = build_publish_correlation_id(
        result=result,
        client_turn_id=client_turn_id_str,
        wake_up=wake_up_dict,
    )
    target_repo_id = resolve_publish_repo_id(
        request=request,
        lifecycle_event=lifecycle_event_dict,
        wake_up=wake_up_dict,
    )
    message = build_publish_message(
        result=result,
        lifecycle_event=lifecycle_event_dict,
        wake_up=wake_up_dict,
        correlation_id=correlation_id,
    )
    workspace_root: Optional[Path] = None
    if isinstance(wake_up_dict, dict):
        workspace_root = (
            Path(wake_up_dict["workspace_root"])
            if normalize_optional_text(wake_up_dict.get("workspace_root"))
            else None
        )
    if workspace_root is None and isinstance(lifecycle_event_dict, dict):
        raw_workspace = normalize_optional_text(
            lifecycle_event_dict.get("workspace_root")
        )
        if raw_workspace:
            workspace_root = Path(raw_workspace)
    outcome = await deliver_pma_notification(
        hub_root=hub_root,
        message=message,
        correlation_id=correlation_id,
        delivery="auto",
        source_kind=(
            normalize_optional_text(
                lifecycle_event_dict.get("event_type") if lifecycle_event_dict else None
            )
            or normalize_optional_text(
                wake_up_dict.get("event_type") if wake_up_dict else None
            )
            or normalize_optional_text(
                wake_up_dict.get("source") if wake_up_dict else None
            )
            or "automation"
        ),
        repo_id=target_repo_id,
        workspace_root=workspace_root,
        run_id=normalize_optional_text(
            (lifecycle_event_dict or {}).get("run_id")
            or (wake_up_dict or {}).get("run_id")
        ),
        managed_thread_id=normalize_optional_text(
            (wake_up_dict or {}).get("thread_id")
        ),
        context_payload={
            "result": dict(result or {}),
            "lifecycle_event": dict(lifecycle_event_dict or {}),
            "wake_up": dict(wake_up_dict or {}),
        },
    )
    targets = int(outcome.get("targets", 0) or 0)
    published = int(outcome.get("published", 0) or 0)
    delivery_status = "success" if published > 0 else "skipped"
    delivery_outcome = {
        "published": published,
        "targets": targets,
        "route": outcome.get("route"),
        "repo_id": target_repo_id,
        "correlation_id": correlation_id,
    }
    log_event(
        logger,
        logging.INFO,
        "pma.turn.publish",
        delivery_status=delivery_status,
        route=outcome.get("route"),
        targets=targets,
        published=published,
        repo_id=target_repo_id,
        correlation_id=correlation_id,
    )
    return {
        "delivery_status": delivery_status,
        "delivery_outcome": delivery_outcome,
    }
