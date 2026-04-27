from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Optional

from ..config import HubConfig
from .git_mirror import (
    AppNotFoundError,
    AppRepoSnapshot,
    FetchedAppManifest,
    NetworkUnavailableError,
    RefNotFoundError,
    RepoNotConfiguredError,
    fetch_app_manifest_from_snapshot,
    fetch_manifest_by_path,
    list_manifest_paths,
    prepare_repo_snapshot,
)
from .manifest import AppManifest, ManifestError
from .refs import parse_app_ref, validate_app_id

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class AppSourceInfo:
    repo_id: str
    path: str
    ref: str
    app_id: str
    app_version: str
    app_name: str
    description: str
    commit_sha: str
    manifest_sha: str
    trusted: bool
    manifest: AppManifest
    manifest_text: str


def index_apps(
    hub_config: HubConfig,
    hub_root: Path,
) -> list[AppSourceInfo]:
    apps: list[AppSourceInfo] = []

    if not hub_config.apps.enabled:
        return apps

    for repo in hub_config.apps.repos:
        apps.extend(_index_single_repo(repo, hub_root))

    return apps


def _index_single_repo(repo, hub_root: Path) -> list[AppSourceInfo]:
    try:
        snapshot = prepare_repo_snapshot(repo, hub_root)
    except (NetworkUnavailableError, RefNotFoundError, RepoNotConfiguredError, OSError):
        return []

    apps: list[AppSourceInfo] = []
    for manifest_path in list_manifest_paths(snapshot):
        try:
            fetched = fetch_manifest_by_path(snapshot, manifest_path)
        except (AppNotFoundError, ManifestError, ValueError) as exc:
            logger.debug(
                "Skipping invalid app manifest repo=%s path=%s: %s",
                repo.id,
                manifest_path,
                exc,
            )
            continue
        apps.append(
            _to_source_info(
                fetched.repo_id,
                fetched.app_path,
                snapshot,
                fetched,
            )
        )
    return apps


def get_app_by_ref(
    hub_config: HubConfig,
    hub_root: Path,
    app_ref: str,
) -> Optional[AppSourceInfo]:
    try:
        parsed = parse_app_ref(app_ref)
    except ValueError:
        return None

    repo = next(
        (repo for repo in hub_config.apps.repos if repo.id == parsed.repo_id), None
    )
    if repo is None:
        return None

    try:
        snapshot = prepare_repo_snapshot(repo, hub_root, ref=parsed.ref)
        fetched = fetch_app_manifest_from_snapshot(snapshot, parsed)
    except (
        AppNotFoundError,
        ManifestError,
        NetworkUnavailableError,
        RefNotFoundError,
        RepoNotConfiguredError,
        ValueError,
    ):
        return None

    return _to_source_info(fetched.repo_id, fetched.app_path, snapshot, fetched)


def is_probably_installed_app_id(raw: str) -> bool:
    if ":" in raw:
        return False
    try:
        validate_app_id(raw)
    except ValueError:
        return False
    return True


def _to_source_info(
    repo_id: str,
    app_path: str,
    snapshot: AppRepoSnapshot,
    fetched: FetchedAppManifest,
) -> AppSourceInfo:
    manifest = fetched.manifest
    return AppSourceInfo(
        repo_id=repo_id,
        path=app_path,
        ref=snapshot.ref,
        app_id=manifest.id,
        app_version=manifest.version,
        app_name=manifest.name,
        description=manifest.description,
        commit_sha=snapshot.commit_sha,
        manifest_sha=fetched.manifest_sha,
        trusted=snapshot.trusted,
        manifest=manifest,
        manifest_text=fetched.manifest_text,
    )


__all__ = [
    "AppSourceInfo",
    "get_app_by_ref",
    "index_apps",
    "is_probably_installed_app_id",
]
