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
                            "name": "path",
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
