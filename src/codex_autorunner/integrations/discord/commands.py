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
                    "description": "Show bot status",
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
