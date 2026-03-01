from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Body, FastAPI, HTTPException
from starlette.routing import Mount
from starlette.types import ASGIApp

from ....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
    active_chat_binding_counts,
)
from ....core.config import ConfigError
from ....core.destinations import resolve_effective_repo_destination
from ....core.flows import FlowEventType, FlowStore
from ....core.git_utils import git_is_clean
from ....core.logging_utils import safe_log
from ....core.pma_context import (
    get_latest_ticket_flow_run_state_with_record,
)
from ....core.request_context import get_request_id
from ....core.runtime import LockError
from ....core.ticket_flow_projection import build_canonical_state_v1
from ....core.ticket_flow_summary import (
    build_ticket_flow_display,
    build_ticket_flow_summary,
)
from ....integrations.app_server.threads import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from ....integrations.chat.channel_directory import (
    ChannelDirectoryStore,
    channel_entry_key,
)
from ....integrations.telegram.state import topic_key
from ....manifest import load_manifest, normalize_manifest_destination, save_manifest
from ..app_state import HubAppContext
from ..schemas import (
    HubArchiveWorktreeRequest,
    HubArchiveWorktreeResponse,
    HubCleanupWorktreeRequest,
    HubCreateRepoRequest,
    HubCreateWorktreeRequest,
    HubDestinationSetRequest,
    HubJobResponse,
    HubPinRepoRequest,
    HubRemoveRepoRequest,
    RunControlRequest,
)


class HubMountManager:
    def __init__(
        self,
        app: FastAPI,
        context: HubAppContext,
        build_repo_app: Callable[[Path], ASGIApp],
    ) -> None:
        self.app = app
        self.context = context
        self._build_repo_app = build_repo_app

        self._mounted_repos: set[str] = set()
        self._mount_errors: dict[str, str] = {}
        self._repo_apps: dict[str, ASGIApp] = {}
        self._repo_lifespans: dict[str, object] = {}
        self._mount_order: list[str] = []
        self._mount_lock: Optional[asyncio.Lock] = None

    async def _get_mount_lock(self) -> asyncio.Lock:
        if self._mount_lock is None:
            self._mount_lock = asyncio.Lock()
        return self._mount_lock

    @staticmethod
    def _unwrap_fastapi(sub_app: ASGIApp) -> Optional[FastAPI]:
        current: ASGIApp = sub_app
        while not isinstance(current, FastAPI):
            nested = getattr(current, "app", None)
            if nested is None:
                return None
            current = nested
        return current

    async def _start_repo_lifespan_locked(self, prefix: str, sub_app: ASGIApp) -> None:
        if prefix in self._repo_lifespans:
            return
        fastapi_app = self._unwrap_fastapi(sub_app)
        if fastapi_app is None:
            return
        try:
            ctx = fastapi_app.router.lifespan_context(fastapi_app)
            await ctx.__aenter__()
            self._repo_lifespans[prefix] = ctx
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan entered for {prefix}",
            )
        except Exception as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning(
                    "Repo lifespan failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan failure for {prefix}",
                    exc=exc2,
                )
            await self._unmount_repo_locked(prefix)

    async def _stop_repo_lifespan_locked(self, prefix: str) -> None:
        ctx = self._repo_lifespans.pop(prefix, None)
        if ctx is None:
            return
        try:
            await ctx.__aexit__(None, None, None)  # type: ignore[attr-defined]
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan exited for {prefix}",
            )
        except Exception as exc:
            try:
                self.app.state.logger.warning(
                    "Repo lifespan shutdown failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan shutdown failure for {prefix}",
                    exc=exc2,
                )

    def _detach_mount_locked(self, prefix: str) -> None:
        mount_path = f"/repos/{prefix}"
        self.app.router.routes = [
            route
            for route in self.app.router.routes
            if not (isinstance(route, Mount) and route.path == mount_path)
        ]
        self._mounted_repos.discard(prefix)
        self._repo_apps.pop(prefix, None)
        if prefix in self._mount_order:
            self._mount_order.remove(prefix)

    async def _unmount_repo_locked(self, prefix: str) -> None:
        await self._stop_repo_lifespan_locked(prefix)
        self._detach_mount_locked(prefix)

    def _mount_repo_sync(self, prefix: str, repo_path: Path) -> bool:
        if prefix in self._mounted_repos:
            return True
        if prefix in self._mount_errors:
            return False
        try:
            sub_app = self._build_repo_app(repo_path)
        except ConfigError as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False
        except Exception as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False

        fastapi_app = self._unwrap_fastapi(sub_app)
        if fastapi_app is not None:
            fastapi_app.state.repo_id = prefix

        self.app.mount(f"/repos/{prefix}", sub_app)
        self._mounted_repos.add(prefix)
        self._repo_apps[prefix] = sub_app
        if prefix not in self._mount_order:
            self._mount_order.append(prefix)
        self._mount_errors.pop(prefix, None)
        return True

    def mount_initial(self, snapshots: Iterable[Any]) -> None:
        for snapshot in snapshots:
            if getattr(snapshot, "initialized", False) and getattr(
                snapshot, "exists_on_disk", False
            ):
                self._mount_repo_sync(snapshot.id, snapshot.path)

    async def refresh_mounts(
        self, snapshots: Iterable[Any], *, full_refresh: bool = True
    ):
        desired = {
            snapshot.id
            for snapshot in snapshots
            if getattr(snapshot, "initialized", False)
            and getattr(snapshot, "exists_on_disk", False)
        }
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            if full_refresh:
                for prefix in list(self._mounted_repos):
                    if prefix not in desired:
                        await self._unmount_repo_locked(prefix)
                for prefix in list(self._mount_errors):
                    if prefix not in desired:
                        self._mount_errors.pop(prefix, None)

            for snapshot in snapshots:
                if snapshot.id not in desired:
                    continue
                if (
                    snapshot.id in self._mounted_repos
                    or snapshot.id in self._mount_errors
                ):
                    continue
                if not self._mount_repo_sync(snapshot.id, snapshot.path):
                    continue
                fastapi_app = self._unwrap_fastapi(self._repo_apps[snapshot.id])
                if fastapi_app is not None:
                    fastapi_app.state.repo_id = snapshot.id
                if self.app.state.hub_started:
                    await self._start_repo_lifespan_locked(
                        snapshot.id, self._repo_apps[snapshot.id]
                    )

    async def start_repo_lifespans(self) -> None:
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            for prefix in list(self._mount_order):
                sub_app = self._repo_apps.get(prefix)
                if sub_app is not None:
                    await self._start_repo_lifespan_locked(prefix, sub_app)

    async def stop_repo_mounts(self) -> None:
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            for prefix in list(reversed(self._mount_order)):
                await self._stop_repo_lifespan_locked(prefix)
            for prefix in list(self._mounted_repos):
                self._detach_mount_locked(prefix)

    def add_mount_info(self, repo_dict: dict) -> dict:
        repo_id = repo_dict.get("id")
        if repo_id in self._mount_errors:
            repo_dict["mounted"] = False
            repo_dict["mount_error"] = self._mount_errors[repo_id]
        elif repo_id in self._mounted_repos:
            repo_dict["mounted"] = True
            if "mount_error" in repo_dict:
                repo_dict.pop("mount_error", None)
        else:
            repo_dict["mounted"] = False
            if "mount_error" in repo_dict:
                repo_dict.pop("mount_error", None)
        return repo_dict


def build_hub_repo_routes(
    context: HubAppContext,
    mount_manager: HubMountManager,
) -> APIRouter:
    router = APIRouter()

    def _active_chat_binding_counts() -> dict[str, int]:
        try:
            return active_chat_binding_counts(
                hub_root=context.config.root,
                raw_config=context.config.raw,
            )
        except Exception as exc:
            safe_log(
                context.logger,
                logging.WARNING,
                "Hub active chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    def _enrich_repo(
        snapshot, chat_binding_counts: Optional[dict[str, int]] = None
    ) -> dict:
        repo_dict = snapshot.to_dict(context.config.root)
        repo_dict = mount_manager.add_mount_info(repo_dict)
        binding_count = int((chat_binding_counts or {}).get(snapshot.id, 0))
        repo_dict["chat_bound"] = binding_count > 0
        repo_dict["chat_bound_thread_count"] = binding_count
        if snapshot.initialized and snapshot.exists_on_disk:
            ticket_flow = _get_ticket_flow_summary(snapshot.path)
            repo_dict["ticket_flow"] = ticket_flow
            if isinstance(ticket_flow, dict):
                repo_dict["ticket_flow_display"] = build_ticket_flow_display(
                    status=(
                        str(ticket_flow.get("status"))
                        if ticket_flow.get("status") is not None
                        else None
                    ),
                    done_count=int(ticket_flow.get("done_count") or 0),
                    total_count=int(ticket_flow.get("total_count") or 0),
                    run_id=(
                        str(ticket_flow.get("run_id"))
                        if ticket_flow.get("run_id")
                        else None
                    ),
                )
            else:
                repo_dict["ticket_flow_display"] = build_ticket_flow_display(
                    status=None,
                    done_count=0,
                    total_count=0,
                    run_id=None,
                )
            run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                snapshot.path, snapshot.id
            )
            repo_dict["run_state"] = run_state
            repo_dict["canonical_state_v1"] = build_canonical_state_v1(
                repo_root=snapshot.path,
                repo_id=snapshot.id,
                run_state=repo_dict["run_state"],
                record=run_record,
                preferred_run_id=(
                    str(snapshot.last_run_id)
                    if snapshot.last_run_id is not None
                    else None
                ),
            )
        else:
            repo_dict["ticket_flow"] = None
            repo_dict["ticket_flow_display"] = None
            repo_dict["run_state"] = None
            repo_dict["canonical_state_v1"] = None
        return repo_dict

    def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict]:
        return build_ticket_flow_summary(repo_path, include_failure=True)

    def _resolve_manifest_repo(repo_id: str):
        manifest = load_manifest(context.config.manifest_path, context.config.root)
        repos_by_id = {entry.id: entry for entry in manifest.repos}
        repo = repos_by_id.get(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"Repo not found: {repo_id}")
        return manifest, repos_by_id, repo

    def _destination_payload(repo, repos_by_id: dict[str, Any]) -> dict[str, Any]:
        resolution = resolve_effective_repo_destination(repo, repos_by_id)
        return {
            "repo_id": repo.id,
            "kind": repo.kind,
            "worktree_of": repo.worktree_of,
            "configured_destination": repo.destination,
            "effective_destination": resolution.to_dict(),
            "source": resolution.source,
            "issues": list(resolution.issues),
        }

    def _normalize_agent(value: Any) -> str:
        if isinstance(value, str) and value.strip().lower() == "opencode":
            return "opencode"
        return "codex"

    def _normalize_scope(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _coerce_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

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
            except Exception:
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
            except Exception:
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
                binding = {
                    "platform": "discord",
                    "chat_id": channel_id.strip(),
                    "workspace_path": workspace_path,
                    "repo_id": repo_id,
                    "pma_enabled": (
                        bool(row["pma_enabled"]) if "pma_enabled" in columns else False
                    ),
                    "agent": _normalize_agent(
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
                except Exception:
                    pass
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
                agent = _normalize_agent(payload.get("agent"))
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
                        "active_thread_id": active_thread_id,
                    },
                )
        except Exception as exc:
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
                except Exception:
                    pass
        return bindings

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
        except Exception:
            return {}

        for payload in by_session.values():
            payload.pop("__index", None)
        return by_session

    def _build_registry_key(
        entry: dict[str, Any],
        binding: dict[str, Any],
        *,
        telegram_require_topics: bool,
    ) -> Optional[str]:
        platform = str(binding.get("platform") or "").strip().lower()
        agent = _normalize_agent(binding.get("agent"))
        pma_enabled = bool(binding.get("pma_enabled"))
        if pma_enabled:
            base_key = PMA_OPENCODE_KEY if agent == "opencode" else PMA_KEY
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
            return f"{base_key}.{topic_key(chat_id, thread_id)}"

        if platform != "discord":
            return None
        channel_id = binding.get("chat_id") or entry.get("chat_id")
        workspace_path = binding.get("workspace_path")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return None
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        digest = hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()[:12]
        prefix = FILE_CHAT_OPENCODE_PREFIX if agent == "opencode" else FILE_CHAT_PREFIX
        return f"{prefix}discord.{channel_id.strip()}.{digest}"

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
                        totals["insertions"] += _coerce_int(data.get("insertions"))
                        totals["deletions"] += _coerce_int(data.get("deletions"))
                        totals["files_changed"] += _coerce_int(
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

    @router.get("/hub/repos")
    async def list_repos():
        safe_log(context.logger, logging.INFO, "Hub list_repos")
        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        chat_binding_counts = await asyncio.to_thread(_active_chat_binding_counts)
        await mount_manager.refresh_mounts(snapshots)
        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": context.supervisor.state.pinned_parent_repo_ids,
            "repos": [_enrich_repo(snap, chat_binding_counts) for snap in snapshots],
        }

    @router.post("/hub/repos/scan")
    async def scan_repos():
        safe_log(context.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(context.supervisor.scan)
        chat_binding_counts = await asyncio.to_thread(_active_chat_binding_counts)
        await mount_manager.refresh_mounts(snapshots)

        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": context.supervisor.state.pinned_parent_repo_ids,
            "repos": [_enrich_repo(snap, chat_binding_counts) for snap in snapshots],
        }

    @router.post("/hub/jobs/scan", response_model=HubJobResponse)
    async def scan_repos_job():
        async def _run_scan():
            snapshots = await asyncio.to_thread(context.supervisor.scan)
            await mount_manager.refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.scan_repos", _run_scan, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/repos")
    async def create_repo(payload: HubCreateRepoRequest):
        git_url = payload.git_url
        repo_id = payload.repo_id
        if not repo_id and not git_url:
            raise HTTPException(status_code=400, detail="Missing repo id")
        repo_path_val = payload.path
        repo_path = Path(repo_path_val) if repo_path_val else None
        git_init = payload.git_init
        force = payload.force
        safe_log(
            context.logger,
            logging.INFO,
            "Hub create repo id=%s path=%s git_init=%s force=%s git_url=%s"
            % (repo_id, repo_path_val, git_init, force, bool(git_url)),
        )
        try:
            if git_url:
                snapshot = await asyncio.to_thread(
                    context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/jobs/repos", response_model=HubJobResponse)
    async def create_repo_job(payload: HubCreateRepoRequest):
        async def _run_create_repo():
            git_url = payload.git_url
            repo_id = payload.repo_id
            if not repo_id and not git_url:
                raise ValueError("Missing repo id")
            repo_path_val = payload.path
            repo_path = Path(repo_path_val) if repo_path_val else None
            git_init = payload.git_init
            force = payload.force
            if git_url:
                snapshot = await asyncio.to_thread(
                    context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
            await mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return _enrich_repo(snapshot)

        job = await context.job_manager.submit(
            "hub.create_repo", _run_create_repo, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/repos/{repo_id}/worktree-setup")
    async def set_worktree_setup(repo_id: str, payload: Annotated[Any, Body()] = None):
        if isinstance(payload, dict):
            commands_raw = payload.get("commands", [])
        elif isinstance(payload, list):
            # Backward compatibility for older clients that posted a raw array.
            commands_raw = payload
        elif payload is None:
            commands_raw = []
        else:
            raise HTTPException(
                status_code=400,
                detail="body must be an object with commands or a commands array",
            )
        if not isinstance(commands_raw, list):
            raise HTTPException(status_code=400, detail="commands must be a list")
        commands = [str(item) for item in commands_raw if str(item).strip()]
        safe_log(
            context.logger,
            logging.INFO,
            "Hub set worktree setup repo=%s commands=%d" % (repo_id, len(commands)),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.set_worktree_setup_commands,
                repo_id,
                commands,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/pin")
    async def pin_parent_repo(
        repo_id: str, payload: Optional[HubPinRepoRequest] = None
    ):
        requested = payload.pinned if payload else True
        safe_log(
            context.logger,
            logging.INFO,
            "Hub pin parent repo=%s pinned=%s" % (repo_id, requested),
        )
        try:
            pinned_parent_repo_ids = await asyncio.to_thread(
                context.supervisor.set_parent_repo_pinned,
                repo_id,
                requested,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "repo_id": repo_id,
            "pinned": requested,
            "pinned_parent_repo_ids": pinned_parent_repo_ids,
        }

    @router.get("/hub/repos/{repo_id}/destination")
    async def get_repo_destination(repo_id: str):
        safe_log(context.logger, logging.INFO, "Hub destination show repo=%s" % repo_id)
        manifest, repos_by_id, repo = await asyncio.to_thread(
            _resolve_manifest_repo, repo_id
        )
        return _destination_payload(repo, repos_by_id)

    @router.post("/hub/repos/{repo_id}/destination")
    async def set_repo_destination(repo_id: str, payload: HubDestinationSetRequest):
        normalized_kind = payload.kind.strip().lower()
        destination: dict[str, Any]
        if normalized_kind == "local":
            destination = {"kind": "local"}
        elif normalized_kind == "docker":
            image = (payload.image or "").strip()
            if not image:
                raise HTTPException(
                    status_code=400, detail="image is required for docker destination"
                )
            destination = {"kind": "docker", "image": image}
            container_name = (payload.container_name or "").strip()
            if container_name:
                destination["container_name"] = container_name
            profile = (payload.profile or "").strip()
            if profile:
                destination["profile"] = profile
            workdir = (payload.workdir or "").strip()
            if workdir:
                destination["workdir"] = workdir
            env_passthrough = [
                str(item).strip()
                for item in (payload.env_passthrough or [])
                if str(item).strip()
            ]
            if env_passthrough:
                destination["env_passthrough"] = env_passthrough
            explicit_env: dict[str, str] = {}
            for raw_key, raw_value in (payload.env or {}).items():
                key = str(raw_key or "").strip()
                if not key:
                    raise HTTPException(
                        status_code=400,
                        detail="Docker env keys must be non-empty strings",
                    )
                explicit_env[key] = str(raw_value)
            if explicit_env:
                destination["env"] = explicit_env
            mounts: list[dict[str, Any]] = []
            for item in payload.mounts or []:
                source = str((item or {}).get("source") or "").strip()
                target = str((item or {}).get("target") or "").strip()
                if not source or not target:
                    raise HTTPException(
                        status_code=400,
                        detail="Each mount requires non-empty source and target",
                    )
                mount_payload: dict[str, Any] = {"source": source, "target": target}
                read_only = (item or {}).get("read_only")
                if read_only is None and "readOnly" in (item or {}):
                    read_only = (item or {}).get("readOnly")
                if read_only is None and "readonly" in (item or {}):
                    read_only = (item or {}).get("readonly")
                if read_only is not None:
                    if not isinstance(read_only, bool):
                        raise HTTPException(
                            status_code=400,
                            detail="Mount read_only must be a boolean when provided",
                        )
                    mount_payload["read_only"] = read_only
                mounts.append(mount_payload)
            if mounts:
                destination["mounts"] = mounts
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported destination kind: {payload.kind!r}. "
                    "Use 'local' or 'docker'."
                ),
            )

        normalized_destination = normalize_manifest_destination(destination)
        if normalized_destination is None:
            raise HTTPException(
                status_code=400, detail=f"Invalid destination payload: {destination!r}"
            )

        manifest, repos_by_id, repo = await asyncio.to_thread(
            _resolve_manifest_repo, repo_id
        )
        repo.destination = normalized_destination
        await asyncio.to_thread(
            save_manifest,
            context.config.manifest_path,
            manifest,
            context.config.root,
        )

        snapshots = await asyncio.to_thread(
            context.supervisor.list_repos, use_cache=False
        )
        await mount_manager.refresh_mounts(snapshots)
        return _destination_payload(repo, repos_by_id)

    @router.get("/hub/chat/channels")
    async def list_chat_channels(query: Optional[str] = None, limit: int = 100):
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        if limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be <= 1000")

        store = ChannelDirectoryStore(context.config.root)
        entries = await asyncio.to_thread(store.list_entries, query=query, limit=limit)
        snapshots: list[Any] = []
        try:
            snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        except Exception as exc:
            safe_log(
                context.logger,
                logging.WARNING,
                "Hub channel enrichment failed listing repo snapshots",
                exc=exc,
            )
        repo_id_by_workspace = _repo_id_by_workspace_path(snapshots)
        discord_state_path = _state_db_path("discord_bot", DISCORD_STATE_FILE_DEFAULT)
        telegram_state_path = _state_db_path(
            "telegram_bot", TELEGRAM_STATE_FILE_DEFAULT
        )
        telegram_require_topics = _telegram_require_topics_enabled()
        discord_bindings_task = asyncio.to_thread(
            _read_discord_bindings, discord_state_path, repo_id_by_workspace
        )
        telegram_bindings_task = asyncio.to_thread(
            _read_telegram_bindings, telegram_state_path, repo_id_by_workspace
        )
        discord_bindings, telegram_bindings = await asyncio.gather(
            discord_bindings_task,
            telegram_bindings_task,
            return_exceptions=False,
        )
        run_cache: dict[str, dict[str, Any]] = {}
        usage_cache: dict[str, dict[str, dict[str, Any]]] = {}
        thread_map_cache: dict[str, dict[str, str]] = {}

        rows: list[dict[str, Any]] = []
        for entry in entries:
            key = channel_entry_key(entry)
            if not isinstance(key, str):
                continue
            row: dict[str, Any] = {
                "key": key,
                "display": entry.get("display"),
                "seen_at": entry.get("seen_at"),
                "meta": entry.get("meta"),
                "entry": entry,
            }
            platform = str(entry.get("platform") or "").strip().lower()
            binding_source = (
                discord_bindings if platform == "discord" else telegram_bindings
            )
            binding = binding_source.get(key)
            if isinstance(binding, dict):
                try:
                    repo_id = binding.get("repo_id")
                    if isinstance(repo_id, str) and repo_id:
                        row["repo_id"] = repo_id
                    workspace_path = binding.get("workspace_path")
                    if isinstance(workspace_path, str) and workspace_path:
                        row["workspace_path"] = workspace_path
                    pma_enabled = bool(binding.get("pma_enabled"))
                    active_thread_id: Optional[str] = None
                    if platform == "telegram" and not pma_enabled:
                        direct_thread = binding.get("active_thread_id")
                        if isinstance(direct_thread, str) and direct_thread:
                            active_thread_id = direct_thread
                    else:
                        registry_key = _build_registry_key(
                            entry,
                            binding,
                            telegram_require_topics=telegram_require_topics,
                        )
                        if registry_key:
                            resolved = _registry_thread_id(
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
                        run_data = _load_workspace_run_data(
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
                        if _is_working(
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
                            usage_by_session = _read_usage_by_session(workspace_path)
                            usage_cache[workspace_path] = usage_by_session
                        usage_payload = usage_by_session.get(active_thread_id)
                        if isinstance(usage_payload, dict):
                            row["token_usage"] = {
                                "total_tokens": _coerce_int(
                                    usage_payload.get("total_tokens")
                                ),
                                "input_tokens": _coerce_int(
                                    usage_payload.get("input_tokens")
                                ),
                                "cached_input_tokens": _coerce_int(
                                    usage_payload.get("cached_input_tokens")
                                ),
                                "output_tokens": _coerce_int(
                                    usage_payload.get("output_tokens")
                                ),
                                "reasoning_output_tokens": _coerce_int(
                                    usage_payload.get("reasoning_output_tokens")
                                ),
                                "turn_id": usage_payload.get("turn_id"),
                                "timestamp": usage_payload.get("timestamp"),
                            }
                except Exception as exc:
                    safe_log(
                        context.logger,
                        logging.WARNING,
                        f"Hub channel enrichment failed for {key}",
                        exc=exc,
                    )
            rows.append(row)
        return {"entries": rows}

    @router.get("/hub/repos/{repo_id}/remove-check")
    async def remove_repo_check(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub remove-check {repo_id}")
        try:
            return await asyncio.to_thread(
                context.supervisor.check_repo_removal, repo_id
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/repos/{repo_id}/remove")
    async def remove_repo(repo_id: str, payload: Optional[HubRemoveRepoRequest] = None):
        payload = payload or HubRemoveRepoRequest()
        force = payload.force
        delete_dir = payload.delete_dir
        delete_worktrees = payload.delete_worktrees
        safe_log(
            context.logger,
            logging.INFO,
            "Hub remove repo id=%s force=%s delete_dir=%s delete_worktrees=%s"
            % (repo_id, force, delete_dir, delete_worktrees),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.remove_repo,
                repo_id,
                force=force,
                delete_dir=delete_dir,
                delete_worktrees=delete_worktrees,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshots = await asyncio.to_thread(
            context.supervisor.list_repos, use_cache=False
        )
        await mount_manager.refresh_mounts(snapshots)
        return {"status": "ok"}

    @router.post("/hub/jobs/repos/{repo_id}/remove", response_model=HubJobResponse)
    async def remove_repo_job(
        repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ):
        payload = payload or HubRemoveRepoRequest()

        async def _run_remove_repo():
            await asyncio.to_thread(
                context.supervisor.remove_repo,
                repo_id,
                force=payload.force,
                delete_dir=payload.delete_dir,
                delete_worktrees=payload.delete_worktrees,
            )
            snapshots = await asyncio.to_thread(
                context.supervisor.list_repos, use_cache=False
            )
            await mount_manager.refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.remove_repo", _run_remove_repo, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/worktrees/create")
    async def create_worktree(payload: HubCreateWorktreeRequest):
        base_repo_id = payload.base_repo_id
        branch = payload.branch
        force = payload.force
        start_point = payload.start_point
        safe_log(
            context.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s start_point=%s"
            % (base_repo_id, branch, force, start_point),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(base_repo_id),
                branch=str(branch),
                force=force,
                start_point=str(start_point) if start_point else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/jobs/worktrees/create", response_model=HubJobResponse)
    async def create_worktree_job(payload: HubCreateWorktreeRequest):
        async def _run_create_worktree():
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(payload.base_repo_id),
                branch=str(payload.branch),
                force=payload.force,
                start_point=str(payload.start_point) if payload.start_point else None,
            )
            await mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return _enrich_repo(snapshot)

        job = await context.job_manager.submit(
            "hub.create_worktree", _run_create_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: HubCleanupWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        delete_branch = payload.delete_branch
        delete_remote = payload.delete_remote
        archive = payload.archive
        force = payload.force
        force_archive = payload.force_archive
        archive_note = payload.archive_note
        safe_log(
            context.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s archive=%s force=%s force_archive=%s"
            % (
                worktree_repo_id,
                delete_branch,
                delete_remote,
                archive,
                force,
                force_archive,
            ),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.cleanup_worktree,
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                archive=archive,
                force=force,
                force_archive=force_archive,
                archive_note=archive_note,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @router.post("/hub/jobs/worktrees/cleanup", response_model=HubJobResponse)
    async def cleanup_worktree_job(payload: HubCleanupWorktreeRequest):
        def _run_cleanup_worktree():
            context.supervisor.cleanup_worktree(
                worktree_repo_id=str(payload.worktree_repo_id),
                delete_branch=payload.delete_branch,
                delete_remote=payload.delete_remote,
                archive=payload.archive,
                force=payload.force,
                force_archive=payload.force_archive,
                archive_note=payload.archive_note,
            )
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.cleanup_worktree", _run_cleanup_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/worktrees/archive", response_model=HubArchiveWorktreeResponse)
    async def archive_worktree(payload: HubArchiveWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        archive_note = payload.archive_note
        safe_log(
            context.logger,
            logging.INFO,
            "Hub archive worktree id=%s" % (worktree_repo_id,),
        )
        try:
            result = await asyncio.to_thread(
                context.supervisor.archive_worktree,
                worktree_repo_id=str(worktree_repo_id),
                archive_note=archive_note,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @router.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            context.logger,
            logging.INFO,
            "Hub run %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.run_repo, repo_id, once=once
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub stop {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.stop_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/resume")
    async def resume_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            context.logger,
            logging.INFO,
            "Hub resume %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.resume_repo, repo_id, once=once
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub kill {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.kill_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub init {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.init_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/sync-main")
    async def sync_repo_main(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub sync main {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.sync_main, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    return router
