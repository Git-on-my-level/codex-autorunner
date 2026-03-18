from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .config import load_repo_config
from .lifecycle_events import LifecycleEvent, LifecycleEventType
from .pma_chat_delivery import (
    notify_preferred_bound_chat_for_workspace,
    notify_primary_pma_chat_for_repo,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchInterceptRule:
    pattern: str
    reply: str
    description: str = ""


@dataclass(frozen=True)
class InterceptionResult:
    action: str
    reason: str
    reply: Optional[str] = None
    escalated: bool = False
    notified: bool = False


def default_dispatch_rules() -> list[DispatchInterceptRule]:
    return [
        DispatchInterceptRule(
            pattern=r"(?i)^(proceed|continue|resume|ok|yes|go ahead)([\s!.]*)?$",
            reply="Continuing with the current task.",
            description="Simple continuation confirmation",
        ),
        DispatchInterceptRule(
            pattern=r"(?i)^skip.*(?:pre.?commit|commit.*hook|hook)([\s!.]*)?$",
            reply="Skipping pre-commit hooks and proceeding.",
            description="Skip pre-commit hooks",
        ),
    ]


def _match_dispatch(
    body: str, rules: list[DispatchInterceptRule]
) -> Optional[DispatchInterceptRule]:
    body_stripped = body.strip()
    for rule in rules:
        try:
            if re.match(rule.pattern, body_stripped):
                return rule
        except re.error:
            logger.warning("Invalid dispatch pattern: %s", rule.pattern)
            continue
    return None


async def _can_auto_resume_run(repo_root: Path, run_id: str) -> bool:
    from .flows.models import FlowRunStatus
    from .flows.store import FlowStore

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return False
    try:
        config = load_repo_config(repo_root)
        with FlowStore(db_path, durable=config.durable_writes) as store:
            run = store.get_flow_run(run_id)
            if not run:
                return False
            return run.status == FlowRunStatus.PAUSED
    except Exception as exc:
        logger.warning("Failed to check run status for %s: %s", run_id, exc)
        return False


async def _write_reply(repo_root: Path, run_id: str, reply_body: str) -> bool:
    from ..tickets.replies import ensure_reply_dirs, resolve_reply_paths

    try:
        workspace_root = repo_root

        reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=run_id)
        ensure_reply_dirs(reply_paths)

        reply_history_dir = reply_paths.reply_history_dir
        if not reply_history_dir:
            logger.warning("Reply history dir not found for run %s", run_id)
            return False

        next_seq = 1
        for child in reply_history_dir.iterdir():
            if child.is_dir() and child.name.isdigit():
                next_seq = max(next_seq, int(child.name) + 1)

        seq_dir = reply_history_dir / f"{next_seq:04d}"
        seq_dir.mkdir(parents=True, exist_ok=True)

        reply_content = f"""---
title: Auto-resolved by PMA
---

{reply_body}
"""
        reply_path = seq_dir / "USER_REPLY.md"
        reply_path.write_text(reply_content, encoding="utf-8")

        return True

    except Exception:
        logger.exception("Failed to write reply for run %s", run_id)
        return False


class PmaDispatchInterceptor:
    def __init__(
        self,
        hub_root: Path,
        supervisor: Any,
        rules: Optional[list[DispatchInterceptRule]] = None,
        on_intercept: Optional[Callable[[str, InterceptionResult], None]] = None,
    ):
        self._hub_root = hub_root
        self._supervisor = supervisor
        self._rules = rules or default_dispatch_rules()
        self._on_intercept = on_intercept
        self._processing = set[str]()
        self._logger = logging.getLogger(f"{__name__}.PmaDispatchInterceptor")

    async def process_dispatch_event(
        self, event: LifecycleEvent, repo_root: Path
    ) -> Optional[InterceptionResult]:
        if event.event_type != LifecycleEventType.DISPATCH_CREATED:
            return None

        event_id = event.event_id
        if event_id in self._processing:
            return None

        run_id = event.run_id
        repo_id = event.data.get("repo_id") or event.repo_id

        try:
            self._processing.add(event_id)

            self._logger.info(
                "Processing dispatch event %s for run %s in repo %s",
                event_id,
                run_id,
                repo_id,
            )

            from .pma_context import _latest_dispatch

            latest = _latest_dispatch(repo_root, run_id, {}, max_text_chars=10000)
            if not latest or not latest.get("dispatch"):
                return InterceptionResult(
                    action="ignore",
                    reason="No dispatch found or dispatch is invalid",
                )

            dispatch_info = latest["dispatch"]
            mode = dispatch_info.get("mode")
            body = dispatch_info.get("body", "")

            if mode != "pause":
                return InterceptionResult(
                    action="ignore",
                    reason=f"Dispatch mode is '{mode}', only 'pause' is intercepted",
                )

            matched_rule = _match_dispatch(body, self._rules)
            if matched_rule:
                self._logger.info(
                    "Dispatch matched auto-resolve rule: %s",
                    matched_rule.description,
                )

                can_resume = await _can_auto_resume_run(repo_root, run_id)
                if not can_resume:
                    notified = await self._notify_escalation(
                        event_id=event_id,
                        repo_root=repo_root,
                        repo_id=repo_id,
                        run_id=run_id,
                        reason=(
                            "PMA could not resolve the paused ticket-flow dispatch "
                            "automatically because the run cannot be resumed safely."
                        ),
                    )
                    return InterceptionResult(
                        action="escalate",
                        reason="Run cannot be auto-resumed",
                        escalated=True,
                        notified=notified,
                    )

                success = await _write_reply(repo_root, run_id, matched_rule.reply)
                if success:
                    notified = await self._notify_auto_resolved(
                        event_id=event_id,
                        repo_root=repo_root,
                        repo_id=repo_id,
                        run_id=run_id,
                        reply=matched_rule.reply,
                    )
                    result = InterceptionResult(
                        action="auto_resolved",
                        reason=matched_rule.description,
                        reply=matched_rule.reply,
                        notified=notified,
                    )
                    if self._on_intercept:
                        self._on_intercept(event_id, result)
                    return result
                else:
                    notified = await self._notify_escalation(
                        event_id=event_id,
                        repo_root=repo_root,
                        repo_id=repo_id,
                        run_id=run_id,
                        reason=(
                            "PMA matched the paused dispatch but failed to persist the "
                            "auto-reply, so the dispatch was escalated for user attention."
                        ),
                    )
                    return InterceptionResult(
                        action="escalate",
                        reason="Failed to write reply",
                        escalated=True,
                        notified=notified,
                    )

            notified = await self._notify_escalation(
                event_id=event_id,
                repo_root=repo_root,
                repo_id=repo_id,
                run_id=run_id,
                reason=(
                    "PMA could not auto-resolve the paused ticket-flow dispatch and "
                    "escalated it for user attention."
                ),
            )
            return InterceptionResult(
                action="escalate",
                reason="Dispatch does not match any auto-resolve rule",
                escalated=True,
                notified=notified,
            )

        except Exception as exc:
            self._logger.exception(
                "Error processing dispatch event %s for run %s", event_id, run_id
            )
            return InterceptionResult(
                action="error",
                reason=f"Processing error: {exc}",
            )
        finally:
            self._processing.discard(event_id)

    async def _notify_auto_resolved(
        self,
        *,
        event_id: str,
        repo_root: Path,
        repo_id: Optional[str],
        run_id: str,
        reply: str,
    ) -> bool:
        correlation_id = f"dispatch-auto:{event_id}"
        message = (
            "PMA handled the paused ticket-flow dispatch automatically.\n"
            f"run_id: {run_id}\n\n"
            f"{reply.strip()}\n\n"
            "No user response is needed."
        )
        try:
            outcome = await notify_preferred_bound_chat_for_workspace(
                hub_root=self._hub_root,
                workspace_root=repo_root,
                repo_id=repo_id,
                message=message,
                correlation_id=correlation_id,
            )
        except Exception:
            self._logger.exception(
                "Failed to notify bound chat for auto-resolved dispatch run_id=%s",
                run_id,
            )
            return False
        return int(outcome.get("published", 0)) > 0

    async def _notify_escalation(
        self,
        *,
        event_id: str,
        repo_root: Path,
        repo_id: Optional[str],
        run_id: str,
        reason: str,
    ) -> bool:
        correlation_id = f"dispatch-escalation:{event_id}"
        message = (
            "PMA escalated a paused ticket-flow dispatch for user attention.\n"
            f"run_id: {run_id}\n"
            f"repo: {repo_id or repo_root}\n\n"
            f"Reason: {reason.strip()}\n\n"
            "If the completed ticket leaves the repo dirty or ownership is ambiguous, "
            "the flow should stop and ask the user instead of guessing."
        )
        try:
            outcome = await notify_primary_pma_chat_for_repo(
                hub_root=self._hub_root,
                repo_id=repo_id,
                message=message,
                correlation_id=correlation_id,
            )
        except Exception:
            self._logger.exception(
                "Failed to notify PMA chat for escalated dispatch run_id=%s",
                run_id,
            )
            return False
        return int(outcome.get("published", 0)) > 0


__all__ = [
    "DispatchInterceptRule",
    "InterceptionResult",
    "default_dispatch_rules",
    "PmaDispatchInterceptor",
]
