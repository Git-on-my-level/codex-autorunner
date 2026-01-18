from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from .events import SSEEvent

PermissionDecision = str
PermissionHandler = Callable[[str, dict[str, Any]], Awaitable[PermissionDecision]]

PERMISSION_ALLOW = "allow"
PERMISSION_DENY = "deny"
PERMISSION_ASK = "ask"


@dataclass(frozen=True)
class OpenCodeMessageResult:
    text: str
    error: Optional[str] = None


@dataclass(frozen=True)
class OpenCodeTurnOutput:
    text: str
    error: Optional[str] = None


def split_model_id(model: Optional[str]) -> Optional[dict[str, str]]:
    if not model or "/" not in model:
        return None
    provider_id, model_id = model.split("/", 1)
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not provider_id or not model_id:
        return None
    return {"providerID": provider_id, "modelID": model_id}


def build_turn_id(session_id: str) -> str:
    return f"{session_id}:{int(time.time() * 1000)}"


def extract_session_id(
    payload: Any, *, allow_fallback_id: bool = False
) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("sessionID", "sessionId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if allow_fallback_id:
        value = payload.get("id")
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        value = properties.get("sessionID")
        if isinstance(value, str) and value:
            return value
        part = properties.get("part")
        if isinstance(part, dict):
            value = part.get("sessionID")
            if isinstance(value, str) and value:
                return value
    session = payload.get("session")
    if isinstance(session, dict):
        return extract_session_id(session, allow_fallback_id=allow_fallback_id)
    return None


def extract_turn_id(session_id: str, payload: Any) -> str:
    if isinstance(payload, dict):
        info = payload.get("info")
        if isinstance(info, dict):
            for key in ("id", "messageId", "message_id", "turn_id", "turnId"):
                value = info.get(key)
                if isinstance(value, str) and value:
                    return value
        for key in ("id", "messageId", "message_id", "turn_id", "turnId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return build_turn_id(session_id)


def parse_message_response(payload: Any) -> OpenCodeMessageResult:
    if not isinstance(payload, dict):
        return OpenCodeMessageResult(text="")
    info = payload.get("info")
    error = _extract_error_text(info) or _extract_error_text(payload)
    parts_raw = payload.get("parts")
    text_parts: list[str] = []
    if isinstance(parts_raw, list):
        for part in parts_raw:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
    return OpenCodeMessageResult(text="".join(text_parts).strip(), error=error)


def _extract_error_text(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "error"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(error, str) and error:
        return error
    for key in ("detail", "message", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_permission_request(payload: Any) -> tuple[Optional[str], dict[str, Any]]:
    if not isinstance(payload, dict):
        return None, {}
    properties = payload.get("properties")
    if isinstance(properties, dict):
        request_id = properties.get("id") or properties.get("requestID")
        if isinstance(request_id, str) and request_id:
            return request_id, properties
    request_id = payload.get("id") or payload.get("requestID")
    if isinstance(request_id, str) and request_id:
        return request_id, payload
    return None, {}


def format_permission_prompt(payload: dict[str, Any]) -> str:
    lines = ["Approval required"]
    reason = payload.get("reason") or payload.get("message") or payload.get("detail")
    if isinstance(reason, str) and reason:
        lines.append(f"Reason: {reason}")
    action = payload.get("action") or payload.get("tool")
    if isinstance(action, str) and action:
        lines.append(f"Action: {action}")
    target = payload.get("target") or payload.get("path")
    if isinstance(target, str) and target:
        lines.append(f"Target: {target}")
    return "\n".join(lines)


def map_approval_policy_to_permission(
    approval_policy: Optional[str], *, default: str = PERMISSION_ALLOW
) -> str:
    if approval_policy is None:
        return default
    normalized = approval_policy.strip().lower()
    if normalized in ("never", "allow", "approved", "approve"):
        return PERMISSION_ALLOW
    if normalized in ("deny", "reject", "blocked"):
        return PERMISSION_DENY
    if normalized in (
        "on-request",
        "on-failure",
        "on_failure",
        "onfailure",
        "unlesstrusted",
        "untrusted",
        "ask",
        "auto",
    ):
        return PERMISSION_ASK
    return default


async def opencode_missing_env(
    client: Any,
    workspace_root: str,
    model_payload: Optional[dict[str, str]],
) -> list[str]:
    if not model_payload:
        return []
    provider_id = model_payload.get("providerID")
    if not provider_id:
        return []
    try:
        payload = await client.providers(directory=workspace_root)
    except Exception:
        return []
    providers: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_providers = payload.get("providers")
        if isinstance(raw_providers, list):
            providers = [entry for entry in raw_providers if isinstance(entry, dict)]
    elif isinstance(payload, list):
        providers = [entry for entry in payload if isinstance(entry, dict)]
    for provider in providers:
        pid = provider.get("id") or provider.get("providerID")
        if pid != provider_id:
            continue
        if _provider_has_auth(pid, workspace_root):
            return []
        env_keys = provider.get("env")
        if not isinstance(env_keys, list):
            return []
        missing = [
            key
            for key in env_keys
            if isinstance(key, str) and key and not os.getenv(key)
        ]
        return missing
    return []


def _provider_has_auth(provider_id: str, workspace_root: str) -> bool:
    auth_path = _find_opencode_auth_path(workspace_root)
    if auth_path is None or not auth_path.exists():
        return False
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    entry = payload.get(provider_id)
    return isinstance(entry, dict) and any(bool(value) for value in entry.values())


def _find_opencode_auth_path(workspace_root: str) -> Optional[Path]:
    data_home = os.getenv("XDG_DATA_HOME")
    if not data_home:
        home = os.getenv("HOME")
        if not home:
            inferred = _infer_home_from_workspace(workspace_root)
            if inferred is None:
                return None
            data_home = str(inferred / ".local" / "share")
        else:
            data_home = str(Path(home) / ".local" / "share")
    return Path(data_home) / "opencode" / "auth.json"


def _infer_home_from_workspace(workspace_root: str) -> Optional[Path]:
    resolved = Path(workspace_root).resolve()
    parts = resolved.parts
    if (
        len(parts) >= 6
        and parts[0] == os.path.sep
        and parts[1] == "System"
        and parts[2] == "Volumes"
        and parts[3] == "Data"
        and parts[4] == "Users"
    ):
        return Path(parts[0]) / parts[1] / parts[2] / parts[3] / parts[4] / parts[5]
    if (
        len(parts) >= 3
        and parts[0] == os.path.sep
        and parts[1]
        in (
            "Users",
            "home",
        )
    ):
        return Path(parts[0]) / parts[1] / parts[2]
    return None


async def collect_opencode_output_from_events(
    events: AsyncIterator[SSEEvent],
    *,
    session_id: str,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    respond_permission: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> OpenCodeTurnOutput:
    text_parts: list[str] = []
    part_lengths: dict[str, int] = {}
    last_full_text = ""
    error: Optional[str] = None

    async for event in events:
        if should_stop is not None and should_stop():
            break
        raw = event.data or ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        event_session_id = extract_session_id(payload)
        if not event_session_id or event_session_id != session_id:
            continue
        if event.event == "permission.asked":
            request_id, props = _extract_permission_request(payload)
            if request_id and respond_permission is not None:
                reply = PERMISSION_DENY
                if permission_policy == PERMISSION_ALLOW:
                    reply = PERMISSION_ALLOW
                elif (
                    permission_policy == PERMISSION_ASK
                    and permission_handler is not None
                ):
                    try:
                        decision = await permission_handler(request_id, props)
                    except Exception:
                        decision = "reject"
                    decision_norm = str(decision or "").strip().lower()
                    if decision_norm in ("allow", "approved", "approve"):
                        reply = PERMISSION_ALLOW
                    elif decision_norm in ("deny", "reject", "cancel"):
                        reply = PERMISSION_DENY
                try:
                    await respond_permission(request_id, reply)
                except Exception:
                    pass
        if event.event == "session.error":
            error = _extract_error_text(payload) or "OpenCode session error"
            break
        if event.event == "message.part.updated":
            properties = (
                payload.get("properties") if isinstance(payload, dict) else None
            )
            if isinstance(properties, dict):
                part = properties.get("part")
                delta = properties.get("delta")
            else:
                part = payload.get("part")
                delta = payload.get("delta")
            if isinstance(delta, dict):
                delta = delta.get("text")
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
            elif isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    part_id = part.get("id") or part.get("partId")
                    if isinstance(part_id, str) and part_id:
                        last_len = part_lengths.get(part_id, 0)
                        if len(text) > last_len:
                            text_parts.append(text[last_len:])
                            part_lengths[part_id] = len(text)
                    else:
                        if last_full_text and text.startswith(last_full_text):
                            text_parts.append(text[len(last_full_text) :])
                        elif text != last_full_text:
                            text_parts.append(text)
                        last_full_text = text
        if event.event in ("message.completed", "message.updated"):
            message_result = parse_message_response(payload)
            if message_result.text and not text_parts:
                text_parts.append(message_result.text)
            if message_result.error and not error:
                error = message_result.error
        if event.event == "session.idle":
            break

    return OpenCodeTurnOutput(text="".join(text_parts).strip(), error=error)


async def collect_opencode_output(
    client: Any,
    *,
    session_id: str,
    workspace_path: str,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> OpenCodeTurnOutput:
    async def _respond(request_id: str, reply: str) -> None:
        await client.respond_permission(request_id=request_id, reply=reply)

    return await collect_opencode_output_from_events(
        client.stream_events(directory=workspace_path),
        session_id=session_id,
        permission_policy=permission_policy,
        permission_handler=permission_handler,
        should_stop=should_stop,
        respond_permission=_respond,
    )


__all__ = [
    "OpenCodeMessageResult",
    "OpenCodeTurnOutput",
    "PERMISSION_ALLOW",
    "PERMISSION_ASK",
    "PERMISSION_DENY",
    "build_turn_id",
    "collect_opencode_output",
    "collect_opencode_output_from_events",
    "extract_session_id",
    "extract_turn_id",
    "format_permission_prompt",
    "map_approval_policy_to_permission",
    "opencode_missing_env",
    "parse_message_response",
    "split_model_id",
]
