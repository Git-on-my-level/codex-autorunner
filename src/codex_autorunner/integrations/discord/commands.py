from __future__ import annotations

from typing import Any

# Discord application command option types.
SUB_COMMAND = 1
SUB_COMMAND_GROUP = 2
STRING = 3
INTEGER = 4


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
                            "description": "Workspace path (optional - shows picker if omitted)",
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
                    "description": "Reset branch from origin/main and start a fresh chat session",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "debug",
                    "description": "Show debug info for troubleshooting",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "agent",
                    "description": "View or set the agent",
                    "options": [
                        {
                            "type": STRING,
                            "name": "name",
                            "description": "Agent name: codex or opencode",
                            "required": False,
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
                        },
                        {
                            "type": STRING,
                            "name": "effort",
                            "description": "Reasoning effort (codex only): none, minimal, low, medium, high, xhigh",
                            "required": False,
                        },
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "update",
                    "description": "Update CAR service (both, web, chat, telegram, discord, or status)",
                    "options": [
                        {
                            "type": STRING,
                            "name": "target",
                            "description": "Target: both, web, chat, telegram, discord, or status (default: both)",
                            "required": False,
                        }
                    ],
                },
                {
                    "type": SUB_COMMAND,
                    "name": "help",
                    "description": "Show available commands",
                },
                {
                    "type": SUB_COMMAND,
                    "name": "ids",
                    "description": "Show channel/user IDs for debugging",
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
                },
                {
                    "type": SUB_COMMAND,
                    "name": "mcp",
                    "description": "Show MCP server status",
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
                    "name": "experimental",
                    "description": "Toggle experimental features",
                    "options": [
                        {
                            "type": STRING,
                            "name": "action",
                            "description": "list, enable, or disable",
                            "required": False,
                        },
                        {
                            "type": STRING,
                            "name": "feature",
                            "description": "Feature name (for enable/disable)",
                            "required": False,
                        },
                    ],
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
                    "name": "flow",
                    "description": "Manage flow runs",
                    "options": [
                        {
                            "type": SUB_COMMAND,
                            "name": "status",
                            "description": "Show flow status",
                            "options": [
                                {
                                    "type": STRING,
                                    "name": "run_id",
                                    "description": "Flow run id",
                                    "required": False,
                                }
                            ],
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "runs",
                            "description": "List flow runs",
                            "options": [
                                {
                                    "type": INTEGER,
                                    "name": "limit",
                                    "description": "Max runs (default 5)",
                                    "required": False,
                                }
                            ],
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "resume",
                            "description": "Resume a flow",
                            "options": [
                                {
                                    "type": STRING,
                                    "name": "run_id",
                                    "description": "Flow run id",
                                    "required": False,
                                }
                            ],
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "stop",
                            "description": "Stop a flow",
                            "options": [
                                {
                                    "type": STRING,
                                    "name": "run_id",
                                    "description": "Flow run id",
                                    "required": False,
                                }
                            ],
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "archive",
                            "description": "Archive a flow",
                            "options": [
                                {
                                    "type": STRING,
                                    "name": "run_id",
                                    "description": "Flow run id",
                                    "required": False,
                                }
                            ],
                        },
                        {
                            "type": SUB_COMMAND,
                            "name": "reply",
                            "description": "Reply to paused flow",
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
                                },
                            ],
                        },
                    ],
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
    ]
