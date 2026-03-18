from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import unquote

from .....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
)
from .....core.flows import FlowEventType, FlowStore
from .....core.git_utils import git_is_clean
from .....core.logging_utils import safe_log
from .....core.orchestration.sqlite import resolve_orchestration_sqlite_path
from .....core.pma_context import (
    get_latest_ticket_flow_run_state_with_record,
)
from .....core.pma_thread_store import default_pma_threads_db_path
from .....integrations.app_server.threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
    file_chat_discord_key,
    pma_base_key,
    pma_topic_scoped_key,
)
from .....integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from .....integrations.telegram.state import topic_key

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext


class HubChannelService:
    def __init__(self, context: HubAppContext) -> None:
        self._context = context

    def _state_db_path(self, section: str, default_path: str) -> Path:
        raw = (
            self._context.config.raw
            if isinstance(self._context.config.raw, dict)
            else {}
        )
        section_cfg = raw.get(section)
        state_file = default_path
        if isinstance(section_cfg, dict):
            candidate = section_cfg.get("state_file")
            if isinstance(candidate, str) and candidate.strip():
                state_file = candidate.strip()
        path = Path(state_file)
        if not path.is_absolute():
            path = (self._context.config.root / path).resolve()
        return path

    def _telegram_require_topics_enabled(self) -> bool:
        raw = (
            self._context.config.raw
            if isinstance(self._context.config.raw, dict)
            else {}
        )
        telegram_cfg = raw.get("telegram_bot")
        if not isinstance(telegram_cfg, dict):
            return False
        return bool(telegram_cfg.get("require_topics", False))

    def _workspace_path_candidates(self, value: Any) -> list[str]:
        if not isinstance(value, str):
            return []
        raw = value.strip()
        if not raw:
            return []
        seen: set[str] = set()
        candidates: list[str] = []
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

    def _canonical_workspace_path(self, value: Any) -> Optional[str]:
        for candidate in self._workspace_path_candidates(value):
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                continue
            try:
                return str(path.resolve())
            except Exception:
                return str(path)
        return None

    def _repo_id_by_workspace_path(self, snapshots: Iterable[Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for snapshot in snapshots:
            repo_id = getattr(snapshot, "id", None)
            path = getattr(snapshot, "path", None)
            if not isinstance(repo_id, str) or not isinstance(path, Path):
                continue
            mapping[str(path)] = repo_id
            try:
                mapping[str(path.resolve())] = repo_id
            except Exception:
                pass
        return mapping

    def _resolve_repo_id(
        self,
        raw_repo_id: Any,
        workspace_path: Any,
        repo_id_by_workspace: dict[str, str],
    ) -> Optional[str]:
        if isinstance(raw_repo_id, str) and raw_repo_id.strip():
            return raw_repo_id.strip()
        for candidate in self._workspace_path_candidates(workspace_path):
            resolved = self._canonical_workspace_path(candidate)
            if resolved and resolved in repo_id_by_workspace:
                return repo_id_by_workspace[resolved]
            if candidate in repo_id_by_workspace:
                return repo_id_by_workspace[candidate]
        return None

    def _open_sqlite_read_only(self, path: Path) -> sqlite3.Connection:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        try:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        except sqlite3.Error:
            return set()
        names: set[str] = set()
        for row in rows:
            name = row["name"] if isinstance(row, sqlite3.Row) else None
            if isinstance(name, str) and name:
                names.add(name)
        return names

    def _normalize_agent(self, value: Any) -> str:
        if isinstance(value, str) and value.strip().lower() == "opencode":
            return "opencode"
        return "codex"

    def _normalize_scope(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _coerce_int(self, value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _coerce_usage_int(self, value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_topic_identity(
        self, chat_raw: Any, thread_raw: Any, topic_raw: Any
    ) -> tuple[Optional[int], Optional[int], Optional[str]]:
        chat_id: Optional[int]
        thread_id: Optional[int]

        if isinstance(chat_raw, int) and not isinstance(chat_raw, bool):
            chat_id = chat_raw
        else:
            chat_id = None
        if thread_raw is None:
            thread_id = None
        elif isinstance(thread_raw, int) and not isinstance(thread_raw, bool):
            thread_id = thread_raw
        else:
            thread_id = None
        if chat_id is not None and (thread_raw is None or thread_id is not None):
            return chat_id, thread_id, None

        if not isinstance(topic_raw, str):
            return None, None, None
        parts = topic_raw.split(":", 2)
        if len(parts) < 2:
            return None, None, None
        try:
            parsed_chat_id = int(parts[0])
        except ValueError:
            return None, None, None
        thread_token = parts[1]
        parsed_thread_id: Optional[int]
        if thread_token == "root":
            parsed_thread_id = None
        else:
            try:
                parsed_thread_id = int(thread_token)
            except ValueError:
                parsed_thread_id = None
        parsed_scope = self._normalize_scope(parts[2]) if len(parts) == 3 else None
        return parsed_chat_id, parsed_thread_id, parsed_scope

    def _read_discord_bindings(
        self, db_path: Path, repo_id_by_workspace: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        if not db_path.exists():
            return {}
        bindings: dict[str, dict[str, Any]] = {}
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_sqlite_read_only(db_path)
            columns = self._table_columns(conn, "channel_bindings")
            if not columns:
                return {}
            select_cols = ["channel_id"]
            for col in (
                "guild_id",
                "workspace_path",
                "repo_id",
                "resource_kind",
                "resource_id",
                "pma_enabled",
                "agent",
                "updated_at",
            ):
                if col in columns:
                    select_cols.append(col)
            query = f"SELECT {', '.join(select_cols)} FROM channel_bindings"
            if "updated_at" in columns:
                query += " ORDER BY updated_at DESC"
            for row in conn.execute(query).fetchall():
                channel_id = row["channel_id"]
                if not isinstance(channel_id, str) or not channel_id.strip():
                    continue
                workspace_path_raw = (
                    row["workspace_path"] if "workspace_path" in columns else None
                )
                workspace_path = self._canonical_workspace_path(workspace_path_raw)
                repo_id = self._resolve_repo_id(
                    row["repo_id"] if "repo_id" in columns else None,
                    workspace_path_raw,
                    repo_id_by_workspace,
                )
                binding = {
                    "platform": "discord",
                    "chat_id": channel_id.strip(),
                    "surface_key": channel_id.strip(),
                    "workspace_path": workspace_path,
                    "repo_id": repo_id,
                    "resource_kind": (
                        str(row["resource_kind"]).strip()
                        if "resource_kind" in columns
                        and isinstance(row["resource_kind"], str)
                        and str(row["resource_kind"]).strip()
                        else None
                    ),
                    "resource_id": (
                        str(row["resource_id"]).strip()
                        if "resource_id" in columns
                        and isinstance(row["resource_id"], str)
                        and str(row["resource_id"]).strip()
                        else None
                    ),
                    "pma_enabled": (
                        bool(row["pma_enabled"]) if "pma_enabled" in columns else False
                    ),
                    "agent": self._normalize_agent(
                        row["agent"] if "agent" in columns else None
                    ),
                    "active_thread_id": None,
                }
                primary_key = f"discord:{binding['chat_id']}"
                bindings.setdefault(primary_key, binding)
                guild_id = row["guild_id"] if "guild_id" in columns else None
                if isinstance(guild_id, str) and guild_id.strip():
                    bindings.setdefault(
                        f"discord:{binding['chat_id']}:{guild_id.strip()}",
                        binding,
                    )
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                f"Hub channel enrichment failed reading discord bindings from {db_path}",
                exc=exc,
            )
            return {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return bindings

    def _read_telegram_scope_map(
        self,
        conn: sqlite3.Connection,
    ) -> Optional[dict[tuple[int, Optional[int]], Optional[str]]]:
        try:
            rows = conn.execute(
                "SELECT chat_id, thread_id, scope FROM telegram_topic_scopes"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise

        scope_map: dict[tuple[int, Optional[int]], Optional[str]] = {}
        for row in rows:
            chat_id = row["chat_id"]
            thread_id = row["thread_id"]
            if not isinstance(chat_id, int) or isinstance(chat_id, bool):
                continue
            if thread_id is not None and (
                not isinstance(thread_id, int) or isinstance(thread_id, bool)
            ):
                continue
            scope_map[(chat_id, thread_id)] = self._normalize_scope(row["scope"])
        return scope_map

    def _read_telegram_bindings(
        self, db_path: Path, repo_id_by_workspace: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        if not db_path.exists():
            return {}
        bindings: dict[str, dict[str, Any]] = {}
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_sqlite_read_only(db_path)
            columns = self._table_columns(conn, "telegram_topics")
            if not columns:
                return {}
            select_cols = ["topic_key"]
            for col in (
                "chat_id",
                "thread_id",
                "scope",
                "workspace_path",
                "repo_id",
                "active_thread_id",
                "payload_json",
                "updated_at",
            ):
                if col in columns:
                    select_cols.append(col)
            scope_map = self._read_telegram_scope_map(conn)
            query = f"SELECT {', '.join(select_cols)} FROM telegram_topics"
            if "updated_at" in columns:
                query += " ORDER BY updated_at DESC"
            for row in conn.execute(query).fetchall():
                parsed_chat_id, parsed_thread_id, parsed_scope = (
                    self._parse_topic_identity(
                        row["chat_id"] if "chat_id" in columns else None,
                        row["thread_id"] if "thread_id" in columns else None,
                        row["topic_key"],
                    )
                )
                if parsed_chat_id is None:
                    continue
                row_scope = (
                    self._normalize_scope(row["scope"])
                    if "scope" in columns
                    else parsed_scope
                )
                if scope_map is not None:
                    current_scope = scope_map.get((parsed_chat_id, parsed_thread_id))
                    if current_scope is None and row_scope is not None:
                        continue
                    if current_scope is not None and row_scope != current_scope:
                        continue
                payload_json = (
                    row["payload_json"] if "payload_json" in columns else None
                )
                payload: dict[str, Any] = {}
                if isinstance(payload_json, str) and payload_json.strip():
                    try:
                        candidate = json.loads(payload_json)
                    except json.JSONDecodeError:
                        candidate = {}
                    if isinstance(candidate, dict):
                        payload = candidate
                workspace_path_raw = (
                    row["workspace_path"]
                    if "workspace_path" in columns
                    else payload.get("workspace_path")
                )
                workspace_path = self._canonical_workspace_path(workspace_path_raw)
                repo_id = self._resolve_repo_id(
                    row["repo_id"] if "repo_id" in columns else payload.get("repo_id"),
                    workspace_path_raw,
                    repo_id_by_workspace,
                )
                active_thread_id = (
                    row["active_thread_id"]
                    if "active_thread_id" in columns
                    else payload.get("active_thread_id")
                )
                if (
                    not isinstance(active_thread_id, str)
                    or not active_thread_id.strip()
                ):
                    active_thread_id = None
                pma_enabled_raw = payload.get("pma_enabled")
                if not isinstance(pma_enabled_raw, bool):
                    pma_enabled_raw = payload.get("pmaEnabled")
                pma_enabled = (
                    bool(pma_enabled_raw)
                    if isinstance(pma_enabled_raw, bool)
                    else False
                )
                resource_kind = payload.get("resource_kind") or payload.get(
                    "resourceKind"
                )
                if not isinstance(resource_kind, str) or not resource_kind.strip():
                    resource_kind = None
                resource_id = payload.get("resource_id") or payload.get("resourceId")
                if not isinstance(resource_id, str) or not resource_id.strip():
                    resource_id = None
                agent = self._normalize_agent(payload.get("agent"))
                key = (
                    f"telegram:{parsed_chat_id}"
                    if parsed_thread_id is None
                    else f"telegram:{parsed_chat_id}:{parsed_thread_id}"
                )
                bindings.setdefault(
                    key,
                    {
                        "platform": "telegram",
                        "chat_id": str(parsed_chat_id),
                        "thread_id": (
                            str(parsed_thread_id)
                            if isinstance(parsed_thread_id, int)
                            else None
                        ),
                        "surface_key": row["topic_key"],
                        "workspace_path": workspace_path,
                        "repo_id": repo_id,
                        "resource_kind": resource_kind,
                        "resource_id": resource_id,
                        "pma_enabled": pma_enabled,
                        "agent": agent,
                        "active_thread_id": active_thread_id,
                    },
                )
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                f"Hub channel enrichment failed reading telegram bindings from {db_path}",
                exc=exc,
            )
            return {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return bindings

    def _read_orchestration_bindings(
        self, hub_root: Path, *, surface_kind: str
    ) -> dict[str, dict[str, Any]]:
        db_path = resolve_orchestration_sqlite_path(hub_root)
        if not db_path.exists():
            return {}
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_sqlite_read_only(db_path)
            columns = self._table_columns(conn, "orch_bindings")
            if not columns:
                return {}
            select_cols = [
                "surface_key",
                "target_id",
                "agent_id",
                "repo_id",
                "resource_kind",
                "resource_id",
                "mode",
                "updated_at",
            ]
            select_exprs = [col for col in select_cols if col in columns]
            if "surface_key" not in select_exprs or "target_id" not in select_exprs:
                return {}
            query = (
                "SELECT "
                + ", ".join(select_exprs)
                + " FROM orch_bindings"
                + " WHERE disabled_at IS NULL"
                + " AND target_kind = 'thread'"
                + " AND surface_kind = ?"
            )
            if "updated_at" in columns:
                query += " ORDER BY updated_at DESC, rowid DESC"
            bindings: dict[str, dict[str, Any]] = {}
            for row in conn.execute(query, (surface_kind,)).fetchall():
                surface_key = row["surface_key"]
                target_id = row["target_id"]
                if (
                    not isinstance(surface_key, str)
                    or not surface_key.strip()
                    or not isinstance(target_id, str)
                    or not target_id.strip()
                ):
                    continue
                bindings.setdefault(
                    surface_key.strip(),
                    {
                        "thread_target_id": target_id.strip(),
                        "agent": self._normalize_agent(row["agent_id"]),
                        "repo_id": (
                            row["repo_id"].strip()
                            if isinstance(row["repo_id"], str)
                            and row["repo_id"].strip()
                            else None
                        ),
                        "resource_kind": (
                            row["resource_kind"].strip()
                            if isinstance(row["resource_kind"], str)
                            and row["resource_kind"].strip()
                            else None
                        ),
                        "resource_id": (
                            row["resource_id"].strip()
                            if isinstance(row["resource_id"], str)
                            and row["resource_id"].strip()
                            else None
                        ),
                        "mode": (
                            row["mode"].strip()
                            if isinstance(row["mode"], str) and row["mode"].strip()
                            else None
                        ),
                    },
                )
            return bindings
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                f"Hub channel enrichment failed reading orchestration bindings from {db_path}",
                exc=exc,
            )
            return {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _read_active_pma_threads(
        self, hub_root: Path, repo_id_by_workspace: dict[str, str]
    ) -> list[dict[str, Any]]:
        db_path = default_pma_threads_db_path(hub_root)
        if not db_path.exists():
            return []
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_sqlite_read_only(db_path)
            columns = self._table_columns(conn, "pma_managed_threads")
            if not columns:
                return []
            turns_columns = self._table_columns(conn, "pma_managed_turns")
            has_turns_table = bool(turns_columns)

            select_cols = [
                "managed_thread_id",
                "agent",
                "repo_id",
                "workspace_root",
                "name",
                "backend_thread_id",
                "status",
                "normalized_status",
                "status_reason_code",
                "metadata_json",
                "updated_at",
            ]
            select_exprs = [col for col in select_cols if col in columns]
            if "managed_thread_id" not in select_exprs:
                return []
            if has_turns_table:
                select_exprs.append(
                    """
                    EXISTS(
                        SELECT 1
                          FROM pma_managed_turns turns
                         WHERE turns.managed_thread_id = pma_managed_threads.managed_thread_id
                           AND turns.status = 'running'
                          LIMIT 1
                    ) AS has_running_turn
                    """.strip()
                )
            query = (
                "SELECT "
                + ", ".join(select_exprs)
                + " FROM pma_managed_threads WHERE status = 'active'"
            )
            if "updated_at" in columns:
                query += " ORDER BY updated_at DESC, managed_thread_id DESC"
            rows = conn.execute(query).fetchall()
            threads: list[dict[str, Any]] = []
            for row in rows:
                managed_thread_id = row["managed_thread_id"]
                if (
                    not isinstance(managed_thread_id, str)
                    or not managed_thread_id.strip()
                ):
                    continue
                workspace_raw = (
                    row["workspace_root"] if "workspace_root" in columns else None
                )
                workspace_path = self._canonical_workspace_path(workspace_raw)
                repo_id = self._resolve_repo_id(
                    row["repo_id"] if "repo_id" in columns else None,
                    workspace_raw,
                    repo_id_by_workspace,
                )
                agent = self._normalize_agent(
                    row["agent"] if "agent" in columns else None
                )
                backend_thread_id = (
                    row["backend_thread_id"] if "backend_thread_id" in columns else None
                )
                if (
                    not isinstance(backend_thread_id, str)
                    or not backend_thread_id.strip()
                ):
                    backend_thread_id = None
                name = row["name"] if "name" in columns else None
                if not isinstance(name, str) or not name.strip():
                    name = None
                updated_at = row["updated_at"] if "updated_at" in columns else None
                if not isinstance(updated_at, str) or not updated_at.strip():
                    updated_at = None
                normalized_status = (
                    row["normalized_status"] if "normalized_status" in columns else None
                )
                if (
                    not isinstance(normalized_status, str)
                    or not normalized_status.strip()
                ):
                    normalized_status = None
                else:
                    normalized_status = normalized_status.strip()
                status_reason_code = (
                    row["status_reason_code"]
                    if "status_reason_code" in columns
                    else None
                )
                if (
                    not isinstance(status_reason_code, str)
                    or not status_reason_code.strip()
                ):
                    status_reason_code = None
                else:
                    status_reason_code = status_reason_code.strip()
                has_running_turn = bool(
                    row["has_running_turn"] if has_turns_table else False
                )
                metadata_json = (
                    row["metadata_json"] if "metadata_json" in columns else None
                )
                metadata: dict[str, Any] = {}
                if isinstance(metadata_json, str) and metadata_json.strip():
                    try:
                        parsed_metadata = json.loads(metadata_json)
                    except json.JSONDecodeError:
                        parsed_metadata = {}
                    if isinstance(parsed_metadata, dict):
                        metadata = parsed_metadata
                threads.append(
                    {
                        "managed_thread_id": managed_thread_id.strip(),
                        "agent": agent,
                        "repo_id": repo_id,
                        "workspace_path": workspace_path,
                        "backend_thread_id": backend_thread_id,
                        "name": name,
                        "updated_at": updated_at,
                        "normalized_status": normalized_status,
                        "status_reason_code": status_reason_code,
                        "has_running_turn": has_running_turn,
                        "metadata": metadata,
                    }
                )
            return threads
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                f"Hub channel enrichment failed reading PMA threads from {db_path}",
                exc=exc,
            )
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _timestamp_rank(self, value: Any) -> float:
        if isinstance(value, bool):
            return float("-inf")
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            return float("-inf")
        text = value.strip()
        if not text:
            return float("-inf")
        if text.isdigit():
            try:
                return float(int(text))
            except ValueError:
                return float("-inf")
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return float("-inf")

    def _read_usage_by_session(self, workspace_path: str) -> dict[str, dict[str, Any]]:
        canonical = self._canonical_workspace_path(workspace_path)
        if canonical is None:
            return {}
        usage_path = (
            Path(canonical)
            / ".codex-autorunner"
            / "usage"
            / "opencode_turn_usage.jsonl"
        )
        if not usage_path.exists():
            return {}
        by_session: dict[str, dict[str, Any]] = {}
        try:
            with usage_path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    session_id = payload.get("session_id")
                    if not isinstance(session_id, str) or not session_id.strip():
                        continue
                    usage_payload = payload.get("usage")
                    if not isinstance(usage_payload, dict):
                        continue
                    input_tokens = self._coerce_usage_int(
                        usage_payload.get("input_tokens")
                    )
                    cached_input_tokens = self._coerce_usage_int(
                        usage_payload.get("cached_input_tokens")
                    )
                    output_tokens = self._coerce_usage_int(
                        usage_payload.get("output_tokens")
                    )
                    reasoning_output_tokens = self._coerce_usage_int(
                        usage_payload.get("reasoning_output_tokens")
                    )
                    total_tokens = self._coerce_usage_int(
                        usage_payload.get("total_tokens")
                    )
                    if total_tokens is None:
                        total_tokens = sum(
                            value or 0
                            for value in (
                                input_tokens,
                                cached_input_tokens,
                                output_tokens,
                                reasoning_output_tokens,
                            )
                        )
                    timestamp = payload.get("timestamp")
                    if not isinstance(timestamp, str) or not timestamp.strip():
                        timestamp = None
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str) or not turn_id.strip():
                        turn_id = None
                    rank = self._timestamp_rank(timestamp)
                    current = by_session.get(session_id)
                    current_rank = (
                        self._timestamp_rank(current.get("timestamp"))
                        if isinstance(current, dict)
                        else float("-inf")
                    )
                    current_index = (
                        int(current.get("__index", -1))
                        if isinstance(current, dict)
                        else -1
                    )
                    if rank < current_rank or (
                        rank == current_rank and index <= current_index
                    ):
                        continue
                    by_session[session_id] = {
                        "total_tokens": total_tokens if total_tokens is not None else 0,
                        "input_tokens": input_tokens if input_tokens is not None else 0,
                        "cached_input_tokens": (
                            cached_input_tokens
                            if cached_input_tokens is not None
                            else 0
                        ),
                        "output_tokens": (
                            output_tokens if output_tokens is not None else 0
                        ),
                        "reasoning_output_tokens": (
                            reasoning_output_tokens
                            if reasoning_output_tokens is not None
                            else 0
                        ),
                        "turn_id": turn_id,
                        "timestamp": timestamp,
                        "__index": index,
                    }
        except Exception:
            return {}

        for payload in by_session.values():
            payload.pop("__index", None)
        return by_session

    def _resolve_pma_managed_thread_id(
        self,
        *,
        pma_threads: list[dict[str, Any]],
        repo_id: Any,
        workspace_path: Any,
        agent: str,
    ) -> Optional[str]:
        normalized_repo_id = (
            repo_id.strip() if isinstance(repo_id, str) and repo_id.strip() else None
        )
        normalized_workspace = (
            self._canonical_workspace_path(workspace_path)
            if isinstance(workspace_path, str) and workspace_path
            else None
        )
        if not pma_threads:
            return None

        def _matches(thread: dict[str, Any], *, exact_agent: bool) -> bool:
            managed_thread_id = thread.get("managed_thread_id")
            if not isinstance(managed_thread_id, str) or not managed_thread_id.strip():
                return False
            if exact_agent and self._normalize_agent(thread.get("agent")) != agent:
                return False
            thread_workspace = thread.get("workspace_path")
            thread_repo_id = thread.get("repo_id")
            if (
                normalized_workspace is not None
                and isinstance(thread_workspace, str)
                and thread_workspace == normalized_workspace
            ):
                return True
            if (
                normalized_repo_id is not None
                and isinstance(thread_repo_id, str)
                and thread_repo_id == normalized_repo_id
            ):
                return True
            return False

        for exact_agent in (True, False):
            for thread in pma_threads:
                if _matches(thread, exact_agent=exact_agent):
                    managed_thread_id = thread.get("managed_thread_id")
                    if isinstance(managed_thread_id, str) and managed_thread_id.strip():
                        return managed_thread_id.strip()
        return None

    def _channel_row_matches_query(self, row: dict[str, Any], query_text: str) -> bool:
        if not query_text:
            return True
        parts = [
            row.get("key"),
            row.get("display"),
            row.get("source"),
            row.get("repo_id"),
            row.get("workspace_path"),
            row.get("active_thread_id"),
            row.get("status_label"),
            row.get("channel_status"),
            json.dumps(row.get("meta") or {}, sort_keys=True),
            json.dumps(row.get("provenance") or {}, sort_keys=True),
        ]
        haystack = " ".join(str(part or "") for part in parts).lower()
        return query_text in haystack

    def _build_registry_key(
        self,
        entry: dict[str, Any],
        binding: dict[str, Any],
        *,
        telegram_require_topics: bool,
    ) -> Optional[str]:
        platform = str(binding.get("platform") or "").strip().lower()
        agent = self._normalize_agent(binding.get("agent"))
        pma_enabled = bool(binding.get("pma_enabled"))
        if pma_enabled:
            base_key = pma_base_key(agent)
            if platform != "telegram" or not telegram_require_topics:
                return base_key
            chat_id_raw = entry.get("chat_id")
            if chat_id_raw is None:
                chat_id_raw = binding.get("chat_id")
            thread_id_raw = entry.get("thread_id")
            if thread_id_raw is None:
                thread_id_raw = binding.get("thread_id")
            try:
                chat_id = int(str(chat_id_raw))
            except (TypeError, ValueError):
                return base_key
            thread_id: Optional[int]
            if thread_id_raw in (None, "", "root"):
                thread_id = None
            else:
                try:
                    thread_id = int(str(thread_id_raw))
                except (TypeError, ValueError):
                    thread_id = None
            return pma_topic_scoped_key(
                agent, chat_id, thread_id, topic_key_fn=topic_key
            )

        if platform != "discord":
            return None
        channel_id = binding.get("chat_id") or entry.get("chat_id")
        workspace_path = binding.get("workspace_path")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return None
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        return file_chat_discord_key(agent, channel_id, workspace_path)

    def _registry_thread_id(
        self,
        workspace_path: Any,
        registry_key: str,
        thread_map_cache: dict[str, dict[str, str]],
    ) -> Optional[str]:
        if not isinstance(registry_key, str) or not registry_key.strip():
            return None
        canonical_workspace = self._canonical_workspace_path(workspace_path)
        if canonical_workspace is None:
            return None
        thread_map = thread_map_cache.get(canonical_workspace)
        if thread_map is None:
            thread_map = {}
            try:
                registry = AppServerThreadRegistry(
                    default_app_server_threads_path(Path(canonical_workspace))
                )
                loaded = registry.load()
                if isinstance(loaded, dict):
                    for key, value in loaded.items():
                        if isinstance(key, str) and isinstance(value, str) and value:
                            thread_map[key] = value
            except Exception:
                thread_map = {}
            thread_map_cache[canonical_workspace] = thread_map
        try:
            resolved = thread_map.get(registry_key)
            if isinstance(resolved, str) and resolved:
                return resolved
        except Exception:
            return None
        return None

    def _is_working(self, run_state: Optional[dict[str, Any]]) -> bool:
        if not isinstance(run_state, dict):
            return False
        state = run_state.get("state")
        flow_status = run_state.get("flow_status")
        if isinstance(state, str) and state.strip().lower() == "running":
            return True
        if isinstance(flow_status, str) and flow_status.strip().lower() in {
            "running",
            "pending",
            "stopping",
        }:
            return True
        return False

    def _load_workspace_run_data(
        self,
        workspace_path: str,
        repo_id: Optional[str],
        cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        canonical = self._canonical_workspace_path(workspace_path)
        if canonical is None:
            return {}
        cached = cache.get(canonical)
        if cached is not None:
            return cached

        payload: dict[str, Any] = {"run_state": None, "diff_stats": None, "dirty": None}
        workspace_root = Path(canonical)
        run_record = None
        try:
            run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                workspace_root,
                repo_id or workspace_root.name,
            )
            payload["run_state"] = run_state
        except Exception:
            run_record = None
        if run_record is not None:
            db_path = workspace_root / ".codex-autorunner" / "flows.db"
            if db_path.exists():
                try:
                    with FlowStore(db_path) as store:
                        events = store.get_events_by_type(
                            run_record.id, FlowEventType.DIFF_UPDATED
                        )
                    totals = {"insertions": 0, "deletions": 0, "files_changed": 0}
                    for event in events:
                        data = event.data or {}
                        totals["insertions"] += self._coerce_int(data.get("insertions"))
                        totals["deletions"] += self._coerce_int(data.get("deletions"))
                        totals["files_changed"] += self._coerce_int(
                            data.get("files_changed")
                        )
                    payload["diff_stats"] = totals
                except Exception:
                    payload["diff_stats"] = None
        try:
            if (workspace_root / ".git").exists():
                payload["dirty"] = not git_is_clean(workspace_root)
        except Exception:
            payload["dirty"] = None
        cache[canonical] = payload
        return payload

    async def list_chat_channels(
        self, query: Optional[str] = None, limit: int = 100
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        if limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be <= 1000")

        store = ChannelDirectoryStore(self._context.config.root)
        entries = await asyncio.to_thread(store.list_entries, query=None, limit=None)
        snapshots: list[Any] = []
        try:
            snapshots = await asyncio.to_thread(self._context.supervisor.list_repos)
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub channel enrichment failed listing repo snapshots",
                exc=exc,
            )
        repo_id_by_workspace = self._repo_id_by_workspace_path(snapshots)
        discord_state_path = self._state_db_path(
            "discord_bot", DISCORD_STATE_FILE_DEFAULT
        )
        telegram_state_path = self._state_db_path(
            "telegram_bot", TELEGRAM_STATE_FILE_DEFAULT
        )
        telegram_require_topics = self._telegram_require_topics_enabled()
        discord_bindings_task = asyncio.to_thread(
            self._read_discord_bindings, discord_state_path, repo_id_by_workspace
        )
        telegram_bindings_task = asyncio.to_thread(
            self._read_telegram_bindings, telegram_state_path, repo_id_by_workspace
        )
        discord_thread_bindings_task = asyncio.to_thread(
            self._read_orchestration_bindings,
            self._context.config.root,
            surface_kind="discord",
        )
        telegram_thread_bindings_task = asyncio.to_thread(
            self._read_orchestration_bindings,
            self._context.config.root,
            surface_kind="telegram",
        )
        pma_threads_task = asyncio.to_thread(
            self._read_active_pma_threads,
            self._context.config.root,
            repo_id_by_workspace,
        )
        (
            discord_bindings,
            telegram_bindings,
            discord_thread_bindings,
            telegram_thread_bindings,
            pma_threads,
        ) = await asyncio.gather(
            discord_bindings_task,
            telegram_bindings_task,
            discord_thread_bindings_task,
            telegram_thread_bindings_task,
            pma_threads_task,
            return_exceptions=False,
        )
        run_cache: dict[str, dict[str, Any]] = {}
        usage_cache: dict[str, dict[str, dict[str, Any]]] = {}
        thread_map_cache: dict[str, dict[str, str]] = {}
        seen_keys: set[str] = set()
        represented_managed_thread_ids: set[str] = set()

        rows: list[dict[str, Any]] = []
        for entry in entries:
            key = channel_entry_key(entry)
            if not isinstance(key, str):
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)
            row: dict[str, Any] = {
                "key": key,
                "display": entry.get("display"),
                "seen_at": entry.get("seen_at"),
                "meta": entry.get("meta"),
                "entry": entry,
            }
            platform = str(entry.get("platform") or "").strip().lower()
            source = platform if platform in {"discord", "telegram"} else "unknown"
            row["source"] = source
            row["provenance"] = {"source": source}
            binding_source = (
                discord_bindings if platform == "discord" else telegram_bindings
            )
            binding = binding_source.get(key)
            if isinstance(binding, dict):
                try:
                    repo_id = binding.get("repo_id")
                    if isinstance(repo_id, str) and repo_id:
                        row["repo_id"] = repo_id
                    resource_kind = binding.get("resource_kind")
                    if isinstance(resource_kind, str) and resource_kind:
                        row["resource_kind"] = resource_kind
                    resource_id = binding.get("resource_id")
                    if isinstance(resource_id, str) and resource_id:
                        row["resource_id"] = resource_id
                    workspace_path = binding.get("workspace_path")
                    if isinstance(workspace_path, str) and workspace_path:
                        row["workspace_path"] = workspace_path
                    pma_enabled = bool(binding.get("pma_enabled"))
                    agent = self._normalize_agent(binding.get("agent"))
                    if pma_enabled:
                        managed_thread_id: Optional[str] = None
                        binding_surface_key = binding.get("surface_key")
                        if isinstance(binding_surface_key, str) and binding_surface_key:
                            surface_binding = (
                                discord_thread_bindings.get(binding_surface_key)
                                if platform == "discord"
                                else telegram_thread_bindings.get(binding_surface_key)
                            )
                            thread_target_id = (
                                surface_binding.get("thread_target_id")
                                if isinstance(surface_binding, dict)
                                else None
                            )
                            if (
                                isinstance(thread_target_id, str)
                                and thread_target_id.strip()
                            ):
                                managed_thread_id = thread_target_id.strip()
                        if managed_thread_id is None:
                            managed_thread_id = self._resolve_pma_managed_thread_id(
                                pma_threads=pma_threads,
                                repo_id=repo_id,
                                workspace_path=workspace_path,
                                agent=agent,
                            )
                        row["source"] = "pma_thread"
                        row["provenance"] = {
                            "source": "pma_thread",
                            "platform": platform,
                            "agent": agent,
                            "resource_kind": resource_kind,
                            "resource_id": resource_id,
                        }
                        if isinstance(managed_thread_id, str) and managed_thread_id:
                            provenance = row.get("provenance")
                            if isinstance(provenance, dict):
                                provenance["managed_thread_id"] = managed_thread_id
                            represented_managed_thread_ids.add(managed_thread_id)
                    elif source in {"discord", "telegram"}:
                        row["provenance"] = {
                            "source": source,
                            "platform": source,
                            "resource_kind": resource_kind,
                            "resource_id": resource_id,
                        }
                    active_thread_id: Optional[str] = None
                    if platform == "telegram" and not pma_enabled:
                        direct_thread = binding.get("active_thread_id")
                        if isinstance(direct_thread, str) and direct_thread:
                            active_thread_id = direct_thread
                    else:
                        registry_key = self._build_registry_key(
                            entry,
                            binding,
                            telegram_require_topics=telegram_require_topics,
                        )
                        if registry_key:
                            resolved = self._registry_thread_id(
                                workspace_path,
                                registry_key,
                                thread_map_cache,
                            )
                            if isinstance(resolved, str) and resolved:
                                active_thread_id = resolved
                    if isinstance(active_thread_id, str) and active_thread_id:
                        row["active_thread_id"] = active_thread_id

                    run_data: dict[str, Any] = {}
                    if isinstance(workspace_path, str) and workspace_path:
                        run_data = self._load_workspace_run_data(
                            workspace_path,
                            repo_id if isinstance(repo_id, str) else None,
                            run_cache,
                        )
                    run_state = (
                        run_data.get("run_state")
                        if isinstance(run_data, dict)
                        else None
                    )
                    if isinstance(run_data.get("diff_stats"), dict):
                        row["diff_stats"] = run_data["diff_stats"]
                    if isinstance(run_data.get("dirty"), bool):
                        row["dirty"] = run_data["dirty"]

                    if isinstance(active_thread_id, str) and active_thread_id:
                        if self._is_working(
                            run_state if isinstance(run_state, dict) else None
                        ):
                            channel_status = "working"
                        else:
                            channel_status = "final"
                    else:
                        channel_status = "clean"
                    row["channel_status"] = channel_status
                    row["status_label"] = channel_status

                    if (
                        isinstance(workspace_path, str)
                        and workspace_path
                        and isinstance(active_thread_id, str)
                        and active_thread_id
                    ):
                        usage_by_session = usage_cache.get(workspace_path)
                        if usage_by_session is None:
                            usage_by_session = self._read_usage_by_session(
                                workspace_path
                            )
                            usage_cache[workspace_path] = usage_by_session
                        usage_payload = usage_by_session.get(active_thread_id)
                        if isinstance(usage_payload, dict):
                            row["token_usage"] = {
                                "total_tokens": self._coerce_int(
                                    usage_payload.get("total_tokens")
                                ),
                                "input_tokens": self._coerce_int(
                                    usage_payload.get("input_tokens")
                                ),
                                "cached_input_tokens": self._coerce_int(
                                    usage_payload.get("cached_input_tokens")
                                ),
                                "output_tokens": self._coerce_int(
                                    usage_payload.get("output_tokens")
                                ),
                                "reasoning_output_tokens": self._coerce_int(
                                    usage_payload.get("reasoning_output_tokens")
                                ),
                                "turn_id": usage_payload.get("turn_id"),
                                "timestamp": usage_payload.get("timestamp"),
                            }
                except Exception as exc:
                    safe_log(
                        self._context.logger,
                        logging.WARNING,
                        f"Hub channel enrichment failed for {key}",
                        exc=exc,
                    )
            rows.append(row)
        for thread in pma_threads:
            managed_thread_id = thread.get("managed_thread_id")
            if not isinstance(managed_thread_id, str) or not managed_thread_id:
                continue
            if managed_thread_id in represented_managed_thread_ids:
                continue
            key = f"pma_thread:{managed_thread_id}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            repo_id = thread.get("repo_id")
            workspace_path = thread.get("workspace_path")
            backend_thread_id = thread.get("backend_thread_id")
            agent = self._normalize_agent(thread.get("agent"))
            has_running_turn = bool(thread.get("has_running_turn"))
            normalized_status = (
                str(thread.get("normalized_status") or "").strip().lower()
            )
            if not normalized_status:
                normalized_status = "running" if has_running_turn else "idle"
            status_reason_code = str(thread.get("status_reason_code") or "").strip()
            metadata = (
                thread.get("metadata")
                if isinstance(thread.get("metadata"), dict)
                else {}
            )
            display_name = thread.get("name")
            short_id = managed_thread_id[:8]
            if not isinstance(display_name, str) or not display_name.strip():
                display_name = f"PMA {agent} · {short_id}"
            else:
                display_name = display_name.strip()
            row: dict[str, Any] = {
                "key": key,
                "display": display_name,
                "seen_at": thread.get("updated_at"),
                "meta": {
                    "agent": agent,
                    "managed_thread_id": managed_thread_id,
                    "status": normalized_status,
                    "status_reason_code": status_reason_code,
                    "thread_kind": metadata.get("thread_kind"),
                    "run_id": metadata.get("run_id"),
                },
                "entry": {
                    "platform": "pma_thread",
                    "thread_id": managed_thread_id,
                    "agent": agent,
                    "status": normalized_status,
                },
                "source": "pma_thread",
                "provenance": {
                    "source": "pma_thread",
                    "managed_thread_id": managed_thread_id,
                    "agent": agent,
                    "status": normalized_status,
                    "status_reason_code": status_reason_code,
                    "thread_kind": metadata.get("thread_kind"),
                    "run_id": metadata.get("run_id"),
                },
                "active_thread_id": managed_thread_id,
                "channel_status": normalized_status,
                "status_label": normalized_status,
            }
            if isinstance(repo_id, str) and repo_id:
                row["repo_id"] = repo_id
            if isinstance(workspace_path, str) and workspace_path:
                row["workspace_path"] = workspace_path
                run_data = self._load_workspace_run_data(
                    workspace_path,
                    repo_id if isinstance(repo_id, str) else None,
                    run_cache,
                )
                if isinstance(run_data.get("diff_stats"), dict):
                    row["diff_stats"] = run_data["diff_stats"]
                if isinstance(run_data.get("dirty"), bool):
                    row["dirty"] = run_data["dirty"]
                usage_session_id = None
                if isinstance(backend_thread_id, str) and backend_thread_id:
                    usage_session_id = backend_thread_id
                elif managed_thread_id:
                    usage_session_id = managed_thread_id
                if usage_session_id:
                    usage_by_session = usage_cache.get(workspace_path)
                    if usage_by_session is None:
                        usage_by_session = self._read_usage_by_session(workspace_path)
                        usage_cache[workspace_path] = usage_by_session
                    usage_payload = usage_by_session.get(usage_session_id)
                    if isinstance(usage_payload, dict):
                        row["token_usage"] = {
                            "total_tokens": self._coerce_int(
                                usage_payload.get("total_tokens")
                            ),
                            "input_tokens": self._coerce_int(
                                usage_payload.get("input_tokens")
                            ),
                            "cached_input_tokens": self._coerce_int(
                                usage_payload.get("cached_input_tokens")
                            ),
                            "output_tokens": self._coerce_int(
                                usage_payload.get("output_tokens")
                            ),
                            "reasoning_output_tokens": self._coerce_int(
                                usage_payload.get("reasoning_output_tokens")
                            ),
                            "turn_id": usage_payload.get("turn_id"),
                            "timestamp": usage_payload.get("timestamp"),
                        }
            rows.append(row)
        query_text = (query or "").strip().lower()
        if query_text:
            rows = [
                row for row in rows if self._channel_row_matches_query(row, query_text)
            ]
        rows.sort(
            key=lambda item: self._timestamp_rank(item.get("seen_at")), reverse=True
        )
        if limit >= 0:
            rows = rows[:limit]
        return {"entries": rows}


def build_hub_channel_router(context: HubAppContext) -> APIRouter:
    from fastapi import APIRouter

    router = APIRouter()
    channel_service = HubChannelService(context)

    @router.get("/hub/chat/channels")
    async def list_chat_channels(query: Optional[str] = None, limit: int = 100):
        return await channel_service.list_chat_channels(query, limit)

    return router
