"""Telegram integration doctor checks."""

from typing import Any, Dict, Union

from ...core.config import HubConfig, RepoConfig
from ...core.engine import DoctorCheck
from ...core.optional_dependencies import missing_optional_dependencies


def telegram_doctor_checks(
    config: Union[HubConfig, RepoConfig, Dict[str, Any]],
) -> list[DoctorCheck]:
    """Run Telegram-specific doctor checks.

    Returns a list of DoctorCheck objects for Telegram integration.
    Works with HubConfig, RepoConfig, or raw dict.
    """
    checks: list[DoctorCheck] = []
    telegram_cfg = None

    if isinstance(config, dict):
        telegram_cfg = config.get("telegram_bot")
    elif isinstance(config.raw, dict):
        telegram_cfg = config.raw.get("telegram_bot")

    if isinstance(telegram_cfg, dict) and telegram_cfg.get("enabled") is True:
        missing_telegram = missing_optional_dependencies((("httpx", "httpx"),))
        if missing_telegram:
            deps_list = ", ".join(missing_telegram)
            checks.append(
                DoctorCheck(
                    check_id="telegram.dependencies",
                    status="error",
                    message=f"Telegram is enabled but missing optional deps: {deps_list}",
                    fix="Install with `pip install codex-autorunner[telegram]`.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    check_id="telegram.dependencies",
                    status="ok",
                    message="Telegram dependencies are installed.",
                )
            )

    return checks
