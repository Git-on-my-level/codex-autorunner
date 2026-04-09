from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from ....core.chat_bindings import (
    active_chat_binding_counts,
    active_chat_binding_counts_by_source,
)
from ....core.destinations import (
    resolve_effective_repo_destination,
)
from ....core.flows import FlowEventType, FlowStore
from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ....core.git_utils import git_is_clean
from ....core.logging_utils import safe_log
from ....core.pma_context import (
    get_latest_ticket_flow_run_state_with_record,
)
from ....core.pma_thread_store import PmaThreadStore, default_pma_threads_db_path
from ....core.text_utils import _coerce_int
from ....integrations.app_server.threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
    file_chat_discord_key,
    pma_base_key,
    pma_topic_scoped_key,
)
from ....integrations.chat.agents import (
    resolve_chat_agent_and_profile,
)
from ....integrations.telegram.state import topic_key
from ....manifest import Manifest, load_manifest
from ..app_state import HubAppContext
from ..schemas import (
    HubArchiveRepoStateRequest,
    HubArchiveRepoStateResponse,
    HubArchiveWorktreeRequest,
    HubArchiveWorktreeResponse,
    HubArchiveWorktreeStateResponse,
    HubCleanupWorktreeRequest,
    HubCreateWorktreeRequest,
    HubDestinationSetRequest,
    HubJobResponse,
    RunControlRequest,
)
from .hub_repo_routes import (
    HubDestinationService,
    HubMountManager,
    HubRepoEnricher,
    HubRunControlService,
    HubWorktreeService,
    build_hub_agent_workspace_router,
    build_hub_channel_router,
    build_hub_repo_crud_router,
    build_hub_repo_listing_router,
)


def build_hub_repo_routes(
    context: HubAppContext,
    mount_manager: HubMountManager,
) -> APIRouter:
    router = APIRouter()

    def _route_method_path_pairs(target_router: APIRouter) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for route in target_router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or ()
            if not isinstance(path, str):
                continue
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                pairs.add((str(method), path))
        return pairs

    def _prune_overlapping_routes(
        target_router: APIRouter, overlapping_pairs: set[tuple[str, str]]
    ) -> None:
        filtered_routes = []
        for route in target_router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or ()
            if not isinstance(path, str):
                filtered_routes.append(route)
                continue
            route_pairs = {
                (str(method), path)
                for method in methods
                if method not in {"HEAD", "OPTIONS"}
            }
            if route_pairs and route_pairs.issubset(overlapping_pairs):
                continue
            filtered_routes.append(route)
        target_router.routes = filtered_routes

    enricher = HubRepoEnricher(context, mount_manager)
    run_control = HubRunControlService(context, mount_manager, enricher)

    def _build_force_attestation_payload(
        attestation: Optional[str], *, target_scope: str
    ) -> Optional[dict[str, str]]:
        if attestation is None:
            return None
        return {
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": attestation,
            "target_scope": target_scope,
        }

    worktree = HubWorktreeService(
        context, mount_manager, enricher, _build_force_attestation_payload
    )
    destination = HubDestinationService(context, mount_manager)

    def _active_chat_binding_counts() -> dict[str, int]:
        try:
            return active_chat_binding_counts(
                hub_root=context.config.root,
                raw_config=context.config.raw,
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            safe_log(
                context.logger,
                logging.WARNING,
                "Hub active chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    def _active_chat_binding_counts_by_source() -> dict[str, dict[str, int]]:
        try:
            return active_chat_binding_counts_by_source(
                hub_root=context.config.root,
                raw_config=context.config.raw,
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            safe_log(
                context.logger,
                logging.WARNING,
                "Hub source chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    def _enrich_repo(
        snapshot,
        chat_binding_counts: Optional[dict[str, int]] = None,
        chat_binding_counts_by_source: Optional[dict[str, dict[str, int]]] = None,
    ) -> dict:
        return enricher.enrich_repo(
            snapshot, chat_binding_counts, chat_binding_counts_by_source
        )

    def _resolve_manifest_repo(repo_id: str):
        manifest = load_manifest(context.config.manifest_path, context.config.root)
        repos_by_id = {entry.id: entry for entry in manifest.repos}
        repo = repos_by_id.get(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")
        return manifest, repos_by_id, repo

    def _destination_payload(
        manifest: Manifest,
        repo,
        repos_by_id: dict[str, Any],
    ) -> dict[str, Any]:
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        merged_issues = [*manifest.issues_for_repo(repo.id), *resolution.issues]
        deduped_issues: list[str] = []
        for message in merged_issues:
            if message in deduped_issues:
                continue
            deduped_issues.append(message)
        return {
            "repo_id": repo.id,
            "kind": repo.kind,
            "worktree_of": repo.worktree_of,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": deduped_issues,
        }

    def _resolve_agent_state(
        agent: Any,
        profile: Any = None,
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

    def _normalize_agent(value: Any) -> str:
        normalized_agent, _ = _resolve_agent_state(value)
        return normalized_agent

    def _normalize_scope(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _coerce_usage_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _state_db_path(section: str, default_path: str) -> Path:
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

    def _telegram_require_topics_enabled() -> bool:
        raw = context.config.raw if isinstance(context.config.raw, dict) else {}
        telegram_cfg = raw.get("telegram_bot")
        if not isinstance(telegram_cfg, dict):
            return False
        return bool(telegram_cfg.get("require_topics", False))

    def _workspace_path_candidates(value: Any) -> list[str]:
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

    def _canonical_workspace_path(value: Any) -> Optional[str]:
        for candidate in _workspace_path_candidates(value):
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                continue
            try:
                return str(path.resolve())
            except OSError:
                return str(path)
        return None

    def _repo_id_by_workspace_path(snapshots: Iterable[Any]) -> dict[str, str]:
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

    def _resolve_repo_id(
        raw_repo_id: Any,
        workspace_path: Any,
        repo_id_by_workspace: dict[str, str],
    ) -> Optional[str]:
        if isinstance(raw_repo_id, str) and raw_repo_id.strip():
            return raw_repo_id.strip()
        for candidate in _workspace_path_candidates(workspace_path):
            resolved = _canonical_workspace_path(candidate)
            if resolved and resolved in repo_id_by_workspace:
                return repo_id_by_workspace[resolved]
            if candidate in repo_id_by_workspace:
                return repo_id_by_workspace[candidate]
        return None

    def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
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

    def _parse_topic_identity(
        chat_raw: Any, thread_raw: Any, topic_raw: Any
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
        parsed_scope = _normalize_scope(parts[2]) if len(parts) == 3 else None
        return parsed_chat_id, parsed_thread_id, parsed_scope

    def _read_discord_bindings(
        db_path: Path, repo_id_by_workspace: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        if not db_path.exists():
            return {}
        bindings: dict[str, dict[str, Any]] = {}
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _open_sqlite_read_only(db_path)
            columns = _table_columns(conn, "channel_bindings")
            if not columns:
                return {}
            select_cols = ["channel_id"]
            for col in (
                "guild_id",
                "workspace_path",
                "repo_id",
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
                    continue
                workspace_path_raw = (
                    row["workspace_path"] if "workspace_path" in columns else None
                )
                workspace_path = _canonical_workspace_path(workspace_path_raw)
                repo_id = _resolve_repo_id(
                    row["repo_id"] if "repo_id" in columns else None,
                    workspace_path_raw,
                    repo_id_by_workspace,
                )
                agent, agent_profile = _resolve_agent_state(
                    row["agent"] if "agent" in columns else None,
                    row["agent_profile"] if "agent_profile" in columns else None,
                )
                binding = {
                    "platform": "discord",
                    "chat_id": channel_id.strip(),
                    "workspace_path": workspace_path,
                    "repo_id": repo_id,
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
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
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

    def _read_telegram_scope_map(
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
            scope_map[(chat_id, thread_id)] = _normalize_scope(row["scope"])
        return scope_map

    def _read_telegram_bindings(
        db_path: Path, repo_id_by_workspace: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        if not db_path.exists():
            return {}
        bindings: dict[str, dict[str, Any]] = {}
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _open_sqlite_read_only(db_path)
            columns = _table_columns(conn, "telegram_topics")
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
            scope_map = _read_telegram_scope_map(conn)
            query = f"SELECT {', '.join(select_cols)} FROM telegram_topics"
            if "updated_at" in columns:
                query += " ORDER BY updated_at DESC"
            for row in conn.execute(query).fetchall():
                parsed_chat_id, parsed_thread_id, parsed_scope = _parse_topic_identity(
                    row["chat_id"] if "chat_id" in columns else None,
                    row["thread_id"] if "thread_id" in columns else None,
                    row["topic_key"],
                )
                if parsed_chat_id is None:
                    continue
                row_scope = (
                    _normalize_scope(row["scope"])
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
                workspace_path = _canonical_workspace_path(workspace_path_raw)
                repo_id = _resolve_repo_id(
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
                agent, agent_profile = _resolve_agent_state(
                    payload.get("agent"),
                    payload.get("agent_profile") or payload.get("agentProfile"),
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
                        "workspace_path": workspace_path,
                        "repo_id": repo_id,
                        "pma_enabled": pma_enabled,
                        "agent": agent,
                        "agent_profile": agent_profile,
                        "active_thread_id": active_thread_id,
                    },
                )
        except (
            sqlite3.Error,
            json.JSONDecodeError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
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

    def _read_active_pma_threads(
        hub_root: Path,
        repo_id_by_workspace: dict[str, str],
    ) -> list[dict[str, Any]]:
        db_path = default_pma_threads_db_path(hub_root)
        if not db_path.exists():
            return []
        try:
            threads: list[dict[str, Any]] = []
            for row in PmaThreadStore(hub_root).list_threads(status="active"):
                managed_thread_id = row.get("managed_thread_id")
                if (
                    not isinstance(managed_thread_id, str)
                    or not managed_thread_id.strip()
                ):
                    continue
                workspace_raw = row.get("workspace_root")
                workspace_path = _canonical_workspace_path(workspace_raw)
                repo_id = _resolve_repo_id(
                    row.get("repo_id"),
                    workspace_raw,
                    repo_id_by_workspace,
                )
                metadata_raw = row.get("metadata")
                metadata: dict[str, Any] = (
                    metadata_raw if isinstance(metadata_raw, dict) else {}
                )
                agent, agent_profile = _resolve_agent_state(
                    row.get("agent"),
                    metadata.get("agent_profile"),
                )
                name = row.get("name")
                if not isinstance(name, str) or not name.strip():
                    name = None
                updated_at = row.get("updated_at")
                if not isinstance(updated_at, str) or not updated_at.strip():
                    updated_at = None
                normalized_status = row.get("normalized_status")
                if (
                    not isinstance(normalized_status, str)
                    or not normalized_status.strip()
                ):
                    normalized_status = "idle"
                else:
                    normalized_status = normalized_status.strip()
                status_reason_code = row.get("status_reason_code")
                if (
                    not isinstance(status_reason_code, str)
                    or not status_reason_code.strip()
                ):
                    status_reason_code = None
                status_updated_at = row.get("status_updated_at")
                if (
                    not isinstance(status_updated_at, str)
                    or not status_updated_at.strip()
                ):
                    status_updated_at = updated_at
                threads.append(
                    {
                        "managed_thread_id": managed_thread_id.strip(),
                        "agent": agent,
                        "agent_profile": agent_profile,
                        "repo_id": repo_id,
                        "workspace_path": workspace_path,
                        "name": name,
                        "updated_at": updated_at,
                        "has_running_turn": normalized_status == "running",
                        "normalized_status": normalized_status,
                        "status_reason_code": status_reason_code,
                        "status_updated_at": status_updated_at,
                    }
                )
            return threads
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
        ) as exc:
            safe_log(
                context.logger,
                logging.WARNING,
                f"Hub channel enrichment failed reading PMA threads from {db_path}",
                exc=exc,
            )
            return []

    def _timestamp_rank(value: Any) -> float:
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

    def _read_usage_by_session(workspace_path: str) -> dict[str, dict[str, Any]]:
        canonical = _canonical_workspace_path(workspace_path)
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
                    input_tokens = _coerce_usage_int(usage_payload.get("input_tokens"))
                    cached_input_tokens = _coerce_usage_int(
                        usage_payload.get("cached_input_tokens")
                    )
                    output_tokens = _coerce_usage_int(
                        usage_payload.get("output_tokens")
                    )
                    reasoning_output_tokens = _coerce_usage_int(
                        usage_payload.get("reasoning_output_tokens")
                    )
                    total_tokens = _coerce_usage_int(usage_payload.get("total_tokens"))
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
                    rank = _timestamp_rank(timestamp)
                    current = by_session.get(session_id)
                    current_rank = (
                        _timestamp_rank(current.get("timestamp"))
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
        except OSError:
            return {}

        for payload in by_session.values():
            payload.pop("__index", None)
        return by_session

    def _resolve_pma_managed_thread_id(
        *,
        pma_threads: list[dict[str, Any]],
        repo_id: Any,
        workspace_path: Any,
        agent: str,
        agent_profile: Optional[str],
    ) -> Optional[str]:
        normalized_repo_id = (
            repo_id.strip() if isinstance(repo_id, str) and repo_id.strip() else None
        )
        normalized_workspace = (
            _canonical_workspace_path(workspace_path)
            if isinstance(workspace_path, str) and workspace_path
            else None
        )
        if not pma_threads:
            return None

        def _matches(
            thread: dict[str, Any],
            *,
            exact_agent: bool,
            exact_profile: bool,
        ) -> bool:
            managed_thread_id = thread.get("managed_thread_id")
            if not isinstance(managed_thread_id, str) or not managed_thread_id.strip():
                return False
            if exact_agent and _normalize_agent(thread.get("agent")) != agent:
                return False
            if exact_profile and thread.get("agent_profile") != agent_profile:
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

        for exact_agent, exact_profile in (
            (True, True),
            (True, False),
            (False, False),
        ):
            for thread in pma_threads:
                if _matches(
                    thread,
                    exact_agent=exact_agent,
                    exact_profile=exact_profile,
                ):
                    managed_thread_id = thread.get("managed_thread_id")
                    if isinstance(managed_thread_id, str) and managed_thread_id.strip():
                        return managed_thread_id.strip()
        return None

    def _channel_row_matches_query(row: dict[str, Any], query_text: str) -> bool:
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
        entry: dict[str, Any],
        binding: dict[str, Any],
        *,
        telegram_require_topics: bool,
    ) -> Optional[str]:
        platform = str(binding.get("platform") or "").strip().lower()
        agent, agent_profile = _resolve_agent_state(
            binding.get("agent"),
            binding.get("agent_profile"),
        )
        pma_enabled = bool(binding.get("pma_enabled"))
        if pma_enabled:
            base_key = pma_base_key(agent, agent_profile)
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
                agent,
                chat_id,
                thread_id,
                topic_key_fn=topic_key,
                profile=agent_profile,
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
        workspace_path: Any,
        registry_key: str,
        thread_map_cache: dict[str, dict[str, str]],
    ) -> Optional[str]:
        if not isinstance(registry_key, str) or not registry_key.strip():
            return None
        canonical_workspace = _canonical_workspace_path(workspace_path)
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
            except (OSError, ValueError):
                thread_map = {}
            thread_map_cache[canonical_workspace] = thread_map
        try:
            resolved = thread_map.get(registry_key)
            if isinstance(resolved, str) and resolved:
                return resolved
        except (TypeError, AttributeError):
            return None
        return None

    def _is_working(run_state: Optional[dict[str, Any]]) -> bool:
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
        workspace_path: str,
        repo_id: Optional[str],
        cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        canonical = _canonical_workspace_path(workspace_path)
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
        except (sqlite3.Error, OSError, RuntimeError, ValueError, KeyError):
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
                        totals["insertions"] += _coerce_int(data.get("insertions"))
                        totals["deletions"] += _coerce_int(data.get("deletions"))
                        totals["files_changed"] += _coerce_int(
                            data.get("files_changed")
                        )
                    payload["diff_stats"] = totals
                except (sqlite3.Error, OSError, ValueError):
                    payload["diff_stats"] = None
        try:
            if (workspace_root / ".git").exists():
                payload["dirty"] = not git_is_clean(workspace_root)
        except OSError:
            payload["dirty"] = None
        cache[canonical] = payload
        return payload

    @router.get("/hub/repos/{repo_id}/destination")
    async def get_repo_destination(repo_id: str):
        return await destination.get_repo_destination(repo_id)

    @router.post("/hub/repos/{repo_id}/destination")
    async def set_repo_destination(repo_id: str, payload: HubDestinationSetRequest):
        return await destination.set_repo_destination(repo_id, payload)

    @router.post("/hub/worktrees/create")
    async def create_worktree(payload: HubCreateWorktreeRequest):
        return await worktree.create_worktree(
            base_repo_id=payload.base_repo_id,
            branch=payload.branch,
            force=payload.force,
            start_point=payload.start_point,
        )

    @router.post("/hub/jobs/worktrees/create", response_model=HubJobResponse)
    async def create_worktree_job(payload: HubCreateWorktreeRequest):
        return await worktree.create_worktree_job(payload)

    @router.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: HubCleanupWorktreeRequest):
        return await worktree.cleanup_worktree(
            worktree_repo_id=payload.worktree_repo_id,
            delete_branch=payload.delete_branch,
            delete_remote=payload.delete_remote,
            archive=payload.archive,
            force=payload.force,
            force_attestation=payload.force_attestation,
            force_archive=payload.force_archive,
            archive_note=payload.archive_note,
            archive_profile=payload.archive_profile,
        )

    @router.post("/hub/jobs/worktrees/cleanup", response_model=HubJobResponse)
    async def cleanup_worktree_job(payload: HubCleanupWorktreeRequest):
        return await worktree.cleanup_worktree_job(payload)

    @router.post("/hub/worktrees/archive", response_model=HubArchiveWorktreeResponse)
    async def archive_worktree(payload: HubArchiveWorktreeRequest):
        return await worktree.archive_worktree(
            worktree_repo_id=payload.worktree_repo_id,
            archive_note=payload.archive_note,
            archive_profile=payload.archive_profile,
        )

    @router.post(
        "/hub/worktrees/archive-state",
        response_model=HubArchiveWorktreeStateResponse,
    )
    async def archive_worktree_state(payload: HubArchiveWorktreeRequest):
        return await worktree.archive_worktree_state(
            worktree_repo_id=payload.worktree_repo_id,
            archive_note=payload.archive_note,
            archive_profile=payload.archive_profile,
        )

    @router.post(
        "/hub/repos/archive-state",
        response_model=HubArchiveRepoStateResponse,
    )
    async def archive_repo_state(payload: HubArchiveRepoStateRequest):
        try:
            result = await asyncio.to_thread(
                context.supervisor.archive_repo_state,
                repo_id=payload.repo_id,
                archive_note=payload.archive_note,
                archive_profile=payload.archive_profile,
            )
            enricher.invalidate_runtime_caches()
            return result
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/repos/{repo_id}/cleanup-threads")
    async def cleanup_repo_threads(repo_id: str):
        try:
            result = await asyncio.to_thread(
                context.supervisor.cleanup_repo_threads,
                repo_id=repo_id,
            )
            enricher.invalidate_runtime_caches()
            return result
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/repos/cleanup-threads")
    async def cleanup_all_repo_threads():
        try:
            result = await asyncio.to_thread(
                context.supervisor.cleanup_all_repo_threads
            )
            enricher.invalidate_runtime_caches()
            return result
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/hub/cleanup-all/preview")
    async def cleanup_all_preview():
        try:
            result = await asyncio.to_thread(
                context.supervisor.cleanup_all,
                dry_run=True,
            )
            return result
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/jobs/cleanup-all", response_model=HubJobResponse)
    async def cleanup_all_job():
        from ....core.request_context import get_request_id

        async def _run_cleanup_all():
            result = await asyncio.to_thread(context.supervisor.cleanup_all)
            enricher.invalidate_runtime_caches()
            return result

        job = await context.job_manager.submit(
            "hub.cleanup_all",
            _run_cleanup_all,
            request_id=get_request_id(),
        )
        return job.to_dict()

    @router.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        return await run_control.run_repo(repo_id, once)

    @router.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        return await run_control.stop_repo(repo_id)

    @router.post("/hub/repos/{repo_id}/resume")
    async def resume_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        return await run_control.resume_repo(repo_id, once)

    @router.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        return await run_control.kill_repo(repo_id)

    @router.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        return await run_control.init_repo(repo_id)

    @router.post("/hub/repos/{repo_id}/sync-main")
    async def sync_repo_main(repo_id: str):
        return await run_control.sync_main(repo_id)

    listing_router = build_hub_repo_listing_router(context, mount_manager, enricher)
    crud_router = build_hub_repo_crud_router(context, mount_manager, enricher)
    agent_workspace_router = build_hub_agent_workspace_router(context)
    channel_router = build_hub_channel_router(context)
    overlapping_pairs = (
        _route_method_path_pairs(listing_router)
        | _route_method_path_pairs(crud_router)
        | _route_method_path_pairs(agent_workspace_router)
        | _route_method_path_pairs(channel_router)
    )
    _prune_overlapping_routes(router, overlapping_pairs)

    router.include_router(listing_router)
    router.include_router(crud_router)
    router.include_router(agent_workspace_router)
    router.include_router(channel_router)

    return router
