from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.utils import RepoNotFoundError, find_repo_root
from ....tickets.portable_lint import run_ticket_lint


def _resolve_repo_root(repo: Optional[Path]) -> Path:
    if repo is not None:
        return repo.expanduser().resolve()
    return find_repo_root(Path.cwd())


def register_tickets_commands(
    tickets_app: typer.Typer,
    *,
    raise_exit: Callable[[str], None],
) -> None:
    @tickets_app.command("lint")
    def tickets_lint(
        repo: Optional[Path] = typer.Option(
            None, "--repo", help="Repo path; defaults to the current git checkout."
        ),
        fix_ticket_ids: bool = typer.Option(
            False,
            "--fix-ticket-ids",
            help="Backfill missing or invalid ticket_id values before linting.",
        ),
    ) -> None:
        """Validate CAR ticket filenames and frontmatter."""
        try:
            repo_root = _resolve_repo_root(repo)
        except RepoNotFoundError as exc:
            raise_exit(str(exc))
            return

        exit_code = run_ticket_lint(
            repo_root / ".codex-autorunner" / "tickets",
            fix_ticket_ids=fix_ticket_ids,
        )
        if exit_code:
            raise typer.Exit(code=exit_code)
