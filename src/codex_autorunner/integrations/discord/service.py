from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.config import load_repo_config
from ...core.filebox import (
    inbox_dir,
    outbox_pending_dir,
    outbox_sent_dir,
)
from ...core.flows import (
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
    archive_flow_run_artifacts,
    load_latest_paused_ticket_flow_dispatch,
)
from ...core.flows.ux_helpers import build_flow_status_snapshot, ensure_worker
from ...core.logging_utils import log_event
from ...core.pma_context import build_hub_snapshot, format_pma_prompt, load_pma_prompt
from ...core.pma_sink import PmaActiveSinkStore
from ...core.ports.run_event import Completed, Failed, Started
from ...core.state import RunnerState
from ...core.utils import canonicalize_path
from ...flows.ticket_flow.runtime_helpers import build_ticket_flow_controller
from ...integrations.agents.backend_orchestrator import BackendOrchestrator
from ...integrations.app_server.threads import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
)
from ...integrations.chat.bootstrap import ChatBootstrapStep, run_chat_bootstrap_steps
from ...integrations.chat.command_ingress import canonicalize_command_ingress
from ...integrations.chat.dispatcher import (
    ChatDispatcher,
    DispatchContext,
)
from ...integrations.chat.models import (
    ChatEvent,
    ChatInteractionEvent,
    ChatMessageEvent,
)
from ...integrations.chat.turn_policy import (
    PlainTextTurnContext,
    should_trigger_plain_text_turn,
)
from ...manifest import load_manifest
from ...tickets.outbox import resolve_outbox_paths
from .adapter import DiscordChatAdapter
from .allowlist import DiscordAllowlist, allowlist_allows
from .command_registry import sync_commands
from .commands import build_application_commands
from .components import (
    build_bind_picker,
    build_flow_runs_picker,
    build_flow_status_buttons,
)
from .config import DiscordBotConfig
from .errors import DiscordAPIError, DiscordTransientError
from .gateway import DiscordGatewayClient
from .interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_component_custom_id,
    extract_component_values,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
    is_component_interaction,
)
from .outbox import DiscordOutboxManager
from .rendering import (
    chunk_discord_message,
    truncate_for_discord,
)
from .rest import DiscordRestClient
from .state import DiscordStateStore, OutboxRecord

DISCORD_EPHEMERAL_FLAG = 64
PAUSE_SCAN_INTERVAL_SECONDS = 5.0
FLOW_RUNS_DEFAULT_LIMIT = 5
FLOW_RUNS_MAX_LIMIT = 20
MESSAGE_TURN_APPROVAL_POLICY = "never"
MESSAGE_TURN_SANDBOX_POLICY = "dangerFullAccess"


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
        manifest_path: Optional[Path] = None,
        chat_adapter: Optional[DiscordChatAdapter] = None,
        dispatcher: Optional[ChatDispatcher] = None,
        backend_orchestrator_factory: Optional[
            Callable[[Path], BackendOrchestrator]
        ] = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._manifest_path = manifest_path
        self._backend_orchestrator_factory = backend_orchestrator_factory

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

        self._chat_adapter = (
            chat_adapter
            if chat_adapter is not None
            else DiscordChatAdapter(
                rest_client=self._rest,
                application_id=config.application_id or "",
                logger=logger,
                message_overflow=config.message_overflow,
            )
        )
        self._dispatcher = dispatcher or ChatDispatcher(
            logger=logger,
            allowlist_predicate=lambda event, context: self._allowlist_predicate(
                event, context
            ),
        )
        self._backend_orchestrators: dict[str, BackendOrchestrator] = {}
        self._backend_lock = asyncio.Lock()
        self._hub_config_path: Optional[Path] = None
        generated_hub_config = self._config.root / ".codex-autorunner" / "config.yml"
        if generated_hub_config.exists():
            self._hub_config_path = generated_hub_config
        else:
            root_hub_config = self._config.root / "codex-autorunner.yml"
            if root_hub_config.exists():
                self._hub_config_path = root_hub_config

        self._hub_supervisor = None
        try:
            from ...core.hub import HubSupervisor

            self._hub_supervisor = HubSupervisor.from_path(self._config.root)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.pma.hub_supervisor.unavailable",
                hub_root=str(self._config.root),
                exc=exc,
            )

    async def run_forever(self) -> None:
        await self._store.initialize()
        await run_chat_bootstrap_steps(
            platform="discord",
            logger=self._logger,
            steps=(
                ChatBootstrapStep(
                    name="sync_application_commands",
                    action=self._sync_application_commands_on_startup,
                    required=True,
                ),
            ),
        )
        self._outbox.start()
        outbox_task = asyncio.create_task(self._outbox.run_loop())
        pause_watch_task = asyncio.create_task(self._watch_ticket_flow_pauses())
        dispatcher_loop_task = asyncio.create_task(self._run_dispatcher_loop())
        try:
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.starting",
                state_file=str(self._config.state_file),
            )
            await self._gateway.run(self._on_dispatch)
        finally:
            with contextlib.suppress(Exception):
                await self._dispatcher.wait_idle()
            dispatcher_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher_loop_task
            pause_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pause_watch_task
            outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbox_task
            await self._shutdown()

    async def _run_dispatcher_loop(self) -> None:
        while True:
            events = await self._chat_adapter.poll_events(timeout_seconds=30.0)
            for event in events:
                await self._dispatcher.dispatch(event, self._handle_chat_event)

    async def _handle_chat_event(
        self, event: ChatEvent, context: DispatchContext
    ) -> None:
        if isinstance(event, ChatInteractionEvent):
            await self._handle_normalized_interaction(event, context)
            return
        if isinstance(event, ChatMessageEvent):
            await self._handle_message_event(event, context)
            return

    def _allowlist_predicate(self, event: ChatEvent, context: DispatchContext) -> bool:
        fake_payload = {
            "channel_id": context.chat_id,
            "guild_id": context.thread_id if context.thread_id else None,
            "member": {"user": {"id": context.user_id}} if context.user_id else None,
        }
        return allowlist_allows(fake_payload, self._allowlist)

    async def _handle_normalized_interaction(
        self, event: ChatInteractionEvent, context: DispatchContext
    ) -> None:
        import json

        payload_str = event.payload or "{}"
        try:
            payload_data = json.loads(payload_str)
        except json.JSONDecodeError:
            payload_data = {}

        interaction_id = payload_data.get(
            "_discord_interaction_id", event.interaction.interaction_id
        )
        interaction_token = payload_data.get("_discord_token")
        channel_id = context.chat_id

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_normalized_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        ingress = canonicalize_command_ingress(
            command=payload_data.get("command"),
            options=payload_data.get("options"),
        )
        command = ingress.command if ingress is not None else ""
        guild_id = payload_data.get("guild_id")

        try:
            if ingress is not None and ingress.command_path[:1] == ("car",):
                await self._handle_car_command(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=context.thread_id,
                    user_id=event.from_user_id,
                    command_path=ingress.command_path,
                    options=ingress.options,
                )
            elif ingress is not None and ingress.command_path[:1] == ("pma",):
                await self._handle_pma_command_from_normalized(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    command=command,
                )
            else:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Command not implemented yet for Discord.",
                )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.interaction.unhandled_error",
                command=command,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
            )

    async def _handle_message_event(
        self,
        event: ChatMessageEvent,
        context: DispatchContext,
    ) -> None:
        channel_id = context.chat_id
        text = (event.text or "").strip()
        if not text:
            return
        if text.startswith("/"):
            return
        if not should_trigger_plain_text_turn(
            mode="always",
            context=PlainTextTurnContext(text=text),
        ):
            return

        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._send_channel_message(
                channel_id,
                {
                    "content": "This channel is not bound. Run `/car bind path:<workspace>` or `/pma on`.",
                },
            )
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            await self._send_channel_message(
                channel_id,
                {"content": "Binding is invalid. Run `/car bind path:<workspace>`."},
            )
            return

        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            await self._send_channel_message(
                channel_id,
                {"content": f"Workspace path does not exist: {workspace_root}"},
            )
            return

        if not pma_enabled:
            paused = await self._find_paused_flow_run(workspace_root)
            if paused is not None:
                reply_path = self._write_user_reply(workspace_root, paused, text)
                controller = build_ticket_flow_controller(workspace_root)
                try:
                    updated = await controller.resume_flow(paused.id)
                except ValueError as exc:
                    await self._send_channel_message(
                        channel_id,
                        {"content": f"Failed to resume paused run: {exc}"},
                    )
                    return
                ensure_result = ensure_worker(
                    workspace_root,
                    updated.id,
                    is_terminal=updated.status.is_terminal(),
                )
                self._close_worker_handles(ensure_result)
                await self._send_channel_message(
                    channel_id,
                    {
                        "content": (
                            f"Reply saved to `{reply_path.name}` and resumed paused run `{updated.id}`."
                        )
                    },
                )
                return

        prompt_text = text
        if pma_enabled:
            try:
                snapshot = await build_hub_snapshot(
                    self._hub_supervisor, hub_root=self._config.root
                )
                prompt_base = load_pma_prompt(self._config.root)
                prompt_text = format_pma_prompt(
                    prompt_base,
                    snapshot,
                    text,
                    hub_root=self._config.root,
                )
                PmaActiveSinkStore(self._config.root).set_chat(
                    platform="discord",
                    chat_id=channel_id,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.pma.prompt_build.failed",
                    channel_id=channel_id,
                    exc=exc,
                )
                await self._send_channel_message(
                    channel_id,
                    {"content": "Failed to build PMA context. Please try again."},
                )
                return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT
        model_override = binding.get("model_override")
        if not isinstance(model_override, str) or not model_override.strip():
            model_override = None
        reasoning_effort = binding.get("reasoning_effort")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            reasoning_effort = None

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        try:
            response_text = await self._run_agent_turn_for_message(
                workspace_root=workspace_root,
                prompt_text=prompt_text,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=session_key,
                orchestrator_channel_key=(
                    channel_id if not pma_enabled else f"pma:{channel_id}"
                ),
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.turn.failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            await self._send_channel_message(
                channel_id,
                {"content": f"Turn failed: {exc}"},
            )
            return

        chunks = chunk_discord_message(
            response_text or "(No response text returned.)",
            max_len=self._config.max_message_length,
            with_numbering=True,
        )
        if not chunks:
            chunks = ["(No response text returned.)"]
        for chunk in chunks:
            await self._send_channel_message(channel_id, {"content": chunk})

    async def _find_paused_flow_run(
        self, workspace_root: Path
    ) -> Optional[FlowRunRecord]:
        try:
            store = self._open_flow_store(workspace_root)
        except Exception:
            return None
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            return next(
                (record for record in runs if record.status == FlowRunStatus.PAUSED),
                None,
            )
        except Exception:
            return None
        finally:
            store.close()

    def _build_message_session_key(
        self,
        *,
        channel_id: str,
        workspace_root: Path,
        pma_enabled: bool,
        agent: str,
    ) -> str:
        if pma_enabled:
            return PMA_OPENCODE_KEY if agent == "opencode" else PMA_KEY
        digest = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:12]
        prefix = FILE_CHAT_OPENCODE_PREFIX if agent == "opencode" else FILE_CHAT_PREFIX
        return f"{prefix}discord.{channel_id}.{digest}"

    def _build_runner_state(
        self,
        *,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
    ) -> RunnerState:
        return RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            autorunner_agent_override=agent,
            autorunner_model_override=model_override,
            autorunner_effort_override=reasoning_effort,
            autorunner_approval_policy=MESSAGE_TURN_APPROVAL_POLICY,
            autorunner_sandbox_mode=MESSAGE_TURN_SANDBOX_POLICY,
        )

    async def _orchestrator_for_workspace(
        self, workspace_root: Path, *, channel_id: str
    ) -> BackendOrchestrator:
        key = f"{channel_id}:{workspace_root}"
        async with self._backend_lock:
            existing = self._backend_orchestrators.get(key)
            if existing is not None:
                return existing
            if self._backend_orchestrator_factory is not None:
                orchestrator = self._backend_orchestrator_factory(workspace_root)
            else:
                repo_config = load_repo_config(
                    workspace_root,
                    hub_path=self._hub_config_path,
                )
                orchestrator = BackendOrchestrator(
                    repo_root=workspace_root,
                    config=repo_config,
                    logger=self._logger,
                )
            self._backend_orchestrators[key] = orchestrator
            return orchestrator

    async def _run_agent_turn_for_message(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )
        state = self._build_runner_state(
            agent=agent,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )
        known_session = orchestrator.get_thread_id(session_key)
        final_message = ""
        error_message = None
        session_from_events = known_session
        async for run_event in orchestrator.run_turn(
            agent_id=agent,
            state=state,
            prompt=prompt_text,
            model=model_override,
            reasoning=reasoning_effort,
            session_key=session_key,
            session_id=known_session,
            workspace_root=workspace_root,
        ):
            if isinstance(run_event, Started):
                if isinstance(run_event.session_id, str) and run_event.session_id:
                    session_from_events = run_event.session_id
            elif isinstance(run_event, Completed):
                final_message = run_event.final_message or final_message
            elif isinstance(run_event, Failed):
                error_message = run_event.error_message or "Turn failed"
        if session_from_events:
            orchestrator.set_thread_id(session_key, session_from_events)
        if error_message:
            raise RuntimeError(error_message)
        return final_message

    async def _handle_car_command(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        user_id: Optional[str],
        command_path: tuple[str, ...],
        options: dict[str, Any],
    ) -> None:
        primary = command_path[1] if len(command_path) > 1 else ""

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
        if command_path == ("car", "debug"):
            await self._handle_debug(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "help"):
            await self._handle_help(
                interaction_id,
                interaction_token,
            )
            return
        if command_path == ("car", "ids"):
            await self._handle_ids(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            )
            return
        if command_path == ("car", "agent"):
            await self._handle_car_agent(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "model"):
            await self._handle_car_model(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "repos"):
            await self._handle_repos(
                interaction_id,
                interaction_token,
            )
            return
        if command_path == ("car", "diff"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_diff(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "skills"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_skills(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "mcp"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_mcp(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "init"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_init(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
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
                f"Unknown car flow subcommand: {primary}",
            )
            return

        if command_path[:2] == ("car", "files"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return

            if command_path == ("car", "files", "inbox"):
                await self._handle_files_inbox(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                )
                return
            if command_path == ("car", "files", "outbox"):
                await self._handle_files_outbox(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                )
                return
            if command_path == ("car", "files", "clear"):
                await self._handle_files_clear(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown car files subcommand: {primary}",
            )
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown car subcommand: {primary}",
        )

    async def _handle_pma_command_from_normalized(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        command: str,
    ) -> None:
        subcommand = command.split(":")[-1] if ":" in command else "status"
        if subcommand == "on":
            await self._handle_pma_on(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
            )
        elif subcommand == "off":
            await self._handle_pma_off(
                interaction_id, interaction_token, channel_id=channel_id
            )
        elif subcommand == "status":
            await self._handle_pma_status(
                interaction_id, interaction_token, channel_id=channel_id
            )
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Unknown PMA subcommand. Use on, off, or status.",
            )

    async def _sync_application_commands_on_startup(self) -> None:
        registration = self._config.command_registration
        if not registration.enabled:
            log_event(
                self._logger,
                logging.INFO,
                "discord.commands.sync.disabled",
            )
            return

        application_id = (self._config.application_id or "").strip()
        if not application_id:
            raise ValueError("missing Discord application id for command sync")
        if registration.scope == "guild" and not registration.guild_ids:
            raise ValueError("guild scope requires at least one guild_id")

        commands = build_application_commands()
        try:
            await sync_commands(
                self._rest,
                application_id=application_id,
                commands=commands,
                scope=registration.scope,
                guild_ids=registration.guild_ids,
                logger=self._logger,
            )
        except ValueError:
            raise
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.commands.sync.startup_failed",
                scope=registration.scope,
                command_count=len(commands),
                exc=exc,
            )

    async def _shutdown(self) -> None:
        if self._owns_gateway:
            with contextlib.suppress(Exception):
                await self._gateway.stop()
        if self._owns_rest and hasattr(self._rest, "close"):
            with contextlib.suppress(Exception):
                await self._rest.close()
        if self._owns_store:
            with contextlib.suppress(Exception):
                await self._store.close()
        async with self._backend_lock:
            orchestrators = list(self._backend_orchestrators.values())
            self._backend_orchestrators.clear()
        for orchestrator in orchestrators:
            with contextlib.suppress(Exception):
                await orchestrator.close_all()

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

            chunks = chunk_discord_message(
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
        if event_type == "INTERACTION_CREATE":
            await self._handle_interaction(payload)
        elif event_type == "MESSAGE_CREATE":
            event = self._chat_adapter.parse_message_event(payload)
            if event is not None:
                await self._dispatcher.dispatch(event, self._handle_chat_event)

    async def _handle_interaction(self, interaction_payload: dict[str, Any]) -> None:
        if is_component_interaction(interaction_payload):
            await self._handle_component_interaction(interaction_payload)
            return

        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)
        guild_id = extract_guild_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord command is not authorized for this channel/user/guild.",
            )
            return

        command_path, options = extract_command_path_and_options(interaction_payload)
        ingress = canonicalize_command_ingress(
            command_path=command_path,
            options=options,
        )
        if ingress is None:
            self._logger.warning(
                "handle_interaction: failed to canonicalize command ingress (command_path=%s, options=%s)",
                command_path,
                options,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not parse this interaction. Please retry the command.",
            )
            return

        try:
            if ingress.command_path[:1] == ("car",):
                await self._handle_car_command(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    user_id=extract_user_id(interaction_payload),
                    command_path=ingress.command_path,
                    options=ingress.options,
                )
                return

            if ingress.command_path[:1] == ("pma",):
                await self._handle_pma_command(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    command_path=ingress.command_path,
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Command not implemented yet for Discord.",
            )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.interaction.unhandled_error",
                command_path=ingress.command,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
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
        raw_path = options.get("workspace")
        if isinstance(raw_path, str) and raw_path.strip():
            await self._bind_with_path(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                raw_path=raw_path.strip(),
            )
            return

        repos = self._list_manifest_repos()
        if not repos:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No repos found in manifest. Use /car bind workspace:<workspace> to bind manually.",
            )
            return

        components = [build_bind_picker(repos)]
        await self._respond_with_components(
            interaction_id,
            interaction_token,
            "Select a workspace to bind:",
            components,
        )

    def _list_manifest_repos(self) -> list[tuple[str, str]]:
        if not self._manifest_path or not self._manifest_path.exists():
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._config.root)
            return [
                (repo.id, str(self._config.root / repo.path))
                for repo in manifest.repos
                if repo.id
            ]
        except Exception:
            return []

    async def _bind_with_path(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        raw_path: str,
    ) -> None:
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
                "This channel is not bound. Use /car bind workspace:<workspace>. "
                "Then use /car flow status once flow commands are enabled."
            )
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        lines = []
        is_pma = binding.get("pma_enabled", False)
        workspace_path = binding.get("workspace_path", "unknown")
        repo_id = binding.get("repo_id")
        guild_id = binding.get("guild_id")
        updated_at = binding.get("updated_at", "unknown")

        if is_pma:
            lines.append("Mode: PMA (hub)")
            prev_workspace = binding.get("pma_prev_workspace_path")
            if prev_workspace:
                lines.append(f"Previous binding: {prev_workspace}")
                lines.append("Use /pma off to restore previous binding.")
        else:
            lines.append("Mode: workspace")
            lines.append("Channel is bound.")

        lines.extend(
            [
                f"Workspace: {workspace_path}",
                f"Repo ID: {repo_id or 'none'}",
                f"Guild ID: {guild_id or 'none'}",
                f"Channel ID: {channel_id}",
                f"Last updated: {updated_at}",
            ]
        )

        active_flow_info = await self._get_active_flow_info(workspace_path)
        if active_flow_info:
            lines.append(f"Active flow: {active_flow_info}")

        lines.append("Use /car flow status for ticket flow details.")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _get_active_flow_info(self, workspace_path: str) -> Optional[str]:
        if not workspace_path or workspace_path == "unknown":
            return None
        try:
            workspace_root = canonicalize_path(Path(workspace_path))
            if not workspace_root.exists():
                return None
            store = self._open_flow_store(workspace_root)
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
                for record in runs:
                    if record.status == FlowRunStatus.RUNNING:
                        return f"{record.id} (running)"
                    if record.status == FlowRunStatus.PAUSED:
                        return f"{record.id} (paused)"
            finally:
                store.close()
        except Exception:
            pass
        return None

    async def _handle_debug(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        lines = [
            f"Channel ID: {channel_id}",
        ]
        if binding is None:
            lines.append("Binding: none (unbound)")
            lines.append("Use /car bind path:<workspace> to bind this channel.")
            await self._respond_ephemeral(
                interaction_id, interaction_token, "\n".join(lines)
            )
            return

        workspace_path = binding.get("workspace_path", "unknown")
        lines.extend(
            [
                f"Guild ID: {binding.get('guild_id') or 'none'}",
                f"Workspace: {workspace_path}",
                f"Repo ID: {binding.get('repo_id') or 'none'}",
                f"PMA enabled: {binding.get('pma_enabled', False)}",
                f"PMA prev workspace: {binding.get('pma_prev_workspace_path') or 'none'}",
                f"Updated at: {binding.get('updated_at', 'unknown')}",
            ]
        )

        if workspace_path and workspace_path != "unknown":
            try:
                workspace_root = canonicalize_path(Path(workspace_path))
                lines.append(f"Canonical path: {workspace_root}")
                lines.append(f"Path exists: {workspace_root.exists()}")
                if workspace_root.exists():
                    car_dir = workspace_root / ".codex-autorunner"
                    lines.append(f".codex-autorunner exists: {car_dir.exists()}")
                    flows_db = car_dir / "flows.db"
                    lines.append(f"flows.db exists: {flows_db.exists()}")
            except Exception as exc:
                lines.append(f"Path resolution error: {exc}")

        outbox_items = await self._store.list_outbox()
        pending_outbox = [r for r in outbox_items if r.channel_id == channel_id]
        lines.append(f"Pending outbox items: {len(pending_outbox)}")

        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_help(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        lines = [
            "**CAR Commands:**",
            "",
            "/car bind [path] - Bind channel to workspace",
            "/car status - Show binding status",
            "/car debug - Show debug info",
            "/car help - Show this help",
            "/car ids - Show channel/user IDs for debugging",
            "/car diff [path] - Show git diff",
            "/car skills - List available skills",
            "/car mcp - Show MCP server status",
            "/car init - Generate AGENTS.md",
            "/car repos - List available repos",
            "/car agent [name] - Set or show agent",
            "/car model [name] - Set or show model",
            "",
            "**Flow Commands:**",
            "/car flow status [run_id] - Show flow status",
            "/car flow runs [limit] - List flow runs",
            "/car flow resume [run_id] - Resume a paused flow",
            "/car flow stop [run_id] - Stop a flow",
            "/car flow archive [run_id] - Archive a flow",
            "/car flow reply <text> [run_id] - Reply to paused flow",
            "",
            "**File Commands:**",
            "/car files inbox - List inbox files",
            "/car files outbox - List pending outbox files",
            "/car files clear [target] - Clear inbox/outbox",
            "",
            "**PMA Commands:**",
            "/pma on - Enable PMA mode",
            "/pma off - Disable PMA mode",
            "/pma status - Show PMA status",
        ]
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_ids(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        lines = [
            f"Channel ID: {channel_id}",
            f"Guild ID: {guild_id or 'none'}",
            f"User ID: {user_id or 'unknown'}",
            "",
            "Allowlist example:",
            f"discord_bot.allowed_channel_ids: [{channel_id}]",
        ]
        if guild_id:
            lines.append(f"discord_bot.allowed_guild_ids: [{guild_id}]")
        if user_id:
            lines.append(f"discord_bot.allowed_user_ids: [{user_id}]")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_diff(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        import subprocess

        path_arg = options.get("path")
        cwd = workspace_root
        if isinstance(path_arg, str) and path_arg.strip():
            candidate = Path(path_arg.strip())
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            try:
                cwd = canonicalize_path(candidate)
            except Exception:
                cwd = workspace_root

        git_check = ["git", "rev-parse", "--is-inside-work-tree"]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                git_check,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Not a git repository.",
                )
                return
        except subprocess.TimeoutExpired:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Git check timed out.",
            )
            return
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Git check failed: {exc}",
            )
            return

        diff_cmd = [
            "bash",
            "-lc",
            "git diff --color; git ls-files --others --exclude-standard | "
            'while read -r f; do git diff --color --no-index -- /dev/null "$f"; done',
        ]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                diff_cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout
            if not output.strip():
                output = "(No diff output.)"
        except subprocess.TimeoutExpired:
            output = "Git diff timed out after 30 seconds."
        except Exception as exc:
            output = f"Failed to run git diff: {exc}"

        from .rendering import truncate_for_discord

        output = truncate_for_discord(output, self._config.max_message_length - 100)
        await self._respond_ephemeral(interaction_id, interaction_token, output)

    async def _handle_skills(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Skills listing requires the app server client. "
            "This command is not yet available in Discord.",
        )

    async def _handle_mcp(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "MCP server status requires the app server client. "
            "This command is not yet available in Discord.",
        )

    async def _handle_init(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "AGENTS.md generation requires the app server client. "
            "This command is not yet available in Discord.",
        )

    async def _handle_repos(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        if not self._manifest_path or not self._manifest_path.exists():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Hub manifest not configured.",
            )
            return

        try:
            manifest = load_manifest(self._manifest_path, self._config.root)
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to load manifest: {exc}",
            )
            return

        lines = ["Repositories:"]
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            lines.append(f"- `{repo.id}` ({repo.path})")

        if len(lines) == 1:
            lines.append("No enabled repositories found.")

        lines.append("\nUse /car bind to select a workspace.")

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(lines),
        )

    VALID_AGENT_VALUES = ("codex", "opencode")
    DEFAULT_AGENT = "codex"

    async def _handle_car_agent(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This channel is not bound. Run `/car bind path:<...>` first.",
            )
            return

        current_agent = binding.get("agent") or self.DEFAULT_AGENT
        agent_name = options.get("name")

        if not agent_name:
            lines = [
                f"Current agent: {current_agent}",
                "",
                "Available agents:",
                "  codex - Default Codex agent",
                "  opencode - OpenCode agent (requires opencode binary)",
                "",
                "Use `/car agent <name>` to switch.",
            ]
            await self._respond_ephemeral(
                interaction_id, interaction_token, "\n".join(lines)
            )
            return

        desired = agent_name.lower().strip()
        if desired not in self.VALID_AGENT_VALUES:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid agent '{agent_name}'. Valid options: {', '.join(self.VALID_AGENT_VALUES)}",
            )
            return

        if desired == current_agent:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Agent already set to {current_agent}.",
            )
            return

        await self._store.update_agent_state(channel_id=channel_id, agent=desired)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Agent set to {desired}. Will apply on the next turn.",
        )

    VALID_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")

    async def _handle_car_model(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This channel is not bound. Run `/car bind path:<...>` first.",
            )
            return

        current_agent = binding.get("agent") or self.DEFAULT_AGENT
        current_model = binding.get("model_override")
        current_effort = binding.get("reasoning_effort")
        model_name = options.get("name")
        effort = options.get("effort")

        if not model_name:
            lines = [
                f"Current agent: {current_agent}",
                f"Current model: {current_model or '(default)'}",
            ]
            if current_effort:
                lines.append(f"Reasoning effort: {current_effort}")
            lines.extend(
                [
                    "",
                    "Use `/car model <name>` to set a model.",
                    "Use `/car model <name> effort:<value>` to set model with reasoning effort (codex only).",
                    "",
                    f"Valid efforts: {', '.join(self.VALID_REASONING_EFFORTS)}",
                ]
            )
            await self._respond_ephemeral(
                interaction_id, interaction_token, "\n".join(lines)
            )
            return

        model_name = model_name.strip()
        if model_name.lower() in ("clear", "reset"):
            await self._store.update_model_state(
                channel_id=channel_id, clear_model=True
            )
            await self._respond_ephemeral(
                interaction_id, interaction_token, "Model override cleared."
            )
            return

        if effort:
            if current_agent != "codex":
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Reasoning effort is only supported for the codex agent.",
                )
                return
            effort = effort.lower().strip()
            if effort not in self.VALID_REASONING_EFFORTS:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Invalid effort '{effort}'. Valid options: {', '.join(self.VALID_REASONING_EFFORTS)}",
                )
                return

        await self._store.update_model_state(
            channel_id=channel_id,
            model_override=model_name,
            reasoning_effort=effort,
        )

        effort_note = f" (effort={effort})" if effort else ""
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Model set to {model_name}{effort_note}. Will apply on the next turn.",
        )

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
        if bool(binding.get("pma_enabled", False)):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA mode is enabled for this channel. Run `/pma off` before using `/car flow` commands.",
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
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            record: Optional[FlowRunRecord]
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    record = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                record = self._select_default_status_run(runs)
            if record is None:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, "No ticket_flow runs found."
                )
                return
            try:
                snapshot = build_flow_status_snapshot(workspace_root, record, store)
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.snapshot_failed",
                    exc=exc,
                    run_id=record.id,
                )
                raise DiscordTransientError(
                    f"Failed to build flow snapshot: {exc}",
                    user_message="Unable to build flow snapshot. Please try again later.",
                ) from None
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

        status_buttons = build_flow_status_buttons(
            record.id,
            record.status.value,
            include_refresh=True,
        )
        if status_buttons:
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                "\n".join(lines),
                status_buttons,
            )
        else:
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

        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")[:limit]
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        finally:
            store.close()

        if not runs:
            await self._respond_ephemeral(
                interaction_id, interaction_token, "No ticket_flow runs found."
            )
            return

        run_tuples = [(record.id, record.status.value) for record in runs]
        components = [build_flow_runs_picker(run_tuples)]
        lines = [f"Recent ticket_flow runs (limit={limit}):"]
        for record in runs:
            lines.append(f"- {record.id} [{record.status.value}]")
        await self._respond_with_components(
            interaction_id, interaction_token, "\n".join(lines), components
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
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (
                        record
                        for record in runs
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
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (record for record in runs if not record.status.is_terminal()),
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
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
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
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                if target and target.status != FlowRunStatus.PAUSED:
                    target = None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (
                        record
                        for record in runs
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
        try:
            run_paths.run_dir.mkdir(parents=True, exist_ok=True)
            reply_path = run_paths.run_dir / "USER_REPLY.md"
            reply_path.write_text(text.strip() + "\n", encoding="utf-8")
            return reply_path
        except OSError as exc:
            self._logger.error(
                "Failed to write user reply (run_id=%s, path=%s): %s",
                record.id,
                run_paths.run_dir,
                exc,
            )
            raise

    def _format_file_size(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        value = size / 1024
        for unit in ("KB", "MB", "GB"):
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def _list_files_in_dir(self, folder: Path) -> list[tuple[str, int, str]]:
        if not folder.exists():
            return []
        files: list[tuple[str, int, str]] = []
        for path in folder.iterdir():
            try:
                if path.is_file():
                    stat = path.stat()
                    from datetime import datetime, timezone

                    mtime = datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M")
                    files.append((path.name, stat.st_size, mtime))
            except OSError:
                continue
        return sorted(files, key=lambda x: x[2], reverse=True)

    def _delete_files_in_dir(self, folder: Path) -> int:
        if not folder.exists():
            return 0
        deleted = 0
        for path in folder.iterdir():
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    async def _handle_files_inbox(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        inbox = inbox_dir(workspace_root)
        files = self._list_files_in_dir(inbox)
        if not files:
            await self._respond_ephemeral(
                interaction_id, interaction_token, "Inbox: (empty)"
            )
            return
        lines = [f"Inbox ({len(files)} file(s)):"]
        for name, size, mtime in files[:20]:
            lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
        if len(files) > 20:
            lines.append(f"... and {len(files) - 20} more")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_files_outbox(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        pending = outbox_pending_dir(workspace_root)
        sent = outbox_sent_dir(workspace_root)
        pending_files = self._list_files_in_dir(pending)
        sent_files = self._list_files_in_dir(sent)
        lines = []
        if pending_files:
            lines.append(f"Outbox pending ({len(pending_files)} file(s)):")
            for name, size, mtime in pending_files[:20]:
                lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
            if len(pending_files) > 20:
                lines.append(f"... and {len(pending_files) - 20} more")
        else:
            lines.append("Outbox pending: (empty)")
        lines.append("")
        if sent_files:
            lines.append(f"Outbox sent ({len(sent_files)} file(s)):")
            for name, size, mtime in sent_files[:10]:
                lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
            if len(sent_files) > 10:
                lines.append(f"... and {len(sent_files) - 10} more")
        else:
            lines.append("Outbox sent: (empty)")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_files_clear(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        target = (options.get("target") or "all").lower().strip()
        inbox = inbox_dir(workspace_root)
        pending = outbox_pending_dir(workspace_root)
        sent = outbox_sent_dir(workspace_root)
        deleted = 0
        if target == "inbox":
            deleted = self._delete_files_in_dir(inbox)
        elif target == "outbox":
            deleted = self._delete_files_in_dir(pending)
            deleted += self._delete_files_in_dir(sent)
        elif target == "all":
            deleted = self._delete_files_in_dir(inbox)
            deleted += self._delete_files_in_dir(pending)
            deleted += self._delete_files_in_dir(sent)
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Invalid target. Use: inbox, outbox, or all",
            )
            return
        await self._respond_ephemeral(
            interaction_id, interaction_token, f"Deleted {deleted} file(s)."
        )

    async def _handle_pma_command(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        command_path: tuple[str, ...],
    ) -> None:
        if not self._config.pma_enabled:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA is disabled in hub config. Set pma.enabled: true to enable.",
            )
            return

        subcommand = command_path[1] if len(command_path) > 1 else "status"

        if subcommand == "on":
            await self._handle_pma_on(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
            )
        elif subcommand == "off":
            await self._handle_pma_off(
                interaction_id, interaction_token, channel_id=channel_id
            )
        elif subcommand == "status":
            await self._handle_pma_status(
                interaction_id, interaction_token, channel_id=channel_id
            )
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Unknown PMA subcommand. Use on, off, or status.",
            )

    async def _handle_pma_on(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is not None and binding.get("pma_enabled", False):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA mode is already enabled for this channel. Use /pma off to exit.",
            )
            return

        prev_workspace = binding.get("workspace_path") if binding is not None else None
        prev_repo_id = binding.get("repo_id") if binding is not None else None

        if binding is None:
            # Match Telegram behavior: /pma on can activate PMA on unbound channels.
            await self._store.upsert_binding(
                channel_id=channel_id,
                guild_id=guild_id,
                workspace_path=str(self._config.root),
                repo_id=None,
            )

        await self._store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=True,
            pma_prev_workspace_path=prev_workspace,
            pma_prev_repo_id=prev_repo_id,
        )

        sink_store = PmaActiveSinkStore(self._config.root)
        sink_store.set_chat(
            platform="discord",
            chat_id=channel_id,
        )

        hint = (
            "Use /pma off to exit. Previous binding saved."
            if prev_workspace
            else "Use /pma off to exit."
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"PMA mode enabled. {hint}",
        )

    async def _handle_pma_off(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            sink_store = PmaActiveSinkStore(self._config.root)
            sink = sink_store.load()
            if (
                sink is not None
                and sink.get("platform") == "discord"
                and sink.get("chat_id") == channel_id
            ):
                sink_store.clear()
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA mode disabled. Back to repo mode.",
            )
            return

        prev_workspace = binding.get("pma_prev_workspace_path")
        prev_repo_id = binding.get("pma_prev_repo_id")

        await self._store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=False,
            pma_prev_workspace_path=None,
            pma_prev_repo_id=None,
        )

        sink_store = PmaActiveSinkStore(self._config.root)
        sink = sink_store.load()
        if (
            sink is not None
            and sink.get("platform") == "discord"
            and sink.get("chat_id") == channel_id
        ):
            sink_store.clear()

        if prev_workspace:
            await self._store.upsert_binding(
                channel_id=channel_id,
                guild_id=binding.get("guild_id"),
                workspace_path=prev_workspace,
                repo_id=prev_repo_id,
            )
            hint = f"Restored binding to {prev_workspace}."
        else:
            await self._store.delete_binding(channel_id=channel_id)
            hint = "Back to repo mode."

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"PMA mode disabled. {hint}",
        )

    async def _handle_pma_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            sink_store = PmaActiveSinkStore(self._config.root)
            sink = sink_store.load()
            active_here = (
                sink is not None
                and sink.get("platform") == "discord"
                and sink.get("chat_id") == channel_id
            )
            status = "enabled" if active_here else "disabled"
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "\n".join(
                    [
                        f"PMA mode: {status}",
                        "Current workspace: unbound",
                    ]
                ),
            )
            return

        pma_enabled = binding.get("pma_enabled", False)
        status = "enabled" if pma_enabled else "disabled"

        if pma_enabled:
            sink_store = PmaActiveSinkStore(self._config.root)
            sink = sink_store.load()
            active_here = (
                sink is not None
                and sink.get("platform") == "discord"
                and sink.get("chat_id") == channel_id
            )
            lines = [
                f"PMA mode: {status}",
                f"Active sink targeting this channel: {active_here}",
            ]
        else:
            workspace = binding.get("workspace_path", "unknown")
            lines = [
                f"PMA mode: {status}",
                f"Current workspace: {workspace}",
            ]

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(lines),
        )

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        try:
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
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to send ephemeral response: %s (interaction_id=%s)",
                exc,
                interaction_id,
            )

    async def _respond_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={
                    "type": 4,
                    "data": {
                        "content": content,
                        "flags": DISCORD_EPHEMERAL_FLAG,
                        "components": components,
                    },
                },
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to send component response: %s (interaction_id=%s)",
                exc,
                interaction_id,
            )

    async def _handle_component_interaction(
        self, interaction_payload: dict[str, Any]
    ) -> None:
        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_component_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord interaction is not authorized.",
            )
            return

        custom_id = extract_component_custom_id(interaction_payload)
        if not custom_id:
            self._logger.debug(
                "handle_component_interaction: missing custom_id (interaction_id=%s)",
                interaction_id,
            )
            return

        try:
            if custom_id == "bind_select":
                values = extract_component_values(interaction_payload)
                if values:
                    await self._handle_bind_selection(
                        interaction_id,
                        interaction_token,
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                        selected_repo_id=values[0],
                    )
                return

            if custom_id == "flow_runs_select":
                values = extract_component_values(interaction_payload)
                if values:
                    workspace_root = await self._require_bound_workspace(
                        interaction_id, interaction_token, channel_id=channel_id
                    )
                    if workspace_root:
                        await self._handle_flow_status(
                            interaction_id,
                            interaction_token,
                            workspace_root=workspace_root,
                            options={"run_id": values[0]},
                        )
                return

            if custom_id.startswith("flow:"):
                workspace_root = await self._require_bound_workspace(
                    interaction_id, interaction_token, channel_id=channel_id
                )
                if workspace_root:
                    await self._handle_flow_button(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        custom_id=custom_id,
                    )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown component: {custom_id}",
            )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.component.unhandled_error",
                custom_id=custom_id,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
            )

    async def _handle_bind_selection(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        selected_repo_id: str,
    ) -> None:
        if selected_repo_id == "none":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No workspace selected.",
            )
            return

        repos = self._list_manifest_repos()
        matching = [(rid, path) for rid, path in repos if rid == selected_repo_id]
        if not matching:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Repo not found: {selected_repo_id}",
            )
            return

        _, workspace_path = matching[0]
        workspace = canonicalize_path(Path(workspace_path))
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
            repo_id=selected_repo_id,
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Bound this channel to: {selected_repo_id} ({workspace})",
        )

    async def _handle_flow_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        custom_id: str,
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 3:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid button action: {custom_id}",
            )
            return

        run_id = parts[1]
        action = parts[2]

        if action == "resume":
            controller = build_ticket_flow_controller(workspace_root)
            try:
                updated = await controller.resume_flow(run_id)
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
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
        elif action == "stop":
            controller = build_ticket_flow_controller(workspace_root)
            try:
                updated = await controller.stop_flow(run_id)
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Stop requested for run {updated.id} ({updated.status.value}).",
            )
        elif action == "archive":
            try:
                summary = archive_flow_run_artifacts(
                    workspace_root,
                    run_id=run_id,
                    force=False,
                    delete_run=False,
                )
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Archived run {summary['run_id']} (runs_archived={summary['archived_runs']}).",
            )
        elif action == "refresh":
            await self._handle_flow_status(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": run_id},
            )
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown action: {action}",
            )


def create_discord_bot_service(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
    manifest_path: Optional[Path] = None,
) -> DiscordBotService:
    return DiscordBotService(config, logger=logger, manifest_path=manifest_path)
