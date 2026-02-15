"""Compatibility shim for app-server environment helpers."""

from importlib import import_module
from pathlib import Path
from typing import Any, Optional, Sequence, cast


def _env_module() -> Any:
    return import_module("codex_autorunner.integrations.app_server.env")


def _ids_module() -> Any:
    return import_module("codex_autorunner.integrations.app_server.ids")


def app_server_env(
    command: Sequence[str],
    cwd: Path,
    *,
    base_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    return cast(
        dict[str, str],
        _env_module().app_server_env(command, cwd, base_env=base_env),
    )


def seed_codex_home(
    codex_home: Path,
    *,
    logger: Any = None,
    event_prefix: str = "app_server",
) -> None:
    _env_module().seed_codex_home(
        codex_home,
        logger=logger,
        event_prefix=event_prefix,
    )


def build_app_server_env(
    command: Sequence[str],
    workspace_root: Path,
    state_dir: Path,
    *,
    logger: Any = None,
    event_prefix: str = "app_server",
    base_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    return cast(
        dict[str, str],
        _env_module().build_app_server_env(
            command,
            workspace_root,
            state_dir,
            logger=logger,
            event_prefix=event_prefix,
            base_env=base_env,
        ),
    )


def _extract_thread_id(payload: Any) -> Optional[str]:
    return cast(Optional[str], _ids_module().extract_thread_id(payload))


__all__ = [
    "app_server_env",
    "seed_codex_home",
    "build_app_server_env",
    "_extract_thread_id",
]
