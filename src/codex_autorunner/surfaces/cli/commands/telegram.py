import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional

import typer

from ....core.config import (
    ConfigError,
    collect_env_overrides,
    load_hub_config,
)
from ....core.logging_utils import log_event, setup_rotating_logger
from ....integrations.telegram.adapter import TelegramAPIError, TelegramBotClient
from ....integrations.telegram.service import (
    TelegramBotConfig,
    TelegramBotConfigError,
    TelegramBotLockError,
    TelegramBotService,
)
from ....integrations.telegram.state import TelegramStateStore
from ....voice import VoiceConfig


def register_telegram_commands(
    telegram_app: typer.Typer,
    *,
    raise_exit: Callable,
    require_optional_feature: Callable,
) -> None:
    @telegram_app.command("start")
    def telegram_start(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ):
        require_optional_feature(
            feature="telegram",
            deps=[("httpx", "httpx")],
            extra="telegram",
        )
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        telegram_cfg = TelegramBotConfig.from_raw(
            config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
            root=config.root,
            agent_binaries=getattr(config, "agents", None)
            and {name: agent.binary for name, agent in config.agents.items()},
        )
        if not telegram_cfg.enabled:
            raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")
        try:
            telegram_cfg.validate()
        except TelegramBotConfigError as exc:
            raise_exit(str(exc), cause=exc)
        logger = setup_rotating_logger("codex-autorunner-telegram", config.log)
        env_overrides = collect_env_overrides(env=os.environ, include_telegram=True)
        if env_overrides:
            logger.info("Environment overrides active: %s", ", ".join(env_overrides))
        log_event(
            logger,
            logging.INFO,
            "telegram.bot.starting",
            root=str(config.root),
            mode="hub",
        )
        voice_raw = config.repo_defaults.get("voice") if config.repo_defaults else None
        voice_config = VoiceConfig.from_raw(voice_raw, env=os.environ)
        update_repo_url = config.update_repo_url
        update_repo_ref = config.update_repo_ref

        async def _run() -> None:
            service = TelegramBotService(
                telegram_cfg,
                logger=logger,
                hub_root=config.root,
                manifest_path=config.manifest_path,
                voice_config=voice_config,
                housekeeping_config=config.housekeeping,
                update_repo_url=update_repo_url,
                update_repo_ref=update_repo_ref,
                update_skip_checks=config.update_skip_checks,
                app_server_auto_restart=config.app_server.auto_restart,
            )
            await service.run_polling()

        try:
            asyncio.run(_run())
        except TelegramBotLockError as exc:
            raise_exit(str(exc), cause=exc)

    @telegram_app.command("health")
    def telegram_health(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
        timeout: float = typer.Option(5.0, "--timeout", help="Timeout (seconds)"),
    ):
        require_optional_feature(
            feature="telegram",
            deps=[("httpx", "httpx")],
            extra="telegram",
        )
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        telegram_cfg = TelegramBotConfig.from_raw(
            config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
            root=config.root,
            agent_binaries=getattr(config, "agents", None)
            and {name: agent.binary for name, agent in config.agents.items()},
        )
        if not telegram_cfg.enabled:
            raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")
        bot_token = telegram_cfg.bot_token
        if not bot_token:
            raise_exit(f"missing bot token env '{telegram_cfg.bot_token_env}'")
        assert bot_token is not None
        timeout_seconds = max(float(timeout), 0.1)

        async def _run() -> None:
            async with TelegramBotClient(bot_token) as client:
                await asyncio.wait_for(client.get_me(), timeout=timeout_seconds)

        try:
            asyncio.run(_run())
        except TelegramAPIError as exc:
            raise_exit(f"Telegram health check failed: {exc}", cause=exc)

    @telegram_app.command("state-check")
    def telegram_state_check(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ):
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        telegram_cfg = TelegramBotConfig.from_raw(
            config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
            root=config.root,
            agent_binaries=getattr(config, "agents", None)
            and {name: agent.binary for name, agent in config.agents.items()},
        )
        if not telegram_cfg.enabled:
            raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")

        try:
            store = TelegramStateStore(
                telegram_cfg.state_file,
                default_approval_mode=telegram_cfg.defaults.approval_mode,
            )
            store._connection_sync()
        except Exception as exc:
            raise_exit(f"Telegram state check failed: {exc}", cause=exc)
