from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .pma_thread_store import PmaThreadStore, default_pma_threads_db_path
from .sqlite_utils import open_sqlite

logger = logging.getLogger("codex_autorunner.core.chat_bindings")

DISCORD_STATE_FILE_DEFAULT = ".codex-autorunner/discord_state.sqlite3"
TELEGRAM_STATE_FILE_DEFAULT = ".codex-autorunner/telegram_state.sqlite3"


def _normalize_repo_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    repo_id = value.strip()
    return repo_id or None


def _coerce_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            parsed = int(raw)
            return parsed if parsed > 0 else 0
    return 0


def _resolve_state_path(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    section: str,
    default_state_file: str,
) -> Path:
    section_cfg = raw_config.get(section)
    if not isinstance(section_cfg, Mapping):
        section_cfg = {}
    state_file = section_cfg.get("state_file")
    if not isinstance(state_file, str) or not state_file.strip():
        state_file = default_state_file
    state_path = Path(state_file)
    if not state_path.is_absolute():
        state_path = (hub_root / state_path).resolve()
    return state_path


def _read_sqlite_repo_counts(
    *,
    db_path: Path,
    table_name: str,
    repo_column: str,
) -> dict[str, int]:
    if not db_path.exists():
        return {}
    query = (
        f"SELECT {repo_column} AS repo_id, COUNT(*) AS count "
        f"FROM {table_name} "
        f"WHERE {repo_column} IS NOT NULL AND TRIM({repo_column}) != '' "
        f"GROUP BY {repo_column}"
    )
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(query).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc

    counts: dict[str, int] = {}
    for row in rows:
        repo_id = _normalize_repo_id(row["repo_id"])
        if repo_id is None:
            continue
        count = _coerce_count(row["count"])
        if count <= 0:
            continue
        counts[repo_id] = count
    return counts


def _sqlite_repo_has_binding(
    *,
    db_path: Path,
    table_name: str,
    repo_column: str,
    repo_id: str,
) -> bool:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None or not db_path.exists():
        return False
    query = f"SELECT 1 FROM {table_name} " f"WHERE {repo_column} = ? " "LIMIT 1"
    try:
        with open_sqlite(db_path) as conn:
            row = conn.execute(query, (normalized_repo_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc
    return row is not None


def _normalize_scope(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    scope = value.strip()
    return scope or None


def _normalize_chat_identity(
    *, chat_id: Any, thread_id: Any
) -> tuple[int, int | None] | None:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        return None
    if thread_id is None:
        return chat_id, None
    if isinstance(thread_id, bool) or not isinstance(thread_id, int):
        return None
    return chat_id, thread_id


def _read_telegram_current_scope_map(
    conn: Any,
) -> dict[tuple[int, int | None], str | None] | None:
    try:
        rows = conn.execute(
            "SELECT chat_id, thread_id, scope FROM telegram_topic_scopes"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise

    scope_map: dict[tuple[int, int | None], str | None] = {}
    for row in rows:
        identity = _normalize_chat_identity(
            chat_id=row["chat_id"], thread_id=row["thread_id"]
        )
        if identity is None:
            continue
        scope_map[identity] = _normalize_scope(row["scope"])
    return scope_map


def _is_current_telegram_topic_row(
    *,
    row: Any,
    scope_map: dict[tuple[int, int | None], str | None] | None,
) -> bool:
    if scope_map is None:
        return True
    identity = _normalize_chat_identity(
        chat_id=row["chat_id"], thread_id=row["thread_id"]
    )
    if identity is None:
        return True
    current_scope = scope_map.get(identity)
    row_scope = _normalize_scope(row["scope"])
    if current_scope is None:
        return row_scope is None
    return row_scope == current_scope


def _read_current_telegram_repo_counts(*, db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT topic_key, chat_id, thread_id, scope, repo_id
                  FROM telegram_topics
                 WHERE repo_id IS NOT NULL AND TRIM(repo_id) != ''
                """
            ).fetchall()
            scope_map = _read_telegram_current_scope_map(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc

    counts: Counter[str] = Counter()
    for row in rows:
        if not _is_current_telegram_topic_row(row=row, scope_map=scope_map):
            continue
        repo_id = _normalize_repo_id(row["repo_id"])
        if repo_id is None:
            continue
        counts[repo_id] += 1
    return dict(counts)


def _telegram_repo_has_current_binding(*, db_path: Path, repo_id: str) -> bool:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None or not db_path.exists():
        return False
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT topic_key, chat_id, thread_id, scope, repo_id
                  FROM telegram_topics
                 WHERE repo_id = ?
                """,
                (normalized_repo_id,),
            ).fetchall()
            if not rows:
                return False
            scope_map = _read_telegram_current_scope_map(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc

    for row in rows:
        if _is_current_telegram_topic_row(row=row, scope_map=scope_map):
            return True
    return False


def _active_pma_thread_counts(hub_root: Path) -> dict[str, int]:
    db_path = default_pma_threads_db_path(hub_root)
    if not db_path.exists():
        return {}
    store = PmaThreadStore(hub_root)
    raw_counts = store.count_threads_by_repo(status="active")
    counts: dict[str, int] = {}
    for raw_repo_id, raw_count in raw_counts.items():
        repo_id = _normalize_repo_id(raw_repo_id)
        if repo_id is None:
            continue
        count = _coerce_count(raw_count)
        if count <= 0:
            continue
        counts[repo_id] = count
    return counts


def _has_active_pma_thread(hub_root: Path, repo_id: str) -> bool:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None:
        return False
    db_path = default_pma_threads_db_path(hub_root)
    if not db_path.exists():
        return False
    store = PmaThreadStore(hub_root)
    return bool(
        store.list_threads(status="active", repo_id=normalized_repo_id, limit=1)
    )


def _resolve_discord_state_path(hub_root: Path, raw_config: Mapping[str, Any]) -> Path:
    return _resolve_state_path(
        hub_root=hub_root,
        raw_config=raw_config,
        section="discord_bot",
        default_state_file=DISCORD_STATE_FILE_DEFAULT,
    )


def _resolve_telegram_state_path(hub_root: Path, raw_config: Mapping[str, Any]) -> Path:
    return _resolve_state_path(
        hub_root=hub_root,
        raw_config=raw_config,
        section="telegram_bot",
        default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
    )


def active_chat_binding_counts(
    *, hub_root: Path, raw_config: Mapping[str, Any]
) -> dict[str, int]:
    """Return repo-id keyed counts of active chat bindings from persisted stores."""

    counts: Counter[str] = Counter()

    for repo_id, count in _active_pma_thread_counts(hub_root).items():
        counts[repo_id] += count

    discord_state_path = _resolve_discord_state_path(hub_root, raw_config)
    for repo_id, count in _read_sqlite_repo_counts(
        db_path=discord_state_path,
        table_name="channel_bindings",
        repo_column="repo_id",
    ).items():
        counts[repo_id] += count

    telegram_state_path = _resolve_telegram_state_path(hub_root, raw_config)
    for repo_id, count in _read_current_telegram_repo_counts(
        db_path=telegram_state_path
    ).items():
        counts[repo_id] += count

    return dict(counts)


def repo_has_active_chat_binding(
    *, hub_root: Path, raw_config: Mapping[str, Any], repo_id: str
) -> bool:
    """Return True when a repo has any persisted active chat binding."""

    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None:
        return False

    if _has_active_pma_thread(hub_root, normalized_repo_id):
        return True

    discord_state_path = _resolve_discord_state_path(hub_root, raw_config)
    if _sqlite_repo_has_binding(
        db_path=discord_state_path,
        table_name="channel_bindings",
        repo_column="repo_id",
        repo_id=normalized_repo_id,
    ):
        return True

    telegram_state_path = _resolve_telegram_state_path(hub_root, raw_config)
    if _telegram_repo_has_current_binding(
        db_path=telegram_state_path, repo_id=normalized_repo_id
    ):
        return True

    return False


__all__ = [
    "active_chat_binding_counts",
    "repo_has_active_chat_binding",
]
