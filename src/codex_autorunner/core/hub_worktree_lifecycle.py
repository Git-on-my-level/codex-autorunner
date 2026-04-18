from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from .hub import RepoSnapshot
    from .manifest import Manifest, ManifestRepo


class WorktreeHubContext(Protocol):
    """Narrow collaborator contract for worktree lifecycle operations.

    Replaces the 7 individual callbacks previously passed to WorktreeManager
    with a single named-method protocol. Each method maps to one hub-supplied
    capability that the worktree manager needs but should not own.
    """

    def invalidate_cache(self) -> None: ...

    def snapshot_for_repo(self, repo_id: str) -> "RepoSnapshot": ...

    def stop_runner(
        self,
        *,
        repo_id: str,
        repo_path: Path,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.2,
    ) -> None: ...

    def archive_repo_state(
        self,
        *,
        repo_id: str,
        archive_note: Optional[str] = None,
        archive_profile: Optional[str] = None,
    ) -> Dict[str, object]: ...

    def base_repo_paths(self, manifest: "Manifest") -> dict[str, Path]: ...

    def collect_unbound_repo_threads(
        self,
        *,
        manifest: Optional["Manifest"] = None,
    ) -> dict[str, list[str]]: ...

    def archive_unbound_repo_threads(
        self,
        *,
        repo_id: str,
        unbound_threads_by_repo: Optional[dict[str, list[str]]] = None,
    ) -> list[str]: ...


@dataclass
class CleanupStepRecord:
    step: str
    status: str
    detail: Optional[str] = None


@dataclass
class WorktreeCleanupReport:
    steps: List[CleanupStepRecord] = field(default_factory=list)
    docker_cleanup: Optional[Dict[str, object]] = None
    archived_thread_ids: List[str] = field(default_factory=list)

    def add_step(
        self,
        step: str,
        status: str,
        detail: Optional[str] = None,
    ) -> None:
        self.steps.append(CleanupStepRecord(step=step, status=status, detail=detail))

    @property
    def completed_steps(self) -> List[str]:
        return [s.step for s in self.steps if s.status == "ok"]

    @property
    def failed_step(self) -> Optional[str]:
        for s in self.steps:
            if s.status == "error":
                return s.step
        return None


@dataclass
class ResolvedWorktreeEntry:
    manifest: "Manifest"
    entry: "ManifestRepo"
    base: "ManifestRepo"
    base_path: Path
    worktree_path: Path
