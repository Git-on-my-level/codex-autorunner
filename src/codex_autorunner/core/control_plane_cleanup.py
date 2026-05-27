from __future__ import annotations

import dataclasses
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .hub_topology import (
    ControlPlaneRole,
    HubTopologyRepository,
    NonAuthoritativeArtifact,
    WorkspaceRegistryEntry,
)
from .state_roots import (
    HUB_MANIFEST_FILENAME,
    HUB_PROJECTION_DB_FILENAME,
    ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME,
    ORCHESTRATION_DB_FILENAME,
)

_CLEANUP_ARCHIVE_ROOT = Path(".codex-autorunner") / "archive" / "control-plane-cleanup"
_SAFE_CLEANUP_ROLES = {
    ControlPlaneRole.HUB_OWNED,
    ControlPlaneRole.REPO_LOCAL_DATA,
}
_ARTIFACT_FILENAMES = frozenset(
    {
        HUB_MANIFEST_FILENAME,
        ORCHESTRATION_DB_FILENAME,
        f"{ORCHESTRATION_DB_FILENAME}-wal",
        f"{ORCHESTRATION_DB_FILENAME}-shm",
        HUB_PROJECTION_DB_FILENAME,
        f"{HUB_PROJECTION_DB_FILENAME}-wal",
        f"{HUB_PROJECTION_DB_FILENAME}-shm",
        ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME,
        f"{ORCHESTRATION_DB_FILENAME}.migrate.lock",
    }
)


@dataclasses.dataclass(frozen=True)
class ControlPlaneCleanupCandidate:
    path: Path
    kind: str
    size_bytes: int
    reason: str
    proposed_action: str
    workspace_root: Path
    control_plane_role: ControlPlaneRole
    repo_id: str | None
    archive_path: Path | None = None

    def to_payload(self, hub_root: Path) -> dict[str, Any]:
        return {
            "path": _display_path(self.path, hub_root),
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
            "proposed_action": self.proposed_action,
            "workspace_root": _display_path(self.workspace_root, hub_root),
            "control_plane_role": self.control_plane_role.value,
            "repo_id": self.repo_id,
            "archive_path": (
                _display_path(self.archive_path, hub_root)
                if self.archive_path is not None
                else None
            ),
        }


@dataclasses.dataclass(frozen=True)
class ControlPlaneCleanupReport:
    hub_root: Path
    dry_run: bool
    candidates: tuple[ControlPlaneCleanupCandidate, ...]
    skipped: tuple[dict[str, str], ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def total_reclaimable_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)

    def to_payload(self) -> dict[str, Any]:
        return {
            "hub_root": str(self.hub_root),
            "dry_run": self.dry_run,
            "candidate_count": len(self.candidates),
            "total_reclaimable_bytes": self.total_reclaimable_bytes,
            "candidates": [
                candidate.to_payload(self.hub_root) for candidate in self.candidates
            ],
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


def plan_control_plane_cleanup(
    *,
    hub_root: Path,
    manifest_path: Path,
    archive_stamp: str | None = None,
) -> ControlPlaneCleanupReport:
    """Classify and report non-authoritative control-plane-looking artifacts."""
    resolved_hub = hub_root.resolve()
    repository = HubTopologyRepository(
        hub_root=resolved_hub,
        manifest_path=manifest_path,
    )
    stamp = archive_stamp or _archive_stamp()
    candidates: list[ControlPlaneCleanupCandidate] = []
    skipped: list[dict[str, str]] = []

    for path in _candidate_paths(resolved_hub):
        workspace_root = path.parent.parent
        entry = repository.classify_workspace_path(workspace_root)
        if entry.control_plane_role == ControlPlaneRole.STANDALONE_HUB:
            skipped.append(
                {
                    "path": _display_path(path, resolved_hub),
                    "reason": "explicit standalone hub control plane",
                }
            )
            continue
        if entry.control_plane_role not in _SAFE_CLEANUP_ROLES:
            skipped.append(
                {
                    "path": _display_path(path, resolved_hub),
                    "reason": f"classified as {entry.control_plane_role.value}",
                }
            )
            continue
        artifact = _artifact_for_path(entry, path)
        if artifact is None:
            skipped.append(
                {
                    "path": _display_path(path, resolved_hub),
                    "reason": "not classified as a non-authoritative artifact",
                }
            )
            continue
        candidates.append(
            ControlPlaneCleanupCandidate(
                path=path,
                kind=artifact.kind,
                size_bytes=_file_size(path),
                reason=artifact.reason,
                proposed_action="archive",
                workspace_root=entry.workspace_root,
                control_plane_role=entry.control_plane_role,
                repo_id=entry.repo_id,
                archive_path=_archive_path(
                    hub_root=resolved_hub,
                    source=path,
                    stamp=stamp,
                ),
            )
        )

    return ControlPlaneCleanupReport(
        hub_root=resolved_hub,
        dry_run=True,
        candidates=tuple(candidates),
        skipped=tuple(skipped),
    )


def apply_control_plane_cleanup(
    report: ControlPlaneCleanupReport,
) -> ControlPlaneCleanupReport:
    moved: list[ControlPlaneCleanupCandidate] = []
    errors: list[str] = []
    for candidate in report.candidates:
        archive_path = candidate.archive_path
        if archive_path is None:
            errors.append(f"{candidate.path}: missing archive path")
            continue
        if not candidate.path.exists():
            errors.append(f"{candidate.path}: disappeared before archive")
            continue
        try:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            destination = _unique_destination(archive_path)
            shutil.move(str(candidate.path), str(destination))
            moved.append(dataclasses.replace(candidate, archive_path=destination))
        except OSError as exc:
            errors.append(f"{candidate.path}: {exc}")

    return ControlPlaneCleanupReport(
        hub_root=report.hub_root,
        dry_run=False,
        candidates=tuple(moved),
        skipped=report.skipped,
        errors=tuple([*report.errors, *errors]),
    )


def _candidate_paths(hub_root: Path) -> Iterable[Path]:
    state_dir_name = ".codex-autorunner"
    for state_root in sorted(hub_root.glob(f"**/{state_dir_name}")):
        if not state_root.is_dir() or state_root.is_symlink():
            continue
        for filename in sorted(_ARTIFACT_FILENAMES):
            candidate = state_root / filename
            if candidate.is_file() or candidate.is_symlink():
                yield candidate.resolve()


def _artifact_for_path(
    entry: WorkspaceRegistryEntry,
    path: Path,
) -> NonAuthoritativeArtifact | None:
    for artifact in entry.non_authoritative_artifacts:
        if artifact.path == path:
            return artifact
    return None


def _archive_path(*, hub_root: Path, source: Path, stamp: str) -> Path:
    try:
        rel = source.relative_to(hub_root)
    except ValueError:
        rel = Path(source.name)
    return hub_root / _CLEANUP_ARCHIVE_ROOT / stamp / rel


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.{index}")
        if not candidate.exists():
            return candidate
    raise OSError(f"unable to choose a unique archive path for {path}")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _display_path(path: Path | None, hub_root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(hub_root))
    except ValueError:
        return str(path)


def _archive_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "ControlPlaneCleanupCandidate",
    "ControlPlaneCleanupReport",
    "apply_control_plane_cleanup",
    "plan_control_plane_cleanup",
]
