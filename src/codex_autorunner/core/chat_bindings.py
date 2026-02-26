from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..manifest import load_manifest
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


def _repo_id_by_workspace_path(hub_root: Path) -> dict[str, str]:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    if not manifest_path.exists():
        return {}
    try:
        manifest = load_manifest(manifest_path, hub_root)
    except Exception as exc:
        logger.warning("Failed loading manifest for chat binding lookup: %s", exc)
        return {}
    mapping: dict[str, str] = {}
    for repo in manifest.repos:
        try:
            workspace_path = (hub_root / repo.path).resolve()
        except Exception:
            continue
        mapping[str(workspace_path)] = repo.id
    return mapping


def _workspace_path_candidates(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    raw = value.strip()
    if not raw:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for token in (raw, unquote(raw)):
        normalized = token.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
        if "@" in normalized:
            suffix = normalized.rsplit("@", 1)[1].strip()
            if suffix and suffix not in seen:
                seen.add(suffix)
                candidates.append(suffix)
    return candidates


def _resolve_workspace_path(value: Any) -> str | None:
    for candidate in _workspace_path_candidates(value):
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            continue
        try:
            return str(path.resolve())
        except Exception:
            return str(path)
    return None


def _resolve_bound_repo_id(
    *,
    repo_id: Any,
    repo_id_by_workspace: Mapping[str, str],
    workspace_values: tuple[Any, ...] = (),
) -> str | None:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is not None:
        return normalized_repo_id
    for workspace_value in workspace_values:
        workspace_path = _resolve_workspace_path(workspace_value)
        if workspace_path is None:
            continue
        mapped_repo_id = _normalize_repo_id(repo_id_by_workspace.get(workspace_path))
        if mapped_repo_id is not None:
            return mapped_repo_id
    return None


def _table_columns(conn: Any, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.OperationalError:
        return set()
    cols: set[str] = set()
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else None
        if isinstance(name, str) and name:
            cols.add(name)
    return cols


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


def _read_current_telegram_repo_counts(
    *, db_path: Path, repo_id_by_workspace: Mapping[str, str]
) -> dict[str, int]:
    if not db_path.exists():
        return {}
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT topic_key, chat_id, thread_id, scope, workspace_path, repo_id
                  FROM telegram_topics
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
        repo_id = _resolve_bound_repo_id(
            repo_id=row["repo_id"],
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(row["workspace_path"], row["scope"]),
        )
        if repo_id is None:
            continue
        counts[repo_id] += 1
    return dict(counts)


def _telegram_repo_has_current_binding(
    *, db_path: Path, repo_id: str, repo_id_by_workspace: Mapping[str, str]
) -> bool:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None or not db_path.exists():
        return False
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT topic_key, chat_id, thread_id, scope, workspace_path, repo_id
                  FROM telegram_topics
                """
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
        if not _is_current_telegram_topic_row(row=row, scope_map=scope_map):
            continue
        resolved_repo_id = _resolve_bound_repo_id(
            repo_id=row["repo_id"],
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(row["workspace_path"], row["scope"]),
        )
        if resolved_repo_id == normalized_repo_id:
            return True
    return False


def _read_discord_repo_counts(
    *, db_path: Path, repo_id_by_workspace: Mapping[str, str]
) -> dict[str, int]:
    if not db_path.exists():
        return {}
    try:
        with open_sqlite(db_path) as conn:
            columns = _table_columns(conn, "channel_bindings")
            if not columns:
                return {}
            has_workspace_path = "workspace_path" in columns
            rows = conn.execute(
                "SELECT repo_id"
                + (", workspace_path" if has_workspace_path else "")
                + " FROM channel_bindings"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc

    counts: Counter[str] = Counter()
    for row in rows:
        repo_id = _resolve_bound_repo_id(
            repo_id=row["repo_id"],
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=((row["workspace_path"],) if has_workspace_path else ()),
        )
        if repo_id is None:
            continue
        counts[repo_id] += 1
    return dict(counts)


def _discord_repo_has_binding(
    *, db_path: Path, repo_id: str, repo_id_by_workspace: Mapping[str, str]
) -> bool:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None or not db_path.exists():
        return False
    try:
        with open_sqlite(db_path) as conn:
            columns = _table_columns(conn, "channel_bindings")
            if not columns:
                return False
            has_workspace_path = "workspace_path" in columns
            rows = conn.execute(
                "SELECT repo_id"
                + (", workspace_path" if has_workspace_path else "")
                + " FROM channel_bindings"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc

    for row in rows:
        resolved_repo_id = _resolve_bound_repo_id(
            repo_id=row["repo_id"],
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=((row["workspace_path"],) if has_workspace_path else ()),
        )
        if resolved_repo_id == normalized_repo_id:
            return True
    return False


def _active_pma_thread_counts(
    hub_root: Path, repo_id_by_workspace: Mapping[str, str]
) -> dict[str, int]:
    db_path = default_pma_threads_db_path(hub_root)
    if not db_path.exists():
        return {}
    store = PmaThreadStore(hub_root)
    raw_counts = store.count_threads_by_repo(status="active")
    counts: Counter[str] = Counter()
    for raw_repo_id, raw_count in raw_counts.items():
        repo_id = _normalize_repo_id(raw_repo_id)
        if repo_id is None:
            continue
        count = _coerce_count(raw_count)
        if count <= 0:
            continue
        counts[repo_id] += count
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT workspace_root
                  FROM pma_managed_threads
                 WHERE status = 'active'
                   AND (repo_id IS NULL OR TRIM(repo_id) = '')
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return dict(counts)
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc

    for row in rows:
        repo_id = _resolve_bound_repo_id(
            repo_id=None,
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(row["workspace_root"],),
        )
        if repo_id is None:
            continue
        counts[repo_id] += 1
    return dict(counts)


def _has_active_pma_thread(
    hub_root: Path, repo_id: str, repo_id_by_workspace: Mapping[str, str]
) -> bool:
    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None:
        return False
    db_path = default_pma_threads_db_path(hub_root)
    if not db_path.exists():
        return False
    store = PmaThreadStore(hub_root)
    if store.list_threads(status="active", repo_id=normalized_repo_id, limit=1):
        return True
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT workspace_root
                  FROM pma_managed_threads
                 WHERE status = 'active'
                   AND (repo_id IS NULL OR TRIM(repo_id) = '')
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise RuntimeError(
            f"Failed reading chat bindings from {db_path}: {exc}"
        ) from exc
    for row in rows:
        resolved_repo_id = _resolve_bound_repo_id(
            repo_id=None,
            repo_id_by_workspace=repo_id_by_workspace,
            workspace_values=(row["workspace_root"],),
        )
        if resolved_repo_id == normalized_repo_id:
            return True
    return False


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

    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)
    counts: Counter[str] = Counter()

    for repo_id, count in _active_pma_thread_counts(
        hub_root, repo_id_by_workspace
    ).items():
        counts[repo_id] += count

    discord_state_path = _resolve_discord_state_path(hub_root, raw_config)
    for repo_id, count in _read_discord_repo_counts(
        db_path=discord_state_path,
        repo_id_by_workspace=repo_id_by_workspace,
    ).items():
        counts[repo_id] += count

    telegram_state_path = _resolve_telegram_state_path(hub_root, raw_config)
    for repo_id, count in _read_current_telegram_repo_counts(
        db_path=telegram_state_path,
        repo_id_by_workspace=repo_id_by_workspace,
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

    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)

    if _has_active_pma_thread(hub_root, normalized_repo_id, repo_id_by_workspace):
        return True

    discord_state_path = _resolve_discord_state_path(hub_root, raw_config)
    if _discord_repo_has_binding(
        db_path=discord_state_path,
        repo_id=normalized_repo_id,
        repo_id_by_workspace=repo_id_by_workspace,
    ):
        return True

    telegram_state_path = _resolve_telegram_state_path(hub_root, raw_config)
    if _telegram_repo_has_current_binding(
        db_path=telegram_state_path,
        repo_id=normalized_repo_id,
        repo_id_by_workspace=repo_id_by_workspace,
    ):
        return True

    return False


__all__ = [
    "active_chat_binding_counts",
    "repo_has_active_chat_binding",
]
