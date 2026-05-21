from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Protocol, cast

from fastapi import HTTPException

from ....adapters.chat.channel_directory import ChannelDirectoryStore, channel_entry_key
from ....contextspace.paths import read_contextspace_docs
from ....core.chat_bindings import active_chat_binding_counts_by_source
from ....core.freshness import iso_now
from ....core.orchestration.flow_run_projection import (
    project_ticket_flow_run_records,
)
from ....core.orchestration.sqlite import open_orchestration_sqlite
from ....core.state_roots import resolve_repo_flows_db_path, resolve_repo_state_root
from ....tickets.files import list_ticket_paths
from ..read_model_contracts import (
    PageWindow,
    ProjectionCursor,
    RepairPolicy,
    RepoTopology,
    RepoWorktreeDetailSnapshot,
    RepoWorktreeRuntimeSnapshot,
    RepoWorktreeTopologySnapshot,
    RunProjection,
    RuntimeProjection,
    TicketDetailSnapshot,
    TicketProjection,
    TicketQueueSibling,
    WorktreeTopology,
    dump_read_model_contract,
    read_model_now,
)
from ..routes.hub_repo_routes.channel_source_readers import (
    read_discord_bindings,
    read_orchestration_bindings,
    read_telegram_bindings,
    state_db_path,
    workspace_scope_index,
)
from . import flow_store as flow_store_service
from .flow_history_artifacts import get_dispatch_history
from .ticket_read_models import (
    enrich_current_ticket_payload,
    mark_duplicate_ticket_numbers,
    ticket_payload,
)

DISCORD_STATE_FILE_DEFAULT = ".codex-autorunner/discord_state.sqlite3"
TELEGRAM_STATE_FILE_DEFAULT = ".codex-autorunner/telegram_state.sqlite3"


class _HubSupervisorPort(Protocol):
    def list_repos(self, *, use_cache: bool) -> list[Any]: ...


class _HubConfigPort(Protocol):
    @property
    def root(self) -> Path: ...


class HubAppContextPort(Protocol):
    @property
    def config(self) -> _HubConfigPort: ...

    @property
    def logger(self) -> Any: ...

    @property
    def supervisor(self) -> _HubSupervisorPort: ...


class HubMountManagerPort(Protocol):
    async def refresh_mounts(self, snapshots: list[Any]) -> None: ...


class HubRepoEnricherPort(Protocol):
    def enrich_repo(
        self,
        snapshot: Any,
        chat_binding_counts: Optional[dict[str, int]] = None,
        chat_binding_counts_by_source: Optional[dict[str, dict[str, int]]] = None,
        unbound_thread_counts: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]: ...


def _bounded_limit(limit: int, *, default: int = 50, maximum: int = 200) -> int:
    if limit <= 0:
        return default
    return max(1, min(limit, maximum))


def _offset_cursor(raw: Optional[str]) -> int:
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid cursor") from None


def _cursor(source: str, sequence: int = 1) -> ProjectionCursor:
    return ProjectionCursor(
        value=f"{source}:{sequence}",
        sequence=sequence,
        source=source,
        issued_at=read_model_now(),
    )


def _window(*, offset: int, limit: int, total: int) -> PageWindow:
    next_offset = offset + limit
    previous_offset = max(0, offset - limit)
    return PageWindow(
        limit=limit,
        next_cursor=str(next_offset) if next_offset < total else None,
        previous_cursor=str(previous_offset) if offset > 0 else None,
        total_estimate=total,
        total_is_exact=True,
    )


def _dict_value(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


async def _empty_list() -> list[Any]:
    return []


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _topology_worktree_setup_commands(item: dict[str, Any]) -> Optional[list[str]]:
    raw = item.get("worktree_setup_commands")
    if not isinstance(raw, list):
        return None
    commands = [str(cmd) for cmd in raw if str(cmd).strip()]
    return commands or None


def _positive_source_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        count = _int_value(raw_count)
        if count > 0:
            out[raw_key.strip()] = count
    return out


def _chat_binding_sources(item: Mapping[str, Any]) -> dict[str, int]:
    sources = _positive_source_counts(item.get("chat_binding_sources"))
    if sources:
        return sources
    fallback: dict[str, int] = {}
    for source in ("pma", "discord", "telegram"):
        count = _int_value(item.get(f"{source}_chat_bound_thread_count"))
        if count > 0:
            fallback[source] = count
    return fallback


def _chat_binding_display_names(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("chat_binding_display_names")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        names.append(text)
    return names


def _chat_binding_count(item: Mapping[str, Any]) -> int:
    explicit = _int_value(item.get("chat_bound_thread_count"))
    if explicit > 0:
        return explicit
    return sum(_chat_binding_sources(item).values())


def _runtime_projection(item: dict[str, Any]) -> RuntimeProjection:
    item_id = str(
        item.get("id") or item.get("repo_id") or item.get("worktree_id") or ""
    )
    is_worktree = str(item.get("kind") or "") == "worktree" or bool(
        item.get("worktree_of")
    )
    ticket_flow = _dict_value(item.get("ticket_flow_display"))
    run_state = _dict_value(item.get("run_state"))
    git = _dict_value(item.get("git_status"))
    status = ticket_flow.get("status") or run_state.get("flow_status")
    active_run_id = ticket_flow.get("run_id") or item.get("last_run_id")
    total_count = _int_value(ticket_flow.get("total_count"))
    done_count = _int_value(ticket_flow.get("done_count"))
    active_count = 1 if status in {"running", "paused", "waiting", "blocked"} else 0
    cleanup_blockers: list[str] = []
    if item.get("cleanup_blocked_by_chat_binding"):
        cleanup_blockers.append("chat_binding")
    return RuntimeProjection(
        entity_kind="worktree" if is_worktree else "repo",
        entity_id=item_id,
        git_dirty=git.get("dirty") if isinstance(git.get("dirty"), bool) else None,
        git_ahead=git.get("ahead") if isinstance(git.get("ahead"), int) else None,
        git_behind=git.get("behind") if isinstance(git.get("behind"), int) else None,
        active_run_id=str(active_run_id) if active_run_id else None,
        active_run_status=str(status) if status else None,
        waiting_ticket_count=max(0, total_count - done_count - active_count),
        running_ticket_count=active_count,
        chat_count=_int_value(item.get("chat_bound_thread_count"))
        + _int_value(item.get("unbound_managed_thread_count")),
        cleanup_blockers=cleanup_blockers,
        updated_at=read_model_now(),
    )


def _run_payload(
    record: Any, projected: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    status = getattr(getattr(record, "status", None), "value", None) or getattr(
        record, "status", None
    )
    state = getattr(record, "state", None)
    payload = {
        "id": str(getattr(record, "id", "")),
        "run_id": str(getattr(record, "id", "")),
        "flow_type": getattr(record, "flow_type", None),
        "status": status,
        "current_step": getattr(record, "current_step", None),
        "created_at": getattr(record, "created_at", None),
        "started_at": getattr(record, "started_at", None),
        "finished_at": getattr(record, "finished_at", None),
        "error_message": getattr(record, "error_message", None),
        "state": state if isinstance(state, dict) else {},
    }
    if projected:
        payload["projection"] = dict(projected)
        payload["current_ticket"] = projected.get("current_ticket")
        payload["archive_ready"] = projected.get("archive_ready")
        progress = projected.get("progress")
        if isinstance(progress, Mapping):
            payload["progress"] = dict(progress)
    return payload


def _ticket_projection_status(raw: object) -> str:
    status = str(raw or "queued")
    if status == "idle":
        return "queued"
    if status in {
        "queued",
        "waiting",
        "running",
        "blocked",
        "done",
        "failed",
        "invalid",
    }:
        return status
    return "queued"


def _ticket_detail_compatibility_fields(
    *,
    selected_ticket: dict[str, Any],
    scoped_tickets: list[dict[str, Any]],
    scoped_runs: list[dict[str, Any]],
    scoped_chats: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep legacy response keys isolated while clients migrate to typed fields."""
    return {
        "legacyTicket": selected_ticket,
        "scopedTickets": scoped_tickets,
        "scopedRuns": scoped_runs,
        "scopedChats": scoped_chats,
    }


def _repo_worktree_detail_compatibility_fields(
    *,
    scoped_tickets: list[dict[str, Any]],
    scoped_runs: list[dict[str, Any]],
    scoped_chats: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scopedTickets": scoped_tickets,
        "scopedRuns": scoped_runs,
        "scopedChats": scoped_chats,
    }


class RepoWorktreeReadModelService:
    def __init__(
        self,
        context: HubAppContextPort,
        mount_manager: HubMountManagerPort,
        enricher: HubRepoEnricherPort,
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager
        self._enricher = enricher

    def _active_chat_binding_counts_by_source(self) -> dict[str, dict[str, int]]:
        raw_config = getattr(self._context.config, "raw", {})
        if not isinstance(raw_config, Mapping):
            raw_config = {}
        try:
            return active_chat_binding_counts_by_source(
                hub_root=self._context.config.root,
                raw_config=raw_config,
            )
        except Exception as exc:  # intentional: keep repo/worktree read model available
            logger = getattr(self._context, "logger", None)
            if logger is not None:
                logger.warning("Repo/worktree chat binding lookup failed: %s", exc)
            return {}

    def _chat_binding_display_names_by_repo(
        self, snapshots: list[Any]
    ) -> dict[str, list[str]]:
        root = self._context.config.root
        display_by_key = self._channel_display_by_key(root)
        scope_index = workspace_scope_index(snapshots)
        displays_by_repo: dict[str, list[str]] = {}
        context = cast(Any, self._context)

        binding_groups: list[tuple[str, dict[str, dict[str, Any]]]] = []
        try:
            discord_path = state_db_path(
                context, "discord_bot", DISCORD_STATE_FILE_DEFAULT
            )
            binding_groups.append(
                (
                    "discord",
                    read_discord_bindings(discord_path, scope_index, context=context),
                )
            )
        except Exception as exc:  # intentional: optional display-name enrichment
            logger = getattr(self._context, "logger", None)
            if logger is not None:
                logger.warning("Discord binding display lookup failed: %s", exc)
        try:
            telegram_path = state_db_path(
                context, "telegram_bot", TELEGRAM_STATE_FILE_DEFAULT
            )
            binding_groups.append(
                (
                    "telegram",
                    read_telegram_bindings(telegram_path, scope_index, context=context),
                )
            )
        except Exception as exc:  # intentional: optional display-name enrichment
            logger = getattr(self._context, "logger", None)
            if logger is not None:
                logger.warning("Telegram binding display lookup failed: %s", exc)
        for surface in ("discord", "telegram"):
            try:
                binding_groups.append(
                    (
                        surface,
                        read_orchestration_bindings(
                            root, surface_kind=surface, context=context
                        ),
                    )
                )
            except Exception as exc:  # intentional: optional display-name enrichment
                logger = getattr(self._context, "logger", None)
                if logger is not None:
                    logger.warning(
                        "%s orchestration binding display lookup failed: %s",
                        surface.capitalize(),
                        exc,
                    )

        for surface, bindings in binding_groups:
            for raw_key, binding in bindings.items():
                if not isinstance(binding, dict):
                    continue
                repo_id = self._binding_repo_id(binding)
                if not repo_id:
                    continue
                display = self._binding_display_name(
                    surface=surface,
                    raw_key=raw_key,
                    binding=binding,
                    display_by_key=display_by_key,
                )
                if not display:
                    continue
                self._append_unique_display(displays_by_repo, repo_id, display)
        return displays_by_repo

    @staticmethod
    def _channel_display_by_key(root: Path) -> dict[str, str]:
        try:
            entries = ChannelDirectoryStore(root).list_entries(limit=None)
        except Exception:
            return {}
        out: dict[str, str] = {}
        for entry in entries:
            key = channel_entry_key(entry)
            display = entry.get("display") if isinstance(entry, dict) else None
            if isinstance(key, str) and isinstance(display, str) and display.strip():
                out[key] = display.strip()
        return out

    @staticmethod
    def _binding_repo_id(binding: Mapping[str, Any]) -> Optional[str]:
        worktree_id = binding.get("worktree_id")
        if isinstance(worktree_id, str) and worktree_id.strip():
            return worktree_id.strip()
        resource_kind = str(binding.get("resource_kind") or "").strip().lower()
        resource_id = binding.get("resource_id")
        if (
            resource_kind in {"worktree", "repo"}
            and isinstance(resource_id, str)
            and resource_id.strip()
        ):
            return resource_id.strip()
        repo_id = binding.get("repo_id")
        if isinstance(repo_id, str) and repo_id.strip():
            return repo_id.strip()
        return None

    @staticmethod
    def _append_unique_display(
        displays_by_repo: dict[str, list[str]], repo_id: str, display: str
    ) -> None:
        existing = displays_by_repo.setdefault(repo_id, [])
        if display not in existing:
            existing.append(display)

    @staticmethod
    def _binding_display_name(
        *,
        surface: str,
        raw_key: str,
        binding: Mapping[str, Any],
        display_by_key: Mapping[str, str],
    ) -> Optional[str]:
        candidates: list[str] = []
        for key in (raw_key, binding.get("surface_key")):
            if isinstance(key, str) and key.strip():
                candidates.append(key.strip())
        if surface == "discord":
            chat_id = binding.get("chat_id")
            guild_id = binding.get("guild_id")
            if isinstance(chat_id, str) and chat_id.strip():
                candidates.append(f"discord:{chat_id.strip()}")
                if isinstance(guild_id, str) and guild_id.strip():
                    candidates.append(f"discord:{chat_id.strip()}:{guild_id.strip()}")
        elif surface == "telegram":
            chat_id = binding.get("chat_id")
            thread_id = binding.get("thread_id")
            if isinstance(chat_id, str) and chat_id.strip():
                if isinstance(thread_id, str) and thread_id.strip():
                    candidates.append(f"telegram:{chat_id.strip()}:{thread_id.strip()}")
                candidates.append(f"telegram:{chat_id.strip()}")

        for candidate in candidates:
            display = display_by_key.get(candidate)
            if display:
                return display

        if surface == "discord":
            chat_id = binding.get("chat_id")
            guild_id = binding.get("guild_id")
            if not isinstance(chat_id, str) or not chat_id.strip():
                body = raw_key.removeprefix("discord:")
                chat_id = body.split(":", 1)[0]
            if isinstance(guild_id, str) and guild_id.strip():
                return f"guild:{guild_id.strip()} / #{chat_id.strip()}"
            return f"discord:{chat_id.strip()}" if chat_id.strip() else None
        if surface == "telegram":
            if raw_key.startswith("telegram:"):
                return raw_key
            chat_id = binding.get("chat_id")
            thread_id = binding.get("thread_id")
            if isinstance(chat_id, str) and chat_id.strip():
                if isinstance(thread_id, str) and thread_id.strip():
                    return f"telegram:{chat_id.strip()}:{thread_id.strip()}"
                return f"telegram:{chat_id.strip()}"
        return None

    async def _enriched_repos(self) -> list[dict[str, Any]]:
        snapshots = list(
            await asyncio.to_thread(self._context.supervisor.list_repos, use_cache=True)
        )
        await self._mount_manager.refresh_mounts(snapshots)
        chat_binding_counts_by_source = await asyncio.to_thread(
            self._active_chat_binding_counts_by_source
        )
        chat_binding_counts = {
            workspace_id: sum(source_counts.values())
            for workspace_id, source_counts in chat_binding_counts_by_source.items()
        }
        display_names_by_repo = await asyncio.to_thread(
            self._chat_binding_display_names_by_repo, snapshots
        )
        enriched = await asyncio.gather(
            *[
                asyncio.to_thread(
                    self._enricher.enrich_repo,
                    snapshot,
                    chat_binding_counts,
                    chat_binding_counts_by_source,
                )
                for snapshot in snapshots
            ]
        )
        for item in enriched:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            sources = _positive_source_counts(
                chat_binding_counts_by_source.get(item_id, {})
            )
            if sources:
                item["chat_binding_sources"] = sources
            displays = display_names_by_repo.get(item_id, [])
            if displays:
                item["chat_binding_display_names"] = displays
        return enriched

    def _snapshot_by_id(self, repo_id: str) -> Any:
        for snapshot in self._context.supervisor.list_repos(use_cache=True):
            if str(getattr(snapshot, "id", "")) == repo_id:
                return snapshot
        raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")

    async def topology(
        self, *, kind: str, limit: int, cursor: Optional[str]
    ) -> dict[str, Any]:
        bounded = _bounded_limit(limit)
        offset = _offset_cursor(cursor)
        enriched = await self._enriched_repos()
        repos_by_id = {
            str(item.get("id")): item
            for item in enriched
            if str(item.get("kind") or "") != "worktree"
        }
        child_ids: dict[str, list[str]] = {repo_id: [] for repo_id in repos_by_id}
        for item in enriched:
            if str(item.get("kind") or "") == "worktree" and item.get("worktree_of"):
                child_ids.setdefault(str(item.get("worktree_of")), []).append(
                    str(item.get("id"))
                )
        repo_rows = [
            RepoTopology(
                repo_id=str(item.get("id")),
                label=str(item.get("name") or item.get("id")),
                path=str(item.get("path") or ""),
                archived=bool(item.get("archived")),
                is_pinned=bool(item.get("is_pinned")),
                destination_id=(
                    item.get("destination_id")
                    if isinstance(item.get("destination_id"), str)
                    else None
                ),
                child_worktree_ids=sorted(child_ids.get(str(item.get("id")), [])),
                worktree_setup_commands=_topology_worktree_setup_commands(item),
                chat_bound=bool(item.get("chat_bound")),
                chat_binding_count=_chat_binding_count(item),
                chat_binding_sources=_chat_binding_sources(item),
                chat_binding_display_names=_chat_binding_display_names(item),
            )
            for item in enriched
            if str(item.get("kind") or "") != "worktree" and kind in {"all", "repo"}
        ]
        worktree_rows = [
            WorktreeTopology(
                worktree_id=str(item.get("id")),
                repo_id=str(item.get("worktree_of") or ""),
                label=str(item.get("name") or item.get("branch") or item.get("id")),
                path=str(item.get("path") or ""),
                branch=str(item.get("branch")) if item.get("branch") else None,
                archived=bool(item.get("archived")),
                destination_id=(
                    item.get("destination_id")
                    if isinstance(item.get("destination_id"), str)
                    else None
                ),
                chat_bound=bool(item.get("chat_bound")),
                chat_binding_count=_chat_binding_count(item),
                chat_binding_sources=_chat_binding_sources(item),
                chat_binding_display_names=_chat_binding_display_names(item),
            )
            for item in enriched
            if str(item.get("kind") or "") == "worktree" and kind in {"all", "worktree"}
        ]
        combined_total = len(repo_rows) + len(worktree_rows)
        if kind == "repo":
            repo_rows = repo_rows[offset : offset + bounded]
            worktree_rows = []
        elif kind == "worktree":
            worktree_rows = worktree_rows[offset : offset + bounded]
            repo_rows = []
        else:
            combined: list[tuple[str, RepoTopology | WorktreeTopology]] = [
                ("repo", row) for row in repo_rows
            ] + [("worktree", row) for row in worktree_rows]
            selected = combined[offset : offset + bounded]
            repo_rows = [
                cast(RepoTopology, row)
                for row_kind, row in selected
                if row_kind == "repo"
            ]
            worktree_rows = [
                cast(WorktreeTopology, row)
                for row_kind, row in selected
                if row_kind == "worktree"
            ]
        snapshot = RepoWorktreeTopologySnapshot(
            cursor=_cursor("repo_worktree.topology"),
            window=_window(offset=offset, limit=bounded, total=combined_total),
            repos=repo_rows,
            worktrees=worktree_rows,
            repair=RepairPolicy(
                snapshot_route="/hub/read-models/repo-worktree/topology"
            ),
        )
        return dump_read_model_contract(snapshot)

    async def runtime(
        self, *, kind: str, limit: int, cursor: Optional[str]
    ) -> dict[str, Any]:
        bounded = _bounded_limit(limit)
        offset = _offset_cursor(cursor)
        enriched = await self._enriched_repos()
        runtime = [
            _runtime_projection(item)
            for item in enriched
            if kind == "all"
            or (kind == "repo" and str(item.get("kind") or "") != "worktree")
            or (kind == "worktree" and str(item.get("kind") or "") == "worktree")
        ]
        snapshot = RepoWorktreeRuntimeSnapshot(
            cursor=_cursor("repo_worktree.runtime"),
            window=_window(offset=offset, limit=bounded, total=len(runtime)),
            runtime=runtime[offset : offset + bounded],
            repair=RepairPolicy(
                snapshot_route="/hub/read-models/repo-worktree/runtime"
            ),
        )
        return dump_read_model_contract(snapshot)

    def _scoped_tickets(self, snapshot: Any, *, limit: int) -> list[dict[str, Any]]:
        from ....core.flows.store import FlowStore
        from ....core.pma_context import get_latest_ticket_flow_run_state_with_record

        workspace_kind = "worktree" if snapshot.kind == "worktree" else "repo"
        workspace_root = snapshot.path.resolve()
        state_root = resolve_repo_state_root(workspace_root)
        ticket_dir = state_root / "tickets"
        if not ticket_dir.exists():
            return []
        repo_id = snapshot.worktree_of if workspace_kind == "worktree" else snapshot.id
        worktree_id = snapshot.id if workspace_kind == "worktree" else None
        store = None
        run_state: Any = None
        run_record: Any = None
        db_path = resolve_repo_flows_db_path(workspace_root)
        if db_path.exists():
            try:
                store = FlowStore.connect_readonly(db_path)
                store.initialize()
                run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                    workspace_root,
                    snapshot.id,
                    store=store,
                )
            except Exception:
                store = None
        try:
            payloads = [
                payload
                for path in list_ticket_paths(ticket_dir)
                if (
                    payload := ticket_payload(
                        hub_root=self._context.config.root,
                        workspace_root=workspace_root,
                        ticket_dir=ticket_dir,
                        workspace_kind=workspace_kind,
                        workspace_id=snapshot.id,
                        repo_id=repo_id,
                        worktree_id=worktree_id,
                        path=path,
                    )
                )
            ]
            if run_record is not None:
                for payload in payloads:
                    enrich_current_ticket_payload(
                        payload, run_state=run_state, run_record=run_record
                    )
            mark_duplicate_ticket_numbers(payloads)
            payloads.sort(key=lambda item: _int_value(item.get("ticket_number")))
            return payloads[:limit]
        finally:
            if store is not None:
                store.close()

    def _scoped_runs(self, workspace_root: Path, *, limit: int) -> list[dict[str, Any]]:
        records = flow_store_service.safe_list_flow_runs(
            workspace_root,
            flow_type="ticket_flow",
            recover_stuck=True,
            logger=self._context.logger,
        )
        repo_id = str(getattr(self._snapshot_by_path(workspace_root), "id", "") or "")
        if repo_id:
            projected = project_ticket_flow_run_records(
                self._context.config.root,
                workspace_root,
                repo_id,
                records,
            )
            projected_by_id = {
                str(row.get("run_id") or row.get("flow_run_id")): row
                for row in projected
            }
        else:
            projected_by_id = {}
        return [
            _run_payload(record, projected_by_id.get(str(getattr(record, "id", ""))))
            for record in records[:limit]
        ]

    def _snapshot_by_path(self, workspace_root: Path) -> Any:
        resolved = workspace_root.resolve()
        for snapshot in self._context.supervisor.list_repos(use_cache=True):
            try:
                if snapshot.path.resolve() == resolved:
                    return snapshot
            except OSError:
                continue
        return None

    def _scoped_chats(
        self, *, owner_kind: str, owner_id: str, limit: int
    ) -> list[dict[str, Any]]:
        db_rows: list[dict[str, Any]] = []
        try:
            self._repair_chat_resource_owners_from_topology()
            with open_orchestration_sqlite(
                self._context.config.root, durable=True, migrate=True
            ) as conn:
                rows = conn.execute(
                    """
                    SELECT thread_target_id, agent_id, repo_id, resource_kind, resource_id,
                           display_name, lifecycle_status, runtime_status, metadata_json,
                           created_at, updated_at
                    FROM orch_thread_targets
                    WHERE resource_kind = ? AND resource_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (owner_kind, owner_id, limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        for row in rows:
            metadata: dict[str, Any] = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            db_rows.append(
                {
                    "thread_target_id": row["thread_target_id"],
                    "managed_thread_id": row["thread_target_id"],
                    "agent_id": row["agent_id"],
                    "repo_id": row["repo_id"],
                    "resource_kind": row["resource_kind"],
                    "resource_id": row["resource_id"],
                    "display_name": row["display_name"],
                    "title": row["display_name"],
                    "lifecycle_status": row["lifecycle_status"],
                    "runtime_status": row["runtime_status"],
                    "status": row["runtime_status"],
                    "chat_kind": metadata.get("chat_kind"),
                    "thread_kind": metadata.get("thread_kind"),
                    "model": metadata.get("model"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return db_rows

    def _repair_chat_resource_owners_from_topology(self) -> None:
        """Normalize legacy repo-shaped rows whose id is now known as a worktree."""

        snapshots = self._context.supervisor.list_repos(use_cache=True)
        scope_index = workspace_scope_index(snapshots)
        try:
            with open_orchestration_sqlite(
                self._context.config.root, durable=True, migrate=True
            ) as conn:
                rows = conn.execute(
                    """
                    SELECT thread_target_id, repo_id, resource_kind, resource_id,
                           workspace_root
                    FROM orch_thread_targets
                    WHERE resource_kind = 'repo'
                    """
                ).fetchall()
                for row in rows:
                    resolution = scope_index.resolve(
                        raw_repo_id=row["repo_id"],
                        workspace_path=row["workspace_root"],
                        resource_kind=row["resource_kind"],
                        resource_id=row["resource_id"],
                    )
                    if resolution is None or resolution.scope.kind != "worktree":
                        continue
                    owner = resolution.owner_fields()
                    conn.execute(
                        """
                        UPDATE orch_thread_targets
                           SET repo_id = ?,
                               resource_kind = ?,
                               resource_id = ?
                         WHERE thread_target_id = ?
                        """,
                        (
                            owner.get("repo_id"),
                            owner.get("resource_kind"),
                            owner.get("resource_id"),
                            row["thread_target_id"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE orch_bindings
                           SET repo_id = ?,
                               resource_kind = ?,
                               resource_id = ?
                         WHERE target_kind = 'thread'
                           AND target_id = ?
                        """,
                        (
                            owner.get("repo_id"),
                            owner.get("resource_kind"),
                            owner.get("resource_id"),
                            row["thread_target_id"],
                        ),
                    )
        except sqlite3.Error:
            return

    def _contextspace_summary(self, workspace_root: Path) -> list[dict[str, Any]]:
        docs = read_contextspace_docs(workspace_root)
        return [
            {
                "kind": kind,
                "name": f"{kind}.md",
                "content": content,
                "updated_at": iso_now(),
                "is_pinned": True,
            }
            for kind, content in docs.items()
        ]

    async def _child_worktrees(self, repo_id: str) -> list[dict[str, Any]]:
        snapshots = await asyncio.to_thread(
            self._context.supervisor.list_repos, use_cache=True
        )
        chat_binding_counts_by_source = await asyncio.to_thread(
            self._active_chat_binding_counts_by_source
        )
        chat_binding_counts = {
            workspace_id: sum(source_counts.values())
            for workspace_id, source_counts in chat_binding_counts_by_source.items()
        }
        display_names_by_repo = await asyncio.to_thread(
            self._chat_binding_display_names_by_repo, snapshots
        )
        children = [
            snapshot
            for snapshot in snapshots
            if str(getattr(snapshot, "kind", "") or "") == "worktree"
            and str(getattr(snapshot, "worktree_of", "") or "") == repo_id
        ]
        if not children:
            return []
        enriched = await asyncio.gather(
            *[
                asyncio.to_thread(
                    self._enricher.enrich_repo,
                    snapshot,
                    chat_binding_counts,
                    chat_binding_counts_by_source,
                )
                for snapshot in children
            ]
        )
        out: list[dict[str, Any]] = []
        for item in enriched:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            sources = _positive_source_counts(
                chat_binding_counts_by_source.get(item_id, {})
            )
            if sources:
                item["chat_binding_sources"] = sources
            displays = display_names_by_repo.get(item_id, [])
            if displays:
                item["chat_binding_display_names"] = displays
            out.append(item)
        return out

    async def detail(
        self,
        *,
        owner_kind: Literal["repo", "worktree"],
        owner_id: str,
        ticket_limit: int,
        run_limit: int,
        chat_limit: int,
        artifact_limit: int,
    ) -> dict[str, Any]:
        snapshot_obj = self._snapshot_by_id(owner_id)
        if owner_kind == "repo" and snapshot_obj.kind == "worktree":
            raise HTTPException(status_code=404, detail=f"Repo not found: {owner_id}")
        if owner_kind == "worktree" and snapshot_obj.kind != "worktree":
            raise HTTPException(
                status_code=404, detail=f"Worktree not found: {owner_id}"
            )
        enriched = await asyncio.to_thread(self._enricher.enrich_repo, snapshot_obj)
        ticket_limit = _bounded_limit(ticket_limit, maximum=100)
        run_limit = _bounded_limit(run_limit, maximum=100)
        chat_limit = _bounded_limit(chat_limit, maximum=100)
        artifact_limit = _bounded_limit(artifact_limit, maximum=100)
        tickets_task = asyncio.to_thread(
            self._scoped_tickets, snapshot_obj, limit=ticket_limit
        )
        runs_task = asyncio.to_thread(
            self._scoped_runs, snapshot_obj.path, limit=run_limit
        )
        chats_task = asyncio.to_thread(
            self._scoped_chats,
            owner_kind=owner_kind,
            owner_id=owner_id,
            limit=chat_limit,
        )
        contextspace_task = asyncio.to_thread(
            self._contextspace_summary, snapshot_obj.path
        )
        children_task = (
            self._child_worktrees(owner_id) if owner_kind == "repo" else _empty_list()
        )
        tickets, runs, chats, contextspace, children = await asyncio.gather(
            tickets_task,
            runs_task,
            chats_task,
            contextspace_task,
            children_task,
        )
        artifacts = list(enriched.get("current_run_artifacts") or [])[:artifact_limit]
        parent_links: dict[str, Any] = {}
        if owner_kind == "worktree":
            parent_links["repo_id"] = getattr(snapshot_obj, "worktree_of", None)
        detail = RepoWorktreeDetailSnapshot(
            cursor=_cursor(f"{owner_kind}.detail"),
            owner_kind=owner_kind,
            owner_id=owner_id,
            identity=enriched,
            parent_links=parent_links,
            topology={"children": children},
            runtime=_runtime_projection(enriched).model_dump(mode="json"),
            ticket_queue=tickets,
            run_queue=runs,
            chat_queue=chats,
            contextspace_summary=contextspace,
            current_artifacts=artifacts,
            ticket_window=_window(offset=0, limit=ticket_limit, total=len(tickets)),
            run_window=_window(offset=0, limit=run_limit, total=len(runs)),
            chat_window=_window(offset=0, limit=chat_limit, total=len(chats)),
            artifact_window=_window(
                offset=0, limit=artifact_limit, total=len(artifacts)
            ),
            repair=RepairPolicy(
                snapshot_route=f"/hub/read-models/{owner_kind}s/{owner_id}/detail"
            ),
        )
        payload = dump_read_model_contract(detail)
        payload.update(
            _repo_worktree_detail_compatibility_fields(
                scoped_tickets=tickets,
                scoped_runs=runs,
                scoped_chats=chats,
            )
        )
        return payload

    async def ticket_detail(
        self,
        *,
        owner_kind: Literal["repo", "worktree"],
        owner_id: str,
        ticket_id: str,
    ) -> dict[str, Any]:
        snapshot_obj = self._snapshot_by_id(owner_id)
        tickets = self._scoped_tickets(snapshot_obj, limit=500)
        selected = next(
            (
                ticket
                for ticket in tickets
                if ticket_id
                in {
                    str(ticket.get("id")),
                    str(ticket.get("ticket_id")),
                    str(ticket.get("index")),
                    Path(
                        str(ticket.get("ticket_path") or ticket.get("path") or "")
                    ).name,
                    Path(
                        str(ticket.get("ticket_path") or ticket.get("path") or "")
                    ).stem,
                }
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=404, detail=f"Ticket not found: {ticket_id}"
            )
        index = tickets.index(selected)

        def _ticket_projection(ticket: dict[str, Any]) -> TicketProjection:
            fm = _dict_value(ticket.get("frontmatter"))
            return TicketProjection(
                ticket_id=str(ticket.get("id") or ticket.get("ticket_id")),
                route_id=str(ticket.get("index") or ticket.get("ticket_id")),
                title=str(fm.get("title") or ticket.get("title") or ticket.get("id")),
                status=_ticket_projection_status(ticket.get("status")),  # type: ignore[arg-type]
                owner_kind=owner_kind,
                owner_id=owner_id,
                agent=str(fm.get("agent")) if fm.get("agent") else None,
                model=str(fm.get("model")) if fm.get("model") else None,
                done=bool(fm.get("done") or ticket.get("done")),
            )

        siblings = []
        for sibling_index in range(max(0, index - 5), min(len(tickets), index + 6)):
            ticket = tickets[sibling_index]
            fm = _dict_value(ticket.get("frontmatter"))
            siblings.append(
                TicketQueueSibling(
                    ticket_id=str(ticket.get("id") or ticket.get("ticket_id")),
                    route_id=str(ticket.get("index") or ticket.get("ticket_id")),
                    title=str(
                        fm.get("title") or ticket.get("title") or ticket.get("id")
                    ),
                    status=str(ticket.get("status") or "queued"),
                    previous_ticket_id=(
                        str(tickets[sibling_index - 1].get("id"))
                        if sibling_index > 0
                        else None
                    ),
                    next_ticket_id=(
                        str(tickets[sibling_index + 1].get("id"))
                        if sibling_index + 1 < len(tickets)
                        else None
                    ),
                )
            )
        chats = self._scoped_chats(owner_kind=owner_kind, owner_id=owner_id, limit=50)
        run_id = selected.get("run_id")
        runs = self._scoped_runs(snapshot_obj.path, limit=50)
        linked_run_payload = next(
            (
                run
                for run in runs
                if run.get("id") == run_id or run.get("run_id") == run_id
            ),
            None,
        )
        linked_run = (
            RunProjection(
                run_id=str(
                    linked_run_payload.get("run_id") or linked_run_payload.get("id")
                ),
                status=str(linked_run_payload.get("status") or "unknown"),
                started_at=(
                    linked_run_payload.get("started_at")
                    if isinstance(linked_run_payload.get("started_at"), str)
                    else None
                ),
                finished_at=(
                    linked_run_payload.get("finished_at")
                    if isinstance(linked_run_payload.get("finished_at"), str)
                    else None
                ),
                worker_activity=(
                    linked_run_payload.get("current_step")
                    if isinstance(linked_run_payload.get("current_step"), str)
                    else None
                ),
            )
            if linked_run_payload is not None
            else None
        )
        dispatches: list[dict[str, Any]] = []
        if linked_run is not None:
            try:
                history = get_dispatch_history(
                    snapshot_obj.path, linked_run.run_id, "ticket_flow"
                )
                raw_history = history.get("history")
                if isinstance(raw_history, list):
                    dispatches = [
                        item for item in raw_history[:25] if isinstance(item, dict)
                    ]
            except Exception:
                dispatches = []
        detail = TicketDetailSnapshot(
            cursor=_cursor("ticket.detail"),
            ticket=_ticket_projection(selected),
            ticket_detail=selected,
            ticket_queue=tickets,
            run_queue=runs,
            chat_queue=chats,
            siblings=siblings,
            linked_run=linked_run,
            linked_chats=[],
            artifacts=[],
            dispatch_window=_window(offset=0, limit=25, total=len(dispatches)),
            dispatches=dispatches,
            repair=RepairPolicy(snapshot_route=f"/hub/read-models/tickets/{ticket_id}"),
        )
        payload = dump_read_model_contract(detail)
        payload.update(
            _ticket_detail_compatibility_fields(
                selected_ticket=selected,
                scoped_tickets=tickets,
                scoped_runs=runs,
                scoped_chats=chats,
            )
        )
        return payload


__all__ = ["RepoWorktreeReadModelService"]
