import dataclasses
from pathlib import Path
from typing import List, Optional, Tuple

from .bootstrap import seed_repo_files
from .core.config import HubConfig
from .core.git_utils import git_branch, git_linked_worktree_base_root
from .manifest import (
    Manifest,
    ManifestRepo,
    ensure_unique_repo_id,
    is_safe_repo_id,
    load_manifest,
    match_base_repo_id,
    sanitize_repo_id,
    save_manifest,
)


@dataclasses.dataclass
class DiscoveryRecord:
    repo: ManifestRepo
    absolute_path: Path
    added_to_manifest: bool
    exists_on_disk: bool
    initialized: bool
    init_error: Optional[str] = None


def discover_and_init(hub_config: HubConfig) -> Tuple[Manifest, List[DiscoveryRecord]]:
    """
    Perform a shallow scan (depth=1) for git repos, update the manifest,
    and auto-init missing .codex-autorunner directories when enabled.
    """
    manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    records: List[DiscoveryRecord] = []
    seen_ids: set[str] = set()

    def _infer_repo_shape(
        repo_path: Path,
        *,
        display_name: str,
        default_kind: str,
    ) -> tuple[str, Optional[str], Optional[str]]:
        branch = git_branch(repo_path)
        branch_from_name: Optional[str] = None
        if "--" in display_name:
            _, branch_suffix = display_name.split("--", 1)
            branch_from_name = branch_suffix or None

        base_path = git_linked_worktree_base_root(repo_path)
        if base_path is None and default_kind != "worktree":
            return default_kind, None, branch or branch_from_name

        kind = (
            "worktree"
            if base_path is not None or default_kind == "worktree"
            else default_kind
        )
        base_name: Optional[str] = None
        if base_path is not None:
            base_name = base_path.name
        elif "--" in display_name:
            base_name = display_name.split("--", 1)[0]

        worktree_of = None
        if kind == "worktree" and base_name:
            worktree_of = match_base_repo_id(
                manifest, hub_config.root, base_name, base_path=base_path
            )
        return kind, worktree_of, branch or branch_from_name

    def _normalize_manifest_ids() -> None:
        reserved_ids = {repo.id for repo in manifest.repos}
        id_map: dict[str, str] = {}
        for repo in manifest.repos:
            display_name = repo.display_name or repo.id
            if not repo.display_name:
                repo.display_name = display_name
            if is_safe_repo_id(repo.id):
                continue
            reserved_ids.discard(repo.id)
            safe_id = ensure_unique_repo_id(
                sanitize_repo_id(display_name), reserved_ids
            )
            reserved_ids.add(safe_id)
            id_map[repo.id] = safe_id
            repo.id = safe_id
        if id_map:
            for repo in manifest.repos:
                if repo.worktree_of in id_map:
                    repo.worktree_of = id_map[repo.worktree_of]

    def _record_repo(repo_entry: ManifestRepo, *, added: bool) -> None:
        repo_path = (hub_config.root / repo_entry.path).resolve()
        initialized = (repo_path / ".codex-autorunner" / "tickets").exists()
        init_error: Optional[str] = None
        if hub_config.auto_init_missing and repo_path.exists() and not initialized:
            try:
                seed_repo_files(repo_path, force=False, git_required=False)
                initialized = True
            except (OSError, ValueError) as exc:  # pragma: no cover - defensive guard
                init_error = str(exc)

        records.append(
            DiscoveryRecord(
                repo=repo_entry,
                absolute_path=repo_path,
                added_to_manifest=added,
                exists_on_disk=repo_path.exists(),
                initialized=initialized,
                init_error=init_error,
            )
        )
        seen_ids.add(repo_entry.id)

    def _scan_root(root: Path, *, kind: str) -> None:
        if not root.exists():
            return
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if not (child / ".git").exists():
                continue

            display_name = child.name
            existing_entry = manifest.get_by_path(hub_config.root, child)
            if not existing_entry:
                existing_entry = manifest.get(display_name)
            detected_kind, detected_worktree_of, detected_branch = _infer_repo_shape(
                child,
                display_name=display_name,
                default_kind=kind,
            )
            added = False
            if not existing_entry:
                existing_entry = manifest.ensure_repo(
                    hub_config.root,
                    child,
                    repo_id=display_name,
                    display_name=display_name,
                    kind=detected_kind,
                    worktree_of=detected_worktree_of,
                    branch=detected_branch,
                )
                added = True
            if existing_entry.display_name is None:
                existing_entry.display_name = display_name
            if detected_kind == "worktree":
                existing_entry.kind = "worktree"
                if detected_worktree_of:
                    existing_entry.worktree_of = detected_worktree_of
                if detected_branch:
                    existing_entry.branch = detected_branch
            repo_entry = existing_entry
            _record_repo(repo_entry, added=added)

    _normalize_manifest_ids()
    _scan_root(hub_config.repos_root, kind="base")
    _scan_root(hub_config.worktrees_root, kind="worktree")

    root_resolved = hub_config.root.resolve()
    root_entries = [
        entry
        for entry in manifest.repos
        if (hub_config.root / entry.path).resolve() == root_resolved
    ]
    root_entry = root_entries[0] if root_entries else None
    if not hub_config.include_root_repo:
        if root_entries:
            root_ids = {entry.id for entry in root_entries}
            manifest.repos = [
                entry for entry in manifest.repos if entry.id not in root_ids
            ]
            seen_ids.difference_update(root_ids)
    elif root_entry:
        if root_entry.id not in seen_ids:
            _record_repo(root_entry, added=False)
    else:
        state_path = hub_config.root / ".codex-autorunner" / "state.sqlite3"
        root_is_repo = state_path.exists()
        if not root_is_repo and hub_config.repos_root.resolve() == root_resolved:
            root_is_repo = (hub_config.root / ".git").exists()
        if root_is_repo:
            repo_id_base = hub_config.root.name or "root"
            repo_id = repo_id_base
            suffix = 0
            existing_ids = {repo.id for repo in manifest.repos}
            while repo_id in existing_ids:
                suffix += 1
                if suffix == 1:
                    repo_id = f"{repo_id_base}-root"
                else:
                    repo_id = f"{repo_id_base}-root-{suffix}"
            root_entry = manifest.ensure_repo(
                hub_config.root,
                hub_config.root,
                repo_id=repo_id,
                display_name=hub_config.root.name or repo_id,
                kind="base",
            )
            _record_repo(root_entry, added=True)

    for entry in manifest.repos:
        if entry.id in seen_ids:
            continue
        repo_path = (hub_config.root / entry.path).resolve()
        records.append(
            DiscoveryRecord(
                repo=entry,
                absolute_path=repo_path,
                added_to_manifest=False,
                exists_on_disk=repo_path.exists(),
                initialized=(repo_path / ".codex-autorunner" / "tickets").exists(),
                init_error=None,
            )
        )

    save_manifest(hub_config.manifest_path, manifest, hub_config.root)
    return manifest, records
