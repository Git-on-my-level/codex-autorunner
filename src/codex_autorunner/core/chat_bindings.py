from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..manifest import load_manifest
from .pma_thread_store import PmaThreadStore, default_pma_threads_db_path
from .sqlite_utils import open_sqlite

logger = logging.getLogger("codex_autorunner.core.chat_bindings")

DISCORD_STATE_FILE_DEFAULT = ".codex-autorunner/discord_state.sqlite3"
TELEGRAM_STATE_FILE_DEFAULT = ".codex-autorunner/telegram_state.sqlite3"
MANIFEST_FILE_DEFAULT = ".codex-autorunner/manifest.yml"


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


def _resolve_manifest_path(hub_root: Path, raw_config: Mapping[str, Any]) -> Path:
    hub_cfg = raw_config.get("hub")
    if not isinstance(hub_cfg, Mapping):
        hub_cfg = {}
    manifest_file = hub_cfg.get("manifest")
    if not isinstance(manifest_file, str) or not manifest_file.strip():
        manifest_file = MANIFEST_FILE_DEFAULT
    manifest_path = Path(manifest_file)
    if not manifest_path.is_absolute():
        manifest_path = (hub_root / manifest_path).resolve()
    return manifest_path


def _repo_id_by_workspace_path(
    hub_root: Path, raw_config: Mapping[str, Any]
) -> dict[str, str]:
    manifest_path = _resolve_manifest_path(hub_root, raw_config)
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


def _normalize_workspace_path(value: Any) -> str | None:
    if isinstance(value, Path):
        try:
            return str(value.resolve())
        except Exception:
            return str(value)
    return _resolve_workspace_path(value)


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


def _parse_iso_timestamp(raw: Any) -> float:
    if not isinstance(raw, str):
        return float("-inf")
    value = raw.strip()
    if not value:
        return float("-inf")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return float("-inf")


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


def _latest_discord_binding_timestamp_for_workspace(
    *, db_path: Path, workspace_path: Path
) -> float:
    if not db_path.exists():
        return float("-inf")
    target_workspace = _normalize_workspace_path(workspace_path)
    if target_workspace is None:
        return float("-inf")
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT workspace_path, updated_at
                  FROM channel_bindings
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return float("-inf")
        raise RuntimeError(
            f"Failed reading Discord chat bindings from {db_path}: {exc}"
        ) from exc

    latest = float("-inf")
    for row in rows:
        if _normalize_workspace_path(row["workspace_path"]) != target_workspace:
            continue
        latest = max(latest, _parse_iso_timestamp(row["updated_at"]))
    return latest


def _latest_current_telegram_binding_timestamp_for_workspace(
    *, db_path: Path, workspace_path: Path
) -> float:
    if not db_path.exists():
        return float("-inf")
    target_workspace = _normalize_workspace_path(workspace_path)
    if target_workspace is None:
        return float("-inf")
    try:
        with open_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT chat_id, thread_id, scope, workspace_path, last_active_at, updated_at
                  FROM telegram_topics
                """
            ).fetchall()
            scope_map = _read_telegram_current_scope_map(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return float("-inf")
        raise RuntimeError(
            f"Failed reading Telegram chat bindings from {db_path}: {exc}"
        ) from exc

    latest = float("-inf")
    for row in rows:
        if not _is_current_telegram_topic_row(row=row, scope_map=scope_map):
            continue
        if _normalize_workspace_path(row["workspace_path"]) != target_workspace:
            continue
        latest = max(
            latest,
            _parse_iso_timestamp(row["last_active_at"]),
            _parse_iso_timestamp(row["updated_at"]),
        )
    return latest


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

    counts_by_source = active_chat_binding_counts_by_source(
        hub_root=hub_root,
        raw_config=raw_config,
    )
    counts: Counter[str] = Counter()
    for repo_id, source_counts in counts_by_source.items():
        counts[repo_id] += sum(source_counts.values())
    return dict(counts)


def active_chat_binding_counts_by_source(
    *, hub_root: Path, raw_config: Mapping[str, Any]
) -> dict[str, dict[str, int]]:
    """Return repo-id keyed active chat binding counts split by source."""

    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root, raw_config)
    source_counts: dict[str, dict[str, int]] = {}

    def _merge_counts(source: str, counts: Mapping[str, int]) -> None:
        for repo_id, count in counts.items():
            if count <= 0:
                continue
            repo_counts = source_counts.setdefault(repo_id, {})
            repo_counts[source] = int(repo_counts.get(source, 0)) + int(count)

    _merge_counts("pma", _active_pma_thread_counts(hub_root, repo_id_by_workspace))

    discord_state_path = _resolve_discord_state_path(hub_root, raw_config)
    _merge_counts(
        "discord",
        _read_discord_repo_counts(
            db_path=discord_state_path,
            repo_id_by_workspace=repo_id_by_workspace,
        ),
    )

    telegram_state_path = _resolve_telegram_state_path(hub_root, raw_config)
    _merge_counts(
        "telegram",
        _read_current_telegram_repo_counts(
            db_path=telegram_state_path,
            repo_id_by_workspace=repo_id_by_workspace,
        ),
    )

    return source_counts


def repo_has_active_chat_binding(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    repo_id: str,
    include_pma: bool = True,
) -> bool:
    """Return True when a repo has any persisted active chat binding."""

    normalized_repo_id = _normalize_repo_id(repo_id)
    if normalized_repo_id is None:
        return False

    source_counts = active_chat_binding_counts_by_source(
        hub_root=hub_root,
        raw_config=raw_config,
    ).get(normalized_repo_id, {})
    if include_pma:
        return any(int(count) > 0 for count in source_counts.values())
    return any(
        source != "pma" and int(count) > 0 for source, count in source_counts.items()
    )


def repo_has_active_non_pma_chat_binding(
    *, hub_root: Path, raw_config: Mapping[str, Any], repo_id: str
) -> bool:
    """Return True when a repo has active non-PMA chat bindings."""

    return repo_has_active_chat_binding(
        hub_root=hub_root,
        raw_config=raw_config,
        repo_id=repo_id,
        include_pma=False,
    )


def preferred_non_pma_chat_notification_source_for_workspace(
    *, hub_root: Path, raw_config: Mapping[str, Any], workspace_root: Path
) -> str | None:
    """Return the preferred bound chat surface for workspace notifications."""

    discord_timestamp = _latest_discord_binding_timestamp_for_workspace(
        db_path=_resolve_discord_state_path(hub_root, raw_config),
        workspace_path=workspace_root,
    )
    telegram_timestamp = _latest_current_telegram_binding_timestamp_for_workspace(
        db_path=_resolve_telegram_state_path(hub_root, raw_config),
        workspace_path=workspace_root,
    )

    candidates = [
        ("discord", discord_timestamp),
        ("telegram", telegram_timestamp),
    ]
    available = [item for item in candidates if item[1] != float("-inf")]
    if not available:
        return None
    # Prefer the freshest persisted binding signal; ties fall back to Telegram.
    available.sort(key=lambda item: (item[1], 1 if item[0] == "telegram" else 0))
    return available[-1][0]


__all__ = [
    "active_chat_binding_counts",
    "active_chat_binding_counts_by_source",
    "preferred_non_pma_chat_notification_source_for_workspace",
    "repo_has_active_chat_binding",
    "repo_has_active_non_pma_chat_binding",
]
