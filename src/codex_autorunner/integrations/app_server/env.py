from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ...core.logging_utils import log_event
from ...core.utils import resolve_executable, subprocess_env


def app_server_env(
    command: Sequence[str],
    cwd: Path,
    *,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    extra_paths: list[str] = []
    if command:
        binary = command[0]
        resolved = resolve_executable(binary, env=base_env)
        candidate: Optional[Path] = Path(resolved) if resolved else None
        if candidate is None:
            candidate = Path(binary).expanduser()
            if not candidate.is_absolute():
                candidate = (cwd / candidate).resolve()
        if candidate.exists():
            extra_paths.append(str(candidate.parent))
    return subprocess_env(extra_paths=extra_paths, base_env=base_env)


def seed_codex_home(
    codex_home: Path,
    *,
    logger: Optional[logging.Logger] = None,
    event_prefix: str = "app_server",
) -> None:
    logger = logger or logging.getLogger(__name__)
    auth_path = codex_home / "auth.json"
    source_root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    if source_root.resolve() == codex_home.resolve():
        return
    source_auth = source_root / "auth.json"
    if auth_path.exists():
        if auth_path.is_symlink() and auth_path.resolve() == source_auth.resolve():
            return
        log_event(
            logger,
            logging.INFO,
            f"{event_prefix}.codex_home.seed.skipped",
            reason="auth_exists",
            source=str(source_root),
            target=str(codex_home),
        )
        return
    if not source_root.exists():
        log_event(
            logger,
            logging.WARNING,
            f"{event_prefix}.codex_home.seed.skipped",
            reason="source_missing",
            source=str(source_root),
            target=str(codex_home),
        )
        return
    if not source_auth.exists():
        log_event(
            logger,
            logging.WARNING,
            f"{event_prefix}.codex_home.seed.skipped",
            reason="auth_missing",
            source=str(source_root),
            target=str(codex_home),
        )
        return
    try:
        auth_path.symlink_to(source_auth)
        log_event(
            logger,
            logging.INFO,
            f"{event_prefix}.codex_home.seeded",
            source=str(source_root),
            target=str(codex_home),
        )
    except OSError as exc:
        log_event(
            logger,
            logging.WARNING,
            f"{event_prefix}.codex_home.seed.failed",
            exc=exc,
            source=str(source_root),
            target=str(codex_home),
        )


def build_app_server_env(
    command: Sequence[str],
    workspace_root: Path,
    state_dir: Path,
    *,
    logger: Optional[logging.Logger] = None,
    event_prefix: str = "app_server",
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    env = app_server_env(command, workspace_root, base_env=base_env)
    codex_home = state_dir / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    seed_codex_home(codex_home, logger=logger, event_prefix=event_prefix)
    env["CODEX_HOME"] = str(codex_home)
    return env
