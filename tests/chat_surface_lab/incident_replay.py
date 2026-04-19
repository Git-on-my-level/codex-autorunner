from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from tests.chat_surface_lab.scenario_runner import load_scenario

_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "authorization",
    "cookie",
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._-]+")
_LONG_TOKEN_RE = re.compile(r"\b[a-zA-Z0-9_\-]{24,}\b")
_LONG_NUMERIC_ID_RE = re.compile(r"\b\d{6,}\b")
_USER_HOME_RE = re.compile(r"/Users/[^/\s]+")


def convert_incident_trace_to_scenario(
    *,
    incident_path: Path,
    output_path: Path,
    scenario_id: Optional[str] = None,
) -> Path:
    incident_payload = json.loads(incident_path.read_text(encoding="utf-8"))
    if not isinstance(incident_payload, dict):
        raise ValueError(f"{incident_path}: incident payload must be a JSON object")
    sanitized = sanitize_incident_payload(incident_payload)
    scenario_payload = build_replay_scenario_payload(
        incident=sanitized,
        scenario_id=scenario_id,
    )
    _write_json(output_path, scenario_payload)
    load_scenario(output_path)
    return output_path


def build_replay_scenario_payload(
    *,
    incident: dict[str, Any],
    scenario_id: Optional[str] = None,
) -> dict[str, Any]:
    explicit = incident.get("scenario")
    if isinstance(explicit, dict):
        payload = dict(explicit)
    else:
        payload = _build_scenario_from_symptoms(incident)
    normalized_id = str(
        scenario_id
        or payload.get("scenario_id")
        or incident.get("incident_id")
        or "incident_replay"
    ).strip()
    if not normalized_id:
        normalized_id = "incident_replay"
    payload["scenario_id"] = normalized_id
    payload.setdefault("schema_version", 1)
    payload.setdefault(
        "description",
        str(incident.get("title") or "Incident replay generated from trace").strip(),
    )
    tags = payload.get("tags")
    if not isinstance(tags, list):
        tags = []
    if "incident_replay" not in tags:
        tags.append("incident_replay")
    payload["tags"] = tags
    notes = str(incident.get("notes") or "").strip()
    if notes and not payload.get("notes"):
        payload["notes"] = notes
    return payload


def sanitize_incident_payload(payload: Any) -> Any:
    return _sanitize_value(payload, key_hint=None)


def _build_scenario_from_symptoms(incident: dict[str, Any]) -> dict[str, Any]:
    surfaces = _normalize_surfaces(incident)
    prompt = str(incident.get("prompt") or "incident replay").strip()
    symptoms = {str(value).strip() for value in _iter_symptoms(incident)}
    runtime_scenario = "official"
    actions: list[dict[str, Any]] = []
    faults: list[dict[str, Any]] = []
    invariants: list[dict[str, Any]] = []

    if "delayed_terminal_event" in symptoms:
        runtime_scenario = "official_request_return_after_terminal"
    if "timeout_when_terminal_missing" in symptoms:
        runtime_scenario = "official_prompt_hang"
        faults.append(
            {
                "kind": "set_surface_timeout",
                "target": "surface",
                "surfaces": list(surfaces),
                "parameters": {"seconds": 0.05},
            }
        )
    if "delete_failure" in symptoms:
        if "discord" in surfaces:
            faults.append(
                {
                    "kind": "fail_progress_delete",
                    "target": "surface",
                    "surfaces": ["discord"],
                    "parameters": {"message_id": "msg-1"},
                }
            )
        if "telegram" in surfaces:
            faults.append(
                {
                    "kind": "fail_progress_delete",
                    "target": "surface",
                    "surfaces": ["telegram"],
                    "parameters": {"message_id": 1},
                }
            )
    if "retry_after_backpressure" in symptoms:
        if "discord" in surfaces:
            faults.append(
                {
                    "kind": "retry_after",
                    "target": "surface",
                    "surfaces": ["discord"],
                    "parameters": {
                        "operation": "create_channel_message",
                        "seconds": [1],
                    },
                }
            )
        if "telegram" in surfaces:
            faults.append(
                {
                    "kind": "retry_after",
                    "target": "surface",
                    "surfaces": ["telegram"],
                    "parameters": {
                        "operation": "send_message",
                        "seconds": [1],
                    },
                }
            )

    if "restart_window" in symptoms:
        if "discord" in surfaces:
            actions.extend(
                [
                    {
                        "kind": "run_status_interaction",
                        "actor": "user",
                        "surfaces": ["discord"],
                        "payload": {"interaction_id": "incident-restart-1"},
                    },
                    {
                        "kind": "restart_surface_harness",
                        "actor": "system",
                        "surfaces": ["discord"],
                        "payload": {},
                    },
                    {
                        "kind": "run_status_interaction",
                        "actor": "user",
                        "surfaces": ["discord"],
                        "payload": {"interaction_id": "incident-restart-1"},
                    },
                ]
            )
        if "telegram" in surfaces:
            actions.extend(
                [
                    {
                        "kind": "run_status_update",
                        "actor": "user",
                        "surfaces": ["telegram"],
                        "payload": {"update_id": 91, "thread_id": 55},
                    },
                    {
                        "kind": "restart_surface_harness",
                        "actor": "system",
                        "surfaces": ["telegram"],
                        "payload": {},
                    },
                    {
                        "kind": "run_status_update",
                        "actor": "user",
                        "surfaces": ["telegram"],
                        "payload": {"update_id": 91, "thread_id": 55},
                    },
                ]
            )
    if "duplicate_delivery" in symptoms:
        if "discord" in surfaces:
            actions.append(
                {
                    "kind": "run_duplicate_status_interaction",
                    "actor": "user",
                    "surfaces": ["discord"],
                    "payload": {"interaction_id": "incident-dup-1"},
                }
            )
        if "telegram" in surfaces:
            actions.append(
                {
                    "kind": "run_duplicate_status_update",
                    "actor": "user",
                    "surfaces": ["telegram"],
                    "payload": {"update_id": 92, "thread_id": 55},
                }
            )
    if "queued_submission" in symptoms:
        if "discord" in surfaces:
            actions.extend(
                [
                    {
                        "kind": "start_message",
                        "actor": "user",
                        "surfaces": ["discord"],
                        "payload": {"text": "cancel me"},
                    },
                    {
                        "kind": "wait_for_running_execution",
                        "actor": "system",
                        "surfaces": ["discord"],
                        "payload": {"timeout_seconds": 2.0},
                    },
                    {
                        "kind": "submit_active_message",
                        "actor": "user",
                        "surfaces": ["discord"],
                        "payload": {
                            "text": "echo queued after busy",
                            "message_id": "m-2",
                        },
                    },
                    {
                        "kind": "wait_for_log_event",
                        "actor": "system",
                        "surfaces": ["discord"],
                        "payload": {
                            "event_name": "discord.turn.managed_thread_submission",
                            "field": "queued",
                            "equals": True,
                            "timeout_seconds": 2.0,
                        },
                    },
                    {
                        "kind": "stop_active_thread",
                        "actor": "system",
                        "surfaces": ["discord"],
                        "payload": {},
                    },
                    {
                        "kind": "await_active_message",
                        "actor": "system",
                        "surfaces": ["discord"],
                        "payload": {},
                    },
                ]
            )
        if "telegram" in surfaces:
            actions.extend(
                [
                    {
                        "kind": "start_message",
                        "actor": "user",
                        "surfaces": ["telegram"],
                        "payload": {"text": "cancel me"},
                    },
                    {
                        "kind": "wait_for_running_execution",
                        "actor": "system",
                        "surfaces": ["telegram"],
                        "payload": {"timeout_seconds": 2.0},
                    },
                    {
                        "kind": "submit_active_message",
                        "actor": "user",
                        "surfaces": ["telegram"],
                        "payload": {
                            "text": "echo queued after busy",
                            "thread_id": 55,
                            "message_id": 2,
                            "update_id": 2,
                        },
                    },
                    {
                        "kind": "wait_for_log_event",
                        "actor": "system",
                        "surfaces": ["telegram"],
                        "payload": {
                            "event_name": "telegram.turn.queued",
                            "timeout_seconds": 2.0,
                        },
                    },
                    {
                        "kind": "stop_active_thread",
                        "actor": "system",
                        "surfaces": ["telegram"],
                        "payload": {},
                    },
                    {
                        "kind": "await_active_message",
                        "actor": "system",
                        "surfaces": ["telegram"],
                        "payload": {},
                    },
                ]
            )
    if not actions:
        actions.append(
            {
                "kind": "send_message",
                "actor": "user",
                "payload": {"text": prompt or "incident replay"},
            }
        )

    for needle in _normalize_contains_text(incident.get("assert_contains")):
        invariants.append(
            {
                "kind": "contains_text",
                "surfaces": list(surfaces),
                "params": {"text": needle},
            }
        )

    return {
        "schema_version": 1,
        "surfaces": list(surfaces),
        "runtime_fixture": {
            "kind": "hermes",
            "scenario": runtime_scenario,
        },
        "actions": actions,
        "faults": faults,
        "transcript_invariants": invariants,
    }


def _sanitize_value(value: Any, *, key_hint: Optional[str]) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, raw in value.items():
            normalized_key = str(key)
            if _looks_sensitive_key(normalized_key):
                sanitized[normalized_key] = "<redacted>"
                continue
            sanitized[normalized_key] = _sanitize_value(raw, key_hint=normalized_key)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, key_hint=key_hint) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value, key_hint=key_hint)
    return value


def _sanitize_string(value: str, *, key_hint: Optional[str]) -> str:
    if key_hint is not None and _looks_sensitive_key(key_hint):
        return "<redacted>"
    sanitized = str(value)
    sanitized = _USER_HOME_RE.sub("/Users/<redacted>", sanitized)
    sanitized = _BEARER_TOKEN_RE.sub("Bearer <redacted>", sanitized)
    sanitized = _LONG_TOKEN_RE.sub("<redacted-token>", sanitized)
    sanitized = _LONG_NUMERIC_ID_RE.sub("<redacted-id>", sanitized)
    return sanitized


def _looks_sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _iter_symptoms(incident: dict[str, Any]) -> Iterable[str]:
    values = incident.get("symptoms")
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str) and item.strip():
                yield item.strip()


def _normalize_surfaces(incident: dict[str, Any]) -> tuple[str, ...]:
    raw = incident.get("surfaces")
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        single = str(incident.get("surface") or "").strip()
        values = [single] if single else []
    normalized = tuple(
        sorted({value for value in values if value in {"discord", "telegram"}})
    )
    if normalized:
        return normalized
    return ("discord",)


def _normalize_contains_text(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        value = raw.strip()
        return (value,) if value else ()
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            values.append(text)
    return tuple(values)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "build_replay_scenario_payload",
    "convert_incident_trace_to_scenario",
    "sanitize_incident_payload",
]
