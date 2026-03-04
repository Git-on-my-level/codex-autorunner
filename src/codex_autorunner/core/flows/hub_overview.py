from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...manifest import Manifest
from ..chat_bindings import active_chat_binding_counts


@dataclass(frozen=True)
class HubFlowOverviewEntry:
    repo_id: str
    repo_root: Path
    label: str
    indent: str
    group: str
    unregistered: bool = False


def _worktree_suffix(repo_id: str) -> str | None:
    parts = [part for part in repo_id.split("--") if part]
    if len(parts) <= 1:
        return None
    return parts[-1]


def _is_manifest_worktree(repo: object) -> bool:
    kind = str(getattr(repo, "kind", "") or "").strip().lower()
    if kind == "worktree":
        return True
    if kind == "base":
        return False
    worktree_of = getattr(repo, "worktree_of", None)
    if isinstance(worktree_of, str) and worktree_of.strip():
        return True
    # Compatibility fallback for manifests that may omit explicit kind/worktree_of.
    repo_id = str(getattr(repo, "id", "") or "").strip()
    return kind == "" and "--" in repo_id


def _group_id_for_repo(repo: object) -> str:
    worktree_of = getattr(repo, "worktree_of", None)
    if isinstance(worktree_of, str) and worktree_of.strip():
        return worktree_of.strip()
    repo_id = str(getattr(repo, "id", "") or "").strip()
    if "--" in repo_id:
        return repo_id.split("--", 1)[0]
    return repo_id


def _group_id_for_repo_id(repo_id: str) -> str:
    if "--" in repo_id:
        return repo_id.split("--", 1)[0]
    return repo_id


def _resolve_worktrees_root(hub_root: Path, raw_config: Mapping[str, Any]) -> Path:
    hub_cfg = raw_config.get("hub")
    worktrees_root_raw = None
    if isinstance(hub_cfg, Mapping):
        worktrees_root_raw = hub_cfg.get("worktrees_root")
    if not isinstance(worktrees_root_raw, str) or not worktrees_root_raw.strip():
        worktrees_root_raw = "worktrees"
    worktrees_root = Path(worktrees_root_raw)
    if not worktrees_root.is_absolute():
        worktrees_root = (hub_root / worktrees_root).resolve()
    return worktrees_root


def build_hub_flow_overview_entries(
    *,
    hub_root: Path,
    manifest: Manifest,
    raw_config: Mapping[str, Any],
) -> list[HubFlowOverviewEntry]:
    chat_binding_counts: Mapping[str, int] | None
    try:
        chat_binding_counts = active_chat_binding_counts(
            hub_root=hub_root, raw_config=raw_config
        )
    except Exception:
        # Fail open on binding lookup errors so flow status remains visible.
        chat_binding_counts = None

    manifest_repo_ids = {
        str(getattr(repo, "id", "") or "").strip() for repo in manifest.repos
    }
    manifest_worktree_ids = {
        str(getattr(repo, "id", "") or "").strip()
        for repo in manifest.repos
        if _is_manifest_worktree(repo)
    }

    binding_lookup_failed = chat_binding_counts is None
    active_worktree_repo_ids: set[str] = set()
    if chat_binding_counts is not None:
        for raw_repo_id, raw_count in chat_binding_counts.items():
            if not isinstance(raw_repo_id, str):
                continue
            repo_id = raw_repo_id.strip()
            if not repo_id:
                continue
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if repo_id in manifest_worktree_ids or "--" in repo_id:
                active_worktree_repo_ids.add(repo_id)

    entries: list[HubFlowOverviewEntry] = []
    for repo in manifest.repos:
        if not getattr(repo, "enabled", True):
            continue
        repo_id = str(getattr(repo, "id", "") or "").strip()
        if not repo_id:
            continue
        is_worktree = _is_manifest_worktree(repo)
        if (
            is_worktree
            and not binding_lookup_failed
            and repo_id not in active_worktree_repo_ids
        ):
            continue
        repo_root = (hub_root / repo.path).resolve()
        label = _worktree_suffix(repo_id) if is_worktree else None
        if not label:
            label = repo_id
        entries.append(
            HubFlowOverviewEntry(
                repo_id=repo_id,
                repo_root=repo_root,
                label=label,
                indent="  - " if is_worktree else "",
                group=_group_id_for_repo(repo),
                unregistered=False,
            )
        )

    worktrees_root = _resolve_worktrees_root(hub_root, raw_config)
    if binding_lookup_failed:
        return entries
    for repo_id in sorted(active_worktree_repo_ids):
        if repo_id in manifest_repo_ids:
            continue
        repo_root = worktrees_root / repo_id
        if not repo_root.exists() or not repo_root.is_dir():
            continue
        flows_root = repo_root / ".codex-autorunner" / "flows"
        flows_db = repo_root / ".codex-autorunner" / "flows.db"
        if not flows_root.exists() and not flows_db.exists():
            continue
        suffix = _worktree_suffix(repo_id)
        label = f"{suffix or repo_id} (unregistered)"
        entries.append(
            HubFlowOverviewEntry(
                repo_id=repo_id,
                repo_root=repo_root.resolve(),
                label=label,
                indent="  - ",
                group=_group_id_for_repo_id(repo_id),
                unregistered=True,
            )
        )

    return entries


__all__ = ["HubFlowOverviewEntry", "build_hub_flow_overview_entries"]
