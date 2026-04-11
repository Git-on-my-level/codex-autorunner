import json
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, cast
from urllib.parse import urlencode

import httpx
import typer
import uvicorn

from ....core.config import HubConfig
from ....core.destinations import (
    resolve_effective_agent_workspace_destination,
    resolve_effective_repo_destination,
    validate_destination_write_payload,
)
from ....core.hub import HubSupervisor
from ....core.hub_diagnostics import read_hub_endpoint
from ....core.orchestration import verify_migration
from ....core.orchestration.sqlite import open_orchestration_sqlite
from ....core.pma_automation_store import PmaAutomationStore
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
    subscription_app = typer.Typer(
        add_completion=False,
        help="Inspect and manage hub PMA lifecycle subscriptions.",
    )
    hub_app.add_typer(subscription_app, name="subscription")

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

    def _subscription_store(config: HubConfig) -> PmaAutomationStore:
        return PmaAutomationStore(config.root)

    def _normalize_subscription_state_filter(value: str) -> str:
        normalized = str(value or "").strip().lower() or "all"
        if normalized not in {"active", "cancelled", "all"}:
            raise_exit(
                "Unsupported subscription state filter. Use active, cancelled, or all."
            )
        return normalized

    def _truncate_table_cell(value: Any, *, width: int) -> str:
        if isinstance(value, list):
            text = ",".join(str(item) for item in value if item is not None) or "-"
        else:
            text = str(value).strip() if value is not None else "-"
            if not text:
                text = "-"
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return f"{text[: width - 3]}..."

    def _subscription_matches_text(subscription: dict[str, Any]) -> str:
        match_count = subscription.get("match_count")
        max_matches = subscription.get("max_matches")
        count_text = str(match_count if match_count is not None else 0)
        max_text = str(max_matches) if max_matches is not None else "-"
        return f"{count_text}/{max_text}"

    def _render_subscription_table(subscriptions: list[dict[str, Any]]) -> list[str]:
        columns = [
            ("ID", "subscription_id", 36),
            ("STATE", "state", 10),
            ("EVENTS", "event_types", 20),
            ("THREAD", "thread_id", 18),
            ("LANE", "lane_id", 18),
            ("MATCHES", "__matches__", 9),
            ("UPDATED", "updated_at", 20),
        ]
        rows: list[dict[str, str]] = []
        for subscription in subscriptions:
            rows.append(
                {
                    "ID": _truncate_table_cell(
                        subscription.get("subscription_id"), width=36
                    ),
                    "STATE": _truncate_table_cell(subscription.get("state"), width=10),
                    "EVENTS": _truncate_table_cell(
                        subscription.get("event_types"),
                        width=20,
                    ),
                    "THREAD": _truncate_table_cell(
                        subscription.get("thread_id"), width=18
                    ),
                    "LANE": _truncate_table_cell(subscription.get("lane_id"), width=18),
                    "MATCHES": _truncate_table_cell(
                        _subscription_matches_text(subscription),
                        width=9,
                    ),
                    "UPDATED": _truncate_table_cell(
                        subscription.get("updated_at"), width=20
                    ),
                }
            )
        widths: dict[str, int] = {}
        for header, _key, max_width in columns:
            cell_lengths = [len(row[header]) for row in rows] if rows else [0]
            widths[header] = min(max(max(cell_lengths), len(header)), max_width)
        header_line = "  ".join(
            header.ljust(widths[header]) for header, _, _ in columns
        )
        separator_line = "  ".join("-" * widths[header] for header, _, _ in columns)
        lines = [header_line, separator_line]
        for row in rows:
            lines.append(
                "  ".join(row[header].ljust(widths[header]) for header, _, _ in columns)
            )
        return lines

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
            if not isinstance(image, str):
                raise_exit("image is required for docker destination")
            image_ref = cast(str, image).strip()
            if not image_ref:
                raise_exit("image is required for docker destination")
            destination = {"kind": "docker", "image": image_ref}
            if isinstance(name, str):
                container_name = name.strip()
                if container_name:
                    destination["container_name"] = container_name
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
            if isinstance(profile, str):
                profile_value = profile.strip()
                if profile_value:
                    destination["profile"] = profile_value
            if isinstance(workdir, str):
                workdir_value = workdir.strip()
                if workdir_value:
                    destination["workdir"] = workdir_value
        else:
            raise_exit(
                f"Unsupported destination kind: {kind!r}. Use 'local' or 'docker'."
            )

        validated = validate_destination_write_payload(
            destination, context="destination"
        )
        normalized_destination = validated.normalized_destination
        if not validated.valid:
            raise_exit("; ".join(validated.errors) or "Invalid destination payload")
        if normalized_destination is None:
            raise_exit("; ".join(validated.errors) or "Invalid destination payload")
        return cast(dict[str, Any], normalized_destination)

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

    @subscription_app.command("list", help="List hub PMA lifecycle subscriptions.")
    def hub_subscription_list(
        state: str = typer.Option(
            "all",
            "--state",
            help="State filter (active/cancelled/all)",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="JSON output"),
    ) -> None:
        config = require_hub_config(path)
        store = _subscription_store(config)
        state_filter = _normalize_subscription_state_filter(state)
        subscriptions = store.list_subscriptions(
            include_inactive=state_filter in {"cancelled", "all"}
        )
        if state_filter != "all":
            subscriptions = [
                entry
                for entry in subscriptions
                if str(entry.get("state") or "").strip().lower() == state_filter
            ]
        payload = {
            "subscriptions": subscriptions,
            "count": len(subscriptions),
            "state": state_filter,
            "store_path": str(store.path),
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"Subscriptions ({len(subscriptions)}) state={state_filter}")
        if not subscriptions:
            typer.echo("No subscriptions.")
            return
        for line in _render_subscription_table(subscriptions):
            typer.echo(line)

    @subscription_app.command("cancel", help="Cancel a hub PMA lifecycle subscription.")
    def hub_subscription_cancel(
        subscription_id: str = typer.Option(
            ...,
            "--id",
            help="Subscription id to cancel.",
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ) -> None:
        config = require_hub_config(path)
        store = _subscription_store(config)
        normalized_id = str(subscription_id or "").strip()
        if not normalized_id:
            raise_exit("subscription id is required")
        subscriptions = store.list_subscriptions(include_inactive=True)
        existing = next(
            (
                entry
                for entry in subscriptions
                if str(entry.get("subscription_id") or "").strip() == normalized_id
            ),
            None,
        )
        if existing is None:
            raise_exit(f"Subscription not found: {normalized_id}")
        changed = store.cancel_subscription(normalized_id)
        payload = {
            "subscription_id": normalized_id,
            "changed": changed,
            "state": "cancelled",
            "status": "cancelled" if changed else "already_cancelled",
            "store_path": str(store.path),
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        if changed:
            typer.echo(f"Cancelled {normalized_id}")
            return
        typer.echo(f"Already cancelled: {normalized_id}")

    @subscription_app.command(
        "purge",
        help="Purge cancelled hub PMA lifecycle subscriptions from the store.",
    )
    def hub_subscription_purge(
        state: str = typer.Option(
            "cancelled",
            "--state",
            help="Subscription state to purge. Only cancelled is supported.",
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ) -> None:
        config = require_hub_config(path)
        store = _subscription_store(config)
        state_filter = str(state or "").strip().lower() or "cancelled"
        if state_filter != "cancelled":
            raise_exit("Unsupported purge state. Only cancelled is supported.")
        removed = store.purge_subscriptions(
            state_filter=state_filter,
            dry_run=dry_run,
        )
        payload = {
            "subscriptions": removed,
            "subscription_ids": [
                str(entry.get("subscription_id") or "")
                for entry in removed
                if str(entry.get("subscription_id") or "").strip()
            ],
            "count": len(removed),
            "state": state_filter,
            "dry_run": dry_run,
            "store_path": str(store.path),
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        action = "Would purge" if dry_run else "Purged"
        typer.echo(f"{action} {len(removed)} subscription(s) with state={state_filter}")
        if not removed:
            typer.echo("No matches.")
            return
        for line in _render_subscription_table(removed):
            typer.echo(line)

    @destination_app.command("show")
    def hub_destination_show(
        repo_id: str = typer.Argument(..., help="Repo id from hub manifest"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Show repo destination."""
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

        parts = [f"{repo.id} kind={repo.kind}"]
        if repo.worktree_of:
            parts.append(f"worktree_of={repo.worktree_of}")
        typer.echo(" ".join(parts))
        typer.echo(f"destination source={resolution.source}:")
        typer.echo(
            json.dumps(payload["effective_destination"], indent=2, sort_keys=True)
        )
        if issues:
            for issue in issues:
                typer.echo(f"issue: {issue}")

    @destination_app.command("set")
    def hub_destination_set(
        repo_id: str = typer.Argument(..., help="Repo id from hub manifest"),
        kind: str = typer.Argument(..., help="Destination kind (local|docker)"),
        image: Optional[str] = typer.Option(
            None,
            "--image",
            help=("Docker image ref"),
        ),
        name: Optional[str] = typer.Option(
            None, "--name", help="Docker container name override"
        ),
        env: Optional[list[str]] = typer.Option(
            None,
            "--env",
            help="Env passthrough patterns",
        ),
        env_map: Optional[list[str]] = typer.Option(
            None,
            "--env-map",
            help="KEY=VALUE env entries",
        ),
        mount: Optional[list[str]] = typer.Option(
            None,
            "--mount",
            help="Mount source:target",
        ),
        mount_ro: Optional[list[str]] = typer.Option(
            None,
            "--mount-ro",
            help="Read-only mount source:target",
        ),
        profile: Optional[str] = typer.Option(
            None,
            "--profile",
            help="Docker profile (full-dev)",
        ),
        workdir: Optional[str] = typer.Option(None, "--workdir", help="Docker workdir"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Set repo destination kind (local|docker).

        Example:
        `car hub destination set <repo_id> docker --image <image>`
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
            f"destination: {repo.id} -> {resolution.destination.kind} (source={resolution.source})"
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
            typer.echo("No workspaces.")
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
        typer.echo(
            f"{payload['id']} runtime={payload['runtime']} enabled={payload['enabled']}"
        )
        typer.echo(f"destination source={payload['source']}:")
        typer.echo(
            json.dumps(payload["effective_destination"], indent=2, sort_keys=True)
        )
        readiness = payload.get("readiness")
        if isinstance(readiness, dict):
            status = str(readiness.get("status") or "unknown")
            version = readiness.get("version")
            ver_part = f" version={version}" if version else ""
            typer.echo(f"readiness: {status}{ver_part}")
            message = str(readiness.get("message") or "").strip()
            if message:
                typer.echo(f"  {message}")
        if payload["issues"]:
            for issue in payload["issues"]:
                typer.echo(f"issue: {issue}")

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
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
            raise_exit(str(exc), cause=exc)
        payload = _agent_workspace_payload(config, supervisor, snapshot.id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(
            f"created workspace {snapshot.id} runtime={snapshot.runtime} at {snapshot.path}"
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
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
            raise_exit(str(exc), cause=exc)
        payload = _agent_workspace_payload(config, supervisor, workspace_id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(f"updated workspace {workspace_id} enabled={payload['enabled']}")

    @agent_workspace_app.command("remove")
    def hub_agent_workspace_remove(
        workspace_id: str = typer.Argument(..., help="Agent workspace id"),
        delete_files: bool = typer.Option(
            False,
            "--delete-files",
            help="Delete files too",
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
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
            raise_exit(str(exc), cause=exc)
        payload = {
            "status": "ok",
            "workspace_id": workspace_id,
            "delete_dir": delete_files,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(
            f"{'removed+deleted' if delete_files else 'removed'} workspace {workspace_id}"
        )

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
        typer.echo(f"workspace={workspace_id} source={payload['source']}")
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
            help="Env passthrough patterns",
        ),
        env_map: Optional[list[str]] = typer.Option(
            None,
            "--env-map",
            help="KEY=VALUE env entries",
        ),
        mount: Optional[list[str]] = typer.Option(
            None,
            "--mount",
            help="Mount source:target",
        ),
        mount_ro: Optional[list[str]] = typer.Option(
            None,
            "--mount-ro",
            help="Read-only mount source:target",
        ),
        profile: Optional[str] = typer.Option(
            None, "--profile", help="Docker profile (full-dev)"
        ),
        workdir: Optional[str] = typer.Option(None, "--workdir", help="Docker workdir"),
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
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
            raise_exit(str(exc), cause=exc)
        payload = _agent_workspace_payload(config, supervisor, workspace_id)
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(
            f"destination: {workspace_id} -> {payload['effective_destination']['kind']} (source={payload['source']})"
        )

    @orchestration_app.command("verify")
    def orchestration_verify(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        """Verify orchestration migration parity."""
        config = require_hub_config(path)
        try:
            with open_orchestration_sqlite(config.root, migrate=False) as conn:
                summary = verify_migration(config.root, conn)
        except (sqlite3.Error, OSError) as exc:  # intentional: top-level error handler
            raise_exit(f"Migration verification failed: {exc}", cause=exc)
        if output_json:
            typer.echo(json.dumps(summary.to_dict(), indent=2))
            return
        typer.echo(
            f"verify: {summary.status} passed={summary.overall_passed} run={summary.run_id}"
        )
        for check in (
            summary.thread_parity
            + summary.automation_parity
            + summary.queue_parity
            + summary.event_parity
        ):
            icon = "ok" if check.status == "passed" else "FAIL"
            typer.echo(f"  {icon} {check.check_name}: {check.message}")
        tp = summary.transcript_parity
        if tp:
            icon = "ok" if tp.status == "passed" else "FAIL"
            typer.echo(f"  {icon} {tp.check_name}: {tp.message}")
        ap = summary.audit_parity
        icon = "ok" if ap.status == "passed" else "FAIL"
        typer.echo(f"  {icon} {ap.check_name}: {ap.message}")
        if summary.recommendations:
            for rec in summary.recommendations:
                typer.echo(f"  rec: {rec}")

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
        except (sqlite3.Error, OSError) as exc:  # intentional: top-level error handler
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
        typer.echo(f"schema: {current_version}/{ORCHESTRATION_SCHEMA_VERSION}")
        for table, count in sorted(table_counts.items()):
            typer.echo(f"  {table}: {count}")
        for store, exists in legacy_status.items():
            typer.echo(f"  legacy_{store}: {'yes' if exists else 'no'}")

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
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
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
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
            raise_exit(str(exc), cause=exc)
        typer.echo(
            f"cloned {snapshot.id} at {snapshot.path} status={snapshot.status.value}"
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
            create_hub_app(
                config.root,
                base_path=normalized_base,
                endpoint_host=bind_host,
                endpoint_port=bind_port,
            ),
            host=bind_host,
            port=bind_port,
            root_path="",
            access_log=config.server_access_log,
        )

    @hub_app.command("endpoint")
    def hub_endpoint(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    ) -> None:
        """Print the currently running hub API base URL."""
        config = require_hub_config(path)
        endpoint = read_hub_endpoint(config.root)
        if endpoint is None:
            raise_exit("Hub not running.")
            return
        typer.echo(str(endpoint["url"]))

    @hub_app.command("scan")
    def hub_scan(
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    ):
        """Scan repos/worktrees from disk and print canonical follow-up commands."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        snapshots = supervisor.scan()
        typer.echo(f"scanned: {config.root}")
        for snap in snapshots:
            hint = (
                _with_hub_path(f"car hub worktree archive {snap.id}", config.root)
                if snap.kind == "worktree"
                else _with_hub_path(f"car hub destination show {snap.id}", config.root)
            )
            typer.echo(f"- {snap.id}: {snap.status.value} -> {hint}")

    @hub_app.command("cleanup")
    def hub_cleanup(
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview only, don't archive anything"
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        output_json: bool = typer.Option(
            True, "--json/--no-json", help="Emit JSON output (default: true)"
        ),
        pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output"),
    ):
        """Clean slate: archive unbound threads, eligible worktrees, and completed flow runs."""
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        try:
            result = supervisor.cleanup_all(dry_run=dry_run)
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:  # intentional: top-level error handler
            raise_exit(str(exc), cause=exc)
        if output_json:
            indent = 2 if pretty else None
            typer.echo(json.dumps(result, indent=indent, default=str))
        else:
            typer.echo(str(result.get("message", "Done")))

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
        section: list[str] = typer.Option(
            None,
            "--section",
            help=(
                "Return only specific snapshot sections "
                "(repeatable: repos, agent_workspaces, inbox, pma_threads, "
                "pma_files_detail, automation, action_queue)."
            ),
        ),
        timeout_seconds: float = typer.Option(
            15.0,
            "--timeout",
            min=1.0,
            help="HTTP timeout for hub snapshot requests in seconds.",
        ),
    ):
        """Fetch a compact hub snapshot (repos + inbox + run-state recommendations)."""

        def _normalize_sections(values: list[str]) -> list[str]:
            aliases = {"inbox_items": "inbox"}
            allowed = {
                "repos",
                "agent_workspaces",
                "inbox",
                "pma_threads",
                "pma_files_detail",
                "automation",
                "action_queue",
            }
            normalized: list[str] = []
            for value in values or []:
                candidate = (
                    aliases.get(str(value or "").strip().lower())
                    or str(value or "").strip().lower()
                )
                if not candidate:
                    continue
                if candidate not in allowed:
                    allowed_text = ", ".join(sorted(allowed))
                    raise_exit(
                        f"Unsupported --section {value!r}. Allowed: {allowed_text}."
                    )
                if candidate not in normalized:
                    normalized.append(candidate)
            return normalized

        def _append_query(url: str, params: dict[str, Any]) -> str:
            filtered = {
                key: value for key, value in params.items() if value not in {None, ""}
            }
            if not filtered:
                return url
            separator = "&" if "?" in url else "?"
            return f"{url}{separator}{urlencode(filtered)}"

        def _read_pma_threads_artifact(hub_root: Path) -> Optional[dict[str, Any]]:
            artifact_path = hub_root / ".codex-autorunner" / "pma_threads.json"
            if not artifact_path.exists():
                return None
            try:
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        requested_sections = _normalize_sections(section)
        config = require_hub_config(path)
        repos_url = build_server_url(config, "/hub/repos", base_path_override=base_path)
        messages_url = build_server_url(
            config, "/hub/messages", base_path_override=base_path
        )

        repos_response: dict[str, Any] = {}
        messages_response: dict[str, Any] = {}
        fetch_errors: list[dict[str, str]] = []

        repo_sections = [
            name for name in requested_sections if name in {"repos", "agent_workspaces"}
        ]
        message_sections = [
            name
            for name in requested_sections
            if name
            in {
                "inbox",
                "pma_threads",
                "pma_files_detail",
                "automation",
                "action_queue",
            }
        ]
        if not requested_sections:
            repo_sections = ["repos"]
            message_sections = ["inbox"]

        repos_request_url = _append_query(
            repos_url,
            {"sections": ",".join(repo_sections)} if repo_sections else {},
        )
        messages_request_url = _append_query(
            messages_url,
            (
                {
                    "limit": 50,
                    "sections": ",".join(message_sections),
                }
                if message_sections
                else {}
            ),
        )

        attempted_urls: list[str] = []
        if repo_sections:
            attempted_urls.append(repos_request_url)
            try:
                repos_response = request_json(
                    "GET",
                    repos_request_url,
                    token_env=config.server_auth_token_env,
                    timeout_seconds=timeout_seconds,
                )
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                httpx.HTTPError,
            ) as exc:  # intentional: best-effort
                fetch_errors.append(
                    {
                        "target": "repos",
                        "url": repos_request_url,
                        "error": str(exc) or exc.__class__.__name__,
                    }
                )
        if message_sections:
            attempted_urls.append(messages_request_url)
            try:
                messages_response = request_json(
                    "GET",
                    messages_request_url,
                    token_env=config.server_auth_token_env,
                    timeout_seconds=timeout_seconds,
                )
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                httpx.HTTPError,
            ) as exc:  # intentional: best-effort
                artifact_fallback = (
                    _read_pma_threads_artifact(config.root)
                    if requested_sections == ["pma_threads"]
                    else None
                )
                if artifact_fallback is not None:
                    messages_response = {
                        "generated_at": artifact_fallback.get("generated_at"),
                        "pma_threads": artifact_fallback.get("threads", []),
                    }
                else:
                    fetch_errors.append(
                        {
                            "target": "messages",
                            "url": messages_request_url,
                            "error": str(exc) or exc.__class__.__name__,
                        }
                    )

        if not repos_response and not messages_response and fetch_errors:
            error_msgs = "; ".join(error["error"] for error in fetch_errors)
            raise_exit(
                f"Hub server unreachable (timeout={timeout_seconds:.0f}s): {error_msgs}",
                cause=RuntimeError(error_msgs),
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

        if requested_sections:
            section_snapshot: dict[str, Any] = {
                "generated_at": repos_payload.get("generated_at")
                or messages_payload.get("generated_at")
            }
            if "repos" in requested_sections:
                section_snapshot["repos"] = [_summarize_repo(repo) for repo in repos]
            if "agent_workspaces" in requested_sections:
                section_snapshot["agent_workspaces"] = repos_payload.get(
                    "agent_workspaces", []
                )
            if "inbox" in requested_sections:
                section_snapshot["inbox_items"] = [
                    _summarize_message(msg) for msg in messages_items
                ]
            for key in (
                "pma_threads",
                "pma_files_detail",
                "automation",
                "action_queue",
            ):
                if key in requested_sections:
                    section_snapshot[key] = messages_payload.get(key)
            if fetch_errors:
                section_snapshot["errors"] = fetch_errors

            if not output_json:
                typer.echo(
                    "Hub Snapshot Sections: "
                    + ", ".join(
                        f"{name}={len(section_snapshot.get('inbox_items', [])) if name == 'inbox' else len(section_snapshot.get(name, [])) if isinstance(section_snapshot.get(name), list) else '1'}"
                        for name in requested_sections
                    )
                )
                if section_snapshot.get("generated_at"):
                    typer.echo(f"generated_at: {section_snapshot.get('generated_at')}")
                for error in fetch_errors:
                    typer.echo(
                        f"warning: failed to fetch {error['target']} from {error['url']}: {error['error']}"
                    )
                return

            indent = 2 if pretty else None
            typer.echo(json.dumps(section_snapshot, indent=indent))
            return

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
        if fetch_errors:
            snapshot["errors"] = fetch_errors

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
            for error in fetch_errors:
                typer.echo(
                    f"warning: failed to fetch {error['target']} from {error['url']}: {error['error']}"
                )
            for repo in snapshot_repos:
                if not isinstance(repo, dict):
                    continue
                run_state: dict = {}
                rs = repo.get("run_state")
                if isinstance(rs, dict):
                    run_state = rs
                typer.echo(
                    f"- {repo.get('id')}: {repo.get('status')} run_state={run_state.get('state')}"
                )
                if run_state.get("blocking_reason"):
                    typer.echo(f"  blocked: {run_state.get('blocking_reason')}")
                if run_state.get("recommended_action"):
                    typer.echo(f"  action: {run_state.get('recommended_action')}")
                freshness = repo.get("freshness")
                if isinstance(freshness, dict) and freshness.get("basis_at"):
                    typer.echo(
                        f"  freshness: {freshness.get('status')} basis={freshness.get('recency_basis')} "
                        f"at={freshness.get('basis_at')}"
                    )
            for msg in snapshot_inbox:
                if not isinstance(msg, dict):
                    continue
                run_state_inbox: dict = {}
                rs = msg.get("run_state")
                if isinstance(rs, dict):
                    run_state_inbox = rs
                typer.echo(
                    f"- inbox: repo={msg.get('repo_id')} run={msg.get('run_id')} "
                    f"title={msg.get('dispatch', {}).get('title')} state={run_state_inbox.get('state')}"
                )
                if run_state_inbox.get("blocking_reason"):
                    typer.echo(f"  blocked: {run_state_inbox.get('blocking_reason')}")
                if run_state_inbox.get("recommended_action"):
                    typer.echo(f"  action: {run_state_inbox.get('recommended_action')}")
                freshness = msg.get("freshness")
                if isinstance(freshness, dict) and freshness.get("basis_at"):
                    typer.echo(
                        f"  freshness: {freshness.get('status')} basis={freshness.get('recency_basis')} "
                        f"at={freshness.get('basis_at')}"
                    )
            return

        indent = 2 if pretty else None
        typer.echo(json.dumps(snapshot, indent=indent))
