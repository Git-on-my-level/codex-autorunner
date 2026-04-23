from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
)
from .....core.flows import FlowEventType, FlowStore
from .....core.git_utils import git_is_clean
from .....core.logging_utils import safe_log
from .....core.managed_thread_identity import (
    ManagedThreadIdentityStore,
    file_chat_discord_key,
    pma_base_key,
    pma_topic_scoped_key,
)
from .....core.pma_context import (
    get_latest_ticket_flow_run_state_with_record,
)
from .....integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from .....integrations.telegram.state import topic_key
from .channel_source_readers import (
    canonical_workspace_path,
    coerce_int,
    normalize_agent,
    read_active_pma_threads,
    read_discord_bindings,
    read_orchestration_bindings,
    read_telegram_bindings,
    read_usage_by_session,
    resolve_agent_state,
    state_db_path,
    telegram_require_topics_enabled,
    timestamp_rank,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext


_CHANNEL_DIR_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _ChannelDirectoryCacheEntry:
    expires_at: float
    rows: list[dict[str, Any]]


class HubChannelService:
    def __init__(self, context: HubAppContext) -> None:
        self._context = context
        self._channel_dir_cache: Optional[_ChannelDirectoryCacheEntry] = None

    def _resolve_pma_managed_thread_id(
        self,
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
            canonical_workspace_path(workspace_path)
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
            if (
                exact_agent
                and normalize_agent(thread.get("agent"), context=self._context) != agent
            ):
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
            candidates = [
                thread
                for thread in pma_threads
                if _matches(
                    thread,
                    exact_agent=exact_agent,
                    exact_profile=exact_profile,
                )
            ]
            if not candidates:
                continue
            selected = max(
                candidates,
                key=lambda thread: (
                    bool(thread.get("has_running_turn")),
                    timestamp_rank(thread.get("updated_at")),
                ),
            )
            managed_thread_id = selected.get("managed_thread_id")
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
        agent, agent_profile = resolve_agent_state(
            binding.get("agent"),
            binding.get("agent_profile"),
            context=self._context,
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
        self,
        workspace_path: Any,
        registry_key: str,
        thread_map_cache: dict[str, dict[str, str]],
    ) -> Optional[str]:
        if not isinstance(registry_key, str) or not registry_key.strip():
            return None
        canonical_workspace = canonical_workspace_path(workspace_path)
        if canonical_workspace is None:
            return None
        thread_map = thread_map_cache.get(canonical_workspace)
        if thread_map is None:
            thread_map = {}
            try:
                registry = ManagedThreadIdentityStore(Path(canonical_workspace))
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
        except TypeError:
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
        canonical = canonical_workspace_path(workspace_path)
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
        except (sqlite3.Error, OSError):
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
                        totals["insertions"] += coerce_int(data.get("insertions"))
                        totals["deletions"] += coerce_int(data.get("deletions"))
                        totals["files_changed"] += coerce_int(data.get("files_changed"))
                    payload["diff_stats"] = totals
                except sqlite3.Error:
                    payload["diff_stats"] = None
        try:
            if (workspace_root / ".git").exists():
                payload["dirty"] = not git_is_clean(workspace_root)
        except OSError:
            payload["dirty"] = None
        cache[canonical] = payload
        return payload

    async def list_chat_channels(
        self,
        query: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        if limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be <= 1000")

        rows = await self._list_cached_channel_rows()
        query_text = (query or "").strip().lower()
        if query_text:
            rows = [
                row for row in rows if self._channel_row_matches_query(row, query_text)
            ]
        if limit >= 0:
            rows = rows[:limit]
        return {"entries": rows}

    async def _build_channel_rows(self) -> list[dict[str, Any]]:
        store = ChannelDirectoryStore(self._context.config.root)
        entries = await asyncio.to_thread(store.list_entries, query=None, limit=None)
        snapshots: list[Any] = []
        try:
            snapshots = await asyncio.to_thread(self._context.supervisor.list_repos)
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub channel enrichment failed listing repo snapshots",
                exc=exc,
            )
        from .channel_source_readers import repo_id_by_workspace_path

        repo_id_by_workspace = repo_id_by_workspace_path(snapshots)
        discord_state_path = state_db_path(
            self._context, "discord_bot", DISCORD_STATE_FILE_DEFAULT
        )
        telegram_state_path = state_db_path(
            self._context, "telegram_bot", TELEGRAM_STATE_FILE_DEFAULT
        )
        tg_require_topics = telegram_require_topics_enabled(self._context)
        discord_bindings_task = asyncio.to_thread(
            read_discord_bindings,
            discord_state_path,
            repo_id_by_workspace,
            context=self._context,
        )
        telegram_bindings_task = asyncio.to_thread(
            read_telegram_bindings,
            telegram_state_path,
            repo_id_by_workspace,
            context=self._context,
        )
        discord_thread_bindings_task = asyncio.to_thread(
            read_orchestration_bindings,
            self._context.config.root,
            surface_kind="discord",
            context=self._context,
        )
        telegram_thread_bindings_task = asyncio.to_thread(
            read_orchestration_bindings,
            self._context.config.root,
            surface_kind="telegram",
            context=self._context,
        )
        pma_threads_task = asyncio.to_thread(
            read_active_pma_threads,
            self._context.config.root,
            repo_id_by_workspace,
            context=self._context,
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
                    agent = normalize_agent(binding.get("agent"), context=self._context)
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
                            binding_mode = (
                                str(surface_binding.get("mode") or "").strip().lower()
                                if isinstance(surface_binding, dict)
                                else ""
                            )
                            if (
                                binding_mode == "pma"
                                and isinstance(thread_target_id, str)
                                and thread_target_id.strip()
                            ):
                                managed_thread_id = thread_target_id.strip()
                        if managed_thread_id is None:
                            managed_thread_id = self._resolve_pma_managed_thread_id(
                                pma_threads=pma_threads,
                                repo_id=repo_id,
                                workspace_path=workspace_path,
                                agent=agent,
                                agent_profile=binding.get("agent_profile"),
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
                            telegram_require_topics=tg_require_topics,
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
                        managed_thread_id = (
                            row.get("provenance", {}).get("managed_thread_id")
                            if isinstance(row.get("provenance"), dict)
                            else None
                        )
                        if isinstance(managed_thread_id, str) and managed_thread_id:
                            represented_managed_thread_ids.add(managed_thread_id)

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
                            usage_by_session = read_usage_by_session(workspace_path)
                            usage_cache[workspace_path] = usage_by_session
                        usage_payload = usage_by_session.get(active_thread_id)
                        if isinstance(usage_payload, dict):
                            row["token_usage"] = {
                                "total_tokens": coerce_int(
                                    usage_payload.get("total_tokens")
                                ),
                                "input_tokens": coerce_int(
                                    usage_payload.get("input_tokens")
                                ),
                                "cached_input_tokens": coerce_int(
                                    usage_payload.get("cached_input_tokens")
                                ),
                                "output_tokens": coerce_int(
                                    usage_payload.get("output_tokens")
                                ),
                                "reasoning_output_tokens": coerce_int(
                                    usage_payload.get("reasoning_output_tokens")
                                ),
                                "turn_id": usage_payload.get("turn_id"),
                                "timestamp": usage_payload.get("timestamp"),
                            }
                except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
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
            agent = normalize_agent(thread.get("agent"), context=self._context)
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
            thread_row: dict[str, Any] = {
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
                thread_row["repo_id"] = repo_id
            if isinstance(workspace_path, str) and workspace_path:
                thread_row["workspace_path"] = workspace_path
                run_data = self._load_workspace_run_data(
                    workspace_path,
                    repo_id if isinstance(repo_id, str) else None,
                    run_cache,
                )
                if isinstance(run_data.get("diff_stats"), dict):
                    thread_row["diff_stats"] = run_data["diff_stats"]
                if isinstance(run_data.get("dirty"), bool):
                    thread_row["dirty"] = run_data["dirty"]
                usage_session_id = managed_thread_id
                if usage_session_id:
                    usage_by_session = usage_cache.get(workspace_path)
                    if usage_by_session is None:
                        usage_by_session = read_usage_by_session(workspace_path)
                        usage_cache[workspace_path] = usage_by_session
                    usage_payload = usage_by_session.get(usage_session_id)
                    if isinstance(usage_payload, dict):
                        thread_row["token_usage"] = {
                            "total_tokens": coerce_int(
                                usage_payload.get("total_tokens")
                            ),
                            "input_tokens": coerce_int(
                                usage_payload.get("input_tokens")
                            ),
                            "cached_input_tokens": coerce_int(
                                usage_payload.get("cached_input_tokens")
                            ),
                            "output_tokens": coerce_int(
                                usage_payload.get("output_tokens")
                            ),
                            "reasoning_output_tokens": coerce_int(
                                usage_payload.get("reasoning_output_tokens")
                            ),
                            "turn_id": usage_payload.get("turn_id"),
                            "timestamp": usage_payload.get("timestamp"),
                        }
            rows.append(thread_row)
        rows.sort(key=lambda item: timestamp_rank(item.get("seen_at")), reverse=True)
        return rows

    async def _list_cached_channel_rows(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._channel_dir_cache
        if cached is not None and cached.expires_at > now:
            return copy.deepcopy(cached.rows)

        rows = await self._build_channel_rows()
        self._channel_dir_cache = _ChannelDirectoryCacheEntry(
            expires_at=time.monotonic() + _CHANNEL_DIR_CACHE_TTL_SECONDS,
            rows=copy.deepcopy(rows),
        )
        return rows


def build_hub_channel_router(context: HubAppContext) -> APIRouter:
    from fastapi import APIRouter

    router = APIRouter()
    channel_service = HubChannelService(context)

    @router.get("/hub/chat/channels")
    async def list_chat_channels(query: Optional[str] = None, limit: int = 100):
        return await channel_service.list_chat_channels(query, limit)

    return router
