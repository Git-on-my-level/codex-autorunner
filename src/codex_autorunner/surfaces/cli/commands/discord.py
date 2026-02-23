from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import typer

from ....core.config import ConfigError, load_hub_config
from ....core.logging_utils import setup_rotating_logger
from ....integrations.discord.command_registry import sync_commands
from ....integrations.discord.commands import build_application_commands
from ....integrations.discord.config import DiscordBotConfig, DiscordBotConfigError
from ....integrations.discord.rest import DiscordRestClient
from ....integrations.discord.service import create_discord_bot_service


def _require_discord_feature(require_optional_feature: Callable) -> None:
    require_optional_feature(
        feature="discord",
        deps=[("websockets", "websockets")],
        extra="discord",
    )


def _resolve_pma_enabled(hub_config: Any) -> bool:
    pma_raw = getattr(hub_config, "raw", {}).get("pma", {})
    if isinstance(pma_raw, dict):
        return bool(pma_raw.get("enabled", True))
    return True


async def _sync_discord_application_commands(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
    rest_client_factory: Callable[..., Any] = DiscordRestClient,
    sync_func: Callable[..., Awaitable[None]] = sync_commands,
) -> None:
    if not config.bot_token:
        raise DiscordBotConfigError(f"missing bot token env '{config.bot_token_env}'")
    if not config.application_id:
        raise DiscordBotConfigError(f"missing application id env '{config.app_id_env}'")

    commands = build_application_commands()
    async with rest_client_factory(bot_token=config.bot_token) as rest:
        await sync_func(
            rest,
            application_id=config.application_id,
            commands=commands,
            scope=config.command_registration.scope,
            guild_ids=config.command_registration.guild_ids,
            logger=logger,
        )


def register_discord_commands(
    app: typer.Typer,
    *,
    raise_exit: Callable,
    require_optional_feature: Callable,
) -> None:
    @app.command("start")
    def discord_start(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ) -> None:
        _require_discord_feature(require_optional_feature)
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        try:
            discord_raw = (
                config.raw.get("discord_bot") if isinstance(config.raw, dict) else {}
            )
            pma_enabled = _resolve_pma_enabled(config)
            discord_cfg = DiscordBotConfig.from_raw(
                root=config.root,
                raw=discord_raw if isinstance(discord_raw, dict) else {},
                pma_enabled=pma_enabled,
            )
            if not discord_cfg.enabled:
                raise_exit("discord_bot is disabled; set discord_bot.enabled: true")
            logger = setup_rotating_logger("codex-autorunner-discord", config.log)
            service = create_discord_bot_service(
                discord_cfg, logger=logger, manifest_path=config.manifest_path
            )
            asyncio.run(service.run_forever())
        except DiscordBotConfigError as exc:
            raise_exit(str(exc), cause=exc)
        except KeyboardInterrupt:
            typer.echo("Discord bot stopped.")

    @app.command("health")
    def discord_health(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ) -> None:
        _require_discord_feature(require_optional_feature)
        raise NotImplementedError("Discord health check is not implemented yet.")

    @app.command("register-commands")
    def discord_register_commands(
        path: Optional[Path] = typer.Option(
            None, "--path", help="Repo or hub root path"
        ),
    ) -> None:
        _require_discord_feature(require_optional_feature)
        try:
            config = load_hub_config(path or Path.cwd())
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)

        try:
            discord_raw = (
                config.raw.get("discord_bot") if isinstance(config.raw, dict) else {}
            )
            pma_enabled = _resolve_pma_enabled(config)
            discord_cfg = DiscordBotConfig.from_raw(
                root=config.root,
                raw=discord_raw if isinstance(discord_raw, dict) else {},
                pma_enabled=pma_enabled,
            )
            if not discord_cfg.enabled:
                raise_exit("discord_bot is disabled; set discord_bot.enabled: true")
            asyncio.run(
                _sync_discord_application_commands(
                    discord_cfg,
                    logger=logging.getLogger("codex_autorunner.discord.commands"),
                )
            )
        except (DiscordBotConfigError, ValueError) as exc:
            raise_exit(str(exc), cause=exc)

        typer.echo("Discord application commands synchronized.")
