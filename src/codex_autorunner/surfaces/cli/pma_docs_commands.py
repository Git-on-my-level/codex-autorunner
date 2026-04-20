"""PMA docs CLI commands (non-thread docs show)."""

from pathlib import Path
from typing import Optional

import typer

from ...bootstrap import ensure_pma_docs, pma_doc_path
from .hub_path_option import hub_root_path_option
from .pma_control_plane import resolve_hub_path


def register_docs_commands(app: typer.Typer) -> None:
    app.command("show")(pma_docs_show)


def pma_docs_show(
    doc_type: str = typer.Argument(..., help="Document type: agents, active, or log"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show PMA docs content to stdout."""
    hub_root = resolve_hub_path(path)
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        typer.echo(f"Failed to ensure PMA docs: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if doc_type == "agents":
        doc_path = pma_doc_path(hub_root, "AGENTS.md")
    elif doc_type == "active":
        doc_path = pma_doc_path(hub_root, "active_context.md")
    elif doc_type == "log":
        doc_path = pma_doc_path(hub_root, "context_log.md")
    else:
        typer.echo("Invalid doc_type. Must be one of: agents, active, log", err=True)
        raise typer.Exit(code=1) from None

    try:
        content = doc_path.read_text(encoding="utf-8")
        typer.echo(content, nl=False)
    except OSError as exc:
        typer.echo(f"Failed to read {doc_path}: {exc}", err=True)
        raise typer.Exit(code=1) from None
