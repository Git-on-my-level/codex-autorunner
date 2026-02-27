from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)


def register_chat_commands(
    app: typer.Typer,
    *,
    resolve_hub_path: Callable[[Optional[Path]], Path],
) -> None:
    channels_app = typer.Typer(add_completion=False)
    app.add_typer(channels_app, name="channels")

    @channels_app.command("list")
    def chat_channels_list(
        query: Optional[str] = typer.Option(
            None, "--query", help="Filter entries by substring"
        ),
        limit: int = typer.Option(100, "--limit", min=1, help="Maximum rows"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
    ) -> None:
        """List cached chat channels/topics discovered from inbound traffic."""
        hub_root = resolve_hub_path(path)
        store = ChannelDirectoryStore(hub_root)
        entries = store.list_entries(query=query, limit=limit)

        rows: list[dict[str, Any]] = []
        for entry in entries:
            key = channel_entry_key(entry)
            if not isinstance(key, str):
                continue
            rows.append(
                {
                    "key": key,
                    "display": entry.get("display"),
                    "seen_at": entry.get("seen_at"),
                    "meta": entry.get("meta"),
                    "entry": entry,
                }
            )

        if output_json:
            typer.echo(json.dumps({"entries": rows}, indent=2))
            return

        if not rows:
            typer.echo("No chat channel directory entries found.")
            return

        lines = ["Chat channel directory entries:"]
        for row in rows:
            key = row["key"]
            display = row.get("display")
            seen_at = row.get("seen_at")
            label = display if isinstance(display, str) and display else key
            if isinstance(seen_at, str) and seen_at:
                lines.append(f"- {key} ({label}) seen={seen_at}")
            else:
                lines.append(f"- {key} ({label})")
        typer.echo("\n".join(lines))
