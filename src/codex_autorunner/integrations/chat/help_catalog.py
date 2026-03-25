from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Literal, Optional

from ...core.flows import flow_help_lines

HelpSurface = Literal["discord", "telegram"]


@dataclass(frozen=True)
class HelpCommandDescriptor:
    id: str
    description: str
    section: Literal["core", "session", "files", "pma"]
    telegram_command: Optional[str] = None
    discord_path: Optional[tuple[str, ...]] = None
    telegram_usage: str = ""
    discord_usage: str = ""


_HELP_COMMANDS: tuple[HelpCommandDescriptor, ...] = (
    HelpCommandDescriptor(
        id="car.bind",
        description="Bind to workspace",
        section="core",
        telegram_command="bind",
        discord_path=("car", "bind"),
        discord_usage="[path]",
    ),
    HelpCommandDescriptor(
        id="car.status",
        description="Show binding and runtime status",
        section="core",
        telegram_command="status",
        discord_path=("car", "status"),
    ),
    HelpCommandDescriptor(
        id="car.new",
        description="Start a fresh chat session",
        section="core",
        telegram_command="new",
        discord_path=("car", "new"),
    ),
    HelpCommandDescriptor(
        id="car.newt",
        description="Reset branch from origin default and start fresh session",
        section="core",
        telegram_command="newt",
        discord_path=("car", "newt"),
    ),
    HelpCommandDescriptor(
        id="car.archive",
        description="Archive workspace state for a fresh start",
        section="core",
        telegram_command="archive",
        discord_path=("car", "archive"),
    ),
    HelpCommandDescriptor(
        id="car.debug",
        description="Show debug info for troubleshooting",
        section="core",
        telegram_command="debug",
        discord_path=("car", "debug"),
    ),
    HelpCommandDescriptor(
        id="car.ids",
        description="Show chat/user IDs for debugging",
        section="core",
        telegram_command="ids",
        discord_path=("car", "ids"),
    ),
    HelpCommandDescriptor(
        id="car.diff",
        description="Show git diff",
        section="core",
        telegram_command="diff",
        discord_path=("car", "diff"),
        discord_usage="[path]",
    ),
    HelpCommandDescriptor(
        id="car.skills",
        description="List available skills",
        section="core",
        telegram_command="skills",
        discord_path=("car", "skills"),
        telegram_usage="[search]",
        discord_usage="[search]",
    ),
    HelpCommandDescriptor(
        id="car.tickets",
        description="Browse and edit tickets",
        section="core",
        discord_path=("car", "tickets"),
        discord_usage="[search]",
    ),
    HelpCommandDescriptor(
        id="car.mcp",
        description="Show MCP server status",
        section="core",
        telegram_command="mcp",
        discord_path=("car", "mcp"),
    ),
    HelpCommandDescriptor(
        id="car.init",
        description="Generate AGENTS.md guidance",
        section="core",
        telegram_command="init",
        discord_path=("car", "init"),
    ),
    HelpCommandDescriptor(
        id="car.repos",
        description="List available repositories",
        section="core",
        telegram_command="repos",
        discord_path=("car", "repos"),
    ),
    HelpCommandDescriptor(
        id="car.agent",
        description="Show or set the active agent",
        section="core",
        telegram_command="agent",
        discord_path=("car", "agent"),
        telegram_usage="[name]",
        discord_usage="[name]",
    ),
    HelpCommandDescriptor(
        id="car.model",
        description="List or set the model",
        section="core",
        telegram_command="model",
        discord_path=("car", "model"),
        telegram_usage="[name]",
        discord_usage="[name]",
    ),
    HelpCommandDescriptor(
        id="car.update",
        description="Start update or check status",
        section="core",
        telegram_command="update",
        discord_path=("car", "update"),
        telegram_usage="[target]",
        discord_usage="[target]",
    ),
    HelpCommandDescriptor(
        id="car.review",
        description="Run a code review",
        section="core",
        telegram_command="review",
        discord_path=("car", "review"),
        telegram_usage="[target]",
        discord_usage="[target]",
    ),
    HelpCommandDescriptor(
        id="car.approvals",
        description="Set approval and sandbox policy",
        section="core",
        telegram_command="approvals",
        discord_path=("car", "approvals"),
        telegram_usage="[mode]",
        discord_usage="[mode]",
    ),
    HelpCommandDescriptor(
        id="car.mention",
        description="Include a file in a new request",
        section="core",
        telegram_command="mention",
        discord_path=("car", "mention"),
        telegram_usage="<path> [request]",
        discord_usage="<path> [request]",
    ),
    HelpCommandDescriptor(
        id="car.experimental",
        description="Toggle experimental features",
        section="core",
        telegram_command="experimental",
        discord_path=("car", "experimental"),
        telegram_usage="[action] [feature]",
        discord_usage="[action] [feature]",
    ),
    HelpCommandDescriptor(
        id="car.rollout",
        description="Show current thread rollout path",
        section="core",
        telegram_command="rollout",
        discord_path=("car", "rollout"),
    ),
    HelpCommandDescriptor(
        id="car.feedback",
        description="Send feedback and logs",
        section="core",
        telegram_command="feedback",
        discord_path=("car", "feedback"),
        telegram_usage="<reason>",
        discord_usage="<reason>",
    ),
    HelpCommandDescriptor(
        id="car.help",
        description="Show this help",
        section="core",
        telegram_command="help",
        discord_path=("car", "help"),
    ),
    HelpCommandDescriptor(
        id="car.resume",
        description="Resume a previous chat thread",
        section="session",
        telegram_command="resume",
        discord_path=("car", "session", "resume"),
        telegram_usage="[thread_id]",
        discord_usage="[thread_id]",
    ),
    HelpCommandDescriptor(
        id="car.reset",
        description="Reset PMA thread state",
        section="session",
        telegram_command="reset",
        discord_path=("car", "session", "reset"),
    ),
    HelpCommandDescriptor(
        id="car.compact",
        description="Compact the conversation",
        section="session",
        telegram_command="compact",
        discord_path=("car", "session", "compact"),
    ),
    HelpCommandDescriptor(
        id="car.interrupt",
        description="Stop the active turn",
        section="session",
        telegram_command="interrupt",
        discord_path=("car", "session", "interrupt"),
    ),
    HelpCommandDescriptor(
        id="car.logout",
        description="Log out of the Codex account",
        section="session",
        telegram_command="logout",
        discord_path=("car", "session", "logout"),
    ),
    HelpCommandDescriptor(
        id="car.files",
        description="List or manage file inbox/outbox",
        section="files",
        telegram_command="files",
        discord_path=("car", "files"),
    ),
    HelpCommandDescriptor(
        id="pma",
        description="PMA mode controls (on/off/status)",
        section="pma",
        telegram_command="pma",
    ),
    HelpCommandDescriptor(
        id="pma.on",
        description="Enable PMA mode",
        section="pma",
        discord_path=("pma", "on"),
    ),
    HelpCommandDescriptor(
        id="pma.off",
        description="Disable PMA mode and restore previous binding",
        section="pma",
        discord_path=("pma", "off"),
    ),
    HelpCommandDescriptor(
        id="pma.status",
        description="Show PMA mode status",
        section="pma",
        discord_path=("pma", "status"),
    ),
)

_TELEGRAM_COMMAND_ORDER: tuple[str, ...] = (
    "bind",
    "status",
    "new",
    "newt",
    "archive",
    "debug",
    "ids",
    "repos",
    "agent",
    "model",
    "update",
    "review",
    "approvals",
    "mention",
    "diff",
    "skills",
    "mcp",
    "experimental",
    "init",
    "resume",
    "reset",
    "compact",
    "interrupt",
    "logout",
    "files",
    "flow",
    "reply",
    "pma",
    "rollout",
    "feedback",
    "help",
)

_DISCORD_SECTION_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "**CAR Commands:**",
        (
            "car.bind",
            "car.status",
            "car.new",
            "car.newt",
            "car.debug",
            "car.help",
            "car.ids",
            "car.diff",
            "car.skills",
            "car.tickets",
            "car.mcp",
            "car.init",
            "car.repos",
            "car.agent",
            "car.model",
            "car.update",
            "car.review",
            "car.approvals",
            "car.mention",
            "car.experimental",
            "car.rollout",
            "car.feedback",
            "car.archive",
        ),
    ),
    (
        "**Session Commands:**",
        (
            "car.resume",
            "car.reset",
            "car.compact",
            "car.interrupt",
            "car.logout",
        ),
    ),
    ("**File Commands:**", ("car.files",)),
    ("**PMA Commands:**", ("pma.on", "pma.off", "pma.status")),
)

_DESCRIPTOR_BY_ID = {entry.id: entry for entry in _HELP_COMMANDS}
_DESCRIPTOR_BY_TELEGRAM = {
    entry.telegram_command: entry
    for entry in _HELP_COMMANDS
    if entry.telegram_command is not None
}


def _render_path(path: tuple[str, ...], usage: str = "") -> str:
    usage_part = f" {usage.strip()}" if usage.strip() else ""
    return f"/{' '.join(path)}{usage_part}"


def _render_telegram_line(command_name: str) -> Optional[str]:
    descriptor = _DESCRIPTOR_BY_TELEGRAM.get(command_name)
    if descriptor is None:
        return None
    usage_part = (
        f" {descriptor.telegram_usage.strip()}"
        if descriptor.telegram_usage.strip()
        else ""
    )
    return f"/{command_name}{usage_part} - {descriptor.description}"


def _render_discord_line(command_id: str) -> Optional[str]:
    descriptor = _DESCRIPTOR_BY_ID.get(command_id)
    if descriptor is None or descriptor.discord_path is None:
        return None
    return f"{_render_path(descriptor.discord_path, descriptor.discord_usage)} - {descriptor.description}"


def _discord_file_help_lines() -> list[str]:
    return [
        "/car files inbox - List files in inbox",
        "/car files outbox - List pending outbox files",
        "/car files clear [target] - Clear inbox/outbox",
    ]


def _telegram_file_help_lines(command_names: Collection[str]) -> list[str]:
    if "files" not in set(command_names):
        return []
    return [
        "/files - List or manage file inbox/outbox",
        "/files inbox",
        "/files outbox",
        "/files all",
        "/files send <filename>",
        "/files clear inbox|outbox|all",
    ]


def build_discord_help_lines() -> list[str]:
    lines: list[str] = []
    for heading, command_ids in _DISCORD_SECTION_ORDER:
        if lines:
            lines.append("")
        lines.append(heading)
        if heading == "**File Commands:**":
            lines.extend(_discord_file_help_lines())
            continue
        for command_id in command_ids:
            line = _render_discord_line(command_id)
            if line is not None:
                lines.append(line)

    lines.extend(
        [
            "",
            "**Flow Commands:**",
            *flow_help_lines(prefix="/flow", usage_overrides={"runs": "[limit]"}),
            "",
            "Direct shell:",
            "!<cmd> - run a bash command in the bound workspace",
        ]
    )
    return lines


def build_telegram_help_text(command_names: Collection[str]) -> str:
    available = set(command_names)
    lines = ["Commands:"]
    for command_name in _TELEGRAM_COMMAND_ORDER:
        if command_name not in available:
            continue
        if command_name == "flow":
            lines.append("/flow - ticket flow controls")
            continue
        if command_name == "reply":
            lines.append("/reply <message> - reply to a paused ticket flow dispatch")
            continue
        line = _render_telegram_line(command_name)
        if line is not None:
            lines.append(line)

    if "review" in available:
        lines.extend(
            [
                "",
                "Review:",
                "/review",
                "/review pr [branch]",
                "/review commit <sha> (or /review commit to pick)",
                "/review custom <instructions> (or /review custom to prompt)",
                "/review detached ...",
            ]
        )

    if "flow" in available:
        lines.extend(
            [
                "",
                "Flow:",
                "/flow",
                *flow_help_lines(
                    prefix="/flow",
                    usage_overrides={"start": "[--force-new]", "reply": "<message>"},
                )[1:],
                "(Use /pma for full flow controls via web app)",
            ]
        )
        if "reply" in available:
            lines.append("/reply <message> (legacy)")

    file_lines = _telegram_file_help_lines(available)
    if file_lines:
        lines.extend(["", "Files:", *file_lines])

    lines.extend(
        [
            "",
            "Other:",
            "Note: /resume is supported for the codex and opencode agents.",
            "!<cmd> - run a bash command in the bound workspace (non-interactive; long-running commands time out)",
        ]
    )
    return "\n".join(lines)


__all__ = ["build_discord_help_lines", "build_telegram_help_text"]
