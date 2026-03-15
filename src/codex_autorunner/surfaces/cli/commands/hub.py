import json
import shlex
from pathlib import Path
from typing import Any, Callable, Optional

import typer
import uvicorn

from ....core.config import HubConfig
from ....core.destinations import (
    resolve_effective_agent_workspace_destination,
    resolve_effective_repo_destination,
    validate_destination_write_payload,
)
from ....core.hub import HubSupervisor
from ....core.orchestration import verify_migration
from ....core.orchestration.sqlite import open_orchestration_sqlite
from ....manifest import Manifest, load_manifest, save_manifest
from ...web.app import create_hub_app


def register_hub_commands(
    hub_app: typer.Typer,
    *,
    require_hub_config: Callable[[Optional[Path]], HubConfig],
    raise_exit: Callable,
    build_supervisor: Callable[[HubConfig], HubSupervisor],
    enforce_bind_auth: Callable,
    build_server_url: Callable,
    request_json: Callable,
    normalize_base_path: Callable,
) -> None:
    destination_app = typer.Typer(
        add_completion=False, help="Inspect and set per-repo runtime destinations."
    )
    hub_app.add_typer(destination_app, name="destination")

    agent_workspace_app = typer.Typer(
        add_completion=False,
        help="Create, inspect, and manage first-class hub agent workspaces.",
    )
    hub_app.add_typer(agent_workspace_app, name="agent-workspace")

    agent_workspace_destination_app = typer.Typer(
        add_completion=False,
        help="Inspect and set per-agent-workspace runtime destinations.",
    )
    agent_workspace_app.add_typer(agent_workspace_destination_app, name="destination")

    orchestration_app = typer.Typer(
        add_completion=False, help="Orchestration state migration and verification."
    )
    hub_app.add_typer(orchestration_app, name="orchestration")

    def _resolve_repo_entry(config: HubConfig, repo_id: str):
        manifest = load_manifest(config.manifest_path, config.root)
        repos_by_id = {entry.id: entry for entry in manifest.repos}
        repo = repos_by_id.get(repo_id)
        if repo is None:
            raise_exit(f"Repo id not found in hub manifest: {repo_id}")
        return manifest, repos_by_id, repo

    def _destination_issues(
        manifest: Manifest,
        *,
        repo_id: str,
        resolution_issues: tuple[str, ...],
    ) -> list[str]:
        merged = [*manifest.issues_for_repo(repo_id), *resolution_issues]
        deduped: list[str] = []
        for message in merged:
            if message in deduped:
                continue
            deduped.append(message)
        return deduped

    def _resolve_agent_workspace_entry(config: HubConfig, workspace_id: str):
        manifest = load_manifest(config.manifest_path, config.root)
        workspace = manifest.get_agent_workspace(workspace_id)
        if workspace is None:
            raise_exit(f"Agent workspace id not found in hub manifest: {workspace_id}")
        return manifest, workspace

    def _agent_workspace_payload(
        config: HubConfig,
        supervisor: HubSupervisor,
        workspace_id: str,
    ) -> dict[str, Any]:
        manifest, workspace = _resolve_agent_workspace_entry(config, workspace_id)
        snapshot = supervisor.get_agent_workspace_snapshot(workspace_id)
        resolution = resolve_effective_agent_workspace_destination(workspace)
        payload: dict[str, Any] = {
            **snapshot.to_dict(config.root),
            "configured_destination": workspace.destination,
            "effective_destination": resolution.to_dict(),
            "source": "configured" if workspace.destination else "default",
            "issues": [
                *manifest.issues_for_repo(workspace.id),
                *list(resolution.issues or ()),
            ],
        }
        readiness = supervisor.get_agent_workspace_runtime_readiness(workspace_id)
        if readiness is not None:
            payload["readiness"] = readiness
        return payload

    def _normalize_destination_payload(
        *,
        kind: str,
        image: Optional[str],
        name: Optional[str],
        env: Optional[list[str]],
        env_map: Optional[list[str]],
        mount: Optional[list[str]],
        mount_ro: Optional[list[str]],
        profile: Optional[str],
        workdir: Optional[str],
    ) -> dict[str, Any]:
        normalized_kind = kind.strip().lower()
        destination: dict[str, Any]
        if normalized_kind == "local":
            destination = {"kind": "local"}
        elif normalized_kind == "docker":
            if not isinstance(image, str) or not image.strip():
                raise_exit("image is required for docker destination")
            destination = {"kind": "docker", "image": image.strip()}
            if isinstance(name, str) and name.strip():
                destination["container_name"] = name.strip()
            env_passthrough = [item.strip() for item in (env or []) if item.strip()]
            if env_passthrough:
                destination["env_passthrough"] = env_passthrough
            explicit_env: dict[str, str] = {}
            for entry in env_map or []:
                env_key, env_value = _parse_env_map_ref(entry)
                explicit_env[env_key] = env_value
            if explicit_env:
                destination["env"] = explicit_env
            mounts: list[dict[str, Any]] = [
                _parse_mount_ref(item) for item in (mount or [])
            ]
            mounts.extend(
                {
                    **_parse_mount_ref(item),
                    "read_only": True,
                }
                for item in (mount_ro or [])
            )
            if mounts:
                destination["mounts"] = mounts
            if isinstance(profile, str) and profile.strip():
                destination["profile"] = profile.strip()
            if isinstance(workdir, str) and workdir.strip():
                destination["workdir"] = workdir.strip()
        else:
            raise_exit(
                f"Unsupported destination kind: {kind!r}. Use 'local' or 'docker'."
            )

        validated = validate_destination_write_payload(
            destination, context="destination"
        )
        if not validated.valid or validated.normalized_destination is None:
            raise_exit("; ".join(validated.errors) or "Invalid destination payload")
        return validated.normalized_destination

    def _parse_mount_ref(value: str) -> dict[str, str]:
        source, sep, target = value.partition(":")
        source = source.strip()
        target = target.strip()
        if sep != ":" or not source or not target:
            raise_exit(
                f"Invalid --mount value: {value!r}. Expected format source:target"
            )
        return {"source": source, "target": target}

    def _parse_env_map_ref(value: str) -> tuple[str, str]:
        key, sep, raw_value = value.partition("=")
        key = key.strip()
        if sep != "=" or not key:
            raise_exit(f"Invalid --env-map value: {value!r}. Expected format KEY=VALUE")
        return key, raw_value

    def _with_hub_path(command: str, hub_root: Path) -> str:
        return f"{command} --path {shlex.quote(str(hub_root))}"

    @destination_app.command("show")
    def hub_destination_show(
        repo_id: str = typer.Argument(..., help="Repo id from hub manifest"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Show effective execution destination for a repo.

        Examples:
        - Update destination config:
          `car hub destination set --help`
        - Deep docs:
          `docs/configuration/destinations.md`
        """
        config = require_hub_config(path)
        manifest, repos_by_id, repo = _resolve_repo_entry(config, repo_id)
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        issues = _destination_issues(
            manifest,
            repo_id=repo.id,
            resolution_issues=resolution.issues,
        )
        payload = {
            "repo_id": repo.id,
            "kind": repo.kind,
            "worktree_of": repo.worktree_of,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": issues,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"Repo: {repo.id}")
        typer.echo(f"Kind: {repo.kind}")
        if repo.worktree_of:
            typer.echo(f"Worktree of: {repo.worktree_of}")
        configured = (
            json.dumps(repo.destination, sort_keys=True)
            if isinstance(repo.destination, dict)
            else "<none>"
        )
        typer.echo(f"Configured destination: {configured}")
        typer.echo(f"Effective destination (source={resolution.source}):")
        typer.echo(
            json.dumps(payload["effective_destination"], indent=2, sort_keys=True)
        )
        if issues:
            typer.echo("Validation issues:")
            for issue in issues:
                typer.echo(f"- {issue}")

    @destination_app.command("set")
    def hub_destination_set(
        repo_id: str = typer.Argument(..., help="Repo id from hub manifest"),
        kind: str = typer.Argument(..., help="Destination kind (local|docker)"),
        image: Optional[str] = typer.Option(
            None,
            "--image",
            help=(
                "Docker image ref (required for docker kind; supports custom images "
                "like ghcr.io/org/dev-image:tag)"
            ),
        ),
        name: Optional[str] = typer.Option(
            None, "--name", help="Docker container name override"
        ),
        env: Optional[list[str]] = typer.Option(
            None,
            "--env",
            help="Repeat to add env passthrough patterns (example: CAR_*)",
        ),
        env_map: Optional[list[str]] = typer.Option(
            None,
            "--env-map",
            help="Repeat explicit docker env entries using KEY=VALUE format",
        ),
        mount: Optional[list[str]] = typer.Option(
            None,
            "--mount",
            help="Repeat bind mount entries using source:target format",
        ),
        mount_ro: Optional[list[str]] = typer.Option(
            None,
            "--mount-ro",
            help="Repeat read-only bind mount entries using source:target format",
        ),
        profile: Optional[str] = typer.Option(
            None,
            "--profile",
            help="Docker runtime profile (currently supported: full-dev)",
        ),
        workdir: Optional[str] = typer.Option(
            None, "--workdir", help="Docker workdir override inside the container"
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Set repo execution destination.

        Examples:
        - Bring your own image:
          `car hub destination set <repo_id> docker --image ghcr.io/org/dev-image:tag --path <hub_root>`
        - Inspect advanced runtime flags:
          `car hub destination set --help`
        - Deep docs:
          `docs/configuration/destinations.md`
        """
        config = require_hub_config(path)
        manifest, repos_by_id, repo = _resolve_repo_entry(config, repo_id)
        normalized_destination = _normalize_destination_payload(
            kind=kind,
            image=image,
            name=name,
            env=env,
            env_map=env_map,
            mount=mount,
            mount_ro=mount_ro,
            profile=profile,
            workdir=workdir,
        )
        repo.destination = normalized_destination
        save_manifest(config.manifest_path, manifest, config.root)

        manifest, repos_by_id, repo = _resolve_repo_entry(config, repo_id)
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        issues = _destination_issues(
            manifest,
            repo_id=repo.id,
            resolution_issues=resolution.issues,
        )
        payload = {
            "repo_id": repo.id,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": issues,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(
            f"Updated destination for {repo.id} to "
            f"{resolution.destination.kind} (source={resolution.source})"
        )

    @agent_workspace_app.command("list")
    def hub_agent_workspace_list(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """List managed agent workspaces."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        snapshots = supervisor.list_agent_workspaces(use_cache=False)
        payload = [
            _agent_workspace_payload(config, supervisor, snapshot.id)
            for snapshot in snapshots
        ]
        if output_json:
            typer.echo(json.dumps({"agent_workspaces": payload}, indent=2))
            return
        if not payload:
            typer.echo("No agent workspaces found.")
            return
        typer.echo(f"Agent workspaces ({len(payload)}):")
        for item in payload:
            typer.echo(
                "  - {id} (runtime={runtime}, enabled={enabled}, exists={exists_on_disk}, path={path})".format(
                    **item
                )
            )

    @agent_workspace_app.command("show")
    def hub_agent_workspace_show(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """Show effective destination and readiness for one agent workspace."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        payload = _agent_workspace_payload(config, supervisor, workspace_id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(f"Agent workspace: {payload['id']}")
        typer.echo(f"Runtime: {payload['runtime']}")
        typer.echo(f"Display name: {payload['display_name']}")
        typer.echo(f"Enabled: {payload['enabled']}")
        typer.echo(f"Path: {payload['path']}")
        typer.echo(f"Effective destination (source={payload['source']}):")
        typer.echo(
            json.dumps(payload["effective_destination"], indent=2, sort_keys=True)
        )
        readiness = payload.get("readiness")
        if isinstance(readiness, dict):
            typer.echo(
                "Readiness: {status}{version}".format(
                    status=str(readiness.get("status") or "unknown"),
                    version=(
                        f" (version={readiness['version']})"
                        if readiness.get("version")
                        else ""
                    ),
                )
            )
            message = str(readiness.get("message") or "").strip()
            if message:
                typer.echo(f"Readiness detail: {message}")
        if payload["issues"]:
            typer.echo("Validation issues:")
            for issue in payload["issues"]:
                typer.echo(f"- {issue}")

    @agent_workspace_app.command("create")
    def hub_agent_workspace_create(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        runtime: str = typer.Option(..., "--runtime", help="Runtime id"),
        display_name: Optional[str] = typer.Option(
            None, "--name", help="Display name override"
        ),
        enabled: bool = typer.Option(
            True,
            "--enabled/--disabled",
            help="Enable the workspace immediately (preflight runs when enabled).",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """Create a first-class managed agent workspace."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        try:
            snapshot = supervisor.create_agent_workspace(
                workspace_id=workspace_id,
                runtime=runtime,
                display_name=display_name,
                enabled=enabled,
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        payload = _agent_workspace_payload(config, supervisor, snapshot.id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(
            f"Created agent workspace {snapshot.id} (runtime={snapshot.runtime}, enabled={snapshot.enabled}) at {snapshot.path}"
        )

    @agent_workspace_app.command("update")
    def hub_agent_workspace_update(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        display_name: Optional[str] = typer.Option(
            None, "--name", help="Display name override"
        ),
        enabled: Optional[bool] = typer.Option(
            None,
            "--enabled/--disabled",
            help="Enable or disable the workspace",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """Update non-destination agent workspace settings."""
        if enabled is None and display_name is None:
            raise_exit("Provide at least one of --name or --enabled/--disabled.")
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        try:
            supervisor.update_agent_workspace(
                workspace_id,
                enabled=enabled,
                display_name=display_name,
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        payload = _agent_workspace_payload(config, supervisor, workspace_id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(
            f"Updated agent workspace {workspace_id} (enabled={payload['enabled']}, display_name={payload['display_name']})"
        )

    @agent_workspace_app.command("remove")
    def hub_agent_workspace_remove(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        delete_files: bool = typer.Option(
            False,
            "--delete-files",
            help="Also delete the workspace directory from disk.",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """Remove an agent workspace from the manifest.

        By default this keeps the workspace files on disk.
        """
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        try:
            supervisor.remove_agent_workspace(workspace_id, delete_dir=delete_files)
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        payload = {
            "status": "ok",
            "workspace_id": workspace_id,
            "delete_dir": delete_files,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        action = "Removed and deleted files for" if delete_files else "Removed"
        typer.echo(f"{action} agent workspace {workspace_id}")

    @agent_workspace_destination_app.command("show")
    def hub_agent_workspace_destination_show(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """Show effective execution destination for an agent workspace."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        payload = _agent_workspace_payload(config, supervisor, workspace_id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(f"Agent workspace: {workspace_id}")
        configured = (
            json.dumps(payload["configured_destination"], sort_keys=True)
            if isinstance(payload["configured_destination"], dict)
            else "<none>"
        )
        typer.echo(f"Configured destination: {configured}")
        typer.echo(f"Effective destination (source={payload['source']}):")
        typer.echo(
            json.dumps(payload["effective_destination"], indent=2, sort_keys=True)
        )

    @agent_workspace_destination_app.command("set")
    def hub_agent_workspace_destination_set(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        kind: str = typer.Argument(..., help="Destination kind (local|docker)"),
        image: Optional[str] = typer.Option(
            None, "--image", help="Docker image ref for docker destinations"
        ),
        name: Optional[str] = typer.Option(
            None, "--name", help="Docker container name override"
        ),
        env: Optional[list[str]] = typer.Option(
            None,
            "--env",
            help="Repeat to add env passthrough patterns (example: CAR_*)",
        ),
        env_map: Optional[list[str]] = typer.Option(
            None,
            "--env-map",
            help="Repeat explicit docker env entries using KEY=VALUE format",
        ),
        mount: Optional[list[str]] = typer.Option(
            None,
            "--mount",
            help="Repeat bind mount entries using source:target format",
        ),
        mount_ro: Optional[list[str]] = typer.Option(
            None,
            "--mount-ro",
            help="Repeat read-only bind mount entries using source:target format",
        ),
        profile: Optional[str] = typer.Option(
            None, "--profile", help="Docker runtime profile"
        ),
        workdir: Optional[str] = typer.Option(
            None, "--workdir", help="Docker workdir override inside the container"
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON payload"),
    ):
        """Set execution destination for an agent workspace."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        normalized_destination = _normalize_destination_payload(
            kind=kind,
            image=image,
            name=name,
            env=env,
            env_map=env_map,
            mount=mount,
            mount_ro=mount_ro,
            profile=profile,
            workdir=workdir,
        )
        try:
            supervisor.set_agent_workspace_destination(
                workspace_id, normalized_destination
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        payload = _agent_workspace_payload(config, supervisor, workspace_id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(
            f"Updated destination for {workspace_id} to {payload['effective_destination']['kind']} (source={payload['source']})"
        )

    @orchestration_app.command("verify")
    def orchestration_verify(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Verify orchestration state migration parity between legacy stores and orchestration.sqlite3.

        This command compares row counts, representative IDs, and content hashes
        between legacy PMA stores and the new orchestration SQLite database.

        Examples:
        - `car hub orchestration verify --path <hub_root>`
        - `car hub orchestration verify --path <hub_root> --json`
        """
        config = require_hub_config(path)
        try:
            with open_orchestration_sqlite(config.root, migrate=False) as conn:
                summary = verify_migration(config.root, conn)
        except Exception as exc:
            raise_exit(f"Migration verification failed: {exc}", cause=exc)
        if output_json:
            typer.echo(json.dumps(summary.to_dict(), indent=2))
            return
        typer.echo("Migration Verification Summary")
        typer.echo("=" * 50)
        typer.echo(f"Run ID: {summary.run_id}")
        typer.echo(f"Status: {summary.status}")
        typer.echo(f"Overall Passed: {summary.overall_passed}")
        typer.echo(f"Rollback Available: {summary.rollback_available}")
        typer.echo("")
        typer.echo("Thread Parity:")
        for check in summary.thread_parity:
            status_icon = "✓" if check.status == "passed" else "✗"
            typer.echo(f"  {status_icon} {check.check_name}: {check.message}")
        typer.echo("")
        typer.echo("Automation Parity:")
        for check in summary.automation_parity:
            status_icon = "✓" if check.status == "passed" else "✗"
            typer.echo(f"  {status_icon} {check.check_name}: {check.message}")
        typer.echo("")
        typer.echo("Queue Parity:")
        for check in summary.queue_parity:
            status_icon = "✓" if check.status == "passed" else "✗"
            typer.echo(f"  {status_icon} {check.check_name}: {check.message}")
        typer.echo("")
        tp = summary.transcript_parity
        if tp:
            status_icon = "✓" if tp.status == "passed" else "✗"
            typer.echo("Transcript Parity:")
            typer.echo(f"  {status_icon} {tp.check_name}: {tp.message}")
        typer.echo("")
        typer.echo("Event Parity:")
        for check in summary.event_parity:
            status_icon = "✓" if check.status == "passed" else "✗"
            typer.echo(f"  {status_icon} {check.check_name}: {check.message}")
        typer.echo("")
        ap = summary.audit_parity
        status_icon = "✓" if ap.status == "passed" else "✗"
        typer.echo("Audit Parity:")
        typer.echo(f"  {status_icon} {ap.check_name}: {ap.message}")
        typer.echo("")
        typer.echo("Recommendations:")
        for rec in summary.recommendations:
            typer.echo(f"  - {rec}")

    @orchestration_app.command("status")
    def orchestration_status(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Show orchestration SQLite state and migration status.

        Displays schema version, migration history, and table statistics.
        """
        config = require_hub_config(path)
        from ....core.orchestration.migrate_legacy_state import (
            LEGACY_PMA_AUDIT_LOG_PATH,
            LEGACY_PMA_AUTOMATION_PATH,
            LEGACY_PMA_LIFECYCLE_LOG_PATH,
            LEGACY_PMA_QUEUE_DIR,
            LEGACY_PMA_THREADS_DB_PATH,
            LEGACY_PMA_TRANSCRIPTS_DIR,
        )
        from ....core.orchestration.migrations import (
            ORCHESTRATION_SCHEMA_VERSION,
            current_orchestration_schema_version,
        )

        try:
            with open_orchestration_sqlite(config.root, migrate=False) as conn:
                current_version = current_orchestration_schema_version(conn)
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'orch_%' ORDER BY name"
                ).fetchall()
                table_counts = {}
                for row in tables:
                    table_name = row["name"]
                    cnt = conn.execute(
                        f"SELECT COUNT(*) as cnt FROM {table_name}"
                    ).fetchone()
                    table_counts[table_name] = int(cnt["cnt"]) if cnt else 0
        except Exception as exc:
            raise_exit(f"Failed to read orchestration state: {exc}", cause=exc)
        legacy_status = {
            "threads_db": (config.root / LEGACY_PMA_THREADS_DB_PATH).exists(),
            "automation": (config.root / LEGACY_PMA_AUTOMATION_PATH).exists(),
            "queue": (config.root / LEGACY_PMA_QUEUE_DIR).exists(),
            "transcripts": (config.root / LEGACY_PMA_TRANSCRIPTS_DIR).exists(),
            "audit_log": (config.root / LEGACY_PMA_AUDIT_LOG_PATH).exists(),
            "lifecycle": (config.root / LEGACY_PMA_LIFECYCLE_LOG_PATH).exists(),
        }
        payload = {
            "schema_version": current_version,
            "target_version": ORCHESTRATION_SCHEMA_VERSION,
            "tables": table_counts,
            "legacy_stores": legacy_status,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo("Orchestration Status")
        typer.echo("=" * 50)
        typer.echo(
            f"Schema Version: {current_version} / {ORCHESTRATION_SCHEMA_VERSION}"
        )
        typer.echo("")
        typer.echo("Table Counts:")
        for table, count in sorted(table_counts.items()):
            typer.echo(f"  {table}: {count}")
        typer.echo("")
        typer.echo("Legacy Stores Available:")
        for store, exists in legacy_status.items():
            status = "yes" if exists else "no"
            typer.echo(f"  {store}: {status}")

    @hub_app.command("create")
    def hub_create(
        repo_id: str = typer.Argument(
            ..., help="Base repo id to create and initialize"
        ),
        repo_path: Optional[Path] = typer.Option(
            None,
            "--repo-path",
            help="Custom repo path relative to hub repos_root",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        force: bool = typer.Option(False, "--force", help="Allow existing directory"),
        git_init: bool = typer.Option(
            True, "--git-init/--no-git-init", help="Run git init in the new repo"
        ),
    ):
        """Create and register a new hub repo workspace."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        try:
            snapshot = supervisor.create_repo(
                repo_id, repo_path, git_init=git_init, force=force
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        typer.echo(f"Created repo {snapshot.id} at {snapshot.path}")

    @hub_app.command("clone")
    def hub_clone(
        git_url: str = typer.Option(
            ..., "--git-url", help="Git URL or local path to clone"
        ),
        repo_id: Optional[str] = typer.Option(
            None, "--id", help="Repo id to register (defaults from git URL)"
        ),
        repo_path: Optional[Path] = typer.Option(
            None,
            "--repo-path",
            help="Custom repo path relative to hub repos_root",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        force: bool = typer.Option(False, "--force", help="Allow existing directory"),
    ):
        """Clone a repository into the hub and register it in the manifest."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        try:
            snapshot = supervisor.clone_repo(
                git_url=git_url, repo_id=repo_id, repo_path=repo_path, force=force
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        typer.echo(
            f"Cloned repo {snapshot.id} at {snapshot.path} (status={snapshot.status.value})"
        )

    @hub_app.command("serve")
    def hub_serve(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        host: Optional[str] = typer.Option(None, "--host", help="Host to bind"),
        port: Optional[int] = typer.Option(None, "--port", help="Port to bind"),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Base path for the server"
        ),
    ):
        """Start the hub API/UI server for repo and PMA operations."""
        config = require_hub_config(path)
        normalized_base = (
            normalize_base_path(base_path)
            if base_path is not None
            else config.server_base_path
        )
        bind_host = host or config.server_host
        bind_port = port or config.server_port
        enforce_bind_auth(bind_host, config.server_auth_token_env)
        typer.echo(
            f"Serving hub on http://{bind_host}:{bind_port}{normalized_base or ''}"
        )
        uvicorn.run(
            create_hub_app(config.root, base_path=normalized_base),
            host=bind_host,
            port=bind_port,
            root_path="",
            access_log=config.server_access_log,
        )

    @hub_app.command("scan")
    def hub_scan(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    ):
        """Scan repos/worktrees from disk and print canonical follow-up commands."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        snapshots = supervisor.scan()
        typer.echo(f"Scanned hub at {config.root} (repos_root={config.repos_root})")
        for snap in snapshots:
            hint = (
                _with_hub_path(f"car hub worktree archive {snap.id}", config.root)
                if snap.kind == "worktree"
                else _with_hub_path(f"car hub destination show {snap.id}", config.root)
            )
            typer.echo(
                f"- {snap.id}: {snap.status.value}, initialized={snap.initialized}, exists={snap.exists_on_disk}, recommended={hint}"
            )

    @hub_app.command("snapshot")
    def hub_snapshot(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            True, "--json/--no-json", help="Emit JSON output (default: true)"
        ),
        pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output"),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override hub server base path (e.g. /car)"
        ),
    ):
        """Fetch a compact hub snapshot (repos + inbox + run-state recommendations)."""
        config = require_hub_config(path)
        repos_url = build_server_url(config, "/hub/repos", base_path_override=base_path)
        messages_url = build_server_url(
            config, "/hub/messages?limit=50", base_path_override=base_path
        )

        try:
            repos_response = request_json(
                "GET", repos_url, token_env=config.server_auth_token_env
            )
            messages_response = request_json(
                "GET", messages_url, token_env=config.server_auth_token_env
            )
        except Exception as exc:
            raise_exit(
                "Failed to connect to hub server. Ensure 'car hub serve' is running.\n"
                f"Attempted:\n- {repos_url}\n- {messages_url}\n"
                "If the hub UI is served under a base path (commonly /car), either set "
                "`server.base_path` in the hub config or pass `--base-path /car`.",
                cause=exc,
            )

        repos_payload = repos_response if isinstance(repos_response, dict) else {}
        messages_payload = (
            messages_response if isinstance(messages_response, dict) else {}
        )

        repos = (
            repos_payload.get("repos", []) if isinstance(repos_payload, dict) else []
        )
        messages_items = (
            messages_payload.get("items", [])
            if isinstance(messages_payload, dict)
            else []
        )

        def _summarize_repo(repo: dict) -> dict:
            if not isinstance(repo, dict):
                return {}
            ticket_flow = (
                repo.get("ticket_flow")
                if isinstance(repo.get("ticket_flow"), dict)
                else {}
            )
            failure = (
                ticket_flow.get("failure") if isinstance(ticket_flow, dict) else None
            )
            failure_summary = (
                ticket_flow.get("failure_summary")
                if isinstance(ticket_flow, dict)
                else None
            )
            pr_url = (
                ticket_flow.get("pr_url") if isinstance(ticket_flow, dict) else None
            )
            final_review_status = (
                ticket_flow.get("final_review_status")
                if isinstance(ticket_flow, dict)
                else None
            )
            run_state = repo.get("run_state")
            if not isinstance(run_state, dict):
                run_state = {}
            canonical = repo.get("canonical_state_v1")
            if not isinstance(canonical, dict):
                canonical = {}
            return {
                "id": repo.get("id"),
                "display_name": repo.get("display_name"),
                "status": repo.get("status"),
                "initialized": repo.get("initialized"),
                "exists_on_disk": repo.get("exists_on_disk"),
                "last_run_id": repo.get("last_run_id"),
                "last_run_started_at": repo.get("last_run_started_at"),
                "last_run_finished_at": repo.get("last_run_finished_at"),
                "failure": failure,
                "failure_summary": failure_summary,
                "pr_url": pr_url,
                "final_review_status": final_review_status,
                "run_state": {
                    "state": run_state.get("state"),
                    "blocking_reason": run_state.get("blocking_reason"),
                    "current_ticket": run_state.get("current_ticket"),
                    "last_progress_at": run_state.get("last_progress_at"),
                    "recommended_action": run_state.get("recommended_action"),
                },
                "freshness": canonical.get("freshness"),
            }

        def _summarize_message(msg: dict) -> dict:
            if not isinstance(msg, dict):
                return {}
            dispatch = msg.get("dispatch", {})
            if not isinstance(dispatch, dict):
                dispatch = {}
            body = dispatch.get("body", "")
            title = dispatch.get("title", "")
            truncated_body = (body[:200] + "...") if len(body) > 200 else body
            run_state = msg.get("run_state")
            if not isinstance(run_state, dict):
                run_state = {}
            canonical = msg.get("canonical_state_v1")
            if not isinstance(canonical, dict):
                canonical = {}
            return {
                "item_type": msg.get("item_type"),
                "next_action": msg.get("next_action"),
                "repo_id": msg.get("repo_id"),
                "repo_display_name": msg.get("repo_display_name"),
                "run_id": msg.get("run_id"),
                "run_created_at": msg.get("run_created_at"),
                "status": msg.get("status"),
                "seq": msg.get("seq"),
                "dispatch": {
                    "mode": dispatch.get("mode"),
                    "title": title,
                    "body": truncated_body,
                    "is_handoff": dispatch.get("is_handoff"),
                },
                "files_count": (
                    len(msg.get("files", []))
                    if isinstance(msg.get("files"), list)
                    else 0
                ),
                "reason": msg.get("reason"),
                "run_state": {
                    "state": run_state.get("state"),
                    "blocking_reason": run_state.get("blocking_reason"),
                    "current_ticket": run_state.get("current_ticket"),
                    "last_progress_at": run_state.get("last_progress_at"),
                    "recommended_action": run_state.get("recommended_action"),
                },
                "freshness": canonical.get("freshness"),
            }

        snapshot = {
            "generated_at": repos_payload.get("generated_at")
            or messages_payload.get("generated_at"),
            "last_scan_at": (
                repos_payload.get("last_scan_at")
                if isinstance(repos_payload, dict)
                else None
            ),
            "freshness": {
                "repos": (
                    repos_payload.get("freshness")
                    if isinstance(repos_payload, dict)
                    else None
                ),
                "inbox": (
                    messages_payload.get("freshness")
                    if isinstance(messages_payload, dict)
                    else None
                ),
            },
            "repos": [_summarize_repo(repo) for repo in repos],
            "inbox_items": [_summarize_message(msg) for msg in messages_items],
        }

        snapshot_repos = snapshot.get("repos", []) or []
        snapshot_inbox = snapshot.get("inbox_items", []) or []
        if not isinstance(snapshot_repos, list):
            snapshot_repos = []
        if not isinstance(snapshot_inbox, list):
            snapshot_inbox = []

        if not output_json:
            typer.echo(
                f"Hub Snapshot (repos={len(snapshot_repos)}, inbox={len(snapshot_inbox)})"
            )
            if snapshot.get("generated_at"):
                typer.echo(f"generated_at: {snapshot.get('generated_at')}")
            for repo in snapshot_repos:
                if not isinstance(repo, dict):
                    continue
                pr_url = repo.get("pr_url")
                final_review_status = repo.get("final_review_status")
                run_state: dict = {}
                rs = repo.get("run_state")
                if isinstance(rs, dict):
                    run_state = rs
                typer.echo(
                    f"- {repo.get('id')}: status={repo.get('status')}, "
                    f"initialized={repo.get('initialized')}, exists={repo.get('exists_on_disk')}, "
                    f"final_review={final_review_status}, pr_url={pr_url}, "
                    f"run_state={run_state.get('state')}"
                )
                if run_state.get("blocking_reason"):
                    typer.echo(f"  blocking_reason: {run_state.get('blocking_reason')}")
                if run_state.get("recommended_action"):
                    typer.echo(
                        f"  recommended_action: {run_state.get('recommended_action')}"
                    )
                freshness = repo.get("freshness")
                if isinstance(freshness, dict) and freshness.get("basis_at"):
                    typer.echo(
                        "  freshness: "
                        f"{freshness.get('status')} basis={freshness.get('recency_basis')} "
                        f"basis_at={freshness.get('basis_at')}"
                    )
            for msg in snapshot_inbox:
                if not isinstance(msg, dict):
                    continue
                run_state_inbox: dict = {}
                rs = msg.get("run_state")
                if isinstance(rs, dict):
                    run_state_inbox = rs
                typer.echo(
                    f"- Inbox: repo={msg.get('repo_id')}, run_id={msg.get('run_id')}, "
                    f"title={msg.get('dispatch', {}).get('title')}, state={run_state_inbox.get('state')}"
                )
                if run_state_inbox.get("blocking_reason"):
                    typer.echo(
                        f"  blocking_reason: {run_state_inbox.get('blocking_reason')}"
                    )
                if run_state_inbox.get("recommended_action"):
                    typer.echo(
                        f"  recommended_action: {run_state_inbox.get('recommended_action')}"
                    )
                freshness = msg.get("freshness")
                if isinstance(freshness, dict) and freshness.get("basis_at"):
                    typer.echo(
                        "  freshness: "
                        f"{freshness.get('status')} basis={freshness.get('recency_basis')} "
                        f"basis_at={freshness.get('basis_at')}"
                    )
            return

        indent = 2 if pretty else None
        typer.echo(json.dumps(snapshot, indent=indent))
