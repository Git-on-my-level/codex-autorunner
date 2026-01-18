import dataclasses
from pathlib import Path
from typing import List, Optional, Tuple

from .bootstrap import seed_repo_files
from .core.config import HubConfig
from .manifest import Manifest, ManifestRepo, load_manifest, save_manifest


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

    def _record_repo(repo_entry: ManifestRepo, *, added: bool) -> None:
        repo_path = (hub_config.root / repo_entry.path).resolve()
        initialized = (repo_path / ".codex-autorunner" / "state.json").exists()
        init_error: Optional[str] = None
        if hub_config.auto_init_missing and repo_path.exists() and not initialized:
            try:
                seed_repo_files(repo_path, force=False, git_required=False)
                initialized = True
            except Exception as exc:  # pragma: no cover - defensive guard
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

            repo_id = child.name
            existing_entry = manifest.get(repo_id)
            added = False
            if not existing_entry:
                # Best-effort grouping inference for worktrees created outside of CAR:
                # name convention: <base_repo_id>--<branch>
                worktree_of: Optional[str] = None
                branch: Optional[str] = None
                if kind == "worktree" and "--" in repo_id:
                    base_id, rest = repo_id.split("--", 1)
                    worktree_of = base_id or None
                    branch = rest or None
                existing_entry = manifest.ensure_repo(
                    hub_config.root,
                    child,
                    repo_id=repo_id,
                    kind=kind,
                    worktree_of=worktree_of,
                    branch=branch,
                )
                added = True
            repo_entry = existing_entry
            _record_repo(repo_entry, added=added)

    _scan_root(hub_config.repos_root, kind="base")
    _scan_root(hub_config.worktrees_root, kind="worktree")

    root_resolved = hub_config.root.resolve()
    root_entry = next(
        (
            entry
            for entry in manifest.repos
            if (hub_config.root / entry.path).resolve() == root_resolved
        ),
        None,
    )
    if root_entry:
        if root_entry.id not in seen_ids:
            _record_repo(root_entry, added=False)
    else:
        state_path = hub_config.root / ".codex-autorunner" / "state.json"
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
                hub_config.root, hub_config.root, repo_id=repo_id, kind="base"
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
                initialized=(repo_path / ".codex-autorunner" / "state.json").exists(),
                init_error=None,
            )
        )

    save_manifest(hub_config.manifest_path, manifest, hub_config.root)
    return manifest, records
