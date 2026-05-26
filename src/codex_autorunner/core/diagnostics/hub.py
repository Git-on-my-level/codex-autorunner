from __future__ import annotations

from pathlib import Path

from ...manifest import load_manifest, load_manifest_with_issues
from ..config import HubConfig
from ..destinations import (
    probe_docker_readiness,
    resolve_effective_repo_destination,
)
from ..orchestration.sqlite import collect_orchestration_control_plane_status
from ..state_roots import (
    HUB_MANIFEST_FILENAME,
    ORCHESTRATION_DB_FILENAME,
    resolve_hub_manifest_path,
    resolve_hub_orchestration_db_path,
)
from .types import DoctorCheck


def hub_worktree_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Check for unregistered worktrees under the hub worktrees root."""
    checks: list[DoctorCheck] = []
    worktrees_root = hub_config.worktrees_root
    manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    manifest_paths = {
        (hub_config.root / repo.path).resolve() for repo in manifest.repos
    }

    orphans: list[Path] = []
    if worktrees_root.exists():
        try:
            entries = list(worktrees_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not (entry / ".git").exists():
                continue
            resolved = entry.resolve()
            if resolved not in manifest_paths:
                orphans.append(resolved)

    if orphans:
        checks.append(
            DoctorCheck(
                name="Hub worktrees registered",
                passed=False,
                message=(
                    f"{len(orphans)} worktree(s) exist under {worktrees_root} "
                    "but are not in the hub manifest. "
                    "Orphaned worktrees are not auto-deleted per PMA policy; "
                    "use explicit retire or delete commands to remove them."
                ),
                severity="warning",
                fix=f"Run: car hub scan --path {hub_config.root} to register them, "
                "or use: car hub worktree retire <repo_id> to preserve artifacts and remove.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Hub worktrees registered",
                passed=True,
                message="OK",
                severity="warning",
            )
        )
    return checks


def hub_destination_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Report effective destination status and validation issues for hub resources."""
    checks: list[DoctorCheck] = []

    try:
        manifest, manifest_issues = load_manifest_with_issues(
            hub_config.manifest_path, hub_config.root
        )
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        checks.append(
            DoctorCheck(
                name="Hub destination configuration",
                passed=False,
                message=f"Failed to load hub manifest for destination checks: {exc}",
                severity="warning",
                check_id="hub.destination",
                fix=f"Validate manifest at {hub_config.manifest_path}",
            )
        )
        return checks

    repos_by_id = {repo.id: repo for repo in manifest.repos}
    known_repo_ids = set(repos_by_id.keys())
    issues_by_repo: dict[str, list[str]] = {}
    for issue in manifest_issues:
        issues_by_repo.setdefault(issue.repo_id, []).append(issue.message)

    if not manifest.repos:
        checks.append(
            DoctorCheck(
                name="Hub destination configuration",
                passed=True,
                message="No managed resources in hub manifest.",
                severity="info",
                check_id="hub.destination",
            )
        )
        return checks

    docker_targets: list[str] = []

    for repo in manifest.repos:
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        kind = resolution.destination.kind
        source = resolution.source
        if kind == "docker":
            docker_targets.append(f"repo:{repo.id}")
        checks.append(
            DoctorCheck(
                name=f"Hub destination ({repo.id})",
                passed=True,
                message=f"{repo.id}: effective destination '{kind}' (source={source})",
                severity="info",
                check_id="hub.destination",
            )
        )

        issue_messages: list[str] = []
        issue_messages.extend(list(resolution.issues))
        issue_messages.extend(issues_by_repo.get(repo.id, []))
        # preserve order while de-duping repeated issue strings
        deduped_messages = list(dict.fromkeys(issue_messages))
        for message in deduped_messages:
            checks.append(
                DoctorCheck(
                    name=f"Hub destination ({repo.id})",
                    passed=False,
                    message=f"{repo.id}: {message}",
                    severity="warning",
                    check_id="hub.destination",
                    fix=(
                        "Update destination config for this repo in "
                        f"{hub_config.manifest_path}"
                    ),
                )
            )

    for repo_id, messages in sorted(issues_by_repo.items()):
        if repo_id in known_repo_ids:
            continue
        for message in list(dict.fromkeys(messages)):
            checks.append(
                DoctorCheck(
                    name=f"Hub destination ({repo_id})",
                    passed=False,
                    message=f"{repo_id}: {message}",
                    severity="warning",
                    check_id="hub.destination",
                    fix=f"Update malformed manifest repo entry in {hub_config.manifest_path}",
                )
            )

    if docker_targets:
        readiness = probe_docker_readiness()
        resource_targets = ", ".join(sorted(docker_targets))
        checks.append(
            DoctorCheck(
                name="Hub destination (docker binary)",
                passed=readiness.binary_available,
                message=(
                    "Docker CLI is available."
                    if readiness.binary_available
                    else f"Docker CLI unavailable: {readiness.detail}"
                ),
                severity="info" if readiness.binary_available else "warning",
                check_id="hub.destination.docker.binary",
                fix=(
                    None
                    if readiness.binary_available
                    else "Install Docker CLI and ensure it is in PATH."
                ),
            )
        )
        checks.append(
            DoctorCheck(
                name="Hub destination (docker daemon)",
                passed=readiness.daemon_reachable,
                message=(
                    f"Docker daemon reachable for resources: {resource_targets}. "
                    f"{readiness.detail}"
                    if readiness.daemon_reachable
                    else (
                        "Docker daemon unreachable for resources: "
                        f"{resource_targets}. {readiness.detail or 'Run `docker info` for details.'}"
                    )
                ),
                severity="info" if readiness.daemon_reachable else "warning",
                check_id="hub.destination.docker.daemon",
                fix=(
                    None
                    if readiness.daemon_reachable
                    else (
                        "Start Docker daemon/Desktop and rerun `docker info`. "
                        "Destination kind=docker requires daemon connectivity."
                    )
                ),
            )
        )

    return checks


def hub_control_plane_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Report authoritative hub DB compatibility and nested local artifacts."""
    checks: list[DoctorCheck] = []
    status = collect_orchestration_control_plane_status(hub_config.root)
    compatibility = status.get("compatibility")
    compatible = (
        isinstance(compatibility, dict)
        and compatibility.get("status") == "compatible"
        and status.get("error") is None
    )
    checks.append(
        DoctorCheck(
            name="Hub control-plane orchestration DB",
            passed=compatible,
            message=(
                "authoritative DB "
                f"{status.get('db_path')} schema={status.get('schema_generation')} "
                f"target={status.get('target_schema_generation')} "
                f"compatibility={compatibility.get('status') if isinstance(compatibility, dict) else 'unknown'}"
            ),
            severity="error" if not compatible else "info",
            check_id="hub.control_plane.orchestration_db",
            fix=(
                None
                if compatible
                else "Start the hub with the current build or run the explicit hub upgrade path."
            ),
        )
    )
    active_count = len(status.get("active_declarations") or [])
    stale_count = len(status.get("stale_declarations") or [])
    checks.append(
        DoctorCheck(
            name="Hub control-plane compatibility declarations",
            passed=True,
            message=(
                f"active_declarations={active_count}; stale_declarations={stale_count}; "
                f"metadata_present={status.get('metadata') is not None}"
            ),
            severity="info",
            check_id="hub.control_plane.compatibility_declarations",
        )
    )

    canonical_db = resolve_hub_orchestration_db_path(hub_config.root).resolve()
    canonical_manifest = resolve_hub_manifest_path(
        hub_config.root,
        raw_config=hub_config.raw,
    ).resolve()
    nested_db_count = 0
    nested_manifest_count = 0
    for pattern, filename in (
        (
            f"**/.codex-autorunner/{ORCHESTRATION_DB_FILENAME}",
            ORCHESTRATION_DB_FILENAME,
        ),
        (f"**/.codex-autorunner/{HUB_MANIFEST_FILENAME}", HUB_MANIFEST_FILENAME),
    ):
        try:
            candidates = list(hub_config.root.glob(pattern))
        except OSError:
            candidates = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if filename == ORCHESTRATION_DB_FILENAME:
                if resolved == canonical_db:
                    continue
                nested_db_count += 1
            else:
                if resolved == canonical_manifest:
                    continue
                nested_manifest_count += 1
    if nested_db_count or nested_manifest_count:
        checks.append(
            DoctorCheck(
                name="Nested non-authoritative control-plane artifacts",
                passed=True,
                message=(
                    f"Found {nested_db_count} non-authoritative orchestration DB(s) "
                    f"and {nested_manifest_count} non-authoritative manifest(s) under "
                    f"{hub_config.root}. These are repo/worktree artifacts, not the "
                    "running hub authority."
                ),
                severity="info",
                check_id="hub.control_plane.nested_artifacts",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Nested non-authoritative control-plane artifacts",
                passed=True,
                message="No nested non-authoritative orchestration DBs or manifests found.",
                severity="info",
                check_id="hub.control_plane.nested_artifacts",
            )
        )
    return checks


__all__ = [
    "hub_control_plane_doctor_checks",
    "hub_destination_doctor_checks",
    "hub_worktree_doctor_checks",
]
