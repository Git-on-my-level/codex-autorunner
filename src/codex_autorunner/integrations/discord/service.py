from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Optional

from ...core.config import load_repo_config
from ...core.flows import (
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
    archive_flow_run_artifacts,
    load_latest_paused_ticket_flow_dispatch,
)
from ...core.flows.ux_helpers import build_flow_status_snapshot, ensure_worker
from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path
from ...flows.ticket_flow.runtime_helpers import build_ticket_flow_controller
from ...integrations.chat.text_chunking import chunk_text
from ...tickets.outbox import resolve_outbox_paths
from .allowlist import DiscordAllowlist, allowlist_allows
from .config import DiscordBotConfig
from .gateway import DiscordGatewayClient
from .interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
)
from .outbox import DiscordOutboxManager
from .rest import DiscordRestClient
from .state import DiscordStateStore, OutboxRecord

DISCORD_EPHEMERAL_FLAG = 64
PAUSE_SCAN_INTERVAL_SECONDS = 5.0
FLOW_RUNS_DEFAULT_LIMIT = 5
FLOW_RUNS_MAX_LIMIT = 20


class DiscordBotService:
    def __init__(
        self,
        config: DiscordBotConfig,
        *,
        logger: logging.Logger,
        rest_client: Optional[DiscordRestClient] = None,
        gateway_client: Optional[DiscordGatewayClient] = None,
        state_store: Optional[DiscordStateStore] = None,
        outbox_manager: Optional[DiscordOutboxManager] = None,
    ) -> None:
        self._config = config
        self._logger = logger

        self._rest = (
            rest_client
            if rest_client is not None
            else DiscordRestClient(bot_token=config.bot_token or "")
        )
        self._owns_rest = rest_client is None

        self._gateway = (
            gateway_client
            if gateway_client is not None
            else DiscordGatewayClient(
                bot_token=config.bot_token or "",
                intents=config.intents,
                logger=logger,
            )
        )
        self._owns_gateway = gateway_client is None

        self._store = (
            state_store
            if state_store is not None
            else DiscordStateStore(config.state_file)
        )
        self._owns_store = state_store is None

        self._outbox = (
            outbox_manager
            if outbox_manager is not None
            else DiscordOutboxManager(
                self._store,
                send_message=self._send_channel_message,
                logger=logger,
            )
        )
        self._allowlist = DiscordAllowlist(
            allowed_guild_ids=config.allowed_guild_ids,
            allowed_channel_ids=config.allowed_channel_ids,
            allowed_user_ids=config.allowed_user_ids,
        )

    async def run_forever(self) -> None:
        await self._store.initialize()
        self._outbox.start()
        outbox_task = asyncio.create_task(self._outbox.run_loop())
        pause_watch_task = asyncio.create_task(self._watch_ticket_flow_pauses())
        try:
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.starting",
                state_file=str(self._config.state_file),
            )
            await self._gateway.run(self._on_dispatch)
        finally:
            pause_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pause_watch_task
            outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbox_task
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._owns_gateway:
            with contextlib.suppress(Exception):
                await self._gateway.stop()
        if self._owns_rest and hasattr(self._rest, "close"):
            with contextlib.suppress(Exception):
                await self._rest.close()  # type: ignore[func-returns-value]
        if self._owns_store:
            with contextlib.suppress(Exception):
                await self._store.close()

    async def _watch_ticket_flow_pauses(self) -> None:
        while True:
            try:
                await self._scan_and_enqueue_pause_notifications()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.pause_watch.scan_failed",
                    exc=exc,
                )
            await asyncio.sleep(PAUSE_SCAN_INTERVAL_SECONDS)

    async def _scan_and_enqueue_pause_notifications(self) -> None:
        bindings = await self._store.list_bindings()
        for binding in bindings:
            channel_id = binding.get("channel_id")
            workspace_raw = binding.get("workspace_path")
            if not isinstance(channel_id, str) or not isinstance(workspace_raw, str):
                continue
            workspace_root = canonicalize_path(Path(workspace_raw))
            snapshot = await asyncio.to_thread(
                load_latest_paused_ticket_flow_dispatch, workspace_root
            )
            if snapshot is None:
                continue

            if (
                binding.get("last_pause_run_id") == snapshot.run_id
                and binding.get("last_pause_dispatch_seq") == snapshot.dispatch_seq
            ):
                continue

            chunks = chunk_text(
                snapshot.dispatch_markdown,
                max_len=self._config.max_message_length,
                with_numbering=True,
            )
            if not chunks:
                chunks = ["(pause notification had no content)"]

            enqueued = True
            for index, chunk in enumerate(chunks, start=1):
                record_id = f"pause:{channel_id}:{snapshot.run_id}:{snapshot.dispatch_seq}:{index}"
                try:
                    await self._store.enqueue_outbox(
                        OutboxRecord(
                            record_id=record_id,
                            channel_id=channel_id,
                            message_id=None,
                            operation="send",
                            payload_json={"content": chunk},
                        )
                    )
                except Exception as exc:
                    enqueued = False
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.pause_watch.enqueue_failed",
                        exc=exc,
                        channel_id=channel_id,
                        run_id=snapshot.run_id,
                        dispatch_seq=snapshot.dispatch_seq,
                    )
                    break

            if not enqueued:
                continue

            await self._store.mark_pause_dispatch_seen(
                channel_id=channel_id,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
            )
            log_event(
                self._logger,
                logging.INFO,
                "discord.pause_watch.notified",
                channel_id=channel_id,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
                chunk_count=len(chunks),
            )

    async def _send_channel_message(
        self, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rest.create_channel_message(
            channel_id=channel_id, payload=payload
        )

    async def _on_dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type != "INTERACTION_CREATE":
            return
        await self._handle_interaction(payload)

    async def _handle_interaction(self, interaction_payload: dict[str, Any]) -> None:
        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)
        guild_id = extract_guild_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord command is not authorized for this channel/user/guild.",
            )
            return

        command_path, options = extract_command_path_and_options(interaction_payload)
        if not command_path:
            return

        if command_path == ("car", "bind"):
            await self._handle_bind(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                options=options,
            )
            return
        if command_path == ("car", "status"):
            await self._handle_status(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return

        if command_path[:2] == ("car", "flow"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return

            if command_path == ("car", "flow", "status"):
                await self._handle_flow_status(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "runs"):
                await self._handle_flow_runs(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "resume"):
                await self._handle_flow_resume(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "stop"):
                await self._handle_flow_stop(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "archive"):
                await self._handle_flow_archive(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "reply"):
                await self._handle_flow_reply(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Command not implemented yet for Discord.",
        )

    async def _handle_bind(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        options: dict[str, Any],
    ) -> None:
        raw_path = options.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Missing required option: path",
            )
            return

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._config.root / candidate
        workspace = canonicalize_path(candidate)
        if not workspace.exists() or not workspace.is_dir():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Workspace path does not exist: {workspace}",
            )
            return

        await self._store.upsert_binding(
            channel_id=channel_id,
            guild_id=guild_id,
            workspace_path=str(workspace),
            repo_id=None,
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Bound this channel to workspace: {workspace}",
        )

    async def _handle_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = (
                "This channel is not bound. Use /car bind path:<workspace>. "
                "Then use /car flow status once flow commands are enabled."
            )
        else:
            text = (
                "Channel is bound to workspace: "
                f"{binding['workspace_path']}. "
                "Use /car flow status for ticket flow details."
            )
        await self._respond_ephemeral(interaction_id, interaction_token, text)

    async def _require_bound_workspace(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> Optional[Path]:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This channel is not bound. Run `/car bind path:<...>` first.",
            )
            return None
        workspace_raw = binding.get("workspace_path")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Binding is invalid. Run `/car bind path:<...>` first.",
            )
            return None
        return canonicalize_path(Path(workspace_raw))

    def _open_flow_store(self, workspace_root: Path) -> FlowStore:
        config = load_repo_config(workspace_root)
        store = FlowStore(
            workspace_root / ".codex-autorunner" / "flows.db",
            durable=config.durable_writes,
        )
        store.initialize()
        return store

    def _resolve_flow_run_by_id(
        self,
        store: FlowStore,
        *,
        run_id: str,
    ) -> Optional[FlowRunRecord]:
        record = store.get_flow_run(run_id)
        if record is None or record.flow_type != "ticket_flow":
            return None
        return record

    @staticmethod
    def _select_default_status_run(
        records: list[FlowRunRecord],
    ) -> Optional[FlowRunRecord]:
        if not records:
            return None
        for record in records:
            if record.status in {FlowRunStatus.RUNNING, FlowRunStatus.PAUSED}:
                return record
        return records[0]

    async def _handle_flow_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        run_id_opt = options.get("run_id")
        store = self._open_flow_store(workspace_root)
        try:
            record: Optional[FlowRunRecord]
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                record = self._resolve_flow_run_by_id(store, run_id=run_id_opt.strip())
            else:
                record = self._select_default_status_run(
                    store.list_flow_runs(flow_type="ticket_flow")
                )
            if record is None:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, "No ticket_flow runs found."
                )
                return
            snapshot = build_flow_status_snapshot(workspace_root, record, store)
        finally:
            store.close()

        worker = snapshot.get("worker_health")
        worker_status = getattr(worker, "status", "unknown")
        worker_pid = getattr(worker, "pid", None)
        worker_text = (
            f"{worker_status} (pid={worker_pid})"
            if isinstance(worker_pid, int)
            else str(worker_status)
        )
        last_event_seq = snapshot.get("last_event_seq")
        last_event_at = snapshot.get("last_event_at")
        current_ticket = snapshot.get("effective_current_ticket")
        lines = [
            f"Run: {record.id}",
            f"Status: {record.status.value}",
            f"Last event: {last_event_seq if last_event_seq is not None else '-'} at {last_event_at or '-'}",
            f"Worker: {worker_text}",
            f"Current ticket: {current_ticket or '-'}",
        ]
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_flow_runs(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        raw_limit = options.get("limit")
        limit = FLOW_RUNS_DEFAULT_LIMIT
        if isinstance(raw_limit, int):
            limit = raw_limit
        limit = max(1, min(limit, FLOW_RUNS_MAX_LIMIT))

        store = self._open_flow_store(workspace_root)
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")[:limit]
        finally:
            store.close()

        if not runs:
            await self._respond_ephemeral(
                interaction_id, interaction_token, "No ticket_flow runs found."
            )
            return

        lines = [f"Recent ticket_flow runs (limit={limit}):"]
        for record in runs:
            lines.append(f"- {record.id} [{record.status.value}]")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    @staticmethod
    def _close_worker_handles(ensure_result: dict[str, Any]) -> None:
        for key in ("stdout", "stderr"):
            handle = ensure_result.get(key)
            close = getattr(handle, "close", None)
            if callable(close):
                close()

    async def _handle_flow_resume(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        run_id_opt = options.get("run_id")
        store = self._open_flow_store(workspace_root)
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                target = self._resolve_flow_run_by_id(store, run_id=run_id_opt.strip())
            else:
                target = next(
                    (
                        record
                        for record in store.list_flow_runs(flow_type="ticket_flow")
                        if record.status == FlowRunStatus.PAUSED
                    ),
                    None,
                )
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No paused ticket_flow run found to resume.",
            )
            return

        controller = build_ticket_flow_controller(workspace_root)
        try:
            updated = await controller.resume_flow(target.id)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        ensure_result = ensure_worker(
            workspace_root, updated.id, is_terminal=updated.status.is_terminal()
        )
        self._close_worker_handles(ensure_result)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Resumed run {updated.id}.",
        )

    async def _handle_flow_stop(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        run_id_opt = options.get("run_id")
        store = self._open_flow_store(workspace_root)
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                target = self._resolve_flow_run_by_id(store, run_id=run_id_opt.strip())
            else:
                target = next(
                    (
                        record
                        for record in store.list_flow_runs(flow_type="ticket_flow")
                        if not record.status.is_terminal()
                    ),
                    None,
                )
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No active ticket_flow run found to stop.",
            )
            return

        controller = build_ticket_flow_controller(workspace_root)
        try:
            updated = await controller.stop_flow(target.id)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Stop requested for run {updated.id} ({updated.status.value}).",
        )

    async def _handle_flow_archive(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        run_id_opt = options.get("run_id")
        store = self._open_flow_store(workspace_root)
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                target = self._resolve_flow_run_by_id(store, run_id=run_id_opt.strip())
            else:
                runs = store.list_flow_runs(flow_type="ticket_flow")
                target = runs[0] if runs else None
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No ticket_flow run found to archive.",
            )
            return

        try:
            summary = archive_flow_run_artifacts(
                workspace_root,
                run_id=target.id,
                force=False,
                delete_run=False,
            )
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Archived run {summary['run_id']} (runs_archived={summary['archived_runs']}).",
        )

    async def _handle_flow_reply(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        text = options.get("text")
        if not isinstance(text, str) or not text.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Missing required option: text",
            )
            return

        run_id_opt = options.get("run_id")
        store = self._open_flow_store(workspace_root)
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                target = self._resolve_flow_run_by_id(store, run_id=run_id_opt.strip())
                if target and target.status != FlowRunStatus.PAUSED:
                    target = None
            else:
                target = next(
                    (
                        record
                        for record in store.list_flow_runs(flow_type="ticket_flow")
                        if record.status == FlowRunStatus.PAUSED
                    ),
                    None,
                )
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No paused ticket_flow run found for reply.",
            )
            return

        reply_path = self._write_user_reply(workspace_root, target, text)

        controller = build_ticket_flow_controller(workspace_root)
        try:
            updated = await controller.resume_flow(target.id)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        ensure_result = ensure_worker(
            workspace_root, updated.id, is_terminal=updated.status.is_terminal()
        )
        self._close_worker_handles(ensure_result)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Reply saved to {reply_path.name} and resumed run {updated.id}.",
        )

    def _write_user_reply(
        self,
        workspace_root: Path,
        record: FlowRunRecord,
        text: str,
    ) -> Path:
        runs_dir_raw = record.input_data.get("runs_dir")
        runs_dir = (
            Path(runs_dir_raw)
            if isinstance(runs_dir_raw, str) and runs_dir_raw
            else Path(".codex-autorunner/runs")
        )
        run_paths = resolve_outbox_paths(
            workspace_root=workspace_root,
            runs_dir=runs_dir,
            run_id=record.id,
        )
        run_paths.run_dir.mkdir(parents=True, exist_ok=True)
        reply_path = run_paths.run_dir / "USER_REPLY.md"
        reply_path.write_text(text.strip() + "\n", encoding="utf-8")
        return reply_path

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = text if len(text) <= max_len else f"{text[: max_len - 3]}..."
        await self._rest.create_interaction_response(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            payload={
                "type": 4,
                "data": {
                    "content": content,
                    "flags": DISCORD_EPHEMERAL_FLAG,
                },
            },
        )


def create_discord_bot_service(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
) -> DiscordBotService:
    return DiscordBotService(config, logger=logger)
