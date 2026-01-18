import dataclasses
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import yaml

from .core.repo_ids import ensure_unique_repo_id, repo_id_is_safe, sanitize_repo_id

MANIFEST_VERSION = 3


class ManifestError(Exception):
    pass


@dataclasses.dataclass
class ManifestRepo:
    id: str
    path: Path  # relative to hub root
    display_name: str = ""
    enabled: bool = True
    auto_run: bool = False
    kind: str = "base"  # base|worktree
    worktree_of: Optional[str] = None
    branch: Optional[str] = None

    def to_dict(self, hub_root: Path) -> Dict[str, object]:
        rel = _relative_to_hub_root(hub_root, self.path)
        payload: Dict[str, object] = {
            "id": self.id,
            "path": rel.as_posix(),
            "display_name": self.display_name or self.id,
            "enabled": bool(self.enabled),
            "auto_run": bool(self.auto_run),
            "kind": str(self.kind),
        }
        if self.worktree_of:
            payload["worktree_of"] = str(self.worktree_of)
        if self.branch:
            payload["branch"] = str(self.branch)
        return payload


@dataclasses.dataclass
class Manifest:
    version: int
    repos: List[ManifestRepo]

    def get(self, repo_id: str) -> Optional[ManifestRepo]:
        for repo in self.repos:
            if repo.id == repo_id:
                return repo
        return None

    def ensure_repo(
        self,
        hub_root: Path,
        repo_path: Path,
        repo_id: Optional[str] = None,
        *,
        display_name: Optional[str] = None,
        kind: str = "base",
        worktree_of: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> ManifestRepo:
        repo_id = repo_id or repo_path.name
        existing = self.get(repo_id)
        if existing:
            if display_name and existing.display_name != display_name:
                existing.display_name = display_name
            return existing
        normalized_path = _relative_to_hub_root(hub_root, repo_path)
        repo = ManifestRepo(
            id=repo_id,
            path=normalized_path,
            display_name=display_name or repo_id,
            enabled=True,
            auto_run=False,
            kind=str(kind),
            worktree_of=str(worktree_of) if worktree_of else None,
            branch=str(branch) if branch else None,
        )
        self.repos.append(repo)
        return repo


def _relative_to_hub_root(hub_root: Path, target: Path) -> Path:
    if not target.is_absolute():
        target = (hub_root / target).resolve()
    else:
        target = target.resolve()
    try:
        return target.relative_to(hub_root)
    except ValueError:
        return Path(os.path.relpath(target, hub_root))


def load_manifest(manifest_path: Path, hub_root: Path) -> Manifest:
    if not manifest_path.exists():
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = Manifest(version=MANIFEST_VERSION, repos=[])
        save_manifest(manifest_path, manifest, hub_root)
        return manifest

    with manifest_path.open("r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f) or {}
    if not isinstance(raw_data, dict):
        raw_data = {}
    data = cast(Dict[str, Any], raw_data)

    version = data.get("version")
    if version not in (2, MANIFEST_VERSION):
        raise ManifestError(
            f"Unsupported manifest version {version}; expected {MANIFEST_VERSION}"
        )
    repos_data = data.get("repos", []) or []
    if not isinstance(repos_data, list):
        repos_data = []
    repos: List[ManifestRepo] = []
    for entry in repos_data:
        if not isinstance(entry, dict):
            continue
        repo_id = entry.get("id")
        path_val = entry.get("path")
        if not isinstance(repo_id, str) or not repo_id:
            continue
        if not isinstance(path_val, str) or not path_val:
            continue
        display_name = entry.get("display_name") or entry.get("displayName")
        if not isinstance(display_name, str) or not display_name:
            display_name = repo_id
        kind = entry.get("kind")
        if kind not in ("base", "worktree"):
            raise ManifestError(
                f"Invalid manifest repo kind for {repo_id}: {kind} (expected base|worktree)"
            )
        repos.append(
            ManifestRepo(
                id=repo_id,
                path=_relative_to_hub_root(hub_root, hub_root / path_val),
                display_name=display_name,
                enabled=bool(entry.get("enabled", True)),
                auto_run=bool(entry.get("auto_run", False)),
                kind=str(kind),
                worktree_of=(
                    str(entry.get("worktree_of")) if entry.get("worktree_of") else None
                ),
                branch=str(entry.get("branch")) if entry.get("branch") else None,
            )
        )
    manifest, changed = _normalize_manifest(Manifest(version=version, repos=repos))
    if changed:
        save_manifest(manifest_path, manifest, hub_root)
    return manifest


def save_manifest(manifest_path: Path, manifest: Manifest, hub_root: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "repos": [repo.to_dict(hub_root) for repo in manifest.repos],
    }
    with manifest_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False)


def _normalize_manifest(manifest: Manifest) -> tuple[Manifest, bool]:
    existing_ids: set[str] = set()
    id_map: Dict[str, str] = {}
    normalized: List[ManifestRepo] = []
    changed = manifest.version != MANIFEST_VERSION

    for repo in manifest.repos:
        display_name = repo.display_name or repo.id
        candidate = repo.id
        if not repo_id_is_safe(repo.id):
            candidate = sanitize_repo_id(display_name)
        unique_id = ensure_unique_repo_id(candidate, existing_ids)
        if unique_id != repo.id:
            id_map[repo.id] = unique_id
            changed = True
        if display_name != repo.display_name:
            changed = True
        normalized.append(
            ManifestRepo(
                id=unique_id,
                path=repo.path,
                display_name=display_name,
                enabled=repo.enabled,
                auto_run=repo.auto_run,
                kind=repo.kind,
                worktree_of=repo.worktree_of,
                branch=repo.branch,
            )
        )

    if id_map:
        for repo in normalized:
            if repo.worktree_of and repo.worktree_of in id_map:
                repo.worktree_of = id_map[repo.worktree_of]
                changed = True

    return Manifest(version=MANIFEST_VERSION, repos=normalized), changed
