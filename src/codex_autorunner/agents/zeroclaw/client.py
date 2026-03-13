from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional, Sequence

from ...core.sse import format_sse
from ...core.utils import subprocess_env
from ..types import TerminalTurnResult

_STARTUP_TIMEOUT_SECONDS = 30.0
_PROMPT_SUFFIXES = ("\r\n> ", "\n> ", "> ")
_AUTO_COMPACT_LINE = "🧹 Auto-compaction complete"


class ZeroClawClientError(RuntimeError):
    """Raised when the ZeroClaw wrapper client cannot complete an operation."""


def split_zeroclaw_model(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split `provider/model` IDs into the CLI's separate flags when possible."""

    if not model:
        return None, None
    text = str(model).strip()
    if not text:
        return None, None
    if "/" not in text:
        return None, text
    provider, model_name = text.split("/", 1)
    provider = provider.strip()
    model_name = model_name.strip()
    if not provider or not model_name:
        return None, text
    return provider, model_name


def _strip_prompt_suffix(text: str) -> tuple[str, bool]:
    for suffix in sorted(_PROMPT_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            return text[: -len(suffix)], True
    return text, False


def _clean_terminal_output(text: str) -> str:
    cleaned_lines = [
        line
        for line in text.replace("\r\n", "\n").splitlines()
        if line.strip() != _AUTO_COMPACT_LINE
    ]
    return "\n".join(cleaned_lines).strip()


@dataclass
class ZeroClawTurnState:
    turn_id: str
    started_at: float = field(default_factory=time.monotonic)
    raw_buffer: str = ""
    visible_length: int = 0
    published_events: list[str] = field(default_factory=list)
    subscribers: list[asyncio.Queue[Optional[str]]] = field(default_factory=list)
    result: asyncio.Future[TerminalTurnResult] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class ZeroClawClient:
    """Wrapper around ZeroClaw's documented interactive CLI agent surface."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        workspace_root: Path,
        logger: Optional[logging.Logger] = None,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not command:
            raise ValueError("ZeroClaw command must not be empty")
        self._command = [str(part) for part in command]
        self._workspace_root = workspace_root
        self._logger = logger or logging.getLogger(__name__)
        self._base_env = base_env
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._ready = asyncio.get_running_loop().create_future()
        self._stderr_chunks: list[str] = []
        self._launch_provider: Optional[str] = None
        self._launch_model: Optional[str] = None
        self._active_turn: Optional[ZeroClawTurnState] = None
        self._turns: dict[str, ZeroClawTurnState] = {}

    async def ensure_ready(
        self,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        if self._process is None:
            await self._start_process(provider=provider, model=model)
        elif provider is not None and provider != self._launch_provider:
            raise ZeroClawClientError(
                "ZeroClaw session provider is fixed after the first turn"
            )
        elif model is not None and model != self._launch_model:
            raise ZeroClawClientError(
                "ZeroClaw session model is fixed after the first turn"
            )
        await asyncio.wait_for(self._ready, timeout=_STARTUP_TIMEOUT_SECONDS)

    async def start_turn(
        self,
        prompt: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        await self.ensure_ready(provider=provider, model=model)
        if self._active_turn is not None and not self._active_turn.result.done():
            raise ZeroClawClientError("ZeroClaw session already has an active turn")
        if self._process is None or self._process.stdin is None:
            raise ZeroClawClientError("ZeroClaw process is not available")
        turn = ZeroClawTurnState(turn_id=f"zeroclaw-turn-{int(time.time() * 1000)}")
        self._active_turn = turn
        self._turns[turn.turn_id] = turn
        self._process.stdin.write(prompt.encode("utf-8"))
        self._process.stdin.write(b"\n")
        await self._process.stdin.drain()
        return turn.turn_id

    async def wait_for_turn(
        self,
        turn_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise ZeroClawClientError(f"Unknown ZeroClaw turn '{turn_id}'")
        if timeout is None:
            return await turn.result
        return await asyncio.wait_for(turn.result, timeout=timeout)

    async def stream_turn_events(self, turn_id: str) -> AsyncIterator[str]:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise ZeroClawClientError(f"Unknown ZeroClaw turn '{turn_id}'")
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        for event in turn.published_events:
            queue.put_nowait(event)
        if turn.result.done():
            queue.put_nowait(None)
        else:
            turn.subscribers.append(queue)
        while True:
            next_event: Optional[str] = await queue.get()
            if next_event is None:
                break
            yield next_event

    async def close(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._stdout_task = None
        self._stderr_task = None

    async def _start_process(
        self,
        *,
        provider: Optional[str],
        model: Optional[str],
    ) -> None:
        launch_command = [*self._command, "agent"]
        if provider:
            launch_command.extend(["--provider", provider])
        if model:
            launch_command.extend(["--model", model])
        env = subprocess_env(base_env=self._base_env)
        self._process = await asyncio.create_subprocess_exec(
            *launch_command,
            cwd=str(self._workspace_root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._launch_provider = provider
        self._launch_model = model
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        startup_buffer = ""
        while True:
            chunk = await self._process.stdout.read(512)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if not self._ready.done():
                startup_buffer += text
                startup_buffer, prompt_found = _strip_prompt_suffix(startup_buffer)
                if prompt_found:
                    self._ready.set_result(None)
                continue
            await self._ingest_turn_output(text)
        await self._finalize_process_exit()

    async def _read_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while True:
            chunk = await self._process.stderr.read(512)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if text:
                self._stderr_chunks.append(text)

    async def _ingest_turn_output(self, text: str) -> None:
        turn = self._active_turn
        if turn is None:
            return
        turn.raw_buffer += text
        visible, prompt_found = _strip_prompt_suffix(turn.raw_buffer)
        delta = visible[turn.visible_length :]
        if delta:
            event = format_sse(
                "zeroclaw",
                {"message": {"method": "message.delta", "params": {"text": delta}}},
            )
            turn.published_events.append(event)
            for subscriber in list(turn.subscribers):
                subscriber.put_nowait(event)
            turn.visible_length = len(visible)
        if prompt_found:
            terminal_text = _clean_terminal_output(visible)
            completed_event = format_sse(
                "zeroclaw",
                {
                    "message": {
                        "method": "message.completed",
                        "params": {"text": terminal_text},
                    }
                },
            )
            turn.published_events.append(completed_event)
            for subscriber in list(turn.subscribers):
                subscriber.put_nowait(completed_event)
            result = TerminalTurnResult(
                status="completed",
                assistant_text=terminal_text,
                errors=[],
            )
            if not turn.result.done():
                turn.result.set_result(result)
            self._close_turn(turn.turn_id)

    async def _finalize_process_exit(self) -> None:
        if self._process is not None and self._process.returncode is None:
            await self._process.wait()
        if not self._ready.done():
            error = "".join(self._stderr_chunks).strip() or "ZeroClaw failed to start"
            self._ready.set_exception(ZeroClawClientError(error))
        turn = self._active_turn
        if turn is None or turn.result.done():
            return
        error_text = "".join(self._stderr_chunks).strip()
        terminal_text = _clean_terminal_output(turn.raw_buffer)
        result = TerminalTurnResult(
            status="error",
            assistant_text=terminal_text,
            errors=(
                [error_text] if error_text else ["ZeroClaw session exited unexpectedly"]
            ),
        )
        turn.result.set_result(result)
        self._close_turn(turn.turn_id)

    def _close_turn(self, turn_id: str) -> None:
        turn = self._turns.get(turn_id)
        if turn is None:
            return
        for subscriber in list(turn.subscribers):
            subscriber.put_nowait(None)
        turn.subscribers.clear()
        if self._active_turn is turn:
            self._active_turn = None


__all__ = [
    "ZeroClawClient",
    "ZeroClawClientError",
    "split_zeroclaw_model",
]
