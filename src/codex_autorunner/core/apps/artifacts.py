from __future__ import annotations

import dataclasses
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..config import ConfigError, load_repo_config
from ..flows.models import FlowArtifact
from ..flows.store import FlowStore
from .install import (
    AppInstallError,
    InstalledAppInfo,
    get_installed_app,
    installed_app_paths,
    list_installed_apps,
)
from .manifest import AppTool
from .paths import AppPathError, validate_app_path

_RUNTIME_ALLOWED_DIR_NAMES = ("artifacts", "state", "logs")
_BEFORE_CHAT_WRAPUP_HOOK_POINT = "before_chat_wrapup"
_REGISTERED_APP_ARTIFACT_METADATA_KEYS = frozenset(
    {
        "app_id",
        "app_version",
        "tool_id",
        "hook_point",
        "label",
        "kind",
        "relative_path",
    }
)


class AppArtifactError(Exception):
    """Raised when app artifact discovery or validation fails."""


@dataclasses.dataclass(frozen=True)
class AppArtifactCandidate:
    app_id: str
    app_version: str
    kind: str
    label: str
    relative_path: str
    absolute_path: Path
    tool_id: Optional[str] = None
    hook_point: Optional[str] = None


def collect_declared_tool_artifact_candidates(
    installed: InstalledAppInfo,
    tool: AppTool,
    *,
    hook_point: Optional[str] = None,
) -> tuple[AppArtifactCandidate, ...]:
    found: list[AppArtifactCandidate] = []
    for output in tool.outputs:
        absolute_path = resolve_app_runtime_artifact_path(
            installed,
            output.path,
            allow_any_runtime_path=True,
        )
        if not absolute_path.exists() or not absolute_path.is_file():
            continue
        found.append(
            AppArtifactCandidate(
                app_id=installed.app_id,
                app_version=installed.app_version,
                kind=output.kind,
                label=output.label,
                relative_path=output.path,
                absolute_path=absolute_path,
                tool_id=tool.id,
                hook_point=hook_point,
            )
        )
    return tuple(found)


def register_app_artifact_candidates(
    repo_root: Path,
    run_id: str,
    candidates: Sequence[AppArtifactCandidate],
) -> tuple[FlowArtifact, ...]:
    if not candidates:
        return ()
    db_path = FlowStore.default_path(repo_root)
    if not db_path.exists():
        return ()
    try:
        durable_writes = bool(load_repo_config(repo_root).durable_writes)
    except (ConfigError, OSError, ValueError, TypeError):
        durable_writes = False

    try:
        with FlowStore(db_path, durable=durable_writes) as store:
            if store.get_flow_run(run_id) is None:
                return ()
            created: list[FlowArtifact] = []
            for candidate in candidates:
                created.append(
                    store.create_artifact(
                        artifact_id=str(uuid.uuid4()),
                        run_id=run_id,
                        kind=candidate.kind,
                        path=str(candidate.absolute_path),
                        metadata={
                            "app_id": candidate.app_id,
                            "app_version": candidate.app_version,
                            "tool_id": candidate.tool_id,
                            "hook_point": candidate.hook_point,
                            "label": candidate.label,
                            "kind": candidate.kind,
                            "relative_path": candidate.relative_path,
                        },
                    )
                )
            return tuple(created)
    except (RuntimeError, sqlite3.Error, ValueError, TypeError):
        return ()


def list_registered_app_artifacts(
    repo_root: Path,
    app_id: str,
    *,
    run_id: Optional[str] = None,
) -> tuple[FlowArtifact, ...]:
    db_path = FlowStore.default_path(repo_root)
    if not db_path.exists():
        return ()
    try:
        durable_writes = bool(load_repo_config(repo_root).durable_writes)
    except (ConfigError, OSError, ValueError, TypeError):
        durable_writes = False

    try:
        with FlowStore(db_path, durable=durable_writes) as store:
            artifacts: list[FlowArtifact] = []
            run_ids: list[str] = []
            if run_id is not None:
                if store.get_flow_run(run_id) is None:
                    return ()
                run_ids.append(run_id)
            else:
                run_ids.extend(record.id for record in store.list_flow_runs())
            for candidate_run_id in run_ids:
                for artifact in store.get_artifacts(candidate_run_id):
                    metadata = (
                        artifact.metadata if isinstance(artifact.metadata, dict) else {}
                    )
                    if metadata.get("app_id") != app_id:
                        continue
                    artifacts.append(artifact)
            artifacts.sort(key=lambda item: item.created_at)
            return tuple(artifacts)
    except (RuntimeError, sqlite3.Error, ValueError, TypeError):
        return ()


def list_app_local_artifact_files(
    repo_root: Path,
    app_id: str,
) -> tuple[AppArtifactCandidate, ...]:
    try:
        installed = get_installed_app(repo_root, app_id)
    except AppInstallError as exc:
        raise AppArtifactError(str(exc)) from exc
    if installed is None:
        raise AppArtifactError(f"Installed app not found: {app_id}")

    discovered: dict[str, AppArtifactCandidate] = {}
    for file_path in sorted(installed.paths.artifacts_root.rglob("*")):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(installed.paths.app_root).as_posix()
        discovered[str(file_path.resolve())] = AppArtifactCandidate(
            app_id=installed.app_id,
            app_version=installed.app_version,
            kind=_guess_artifact_kind(file_path),
            label=file_path.name,
            relative_path=relative_path,
            absolute_path=file_path.resolve(),
        )

    for candidate in _manifest_declared_runtime_candidates(installed):
        discovered.setdefault(str(candidate.absolute_path), candidate)

    return tuple(
        sorted(
            discovered.values(),
            key=lambda item: (item.relative_path, str(item.absolute_path)),
        )
    )


def collect_before_chat_wrapup_artifacts(
    repo_root: Path,
    *,
    max_file_size_bytes: int,
) -> tuple[AppArtifactCandidate, ...]:
    if max_file_size_bytes <= 0:
        return ()
    try:
        installed_apps = list_installed_apps(repo_root)
    except AppInstallError as exc:
        raise AppArtifactError(str(exc)) from exc

    collected: dict[str, AppArtifactCandidate] = {}
    for installed in installed_apps:
        for hook in installed.manifest.hooks:
            if hook.point != _BEFORE_CHAT_WRAPUP_HOOK_POINT:
                continue
            for entry in hook.entries:
                for relative_path in entry.artifacts:
                    absolute_path = resolve_app_runtime_artifact_path(
                        installed,
                        relative_path,
                        allowed_dir_names=_RUNTIME_ALLOWED_DIR_NAMES,
                    )
                    if not absolute_path.exists() or not absolute_path.is_file():
                        continue
                    try:
                        size_bytes = absolute_path.stat().st_size
                    except OSError:
                        continue
                    if size_bytes > max_file_size_bytes:
                        continue
                    candidate = AppArtifactCandidate(
                        app_id=installed.app_id,
                        app_version=installed.app_version,
                        kind=_kind_for_relative_path(
                            installed,
                            relative_path,
                            fallback_path=absolute_path,
                        ),
                        label=_label_for_relative_path(
                            installed,
                            relative_path,
                            fallback_path=absolute_path,
                        ),
                        relative_path=relative_path,
                        absolute_path=absolute_path,
                        hook_point=_BEFORE_CHAT_WRAPUP_HOOK_POINT,
                    )
                    collected.setdefault(str(absolute_path), candidate)
    return tuple(
        sorted(
            collected.values(),
            key=lambda item: (item.app_id, item.relative_path, item.absolute_path.name),
        )
    )


def resolve_registered_app_artifact_path(
    repo_root: Path,
    artifact: FlowArtifact,
) -> Optional[Path]:
    metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
    app_id = metadata.get("app_id")
    if not isinstance(app_id, str) or not app_id.strip():
        return None
    raw_path = artifact.path if isinstance(artifact.path, str) else ""
    if not raw_path.strip():
        return None
    absolute_path = Path(raw_path).expanduser()
    if not absolute_path.is_absolute():
        absolute_path = (repo_root / absolute_path).resolve()
    else:
        absolute_path = absolute_path.resolve()

    paths = installed_app_paths(repo_root, app_id)
    app_root = paths.app_root.resolve()
    bundle_root = paths.bundle_root.resolve()
    if not absolute_path.is_relative_to(app_root):
        return None
    if absolute_path.is_relative_to(bundle_root):
        return None
    return absolute_path


def is_registered_app_artifact(artifact: FlowArtifact) -> bool:
    metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
    return any(key in metadata for key in _REGISTERED_APP_ARTIFACT_METADATA_KEYS)


def resolve_app_runtime_artifact_path(
    installed: InstalledAppInfo,
    relative_path: str,
    *,
    allow_any_runtime_path: bool = False,
    allowed_dir_names: Sequence[str] = _RUNTIME_ALLOWED_DIR_NAMES,
) -> Path:
    try:
        normalized = validate_app_path(relative_path)
    except AppPathError as exc:
        raise AppArtifactError(
            f"Invalid app artifact path {relative_path!r}: {exc}"
        ) from exc

    absolute_path = installed.paths.app_root.joinpath(*normalized.parts).resolve()
    app_root = installed.paths.app_root.resolve()
    bundle_root = installed.paths.bundle_root.resolve()
    if not absolute_path.is_relative_to(app_root):
        raise AppArtifactError(
            f"App artifact path escapes installed app runtime: {relative_path!r}"
        )
    if absolute_path.is_relative_to(bundle_root):
        raise AppArtifactError(
            f"App artifact path resolves under bundle/, not runtime output: {relative_path!r}"
        )
    if allow_any_runtime_path:
        return absolute_path

    allowed_roots = tuple(
        (installed.paths.app_root / dir_name).resolve()
        for dir_name in allowed_dir_names
    )
    if any(absolute_path.is_relative_to(root) for root in allowed_roots):
        return absolute_path
    allowed_text = ", ".join(f"{name}/" for name in allowed_dir_names)
    raise AppArtifactError(
        f"App artifact path must remain under an allowed runtime directory ({allowed_text}): {relative_path!r}"
    )


def _manifest_declared_runtime_candidates(
    installed: InstalledAppInfo,
) -> Iterable[AppArtifactCandidate]:
    seen: set[str] = set()
    for tool in installed.manifest.tools.values():
        for output in tool.outputs:
            absolute_path = resolve_app_runtime_artifact_path(
                installed,
                output.path,
                allow_any_runtime_path=True,
            )
            if not absolute_path.exists() or not absolute_path.is_file():
                continue
            key = str(absolute_path)
            if key in seen:
                continue
            seen.add(key)
            yield AppArtifactCandidate(
                app_id=installed.app_id,
                app_version=installed.app_version,
                kind=output.kind,
                label=output.label or absolute_path.name,
                relative_path=output.path,
                absolute_path=absolute_path,
                tool_id=tool.id,
            )
    for hook in installed.manifest.hooks:
        if hook.point != _BEFORE_CHAT_WRAPUP_HOOK_POINT:
            continue
        for entry in hook.entries:
            for relative_path in entry.artifacts:
                absolute_path = resolve_app_runtime_artifact_path(
                    installed,
                    relative_path,
                    allowed_dir_names=_RUNTIME_ALLOWED_DIR_NAMES,
                )
                if not absolute_path.exists() or not absolute_path.is_file():
                    continue
                key = str(absolute_path)
                if key in seen:
                    continue
                seen.add(key)
                yield AppArtifactCandidate(
                    app_id=installed.app_id,
                    app_version=installed.app_version,
                    kind=_kind_for_relative_path(
                        installed,
                        relative_path,
                        fallback_path=absolute_path,
                    ),
                    label=_label_for_relative_path(
                        installed,
                        relative_path,
                        fallback_path=absolute_path,
                    ),
                    relative_path=relative_path,
                    absolute_path=absolute_path,
                    hook_point=hook.point,
                )


def _label_for_relative_path(
    installed: InstalledAppInfo,
    relative_path: str,
    *,
    fallback_path: Path,
) -> str:
    for tool in installed.manifest.tools.values():
        for output in tool.outputs:
            if output.path == relative_path and output.label:
                return output.label
    return fallback_path.name


def _kind_for_relative_path(
    installed: InstalledAppInfo,
    relative_path: str,
    *,
    fallback_path: Path,
) -> str:
    for tool in installed.manifest.tools.values():
        for output in tool.outputs:
            if output.path == relative_path and output.kind:
                return output.kind
    return _guess_artifact_kind(fallback_path)


def _guess_artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix in {".html", ".htm"}:
        return "html"
    return "text"


__all__ = [
    "AppArtifactCandidate",
    "AppArtifactError",
    "collect_before_chat_wrapup_artifacts",
    "collect_declared_tool_artifact_candidates",
    "is_registered_app_artifact",
    "list_app_local_artifact_files",
    "list_registered_app_artifacts",
    "register_app_artifact_candidates",
    "resolve_app_runtime_artifact_path",
    "resolve_registered_app_artifact_path",
]
