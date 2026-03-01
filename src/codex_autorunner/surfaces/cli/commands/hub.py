import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer
import uvicorn

from ....core.config import HubConfig
from ....core.destinations import resolve_effective_repo_destination
from ....core.hub import HubSupervisor
from ....manifest import load_manifest, normalize_manifest_destination, save_manifest
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
    destination_app = typer.Typer(add_completion=False)
    hub_app.add_typer(destination_app, name="destination")

    def _resolve_repo_entry(config: HubConfig, repo_id: str):
        manifest = load_manifest(config.manifest_path, config.root)
        repos_by_id = {entry.id: entry for entry in manifest.repos}
        repo = repos_by_id.get(repo_id)
        if repo is None:
            raise_exit(f"Repo id not found in hub manifest: {repo_id}")
        return manifest, repos_by_id, repo

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

    @destination_app.command("show")
    def hub_destination_show(
        repo_id: str = typer.Argument(..., help="Repo id from hub manifest"),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        config = require_hub_config(path)
        _, repos_by_id, repo = _resolve_repo_entry(config, repo_id)
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        payload = {
            "repo_id": repo.id,
            "kind": repo.kind,
            "worktree_of": repo.worktree_of,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": list(resolution.issues),
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
        if resolution.issues:
            typer.echo("Validation issues:")
            for issue in resolution.issues:
                typer.echo(f"- {issue}")

    @destination_app.command("set")
    def hub_destination_set(
        repo_id: str = typer.Argument(..., help="Repo id from hub manifest"),
        kind: str = typer.Argument(..., help="Destination kind (local|docker)"),
        image: Optional[str] = typer.Option(
            None, "--image", help="Docker image (required for docker kind)"
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
            help="Docker runtime profile (example: full-dev)",
        ),
        workdir: Optional[str] = typer.Option(
            None, "--workdir", help="Docker workdir override inside the container"
        ),
        path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
        output_json: bool = typer.Option(
            False, "--json", help="Emit JSON payload for scripting"
        ),
    ):
        config = require_hub_config(path)
        manifest, repos_by_id, repo = _resolve_repo_entry(config, repo_id)

        normalized_kind = kind.strip().lower()
        destination: dict[str, Any]
        if normalized_kind == "local":
            destination = {"kind": "local"}
        elif normalized_kind == "docker":
            if not isinstance(image, str) or not image.strip():
                raise_exit("--image is required for docker destination")
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

        normalized_destination = normalize_manifest_destination(destination)
        if normalized_destination is None:
            raise_exit(f"Invalid destination payload: {destination!r}")
        repo.destination = normalized_destination
        save_manifest(config.manifest_path, manifest, config.root)

        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        payload = {
            "repo_id": repo.id,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": list(resolution.issues),
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(
            f"Updated destination for {repo.id} to "
            f"{resolution.destination.kind} (source={resolution.source})"
        )

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
        config = require_hub_config(path)
        supervisor = build_supervisor(config)
        snapshots = supervisor.scan()
        typer.echo(f"Scanned hub at {config.root} (repos_root={config.repos_root})")
        for snap in snapshots:
            typer.echo(
                f"- {snap.id}: {snap.status.value}, initialized={snap.initialized}, exists={snap.exists_on_disk}"
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
            }

        snapshot = {
            "last_scan_at": (
                repos_payload.get("last_scan_at")
                if isinstance(repos_payload, dict)
                else None
            ),
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
            return

        indent = 2 if pretty else None
        typer.echo(json.dumps(snapshot, indent=indent))
