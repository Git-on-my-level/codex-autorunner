"""Discord integration doctor checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from ...core.config import HubConfig
from ...core.optional_dependencies import missing_optional_dependencies
from ...core.runtime import DoctorCheck
from ...voice.config import VoiceConfig
from ...voice.provider_catalog import (
    local_voice_provider_spec,
    missing_local_voice_runtime_commands,
)
from .config import (
    DEFAULT_APP_ID_ENV,
    DEFAULT_BOT_TOKEN_ENV,
    DEFAULT_INTENTS,
    DEFAULT_STATE_FILE,
    LEGACY_DEFAULT_INTENTS,
)
from .constants import DISCORD_INTENT_MESSAGE_CONTENT

RECOMMENDED_BOT_PERMISSIONS_INTEGER = "2322563695115328"


def discord_doctor_checks(config: HubConfig) -> list[DoctorCheck]:
    """Run Discord-specific doctor checks for hub configuration."""
    checks: list[DoctorCheck] = []
    raw = config.raw if isinstance(config.raw, dict) else {}
    discord_bot_raw = raw.get("discord_bot")
    discord_cfg: dict[str, Any] = (
        discord_bot_raw if isinstance(discord_bot_raw, dict) else {}
    )
    enabled = bool(discord_cfg.get("enabled", False))

    missing_discord = missing_optional_dependencies((("websockets", "websockets"),))
    if missing_discord:
        deps_list = ", ".join(missing_discord)
        checks.append(
            DoctorCheck(
                name="Discord dependencies",
                passed=not enabled,
                message=(
                    f"Discord is enabled but missing optional deps: {deps_list}"
                    if enabled
                    else f"Discord optional deps not installed: {deps_list}"
                ),
                check_id="discord.dependencies",
                severity="error" if enabled else "warning",
                fix="Install with `pip install codex-autorunner[discord]`.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Discord dependencies",
                passed=True,
                message="Discord dependencies are installed.",
                check_id="discord.dependencies",
                severity="info",
            )
        )

    if not enabled:
        checks.append(
            DoctorCheck(
                name="Discord enabled",
                passed=True,
                message="Discord integration is disabled.",
                check_id="discord.enabled",
                severity="info",
                fix="Set discord_bot.enabled=true in config to enable.",
            )
        )
        return checks

    voice_raw = _resolve_voice_raw(raw)
    voice_config = VoiceConfig.from_raw(voice_raw, env=os.environ)
    voice_ingestion_enabled = _discord_voice_ingestion_enabled(discord_cfg)
    local_provider_spec = local_voice_provider_spec(voice_config.provider)
    if (
        voice_ingestion_enabled
        and voice_config.enabled
        and local_provider_spec is not None
    ):
        provider_name, deps, extra = local_provider_spec
        missing_local_voice = missing_optional_dependencies(deps)
        missing_runtime_commands = missing_local_voice_runtime_commands(provider_name)
        if missing_local_voice:
            missing_desc = ", ".join(missing_local_voice)
            runtime_hint = ""
            if missing_runtime_commands:
                missing_runtime_desc = ", ".join(missing_runtime_commands)
                runtime_hint = (
                    " Required runtime command(s) are also missing from PATH: "
                    f"{missing_runtime_desc}."
                )
            checks.append(
                DoctorCheck(
                    name="Discord voice dependencies",
                    passed=False,
                    message=(
                        "Discord voice transcription is configured with "
                        f"{provider_name} but {missing_desc} is not installed."
                        f"{runtime_hint}"
                    ),
                    check_id="discord.voice.dependencies",
                    severity="error",
                    fix=(
                        f"Install with `pip install codex-autorunner[{extra}]`."
                        + (
                            " Install ffmpeg and ensure it is on PATH (for macOS: "
                            "`brew install ffmpeg`)."
                            if "ffmpeg" in missing_runtime_commands
                            else ""
                        )
                    ),
                )
            )
        elif missing_runtime_commands:
            missing_runtime_desc = ", ".join(missing_runtime_commands)
            checks.append(
                DoctorCheck(
                    name="Discord voice dependencies",
                    passed=False,
                    message=(
                        "Discord voice transcription is configured with "
                        f"{provider_name} but required runtime command(s) are "
                        f"missing from PATH: {missing_runtime_desc}."
                    ),
                    check_id="discord.voice.dependencies",
                    severity="error",
                    fix=(
                        "Install ffmpeg and ensure it is on PATH (for macOS: "
                        "`brew install ffmpeg`)."
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Discord voice dependencies",
                    passed=True,
                    message=(
                        f"Discord voice transcription is using {provider_name} and "
                        "local dependencies are installed."
                    ),
                    check_id="discord.voice.dependencies",
                    severity="info",
                )
            )

    bot_token_env = str(discord_cfg.get("bot_token_env", DEFAULT_BOT_TOKEN_ENV)).strip()
    app_id_env = str(discord_cfg.get("app_id_env", DEFAULT_APP_ID_ENV)).strip()
    if not bot_token_env:
        bot_token_env = DEFAULT_BOT_TOKEN_ENV
    if not app_id_env:
        app_id_env = DEFAULT_APP_ID_ENV

    bot_token = os.environ.get(bot_token_env)
    app_id = os.environ.get(app_id_env)

    if bot_token:
        checks.append(
            DoctorCheck(
                name="Discord bot token",
                passed=True,
                message=f"Bot token configured (env: {bot_token_env}).",
                check_id="discord.bot_token",
                severity="info",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Discord bot token",
                passed=False,
                message=f"Discord bot token not found in environment: {bot_token_env}",
                check_id="discord.bot_token",
                fix=f"Set {bot_token_env} environment variable.",
            )
        )

    if app_id:
        checks.append(
            DoctorCheck(
                name="Discord application ID",
                passed=True,
                message=f"Application ID configured (env: {app_id_env}).",
                check_id="discord.app_id",
                severity="info",
            )
        )
        checks.append(
            DoctorCheck(
                name="Discord invite URL",
                passed=True,
                message=(
                    "Use this OAuth URL to (re)invite the bot with expected scopes "
                    "and permissions. This often fixes the case where slash commands "
                    "work but plain text turns do not.\n"
                    f"https://discord.com/oauth2/authorize?client_id={app_id}"
                    f"&permissions={RECOMMENDED_BOT_PERMISSIONS_INTEGER}"
                    "&scope=bot%20applications.commands"
                ),
                check_id="discord.invite_url",
                severity="info",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Discord application ID",
                passed=False,
                message=f"Discord application ID not found in environment: {app_id_env}",
                check_id="discord.app_id",
                fix=f"Set {app_id_env} environment variable.",
            )
        )
        checks.append(
            DoctorCheck(
                name="Discord invite URL",
                passed=True,
                message=(
                    "Set Discord application ID first, then generate a re-invite URL "
                    "with scopes `bot applications.commands` and permissions integer "
                    f"{RECOMMENDED_BOT_PERMISSIONS_INTEGER}."
                ),
                check_id="discord.invite_url",
                severity="info",
            )
        )

    allowed_guild_ids = _parse_string_ids(discord_cfg.get("allowed_guild_ids"))
    allowed_channel_ids = _parse_string_ids(discord_cfg.get("allowed_channel_ids"))
    allowed_user_ids = _parse_string_ids(discord_cfg.get("allowed_user_ids"))
    intents_value = discord_cfg.get("intents", DEFAULT_INTENTS)

    if isinstance(intents_value, int) and intents_value == LEGACY_DEFAULT_INTENTS:
        checks.append(
            DoctorCheck(
                name="Discord intents",
                passed=True,
                message=(
                    "discord_bot.intents is set to legacy value 513; runtime "
                    "auto-upgrades this to include MESSAGE_CONTENT."
                ),
                check_id="discord.intents",
                severity="warning",
                fix=(
                    "Update discord_bot.intents to 33281 (DEFAULT_INTENTS) to "
                    "make intent behavior explicit in config."
                ),
            )
        )
    elif (
        isinstance(intents_value, int)
        and intents_value & DISCORD_INTENT_MESSAGE_CONTENT
    ):
        checks.append(
            DoctorCheck(
                name="Discord intents",
                passed=True,
                message=f"Discord intents include MESSAGE_CONTENT ({intents_value}).",
                check_id="discord.intents",
                severity="info",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Discord intents",
                passed=False,
                message=(
                    "discord_bot.intents is missing DISCORD_INTENT_MESSAGE_CONTENT; "
                    "message content events will not be delivered."
                ),
                check_id="discord.intents",
                fix=(
                    "Set discord_bot.intents to 33281 (DEFAULT_INTENTS), or add "
                    "32768 (DISCORD_INTENT_MESSAGE_CONTENT) to your current bitmask."
                ),
            )
        )

    if not allowed_guild_ids and not allowed_channel_ids and not allowed_user_ids:
        checks.append(
            DoctorCheck(
                name="Discord allowlists",
                passed=False,
                message=(
                    "No allowlists configured (allowed_guild_ids, "
                    "allowed_channel_ids, or allowed_user_ids)"
                ),
                check_id="discord.allowlists",
                fix=(
                    "Configure at least one of discord_bot.allowed_guild_ids, "
                    "discord_bot.allowed_channel_ids, or "
                    "discord_bot.allowed_user_ids."
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Discord allowlists",
                passed=True,
                message=(
                    "Allowlists configured: "
                    f"{len(allowed_guild_ids)} guilds, "
                    f"{len(allowed_channel_ids)} channels, "
                    f"{len(allowed_user_ids)} users."
                ),
                check_id="discord.allowlists",
                severity="info",
            )
        )

    state_file = _resolve_state_file(config.root, discord_cfg.get("state_file"))
    writable, message, fix = _state_file_writable_check(state_file)
    checks.append(
        DoctorCheck(
            name="Discord state file",
            passed=writable,
            message=message,
            check_id="discord.state_file",
            severity="info" if writable else "error",
            fix=fix,
        )
    )

    return checks


def _resolve_state_file(root: Path, raw_state_file: object) -> Path:
    if isinstance(raw_state_file, str) and raw_state_file.strip():
        return (root / raw_state_file).resolve()
    return (root / DEFAULT_STATE_FILE).resolve()


def _resolve_voice_raw(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    repo_defaults = raw.get("repo_defaults")
    if isinstance(repo_defaults, dict):
        voice = repo_defaults.get("voice")
        if isinstance(voice, dict):
            return voice

    voice_raw = raw.get("voice")
    if isinstance(voice_raw, dict):
        return voice_raw
    return None


def _discord_voice_ingestion_enabled(discord_cfg: dict[str, Any]) -> bool:
    media_raw = discord_cfg.get("media")
    media_cfg = media_raw if isinstance(media_raw, dict) else {}
    media_enabled = bool(media_cfg.get("enabled", True))
    media_voice = bool(media_cfg.get("voice", True))
    return media_enabled and media_voice


def _state_file_writable_check(state_file: Path) -> tuple[bool, str, str | None]:
    if state_file.exists():
        try:
            with state_file.open("ab"):
                pass
        except OSError as exc:
            return (
                False,
                f"Discord state file is not writable: {state_file} ({exc})",
                "Adjust file permissions for the configured state file.",
            )
        return True, f"Discord state file is writable: {state_file}", None

    writable_root = _nearest_existing_parent(state_file.parent)
    if writable_root is None:
        return (
            False,
            f"Discord state file path is not writable: {state_file}",
            "Ensure the state file parent directory exists and is writable.",
        )

    if os.access(writable_root, os.W_OK):
        return (
            True,
            f"Discord state file can be created at: {state_file}",
            None,
        )

    return (
        False,
        f"Discord state file parent is not writable: {writable_root}",
        "Adjust directory permissions or choose a writable discord_bot.state_file path.",
    )


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while True:
        if current.exists():
            return current
        if current == current.parent:
            return None
        current = current.parent


def _parse_string_ids(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        items = [value]
    parsed = {str(item).strip() for item in items if str(item).strip()}
    return frozenset(parsed)
