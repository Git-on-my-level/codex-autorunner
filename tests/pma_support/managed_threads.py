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
