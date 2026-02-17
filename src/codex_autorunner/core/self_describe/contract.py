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
    "CODEX_HOME",
    "CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS",
    "CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS",
    "CODEX_AUTORUNNER_VOICE_ENABLED",
    "CODEX_AUTORUNNER_VOICE_PROVIDER",
    "CODEX_AUTORUNNER_VOICE_LATENCY",
    "CODEX_AUTORUNNER_VOICE_CHUNK_MS",
    "CODEX_AUTORUNNER_VOICE_SAMPLE_RATE",
    "CODEX_AUTORUNNER_VOICE_WARN_REMOTE",
    "CODEX_AUTORUNNER_VOICE_MAX_MS",
    "CODEX_AUTORUNNER_VOICE_SILENCE_MS",
    "CODEX_AUTORUNNER_VOICE_MIN_HOLD_MS",
    "CAR_OPENCODE_COMMAND",
    "CAR_TELEGRAM_APP_SERVER_COMMAND",
    "CAR_TELEGRAM_BOT_TOKEN",
    "CAR_TELEGRAM_CHAT_ID",
    "CAR_TELEGRAM_THREAD_ID",
    "OPENAI_API_KEY",
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
)


def default_runtime_schema_path(repo_root: Path) -> Path:
    """Return the canonical runtime schema path for `car describe --json`."""

    return repo_root / ".codex-autorunner" / "docs" / SCHEMA_FILENAME
