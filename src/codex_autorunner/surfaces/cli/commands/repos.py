import json
from pathlib import Path
from typing import Callable, Optional

import typer

from ..template_repos import TemplatesConfigError, load_template_repos_manager


def register_repos_commands(
    repos_app: typer.Typer,
    *,
    raise_exit: Callable,
) -> None:
    @repos_app.command("list")
    def repos_list(
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        manager = load_template_repos_manager(hub)
        repos = manager.list_repos()

        if output_json:
            payload = {"repos": repos}
            typer.echo(json.dumps(payload, indent=2))
            return

        if not repos:
            typer.echo("No template repos configured.")
            return

        typer.echo(f"Template repos ({len(repos)}):")
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            repo_id = repo.get("id", "")
            url = repo.get("url", "")
            trusted = repo.get("trusted", False)
            default_ref = repo.get("default_ref", "main")
            trusted_text = "trusted" if trusted else "untrusted"
            typer.echo(
                f"  - {repo_id}: {url} [{trusted_text}] (default_ref={default_ref})"
            )

    @repos_app.command("add")
    def repos_add(
        repo_id: str = typer.Argument(..., help="Unique repo ID"),
        url: str = typer.Argument(..., help="Git repo URL or path"),
        trusted: Optional[bool] = typer.Option(
            None, "--trusted/--untrusted", help="Trust level (default: untrusted)"
        ),
        default_ref: str = typer.Option(
            "main", "--default-ref", help="Default git ref"
        ),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        manager = load_template_repos_manager(hub)
        try:
            manager.add_repo(repo_id, url, trusted, default_ref)
        except TemplatesConfigError as exc:
            raise_exit(str(exc), cause=exc)
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        try:
            manager.save()
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        typer.echo(f"Added template repo '{repo_id}' to hub config.")

    @repos_app.command("remove")
    def repos_remove(
        repo_id: str = typer.Argument(..., help="Repo ID to remove"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        manager = load_template_repos_manager(hub)
        try:
            manager.remove_repo(repo_id)
        except TemplatesConfigError as exc:
            raise_exit(str(exc), cause=exc)
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        try:
            manager.save()
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        typer.echo(f"Removed template repo '{repo_id}' from hub config.")

    @repos_app.command("trust")
    def repos_trust(
        repo_id: str = typer.Argument(..., help="Repo ID to trust"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        manager = load_template_repos_manager(hub)
        try:
            manager.set_trusted(repo_id, True)
        except TemplatesConfigError as exc:
            raise_exit(str(exc), cause=exc)
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        try:
            manager.save()
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        typer.echo(f"Marked repo '{repo_id}' as trusted.")

    @repos_app.command("untrust")
    def repos_untrust(
        repo_id: str = typer.Argument(..., help="Repo ID to untrust"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        manager = load_template_repos_manager(hub)
        try:
            manager.set_trusted(repo_id, False)
        except TemplatesConfigError as exc:
            raise_exit(str(exc), cause=exc)
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        try:
            manager.save()
        except OSError as exc:
            raise_exit(f"Failed to write hub config: {exc}", cause=exc)

        typer.echo(f"Marked repo '{repo_id}' as untrusted.")
