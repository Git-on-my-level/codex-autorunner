from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from ....integrations.chat.dispatcher import conversation_id_for
from ....integrations.chat.queue_control import (
    ChatQueueControlStore,
    normalize_chat_thread_id,
)
from ..hub_path_option import hub_root_path_option


def register_chat_commands(
    app: typer.Typer,
    *,
    resolve_hub_path: Callable[[Optional[Path]], Path],
) -> None:
    channels_app = typer.Typer(
        add_completion=False,
        help="Inspect cached chat channel/topic directory entries.",
    )
    queue_app = typer.Typer(
        add_completion=False,
        help="Inspect and remediate live per-conversation chat dispatch queues.",
    )
    app.add_typer(channels_app, name="channels")
    app.add_typer(queue_app, name="queue")

    @channels_app.command("list")
    def chat_channels_list(
        query: Optional[str] = typer.Option(
            None, "--query", help="Filter entries by substring"
        ),
        limit: int = typer.Option(100, "--limit", min=1, help="Maximum rows"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = hub_root_path_option(),
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

        for row in rows:
            typer.echo(f"  {row['key']}")

    @queue_app.command("status")
    def chat_queue_status(
        channel: str = typer.Option(..., "--channel", help="Chat/channel id"),
        thread: Optional[str] = typer.Option(
            None, "--thread", help="Thread/topic id when applicable"
        ),
        platform: str = typer.Option(
            "discord", "--platform", help="Chat platform (default: discord)"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = hub_root_path_option(),
    ) -> None:
        """Show persisted queue status for one channel/topic conversation."""
        hub_root = resolve_hub_path(path)
        conversation_id = conversation_id_for(
            str(platform or "").strip() or "discord",
            str(channel or "").strip(),
            normalize_chat_thread_id(thread),
        )
        store = ChatQueueControlStore(hub_root)
        snapshot = store.read_snapshot(conversation_id)

        payload = {
            "conversation_id": conversation_id,
            "platform": platform,
            "channel": channel,
            "thread": normalize_chat_thread_id(thread),
            "status": snapshot,
        }
        if output_json:
            typer.echo(json.dumps(payload, indent=2))
            return

        if not isinstance(snapshot, dict):
            typer.echo(f"No queue state: {conversation_id}")
            return

        lines = [
            f"conversation={conversation_id}",
            f"pending={int(snapshot.get('pending_count') or 0)}",
            f"active={bool(snapshot.get('active'))}",
        ]
        active_update_id = snapshot.get("active_update_id")
        if isinstance(active_update_id, str) and active_update_id:
            lines.append(f"active_update_id={active_update_id}")
        active_started_at = snapshot.get("active_started_at")
        if isinstance(active_started_at, str) and active_started_at:
            lines.append(f"active_started_at={active_started_at}")
        updated_at = snapshot.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            lines.append(f"updated_at={updated_at}")
        typer.echo("\n".join(lines))

    @queue_app.command("reset")
    def chat_queue_reset(
        channel: str = typer.Option(..., "--channel", help="Chat/channel id"),
        thread: Optional[str] = typer.Option(
            None, "--thread", help="Thread/topic id when applicable"
        ),
        platform: str = typer.Option(
            "discord", "--platform", help="Chat platform (default: discord)"
        ),
        reason: Optional[str] = typer.Option(
            None, "--reason", help="Optional operator note for the reset request"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        path: Optional[Path] = hub_root_path_option(),
    ) -> None:
        """Request a live queue reset for one channel/topic conversation."""
        hub_root = resolve_hub_path(path)
        normalized_platform = str(platform or "").strip() or "discord"
        normalized_channel = str(channel or "").strip()
        normalized_thread = normalize_chat_thread_id(thread)
        conversation_id = conversation_id_for(
            normalized_platform,
            normalized_channel,
            normalized_thread,
        )
        store = ChatQueueControlStore(hub_root)
        request = store.request_reset(
            conversation_id=conversation_id,
            platform=normalized_platform,
            chat_id=normalized_channel,
            thread_id=normalized_thread,
            reason=reason,
        )

        if output_json:
            typer.echo(json.dumps({"status": "ok", "reset_request": request}, indent=2))
            return

        typer.echo(f"Reset: {conversation_id}")
