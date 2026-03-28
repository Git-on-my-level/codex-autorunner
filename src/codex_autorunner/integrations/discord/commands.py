from __future__ import annotations

from typing import Any

from ...core.flows import FLOW_ACTION_SPECS
from ...core.update_targets import update_target_command_choices
from ..chat.agents import chat_agent_command_choices, chat_agent_description
from ..chat.model_selection import (
    reasoning_effort_command_choices,
    reasoning_effort_description,
)

# Discord application command option types.
SUB_COMMAND = 1
SUB_COMMAND_GROUP = 2
STRING = 3
INTEGER = 4
BOOLEAN = 5


def _build_flow_subcommand_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for spec in FLOW_ACTION_SPECS:
        if spec.name == "status":
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": STRING,
                            "name": "run_id",
                            "description": "Flow run id",
                            "required": False,
                            "autocomplete": True,
                        }
                    ],
                }
            )
        elif spec.name == "runs":
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": INTEGER,
                            "name": "limit",
                            "description": "Max runs (default 5)",
                            "required": False,
                        }
                    ],
                }
            )
        elif spec.name == "issue":
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": STRING,
                            "name": "issue_ref",
                            "description": "Issue number or URL",
                            "required": True,
                        }
                    ],
                }
            )
        elif spec.name == "plan":
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": STRING,
                            "name": "text",
                            "description": "Plan text",
                            "required": True,
                        }
                    ],
                }
            )
        elif spec.name == "start":
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": BOOLEAN,
                            "name": "force_new",
                            "description": "Start a new run even if one is active/paused",
                            "required": False,
                        }
                    ],
                }
            )
        elif spec.name in {"restart", "resume", "stop", "archive", "recover"}:
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": STRING,
                            "name": "run_id",
                            "description": "Flow run id",
                            "required": False,
                            "autocomplete": True,
                        }
                    ],
                }
            )
        elif spec.name == "reply":
            options.append(
                {
                    "type": SUB_COMMAND,
                    "name": spec.name,
                    "description": spec.description,
                    "options": [
                        {
                            "type": STRING,
                            "name": "text",
                            "description": "Reply text",
                            "required": True,
                        },
                        {
                            "type": STRING,
                            "name": "run_id",
                            "description": "Flow run id",
                            "required": False,
                            "autocomplete": True,
                        },
                    ],
                }
            )
        else:
            raise AssertionError(f"Unhandled flow action spec: {spec.name}")
    return options


def _build_admin_subcommand_options() -> list[dict[str, Any]]:
    return [
        {
            "type": SUB_COMMAND,
            "name": "help",
            "description": "Show available commands",
        },
        {
            "type": SUB_COMMAND,
            "name": "debug",
            "description": "Show debug info for troubleshooting",
        },
        {
            "type": SUB_COMMAND,
            "name": "ids",
            "description": "Show channel/user IDs for debugging",
        },
        {
            "type": SUB_COMMAND,
            "name": "init",
            "description": "Generate AGENTS.md",
        },
        {
            "type": SUB_COMMAND,
            "name": "repos",
            "description": "List hub repositories",
        },
        {
            "type": SUB_COMMAND,
            "name": "rollout",
            "description": "Show current thread rollout path",
        },
        {
            "type": SUB_COMMAND,
            "name": "feedback",
            "description": "Send feedback and logs",
            "options": [
                {
                    "type": STRING,
                    "name": "reason",
                    "description": "Feedback reason/description",
                    "required": True,
                }
            ],
        },
    ]


def build_application_commands() -> list[dict[str, Any]]:
    return [
        {
            "type": 1,
            "name": "car",
            "description": "Codex Autorunner commands",
            "options": [
                {
                    "type": SUB_COMMAND,
                    "name": "bind",
                    "description": "Bind channel to workspace",
                    "options": [
                        {
                            "type": STRING,
                            "name": "workspace",
                            "description": "Workspace path or repo id (optional - shows picker if omitted)",
                            "autocomplete": True,
                            "required": False,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "status",
                    "description": "Show binding status and active session info",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "new",
                    "description": "Start a fresh chat session for this channel",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "newt",
                    "description": "Reset branch from origin default branch and start a fresh chat session",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "agent",
                    "description": "View or set the agent",
                    "options": [
                        {
                            "type": STRING,
                            "name": "name",
                            "description": f"Agent name: {chat_agent_description()}",
                            "required": False,
                            "choices": list(chat_agent_command_choices()),
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "model",
                    "description": "View or set the model",
                    "options": [
                        {
                            "type": STRING,
                            "name": "name",
                            "description": "Model name (e.g., gpt-5.3-codex or provider/model)",
                            "required": False,
                            "autocomplete": True,
                        },
                        {
                            "type": STRING,
                            "name": "effort",
                            "description": (
                                "Reasoning effort (when supported): "
                                f"{reasoning_effort_description()}"
                            ),
                            "required": False,
                            "choices": list(reasoning_effort_command_choices()),
                        },
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "update",
                    "description": "Update CAR service",
                    "options": [
                        {
                            "type": STRING,
                            "name": "target",
                            "description": "Target: all, web, chat, telegram, discord, or status",
                            "required": False,
                            "choices": list(
                                update_target_command_choices(include_status=True)
                            ),
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "diff",
                    "description": "Show git diff",
                    "options": [
                        {
                            "type": STRING,
                            "name": "path",
                            "description": "Optional path to diff",
                            "required": False,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "skills",
                    "description": "List available skills",
                    "options": [
                        {
                            "type": STRING,
                            "name": "search",
                            "description": "Optional search text to filter skills",
                            "required": False,
                            "autocomplete": True,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "tickets",
                    "description": "Manage tickets via modal",
                    "options": [
                        {
                            "type": STRING,
                            "name": "search",
                            "description": "Optional search text to filter tickets",
                            "required": False,
                            "autocomplete": True,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "review",
                    "description": "Run a code review",
                    "options": [
                        {
                            "type": STRING,
                            "name": "target",
                            "description": "Review target: uncommitted, base <branch>, commit <sha>, or custom",
                            "required": False,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "approvals",
                    "description": "Set approval and sandbox policy",
                    "options": [
                        {
                            "type": STRING,
                            "name": "mode",
                            "description": "Mode: yolo, safe, read-only, auto, or full-access",
                            "required": False,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "mention",
                    "description": "Include a file in a new request",
                    "options": [
                        {
                            "type": STRING,
                            "name": "path",
                            "description": "Path to the file to include",
                            "required": True,
                        },
                        {
                            "type": STRING,
                            "name": "request",
                            "description": "Optional request text",
                            "required": False,
                        },
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "archive",
                    "description": "Archive workspace state for a fresh start",
                },
                {
                    "type": SUB_COMMAND_GROUP,
                    "name": "session",
                    "description": "Session management commands",
                    "options": [
                        {
                            "type": SUB_COMMAND,
                            "name": "resume",
                            "description": "Resume a previous chat thread",
                            "options": [
                                {
                                    "type": STRING,
                                    "name": "thread_id",
                                    "description": "Thread ID to resume (optional - lists recent threads if omitted)",
                                    "required": False,
                                    "autocomplete": True,
                                }
                            ],
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "reset",
                            "description": "Reset PMA thread state (clear volatile state)",
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "compact",
                            "description": "Compact the conversation (summary)",
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "interrupt",
                            "description": "Stop the active turn",
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "logout",
                            "description": "Log out of the Codex account",
                        },
                    ],
                },
                {
                    "type": SUB_COMMAND_GROUP,
                    "name": "files",
                    "description": "Manage file inbox/outbox",
                    "options": [
                        {
                            "type": SUB_COMMAND,
                            "name": "inbox",
                            "description": "List files in inbox",
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "outbox",
                            "description": "List pending outbox files",
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "clear",
                            "description": "Clear inbox/outbox files",
                            "options": [
                                {
                                    "type": STRING,
                                    "name": "target",
                                    "description": "inbox, outbox, or all (default: all)",
                                    "required": False,
                                }
                            ],
                        },
                    ],
                },
                {
                    "type": SUB_COMMAND_GROUP,
                    "name": "admin",
                    "description": "Admin and operator commands",
                    "options": _build_admin_subcommand_options(),
                },
            ],
        },
        {
            "type": 1,
            "name": "pma",
            "description": "Proactive Mode Agent commands",
            "options": [
                {
                    "type": SUB_COMMAND,
                    "name": "on",
                    "description": "Enable PMA mode for this channel",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "off",
                    "description": "Disable PMA mode and restore previous binding",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "status",
                    "description": "Show PMA mode status",
                },
            ],
        },
        {
            "type": 1,
            "name": "flow",
            "description": "Ticket flow commands",
            "options": _build_flow_subcommand_options(),
        },
    ]
