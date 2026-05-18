from pathlib import Path
from typing import Callable, List, Optional

import typer

from ....core.config import HubConfig
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.hub import HubSupervisor
from ..hub_path_option import hub_root_path_option
from ..hub_worktree_read_models import (
    cleanup_status_lines,
    render_worktree_summary_lines,
    worktree_snapshot_payload,
)
from ..output import echo_json


def _emit_cleanup_status(result: object) -> None:
    for line in cleanup_status_lines(result):
        typer.echo(line)


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
        hub: Optional[Path] = hub_root_path_option(),
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
        typer.echo(f"created {snapshot.id} branch={snapshot.branch} at {snapshot.path}")

    @worktree_app.command("list")
    def hub_worktree_list(
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """List hub worktrees and print canonical lifecycle commands."""
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        snapshots = [
            snapshot
            for snapshot in supervisor.list_repos()
            if snapshot.kind == "worktree"
        ]
        payload = [
            worktree_snapshot_payload(snapshot, hub_path=config.root)
            for snapshot in snapshots
        ]
        if output_json:
            echo_json({"worktrees": payload})
            return
        for line in render_worktree_summary_lines(payload):
            typer.echo(line)

    @worktree_app.command("scan")
    def hub_worktree_scan(
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Rescan hub worktrees from disk and print canonical lifecycle commands."""
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        snapshots = [snap for snap in supervisor.scan() if snap.kind == "worktree"]
        payload = [
            worktree_snapshot_payload(snapshot, hub_path=config.root)
            for snapshot in snapshots
        ]
        if output_json:
            echo_json({"worktrees": payload})
            return
        for line in render_worktree_summary_lines(payload):
            typer.echo(line)

    @worktree_app.command("retire")
    def hub_worktree_retire(
        worktree_repo_id: str = typer.Argument(..., help="Worktree repo id to retire"),
        hub: Optional[Path] = hub_root_path_option(),
        delete_branch: bool = typer.Option(
            False, "--delete-branch", help="Delete the local branch"
        ),
        delete_remote: bool = typer.Option(
            False, "--delete-remote", help="Delete the remote branch"
        ),
        force_archive: bool = typer.Option(
            False,
            "--force-archive",
            help="Continue retire if artifact preservation fails",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Allow retire of a worktree bound to an active chat thread",
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
            help="Override the configured archive profile for the retire snapshot (portable or full).",
        ),
    ):
        """Retire a worktree repo and optionally delete branches.

        Safety:
        This preserves CAR artifacts, then removes the worktree from disk.
        """
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            force_attestation_payload: Optional[dict[str, str]] = None
            if force or force_archive:
                force_attestation_payload = _build_force_attestation(
                    force_attestation,
                    target_scope=f"hub.worktree.retire:{worktree_repo_id}",
                )
            if force_attestation_payload is not None:
                result = supervisor.retire_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    force_archive=force_archive,
                    archive_note=archive_note,
                    force=force,
                    force_attestation=force_attestation_payload,
                    archive_profile=archive_profile,
                )
            else:
                result = supervisor.retire_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    force_archive=force_archive,
                    archive_note=archive_note,
                    force=force,
                    archive_profile=archive_profile,
                )
        except (RuntimeError, OSError, ValueError) as exc:
            raise_exit(str(exc), cause=exc)
        _emit_cleanup_status(result)

    @worktree_app.command("delete")
    def hub_worktree_delete(
        worktree_repo_id: str = typer.Argument(
            ..., help="Worktree repo id to delete without retiring"
        ),
        hub: Optional[Path] = hub_root_path_option(),
        delete_branch: bool = typer.Option(
            False, "--delete-branch", help="Delete the local branch"
        ),
        delete_remote: bool = typer.Option(
            False, "--delete-remote", help="Delete the remote branch"
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Required: delete the worktree without preserving artifacts",
        ),
        force_attestation: Optional[str] = typer.Option(
            None,
            "--force-attestation",
            help="Attestation text required with --force/--force-archive for dangerous actions.",
        ),
    ):
        """Delete a worktree without retiring it.

        Safety:
        This discards CAR artifacts. Prefer `retire` for normal lifecycle completion.
        """
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            force_attestation_payload: Optional[dict[str, str]] = None
            if force:
                force_attestation_payload = _build_force_attestation(
                    force_attestation,
                    target_scope=f"hub.worktree.delete:{worktree_repo_id}",
                )
            if force_attestation_payload is not None:
                result = supervisor.delete_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    force=force,
                    force_attestation=force_attestation_payload,
                )
            else:
                result = supervisor.delete_worktree(
                    worktree_repo_id=worktree_repo_id,
                    delete_branch=delete_branch,
                    delete_remote=delete_remote,
                    force=force,
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
        hub: Optional[Path] = hub_root_path_option(),
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
            raise_exit("Provide commands or --clear.")
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
            typer.echo(f"Set {count} setup cmd(s) for {repo_id}")
        else:
            typer.echo(f"Cleared setup for {repo_id}")
