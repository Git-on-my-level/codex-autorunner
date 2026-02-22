"""Shared startup bootstrap helpers for chat surfaces.

This module provides a platform-agnostic way to run startup steps with
consistent observability and required/optional failure handling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from ...core.logging_utils import log_event

BootstrapAction = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ChatBootstrapStep:
    """A single startup step for a chat surface."""

    name: str
    action: BootstrapAction
    required: bool = True


async def run_chat_bootstrap_steps(
    *,
    platform: str,
    logger: logging.Logger,
    steps: Iterable[ChatBootstrapStep],
) -> None:
    """Run ordered startup steps with consistent logging and failure policy."""

    for step in steps:
        try:
            await step.action()
        except Exception as exc:
            level = logging.ERROR if step.required else logging.WARNING
            log_event(
                logger,
                level,
                f"{platform}.bootstrap.step_failed",
                step=step.name,
                required=step.required,
                exc=exc,
            )
            if step.required:
                raise
        else:
            log_event(
                logger,
                logging.INFO,
                f"{platform}.bootstrap.step_ok",
                step=step.name,
                required=step.required,
            )
