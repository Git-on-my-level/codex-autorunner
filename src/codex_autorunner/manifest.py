import dataclasses
import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

MANIFEST_VERSION = 2


class ManifestError(Exception):
    pass


@dataclasses.dataclass
class ManifestRepo:
    id: str
    path: Path  # relative to hub root
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
        kind: str = "base",
        worktree_of: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> ManifestRepo:
        repo_id = repo_id or repo_path.name
        existing = self.get(repo_id)
        if existing:
            return existing
        normalized_path = _relative_to_hub_root(hub_root, repo_path)
        repo = ManifestRepo(
            id=repo_id,
            path=normalized_path,
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
        data = yaml.safe_load(f) or {}

    version = data.get("version")
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"Unsupported manifest version {version}; expected {MANIFEST_VERSION}"
        )
    repos_data = data.get("repos", []) or []
    repos: List[ManifestRepo] = []
    for entry in repos_data:
        if not isinstance(entry, dict):
            continue
        repo_id = str(entry.get("id"))
        path_val = entry.get("path")
        if not repo_id or not path_val:
            continue
        kind = entry.get("kind")
        if kind not in ("base", "worktree"):
            raise ManifestError(
                f"Invalid manifest repo kind for {repo_id}: {kind} (expected base|worktree)"
            )
        repos.append(
            ManifestRepo(
                id=repo_id,
                path=_relative_to_hub_root(hub_root, hub_root / path_val),
                enabled=bool(entry.get("enabled", True)),
                auto_run=bool(entry.get("auto_run", False)),
                kind=str(kind),
                worktree_of=(
                    str(entry.get("worktree_of")) if entry.get("worktree_of") else None
                ),
                branch=str(entry.get("branch")) if entry.get("branch") else None,
            )
        )
    return Manifest(version=MANIFEST_VERSION, repos=repos)


def save_manifest(manifest_path: Path, manifest: Manifest, hub_root: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "repos": [repo.to_dict(hub_root) for repo in manifest.repos],
    }
    with manifest_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
