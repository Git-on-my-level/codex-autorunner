from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .runtime import (
    PERMISSION_ALLOW,
    OpenCodeTurnOutput,
    build_turn_id,
    collect_opencode_output,
    extract_session_id,
    opencode_missing_env,
    parse_message_response,
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
    output_result: Optional[OpenCodeTurnOutput] = None

    stop_task = None
    if should_stop is not None:

        async def _wait_for_stop() -> bool:
            while True:
                if should_stop():
                    return True
                await asyncio.sleep(0.2)

        stop_task = asyncio.create_task(_wait_for_stop())

    async def _abort_session(reason: str) -> None:
        try:
            await client.abort(session_id)
        except Exception as exc:
            if logger is not None:
                logger.warning(f"OpenCode abort failed ({reason}): {exc}")

    permission_policy = config.permission_policy or PERMISSION_ALLOW
    ready_event = asyncio.Event()
    output_task = asyncio.create_task(
        collect_opencode_output(
            client,
            session_id=session_id,
            workspace_path=config.workspace_root,
            model_payload=model_payload,
            permission_policy=permission_policy,
            should_stop=should_stop,
            ready_event=ready_event,
        )
    )
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
    prompt_task = asyncio.create_task(
        client.prompt_async(
            session_id,
            message=config.prompt,
            model=model_payload,
            variant=config.reasoning,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(config.timeout_seconds))

    try:

        async def _finish_output(
            ignore_errors: bool,
        ) -> Optional[OpenCodeTurnOutput]:
            if output_task.done():
                try:
                    return await output_task
                except Exception as exc:
                    if not ignore_errors:
                        raise
                    if logger is not None:
                        logger.warning(f"OpenCode output failed after interrupt: {exc}")
                    return None

            grace_seconds = max(0, config.interrupt_grace_seconds or 0)
            if grace_seconds <= 0:
                output_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await output_task
                return None

            try:
                return await asyncio.wait_for(output_task, timeout=grace_seconds)
            except asyncio.TimeoutError:
                output_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await output_task
                if logger is not None:
                    logger.warning("OpenCode output did not stop within grace period")
                return None
            except Exception as exc:
                if not ignore_errors:
                    raise
                if logger is not None:
                    logger.warning(f"OpenCode output failed after interrupt: {exc}")
                return None

        tasks = {output_task, prompt_task, timeout_task}
        if stop_task is not None:
            tasks.add(stop_task)

        while True:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            if output_task in done:
                output_result = await output_task
                if should_stop is not None and should_stop():
                    stopped = True
                break

            if stop_task is not None and stop_task in done:
                stopped = True
                if logger is not None:
                    logger.info("OpenCode prompt stopped")
                await _abort_session("stop")
                output_result = await _finish_output(ignore_errors=True)
                break

            if timeout_task in done:
                timed_out = True
                if logger is not None:
                    logger.warning("OpenCode prompt timed out")
                await _abort_session("timeout")
                output_result = await _finish_output(ignore_errors=True)
                break

            if prompt_task in done:
                try:
                    await prompt_task
                except Exception as exc:
                    if logger is not None:
                        logger.error(f"OpenCode prompt failed: {exc}")
                    output_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await output_task
                    raise RuntimeError(f"OpenCode prompt failed: {exc}") from exc
                tasks.discard(prompt_task)
                tasks = pending

    finally:
        timeout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_task
        if stop_task is not None:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
        if not prompt_task.done():
            prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prompt_task
        if opencode_turn_started:
            try:
                await supervisor.mark_turn_finished(Path(config.workspace_root))
            except Exception:
                pass

    output_text = output_result.text if output_result else ""
    output_error = output_result.error if output_result else None
    if prompt_task.done() and not output_text:
        try:
            prompt_response = prompt_task.result()
        except Exception:
            prompt_response = None
        if prompt_response is not None:
            fallback = parse_message_response(prompt_response)
            if fallback.text:
                output_text = fallback.text
            if fallback.error and not output_error:
                output_error = fallback.error

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
