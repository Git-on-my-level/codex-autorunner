"""PMA dispatch decision builder (thin adapter over domain types).

Canonical domain types live in ``pma_domain.models`` and
``pma_domain.serialization``.  This module keeps the builder function and
binding-matching helpers that require live adapter data, but delegates all
type definitions, normalization, and serialization to the domain package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .pma_domain.models import (
    PmaDispatchAttempt,
    PmaDispatchDecision,
)
from .pma_domain.serialization import (
    normalize_pma_dispatch_decision as _domain_normalize,
)
from .pma_domain.serialization import (
    pma_dispatch_decision_to_dict as _domain_to_dict,
)
from .pma_origin import extract_pma_origin_metadata
from .text_utils import _normalize_optional_text, _normalize_pma_delivery_target

PmaDispatchAttemptSpec = PmaDispatchAttempt


def normalize_pma_dispatch_decision(value: Any) -> Optional[PmaDispatchDecision]:
    return _domain_normalize(value)


def pma_dispatch_decision_to_dict(decision: PmaDispatchDecision) -> dict[str, Any]:
    return _domain_to_dict(decision)


def _thread_binding_matches(
    *,
    binding_metadata_by_thread: Mapping[str, Mapping[str, Any]],
    thread_id: Optional[str],
    surface_kind: str,
    surface_key: str,
) -> bool:
    normalized_thread_id = _normalize_optional_text(thread_id)
    if normalized_thread_id is None:
        return True
    binding_metadata = binding_metadata_by_thread.get(normalized_thread_id)
    if not isinstance(binding_metadata, Mapping):
        return False
    return (
        _normalize_optional_text(binding_metadata.get("binding_kind")) == surface_kind
        and _normalize_optional_text(binding_metadata.get("binding_id")) == surface_key
    )


def _origin_thread_ids_from_payload(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    thread_ids: list[str] = []

    def _append(candidate: Any) -> None:
        normalized = _normalize_optional_text(candidate)
        if normalized is not None and normalized not in thread_ids:
            thread_ids.append(normalized)

    _append(payload.get("origin_thread_id"))
    metadata = payload.get("metadata")
    origin = extract_pma_origin_metadata(
        metadata if isinstance(metadata, Mapping) else None
    )
    if origin is not None:
        _append(origin.thread_id)
    return tuple(thread_ids)


def _explicit_delivery_target_thread_ids(
    *,
    managed_thread_id: Optional[str],
    context_payload: Optional[Mapping[str, Any]],
) -> tuple[str, ...]:
    thread_ids: list[str] = []

    def _append(candidate: Any) -> None:
        normalized = _normalize_optional_text(candidate)
        if normalized is not None and normalized not in thread_ids:
            thread_ids.append(normalized)

    _append(managed_thread_id)
    if isinstance(context_payload, Mapping):
        for candidate in _origin_thread_ids_from_payload(context_payload):
            _append(candidate)
        wake_up = context_payload.get("wake_up")
        if isinstance(wake_up, Mapping):
            for candidate in _origin_thread_ids_from_payload(wake_up):
                _append(candidate)
    return tuple(thread_ids)


def _delivery_target_matches_any_thread_binding(
    *,
    binding_metadata_by_thread: Mapping[str, Mapping[str, Any]],
    thread_ids: tuple[str, ...],
    surface_kind: str,
    surface_key: str,
) -> bool:
    if not thread_ids:
        return True
    return any(
        _thread_binding_matches(
            binding_metadata_by_thread=binding_metadata_by_thread,
            thread_id=thread_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        for thread_id in thread_ids
    )


def _delivery_target_from_lane_id(lane_id: Any) -> Optional[tuple[str, str]]:
    normalized_lane_id = _normalize_optional_text(lane_id)
    if normalized_lane_id is None:
        return None
    surface_kind_raw, separator, surface_key_raw = normalized_lane_id.partition(":")
    if not separator:
        return None
    surface_kind = _normalize_optional_text(surface_kind_raw)
    surface_key = _normalize_optional_text(surface_key_raw)
    if surface_kind not in {"discord", "telegram"} or surface_key is None:
        return None
    return surface_kind, surface_key


def _lane_delivery_target_from_context(
    context_payload: Optional[Mapping[str, Any]],
) -> Optional[tuple[str, str]]:
    if not isinstance(context_payload, Mapping):
        return None
    wake_up = context_payload.get("wake_up")
    if isinstance(wake_up, Mapping):
        target = _delivery_target_from_lane_id(wake_up.get("lane_id"))
        if target is not None:
            return target
    return _delivery_target_from_lane_id(context_payload.get("lane_id"))


def build_pma_dispatch_decision(
    *,
    message: str,
    requested_delivery: str,
    source_kind: str,
    repo_id: Optional[str],
    workspace_root: Optional[Path],
    managed_thread_id: Optional[str],
    delivery_target: Optional[dict[str, Any]],
    context_payload: Optional[Mapping[str, Any]],
    binding_metadata_by_thread: Mapping[str, Mapping[str, Any]],
    preferred_bound_surface_kinds: tuple[str, ...] = (),
) -> PmaDispatchDecision:
    from .pma_domain.publish_policy import evaluate_publish_suppression

    normalized_delivery = (
        _normalize_optional_text(requested_delivery) or "auto"
    ).lower()
    normalized_source_kind = _normalize_optional_text(source_kind) or "automation"
    normalized_repo_id = _normalize_optional_text(repo_id)

    if normalized_delivery == "none":
        return PmaDispatchDecision(requested_delivery="none")

    attempts: list[PmaDispatchAttempt] = []
    normalized_target = _normalize_pma_delivery_target(delivery_target)
    lane_target = _lane_delivery_target_from_context(context_payload)
    explicit_target = normalized_target or lane_target
    if explicit_target is not None:
        target_requires_known_binding = normalized_target is not None
        surface_kind, surface_key = explicit_target
        explicit_target_thread_ids = _explicit_delivery_target_thread_ids(
            managed_thread_id=managed_thread_id,
            context_payload=context_payload,
        )
        target_matches_managed_thread_binding = _thread_binding_matches(
            binding_metadata_by_thread=binding_metadata_by_thread,
            thread_id=managed_thread_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        target_matches_known_binding = _delivery_target_matches_any_thread_binding(
            binding_metadata_by_thread=binding_metadata_by_thread,
            thread_ids=explicit_target_thread_ids,
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        suppression = evaluate_publish_suppression(
            source_kind=normalized_source_kind,
            message_text=message,
            managed_thread_id=managed_thread_id,
            target_matches_thread_binding=target_matches_managed_thread_binding,
        )
        if suppression.suppressed:
            return PmaDispatchDecision(
                requested_delivery="suppressed_duplicate",
                suppress_publish=True,
            )
        if not target_requires_known_binding or (
            explicit_target_thread_ids and target_matches_known_binding
        ):
            attempts.append(
                PmaDispatchAttempt(
                    route="explicit",
                    delivery_mode="bound",
                    surface_kind=surface_kind,
                    surface_key=surface_key,
                    repo_id=normalized_repo_id,
                )
            )

    if normalized_delivery in {"auto", "primary_pma"} and normalized_repo_id:
        attempts.extend(
            PmaDispatchAttempt(
                route="primary_pma",
                delivery_mode="primary_pma",
                surface_kind=surface_kind,
                repo_id=normalized_repo_id,
            )
            for surface_kind in ("discord", "telegram")
        )

    if normalized_delivery in {"auto", "bound"} and workspace_root is not None:
        attempts.extend(
            PmaDispatchAttempt(
                route="bound",
                delivery_mode="bound",
                surface_kind=surface_kind,
                repo_id=normalized_repo_id,
                workspace_root=str(workspace_root),
            )
            for surface_kind in preferred_bound_surface_kinds
        )

    return PmaDispatchDecision(
        requested_delivery=normalized_delivery,
        attempts=tuple(attempts),
    )


__all__ = [
    "PmaDispatchAttemptSpec",
    "PmaDispatchDecision",
    "build_pma_dispatch_decision",
    "normalize_pma_dispatch_decision",
    "pma_dispatch_decision_to_dict",
]
