from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class ActiveFlowInfo:
    flow_id: str
    status: Literal["running", "paused"]


def build_status_text(
    binding: Optional[dict[str, Any]],
    collaboration_summary_lines: Optional[list[str]],
    active_flow: Optional[ActiveFlowInfo],
    channel_id: str,
    *,
    include_flow_hint: bool = True,
) -> list[str]:
    if binding is None:
        lines = [
            "This channel is not bound.",
            "Use /car bind workspace:<workspace> or /pma on to enable plain-text turns here.",
        ]
        if collaboration_summary_lines:
            lines.extend(collaboration_summary_lines)
        lines.append("Then use /flow status once flow commands are enabled.")
        return lines

    lines = []
    is_pma = binding.get("pma_enabled", False)
    workspace_path = binding.get("workspace_path", "unknown")
    repo_id = binding.get("repo_id")
    guild_id = binding.get("guild_id")
    updated_at = binding.get("updated_at", "unknown")

    if is_pma:
        lines.append("Mode: PMA (hub)")
        prev_workspace = binding.get("pma_prev_workspace_path")
        if prev_workspace:
            lines.append(f"Previous binding: {prev_workspace}")
            lines.append("Use /pma off to restore previous binding.")
    else:
        lines.append("Mode: workspace")
        lines.append("Channel is bound.")

    lines.extend(
        [
            f"Workspace: {workspace_path}",
            f"Repo ID: {repo_id or 'none'}",
            f"Guild ID: {guild_id or 'none'}",
            f"Channel ID: {channel_id}",
            f"Last updated: {updated_at}",
        ]
    )

    if active_flow:
        lines.append(f"Active flow: {active_flow.flow_id} ({active_flow.status})")

    if collaboration_summary_lines:
        lines.extend(collaboration_summary_lines)

    if include_flow_hint:
        lines.append("Use /flow status for ticket flow details.")

    return lines


def build_debug_text(
    binding: Optional[dict[str, Any]],
    collaboration_summary_lines: Optional[list[str]],
    channel_id: str,
    pending_outbox_count: int = 0,
    *,
    application_id: Optional[str] = None,
) -> list[str]:
    lines = [
        f"Channel ID: {channel_id}",
    ]

    if binding is None:
        lines.append("Binding: none (unbound)")
        lines.append("Use /car bind path:<workspace> to bind this channel.")
        if collaboration_summary_lines:
            lines.extend(collaboration_summary_lines)
        return lines

    workspace_path = binding.get("workspace_path", "unknown")
    lines.extend(
        [
            f"Guild ID: {binding.get('guild_id') or 'none'}",
            f"Workspace: {workspace_path}",
            f"Repo ID: {binding.get('repo_id') or 'none'}",
            f"PMA enabled: {binding.get('pma_enabled', False)}",
            f"PMA prev workspace: {binding.get('pma_prev_workspace_path') or 'none'}",
            f"Updated at: {binding.get('updated_at', 'unknown')}",
        ]
    )

    if workspace_path and workspace_path != "unknown":
        try:
            workspace_root = Path(workspace_path).expanduser().resolve()
            lines.append(f"Canonical path: {workspace_root}")
            lines.append(f"Path exists: {workspace_root.exists()}")
            if workspace_root.exists():
                car_dir = workspace_root / ".codex-autorunner"
                lines.append(f".codex-autorunner exists: {car_dir.exists()}")
                flows_db = car_dir / "flows.db"
                lines.append(f"flows.db exists: {flows_db.exists()}")
        except OSError as exc:
            lines.append(f"Path resolution error: {exc}")

    lines.append(f"Pending outbox items: {pending_outbox_count}")

    if collaboration_summary_lines:
        lines.extend(collaboration_summary_lines)

    return lines


def build_ids_text(
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    collaboration_summary_lines: Optional[list[str]] = None,
    snippet_lines: Optional[list[str]] = None,
    binding: Optional[dict[str, Any]] = None,
) -> list[str]:
    lines = [
        f"Channel ID: {channel_id}",
        f"Guild ID: {guild_id or 'none'}",
        f"User ID: {user_id or 'unknown'}",
        "",
        "Allowlist example:",
        f"discord_bot.allowed_channel_ids: [{channel_id}]",
    ]
    if guild_id:
        lines.append(f"discord_bot.allowed_guild_ids: [{guild_id}]")
    if user_id:
        lines.append(f"discord_bot.allowed_user_ids: [{user_id}]")

    if collaboration_summary_lines or binding:
        lines.append("")
        if collaboration_summary_lines:
            lines.extend(collaboration_summary_lines)

    if snippet_lines:
        lines.append("")
        lines.extend(snippet_lines)

    return lines


__all__ = [
    "ActiveFlowInfo",
    "build_debug_text",
    "build_ids_text",
    "build_status_text",
]
