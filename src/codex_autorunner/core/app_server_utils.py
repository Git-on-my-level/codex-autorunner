import os
from pathlib import Path
from typing import Any, Optional, Sequence

from .logging_utils import log_event
from .utils import resolve_executable, subprocess_env


def app_server_env(
    command: Sequence[str],
    cwd: Path,
    *,
    base_env: Optional[dict[str, str]] = None,
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
    logger: Any = None,
    event_prefix: str = "app_server",
) -> None:
    logger = logger or __import__("logging").getLogger(__name__)
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
            __import__("logging").INFO,
            f"{event_prefix}.codex_home.seed.skipped",
            reason="auth_exists",
            source=str(source_root),
            target=str(codex_home),
        )
        return
    if not source_root.exists():
        log_event(
            logger,
            __import__("logging").WARNING,
            f"{event_prefix}.codex_home.seed.skipped",
            reason="source_missing",
            source=str(source_root),
            target=str(codex_home),
        )
        return
    if not source_auth.exists():
        log_event(
            logger,
            __import__("logging").WARNING,
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
            __import__("logging").INFO,
            f"{event_prefix}.codex_home.seeded",
            source=str(source_root),
            target=str(codex_home),
        )
    except OSError as exc:
        log_event(
            logger,
            __import__("logging").WARNING,
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
    logger: Any = None,
    event_prefix: str = "app_server",
    base_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    env = app_server_env(command, workspace_root, base_env=base_env)
    codex_home = state_dir / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    seed_codex_home(codex_home, logger=logger, event_prefix=event_prefix)
    env["CODEX_HOME"] = str(codex_home)
    return env


def _extract_turn_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("turnId", "turn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    turn = payload.get("turn")
    if isinstance(turn, dict):
        for key in ("id", "turnId", "turn_id"):
            value = turn.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_thread_id_from_container(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_thread_id_for_turn(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        thread_id = _extract_thread_id_from_container(candidate)
        if thread_id:
            return thread_id
    return None


def _extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None
