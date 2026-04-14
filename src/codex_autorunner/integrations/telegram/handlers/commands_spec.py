from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ....core.flows import flow_action_summary
from ....core.update_targets import update_target_values
from ...chat.command_contract import (
    TelegramResponsePolicy,
    telegram_command_metadata_for_name,
)
from ..adapter import TelegramMessage


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[TelegramMessage, str, Any], Awaitable[None]]
    allow_during_turn: bool = False
    exposed: bool = True
    legacy_alias: bool = False
    response_policy: TelegramResponsePolicy = "typing"


def build_command_specs(handlers: Any) -> dict[str, CommandSpec]:
    def _spec(
        name: str,
        description: str,
        handler: Callable[[TelegramMessage, str, Any], Awaitable[None]],
    ) -> CommandSpec:
        policy = telegram_command_metadata_for_name(name)
        if policy is None:
            raise ValueError(f"missing Telegram command metadata for {name}")
        return CommandSpec(
            name=name,
            description=description,
            handler=handler,
            allow_during_turn=policy.allow_during_turn,
            exposed=policy.exposure == "public",
            legacy_alias=policy.exposure == "legacy_alias",
            response_policy=policy.response_policy,
        )

    return {
        "repos": _spec(
            "repos",
            "list available repositories in the hub",
            handlers._handle_repos,
        ),
        "bind": _spec(
            "bind",
            "bind this topic to a workspace",
            lambda message, args, _runtime: handlers._handle_bind(message, args),
        ),
        "new": _spec(
            "new",
            "start a new PMA session",
            lambda message, _args, _runtime: handlers._handle_new(message),
        ),
        "newt": _spec(
            "newt",
            "reset current workspace branch from origin default branch and start a new session",
            lambda message, _args, _runtime: handlers._handle_newt(message),
        ),
        "archive": _spec(
            "archive",
            "archive workspace state for a fresh start",
            lambda message, _args, _runtime: handlers._handle_archive(message),
        ),
        "reset": _spec(
            "reset",
            "reset PMA thread state (clear volatile state)",
            lambda message, _args, _runtime: handlers._handle_reset(message),
        ),
        "resume": _spec(
            "resume",
            "list or resume a previous session",
            lambda message, args, _runtime: handlers._handle_resume(message, args),
        ),
        "review": _spec(
            "review",
            "run a code review when supported by the active agent",
            handlers._handle_review,
        ),
        "flow": _spec(
            "flow",
            f"ticket flow controls ({flow_action_summary()})",
            lambda message, args, _runtime: handlers._handle_flow(message, args),
        ),
        "reply": _spec(
            "reply",
            "reply to a paused ticket flow dispatch (prefer /flow reply)",
            lambda message, args, _runtime: handlers._handle_reply(message, args),
        ),
        "agent": _spec(
            "agent",
            "show or set the active agent",
            handlers._handle_agent,
        ),
        "model": _spec(
            "model",
            "list or set the active model when supported",
            handlers._handle_model,
        ),
        "approvals": _spec(
            "approvals",
            "set approval and sandbox policy",
            handlers._handle_approvals,
        ),
        "pma": _spec(
            "pma",
            "PMA mode controls (on/off/status)",
            handlers._handle_pma,
        ),
        "status": _spec(
            "status",
            "show current binding, thread, and collaboration status",
            handlers._handle_status,
        ),
        "processes": _spec(
            "processes",
            "show process monitor summary",
            handlers._handle_processes,
        ),
        "files": _spec(
            "files",
            "list or manage Telegram file inbox/outbox",
            handlers._handle_files,
        ),
        "debug": _spec(
            "debug",
            "show topic debug info and effective collaboration policy",
            handlers._handle_debug,
        ),
        "ids": _spec(
            "ids",
            "show chat/user/thread IDs and collaboration snippets",
            handlers._handle_ids,
        ),
        "diff": _spec(
            "diff",
            "show git diff for the bound workspace",
            handlers._handle_diff,
        ),
        "mention": _spec(
            "mention",
            "include a file in a new request",
            handlers._handle_mention,
        ),
        "skills": _spec(
            "skills",
            "list available skills",
            handlers._handle_skills,
        ),
        "mcp": _spec(
            "mcp",
            "list MCP server status",
            handlers._handle_mcp,
        ),
        "experimental": _spec(
            "experimental",
            "toggle experimental features",
            handlers._handle_experimental,
        ),
        "init": _spec(
            "init",
            "generate AGENTS.md guidance",
            handlers._handle_init,
        ),
        "compact": _spec(
            "compact",
            "compact the conversation (summary)",
            handlers._handle_compact,
        ),
        "rollout": _spec(
            "rollout",
            "show current thread rollout path",
            handlers._handle_rollout,
        ),
        "update": _spec(
            "update",
            (
                "update CAR (prompt or "
                f"{'|'.join(update_target_values(include_status=True))})"
            ),
            handlers._handle_update,
        ),
        "logout": _spec(
            "logout",
            "log out of the Codex account",
            handlers._handle_logout,
        ),
        "feedback": _spec(
            "feedback",
            "send feedback and logs",
            handlers._handle_feedback,
        ),
        "interrupt": _spec(
            "interrupt",
            "stop the active turn",
            lambda message, _args, runtime: handlers._handle_interrupt(
                message, runtime
            ),
        ),
        "help": _spec(
            "help",
            "show this help message",
            handlers._handle_help,
        ),
    }
