from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ...core.state import persist_session_registry
from .pty_session import ActiveSession


def parse_last_seen_at(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def session_last_touch(session: ActiveSession, record) -> float:
    last_seen = parse_last_seen_at(getattr(record, "last_seen_at", None))
    if last_seen is None:
        return session.pty.last_active
    return max(last_seen, session.pty.last_active)


def parse_tui_idle_seconds(config) -> Optional[float]:
    notifications_cfg = (
        config.notifications if isinstance(config.notifications, dict) else {}
    )
    idle_seconds = notifications_cfg.get("tui_idle_seconds")
    if idle_seconds is None:
        return None
    try:
        idle_seconds = float(idle_seconds)
    except (TypeError, ValueError):
        return None
    if idle_seconds <= 0:
        return None
    return idle_seconds


def prune_terminal_registry(
    state_path: Path,
    terminal_sessions: dict[str, ActiveSession],
    session_registry: dict,
    repo_to_session: dict[str, str],
    max_idle_seconds: Optional[int],
) -> bool:
    now = time.time()
    removed_any = False
    for session_id, session in list(terminal_sessions.items()):
        if not session.pty.isalive():
            session.close()
            terminal_sessions.pop(session_id, None)
            session_registry.pop(session_id, None)
            removed_any = True
            continue
        if max_idle_seconds is not None and max_idle_seconds > 0:
            last_touch = session_last_touch(session, session_registry.get(session_id))
            if now - last_touch > max_idle_seconds:
                session.close()
                terminal_sessions.pop(session_id, None)
                session_registry.pop(session_id, None)
                removed_any = True
    for session_id in list(session_registry.keys()):
        if session_id not in terminal_sessions:
            session_registry.pop(session_id, None)
            removed_any = True
    for repo_path, session_id in list(repo_to_session.items()):
        if session_id not in session_registry:
            repo_to_session.pop(repo_path, None)
            removed_any = True
    if removed_any:
        persist_session_registry(state_path, session_registry, repo_to_session)
    return removed_any
