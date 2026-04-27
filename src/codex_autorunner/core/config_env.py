import os
from os import PathLike
from pathlib import Path
from typing import IO, Dict, Mapping, Optional, Union

from .app_server_command import (
    GLOBAL_APP_SERVER_COMMAND_ENV,
)

DOTENV_AVAILABLE = True
try:
    from dotenv import dotenv_values, load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    DOTENV_AVAILABLE = False

    def load_dotenv(
        dotenv_path: Optional[Union[str, PathLike[str]]] = None,
        stream: Optional[IO[str]] = None,
        verbose: bool = False,
        override: bool = False,
        interpolate: bool = True,
        encoding: Optional[str] = None,
    ) -> bool:
        return False

    def dotenv_values(
        dotenv_path: Optional[Union[str, PathLike[str]]] = None,
        stream: Optional[IO[str]] = None,
        verbose: bool = False,
        interpolate: bool = True,
        encoding: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        return {}


def load_dotenv_for_root(root: Path) -> None:
    """
    Best-effort load of environment variables for the provided repo root.

    We intentionally load from deterministic locations rather than relying on
    process CWD (which differs for installed entrypoints, launchd, etc.).
    """
    import logging

    logger = logging.getLogger("codex_autorunner.core.config_env")
    try:
        root = root.resolve()
        candidates = [
            root / ".env",
            root / ".codex-autorunner" / ".env",
        ]

        for candidate in candidates:
            if candidate.exists():
                load_dotenv(dotenv_path=candidate, override=True)
    except OSError as exc:
        logger.debug("Failed to load .env file: %s", exc)


def _parse_dotenv_fallback(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if value and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            env[key] = value
    except OSError:
        return {}
    return env


def resolve_env_for_root(
    root: Path, base_env: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """
    Return a merged env mapping for a repo root without mutating process env.

    Precedence mirrors load_dotenv_for_root: root/.env then root/.codex-autorunner/.env.
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    candidates = [
        root / ".env",
        root / ".codex-autorunner" / ".env",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        if DOTENV_AVAILABLE:
            values = dotenv_values(candidate)
            if isinstance(values, dict):
                for key, value in values.items():
                    if key and value is not None:
                        env[str(key)] = str(value)
                continue
        env.update(_parse_dotenv_fallback(candidate))
    return env


VOICE_ENV_OVERRIDES = (
    "CODEX_AUTORUNNER_VOICE_ENABLED",
    "CODEX_AUTORUNNER_VOICE_PROVIDER",
    "CODEX_AUTORUNNER_VOICE_LATENCY",
    "CODEX_AUTORUNNER_VOICE_CHUNK_MS",
    "CODEX_AUTORUNNER_VOICE_SAMPLE_RATE",
    "CODEX_AUTORUNNER_VOICE_WARN_REMOTE",
    "CODEX_AUTORUNNER_VOICE_MAX_MS",
    "CODEX_AUTORUNNER_VOICE_SILENCE_MS",
    "CODEX_AUTORUNNER_VOICE_MIN_HOLD_MS",
)

COMMON_ENV_OVERRIDES = (GLOBAL_APP_SERVER_COMMAND_ENV,)

TELEGRAM_ENV_OVERRIDES = ("CAR_OPENCODE_COMMAND",)

DISCORD_ENV_OVERRIDES = (
    "CAR_DISCORD_BOT_TOKEN",
    "CAR_DISCORD_APP_ID",
)


def collect_env_overrides(
    *,
    env: Optional[Mapping[str, str]] = None,
    include_telegram: bool = False,
    include_discord: bool = False,
) -> list[str]:
    source = env if env is not None else os.environ
    overrides: list[str] = []

    def _has_value(key: str) -> bool:
        value = source.get(key)
        if value is None:
            return False
        return str(value).strip() != ""

    if source.get("CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS") == "1":
        overrides.append("CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS")
    if _has_value("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS"):
        overrides.append("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS")
    if _has_value("CAR_GLOBAL_STATE_ROOT"):
        overrides.append("CAR_GLOBAL_STATE_ROOT")
    for key in VOICE_ENV_OVERRIDES:
        if _has_value(key):
            overrides.append(key)
    for key in COMMON_ENV_OVERRIDES:
        if _has_value(key):
            overrides.append(key)
    if include_telegram:
        for key in TELEGRAM_ENV_OVERRIDES:
            if _has_value(key):
                overrides.append(key)
    if include_discord:
        for key in DISCORD_ENV_OVERRIDES:
            if _has_value(key):
                overrides.append(key)
    return overrides
