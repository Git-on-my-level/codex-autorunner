from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .runtime import (
    PERMISSION_ALLOW,
    build_turn_id,
    collect_opencode_output,
    extract_session_id,
    opencode_missing_env,
    split_model_id,
)
from .supervisor import OpenCodeSupervisor


@dataclass(frozen=True)
class OpenCodeRunResult:
    session_id: str
    turn_id: str
    output_text: str
    output_error: Optional[str]
    stopped: bool
    timed_out: bool


@dataclass
class OpenCodeRunConfig:
    agent: str
    model: Optional[str]
    reasoning: Optional[str]
    prompt: str
    workspace_root: str
    timeout_seconds: int = 3600
    interrupt_grace_seconds: int = 10
    on_turn_start: Optional[Callable[[str, str], Awaitable[None]]] = None
    permission_policy: str = PERMISSION_ALLOW


async def run_opencode_prompt(
    supervisor: OpenCodeSupervisor,
    config: OpenCodeRunConfig,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
    logger: Optional[logging.Logger] = None,
) -> OpenCodeRunResult:
    client = await supervisor.get_client(Path(config.workspace_root))

    session_id: Optional[str] = None
    try:
        session = await client.create_session(directory=config.workspace_root)
        session_id = extract_session_id(session, allow_fallback_id=True)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("OpenCode did not return a session id")
    except Exception as exc:
        raise RuntimeError(f"Failed to create OpenCode session: {exc}") from exc

    model_payload = split_model_id(config.model)
    missing_env = await opencode_missing_env(
        client, config.workspace_root, model_payload
    )
    if missing_env:
        provider_id = model_payload.get("providerID") if model_payload else None
        missing_label = ", ".join(missing_env)
        raise RuntimeError(
            f"OpenCode provider {provider_id or 'selected'} requires env vars: {missing_label}"
        )

    opencode_turn_started = False
    await supervisor.mark_turn_started(Path(config.workspace_root))
    opencode_turn_started = True
    turn_id = build_turn_id(session_id)

    if config.on_turn_start is not None:
        try:
            await config.on_turn_start(session_id, turn_id)
        except Exception:
            pass

    stopped = False
    timed_out = False
    output_result = None

    permission_policy = config.permission_policy or PERMISSION_ALLOW
    output_task = asyncio.create_task(
        collect_opencode_output(
            client,
            session_id=session_id,
            workspace_path=config.workspace_root,
            permission_policy=permission_policy,
            should_stop=should_stop,
        )
    )
    prompt_task = asyncio.create_task(
        client.prompt(
            session_id,
            message=config.prompt,
            model=model_payload,
            variant=config.reasoning,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(config.timeout_seconds))

    try:
        try:
            await prompt_task
        except Exception as exc:
            if logger is not None:
                logger.error(f"OpenCode prompt failed: {exc}")
            output_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await output_task
            raise RuntimeError(f"OpenCode prompt failed: {exc}") from exc

        tasks = {output_task, timeout_task}
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        if timeout_task in done:
            output_task.add_done_callback(lambda task: task.exception())
            timed_out = True
            if logger is not None:
                logger.warning("OpenCode prompt timed out")

        output_result = await output_task

    finally:
        timeout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_task
        if opencode_turn_started:
            try:
                await supervisor.mark_turn_finished(Path(config.workspace_root))
            except Exception:
                pass

    output_text = output_result.text if output_result else ""
    output_error = output_result.error if output_result else None

    return OpenCodeRunResult(
        session_id=session_id,
        turn_id=turn_id,
        output_text=output_text,
        output_error=output_error,
        stopped=stopped,
        timed_out=timed_out,
    )


__all__ = [
    "OpenCodeRunResult",
    "OpenCodeRunConfig",
    "run_opencode_prompt",
]
