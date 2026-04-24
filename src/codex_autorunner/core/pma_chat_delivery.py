"""PMA notification delivery control plane.

This module owns transport-agnostic PMA delivery intent construction. Surface
adapters translate the resulting attempts into Discord or Telegram delivery
operations via the out-of-core runtime registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .chat_bindings import (
    active_chat_binding_metadata_by_thread,
    preferred_non_pma_chat_notification_source_for_workspace,
)
from .config import load_hub_config
from .pma_domain.publish_policy import evaluate_publish_suppression
from .text_utils import _normalize_optional_text, _normalize_pma_delivery_target

logger = logging.getLogger(__name__)


def _notification_context_payload(
    *,
    message: str,
    correlation_id: str,
    source_kind: str,
    context_payload: Optional[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(context_payload or {})
    payload.setdefault("message", message)
    payload.setdefault("correlation_id", correlation_id)
    payload.setdefault("source_kind", source_kind)
    return payload


def _delivery_target_matches_active_thread_binding(
    *,
    hub_root: Path,
    managed_thread_id: Optional[str],
    surface_kind: str,
    surface_key: str,
) -> bool:
    normalized_thread_id = _normalize_optional_text(managed_thread_id)
    if normalized_thread_id is None:
        return True
    binding_metadata = active_chat_binding_metadata_by_thread(hub_root=hub_root).get(
        normalized_thread_id
    )
    if not isinstance(binding_metadata, dict):
        return False
    return (
        _normalize_optional_text(binding_metadata.get("binding_kind")) == surface_kind
        and _normalize_optional_text(binding_metadata.get("binding_id")) == surface_key
    )


@dataclass(frozen=True)
class PmaChatDeliveryTarget:
    """Surface target selected by control-plane routing."""

    surface_kind: str
    surface_key: Optional[str] = None


@dataclass(frozen=True)
class PmaChatDeliveryAttempt:
    """One adapter delivery attempt emitted by the PMA control plane."""

    route: str
    delivery_mode: str
    target: PmaChatDeliveryTarget
    repo_id: Optional[str] = None
    workspace_root: Optional[Path] = None


@dataclass(frozen=True)
class PmaChatDeliveryIntent:
    """Transport-agnostic PMA notification delivery intent."""

    message: str
    correlation_id: str
    source_kind: str
    requested_delivery: str
    attempts: tuple[PmaChatDeliveryAttempt, ...]
    repo_id: Optional[str] = None
    workspace_root: Optional[Path] = None
    run_id: Optional[str] = None
    managed_thread_id: Optional[str] = None
    context_payload: Mapping[str, Any] = field(default_factory=dict)


def _ordered_surface_kinds_for_bound_delivery(
    *,
    hub_root: Path,
    raw_config: dict[str, Any],
    workspace_root: Path,
) -> tuple[str, ...]:
    preferred_source = preferred_non_pma_chat_notification_source_for_workspace(
        hub_root=hub_root,
        raw_config=raw_config,
        workspace_root=workspace_root,
    )
    if preferred_source in {"discord", "telegram"}:
        ordered_sources = [preferred_source]
        ordered_sources.extend(
            source for source in ("discord", "telegram") if source != preferred_source
        )
        return tuple(ordered_sources)
    return ("discord", "telegram")


def _attempts_from_dispatch_decision(
    *,
    dispatch_decision: dict[str, Any],
    normalized_repo_id: Optional[str],
) -> list[PmaChatDeliveryAttempt]:
    if dispatch_decision.get("suppress_publish"):
        return []
    raw_attempts = dispatch_decision.get("attempts")
    if not isinstance(raw_attempts, (list, tuple)):
        return []
    attempts: list[PmaChatDeliveryAttempt] = []
    for entry in raw_attempts:
        if not isinstance(entry, dict):
            continue
        surface_kind = _normalize_optional_text(entry.get("surface_kind"))
        if surface_kind is None:
            continue
        surface_key = _normalize_optional_text(entry.get("surface_key"))
        repo_id = _normalize_optional_text(entry.get("repo_id")) or normalized_repo_id
        workspace_root_raw = _normalize_optional_text(entry.get("workspace_root"))
        attempts.append(
            PmaChatDeliveryAttempt(
                route=_normalize_optional_text(entry.get("route")) or "auto",
                delivery_mode=(
                    _normalize_optional_text(entry.get("delivery_mode")) or "bound"
                ),
                target=PmaChatDeliveryTarget(
                    surface_kind=surface_kind,
                    surface_key=surface_key,
                ),
                repo_id=repo_id,
                workspace_root=(
                    Path(workspace_root_raw) if workspace_root_raw else None
                ),
            )
        )
    return attempts


async def deliver_pma_notification(
    *,
    hub_root: Path,
    message: str,
    correlation_id: str,
    delivery: str,
    source_kind: str,
    repo_id: Optional[str] = None,
    workspace_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    delivery_target: Optional[dict[str, Any]] = None,
    context_payload: Optional[dict[str, Any]] = None,
    dispatch_decision: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    text = str(message or "").strip()
    normalized_repo_id = _normalize_optional_text(repo_id)
    normalized_source_kind = _normalize_optional_text(source_kind) or "automation"
    normalized_delivery = (_normalize_optional_text(delivery) or "auto").lower()
    if not text:
        return {"route": normalized_delivery, "targets": 0, "published": 0}
    try:
        raw_config = load_hub_config(hub_root).raw
    except (OSError, ValueError):
        raw_config = {}
    payload = _notification_context_payload(
        message=text,
        correlation_id=correlation_id,
        source_kind=normalized_source_kind,
        context_payload=context_payload,
    )
    if normalized_delivery == "none":
        return {"route": "none", "targets": 0, "published": 0}

    if dispatch_decision is not None:
        persisted_attempts = _attempts_from_dispatch_decision(
            dispatch_decision=dispatch_decision,
            normalized_repo_id=normalized_repo_id,
        )
        normalized_target = _normalize_pma_delivery_target(delivery_target)
        if normalized_target is not None:
            surface_kind, surface_key = normalized_target
            target_matches_binding = _delivery_target_matches_active_thread_binding(
                hub_root=hub_root,
                managed_thread_id=managed_thread_id,
                surface_kind=surface_kind,
                surface_key=surface_key,
            )
            suppression = evaluate_publish_suppression(
                source_kind=normalized_source_kind,
                message_text=text,
                managed_thread_id=managed_thread_id,
                target_matches_thread_binding=target_matches_binding,
            )
            if suppression.suppressed:
                return {
                    "route": "suppressed_duplicate",
                    "targets": 1,
                    "published": 0,
                }
        if not persisted_attempts:
            return {"route": normalized_delivery, "targets": 0, "published": 0}
        from ..pma_chat_delivery_runtime import dispatch_pma_chat_delivery_intent

        intent = PmaChatDeliveryIntent(
            message=text,
            correlation_id=correlation_id,
            source_kind=normalized_source_kind,
            requested_delivery=normalized_delivery,
            attempts=tuple(persisted_attempts),
            repo_id=normalized_repo_id,
            workspace_root=workspace_root,
            run_id=run_id,
            managed_thread_id=managed_thread_id,
            context_payload=payload,
        )
        return await dispatch_pma_chat_delivery_intent(
            hub_root=hub_root,
            raw_config=raw_config,
            intent=intent,
        )

    attempts: list[PmaChatDeliveryAttempt] = []
    normalized_target = _normalize_pma_delivery_target(delivery_target)
    if normalized_target is not None:
        surface_kind, surface_key = normalized_target
        target_matches_active_binding = _delivery_target_matches_active_thread_binding(
            hub_root=hub_root,
            managed_thread_id=managed_thread_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        suppression = evaluate_publish_suppression(
            source_kind=normalized_source_kind,
            message_text=text,
            managed_thread_id=managed_thread_id,
            target_matches_thread_binding=target_matches_active_binding,
        )
        if suppression.suppressed:
            return {"route": "suppressed_duplicate", "targets": 1, "published": 0}
        if target_matches_active_binding:
            attempts.append(
                PmaChatDeliveryAttempt(
                    route="explicit",
                    delivery_mode="bound",
                    target=PmaChatDeliveryTarget(
                        surface_kind=surface_kind,
                        surface_key=surface_key,
                    ),
                    repo_id=normalized_repo_id,
                )
            )

    if normalized_delivery in {"auto", "primary_pma"} and normalized_repo_id:
        attempts.extend(
            PmaChatDeliveryAttempt(
                route="primary_pma",
                delivery_mode="primary_pma",
                target=PmaChatDeliveryTarget(surface_kind=surface_kind),
                repo_id=normalized_repo_id,
            )
            for surface_kind in ("discord", "telegram")
        )
        if normalized_delivery == "primary_pma" and attempts:
            from ..pma_chat_delivery_runtime import (
                dispatch_pma_chat_delivery_intent,
            )

            return await dispatch_pma_chat_delivery_intent(
                hub_root=hub_root,
                raw_config=raw_config,
                intent=PmaChatDeliveryIntent(
                    message=text,
                    correlation_id=correlation_id,
                    source_kind=normalized_source_kind,
                    requested_delivery=normalized_delivery,
                    attempts=tuple(attempts),
                    repo_id=normalized_repo_id,
                    workspace_root=workspace_root,
                    run_id=run_id,
                    managed_thread_id=managed_thread_id,
                    context_payload=payload,
                ),
            )

    if normalized_delivery in {"auto", "bound"} and workspace_root is not None:
        attempts.extend(
            PmaChatDeliveryAttempt(
                route="bound",
                delivery_mode="bound",
                target=PmaChatDeliveryTarget(surface_kind=surface_kind),
                repo_id=normalized_repo_id,
                workspace_root=workspace_root,
            )
            for surface_kind in _ordered_surface_kinds_for_bound_delivery(
                hub_root=hub_root,
                raw_config=raw_config,
                workspace_root=workspace_root,
            )
        )

    if not attempts:
        return {"route": normalized_delivery, "targets": 0, "published": 0}

    from ..pma_chat_delivery_runtime import dispatch_pma_chat_delivery_intent

    intent = PmaChatDeliveryIntent(
        message=text,
        correlation_id=correlation_id,
        source_kind=normalized_source_kind,
        requested_delivery=normalized_delivery,
        attempts=tuple(attempts),
        repo_id=normalized_repo_id,
        workspace_root=workspace_root,
        run_id=run_id,
        managed_thread_id=managed_thread_id,
        context_payload=payload,
    )
    return await dispatch_pma_chat_delivery_intent(
        hub_root=hub_root,
        raw_config=raw_config,
        intent=intent,
    )


async def notify_preferred_bound_chat_for_workspace(
    *,
    hub_root: Path,
    workspace_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
    source_kind: str = "notice",
    run_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    context_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return await deliver_pma_notification(
        hub_root=hub_root,
        workspace_root=workspace_root,
        repo_id=repo_id,
        message=message,
        correlation_id=correlation_id,
        delivery="bound",
        source_kind=source_kind,
        run_id=run_id,
        managed_thread_id=managed_thread_id,
        context_payload=context_payload,
    )


async def notify_primary_pma_chat_for_repo(
    *,
    hub_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
    source_kind: str = "notice",
    workspace_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    context_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return await deliver_pma_notification(
        hub_root=hub_root,
        workspace_root=workspace_root,
        repo_id=repo_id,
        message=message,
        correlation_id=correlation_id,
        delivery="primary_pma",
        source_kind=source_kind,
        run_id=run_id,
        managed_thread_id=managed_thread_id,
        context_payload=context_payload,
    )


__all__ = [
    "PmaChatDeliveryAttempt",
    "PmaChatDeliveryIntent",
    "PmaChatDeliveryTarget",
    "deliver_pma_notification",
    "notify_preferred_bound_chat_for_workspace",
    "notify_primary_pma_chat_for_repo",
]
