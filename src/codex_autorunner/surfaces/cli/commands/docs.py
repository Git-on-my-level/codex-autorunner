from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ....core.car_docs import (
    CarDoc,
    collect_car_docs,
    find_doc,
    format_docs_json,
    search_car_docs,
)
from ..hub_path_option import hub_root_path_option


def _print_doc_listing(docs: list[CarDoc]) -> None:
    if not docs:
        typer.echo("No docs found.")
        return
    current_scope = ""
    for doc in docs:
        if doc.scope != current_scope:
            current_scope = doc.scope
            typer.echo(f"{current_scope}:")
        path_text = f" [{doc.path}]" if doc.path is not None else ""
        typer.echo(f"  {doc.doc_id} - {doc.title}{path_text}")
        typer.echo(f"    {doc.summary}")


def register_docs_commands(app: typer.Typer) -> None:
    @app.command("list")
    def docs_list(
        hub: Optional[Path] = hub_root_path_option(),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """List shipped CAR docs plus hub/repo docs when paths are provided."""
        docs = collect_car_docs(hub_root=hub, repo_root=repo)
        if output_json:
            typer.echo(format_docs_json(docs))
            return
        _print_doc_listing(docs)

    @app.command("search")
    def docs_search(
        query: str = typer.Argument(..., help="Search query"),
        hub: Optional[Path] = hub_root_path_option(),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Search CAR docs by id, title, summary, and body text."""
        docs = search_car_docs(query, hub_root=hub, repo_root=repo)
        if output_json:
            typer.echo(format_docs_json(docs))
            return
        _print_doc_listing(docs)

    @app.command("show")
    def docs_show(
        doc_id: str = typer.Argument(..., help="Doc id from `car docs list`"),
        hub: Optional[Path] = hub_root_path_option(),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    ) -> None:
        """Print a CAR doc."""
        doc = find_doc(doc_id, hub_root=hub, repo_root=repo)
        if doc is None:
            typer.echo(f"Unknown doc id: {doc_id}", err=True)
            raise typer.Exit(code=1)
        typer.echo(doc.content, nl=doc.content.endswith("\n") is False)

    @app.command("path")
    def docs_path(
        doc_id: str = typer.Argument(..., help="Doc id from `car docs list`"),
        hub: Optional[Path] = hub_root_path_option(),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    ) -> None:
        """Print the filesystem path for a CAR doc when one exists."""
        doc = find_doc(doc_id, hub_root=hub, repo_root=repo)
        if doc is None:
            typer.echo(f"Unknown doc id: {doc_id}", err=True)
            raise typer.Exit(code=1)
        if doc.path is None:
            typer.echo(f"Doc has no filesystem path: {doc_id}", err=True)
            raise typer.Exit(code=1)
        typer.echo(str(doc.path))
