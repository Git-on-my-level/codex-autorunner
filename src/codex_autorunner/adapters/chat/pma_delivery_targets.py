"""Shared PMA chat-surface delivery target projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ...core.chat_bindings import normalize_workspace_path, resolve_bound_repo_id
from ...core.text_utils import _normalize_optional_text


@dataclass(frozen=True)
class PmaChatSurfaceBinding:
    surface_key: str
    workspace_path: str | None = None
    repo_id: str | None = None
    is_primary_pma: bool = False
    previous_workspace_path: str | None = None
    previous_repo_id: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class PmaChatDeliveryTargetCandidate:
    surface_key: str
    workspace_root: str | None = None


def select_explicit_pma_target(
    *,
    surface_key: str | None,
    bindings: tuple[PmaChatSurfaceBinding, ...],
) -> PmaChatDeliveryTargetCandidate | None:
    normalized_surface_key = _normalize_optional_text(surface_key)
    if normalized_surface_key is None:
        return None
    for binding in bindings:
        if binding.surface_key == normalized_surface_key:
            return PmaChatDeliveryTargetCandidate(surface_key=normalized_surface_key)
    return None


def select_bound_pma_targets(
    *,
    workspace_root: str | None,
    repo_id: str | None,
    bindings: tuple[PmaChatSurfaceBinding, ...],
    repo_id_by_workspace: Mapping[str, str],
) -> tuple[PmaChatDeliveryTargetCandidate, ...]:
    normalized_workspace_root = normalize_workspace_path(workspace_root)
    if normalized_workspace_root is None:
        return ()
    normalized_repo_id = _normalize_optional_text(repo_id)
    seen_surface_keys: set[str] = set()
    candidates: list[PmaChatDeliveryTargetCandidate] = []
    for binding in sorted(bindings, key=lambda item: item.surface_key):
        if binding.is_primary_pma:
            continue
        if (
            normalize_workspace_path(binding.workspace_path)
            != normalized_workspace_root
        ):
            continue
        binding_repo_id = resolve_bound_repo_id(
            repo_id=binding.repo_id,
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(binding.workspace_path,),
        )
        if normalized_repo_id and binding_repo_id not in {None, normalized_repo_id}:
            continue
        if binding.surface_key in seen_surface_keys:
            continue
        seen_surface_keys.add(binding.surface_key)
        candidates.append(
            PmaChatDeliveryTargetCandidate(
                surface_key=binding.surface_key,
                workspace_root=workspace_root,
            )
        )
    return tuple(candidates)


def select_primary_pma_target(
    *,
    repo_id: str | None,
    bindings: tuple[PmaChatSurfaceBinding, ...],
    repo_id_by_workspace: Mapping[str, str],
) -> PmaChatDeliveryTargetCandidate | None:
    normalized_repo_id = _normalize_optional_text(repo_id)
    if normalized_repo_id is None:
        return None
    candidates: list[tuple[str, str, PmaChatDeliveryTargetCandidate]] = []
    for binding in bindings:
        if not binding.is_primary_pma:
            continue
        binding_repo_id = resolve_bound_repo_id(
            repo_id=binding.repo_id,
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(binding.workspace_path,),
        )
        previous_binding_repo_id = resolve_bound_repo_id(
            repo_id=binding.previous_repo_id,
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(binding.previous_workspace_path,),
        )
        previous_repo_id = _normalize_optional_text(binding.previous_repo_id)
        if normalized_repo_id not in {
            binding_repo_id,
            previous_repo_id,
            previous_binding_repo_id,
        }:
            continue
        workspace_root = _normalize_optional_text(
            binding.previous_workspace_path or binding.workspace_path
        )
        candidates.append(
            (
                _normalize_optional_text(binding.updated_at) or "",
                binding.surface_key,
                PmaChatDeliveryTargetCandidate(
                    surface_key=binding.surface_key,
                    workspace_root=workspace_root,
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


__all__ = [
    "PmaChatDeliveryTargetCandidate",
    "PmaChatSurfaceBinding",
    "select_bound_pma_targets",
    "select_explicit_pma_target",
    "select_primary_pma_target",
]
