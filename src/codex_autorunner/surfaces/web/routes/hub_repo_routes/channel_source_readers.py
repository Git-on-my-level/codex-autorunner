"""Source readers and normalization helpers for the hub channel directory.

Reads Discord, Telegram, orchestration, PMA-thread, and usage data from
various SQLite and JSONL sources, normalizing into uniform binding/row dicts.
These are pure-ish reader functions; the route-level HubChannelService owns
assembly, caching, and HTTP concerns.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import unquote

from .....core.logging_utils import safe_log
from .....core.orchestration.sqlite import resolve_orchestration_sqlite_path
from .....core.pma_thread_store import PmaThreadStore
from .....core.text_utils import _coerce_int as _standalone_coerce_int
from .....integrations.chat.agents import (
    resolve_chat_agent_and_profile,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...app_state import HubAppContext


def workspace_path_candidates(value: Any) -> list[str]:
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


def canonical_workspace_path(value: Any) -> Optional[str]:
    for candidate in workspace_path_candidates(value):
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            continue
        try:
            return str(path.resolve())
        except OSError:
            return str(path)
    return None


def repo_id_by_workspace_path(snapshots: Iterable[Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for snapshot in snapshots:
        repo_id = getattr(snapshot, "id", None)
        path = getattr(snapshot, "path", None)
        if not isinstance(repo_id, str) or not isinstance(path, Path):
            continue
        mapping[str(path)] = repo_id
        try:
            mapping[str(path.resolve())] = repo_id
        except OSError:
            pass
    return mapping


def resolve_repo_id(
    raw_repo_id: Any,
    workspace_path: Any,
    repo_id_by_workspace: dict[str, str],
) -> Optional[str]:
    if isinstance(raw_repo_id, str) and raw_repo_id.strip():
        return raw_repo_id.strip()
    for candidate in workspace_path_candidates(workspace_path):
        resolved = canonical_workspace_path(candidate)
        if resolved and resolved in repo_id_by_workspace:
            return repo_id_by_workspace[resolved]
        if candidate in repo_id_by_workspace:
            return repo_id_by_workspace[candidate]
    return None


def open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
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


def normalize_scope(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def coerce_int(value: Any) -> int:
    return _standalone_coerce_int(value)


def coerce_usage_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def timestamp_rank(value: Any) -> float:
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


def resolve_agent_state(
    agent: Any,
    profile: Any = None,
    *,
    context: HubAppContext,
) -> tuple[str, Optional[str]]:
    normalized_agent, normalized_profile = resolve_chat_agent_and_profile(
        agent,
        profile,
        default="codex",
        context=context,
    )
    if (
        normalized_agent == "hermes"
        and normalized_profile is None
        and isinstance(profile, str)
    ):
        raw_profile = profile.strip().lower()
        if raw_profile:
            normalized_profile = raw_profile
    return normalized_agent, normalized_profile


def normalize_agent(value: Any, *, context: HubAppContext) -> str:
    normalized_agent, _ = resolve_agent_state(value, context=context)
    return normalized_agent


def parse_topic_identity(
    chat_raw: Any,
    thread_raw: Any,
    topic_raw: Any,
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
    parsed_scope = normalize_scope(parts[2]) if len(parts) == 3 else None
    return parsed_chat_id, parsed_thread_id, parsed_scope


def state_db_path(context: HubAppContext, section: str, default_path: str) -> Path:
    raw = context.config.raw if isinstance(context.config.raw, dict) else {}
    section_cfg = raw.get(section)
    state_file = default_path
    if isinstance(section_cfg, dict):
        candidate = section_cfg.get("state_file")
        if isinstance(candidate, str) and candidate.strip():
            state_file = candidate.strip()
    path = Path(state_file)
    if not path.is_absolute():
        path = (context.config.root / path).resolve()
    return path


def telegram_require_topics_enabled(context: HubAppContext) -> bool:
    raw = context.config.raw if isinstance(context.config.raw, dict) else {}
    telegram_cfg = raw.get("telegram_bot")
    if not isinstance(telegram_cfg, dict):
        return False
    return bool(telegram_cfg.get("require_topics", False))


def read_discord_bindings(
    db_path: Path,
    repo_id_by_workspace: dict[str, str],
    *,
    context: HubAppContext,
) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    bindings: dict[str, dict[str, Any]] = {}
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = open_sqlite_read_only(db_path)
        columns = table_columns(conn, "channel_bindings")
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
            "agent_profile",
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
                logger.debug(
                    "discord binding row skipped: channel_id is not a non-empty string (%r)",
                    channel_id,
                )
                continue
            workspace_path_raw = (
                row["workspace_path"] if "workspace_path" in columns else None
            )
            wp = canonical_workspace_path(workspace_path_raw)
            repo_id = resolve_repo_id(
                row["repo_id"] if "repo_id" in columns else None,
                workspace_path_raw,
                repo_id_by_workspace,
            )
            agent, agent_profile = resolve_agent_state(
                row["agent"] if "agent" in columns else None,
                row["agent_profile"] if "agent_profile" in columns else None,
                context=context,
            )
            binding = {
                "platform": "discord",
                "chat_id": channel_id.strip(),
                "surface_key": channel_id.strip(),
                "workspace_path": wp,
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
                "agent": agent,
                "agent_profile": agent_profile,
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
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        safe_log(
            context.logger,
            logging.WARNING,
            f"Hub channel enrichment failed reading discord bindings from {db_path}",
            exc=exc,
        )
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logging.getLogger(__name__).debug(
                    "Failed to close discord sqlite connection", exc_info=True
                )
    return bindings


def read_telegram_scope_map(
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
        scope_map[(chat_id, thread_id)] = normalize_scope(row["scope"])
    return scope_map


def read_telegram_bindings(
    db_path: Path,
    repo_id_by_workspace: dict[str, str],
    *,
    context: HubAppContext,
) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    bindings: dict[str, dict[str, Any]] = {}
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = open_sqlite_read_only(db_path)
        columns = table_columns(conn, "telegram_topics")
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
        scope_map = read_telegram_scope_map(conn)
        query = f"SELECT {', '.join(select_cols)} FROM telegram_topics"
        if "updated_at" in columns:
            query += " ORDER BY updated_at DESC"
        for row in conn.execute(query).fetchall():
            parsed_chat_id, parsed_thread_id, parsed_scope = parse_topic_identity(
                row["chat_id"] if "chat_id" in columns else None,
                row["thread_id"] if "thread_id" in columns else None,
                row["topic_key"],
            )
            if parsed_chat_id is None:
                logger.debug(
                    "telegram binding row skipped: topic_key=%r could not resolve chat_id",
                    row["topic_key"],
                )
                continue
            row_scope = (
                normalize_scope(row["scope"]) if "scope" in columns else parsed_scope
            )
            if scope_map is not None:
                current_scope = scope_map.get((parsed_chat_id, parsed_thread_id))
                if current_scope is None and row_scope is not None:
                    continue
                if current_scope is not None and row_scope != current_scope:
                    continue
            payload_json = row["payload_json"] if "payload_json" in columns else None
            payload: dict[str, Any] = {}
            if isinstance(payload_json, str) and payload_json.strip():
                try:
                    candidate = json.loads(payload_json)
                except json.JSONDecodeError:
                    logger.warning(
                        "telegram binding row skipped for topic_key=%r: payload_json is not valid JSON",
                        row["topic_key"],
                    )
                    continue
                if not isinstance(candidate, dict):
                    logger.warning(
                        "telegram binding row skipped for topic_key=%r: payload_json parsed to %s, expected dict",
                        row["topic_key"],
                        type(candidate).__name__,
                    )
                    continue
                payload = candidate
            workspace_path_raw = (
                row["workspace_path"]
                if "workspace_path" in columns
                else payload.get("workspace_path")
            )
            wp = canonical_workspace_path(workspace_path_raw)
            repo_id = resolve_repo_id(
                row["repo_id"] if "repo_id" in columns else payload.get("repo_id"),
                workspace_path_raw,
                repo_id_by_workspace,
            )
            active_thread_id = (
                row["active_thread_id"]
                if "active_thread_id" in columns
                else payload.get("active_thread_id")
            )
            if not isinstance(active_thread_id, str) or not active_thread_id.strip():
                active_thread_id = None
            pma_enabled_raw = payload.get("pma_enabled")
            if not isinstance(pma_enabled_raw, bool):
                pma_enabled_raw = payload.get("pmaEnabled")
            pma_enabled = (
                bool(pma_enabled_raw) if isinstance(pma_enabled_raw, bool) else False
            )
            resource_kind = payload.get("resource_kind") or payload.get("resourceKind")
            if not isinstance(resource_kind, str) or not resource_kind.strip():
                resource_kind = None
            resource_id = payload.get("resource_id") or payload.get("resourceId")
            if not isinstance(resource_id, str) or not resource_id.strip():
                resource_id = None
            agent, agent_profile = resolve_agent_state(
                payload.get("agent"),
                payload.get("agent_profile") or payload.get("agentProfile"),
                context=context,
            )
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
                    "workspace_path": wp,
                    "repo_id": repo_id,
                    "resource_kind": resource_kind,
                    "resource_id": resource_id,
                    "pma_enabled": pma_enabled,
                    "agent": agent,
                    "agent_profile": agent_profile,
                    "active_thread_id": active_thread_id,
                },
            )
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        safe_log(
            context.logger,
            logging.WARNING,
            f"Hub channel enrichment failed reading telegram bindings from {db_path}",
            exc=exc,
        )
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logging.getLogger(__name__).debug(
                    "Failed to close telegram sqlite connection", exc_info=True
                )
    return bindings


def read_orchestration_bindings(
    hub_root: Path,
    *,
    surface_kind: str,
    context: HubAppContext,
) -> dict[str, dict[str, Any]]:
    db_path = resolve_orchestration_sqlite_path(hub_root)
    if not db_path.exists():
        return {}
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = open_sqlite_read_only(db_path)
        columns = table_columns(conn, "orch_bindings")
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
                logger.debug(
                    "orchestration binding row skipped: surface_key=%r, target_id=%r",
                    surface_key,
                    target_id,
                )
                continue
            bindings.setdefault(
                surface_key.strip(),
                {
                    "thread_target_id": target_id.strip(),
                    "agent": normalize_agent(row["agent_id"], context=context),
                    "repo_id": (
                        row["repo_id"].strip()
                        if isinstance(row["repo_id"], str) and row["repo_id"].strip()
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
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        safe_log(
            context.logger,
            logging.WARNING,
            f"Hub channel enrichment failed reading orchestration bindings from {db_path}",
            exc=exc,
        )
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logging.getLogger(__name__).debug(
                    "Failed to close orchestration sqlite connection", exc_info=True
                )


def read_active_pma_threads(
    hub_root: Path,
    repo_id_by_workspace: dict[str, str],
    *,
    context: HubAppContext,
) -> list[dict[str, Any]]:
    db_path = resolve_orchestration_sqlite_path(hub_root)
    if not db_path.exists():
        return []
    try:
        threads: list[dict[str, Any]] = []
        for row in PmaThreadStore(hub_root).list_threads(status="active"):
            managed_thread_id = row.get("managed_thread_id")
            if not isinstance(managed_thread_id, str) or not managed_thread_id.strip():
                continue
            workspace_raw = row.get("workspace_root")
            wp = canonical_workspace_path(workspace_raw)
            repo_id = resolve_repo_id(
                row.get("repo_id"),
                workspace_raw,
                repo_id_by_workspace,
            )
            metadata_raw = row.get("metadata")
            metadata: dict[str, Any] = (
                metadata_raw if isinstance(metadata_raw, dict) else {}
            )
            agent, agent_profile = resolve_agent_state(
                row.get("agent"),
                metadata.get("agent_profile"),
                context=context,
            )
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                name = None
            updated_at = row.get("updated_at")
            if not isinstance(updated_at, str) or not updated_at.strip():
                updated_at = None
            normalized_status = row.get("normalized_status")
            if not isinstance(normalized_status, str) or not normalized_status.strip():
                normalized_status = None
            else:
                normalized_status = normalized_status.strip()
            status_reason_code = row.get("status_reason_code")
            if (
                not isinstance(status_reason_code, str)
                or not status_reason_code.strip()
            ):
                status_reason_code = None
            else:
                status_reason_code = status_reason_code.strip()
            threads.append(
                {
                    "managed_thread_id": managed_thread_id.strip(),
                    "agent": agent,
                    "agent_profile": agent_profile,
                    "repo_id": repo_id,
                    "workspace_path": wp,
                    "name": name,
                    "updated_at": updated_at,
                    "normalized_status": normalized_status,
                    "status_reason_code": status_reason_code,
                    "has_running_turn": normalized_status == "running",
                    "metadata": metadata,
                }
            )
        return threads
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        safe_log(
            context.logger,
            logging.WARNING,
            f"Hub channel enrichment failed reading PMA threads from {db_path}",
            exc=exc,
        )
        return []


def read_usage_by_session(workspace_path: str) -> dict[str, dict[str, Any]]:
    canonical = canonical_workspace_path(workspace_path)
    if canonical is None:
        return {}
    usage_path = (
        Path(canonical) / ".codex-autorunner" / "usage" / "opencode_turn_usage.jsonl"
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
                input_tokens = coerce_usage_int(usage_payload.get("input_tokens"))
                cached_input_tokens = coerce_usage_int(
                    usage_payload.get("cached_input_tokens")
                )
                output_tokens = coerce_usage_int(usage_payload.get("output_tokens"))
                reasoning_output_tokens = coerce_usage_int(
                    usage_payload.get("reasoning_output_tokens")
                )
                total_tokens = coerce_usage_int(usage_payload.get("total_tokens"))
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
                ts = payload.get("timestamp")
                if not isinstance(ts, str) or not ts.strip():
                    ts = None
                turn_id = payload.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id.strip():
                    turn_id = None
                rank = timestamp_rank(ts)
                current = by_session.get(session_id)
                current_rank = (
                    timestamp_rank(current.get("timestamp"))
                    if isinstance(current, dict)
                    else float("-inf")
                )
                current_index = (
                    int(current.get("__index", -1)) if isinstance(current, dict) else -1
                )
                if rank < current_rank or (
                    rank == current_rank and index <= current_index
                ):
                    continue
                by_session[session_id] = {
                    "total_tokens": total_tokens if total_tokens is not None else 0,
                    "input_tokens": input_tokens if input_tokens is not None else 0,
                    "cached_input_tokens": (
                        cached_input_tokens if cached_input_tokens is not None else 0
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
                    "timestamp": ts,
                    "__index": index,
                }
    except OSError:
        return {}

    for payload in by_session.values():
        payload.pop("__index", None)
    return by_session
