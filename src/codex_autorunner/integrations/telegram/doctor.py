"""Telegram integration doctor checks."""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Union

from ...core.config import HubConfig, RepoConfig
from ...core.optional_dependencies import missing_optional_dependencies
from ...core.runtime import DoctorCheck

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
    elif isinstance(config.raw, dict):
        telegram_cfg = config.raw.get("telegram_bot")
        if not telegram_cfg:
            telegram_cfg = config.raw.get("notifications", {}).get("telegram", {})

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
    except Exception as exc:
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
