"""Telegram integration doctor checks."""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from ...core.config import HubConfig, RepoConfig
from ...core.optional_dependencies import missing_optional_dependencies
from ...core.runtime import DoctorCheck
from ...voice.config import VoiceConfig
from ...voice.provider_catalog import (
    local_voice_provider_spec,
    missing_local_voice_runtime_commands,
)
from ..chat.collaboration_policy import (
    CollaborationPolicyError,
    build_telegram_collaboration_policy,
)

logger = logging.getLogger(__name__)

STUCK_TURN_THRESHOLD_MINUTES = 30
STATE_FILE_CHECK = ".codex-autorunner/telegram_state.sqlite3"


def telegram_doctor_checks(
    config: Union[HubConfig, RepoConfig, Dict[str, Any]],
    repo_root: Union[Path, None] = None,
) -> list[DoctorCheck]:
    """Run Telegram-specific doctor checks.

    Returns a list of DoctorCheck objects for Telegram integration.
    Works with HubConfig, RepoConfig, or raw dict.

    Args:
        config: HubConfig, RepoConfig, or raw dict
        repo_root: Optional repo root path for state file checks
    """
    checks: list[DoctorCheck] = []
    telegram_cfg = None

    if isinstance(config, dict):
        telegram_cfg = config.get("telegram_bot")
        if not telegram_cfg:
            telegram_cfg = config.get("notifications", {}).get("telegram", {})
        collaboration_cfg = config.get("collaboration_policy", {})
    elif isinstance(config.raw, dict):
        telegram_cfg = config.raw.get("telegram_bot")
        if not telegram_cfg:
            telegram_cfg = config.raw.get("notifications", {}).get("telegram", {})
        collaboration_cfg = config.raw.get("collaboration_policy", {})
    else:
        collaboration_cfg = {}

    enabled = isinstance(telegram_cfg, dict) and telegram_cfg.get("enabled") is True

    if enabled:
        missing_telegram = missing_optional_dependencies((("httpx", "httpx"),))
        if missing_telegram:
            deps_list = ", ".join(missing_telegram)
            checks.append(
                DoctorCheck(
                    name="Telegram dependencies",
                    passed=False,
                    message=f"Telegram is enabled but missing optional deps: {deps_list}",
                    check_id="telegram.dependencies",
                    fix="Install with `pip install codex-autorunner[telegram]`.",
                )
            )
            return checks
        else:
            checks.append(
                DoctorCheck(
                    name="Telegram dependencies",
                    passed=True,
                    message="Telegram dependencies are installed.",
                    check_id="telegram.dependencies",
                    severity="info",
                )
            )

        voice_raw = _resolve_voice_raw(config)
        voice_config = VoiceConfig.from_raw(voice_raw, env=os.environ)
        local_provider_spec = local_voice_provider_spec(voice_config.provider)
        if (
            _telegram_voice_ingestion_enabled(telegram_cfg)
            and voice_config.enabled
            and local_provider_spec is not None
        ):
            provider_name, deps, extra = local_provider_spec
            missing_local_voice = missing_optional_dependencies(deps)
            missing_runtime_commands = missing_local_voice_runtime_commands(
                provider_name
            )
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
                        name="Telegram voice dependencies",
                        passed=False,
                        message=(
                            "Telegram voice transcription is configured with "
                            f"{provider_name} but {missing_desc} is not installed."
                            f"{runtime_hint}"
                        ),
                        check_id="telegram.voice.dependencies",
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
                        name="Telegram voice dependencies",
                        passed=False,
                        message=(
                            "Telegram voice transcription is configured with "
                            f"{provider_name} but required runtime command(s) are "
                            f"missing from PATH: {missing_runtime_desc}."
                        ),
                        check_id="telegram.voice.dependencies",
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
                        name="Telegram voice dependencies",
                        passed=True,
                        message=(
                            f"Telegram voice transcription is using {provider_name} "
                            "and local dependencies are installed."
                        ),
                        check_id="telegram.voice.dependencies",
                        severity="info",
                    )
                )

        bot_token_env = telegram_cfg.get("bot_token_env", "CAR_TELEGRAM_BOT_TOKEN")
        chat_id_env = telegram_cfg.get("chat_id_env", "CAR_TELEGRAM_CHAT_ID")

        bot_token = os.environ.get(bot_token_env)
        chat_id = os.environ.get(chat_id_env)

        if not bot_token:
            checks.append(
                DoctorCheck(
                    name="Telegram bot token",
                    passed=False,
                    message=f"Telegram bot token not found in environment: {bot_token_env}",
                    check_id="telegram.bot_token",
                    fix=f"Set {bot_token_env} environment variable or disable Telegram.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Telegram bot token",
                    passed=True,
                    message=f"Bot token configured (env: {bot_token_env}).",
                    check_id="telegram.bot_token",
                    severity="info",
                )
            )

        if not chat_id:
            checks.append(
                DoctorCheck(
                    name="Telegram chat ID",
                    passed=False,
                    message=f"Telegram chat_id not found in environment: {chat_id_env}",
                    check_id="telegram.chat_id",
                    fix=f"Set {chat_id_env} environment variable for notifications.",
                    severity="warning",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Telegram chat ID",
                    passed=True,
                    message=f"Chat ID configured (env: {chat_id_env}).",
                    check_id="telegram.chat_id",
                    severity="info",
                )
            )

        allowed_chats = telegram_cfg.get("allowed_chat_ids", [])
        allowed_users = telegram_cfg.get("allowed_user_ids", [])
        if not allowed_chats and not allowed_users:
            checks.append(
                DoctorCheck(
                    name="Telegram access control",
                    passed=False,
                    message="No allowed_chat_ids or allowed_user_ids configured",
                    check_id="telegram.access_control",
                    fix="Configure allowed_chat_ids or allowed_user_ids in telegram_bot config.",
                    severity="warning",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Telegram access control",
                    passed=True,
                    message=f"Access control configured: {len(allowed_chats)} chats, {len(allowed_users)} users.",
                    check_id="telegram.access_control",
                    severity="info",
                )
            )
        try:
            policy = build_telegram_collaboration_policy(
                allowed_chat_ids=allowed_chats,
                allowed_user_ids=allowed_users,
                require_topics=bool(telegram_cfg.get("require_topics", False)),
                trigger_mode=str(telegram_cfg.get("trigger_mode", "all")),
                collaboration_raw=(
                    collaboration_cfg.get("telegram")
                    if isinstance(collaboration_cfg, dict)
                    else None
                ),
                shared_raw=(
                    collaboration_cfg if isinstance(collaboration_cfg, dict) else None
                ),
            )
            checks.append(
                DoctorCheck(
                    name="Telegram collaboration policy",
                    passed=True,
                    message=(
                        "Shared policy compiled: "
                        f"{len(policy.allowed_actor_ids)} actors, "
                        f"{len(policy.allowed_container_ids)} chats, "
                        f"{len(policy.destinations)} destinations, "
                        f"default_mode={policy.default_mode}, "
                        f"default_plain_text_trigger={policy.default_plain_text_trigger}, "
                        f"require_topics={policy.require_subdestination}"
                    ),
                    check_id="telegram.collaboration_policy",
                    severity="info",
                )
            )
            if policy.destinations:
                checks.append(
                    DoctorCheck(
                        name="Telegram collaboration migration",
                        passed=True,
                        message=(
                            "Explicit Telegram collaboration destinations are configured. "
                            "Use /ids to capture exact chat/topic IDs and /status to verify "
                            "the effective mode for each root chat or topic."
                        ),
                        check_id="telegram.collaboration_migration",
                        severity="info",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="Telegram collaboration migration",
                        passed=True,
                        message=(
                            "Telegram allowlists admit commands, but plain-text turns "
                            "now stay quiet until a chat or topic is activated with "
                            "/bind or /pma on. For shared supergroups, migrate to "
                            "collaboration_policy.telegram.destinations and gate the "
                            "root chat with require_topics or mode:silent."
                        ),
                        check_id="telegram.collaboration_migration",
                        severity="info",
                    )
                )
            if _policy_uses_mentions(policy):
                checks.append(
                    DoctorCheck(
                        name="Telegram privacy mode guidance",
                        passed=True,
                        message=(
                            "Mentions-based Telegram collaboration is configured. "
                            "Disable BotFather privacy mode if you want plain-text "
                            "group messages and @mentions to reach CAR."
                        ),
                        check_id="telegram.privacy_mode_guidance",
                        severity="info",
                    )
                )
            if (
                _has_topic_destinations(policy)
                and not policy.require_subdestination
                and not _has_root_destinations(policy)
                and policy.default_mode == "active"
            ):
                checks.append(
                    DoctorCheck(
                        name="Telegram root chat collaboration gap",
                        passed=False,
                        message=(
                            "Topic destinations are configured, but the root chat "
                            "still inherits default_mode=active. Root-chat messages "
                            "can still reach CAR unless you gate or silence the root."
                        ),
                        check_id="telegram.collaboration_policy.root_chat",
                        severity="warning",
                        fix=(
                            "Set telegram_bot.require_topics: true or add an explicit "
                            "root destination such as `{ chat_id: <group>, mode: "
                            "silent }`. Use /ids in Telegram to copy the exact IDs."
                        ),
                    )
                )
        except CollaborationPolicyError as exc:
            checks.append(
                DoctorCheck(
                    name="Telegram collaboration policy",
                    passed=False,
                    message=f"Telegram collaboration policy is invalid: {exc}",
                    check_id="telegram.collaboration_policy",
                    severity="error",
                    fix=(
                        "Fix collaboration_policy.telegram values so IDs, destination modes, "
                        "and plain-text triggers are valid."
                    ),
                )
            )

        state_file_path = None
        if repo_root:
            state_file_path = repo_root / STATE_FILE_CHECK
            if not state_file_path.exists():
                checks.append(
                    DoctorCheck(
                        name="Telegram state file",
                        passed=False,
                        message=f"Telegram state file not found: {state_file_path}",
                        check_id="telegram.state_file",
                        severity="warning",
                        fix="Run a Telegram command to initialize the state file.",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="Telegram state file",
                        passed=True,
                        message=f"State file exists: {state_file_path}",
                        check_id="telegram.state_file",
                        severity="info",
                    )
                )

                _check_stuck_turns(checks, state_file_path)

        mode = telegram_cfg.get("mode", "polling")
        if mode not in ("polling", "webhook"):
            checks.append(
                DoctorCheck(
                    name="Telegram mode",
                    passed=False,
                    message=f"Invalid Telegram mode: {mode}",
                    check_id="telegram.mode",
                    fix="Set mode to 'polling' or 'webhook' in telegram_bot config.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Telegram mode",
                    passed=True,
                    message=f"Telegram mode: {mode}",
                    check_id="telegram.mode",
                    severity="info",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                name="Telegram enabled",
                passed=True,
                message="Telegram integration is disabled.",
                check_id="telegram.enabled",
                severity="info",
                fix="Set telegram_bot.enabled=true in config to enable.",
            )
        )

    return checks


def _resolve_voice_raw(
    config: Union[HubConfig, RepoConfig, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if isinstance(config, dict):
        repo_defaults = config.get("repo_defaults")
        if isinstance(repo_defaults, dict):
            voice = repo_defaults.get("voice")
            if isinstance(voice, dict):
                return voice
        voice = config.get("voice")
        if isinstance(voice, dict):
            return voice
        return None

    repo_defaults = getattr(config, "repo_defaults", None)
    if isinstance(repo_defaults, dict):
        voice = repo_defaults.get("voice")
        if isinstance(voice, dict):
            return voice

    if isinstance(config.raw, dict):
        repo_defaults_raw = config.raw.get("repo_defaults")
        if isinstance(repo_defaults_raw, dict):
            voice = repo_defaults_raw.get("voice")
            if isinstance(voice, dict):
                return voice
        voice = config.raw.get("voice")
        if isinstance(voice, dict):
            return voice
    return None


def _telegram_voice_ingestion_enabled(telegram_cfg: dict[str, Any]) -> bool:
    media_raw = telegram_cfg.get("media")
    media_cfg = media_raw if isinstance(media_raw, dict) else {}
    media_enabled = bool(media_cfg.get("enabled", True))
    media_voice = bool(media_cfg.get("voice", True))
    return media_enabled and media_voice


def _has_topic_destinations(policy: Any) -> bool:
    return any(
        getattr(destination, "subdestination_id", None) is not None
        for destination in getattr(policy, "destinations", ())
    )


def _has_root_destinations(policy: Any) -> bool:
    return any(
        getattr(destination, "subdestination_id", None) is None
        for destination in getattr(policy, "destinations", ())
    )


def _policy_uses_mentions(policy: Any) -> bool:
    if getattr(policy, "default_plain_text_trigger", None) == "mentions":
        return True
    return any(
        getattr(destination, "plain_text_trigger", None) == "mentions"
        for destination in getattr(policy, "destinations", ())
    )


def _check_stuck_turns(checks: list[DoctorCheck], state_file_path: Path) -> None:
    """Check for stuck turns in Telegram state."""
    try:
        from ...core.sqlite_utils import connect_sqlite

        conn = connect_sqlite(state_file_path)
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(turns)")
        columns = {row[1] for row in cursor.fetchall()}

        if "status" not in columns:
            return

        threshold = datetime.now(timezone.utc) - timedelta(
            minutes=STUCK_TURN_THRESHOLD_MINUTES
        )
        cursor.execute(
            """
            SELECT topic_key, status, updated_at
            FROM turns
            WHERE status = 'running' AND updated_at < ?
            ORDER BY updated_at ASC
            LIMIT 5
            """,
            (threshold.isoformat(),),
        )

        stuck_turns = cursor.fetchall()
        conn.close()

        if stuck_turns:
            topics = ", ".join([turn[0] for turn in stuck_turns])
            checks.append(
                DoctorCheck(
                    name="Telegram stuck turns",
                    passed=False,
                    message=f"Found {len(stuck_turns)} stuck turns (inactive > {STUCK_TURN_THRESHOLD_MINUTES}m): {topics}",
                    check_id="telegram.stuck_turns",
                    fix="Review logs and consider restarting the bot or clearing stuck turns.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Telegram stuck turns",
                    passed=True,
                    message="No stuck turns detected.",
                    check_id="telegram.stuck_turns",
                    severity="info",
                )
            )
    except (ImportError, sqlite3.Error, OSError) as exc:
        logger.debug("Failed to check for stuck turns: %s", exc)
        checks.append(
            DoctorCheck(
                name="Telegram stuck turns",
                passed=True,
                message=f"Could not check for stuck turns: {exc}",
                check_id="telegram.stuck_turns",
                severity="warning",
            )
        )
