"""Canonical `car describe` contract constants and shape hints.

This module is the single internal location for self-description contract
definitions. Surfaces should import from here instead of re-defining output
shape details.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_ID = "https://codex-autorunner.dev/schemas/car-describe.schema.json"
SCHEMA_VERSION = "1.0.0"
SCHEMA_FILENAME = "car-describe.schema.json"

# Effective merge order for repo-facing config values.
CONFIG_PRECEDENCE: tuple[str, ...] = (
    "built_in_defaults",
    "codex-autorunner.yml",
    "codex-autorunner.override.yml",
    ".codex-autorunner/config.yml",
    "environment_variables",
)

# Names only; never include resolved values in `car describe --json`.
NON_SECRET_ENV_KNOBS: tuple[str, ...] = (
    "CAR_GLOBAL_STATE_ROOT",
    "CAR_TELEGRAM_BOT_TOKEN",
    "CAR_TELEGRAM_CHAT_ID",
    "CAR_TELEGRAM_THREAD_ID",
    "CAR_DISCORD_BOT_TOKEN",
    "CAR_DISCORD_APP_ID",
    "CAR_DISCORD_WEBHOOK_URL",
    "OPENAI_API_KEY",
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
)


def default_runtime_schema_path(repo_root: Path) -> Path:
    """Return the canonical runtime schema path for `car describe --json`."""

    return repo_root / ".codex-autorunner" / "docs" / SCHEMA_FILENAME
