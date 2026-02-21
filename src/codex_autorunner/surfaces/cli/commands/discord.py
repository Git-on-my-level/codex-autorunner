from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import typer


def _require_discord_feature(require_optional_feature: Callable) -> None:
    require_optional_feature(
        feature="discord",
        deps=[("websockets", "websockets")],
        extra="discord",
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
        raise NotImplementedError("Discord bot start is not implemented yet.")

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
        raise NotImplementedError(
            "Discord slash command registration is not implemented yet."
        )
