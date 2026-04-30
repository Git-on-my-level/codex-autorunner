from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class FakeTurnHandle:
    def __init__(
        self,
        turn_id: str = "backend-turn-1",
        assistant_text: str = "assistant-output",
        blocker: asyncio.Event | None = None,
    ) -> None:
        self.turn_id = turn_id
        self._assistant_text = assistant_text
        self._blocker = blocker

    async def wait(self, timeout=None):
        _ = timeout
        if self._blocker is not None:
            await self._blocker.wait()
        return type(
            "Result",
            (),
            {
                "agent_messages": [self._assistant_text],
                "raw_events": [],
                "errors": [],
            },
        )()


class FakeClient:
    def __init__(
        self,
        *,
        sequential: bool = False,
        turn_error: Exception | None = None,
        blocker: asyncio.Event | None = None,
    ) -> None:
        self._sequential = sequential
        self._turn_error = turn_error
        self._blocker = blocker
        self.turn_start_calls: list[dict[str, Any]] = []
        self.thread_start_roots: list[str] = []
        self.resume_calls: list[str] = []
        self._turn_count = 0
        self._thread_seq = 0

    async def thread_resume(self, thread_id: str) -> None:
        self.resume_calls.append(thread_id)

    async def thread_start(self, root: str) -> dict:
        self._thread_seq += 1
        self.thread_start_roots.append(root)
        if self._sequential:
            return {"id": f"backend-thread-{self._thread_seq}"}
        return {"id": "backend-thread-1"}

    async def turn_start(
        self,
        thread_id: str,
        prompt: str,
        approval_policy: str,
        sandbox_policy: str,
        **turn_kwargs,
    ):
        if self._turn_error is not None:
            raise self._turn_error
        self._turn_count += 1
        self.turn_start_calls.append(
            {
                "thread_id": thread_id,
                "prompt": prompt,
                "approval_policy": approval_policy,
                "sandbox_policy": sandbox_policy,
                "turn_kwargs": dict(turn_kwargs),
            }
        )
        if self._sequential:
            return FakeTurnHandle(
                turn_id=f"backend-turn-{self._turn_count}",
                assistant_text=f"assistant-output-{self._turn_count}",
                blocker=self._blocker,
            )
        return FakeTurnHandle(blocker=self._blocker)

    async def turn_interrupt(
        self, turn_id: str, *, thread_id: str | None = None
    ) -> None:
        _ = turn_id, thread_id
        raise RuntimeError("backend interrupt exploded")


class FakeSupervisor:
    def __init__(self, client: FakeClient | None = None) -> None:
        self.client = client or FakeClient()
        self.workspace_roots: list[Path] = []

    async def get_client(self, hub_root: Path):
        self.workspace_roots.append(hub_root)
        return self.client


class FakeAutomationStore:
    def __init__(self) -> None:
        self.transitions: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, Any]] = []

    def notify_transition(self, payload: dict[str, Any]) -> None:
        self.transitions.append(dict(payload))

    def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        subscription = dict(payload)
        subscription.setdefault("subscription_id", "sub-1")
        subscription.setdefault("thread_id", payload.get("thread_id"))
        self.subscriptions.append(subscription)
        return {"subscription": subscription}


def install_fake_supervisor(
    app,
    *,
    client: FakeClient | None = None,
) -> FakeSupervisor:
    supervisor = FakeSupervisor(client)
    app.state.app_server_supervisor = supervisor
    app.state.app_server_events = object()
    return supervisor


class FakeZeroClawSupervisor:
    def __init__(
        self,
        *,
        sequential_sessions: bool = False,
        block_on_wait: bool = False,
    ) -> None:
        self.create_calls: list[tuple[Path, str | None]] = []
        self.attach_calls: list[tuple[Path, str]] = []
        self.turn_calls: list[dict[str, object]] = []
        self.wait_started: list[str] = []
        self._session_seq = 0
        self._sequential_sessions = sequential_sessions
        self._block_on_wait = block_on_wait
        self._session_events: dict[str, asyncio.Event] = {}

    async def create_session(
        self, workspace_root: Path, title: str | None = None
    ) -> str:
        self.create_calls.append((workspace_root, title))
        if self._sequential_sessions:
            self._session_seq += 1
            session_id = f"zeroclaw-session-{self._session_seq}"
        else:
            session_id = "zeroclaw-session-1"
        self._session_events[session_id] = asyncio.Event()
        return session_id

    async def attach_session(self, workspace_root: Path, session_id: str) -> str:
        self.attach_calls.append((workspace_root, session_id))
        return session_id

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> str:
        self.turn_calls.append(
            {
                "workspace_root": workspace_root,
                "conversation_id": conversation_id,
                "prompt": prompt,
                "model": model,
            }
        )
        if self._sequential_sessions:
            return f"{conversation_id}-turn-{len(self.turn_calls)}"
        return f"zeroclaw-turn-{len(self.turn_calls)}"

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: str,
        *,
        timeout: float | None = None,
    ):
        _ = workspace_root, turn_id, timeout
        if self._block_on_wait:
            self.wait_started.append(conversation_id)
            await self._session_events[conversation_id].wait()
            return type(
                "Result",
                (),
                {
                    "status": "ok",
                    "assistant_text": f"zeroclaw-output:{conversation_id}",
                    "raw_events": [],
                    "errors": [],
                },
            )()
        return type(
            "Result",
            (),
            {
                "status": "ok",
                "assistant_text": f"zeroclaw-output-{len(self.turn_calls)}",
                "raw_events": [],
                "errors": [],
            },
        )()

    async def stream_turn_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        if False:
            yield None


class FakeZeroClawEventSupervisor:
    def __init__(
        self,
        *,
        events: list[dict[str, str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._events = events or []
        self._error = error

    async def list_turn_events(
        self, workspace_root: Path, session_id: str, turn_id: str
    ) -> list[dict[str, str]]:
        _ = workspace_root, session_id, turn_id
        if self._error is not None:
            raise self._error
        return list(self._events)

    async def list_turn_events_by_turn_id(self, turn_id: str) -> list[dict[str, Any]]:
        return await self.list_turn_events(Path("."), "zeroclaw-session-1", turn_id)
