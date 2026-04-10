from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .sqlite_utils import open_sqlite
from .state_roots import resolve_hub_projection_db_path
from .time_utils import now_iso

logger = logging.getLogger("codex_autorunner.core.hub_projection_store")

_CACHE_TABLE = "projection_cache"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def path_stat_fingerprint(path: Path) -> tuple[bool, Optional[int], Optional[int]]:
    try:
        stat = path.stat()
    except OSError:
        return (False, None, None)
    return (True, int(stat.st_mtime_ns), int(stat.st_size))


class HubProjectionStore:
    def __init__(self, hub_root: Path, *, durable: bool = False) -> None:
        self._db_path = resolve_hub_projection_db_path(hub_root)
        self._durable = durable

    @property
    def path(self) -> Path:
        return self._db_path

    @classmethod
    def from_hub_root(cls, hub_root: Path) -> "HubProjectionStore":
        return cls(hub_root)

    def _ensure_schema(self, conn: Any) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(namespace, cache_key)
            )
            """
        )

    def get_cache(
        self,
        cache_key: str,
        fingerprint: Any,
        *,
        namespace: str = "default",
    ) -> Any | None:
        fingerprint_text = _stable_json(fingerprint)
        try:
            with open_sqlite(self._db_path, durable=self._durable) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"""
                    SELECT fingerprint, payload
                      FROM {_CACHE_TABLE}
                     WHERE namespace = ? AND cache_key = ?
                    """,
                    (namespace, cache_key),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "Failed reading hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )
            return None
        if row is None or str(row["fingerprint"]) != fingerprint_text:
            return None
        try:
            return json.loads(str(row["payload"]))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed decoding hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )
            return None

    def set_cache(
        self,
        cache_key: str,
        fingerprint: Any,
        payload: Any,
        *,
        namespace: str = "default",
    ) -> None:
        fingerprint_text = _stable_json(fingerprint)
        payload_text = _stable_json(payload)
        try:
            with open_sqlite(self._db_path, durable=self._durable) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"""
                    INSERT INTO {_CACHE_TABLE} (
                        namespace,
                        cache_key,
                        fingerprint,
                        payload,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, cache_key) DO UPDATE SET
                        fingerprint = excluded.fingerprint,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        namespace,
                        cache_key,
                        fingerprint_text,
                        payload_text,
                        now_iso(),
                    ),
                )
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed writing hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )

    def invalidate_cache(self, cache_key: str, *, namespace: str = "default") -> None:
        try:
            with open_sqlite(self._db_path, durable=self._durable) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"DELETE FROM {_CACHE_TABLE} WHERE namespace = ? AND cache_key = ?",
                    (namespace, cache_key),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "Failed deleting hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )

    def get(self, *, namespace: str, key: str, fingerprint: Any) -> Any | None:
        return self.get_cache(key, fingerprint, namespace=namespace)

    def put(
        self,
        *,
        namespace: str,
        key: str,
        fingerprint: Any,
        payload: Any,
    ) -> None:
        self.set_cache(key, fingerprint, payload, namespace=namespace)

    def delete(self, *, namespace: str, key: Optional[str] = None) -> None:
        if key is None:
            try:
                with open_sqlite(self._db_path, durable=self._durable) as conn:
                    self._ensure_schema(conn)
                    conn.execute(
                        f"DELETE FROM {_CACHE_TABLE} WHERE namespace = ?",
                        (namespace,),
                    )
            except (sqlite3.Error, OSError) as exc:
                logger.warning(
                    "Failed deleting hub projection cache namespace=%s key=%s: %s",
                    namespace,
                    key,
                    exc,
                )
            return
        self.invalidate_cache(key, namespace=namespace)
