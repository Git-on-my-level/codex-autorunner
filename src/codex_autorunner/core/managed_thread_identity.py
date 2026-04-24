from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .orchestration.sqlite import (
    open_orchestration_sqlite,
    prepare_orchestration_sqlite,
)
from .text_utils import lock_path_for
from .utils import atomic_write

APP_SERVER_THREADS_FILENAME = ".codex-autorunner/app_server_threads.json"
APP_SERVER_THREADS_VERSION = 2
APP_SERVER_THREADS_CORRUPT_SUFFIX = ".corrupt"
APP_SERVER_THREADS_NOTICE_SUFFIX = ".corrupt.json"
_APP_SERVER_THREADS_IMPORT_FLAG = "managed_thread_identity_import_v1"
_COMPATIBILITY_CACHE_MODE = "compatibility_cache"
_COMPATIBILITY_CACHE_REMOVAL_CONDITION = (
    "Remove once no runtime or archive readers depend on app_server_threads.json."
)

FILE_CHAT_KEY = "file_chat"
FILE_CHAT_OPENCODE_KEY = "file_chat.opencode"
FILE_CHAT_HERMES_KEY = "file_chat.hermes"
FILE_CHAT_PREFIX = "file_chat."
FILE_CHAT_OPENCODE_PREFIX = "file_chat.opencode."
FILE_CHAT_HERMES_PREFIX = "file_chat.hermes."
PMA_KEY = "pma"
PMA_OPENCODE_KEY = "pma.opencode"
PMA_HERMES_KEY = "pma.hermes"
PMA_PREFIX = "pma."
PMA_OPENCODE_PREFIX = "pma.opencode."
PMA_HERMES_PREFIX = "pma.hermes."
PMA_AUTOMATION_SEGMENT = "automation"

LOGGER = logging.getLogger("codex_autorunner.managed_thread_identity")

FEATURE_KEYS = {
    FILE_CHAT_KEY,
    FILE_CHAT_OPENCODE_KEY,
    FILE_CHAT_HERMES_KEY,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    PMA_HERMES_KEY,
    "autorunner",
    "autorunner.opencode",
    "autorunner.hermes",
}


def _normalize_pma_agent_family(agent: Optional[str]) -> Optional[str]:
    if not isinstance(agent, str):
        return None
    normalized = agent.strip().lower()
    if not normalized or normalized in {"all", "codex"}:
        return None
    return normalized


def _normalize_profile_segment(profile: Optional[str]) -> Optional[str]:
    if not isinstance(profile, str):
        return None
    normalized = profile.strip().lower()
    return normalized or None


def _append_profile_suffix(base_key: str, profile: Optional[str]) -> str:
    normalized_profile = _normalize_profile_segment(profile)
    if normalized_profile is None:
        return base_key
    return f"{base_key}.profile.{normalized_profile}"


def pma_base_key(agent: str, profile: Optional[str] = None) -> str:
    normalized = _normalize_pma_agent_family(agent)
    base_key = PMA_KEY if normalized is None else f"{PMA_KEY}.{normalized}"
    return _append_profile_suffix(base_key, profile)


def pma_automation_key(agent: str, profile: Optional[str] = None) -> str:
    return f"{pma_base_key(agent, profile)}.{PMA_AUTOMATION_SEGMENT}"


def pma_legacy_alias_keys(agent: str, profile: Optional[str]) -> tuple[str, ...]:
    if not isinstance(agent, str) or not isinstance(profile, str):
        return ()
    agent_norm = agent.strip().lower()
    prof_norm = profile.strip().lower()
    if not agent_norm or not prof_norm:
        return ()
    new_key = pma_base_key(agent_norm, prof_norm)
    raw_aliases: set[str] = {
        f"{agent_norm}-{prof_norm}",
        f"{agent_norm}_{prof_norm}",
    }
    unders = prof_norm.replace("-", "_")
    if unders != prof_norm:
        raw_aliases.add(f"{agent_norm}_{unders}")
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in sorted(raw_aliases):
        candidate = pma_base_key(raw)
        if candidate != new_key and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def pma_legacy_alias_key(agent: str, profile: Optional[str]) -> Optional[str]:
    keys = pma_legacy_alias_keys(agent, profile)
    return keys[0] if keys else None


def pma_legacy_migration_fallback_keys(
    canonical_key: str,
    agent: str,
    profile: Optional[str],
) -> tuple[str, ...]:
    logical_base = pma_base_key(agent, profile)
    legacy_bases = pma_legacy_alias_keys(agent, profile)
    if not legacy_bases:
        return ()
    if canonical_key == logical_base:
        return legacy_bases
    topic_prefix = logical_base + "."
    if canonical_key.startswith(topic_prefix):
        suffix = canonical_key[len(topic_prefix) :]
        return tuple(f"{base}.{suffix}" for base in legacy_bases)
    return ()


def pma_prefix_for_agent(agent: Optional[str], profile: Optional[str] = None) -> str:
    normalized = _normalize_pma_agent_family(agent)
    base_key = PMA_KEY if normalized is None else f"{PMA_KEY}.{normalized}"
    return f"{_append_profile_suffix(base_key, profile)}."


def pma_prefixes_for_reset(
    agent: Optional[str], profile: Optional[str] = None
) -> list[str]:
    normalized = _normalize_pma_agent_family(agent)
    normalized_profile = _normalize_profile_segment(profile)
    if normalized_profile is not None:
        base_key = PMA_KEY if normalized is None else f"{PMA_KEY}.{normalized}"
        return [f"{_append_profile_suffix(base_key, normalized_profile)}."]
    if normalized is None:
        return [PMA_PREFIX]
    return [f"{PMA_KEY}.{normalized}."]


def pma_topic_scoped_key(
    agent: str,
    chat_id: int,
    thread_id: Optional[int],
    topic_key_fn=None,
    profile: Optional[str] = None,
) -> str:
    base = pma_base_key(agent, profile)
    if topic_key_fn is None:
        raise ValueError("topic_key_fn is required for PMA topic-scoped keys")
    return f"{base}.{topic_key_fn(chat_id, thread_id)}"


def file_chat_discord_key(agent: str, channel_id: str, workspace_path: str) -> str:
    normalized_agent = (agent or "").strip().lower()
    if normalized_agent in ("codex", ""):
        prefix = FILE_CHAT_PREFIX
    elif normalized_agent == "opencode":
        prefix = FILE_CHAT_OPENCODE_PREFIX
    elif normalized_agent == "hermes":
        prefix = FILE_CHAT_HERMES_PREFIX
    else:
        prefix = f"{FILE_CHAT_PREFIX}{normalized_agent}."
    digest = hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}discord.{channel_id.strip()}.{digest}"


def file_chat_target_key(
    agent: str,
    target_state_key: str,
    profile: Optional[str] = None,
) -> str:
    normalized_agent = (agent or "").strip().lower()
    normalized_target_state_key = str(target_state_key or "").strip().lower()
    if not normalized_target_state_key:
        raise ValueError("target_state_key is required")
    if normalized_agent in ("codex", ""):
        base_key = FILE_CHAT_KEY
    elif normalized_agent == "opencode":
        base_key = FILE_CHAT_OPENCODE_KEY
    elif normalized_agent == "hermes":
        base_key = FILE_CHAT_HERMES_KEY
    else:
        base_key = f"{FILE_CHAT_KEY}.{normalized_agent}"
    scoped_base_key = _append_profile_suffix(base_key, profile)
    return f"{scoped_base_key}.{normalized_target_state_key}"


def default_app_server_threads_path(repo_root: Path) -> Path:
    return repo_root / APP_SERVER_THREADS_FILENAME


def normalize_feature_key(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("feature key must be a string")
    key = raw.strip().lower()
    if not key:
        raise ValueError("feature key is required")
    key = key.replace("/", ".").replace(":", ".")
    if key in FEATURE_KEYS:
        return key
    for prefix in (
        FILE_CHAT_PREFIX,
        FILE_CHAT_OPENCODE_PREFIX,
        FILE_CHAT_HERMES_PREFIX,
        PMA_PREFIX,
        PMA_OPENCODE_PREFIX,
        PMA_HERMES_PREFIX,
    ):
        if key.startswith(prefix) and len(key) > len(prefix):
            return key
    raise ValueError(f"invalid feature key: {raw}")


class ManagedThreadIdentityStore:
    """Canonical orchestration-backed managed-thread identity registry.

    `app_server_threads.json` remains only as a transitional compatibility cache.
    Authoritative reads and writes live in orchestration.sqlite3.
    """

    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        resolved_root = Path(hub_root)
        if resolved_root.suffix == ".json":
            self._path = resolved_root
            self._hub_root = (
                resolved_root.parent.parent
                if resolved_root.parent.name == ".codex-autorunner"
                else resolved_root.parent
            )
        else:
            self._hub_root = resolved_root
            self._path = default_app_server_threads_path(self._hub_root)
        self._durable = durable

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return lock_path_for(self._path)

    def _notice_path(self) -> Path:
        return self._path.with_name(
            f"{self._path.name}{APP_SERVER_THREADS_NOTICE_SUFFIX}"
        )

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _prepare(self) -> None:
        prepare_orchestration_sqlite(self._hub_root, durable=self._durable)

    def _import_marker_present(self, conn: Any) -> bool:
        row = conn.execute(
            """
            SELECT 1 AS ok
              FROM orch_legacy_backfill_flags
             WHERE backfill_key = ?
             LIMIT 1
            """,
            (_APP_SERVER_THREADS_IMPORT_FLAG,),
        ).fetchone()
        return row is not None

    def _mark_imported(self, conn: Any) -> None:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orch_legacy_backfill_flags (
                    backfill_key,
                    completed_at
                ) VALUES (?, ?)
                """,
                (_APP_SERVER_THREADS_IMPORT_FLAG, self._now_iso()),
            )

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _load_legacy_file_unlocked(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._handle_corrupt_registry_unlocked(str(exc))
            return {}
        if not isinstance(data, dict):
            return {}
        threads_raw = data.get("threads")
        source = threads_raw if isinstance(threads_raw, dict) else data
        threads: dict[str, str] = {}
        for key, value in source.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                continue
            try:
                normalized_key = normalize_feature_key(key)
            except ValueError:
                continue
            threads[normalized_key] = value
        return threads

    def _handle_corrupt_registry_unlocked(self, detail: str) -> None:
        stamp = self._stamp()
        backup_path = self._path.with_name(
            f"{self._path.name}{APP_SERVER_THREADS_CORRUPT_SUFFIX}.{stamp}"
        )
        try:
            self._path.replace(backup_path)
            backup_value = str(backup_path)
        except OSError:
            backup_value = ""
        notice = {
            "status": "corrupt",
            "message": "Compatibility cache reset due to corrupted registry.",
            "detail": detail,
            "detected_at": stamp,
            "backup_path": backup_value,
            "authority": "orchestration.sqlite3",
        }
        try:
            atomic_write(self._notice_path(), json.dumps(notice, indent=2) + "\n")
        except OSError:
            LOGGER.warning(
                "Failed to write managed-thread identity corruption notice.",
                exc_info=True,
            )

    def _load_from_conn(self, conn: Any) -> dict[str, str]:
        rows = conn.execute(
            """
            SELECT feature_key, thread_id
              FROM orch_thread_identity_bindings
             ORDER BY feature_key ASC
            """
        ).fetchall()
        return {
            str(row["feature_key"]): str(row["thread_id"])
            for row in rows
            if row["feature_key"] and row["thread_id"]
        }

    def _write_cache_unlocked(self, threads: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": APP_SERVER_THREADS_VERSION,
            "mode": _COMPATIBILITY_CACHE_MODE,
            "authority": "orchestration.sqlite3",
            "removal_when": _COMPATIBILITY_CACHE_REMOVAL_CONDITION,
            "threads": threads,
        }
        atomic_write(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _sync_cache(self, threads: dict[str, str]) -> None:
        with file_lock(self._lock_path()):
            self._write_cache_unlocked(threads)

    def _ensure_imported(self, conn: Any) -> None:
        if self._import_marker_present(conn):
            return
        with file_lock(self._lock_path()):
            threads = self._load_legacy_file_unlocked()
            timestamp = self._now_iso()
            with conn:
                for feature_key, thread_id in threads.items():
                    conn.execute(
                        """
                        INSERT INTO orch_thread_identity_bindings (
                            feature_key,
                            thread_id,
                            created_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(feature_key) DO NOTHING
                        """,
                        (feature_key, thread_id, timestamp, timestamp),
                    )
            self._mark_imported(conn)
            self._write_cache_unlocked(self._load_from_conn(conn))

    def _with_conn(self) -> Any:
        self._prepare()
        return open_orchestration_sqlite(
            self._hub_root,
            durable=self._durable,
            migrate=False,
        )

    def corruption_notice(self) -> Optional[dict]:
        path = self._notice_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def clear_corruption_notice(self) -> None:
        self._notice_path().unlink(missing_ok=True)

    def load(self) -> dict[str, str]:
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            return self._load_from_conn(conn)

    def feature_map(self) -> dict[str, object]:
        threads = self.load()
        payload: dict[str, object] = {
            "file_chat": threads.get(FILE_CHAT_KEY),
            "file_chat_opencode": threads.get(FILE_CHAT_OPENCODE_KEY),
            "file_chat_hermes": threads.get(FILE_CHAT_HERMES_KEY),
            "autorunner": threads.get("autorunner"),
            "autorunner_opencode": threads.get("autorunner.opencode"),
            "autorunner_hermes": threads.get("autorunner.hermes"),
            "pma": threads.get(PMA_KEY),
            "pma_opencode": threads.get(PMA_OPENCODE_KEY),
            "pma_hermes": threads.get(PMA_HERMES_KEY),
            "authority": "orchestration.sqlite3",
            "cache_mode": _COMPATIBILITY_CACHE_MODE,
        }
        notice = self.corruption_notice()
        if notice:
            payload["corruption"] = notice
        return payload

    def get_thread_id(self, key: str) -> Optional[str]:
        normalized = normalize_feature_key(key)
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            row = conn.execute(
                """
                SELECT thread_id
                  FROM orch_thread_identity_bindings
                 WHERE feature_key = ?
                 LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        value = str(row["thread_id"] or "").strip()
        return value or None

    def get_thread_id_with_fallback(
        self, key: str, *fallback_keys: str, migrate: bool = True
    ) -> Optional[str]:
        normalized = normalize_feature_key(key)
        normalized_fallbacks = tuple(
            normalize_feature_key(item) for item in fallback_keys
        )
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            row = conn.execute(
                """
                SELECT thread_id
                  FROM orch_thread_identity_bindings
                 WHERE feature_key = ?
                 LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if row is not None:
                value = str(row["thread_id"] or "").strip()
                return value or None
            for fallback_key in normalized_fallbacks:
                fallback_row = conn.execute(
                    """
                    SELECT thread_id
                      FROM orch_thread_identity_bindings
                     WHERE feature_key = ?
                     LIMIT 1
                    """,
                    (fallback_key,),
                ).fetchone()
                if fallback_row is None:
                    continue
                value = str(fallback_row["thread_id"] or "").strip()
                if not value:
                    continue
                if migrate:
                    timestamp = self._now_iso()
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO orch_thread_identity_bindings (
                                feature_key,
                                thread_id,
                                created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(feature_key) DO UPDATE
                                SET thread_id = excluded.thread_id,
                                    updated_at = excluded.updated_at
                            """,
                            (normalized, value, timestamp, timestamp),
                        )
                        conn.execute(
                            """
                            DELETE FROM orch_thread_identity_bindings
                             WHERE feature_key = ?
                            """,
                            (fallback_key,),
                        )
                    self._sync_cache(self._load_from_conn(conn))
                return value
        return None

    def set_thread_id(self, key: str, thread_id: str) -> None:
        normalized = normalize_feature_key(key)
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread id is required")
        timestamp = self._now_iso()
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_thread_identity_bindings (
                        feature_key,
                        thread_id,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(feature_key) DO UPDATE
                        SET thread_id = excluded.thread_id,
                            updated_at = excluded.updated_at
                    """,
                    (normalized, thread_id, timestamp, timestamp),
                )
            self._sync_cache(self._load_from_conn(conn))

    def reset_thread(self, key: str) -> bool:
        normalized = normalize_feature_key(key)
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            with conn:
                cursor = conn.execute(
                    """
                    DELETE FROM orch_thread_identity_bindings
                     WHERE feature_key = ?
                    """,
                    (normalized,),
                )
            self._sync_cache(self._load_from_conn(conn))
        return bool(cursor.rowcount)

    def reset_threads_by_prefix(
        self, prefix: str, *, exclude_prefixes: tuple[str, ...] = ()
    ) -> list[str]:
        normalized_prefix = str(prefix or "").strip().lower()
        if not normalized_prefix:
            return []
        normalized_excludes = tuple(
            str(item or "").strip().lower() for item in exclude_prefixes if item
        )
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            rows = conn.execute(
                """
                SELECT feature_key
                  FROM orch_thread_identity_bindings
                 WHERE feature_key LIKE ?
                 ORDER BY feature_key ASC
                """,
                (f"{normalized_prefix}%",),
            ).fetchall()
            cleared = [
                str(row["feature_key"])
                for row in rows
                if row["feature_key"]
                and not any(
                    str(row["feature_key"]) == excluded.rstrip(".")
                    or str(row["feature_key"]).startswith(excluded)
                    for excluded in normalized_excludes
                )
            ]
            if cleared:
                with conn:
                    conn.executemany(
                        """
                        DELETE FROM orch_thread_identity_bindings
                         WHERE feature_key = ?
                        """,
                        ((feature_key,) for feature_key in cleared),
                    )
            self._sync_cache(self._load_from_conn(conn))
        return cleared

    def reset_all(self) -> None:
        with self._with_conn() as conn:
            self._ensure_imported(conn)
            with conn:
                conn.execute("DELETE FROM orch_thread_identity_bindings")
            self.clear_corruption_notice()
            self._sync_cache({})

    def compatibility_cache_parity(self) -> dict[str, Any]:
        canonical = self.load()
        with file_lock(self._lock_path()):
            legacy = self._load_legacy_file_unlocked()
        missing_from_cache = sorted(set(canonical) - set(legacy))
        extra_in_cache = sorted(set(legacy) - set(canonical))
        mismatched = sorted(
            key for key in set(canonical) & set(legacy) if canonical[key] != legacy[key]
        )
        return {
            "passed": not missing_from_cache and not extra_in_cache and not mismatched,
            "canonical_count": len(canonical),
            "cache_count": len(legacy),
            "missing_from_cache": missing_from_cache,
            "extra_in_cache": extra_in_cache,
            "mismatched_keys": mismatched,
        }


AppServerThreadRegistry = ManagedThreadIdentityStore


__all__ = [
    "APP_SERVER_THREADS_CORRUPT_SUFFIX",
    "APP_SERVER_THREADS_FILENAME",
    "APP_SERVER_THREADS_NOTICE_SUFFIX",
    "APP_SERVER_THREADS_VERSION",
    "AppServerThreadRegistry",
    "FEATURE_KEYS",
    "FILE_CHAT_HERMES_KEY",
    "FILE_CHAT_HERMES_PREFIX",
    "FILE_CHAT_KEY",
    "FILE_CHAT_OPENCODE_KEY",
    "FILE_CHAT_OPENCODE_PREFIX",
    "FILE_CHAT_PREFIX",
    "ManagedThreadIdentityStore",
    "PMA_HERMES_KEY",
    "PMA_HERMES_PREFIX",
    "PMA_KEY",
    "PMA_OPENCODE_KEY",
    "PMA_OPENCODE_PREFIX",
    "PMA_PREFIX",
    "default_app_server_threads_path",
    "file_chat_discord_key",
    "file_chat_target_key",
    "normalize_feature_key",
    "pma_automation_key",
    "pma_base_key",
    "pma_legacy_alias_key",
    "pma_legacy_alias_keys",
    "pma_legacy_migration_fallback_keys",
    "pma_prefix_for_agent",
    "pma_prefixes_for_reset",
    "pma_topic_scoped_key",
]
