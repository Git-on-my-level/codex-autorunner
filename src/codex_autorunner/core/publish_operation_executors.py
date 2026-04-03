from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from .orchestration.models import MessageRequest, MessageRequestKind
from .pma_chat_delivery import (
    deliver_pma_notification,
    notify_preferred_bound_chat_for_workspace,
    notify_primary_pma_chat_for_repo,
)
from .pma_thread_store import PmaThreadStore
from .publish_executor import PublishActionExecutor, TerminalPublishError
from .publish_journal import PublishOperation
from .scm_observability import correlation_id_for_operation, correlation_id_from_payload


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def _require_text(value: Any, *, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise TerminalPublishError(f"Publish payload is missing '{field_name}'")
    return normalized


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_request_kind(value: Any) -> MessageRequestKind:
    return "review" if _normalize_optional_text(value) == "review" else "message"


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _operation_digest(operation: PublishOperation, *, prefix: str) -> str:
    digest = hashlib.sha256(
        f"{operation.operation_kind}:{operation.operation_key}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _run_coroutine_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _managed_turn_request(
    thread_target_id: str,
    payload: dict[str, Any],
) -> tuple[MessageRequest, Optional[Any]]:
    request_payload = _normalize_mapping(payload.get("request"))
    source = request_payload or payload
    message_text = _normalize_optional_text(
        source.get("message_text") or source.get("prompt") or source.get("body")
    )
    if message_text is None:
        raise TerminalPublishError(
            "Publish payload is missing managed-turn message_text"
        )

    metadata = _normalize_mapping(source.get("metadata"))
    scm_metadata = _normalize_mapping(metadata.get("scm"))
    correlation_id = correlation_id_from_payload(payload)
    if correlation_id is not None:
        scm_metadata["correlation_id"] = correlation_id
        metadata["scm"] = scm_metadata
    input_items_raw = source.get("input_items")
    input_items: Optional[list[dict[str, Any]]] = None
    if isinstance(input_items_raw, list):
        items = [dict(item) for item in input_items_raw if isinstance(item, dict)]
        if items:
            input_items = items

    request = MessageRequest(
        target_id=_normalize_optional_text(source.get("target_id")) or thread_target_id,
        target_kind="thread",
        message_text=message_text,
        kind=_normalize_request_kind(source.get("kind") or source.get("request_kind")),
        busy_policy="queue",
        model=_normalize_optional_text(source.get("model")),
        reasoning=_normalize_optional_text(source.get("reasoning")),
        approval_mode=_normalize_optional_text(source.get("approval_mode")),
        input_items=input_items,
        context_profile=source.get("context_profile"),
        metadata=metadata,
    )
    return request, payload.get("sandbox_policy")


def _managed_turn_result(
    *,
    thread_target_id: str,
    client_request_id: str,
    turn: dict[str, Any],
    existed: bool,
    correlation_id: Optional[str] = None,
) -> dict[str, Any]:
    status = _normalize_optional_text(turn.get("status")) or "unknown"
    result = {
        "thread_target_id": thread_target_id,
        "managed_turn_id": _require_text(
            turn.get("managed_turn_id"), field_name="managed_turn_id"
        ),
        "status": status,
        "queued": status == "queued",
        "client_request_id": client_request_id,
        "deduped": existed,
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return result


def build_enqueue_managed_turn_executor(*, hub_root: Path) -> PublishActionExecutor:
    store = PmaThreadStore(hub_root)

    def executor(operation: PublishOperation) -> dict[str, Any]:
        payload = _normalize_mapping(operation.payload)
        correlation_id = correlation_id_for_operation(operation)
        thread_target_id = _require_text(
            payload.get("thread_target_id"),
            field_name="thread_target_id",
        )
        client_request_id = _operation_digest(operation, prefix="publish-turn")
        existing = store.get_turn_by_client_turn_id(thread_target_id, client_request_id)
        if existing is not None:
            return _managed_turn_result(
                thread_target_id=thread_target_id,
                client_request_id=client_request_id,
                turn=existing,
                existed=True,
                correlation_id=correlation_id,
            )

        request, sandbox_policy = _managed_turn_request(thread_target_id, payload)
        queue_payload = {
            "request": request.to_dict(),
            "client_request_id": client_request_id,
            "sandbox_policy": sandbox_policy,
        }
        created = store.create_turn(
            thread_target_id,
            prompt=request.message_text,
            request_kind=request.kind,
            busy_policy="queue",
            model=request.model,
            reasoning=request.reasoning,
            client_turn_id=client_request_id,
            queue_payload=queue_payload,
        )
        return _managed_turn_result(
            thread_target_id=thread_target_id,
            client_request_id=client_request_id,
            turn=created,
            existed=False,
            correlation_id=correlation_id,
        )

    return executor


def _resolve_thread_context(
    store: PmaThreadStore,
    *,
    payload: dict[str, Any],
) -> tuple[Optional[str], Optional[Path]]:
    repo_id = _normalize_optional_text(payload.get("repo_id"))
    workspace_root_raw = _normalize_optional_text(payload.get("workspace_root"))
    workspace_root = Path(workspace_root_raw) if workspace_root_raw else None
    thread_target_id = _normalize_optional_text(payload.get("thread_target_id"))
    if thread_target_id is None:
        return repo_id, workspace_root
    thread = store.get_thread(thread_target_id)
    if thread is None:
        raise TerminalPublishError(
            f"Unknown managed thread '{thread_target_id}' for notify_chat"
        )
    resolved_repo_id = repo_id or _normalize_optional_text(thread.get("repo_id"))
    if workspace_root is None:
        thread_workspace = _normalize_optional_text(thread.get("workspace_root"))
        workspace_root = Path(thread_workspace) if thread_workspace else None
    return resolved_repo_id, workspace_root


def build_notify_chat_executor(
    *,
    hub_root: Path,
    run_coroutine: Optional[Callable[[Coroutine[Any, Any, Any]], Any]] = None,
) -> PublishActionExecutor:
    store = PmaThreadStore(hub_root)
    coroutine_runner = run_coroutine or _run_coroutine_sync

    def executor(operation: PublishOperation) -> dict[str, Any]:
        payload = _normalize_mapping(operation.payload)
        message = _normalize_optional_text(
            payload.get("message") or payload.get("body") or payload.get("text")
        )
        if message is None:
            raise TerminalPublishError("Publish payload is missing notify_chat message")

        delivery = (
            _normalize_optional_text(
                payload.get("delivery")
                or payload.get("target")
                or payload.get("delivery_target")
            )
            or "auto"
        )
        correlation_id = correlation_id_for_operation(operation)
        delivery_correlation_id = correlation_id or _operation_digest(
            operation,
            prefix="publish-chat",
        )
        repo_id, workspace_root = _resolve_thread_context(store, payload=payload)

        if delivery == "none":
            outcome = {"route": "none", "targets": 0, "published": 0}
        elif delivery == "primary_pma":
            if repo_id is None:
                raise TerminalPublishError(
                    "notify_chat primary_pma delivery requires repo_id"
                )
            outcome = coroutine_runner(
                notify_primary_pma_chat_for_repo(
                    hub_root=hub_root,
                    repo_id=repo_id,
                    message=message,
                    correlation_id=delivery_correlation_id,
                )
            )
        elif delivery == "bound":
            if workspace_root is None:
                raise TerminalPublishError(
                    "notify_chat bound delivery requires workspace_root or thread_target_id"
                )
            outcome = coroutine_runner(
                notify_preferred_bound_chat_for_workspace(
                    hub_root=hub_root,
                    workspace_root=workspace_root,
                    repo_id=repo_id,
                    message=message,
                    correlation_id=delivery_correlation_id,
                )
            )
        else:
            outcome = coroutine_runner(
                deliver_pma_notification(
                    hub_root=hub_root,
                    workspace_root=workspace_root,
                    repo_id=repo_id,
                    message=message,
                    correlation_id=delivery_correlation_id,
                    delivery=delivery,
                    source_kind="publish_operation",
                )
            )

        targets = _coerce_int((outcome or {}).get("targets", 0))
        published = _coerce_int((outcome or {}).get("published", 0))
        result = {
            "delivery": delivery,
            "repo_id": repo_id,
            "targets": targets,
            "published": published,
        }
        route = _normalize_optional_text((outcome or {}).get("route"))
        if route is not None:
            result["route"] = route
        if correlation_id is not None:
            result["correlation_id"] = correlation_id
        return result

    return executor


__all__ = [
    "build_enqueue_managed_turn_executor",
    "build_notify_chat_executor",
]
