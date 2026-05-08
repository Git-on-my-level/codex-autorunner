"""Lightweight command contract manifest for cross-surface parity checks."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional, overload

from .action_ux_contract import (
    DiscordAckPolicy,
    DiscordAckTiming,
    DiscordExposure,
    TelegramExposure,
    TelegramResponsePolicy,
    discord_ack_policy_for_entry,
    discord_exposure_for_entry,
    discord_slash_command_ux_contract_for_id,
    telegram_allow_during_turn_for_entry,
    telegram_command_ux_contract_for_name,
    telegram_exposure_for_entry,
    telegram_response_policy_for_entry,
)

CommandStatus = Literal["stable", "partial", "unsupported"]


@dataclass(frozen=True)
class CommandContractEntry:
    id: str
    path: tuple[str, ...]
    requires_bound_workspace: bool
    status: CommandStatus
    telegram_commands: tuple[str, ...] = ()
    discord_paths: tuple[tuple[str, ...], ...] = ()
    discord_ack_policy: Optional[DiscordAckPolicy] = None
    discord_ack_timing: DiscordAckTiming = "dispatch"
    discord_exposure: Optional[DiscordExposure] = None
    required_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramCommandMetadata:
    exposure: TelegramExposure
    response_policy: TelegramResponsePolicy
    allow_during_turn: bool


def _apply_discord_registry(entry: CommandContractEntry) -> CommandContractEntry:
    from ..discord.interaction_registry import discord_contract_metadata_for_id

    metadata = discord_contract_metadata_for_id(entry.id)
    ux_entry = discord_slash_command_ux_contract_for_id(entry.id)
    required_capabilities = (
        metadata["required_capabilities"] or entry.required_capabilities
    )
    has_cataloged_discord_paths = bool(metadata["discord_paths"])
    return CommandContractEntry(
        id=entry.id,
        path=entry.path,
        requires_bound_workspace=entry.requires_bound_workspace,
        status=entry.status,
        telegram_commands=entry.telegram_commands,
        discord_paths=metadata["discord_paths"],
        discord_ack_policy=(
            discord_ack_policy_for_entry(ux_entry)
            if has_cataloged_discord_paths
            else None
        ),
        discord_ack_timing=(
            ux_entry.ack_timing
            if has_cataloged_discord_paths and ux_entry is not None
            else "dispatch"
        ),
        discord_exposure=(
            discord_exposure_for_entry(ux_entry)
            if has_cataloged_discord_paths
            else None
        ),
        required_capabilities=required_capabilities,
    )


_COMMAND_CONTRACT_BASE: tuple[CommandContractEntry, ...] = (
    # Cross-surface core commands (stable parity).
    CommandContractEntry(
        id="car.bind",
        path=("car", "bind"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("bind",),
        discord_paths=(("car", "bind"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.status",
        path=("car", "status"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("status",),
        discord_paths=(("car", "status"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.processes",
        path=("car", "processes"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("processes",),
        discord_paths=(("car", "processes"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.new",
        path=("car", "new"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("new",),
        discord_paths=(("car", "new"),),
        discord_ack_policy="defer_public",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.newt",
        path=("car", "newt"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("newt",),
        discord_paths=(("car", "newt"),),
        discord_ack_policy="defer_public",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.debug",
        path=("car", "debug"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("debug",),
        discord_paths=(("car", "admin", "debug"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="operator",
    ),
    CommandContractEntry(
        id="car.agent",
        path=("car", "agent"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("agent",),
        discord_paths=(("car", "agent"),),
        discord_ack_policy="immediate",
        discord_exposure="public",
        required_capabilities=("agent_selection",),
    ),
    CommandContractEntry(
        id="car.model",
        path=("car", "model"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("model",),
        discord_paths=(("car", "model"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("model_selection",),
    ),
    CommandContractEntry(
        id="car.update",
        path=("car", "update"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("update",),
        discord_paths=(("car", "update"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("service_update",),
    ),
    CommandContractEntry(
        id="car.help",
        path=("car", "help"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("help",),
        discord_paths=(("car", "admin", "help"),),
        discord_ack_policy="immediate",
        discord_exposure="operator",
    ),
    CommandContractEntry(
        id="car.ids",
        path=("car", "ids"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("ids",),
        discord_paths=(("car", "admin", "ids"),),
        discord_ack_policy="immediate",
        discord_exposure="operator",
    ),
    CommandContractEntry(
        id="car.diff",
        path=("car", "diff"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("diff",),
        discord_paths=(("car", "diff"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.skills",
        path=("car", "skills"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("skills",),
        discord_paths=(("car", "skills"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.tickets",
        path=("car", "tickets"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("tickets",),
        discord_paths=(("car", "tickets"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.contextspace",
        path=("car", "contextspace"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("contextspace",),
        discord_paths=(("car", "contextspace"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    CommandContractEntry(
        id="car.mcp",
        path=("car", "mcp"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("mcp",),
    ),
    CommandContractEntry(
        id="car.init",
        path=("car", "init"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("init",),
        discord_paths=(("car", "admin", "init"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="operator",
    ),
    CommandContractEntry(
        id="car.repos",
        path=("car", "repos"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("repos",),
        discord_paths=(("car", "admin", "repos"),),
        discord_ack_policy="immediate",
        discord_exposure="operator",
    ),
    CommandContractEntry(
        id="car.archive",
        path=("car", "archive"),
        requires_bound_workspace=True,
        status="stable",
        telegram_commands=("archive",),
        discord_paths=(("car", "archive"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
    ),
    # Commands with cross-surface shape differences (partial parity).
    CommandContractEntry(
        id="car.files.inbox",
        path=("car", "files", "inbox"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("files",),
        discord_paths=(("car", "files", "inbox"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("filebox_access",),
    ),
    CommandContractEntry(
        id="car.files.outbox",
        path=("car", "files", "outbox"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("files",),
        discord_paths=(("car", "files", "outbox"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("filebox_access",),
    ),
    CommandContractEntry(
        id="car.files.clear",
        path=("car", "files", "clear"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("files",),
        discord_paths=(("car", "files", "clear"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("filebox_access",),
    ),
    CommandContractEntry(
        id="car.flow.status",
        path=("car", "flow", "status"),
        requires_bound_workspace=False,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "status"),),
        discord_ack_policy="defer_public",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.runs",
        path=("car", "flow", "runs"),
        requires_bound_workspace=False,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "runs"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.issue",
        path=("car", "flow", "issue"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "issue"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow", "github_cli"),
    ),
    CommandContractEntry(
        id="car.flow.plan",
        path=("car", "flow", "plan"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "plan"),),
        discord_ack_policy="immediate",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.start",
        path=("car", "flow", "start"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "start"),),
        discord_ack_policy="defer_public",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.restart",
        path=("car", "flow", "restart"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "restart"),),
        discord_ack_policy="defer_public",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.resume",
        path=("car", "flow", "resume"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "resume"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.stop",
        path=("car", "flow", "stop"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "stop"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.archive",
        path=("car", "flow", "archive"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "archive"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.recover",
        path=("car", "flow", "recover"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow",),
        discord_paths=(("flow", "recover"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="car.flow.reply",
        path=("car", "flow", "reply"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("flow", "reply"),
        discord_paths=(("flow", "reply"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    CommandContractEntry(
        id="pma.on",
        path=("pma", "on"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("pma",),
        discord_paths=(("pma", "on"),),
        discord_ack_policy="immediate",
        discord_exposure="public",
        required_capabilities=("pma_mode",),
    ),
    CommandContractEntry(
        id="pma.off",
        path=("pma", "off"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("pma",),
        discord_paths=(("pma", "off"),),
        discord_ack_policy="immediate",
        discord_exposure="public",
        required_capabilities=("pma_mode",),
    ),
    CommandContractEntry(
        id="pma.status",
        path=("pma", "status"),
        requires_bound_workspace=False,
        status="stable",
        telegram_commands=("pma",),
        discord_paths=(("pma", "status"),),
        discord_ack_policy="immediate",
        discord_exposure="public",
        required_capabilities=("pma_mode",),
    ),
    CommandContractEntry(
        id="car.resume",
        path=("car", "resume"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("resume",),
        discord_paths=(("car", "session", "resume"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("durable_threads",),
    ),
    CommandContractEntry(
        id="car.reset",
        path=("car", "reset"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("reset",),
        discord_paths=(("car", "session", "reset"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("durable_threads",),
    ),
    CommandContractEntry(
        id="car.review",
        path=("car", "review"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("review",),
        discord_paths=(("car", "review"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("review",),
    ),
    CommandContractEntry(
        id="car.approvals",
        path=("car", "approvals"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("approvals",),
        discord_paths=(("car", "approvals"),),
        discord_ack_policy="immediate",
        discord_exposure="public",
        required_capabilities=("approval_policy", "sandbox_policy"),
    ),
    CommandContractEntry(
        id="car.mention",
        path=("car", "mention"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("mention",),
        discord_paths=(("car", "mention"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("file_mentions",),
    ),
    CommandContractEntry(
        id="car.experimental",
        path=("car", "experimental"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("experimental",),
        required_capabilities=("feature_flags",),
    ),
    CommandContractEntry(
        id="car.compact",
        path=("car", "compact"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("compact",),
        discord_paths=(("car", "session", "compact"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("message_turns",),
    ),
    CommandContractEntry(
        id="car.rollout",
        path=("car", "rollout"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("rollout",),
        discord_paths=(("car", "admin", "rollout"),),
        discord_ack_policy="immediate",
        discord_exposure="operator",
    ),
    CommandContractEntry(
        id="car.logout",
        path=("car", "logout"),
        requires_bound_workspace=False,
        status="partial",
        telegram_commands=("logout",),
        discord_paths=(("car", "session", "logout"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("auth_session",),
    ),
    CommandContractEntry(
        id="car.feedback",
        path=("car", "feedback"),
        requires_bound_workspace=False,
        status="partial",
        telegram_commands=("feedback",),
        discord_paths=(("car", "admin", "feedback"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="operator",
        required_capabilities=("feedback_reporting",),
    ),
    CommandContractEntry(
        id="car.interrupt",
        path=("car", "interrupt"),
        requires_bound_workspace=True,
        status="partial",
        telegram_commands=("interrupt",),
        discord_paths=(("car", "session", "interrupt"),),
        discord_ack_policy="defer_ephemeral",
        discord_exposure="public",
        required_capabilities=("interrupt",),
    ),
)


@lru_cache(maxsize=1)
def _resolved_command_contract() -> tuple[CommandContractEntry, ...]:
    return tuple(_apply_discord_registry(entry) for entry in _COMMAND_CONTRACT_BASE)


class _LazyCommandContract(Sequence[CommandContractEntry]):
    def _entries(self) -> tuple[CommandContractEntry, ...]:
        return _resolved_command_contract()

    @overload
    def __getitem__(self, index: int) -> CommandContractEntry: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[CommandContractEntry]: ...

    def __getitem__(
        self, index: int | slice
    ) -> CommandContractEntry | Sequence[CommandContractEntry]:
        return self._entries()[index]

    def __iter__(self) -> Iterator[CommandContractEntry]:
        return iter(self._entries())

    def __len__(self) -> int:
        return len(self._entries())


COMMAND_CONTRACT: Sequence[CommandContractEntry] = _LazyCommandContract()


def telegram_command_metadata_for_name(
    name: str,
) -> Optional[TelegramCommandMetadata]:
    ux_entry = telegram_command_ux_contract_for_name(name)
    exposure = telegram_exposure_for_entry(ux_entry)
    response_policy = telegram_response_policy_for_entry(ux_entry)
    allow_during_turn = telegram_allow_during_turn_for_entry(ux_entry)
    if exposure is None or response_policy is None or allow_during_turn is None:
        return None
    return TelegramCommandMetadata(
        exposure=exposure,
        response_policy=response_policy,
        allow_during_turn=allow_during_turn,
    )


def telegram_runtime_command_names_from_contract(
    contract: Sequence[CommandContractEntry] = COMMAND_CONTRACT,
) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for entry in contract:
        for name in entry.telegram_commands:
            normalized = name.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            names.append(normalized)
    return tuple(names)
