import json
from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.config import HubConfig
from ....core.hub import HubSupervisor


def _worktree_snapshot_payload(snapshot) -> dict:
    return {
        "id": snapshot.id,
        "worktree_of": snapshot.worktree_of,
        "branch": snapshot.branch,
        "path": str(snapshot.path),
        "initialized": snapshot.initialized,
        "exists_on_disk": snapshot.exists_on_disk,
        "status": snapshot.status.value,
    }


def register_worktree_commands(
    worktree_app: typer.Typer,
    *,
    require_hub_config: Callable[[Optional[Path]], HubConfig],
    raise_exit: Callable,
    build_supervisor: Callable[[HubConfig], HubSupervisor],
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
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            snapshot = supervisor.create_worktree(
                base_repo_id=base_repo_id,
                branch=branch,
                force=force,
                start_point=start_point,
            )
        except Exception as exc:
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
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        snapshots = [
            snapshot
            for snapshot in supervisor.list_repos(use_cache=False)
            if snapshot.kind == "worktree"
        ]
        payload = [_worktree_snapshot_payload(snapshot) for snapshot in snapshots]
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

    @worktree_app.command("scan")
    def hub_worktree_scan(
        hub: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        snapshots = [snap for snap in supervisor.scan() if snap.kind == "worktree"]
        payload = [_worktree_snapshot_payload(snapshot) for snapshot in snapshots]
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
        archive_note: Optional[str] = typer.Option(
            None, "--archive-note", help="Optional archive note"
        ),
    ):
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            supervisor.cleanup_worktree(
                worktree_repo_id=worktree_repo_id,
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                archive=archive,
                force_archive=force_archive,
                archive_note=archive_note,
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        typer.echo("ok")

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
        archive_note: Optional[str] = typer.Option(
            None, "--archive-note", help="Optional archive note"
        ),
    ):
        config = require_hub_config(hub)
        supervisor = build_supervisor(config)
        try:
            supervisor.cleanup_worktree(
                worktree_repo_id=worktree_repo_id,
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                archive=True,
                force_archive=force_archive,
                archive_note=archive_note,
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)
        typer.echo("ok")
