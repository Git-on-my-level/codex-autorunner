from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

import yaml

from ..config import AppRepoConfig
from ..git_utils import GitError, run_git
from ..state_roots import resolve_hub_apps_root
from .manifest import AppManifest, ManifestError, parse_app_manifest
from .paths import AppPathError, validate_app_path
from .refs import AppRef, parse_app_ref


class RepoNotConfiguredError(Exception):
    def __init__(self, repo_id: str, *, detail: Optional[str] = None) -> None:
        message = f"App repo not configured: {repo_id}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.repo_id = repo_id
        self.detail = detail


class AppNotFoundError(Exception):
    def __init__(self, repo_id: str, app_path: str, ref: str) -> None:
        super().__init__(
            f"App not found: repo_id={repo_id} app_path={app_path} ref={ref}"
        )
        self.repo_id = repo_id
        self.app_path = app_path
        self.ref = ref


class RefNotFoundError(Exception):
    def __init__(self, repo_id: str, ref: str) -> None:
        super().__init__(f"Ref not found: repo_id={repo_id} ref={ref}")
        self.repo_id = repo_id
        self.ref = ref


class NetworkUnavailableError(Exception):
    def __init__(
        self,
        repo_id: str,
        ref: str,
        *,
        detail: Optional[str] = None,
    ) -> None:
        message = (
            "App fetch failed and cache is unavailable: " f"repo_id={repo_id} ref={ref}"
        )
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.repo_id = repo_id
        self.ref = ref
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class AppRepoSnapshot:
    repo_id: str
    url: str
    trusted: bool
    ref: str
    commit_sha: str
    git_dir: Path


@dataclasses.dataclass(frozen=True)
class FetchedAppManifest:
    repo_id: str
    url: str
    trusted: bool
    app_path: str
    ref: str
    commit_sha: str
    manifest_blob_sha: str
    manifest_sha: str
    manifest_text: str
    manifest: AppManifest


def ensure_git_mirror(repo: AppRepoConfig, hub_root: Path) -> Path:
    apps_root = resolve_hub_apps_root(hub_root)
    mirror_path = apps_root / "git" / f"{repo.id}.git"
    if mirror_path.exists():
        _ensure_origin_remote(mirror_path, repo.url)
        return mirror_path

    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--bare", str(mirror_path)], mirror_path.parent, check=True)
    _ensure_origin_remote(mirror_path, repo.url)
    return mirror_path


def prepare_repo_snapshot(
    repo: AppRepoConfig,
    hub_root: Path,
    *,
    ref: Optional[str] = None,
    fetch_timeout_seconds: int = 30,
) -> AppRepoSnapshot:
    resolved_ref = (ref or repo.default_ref).strip()
    mirror_path = ensure_git_mirror(repo, hub_root)

    fetch_error: Optional[str] = None
    try:
        run_git(
            ["fetch", "--prune", "origin"],
            mirror_path,
            timeout_seconds=fetch_timeout_seconds,
            check=True,
        )
    except GitError as exc:
        fetch_error = str(exc)

    try:
        commit_sha = _resolve_commit(mirror_path, repo.id, resolved_ref)
    except RefNotFoundError as exc:
        if fetch_error:
            raise NetworkUnavailableError(
                repo.id,
                resolved_ref,
                detail=fetch_error,
            ) from exc
        raise

    return AppRepoSnapshot(
        repo_id=repo.id,
        url=repo.url,
        trusted=repo.trusted,
        ref=resolved_ref,
        commit_sha=commit_sha,
        git_dir=mirror_path,
    )


def list_manifest_paths(snapshot: AppRepoSnapshot) -> Iterator[PurePosixPath]:
    try:
        proc = run_git(
            ["ls-tree", "-r", "--name-only", snapshot.commit_sha],
            snapshot.git_dir,
            check=True,
        )
    except GitError:
        return

    for raw_path in (proc.stdout or "").splitlines():
        normalized = raw_path.strip()
        if not normalized:
            continue
        manifest_path = PurePosixPath(normalized)
        if manifest_path.name == "car-app.yaml":
            yield manifest_path


def fetch_app_manifest(
    *,
    repo: AppRepoConfig,
    hub_root: Path,
    app_ref: str,
    fetch_timeout_seconds: int = 30,
) -> FetchedAppManifest:
    parsed = parse_app_ref(app_ref)
    if parsed.repo_id != repo.id:
        raise RepoNotConfiguredError(
            parsed.repo_id,
            detail=f"expected repo_id {repo.id}",
        )

    snapshot = prepare_repo_snapshot(
        repo,
        hub_root,
        ref=parsed.ref,
        fetch_timeout_seconds=fetch_timeout_seconds,
    )
    return fetch_app_manifest_from_snapshot(snapshot, parsed)


def fetch_app_manifest_from_snapshot(
    snapshot: AppRepoSnapshot,
    app_ref: AppRef,
) -> FetchedAppManifest:
    if app_ref.repo_id != snapshot.repo_id:
        raise RepoNotConfiguredError(
            app_ref.repo_id,
            detail=f"expected repo_id {snapshot.repo_id}",
        )
    try:
        app_root = validate_app_path(app_ref.app_path)
    except AppPathError as exc:
        raise ValueError(str(exc)) from exc
    manifest_path = app_root / "car-app.yaml"
    return fetch_manifest_by_path(snapshot, manifest_path)


def fetch_manifest_by_path(
    snapshot: AppRepoSnapshot,
    manifest_path: PurePosixPath,
) -> FetchedAppManifest:
    manifest_blob_sha = _resolve_blob(
        snapshot.git_dir,
        snapshot.commit_sha,
        str(manifest_path),
        snapshot.repo_id,
        snapshot.ref,
    )
    manifest_text = _read_blob(snapshot.git_dir, manifest_blob_sha)
    manifest = _parse_manifest_text(manifest_text)
    manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    return FetchedAppManifest(
        repo_id=snapshot.repo_id,
        url=snapshot.url,
        trusted=snapshot.trusted,
        app_path=str(manifest_path.parent),
        ref=snapshot.ref,
        commit_sha=snapshot.commit_sha,
        manifest_blob_sha=manifest_blob_sha,
        manifest_sha=manifest_sha,
        manifest_text=manifest_text,
        manifest=manifest,
    )


def _parse_manifest_text(text: str) -> AppManifest:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a YAML mapping")
    return parse_app_manifest(data)


def _ensure_origin_remote(mirror_path: Path, url: str) -> None:
    try:
        proc = run_git(["remote", "get-url", "origin"], mirror_path, check=False)
    except GitError:
        proc = None
    if proc and proc.returncode == 0:
        current = (proc.stdout or "").strip()
        if current and current != url:
            run_git(["remote", "set-url", "origin", url], mirror_path, check=True)
    else:
        run_git(["remote", "add", "origin", url], mirror_path, check=True)
    _configure_mirror_remote(mirror_path)


def _configure_mirror_remote(mirror_path: Path) -> None:
    run_git(
        ["config", "remote.origin.fetch", "+refs/*:refs/*"],
        mirror_path,
        check=True,
    )
    run_git(["config", "remote.origin.mirror", "true"], mirror_path, check=True)


def _resolve_commit(mirror_path: Path, repo_id: str, ref: str) -> str:
    try:
        proc = run_git(
            ["rev-parse", f"{ref}^{{commit}}"],
            mirror_path,
            check=True,
        )
    except GitError as exc:
        raise RefNotFoundError(repo_id, ref) from exc
    return (proc.stdout or "").strip()


def _resolve_blob(
    mirror_path: Path,
    commit_sha: str,
    path: str,
    repo_id: str,
    ref: str,
) -> str:
    try:
        proc = run_git(
            ["ls-tree", commit_sha, "--", path],
            mirror_path,
            check=True,
        )
    except GitError as exc:
        raise AppNotFoundError(repo_id, path, ref) from exc

    raw = (proc.stdout or "").strip()
    if not raw:
        raise AppNotFoundError(repo_id, path, ref)

    parts = raw.split()
    if len(parts) < 3:
        raise AppNotFoundError(repo_id, path, ref)
    return parts[2]


def _read_blob(mirror_path: Path, blob_sha: str) -> str:
    proc = run_git(["cat-file", "-p", blob_sha], mirror_path, check=True)
    return proc.stdout or ""


__all__ = [
    "AppNotFoundError",
    "AppRepoSnapshot",
    "FetchedAppManifest",
    "NetworkUnavailableError",
    "RefNotFoundError",
    "RepoNotConfiguredError",
    "ensure_git_mirror",
    "fetch_app_manifest",
    "fetch_app_manifest_from_snapshot",
    "fetch_manifest_by_path",
    "list_manifest_paths",
    "prepare_repo_snapshot",
]
