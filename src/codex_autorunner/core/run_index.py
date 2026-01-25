from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .sqlite_utils import open_sqlite
from .state import now_iso


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _coerce_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _parse_payload(raw: Optional[str]) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_legacy_run_index(path: Path) -> dict[int, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    entries: dict[int, dict[str, Any]] = {}
    for key, entry in payload.items():
        if isinstance(key, int) and not isinstance(key, bool):
            run_id = key
        elif isinstance(key, str) and key.isdigit():
            run_id = int(key)
        else:
            continue
        if isinstance(entry, dict):
            entries[run_id] = entry
    return entries


class RunIndexStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def _ensure_schema(self, conn) -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    start_offset INTEGER,
                    end_offset INTEGER,
                    log_path TEXT,
                    run_log_path TEXT,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_started_at
                    ON runs(started_at)
                """
            )

    def _maybe_migrate_legacy(self, conn) -> None:
        legacy_path = self._path.with_name("run_index.json")
        if not legacy_path.exists():
            return
        existing = conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
        if existing is not None:
            return
        entries = _load_legacy_run_index(legacy_path)
        if not entries:
            return
        with conn:
            for run_id, entry in entries.items():
                self._save_entry(conn, run_id, entry)

    def _load_entry(self, conn, run_id: int) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_payload(row["payload_json"])

    def _save_entry(self, conn, run_id: int, entry: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(entry, ensure_ascii=True)
        started_at = _coerce_str(entry.get("started_at"))
        finished_at = _coerce_str(entry.get("finished_at"))
        exit_code = _coerce_int(entry.get("exit_code"))
        start_offset = _coerce_int(entry.get("start_offset"))
        end_offset = _coerce_int(entry.get("end_offset"))
        log_path = _coerce_str(entry.get("log_path"))
        run_log_path = _coerce_str(entry.get("run_log_path"))
        updated_at = now_iso()
        conn.execute(
            """
            INSERT INTO runs (
                run_id,
                started_at,
                finished_at,
                exit_code,
                start_offset,
                end_offset,
                log_path,
                run_log_path,
                payload_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                exit_code=excluded.exit_code,
                start_offset=excluded.start_offset,
                end_offset=excluded.end_offset,
                log_path=excluded.log_path,
                run_log_path=excluded.run_log_path,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                started_at,
                finished_at,
                exit_code,
                start_offset,
                end_offset,
                log_path,
                run_log_path,
                payload,
                updated_at,
            ),
        )
        return entry

    def load_all(self) -> dict[str, dict[str, Any]]:
        with open_sqlite(self._path) as conn:
            self._ensure_schema(conn)
            self._maybe_migrate_legacy(conn)
            entries: dict[str, dict[str, Any]] = {}
            for row in conn.execute("SELECT run_id, payload_json FROM runs"):
                entry = _parse_payload(row["payload_json"])
                entries[str(row["run_id"])] = entry
            return entries

    def get_entry(self, run_id: int) -> Optional[dict[str, Any]]:
        with open_sqlite(self._path) as conn:
            self._ensure_schema(conn)
            self._maybe_migrate_legacy(conn)
            return self._load_entry(conn, run_id)

    def merge_entry(self, run_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with open_sqlite(self._path) as conn:
            self._ensure_schema(conn)
            self._maybe_migrate_legacy(conn)
            entry = self._load_entry(conn, run_id) or {}
            if isinstance(updates.get("artifacts"), dict):
                existing_artifacts = entry.get("artifacts")
                merged_artifacts = (
                    dict(existing_artifacts)
                    if isinstance(existing_artifacts, dict)
                    else {}
                )
                merged_artifacts.update(updates["artifacts"])
                updates = dict(updates)
                updates["artifacts"] = merged_artifacts
            entry.update(updates)
            with conn:
                return self._save_entry(conn, run_id, entry)

    def update_marker(
        self,
        run_id: int,
        marker: str,
        offset: Optional[tuple[int, int]],
        exit_code: Optional[int],
        *,
        log_path: str,
        run_log_path: str,
        actor: Optional[dict[str, Any]] = None,
        mode: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with open_sqlite(self._path) as conn:
            self._ensure_schema(conn)
            self._maybe_migrate_legacy(conn)
            entry = self._load_entry(conn, run_id) or {}
            if marker == "start":
                entry["start_offset"] = offset[0] if offset else None
                entry["started_at"] = now_iso()
                entry["log_path"] = log_path
                entry["run_log_path"] = run_log_path
                if actor is not None:
                    entry["actor"] = actor
                if mode is not None:
                    entry["mode"] = mode
            elif marker == "end":
                entry["end_offset"] = offset[1] if offset else None
                entry["finished_at"] = now_iso()
                entry["exit_code"] = exit_code
                entry.setdefault("log_path", log_path)
                entry.setdefault("run_log_path", run_log_path)
            with conn:
                return self._save_entry(conn, run_id, entry)
