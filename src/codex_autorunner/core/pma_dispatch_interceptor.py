from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .config import load_repo_config
from .lifecycle_events import LifecycleEvent, LifecycleEventType

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
        config = load_repo_config(repo_root)
        runs_dir = Path(config.raw.get("runs_dir") or ".codex-autorunner/runs")
        workspace_root = repo_root

        reply_paths = resolve_reply_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
        )
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
                    return InterceptionResult(
                        action="escalate",
                        reason="Run cannot be auto-resumed",
                        escalated=True,
                    )

                success = await _write_reply(repo_root, run_id, matched_rule.reply)
                if success:
                    result = InterceptionResult(
                        action="auto_resolved",
                        reason=matched_rule.description,
                        reply=matched_rule.reply,
                    )
                    if self._on_intercept:
                        self._on_intercept(event_id, result)
                    return result
                else:
                    return InterceptionResult(
                        action="escalate",
                        reason="Failed to write reply",
                        escalated=True,
                    )

            return InterceptionResult(
                action="escalate",
                reason="Dispatch does not match any auto-resolve rule",
                escalated=True,
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


__all__ = [
    "DispatchInterceptRule",
    "InterceptionResult",
    "default_dispatch_rules",
    "PmaDispatchInterceptor",
]
