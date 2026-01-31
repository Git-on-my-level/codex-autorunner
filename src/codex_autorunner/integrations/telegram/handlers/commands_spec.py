from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..adapter import TelegramMessage


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[TelegramMessage, str, Any], Awaitable[None]]
    allow_during_turn: bool = False


def build_command_specs(handlers: Any) -> dict[str, CommandSpec]:
    return {
        "bind": CommandSpec(
            "bind",
            "bind this topic to a workspace",
            lambda message, args, _runtime: handlers._handle_bind(message, args),
        ),
        "new": CommandSpec(
            "new",
            "start a new session",
            lambda message, _args, _runtime: handlers._handle_new(message),
        ),
        "resume": CommandSpec(
            "resume",
            "list or resume a previous session",
            lambda message, args, _runtime: handlers._handle_resume(message, args),
        ),
        "review": CommandSpec(
            "review",
            "run a code review",
            handlers._handle_review,
        ),
        "flow": CommandSpec(
            "flow",
            "ticket flow controls (status, runs, bootstrap, resume, stop, archive, reply)",
            lambda message, args, _runtime: handlers._handle_flow(message, args),
            allow_during_turn=True,
        ),
        "flow_status": CommandSpec(
            "flow_status",
            "show ticket flow status (alias for /flow status)",
            lambda message, args, _runtime: handlers._handle_flow_status(message, args),
            allow_during_turn=True,
        ),
        "reply": CommandSpec(
            "reply",
            "reply to a paused ticket flow dispatch (prefer /flow reply)",
            lambda message, args, _runtime: handlers._handle_reply(message, args),
            allow_during_turn=True,
        ),
        "agent": CommandSpec(
            "agent",
            "show or set the active agent",
            handlers._handle_agent,
        ),
        "model": CommandSpec(
            "model",
            "list or set the model",
            handlers._handle_model,
        ),
        "approvals": CommandSpec(
            "approvals",
            "set approval and sandbox policy",
            handlers._handle_approvals,
        ),
        "pma": CommandSpec(
            "pma",
            "toggle PMA mode for this topic",
            handlers._handle_pma,
            allow_during_turn=True,
        ),
        "status": CommandSpec(
            "status",
            "show current binding and thread status",
            handlers._handle_status,
            allow_during_turn=True,
        ),
        "files": CommandSpec(
            "files",
            "list or manage Telegram file inbox/outbox",
            handlers._handle_files,
            allow_during_turn=True,
        ),
        "debug": CommandSpec(
            "debug",
            "show topic debug info",
            handlers._handle_debug,
            allow_during_turn=True,
        ),
        "ids": CommandSpec(
            "ids",
            "show chat/user/thread IDs",
            handlers._handle_ids,
            allow_during_turn=True,
        ),
        "diff": CommandSpec(
            "diff",
            "show git diff for the bound workspace",
            handlers._handle_diff,
            allow_during_turn=True,
        ),
        "mention": CommandSpec(
            "mention",
            "include a file in a new request",
            handlers._handle_mention,
            allow_during_turn=True,
        ),
        "skills": CommandSpec(
            "skills",
            "list available skills",
            handlers._handle_skills,
            allow_during_turn=True,
        ),
        "mcp": CommandSpec(
            "mcp",
            "list MCP server status",
            handlers._handle_mcp,
            allow_during_turn=True,
        ),
        "experimental": CommandSpec(
            "experimental",
            "toggle experimental features",
            handlers._handle_experimental,
        ),
        "init": CommandSpec(
            "init",
            "generate AGENTS.md guidance",
            handlers._handle_init,
        ),
        "compact": CommandSpec(
            "compact",
            "compact the conversation (summary)",
            handlers._handle_compact,
        ),
        "rollout": CommandSpec(
            "rollout",
            "show current thread rollout path",
            handlers._handle_rollout,
            allow_during_turn=True,
        ),
        "update": CommandSpec(
            "update",
            "update CAR (prompt or both|web|telegram)",
            handlers._handle_update,
        ),
        "logout": CommandSpec(
            "logout",
            "log out of the Codex account",
            handlers._handle_logout,
        ),
        "feedback": CommandSpec(
            "feedback",
            "send feedback and logs",
            handlers._handle_feedback,
            allow_during_turn=True,
        ),
        "interrupt": CommandSpec(
            "interrupt",
            "stop the active turn",
            lambda message, _args, runtime: handlers._handle_interrupt(
                message, runtime
            ),
            allow_during_turn=True,
        ),
        "help": CommandSpec(
            "help",
            "show this help message",
            handlers._handle_help,
            allow_during_turn=True,
        ),
    }
