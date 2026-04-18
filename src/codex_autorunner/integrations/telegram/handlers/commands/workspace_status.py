from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....core.flows import FlowStore
from .....core.flows.models import FlowRunStatus
from .....manifest import load_manifest
from ....chat.status_diagnostics import (
    StatusBlockContext,
    build_process_monitor_lines_for_root,
    build_status_block_lines,
)
from ...adapter import TelegramMessage
from ...collaboration_helpers import (
    collaboration_summary_lines,
    evaluate_collaboration_summary,
)
from ...helpers import _approval_age_seconds, _format_token_usage

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord


def _telegram_status_base_lines(
    *,
    message: TelegramMessage,
    record: "TelegramTopicRecord",
    runtime: Any,
    command_policy: Any,
    plain_text_policy: Any,
) -> list[str]:
    workspace_label = record.workspace_path or (
        "hub" if record.pma_enabled else "unbound"
    )
    queue = getattr(runtime, "queue", None) if runtime is not None else None
    pending_queue = queue.pending() if queue is not None else 0
    lines: list[str] = []
    if record.pma_enabled:
        lines.append("Mode: PMA (hub)")
        if record.pma_prev_workspace_path:
            lines.append(f"Previous binding: {record.pma_prev_workspace_path}")
            lines.append("Use /pma off to restore previous binding.")
    elif record.workspace_path:
        lines.append("Mode: workspace")
        lines.append("Topic is bound.")
    lines.extend(
        [
            f"Workspace: {workspace_label}",
            f"Workspace ID: {record.workspace_id or 'unknown'}",
            f"Active thread: {record.active_thread_id or 'none'}",
            f"Active turn: {runtime.current_turn_id or 'none'}",
            f"Queued requests: {pending_queue}",
            *collaboration_summary_lines(
                message,
                command_result=command_policy,
                plain_text_result=plain_text_policy,
            ),
        ]
    )
    if pending_queue:
        lines.append("Queued messages include Cancel and Interrupt + Send buttons.")
    return lines


class WorkspaceStatusMixin:
    async def _handle_status(
        self, message: TelegramMessage, _args: str = "", runtime: Optional[Any] = None
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        await self._refresh_workspace_id(key, record)
        if runtime is None:
            runtime = self._router.runtime_for(key)
        approval_policy, sandbox_policy = self._effective_policies(record)
        agent = self._effective_agent(record)
        is_pma = bool(getattr(record, "pma_enabled", False))
        command_policy, plain_text_policy = evaluate_collaboration_summary(
            self,
            message,
            command_text="/status",
        )
        lines = _telegram_status_base_lines(
            message=message,
            record=record,
            runtime=runtime,
            command_policy=command_policy,
            plain_text_policy=plain_text_policy,
        )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        rate_limits = await self._read_rate_limits(record.workspace_path, agent=agent)
        lines.extend(
            build_status_block_lines(
                StatusBlockContext(
                    agent=agent,
                    resume=(
                        "supported"
                        if self._agent_supports_resume(agent)
                        else "unsupported"
                    ),
                    model=record.model or "default",
                    effort=effort_label,
                    approval_mode=record.approval_mode,
                    approval_policy=approval_policy or "default",
                    sandbox_policy=sandbox_policy,
                    rate_limits=rate_limits,
                )
            )
        )
        pending = await self._store.pending_approvals_for_key(key)
        if pending:
            lines.append(f"Pending approvals: {len(pending)}")
            if len(pending) == 1:
                age = _approval_age_seconds(pending[0].created_at)
                age_label = f"{age}s" if isinstance(age, int) else "unknown age"
                lines.append(f"Pending request: {pending[0].request_id} ({age_label})")
            else:
                preview = ", ".join(item.request_id for item in pending[:3])
                suffix = "" if len(pending) <= 3 else "..."
                lines.append(f"Pending requests: {preview}{suffix}")
        if record.summary:
            lines.append(f"Summary: {record.summary}")
        if record.active_thread_id:
            token_usage = self._token_usage_by_thread.get(record.active_thread_id)
            lines.extend(_format_token_usage(token_usage))

        manifest_path = getattr(self, "_manifest_path", None)
        hub_root = getattr(self, "_hub_root", None)
        if is_pma:
            if hub_root:
                lines.append(f"Hub root: {hub_root}")
            if manifest_path:
                lines.append(f"Manifest: {manifest_path}")
            registry = getattr(self, "_hub_thread_registry", None)
            if registry and hasattr(self, "_pma_registry_key"):
                try:
                    pma_key = self._pma_registry_key(record, message)
                    pma_thread_id = registry.get_thread_id(pma_key) if pma_key else None
                    require_topics = getattr(self._config, "require_topics", False)
                    scoping = "per-topic" if require_topics else "global (per hub)"
                    lines.append(f"PMA thread: {pma_thread_id or 'none'} ({scoping})")
                except (OSError, RuntimeError, ValueError, KeyError):
                    self._logger.debug(
                        "status: pma registry lookup failed", exc_info=True
                    )
            if hub_root:
                try:
                    pma_dir = hub_root / ".codex-autorunner" / "pma"
                    inbox_dir = pma_dir / "inbox"
                    outbox_dir = pma_dir / "outbox"
                    inbox_count = (
                        len(
                            [
                                path
                                for path in inbox_dir.iterdir()
                                if path.is_file() and not path.name.startswith(".")
                            ]
                        )
                        if inbox_dir.exists()
                        else 0
                    )
                    outbox_count = (
                        len(
                            [
                                path
                                for path in outbox_dir.iterdir()
                                if path.is_file() and not path.name.startswith(".")
                            ]
                        )
                        if outbox_dir.exists()
                        else 0
                    )
                    lines.append(
                        f"PMA files: inbox {inbox_count}, outbox {outbox_count}"
                    )
                except OSError:
                    self._logger.debug("status: pma file count failed", exc_info=True)
        if is_pma and manifest_path and hub_root:
            try:
                manifest = load_manifest(manifest_path, hub_root)
                enabled_repos = [repo for repo in manifest.repos if repo.enabled]
                lines.append(
                    f"Hub repos: {len(enabled_repos)}/{len(manifest.repos)} enabled"
                )
                active_count = 0
                paused_count = 0
                idle_count = 0
                active_repos: list[str] = []
                paused_repos: list[str] = []
                for repo in manifest.repos:
                    if not repo.enabled:
                        continue
                    repo_root = (hub_root / repo.path).resolve()
                    db_path = repo_root / ".codex-autorunner" / "flows.db"
                    if not db_path.exists():
                        idle_count += 1
                        continue

                    store = FlowStore(db_path)
                    try:
                        store.initialize()
                        runs = store.list_flow_runs(flow_type="ticket_flow")
                        if runs:
                            latest = runs[0]
                            if latest.status.is_active():
                                active_count += 1
                                active_repos.append(repo.id)
                            elif latest.status == FlowRunStatus.PAUSED:
                                paused_count += 1
                                paused_repos.append(repo.id)
                            else:
                                idle_count += 1
                        else:
                            idle_count += 1
                    except (OSError, RuntimeError, ValueError):
                        self._logger.debug(
                            "status: flow store query failed for repo", exc_info=True
                        )
                    finally:
                        store.close()
                lines.append(
                    f"Hub flows: {active_count} active, {paused_count} paused, {idle_count} idle"
                )
                if active_repos:
                    preview = ", ".join(active_repos[:5])
                    suffix = "" if len(active_repos) <= 5 else "..."
                    lines.append(f"Active repos: {preview}{suffix}")
                if paused_repos:
                    preview = ", ".join(paused_repos[:5])
                    suffix = "" if len(paused_repos) <= 5 else "..."
                    lines.append(f"Paused repos: {preview}{suffix}")
            except Exception:
                self._logger.debug(
                    "status: hub repo status aggregation failed", exc_info=True
                )
        lines.extend(
            build_process_monitor_lines_for_root(
                self._process_monitor_root(record),
                include_history=False,
            )
        )

        if not record.workspace_path and not is_pma:
            lines.append("Use /bind <repo_id> or /bind <path>.")

        if record.workspace_path and not is_pma:
            repo_root = Path(record.workspace_path)
            db_path = repo_root / ".codex-autorunner" / "flows.db"
            if db_path.exists():
                store = FlowStore(db_path)
                try:
                    store.initialize()
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                    if runs:
                        latest = runs[0]
                        if latest.status.is_active():
                            lines.append(
                                f"Active Flow: {latest.status.value} (run {latest.id})"
                            )
                        elif latest.status == FlowRunStatus.PAUSED:
                            lines.append(f"Active Flow: PAUSED (run {latest.id})")
                except (OSError, RuntimeError, ValueError):
                    self._logger.debug(
                        "status: flow store query failed for workspace", exc_info=True
                    )
                finally:
                    store.close()

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_processes(
        self, message: TelegramMessage, _args: str = "", _runtime: Optional[Any] = None
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        root = self._process_monitor_root(record, allow_fallback=True)
        if root is None:
            await self._send_message(
                message.chat_id,
                "Process monitor unavailable; no workspace or hub root is bound.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        lines = [f"Process monitor root: {root}"]
        lines.extend(
            build_process_monitor_lines_for_root(root, include_history=True)
            or ["Process monitor unavailable."]
        )
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
