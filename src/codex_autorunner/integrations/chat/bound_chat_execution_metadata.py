from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

_BOUND_CHAT_EXECUTION_KEY = "bound_chat_execution"
_SUPPORTED_SURFACE_KINDS = frozenset({"discord", "telegram"})


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_surface_kind(value: Any) -> Optional[str]:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered not in _SUPPORTED_SURFACE_KINDS:
        return None
    return lowered


def normalize_bound_chat_surface_targets(
    surface_targets: Sequence[tuple[str, str]] | None,
) -> tuple[tuple[str, str], ...]:
    normalized_targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for surface_kind, surface_key in surface_targets or ():
        normalized_surface_kind = _normalize_surface_kind(surface_kind)
        normalized_surface_key = _normalize_optional_text(surface_key)
        if normalized_surface_kind is None or normalized_surface_key is None:
            continue
        pair = (normalized_surface_kind, normalized_surface_key)
        if pair in seen:
            continue
        seen.add(pair)
        normalized_targets.append(pair)
    return tuple(normalized_targets)


def build_bound_chat_execution_metadata(
    *,
    origin_kind: str,
    origin_surface_kind: Optional[str] = None,
    origin_surface_key: Optional[str] = None,
    progress_targets: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    normalized_origin_kind = _normalize_optional_text(origin_kind)
    if normalized_origin_kind is None:
        return {}
    payload: dict[str, Any] = {}
    if normalized_origin_kind == "surface":
        normalized_surface_kind = _normalize_surface_kind(origin_surface_kind)
        normalized_surface_key = _normalize_optional_text(origin_surface_key)
        if normalized_surface_kind is not None and normalized_surface_key is not None:
            payload["origin"] = {
                "kind": "surface",
                "surface_kind": normalized_surface_kind,
                "surface_key": normalized_surface_key,
            }
    else:
        payload["origin"] = {"kind": normalized_origin_kind}
    normalized_targets = normalize_bound_chat_surface_targets(progress_targets)
    if normalized_targets:
        payload["progress_targets"] = [
            {
                "surface_kind": surface_kind,
                "surface_key": surface_key,
            }
            for surface_kind, surface_key in normalized_targets
        ]
    if not payload:
        return {}
    return {_BOUND_CHAT_EXECUTION_KEY: payload}


def merge_bound_chat_execution_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    origin_kind: str,
    origin_surface_kind: Optional[str] = None,
    origin_surface_key: Optional[str] = None,
    progress_targets: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    payload = build_bound_chat_execution_metadata(
        origin_kind=origin_kind,
        origin_surface_kind=origin_surface_kind,
        origin_surface_key=origin_surface_key,
        progress_targets=progress_targets,
    )
    if payload:
        merged.update(payload)
    return merged


def _bound_chat_execution_payload(
    metadata: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    payload = metadata.get(_BOUND_CHAT_EXECUTION_KEY)
    return payload if isinstance(payload, Mapping) else {}


def legacy_bound_chat_origin(client_request_id: Any) -> Optional[tuple[str, str]]:
    normalized_client_request_id = _normalize_optional_text(client_request_id)
    if normalized_client_request_id is None:
        return None
    for surface_kind in _SUPPORTED_SURFACE_KINDS:
        prefix = f"{surface_kind}:"
        if not normalized_client_request_id.lower().startswith(prefix):
            continue
        remainder = normalized_client_request_id[len(prefix) :]
        surface_key, separator, _nonce = remainder.rpartition(":")
        if not separator:
            continue
        normalized_surface_key = _normalize_optional_text(surface_key)
        if normalized_surface_key is None:
            continue
        return surface_kind, normalized_surface_key
    return None


def bound_chat_execution_origin(
    metadata: Mapping[str, Any] | None,
    *,
    client_request_id: Any = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    payload = _bound_chat_execution_payload(metadata)
    origin = payload.get("origin")
    if isinstance(origin, Mapping):
        origin_kind = _normalize_optional_text(origin.get("kind"))
        if origin_kind == "surface":
            surface_kind = _normalize_surface_kind(origin.get("surface_kind"))
            surface_key = _normalize_optional_text(origin.get("surface_key"))
            if surface_kind is not None and surface_key is not None:
                return "surface", surface_kind, surface_key
        elif origin_kind is not None:
            return origin_kind, None, None
    legacy = legacy_bound_chat_origin(client_request_id)
    if legacy is None:
        return None, None, None
    return "surface", legacy[0], legacy[1]


def bound_chat_execution_progress_targets(
    metadata: Mapping[str, Any] | None,
    *,
    client_request_id: Any = None,
) -> tuple[tuple[str, str], ...]:
    payload = _bound_chat_execution_payload(metadata)
    raw_targets = payload.get("progress_targets")
    if isinstance(raw_targets, list):
        normalized_targets = normalize_bound_chat_surface_targets(
            [
                (
                    str(item.get("surface_kind") or ""),
                    str(item.get("surface_key") or ""),
                )
                for item in raw_targets
                if isinstance(item, Mapping)
            ]
        )
        if normalized_targets:
            return normalized_targets
    origin_kind, origin_surface_kind, origin_surface_key = bound_chat_execution_origin(
        metadata,
        client_request_id=client_request_id,
    )
    if (
        origin_kind == "surface"
        and origin_surface_kind is not None
        and origin_surface_key is not None
    ):
        return ((origin_surface_kind, origin_surface_key),)
    return ()


def bound_chat_execution_origin_matches_surface(
    metadata: Mapping[str, Any] | None,
    *,
    surface_kind: str,
    surface_key: str,
    client_request_id: Any = None,
) -> bool:
    origin_kind, origin_surface_kind, origin_surface_key = bound_chat_execution_origin(
        metadata,
        client_request_id=client_request_id,
    )
    return (
        origin_kind == "surface"
        and origin_surface_kind == _normalize_surface_kind(surface_kind)
        and origin_surface_key == _normalize_optional_text(surface_key)
    )


def bound_chat_progress_targets_from_execution_mapping(
    execution: Mapping[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(execution, Mapping):
        return ()
    metadata = execution.get("metadata")
    return bound_chat_execution_progress_targets(
        metadata if isinstance(metadata, Mapping) else None,
        client_request_id=(
            execution.get("client_turn_id") or execution.get("client_request_id")
        ),
    )


def bound_chat_origin_matches_surface_from_execution_mapping(
    execution: Mapping[str, Any] | None,
    *,
    surface_kind: str,
    surface_key: str,
) -> bool:
    if not isinstance(execution, Mapping):
        return False
    metadata = execution.get("metadata")
    return bound_chat_execution_origin_matches_surface(
        metadata if isinstance(metadata, Mapping) else None,
        surface_kind=surface_kind,
        surface_key=surface_key,
        client_request_id=(
            execution.get("client_turn_id") or execution.get("client_request_id")
        ),
    )


def execution_mapping_has_chat_surface_origin(
    execution: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(execution, Mapping):
        return False
    metadata = execution.get("metadata")
    origin_kind, _surface_kind, _surface_key = bound_chat_execution_origin(
        metadata if isinstance(metadata, Mapping) else None,
        client_request_id=(
            execution.get("client_turn_id") or execution.get("client_request_id")
        ),
    )
    return origin_kind == "surface"


__all__ = [
    "bound_chat_execution_origin",
    "bound_chat_execution_origin_matches_surface",
    "bound_chat_execution_progress_targets",
    "bound_chat_origin_matches_surface_from_execution_mapping",
    "bound_chat_progress_targets_from_execution_mapping",
    "build_bound_chat_execution_metadata",
    "execution_mapping_has_chat_surface_origin",
    "legacy_bound_chat_origin",
    "merge_bound_chat_execution_metadata",
    "normalize_bound_chat_surface_targets",
]
