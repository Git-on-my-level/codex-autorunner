import json
import shlex
from pathlib import Path
from typing import Callable, List, Optional

import typer

from ....core.config import HubConfig
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.hub import HubSupervisor


def _worktree_recommended_actions(
    worktree_repo_id: str, *, hub_path: Optional[Path] = None
) -> list[str]:
    hub_suffix = ""
    if hub_path is not None:
        hub_suffix = f" --path {shlex.quote(str(hub_path))}"
    return [
        f"car hub worktree archive {worktree_repo_id}{hub_suffix}",
        f"car hub worktree cleanup {worktree_repo_id}{hub_suffix}",
        f"car hub destination show {worktree_repo_id}{hub_suffix}",
    ]


def _worktree_snapshot_payload(snapshot, *, hub_path: Optional[Path] = None) -> dict:
    recommended_actions = _worktree_recommended_actions(snapshot.id, hub_path=hub_path)
    return {
        "id": snapshot.id,
        "worktree_of": snapshot.worktree_of,
        "branch": snapshot.branch,
        "path": str(snapshot.path),
        "initialized": snapshot.initialized,
        "exists_on_disk": snapshot.exists_on_disk,
        "status": snapshot.status.value,
        "recommended_command": recommended_actions[0],
        "recommended_actions": recommended_actions,
    }


def _emit_cleanup_status(result: object) -> None:
    typer.echo("ok")
    if not isinstance(result, dict):
        return
    docker_cleanup = result.get("docker_cleanup")
    if not isinstance(docker_cleanup, dict):
        return
    status = str(docker_cleanup.get("status", "unknown")).strip() or "unknown"
    parts = [f"docker_cleanup={status}"]
    container_name = docker_cleanup.get("container_name")
    if isinstance(container_name, str) and container_name.strip():
        parts.append(f"container={container_name.strip()}")
    message = docker_cleanup.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(f"detail={message.strip()}")
    typer.echo(" ".join(parts))


def _build_force_attestation(
    force_attestation: Optional[str], *, target_scope: str
) -> Optional[dict[str, str]]:
    if force_attestation is None:
        return None
    return {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": force_attestation,
        "target_scope": target_scope,
    }


def register_worktree_commands(
    worktree_app: typer.Typer,
    *,
    require_hub_config: Callable[[Optional[Path]], HubConfig],
    raise_exit: Callable,
    build_supervisor: Callable[[HubConfig], HubSupervisor],
    build_server_url: Callable,
    request_json: Callable,
) -> None:
    @worktree_app.command("create")
    def hub_worktree_create(
        base_repo_id: str = typer.Argument(..., help="Base repo id to branch from"),
        branch: str = typer.Argument(..., help="Branch name for the new worktree"),
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        force: bool = typer.Option(False, "--force", help="Allow existing directory"),
        start_point: Optional[str] = typer.Option(
            None,
            "--start-point",
            help="Optional git ref to branch from (default: origin/<default-branch>)",
        ),
    ):
        """Create a new worktree from a base repo branch."""
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            snapshot = supervisor.create_worktree(
                base_repo_id=base_repo_id,
                branch=branch,
                force=force,
                start_point=start_point,
            )
        except (RuntimeError, OSError, ValueError) as exc:
            raise_exit(str(exc), cause=exc)
        typer.echo(
            f"Created worktree {snapshot.id} (branch={snapshot.branch}) at {snapshot.path}"
        )

    @worktree_app.command("list")
    def hub_worktree_list(
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """List hub worktrees and print canonical lifecycle commands."""
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        snapshots = [
            snapshot
            for snapshot in supervisor.list_repos(use_cache=False)
            if snapshot.kind == "worktree"
        ]
        payload = [
            _worktree_snapshot_payload(snapshot, hub_path=config.root)
            for snapshot in snapshots
        ]
        if output_json:
            typer.echo(json.dumps({"worktrees": payload}, indent=2))
            return
        if not payload:
            typer.echo("No worktrees found.")
            return
        typer.echo(f"Worktrees ({len(payload)}):")
        for item in payload:
            typer.echo(
                "  - {id} (base={worktree_of}, branch={branch}, status={status}, initialized={initialized}, exists={exists_on_disk}, path={path})".format(
                    **item
                )
            )
            typer.echo(f"    recommended: {item['recommended_command']}")

    @worktree_app.command("scan")
    def hub_worktree_scan(
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Rescan hub worktrees from disk and print canonical lifecycle commands."""
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        snapshots = [snap for snap in supervisor.scan() if snap.kind == "worktree"]
        payload = [
            _worktree_snapshot_payload(snapshot, hub_path=config.root)
            for snapshot in snapshots
        ]
        if output_json:
            typer.echo(json.dumps({"worktrees": payload}, indent=2))
            return
        if not payload:
            typer.echo("No worktrees found.")
            return
        typer.echo(f"Worktrees ({len(payload)}):")
        for item in payload:
            typer.echo(
                "  - {id} (base={worktree_of}, branch={branch}, status={status}, initialized={initialized}, exists={exists_on_disk}, path={path})".format(
                    **item
                )
            )
            typer.echo(f"    recommended: {item['recommended_command']}")

    @worktree_app.command("cleanup")
    def hub_worktree_cleanup(
        worktree_repo_id: str = typer.Argument(..., help="Worktree repo id to remove"),
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        delete_branch: bool = typer.Option(
            False, "--delete-branch", help="Delete the local branch"
        ),
        delete_remote: bool = typer.Option(
            False, "--delete-remote", help="Delete the remote branch"
        ),
        archive: bool = typer.Option(
            True,
            "--archive/--no-archive",
            help="Archive worktree snapshot before cleanup (required by PMA policy)",
        ),
        force_archive: bool = typer.Option(
            False, "--force-archive", help="Continue cleanup if archive fails"
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Allow cleanup of a worktree bound to an active chat thread",
        ),
        force_attestation: Optional[str] = typer.Option(
            None,
            "--force-attestation",
            help="Attestation text required with --force/--force-archive for dangerous actions.",
        ),
        archive_note: Optional[str] = typer.Option(
            None, "--archive-note", help="Optional archive note"
        ),
        archive_profile: Optional[str] = typer.Option(
            None,
            "--archive-profile",
            help="Override the configured archive profile for the cleanup snapshot (portable or full).",
        ),
    ):
        """Cleanup a worktree repo and optionally delete branches.

        Safety:
        This removes the worktree from disk. Keep archive enabled unless you are
        intentionally discarding artifacts.
        """
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            force_attestation_payload: Optional[dict[str, str]] = None
            if force or force_archive:
                force_attestation_payload = _build_force_attestation(
                    force_attestation,
                    target_scope=f"hub.worktree.cleanup:{worktree_repo_id}",
                )
            if force_attestation_payload is not None:
                result = supervisor.cleanup_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    archive=archive,
                    force_archive=force_archive,
                    archive_note=archive_note,
                    force=force,
                    force_attestation=force_attestation_payload,
                    archive_profile=archive_profile,
                )
            else:
                result = supervisor.cleanup_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    archive=archive,
                    force_archive=force_archive,
                    archive_note=archive_note,
                    force=force,
                    archive_profile=archive_profile,
                )
        except (RuntimeError, OSError, ValueError) as exc:
            raise_exit(str(exc), cause=exc)
        _emit_cleanup_status(result)

    @worktree_app.command("archive")
    def hub_worktree_archive(
        worktree_repo_id: str = typer.Argument(..., help="Worktree repo id to archive"),
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        delete_branch: bool = typer.Option(
            False, "--delete-branch", help="Delete the local branch"
        ),
        delete_remote: bool = typer.Option(
            False, "--delete-remote", help="Delete the remote branch"
        ),
        force_archive: bool = typer.Option(
            False, "--force-archive", help="Continue cleanup if archive fails"
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Allow archive+cleanup of a worktree bound to an active chat thread",
        ),
        force_attestation: Optional[str] = typer.Option(
            None,
            "--force-attestation",
            help="Attestation text required with --force/--force-archive for dangerous actions.",
        ),
        archive_note: Optional[str] = typer.Option(
            None, "--archive-note", help="Optional archive note"
        ),
        archive_profile: Optional[str] = typer.Option(
            None,
            "--archive-profile",
            help="Override the configured archive profile for the cleanup snapshot (portable or full).",
        ),
    ):
        """Archive and cleanup a worktree (canonical lifecycle command).

        Safety:
        This removes the worktree after archiving. Use `--force` only when the
        worktree is intentionally bound to an active chat thread.
        """
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            force_attestation_payload: Optional[dict[str, str]] = None
            if force or force_archive:
                force_attestation_payload = _build_force_attestation(
                    force_attestation,
                    target_scope=f"hub.worktree.archive:{worktree_repo_id}",
                )
            if force_attestation_payload is not None:
                result = supervisor.cleanup_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    archive=True,
                    force_archive=force_archive,
                    archive_note=archive_note,
                    force=force,
                    force_attestation=force_attestation_payload,
                    archive_profile=archive_profile,
                )
            else:
                result = supervisor.cleanup_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    archive=True,
                    force_archive=force_archive,
                    archive_note=archive_note,
                    force=force,
                    archive_profile=archive_profile,
                )
        except (RuntimeError, OSError, ValueError) as exc:
            raise_exit(str(exc), cause=exc)
        _emit_cleanup_status(result)

    @worktree_app.command("setup")
    def hub_worktree_setup(
        repo_id: str = typer.Argument(..., help="Base repo id to configure"),
        commands: Optional[List[str]] = typer.Argument(
            None, help="Commands to run after worktree creation (one per arg)"
        ),
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        clear: bool = typer.Option(
            False, "--clear", help="Clear all worktree setup commands"
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override hub server base path (e.g. /car)"
        ),
    ):
        """
        Configure commands to run automatically after creating a new worktree.

        Commands run with /bin/sh -lc in the new worktree directory.

        Examples:
          car hub worktree setup my-repo 'make setup' 'pre-commit install'
          car hub worktree setup my-repo --clear
        """
        config = require_hub_config(hub)
        normalized_commands: List[str] = []
        if clear:
            normalized_commands = []
        elif commands:
            normalized_commands = [cmd.strip() for cmd in commands if cmd.strip()]
        else:
            raise_exit(
                "Provide commands as arguments or use --clear to remove them.\n"
                "Example: car hub worktree setup my-repo 'make setup' 'pre-commit install'"
            )
            return
        url = build_server_url(
            config, f"/hub/repos/{repo_id}/worktree-setup", base_path_override=base_path
        )
        try:
            request_json(
                "POST",
                url,
                token_env=config.server_auth_token_env,
                payload={"commands": normalized_commands},
            )
        except (RuntimeError, OSError, ValueError) as exc:
            raise_exit(
                f"Failed to set worktree setup commands for {repo_id}: {exc}",
                cause=exc,
            )
        count = len(normalized_commands)
        if count:
            typer.echo(f"Set {count} worktree setup command(s) for {repo_id}")
        else:
            typer.echo(f"Cleared worktree setup commands for {repo_id}")
