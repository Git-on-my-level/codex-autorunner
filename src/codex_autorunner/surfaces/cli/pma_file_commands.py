"""PMA file CLI commands (non-thread FileBox lifecycle commands)."""

import json
from pathlib import Path
from typing import Any, Optional

import typer

from ...core.filebox import BOXES, list_filebox
from ...core.filebox_lifecycle import (
    LIFECYCLE_BOXES,
    consume_inbox_file,
    dismiss_inbox_file,
    list_consumed_files,
    unconsume_inbox_file,
)
from .hub_path_option import hub_root_path_option
from .pma_control_plane import resolve_hub_path


def register_file_commands(app: typer.Typer) -> None:
    app.command("consume")(pma_file_consume)
    app.command("dismiss")(pma_file_dismiss)
    app.command("restore")(pma_file_restore)
    app.command("list")(pma_file_list)


def _pma_file_listings(hub_root: Path) -> dict[str, list[Any]]:
    listing = list_filebox(hub_root)
    archived = list_consumed_files(hub_root)
    for box in LIFECYCLE_BOXES:
        listing[box] = [entry for entry in archived if entry.box == box]
    return listing


def _emit_pma_file_listing(
    *,
    listing: dict[str, list[Any]],
    boxes: list[str],
    output_json: bool,
) -> None:
    if output_json:
        payload = {
            box: [
                {
                    "name": entry.name,
                    "box": entry.box,
                    "size": entry.size,
                    "modified_at": entry.modified_at,
                    "source": entry.source,
                }
                for entry in listing.get(box, [])
            ]
            for box in boxes
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    for index, box in enumerate(boxes):
        entries = listing.get(box, [])
        heading = box.capitalize()
        prefix = "\n" if index else ""
        typer.echo(f"{prefix}{heading} ({len(entries)}):")
        for entry in entries:
            source_str = f", source={entry.source}" if entry.source else ""
            typer.echo(
                f"  - {entry.name} ({entry.size} bytes, {entry.modified_at}{source_str})"
            )


def box_choices_text() -> str:
    return "|".join(BOXES)


def _file_lifecycle_box_choices_text() -> str:
    return "inbox|consumed|dismissed|all"


def pma_file_consume(
    filename: str = typer.Argument(..., help="Inbox filename to mark as consumed"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Move a PMA inbox file into the recoverable consumed archive."""
    hub_root = resolve_hub_path(path)
    try:
        entry = consume_inbox_file(hub_root, filename)
    except ValueError as exc:
        typer.echo(f"Invalid filename: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Consumed {entry.name} -> {entry.box}")


def pma_file_dismiss(
    filename: str = typer.Argument(..., help="Inbox filename to mark as dismissed"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Move a PMA inbox file into the recoverable dismissed archive."""
    hub_root = resolve_hub_path(path)
    try:
        entry = dismiss_inbox_file(hub_root, filename)
    except ValueError as exc:
        typer.echo(f"Invalid filename: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Dismissed {entry.name} -> {entry.box}")


def pma_file_restore(
    filename: str = typer.Argument(..., help="Archived filename to restore to inbox"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Restore a consumed or dismissed PMA file back to the inbox."""
    hub_root = resolve_hub_path(path)
    try:
        entry = unconsume_inbox_file(hub_root, filename)
    except ValueError as exc:
        typer.echo(f"Invalid filename: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"Restored {entry.name} -> {entry.box}")


def pma_file_list(
    box: str = typer.Option(
        "all",
        "--box",
        help=f"Box to list ({_file_lifecycle_box_choices_text()})",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List PMA inbox and archived lifecycle files from the local FileBox."""
    normalized_box = str(box or "").strip().lower()
    if normalized_box not in {"inbox", *LIFECYCLE_BOXES, "all"}:
        typer.echo(
            f"Box must be one of: {_file_lifecycle_box_choices_text()}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    hub_root = resolve_hub_path(path)
    listing = _pma_file_listings(hub_root)
    boxes = ["inbox", *LIFECYCLE_BOXES] if normalized_box == "all" else [normalized_box]
    _emit_pma_file_listing(listing=listing, boxes=boxes, output_json=output_json)
