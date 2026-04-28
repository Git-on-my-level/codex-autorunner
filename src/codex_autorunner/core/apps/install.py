from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from ..config import HubConfig
from ..git_utils import GitError, run_git
from ..state_roots import resolve_repo_state_root
from ..utils import atomic_write
from .git_mirror import (
    AppNotFoundError,
    AppRepoSnapshot,
    RepoNotConfiguredError,
    fetch_app_manifest_from_snapshot,
    prepare_repo_snapshot,
)
from .manifest import AppManifest, ManifestError, load_app_manifest
from .paths import AppPathError, validate_app_path
from .refs import AppRef, parse_app_ref, validate_app_id


class AppInstallError(Exception):
    """Raised when an app installation or installed-app load fails."""


class AppInstallConflictError(AppInstallError):
    """Raised when an app id is already installed with different provenance."""


@dataclasses.dataclass(frozen=True)
class InstalledAppPaths:
    apps_root: Path
    app_root: Path
    lock_path: Path
    bundle_root: Path
    state_root: Path
    artifacts_root: Path
    logs_root: Path


@dataclasses.dataclass(frozen=True)
class AppLock:
    id: str
    version: str
    source_repo_id: str
    source_url: str
    source_path: str
    source_ref: str
    commit_sha: str
    manifest_sha: str
    bundle_sha: str
    trusted: bool
    installed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "source_repo_id": self.source_repo_id,
            "source_url": self.source_url,
            "source_path": self.source_path,
            "source_ref": self.source_ref,
            "commit_sha": self.commit_sha,
            "manifest_sha": self.manifest_sha,
            "bundle_sha": self.bundle_sha,
            "trusted": self.trusted,
            "installed_at": self.installed_at,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "AppLock":
        try:
            app_id = validate_app_id(str(payload["id"]))
            version = str(payload["version"])
            source_repo_id = str(payload["source_repo_id"])
            source_url = str(payload["source_url"])
            source_path = str(validate_app_path(str(payload["source_path"])))
            source_ref = str(payload["source_ref"])
            commit_sha = str(payload["commit_sha"])
            manifest_sha = str(payload["manifest_sha"])
            bundle_sha = str(payload["bundle_sha"])
            installed_at = str(payload["installed_at"])
        except KeyError as exc:
            raise AppInstallError(f"app lock missing field: {exc.args[0]}") from exc
        except (AppPathError, ValueError) as exc:
            raise AppInstallError(f"invalid app lock: {exc}") from exc

        if not version.strip():
            raise AppInstallError("invalid app lock: version must not be empty")
        if not source_repo_id.strip():
            raise AppInstallError("invalid app lock: source_repo_id must not be empty")
        if not source_url.strip():
            raise AppInstallError("invalid app lock: source_url must not be empty")
        if not source_ref.strip():
            raise AppInstallError("invalid app lock: source_ref must not be empty")
        if not commit_sha.strip():
            raise AppInstallError("invalid app lock: commit_sha must not be empty")
        if not manifest_sha.strip():
            raise AppInstallError("invalid app lock: manifest_sha must not be empty")
        if not bundle_sha.strip():
            raise AppInstallError("invalid app lock: bundle_sha must not be empty")
        if not installed_at.strip():
            raise AppInstallError("invalid app lock: installed_at must not be empty")

        return AppLock(
            id=app_id,
            version=version,
            source_repo_id=source_repo_id,
            source_url=source_url,
            source_path=source_path,
            source_ref=source_ref,
            commit_sha=commit_sha,
            manifest_sha=manifest_sha,
            bundle_sha=bundle_sha,
            trusted=bool(payload.get("trusted", False)),
            installed_at=installed_at,
        )


@dataclasses.dataclass(frozen=True)
class InstalledAppInfo:
    paths: InstalledAppPaths
    lock: AppLock
    manifest: AppManifest
    manifest_text: str
    bundle_verified: bool

    @property
    def app_id(self) -> str:
        return self.lock.id

    @property
    def app_version(self) -> str:
        return self.lock.version


@dataclasses.dataclass(frozen=True)
class InstallResult:
    app: InstalledAppInfo
    changed: bool


@dataclasses.dataclass(frozen=True)
class _BundleFile:
    rel_path: PurePosixPath
    mode: str
    content: bytes


def installed_apps_root(repo_root: Path) -> Path:
    return resolve_repo_state_root(repo_root) / "apps"


def installed_app_paths(repo_root: Path, app_id: str) -> InstalledAppPaths:
    validated_app_id = validate_app_id(app_id)
    apps_root = installed_apps_root(repo_root)
    app_root = apps_root / validated_app_id
    return InstalledAppPaths(
        apps_root=apps_root,
        app_root=app_root,
        lock_path=app_root / "app.lock.json",
        bundle_root=app_root / "bundle",
        state_root=app_root / "state",
        artifacts_root=app_root / "artifacts",
        logs_root=app_root / "logs",
    )


def load_app_lock(path: Path) -> AppLock:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AppInstallError(f"app lock missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AppInstallError(f"invalid app lock JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AppInstallError(f"invalid app lock JSON object: {path}")
    return AppLock.from_dict(payload)


def compute_bundle_sha(bundle_root: Path) -> str:
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    if not bundle_root.exists():
        raise AppInstallError(f"bundle directory not found: {bundle_root}")

    for path in bundle_root.rglob("*"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(bundle_root).as_posix()
        try:
            normalized = str(validate_app_path(rel_path))
        except AppPathError as exc:
            raise AppInstallError(
                f"bundle contains invalid relative path {rel_path!r}: {exc}"
            ) from exc
        files.append((normalized, path))

    for rel_path, path in sorted(files):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_bundle_sha(bundle_root: Path, expected_sha: str) -> bool:
    return compute_bundle_sha(bundle_root) == expected_sha


def get_installed_app(repo_root: Path, app_id: str) -> Optional[InstalledAppInfo]:
    paths = installed_app_paths(repo_root, app_id)
    if not paths.lock_path.exists():
        return None
    return _load_installed_app(paths)


def list_installed_apps(repo_root: Path) -> list[InstalledAppInfo]:
    apps_root = installed_apps_root(repo_root)
    if not apps_root.exists():
        return []

    apps: list[InstalledAppInfo] = []
    for child in sorted(apps_root.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        lock_path = child / "app.lock.json"
        if not lock_path.exists():
            continue
        apps.append(_load_installed_app(installed_app_paths(repo_root, child.name)))
    return apps


def install_app(
    hub_config: HubConfig,
    hub_root: Path,
    repo_root: Path,
    app_ref: str,
    *,
    force: bool = False,
) -> InstallResult:
    try:
        parsed = parse_app_ref(app_ref)
    except ValueError as exc:
        raise AppInstallError(str(exc)) from exc

    repo = next(
        (
            candidate
            for candidate in hub_config.apps.repos
            if candidate.id == parsed.repo_id
        ),
        None,
    )
    if repo is None:
        raise AppInstallError(str(RepoNotConfiguredError(parsed.repo_id)))

    try:
        snapshot = prepare_repo_snapshot(repo, hub_root, ref=parsed.ref)
        fetched = fetch_app_manifest_from_snapshot(snapshot, parsed)
        bundle_files = _read_bundle_files(snapshot, parsed)
    except (
        AppInstallError,
        AppNotFoundError,
        ManifestError,
        RepoNotConfiguredError,
        ValueError,
    ) as exc:
        raise AppInstallError(str(exc)) from exc

    candidate_bundle_sha = _bundle_sha_for_entries(bundle_files)
    existing = get_installed_app(repo_root, fetched.manifest.id)
    candidate_lock = AppLock(
        id=fetched.manifest.id,
        version=fetched.manifest.version,
        source_repo_id=snapshot.repo_id,
        source_url=snapshot.url,
        source_path=fetched.app_path,
        source_ref=snapshot.ref,
        commit_sha=snapshot.commit_sha,
        manifest_sha=fetched.manifest_sha,
        bundle_sha=candidate_bundle_sha,
        trusted=snapshot.trusted,
        installed_at=_utc_now_iso(),
    )

    if existing is not None:
        if _lock_equivalent(existing.lock, candidate_lock):
            if not existing.bundle_verified:
                if not force:
                    raise AppInstallError(
                        f"Installed bundle hash mismatch for app {existing.app_id}; "
                        "use --force to repair."
                    )
            else:
                return InstallResult(app=existing, changed=False)
        elif not force:
            raise AppInstallConflictError(
                f"App {fetched.manifest.id} is already installed from "
                f"{existing.lock.source_repo_id}:{existing.lock.source_path}@"
                f"{existing.lock.source_ref} ({existing.lock.commit_sha}); "
                "use --force to replace it."
            )

    paths = installed_app_paths(repo_root, fetched.manifest.id)
    _materialize_bundle(paths, bundle_files)
    materialized_bundle_sha = compute_bundle_sha(paths.bundle_root)
    if materialized_bundle_sha != candidate_bundle_sha:
        raise AppInstallError(
            f"materialized bundle hash mismatch for {fetched.manifest.id}: "
            f"expected {candidate_bundle_sha}, got {materialized_bundle_sha}"
        )

    lock = dataclasses.replace(candidate_lock, bundle_sha=materialized_bundle_sha)
    atomic_write(paths.lock_path, json.dumps(lock.to_dict(), indent=2) + "\n")
    installed = _load_installed_app(paths)
    return InstallResult(app=installed, changed=True)


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_installed_app(paths: InstalledAppPaths) -> InstalledAppInfo:
    lock = load_app_lock(paths.lock_path)
    manifest_path = paths.bundle_root / "car-app.yaml"
    if not manifest_path.exists():
        raise AppInstallError(f"installed manifest missing: {manifest_path}")
    try:
        manifest = load_app_manifest(manifest_path)
    except ManifestError as exc:
        raise AppInstallError(
            f"invalid installed manifest {manifest_path}: {exc}"
        ) from exc
    manifest_text = manifest_path.read_text(encoding="utf-8")
    bundle_verified = verify_bundle_sha(paths.bundle_root, lock.bundle_sha)
    return InstalledAppInfo(
        paths=paths,
        lock=lock,
        manifest=manifest,
        manifest_text=manifest_text,
        bundle_verified=bundle_verified,
    )


def _lock_equivalent(left: AppLock, right: AppLock) -> bool:
    return (
        left.id == right.id
        and left.version == right.version
        and left.source_repo_id == right.source_repo_id
        and left.source_url == right.source_url
        and left.source_path == right.source_path
        and left.source_ref == right.source_ref
        and left.commit_sha == right.commit_sha
        and left.manifest_sha == right.manifest_sha
        and left.bundle_sha == right.bundle_sha
        and left.trusted == right.trusted
    )


def _materialize_bundle(
    paths: InstalledAppPaths, bundle_files: list[_BundleFile]
) -> None:
    paths.apps_root.mkdir(parents=True, exist_ok=True)
    paths.app_root.mkdir(parents=True, exist_ok=True)
    for directory in (paths.state_root, paths.artifacts_root, paths.logs_root):
        directory.mkdir(parents=True, exist_ok=True)

    stage_dir: Optional[Path] = Path(
        tempfile.mkdtemp(prefix=f".{paths.app_root.name}.bundle.", dir=paths.apps_root)
    )
    backup_dir: Optional[Path] = None
    try:
        for bundle_file in bundle_files:
            assert stage_dir is not None
            target = stage_dir.joinpath(*bundle_file.rel_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle_file.content)
            target.chmod(0o755 if bundle_file.mode == "100755" else 0o644)

        if paths.bundle_root.exists():
            backup_dir = paths.apps_root / f".{paths.app_root.name}.bundle.backup"
            if backup_dir.exists():
                _remove_path(backup_dir)
            paths.bundle_root.rename(backup_dir)

        assert stage_dir is not None
        stage_dir.rename(paths.bundle_root)
        stage_dir = None

        if backup_dir is not None and backup_dir.exists():
            _remove_path(backup_dir)
    except Exception:
        if (
            backup_dir is not None
            and backup_dir.exists()
            and not paths.bundle_root.exists()
        ):
            backup_dir.rename(paths.bundle_root)
        raise
    finally:
        if stage_dir is not None and stage_dir.exists():
            _remove_path(stage_dir)
        if backup_dir is not None and backup_dir.exists():
            _remove_path(backup_dir)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _bundle_sha_for_entries(bundle_files: list[_BundleFile]) -> str:
    digest = hashlib.sha256()
    for bundle_file in sorted(bundle_files, key=lambda item: item.rel_path.as_posix()):
        digest.update(bundle_file.rel_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bundle_file.content)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_bundle_files(
    snapshot: AppRepoSnapshot,
    app_ref: AppRef,
) -> list[_BundleFile]:
    try:
        app_root = validate_app_path(app_ref.app_path)
    except AppPathError as exc:
        raise AppInstallError(str(exc)) from exc

    try:
        proc = run_git(
            ["ls-tree", "-r", "-z", snapshot.commit_sha, "--", str(app_root)],
            snapshot.git_dir,
            check=True,
        )
    except GitError as exc:
        raise AppNotFoundError(snapshot.repo_id, str(app_root), snapshot.ref) from exc

    entries: list[_BundleFile] = []
    raw_output = proc.stdout or ""
    if not raw_output:
        raise AppNotFoundError(snapshot.repo_id, str(app_root), snapshot.ref)

    for raw_entry in raw_output.split("\0"):
        if not raw_entry:
            continue
        header, sep, full_path = raw_entry.partition("\t")
        if not sep:
            continue
        parts = header.split()
        if len(parts) != 3:
            continue
        mode, object_type, blob_sha = parts
        if object_type != "blob" or not mode.startswith("100"):
            continue

        rel_path = _relative_bundle_path(full_path, app_root)
        if rel_path is None:
            continue
        content = _read_blob(snapshot.git_dir, blob_sha)
        entries.append(_BundleFile(rel_path=rel_path, mode=mode, content=content))

    if not entries:
        raise AppInstallError(
            f"app bundle has no materializable files: {snapshot.repo_id}:{app_root}"
        )
    return entries


def _relative_bundle_path(
    full_path: str,
    app_root: PurePosixPath,
) -> Optional[PurePosixPath]:
    try:
        full_posix = PurePosixPath(full_path)
        rel_path = full_posix.relative_to(app_root)
    except ValueError:
        return None
    if str(rel_path) in {"", "."}:
        return None
    try:
        return validate_app_path(str(rel_path))
    except AppPathError:
        return None


def _read_blob(git_dir: Path, blob_sha: str) -> bytes:
    try:
        proc = run_git(["cat-file", "blob", blob_sha], git_dir, check=True)
    except GitError as exc:
        raise AppInstallError(f"failed to read blob {blob_sha}: {exc}") from exc
    return (proc.stdout or "").encode("utf-8")


__all__ = [
    "AppInstallConflictError",
    "AppInstallError",
    "AppLock",
    "InstallResult",
    "InstalledAppInfo",
    "InstalledAppPaths",
    "compute_bundle_sha",
    "get_installed_app",
    "install_app",
    "installed_app_paths",
    "installed_apps_root",
    "list_installed_apps",
    "load_app_lock",
    "verify_bundle_sha",
]
