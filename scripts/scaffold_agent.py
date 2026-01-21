#!/usr/bin/env python3
"""
Scaffold a new agent harness and supervisor for Codex Autorunner.

Usage:
    python scripts/scaffold_agent.py myagent "My Agent"
"""

import argparse
import keyword
import sys
from pathlib import Path


def validate_agent_name(agent_name: str) -> str:
    normalized = agent_name.lower().strip()

    if ".." in normalized or "/" in normalized or "\\" in normalized:
        raise ValueError("Agent name cannot contain path separators or '..'")

    if not normalized.isidentifier():
        raise ValueError(f"'{agent_name}' is not a valid Python identifier")

    if normalized in keyword.kwlist:
        raise ValueError(f"'{agent_name}' is a Python keyword and cannot be used")

    return normalized


def create_harness(agent_name: str, display_name: str) -> str:
    """Generate harness.py content."""
    return f'''from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.app_server_events import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ..base import AgentHarness
from ..types import AgentId, ConversationRef, ModelCatalog, ModelSpec, TurnRef


class {agent_name.capitalize()}Harness(AgentHarness):
    agent_id: AgentId = AgentId("{agent_name}")
    display_name = "{display_name}"

    def __init__(
        self,
        supervisor: Any,  # Replace with your supervisor type
    ) -> None:
        self._supervisor = supervisor

    async def ensure_ready(self, workspace_root: Path) -> None:
        """Ensure agent is ready to use."""
        await self._supervisor.get_client(workspace_root)

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        """Get available models from the agent."""
        # TODO: Implement model listing
        client = await self._supervisor.get_client(workspace_root)
        result = await client.get_models()
        models = [
            ModelSpec(
                id=model["id"],
                display_name=model["name"],
                supports_reasoning=model.get("supports_reasoning", False),
                reasoning_options=model.get("reasoning_options", []),
            )
            for model in result.get("models", [])
        ]
        return ModelCatalog(
            default_model=result.get("default_model", ""),
            models=models,
        )

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        """Create a new conversation."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.create_conversation(title=title)
        thread_id = result.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("{display_name} did not return a conversation id")
        return ConversationRef(agent=self.agent_id, id=thread_id)

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        """List existing conversations."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.list_conversations()
        return [
            ConversationRef(agent=self.agent_id, id=c["id"])
            for c in result.get("conversations", [])
        ]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        """Resume an existing conversation."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.get_conversation(conversation_id)
        thread_id = result.get("id", conversation_id)
        return ConversationRef(agent=self.agent_id, id=thread_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        """Start a new turn."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.start_turn(
            conversation_id,
            prompt,
            model=model,
            reasoning=reasoning,
        )
        turn_id = result.get("turn_id")
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> TurnRef:
        """Start a review (if supported)."""
        client = await self._supervisor.get_client(workspace_root)
        result = await client.start_review(conversation_id, prompt)
        turn_id = result.get("turn_id")
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        """Interrupt a running turn."""
        client = await self._supervisor.get_client(workspace_root)
        await client.interrupt_turn(turn_id, conversation_id=conversation_id)

    def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        """Stream turn events."""
        client = self._supervisor.get_client(workspace_root)
        return client.stream_events(conversation_id, turn_id)


__all__ = ["{agent_name.capitalize()}Harness"]
'''


def create_supervisor(agent_name: str, display_name: str) -> str:
    """Generate supervisor.py content."""
    return f'''from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence


class {agent_name.capitalize()}SupervisorError(Exception):
    pass


@dataclass
class {agent_name.capitalize()}Handle:
    workspace_id: str
    workspace_root: Path
    process: Optional[asyncio.subprocess.Process]
    client: Optional[Any]
    start_lock: asyncio.Lock
    started: bool = False
    last_used_at: float = 0.0
    active_turns: int = 0


class {agent_name.capitalize()}Supervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        logger: Optional[logging.Logger] = None,
        request_timeout: Optional[float] = None,
        max_handles: Optional[int] = None,
        idle_ttl_seconds: Optional[float] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_env: Optional[Any] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._command = [str(arg) for arg in command]
        self._logger = logger or logging.getLogger(__name__)
        self._request_timeout = request_timeout
        self._max_handles = max_handles
        self._idle_ttl_seconds = idle_ttl_seconds
        self._auth = (username, password) if username and password else None
        self._base_env = base_env
        self._base_url = base_url
        self._handles: dict[str, {agent_name.capitalize()}Handle] = {{}}
        self._lock = asyncio.Lock()

    async def get_client(self, workspace_root: Path) -> Any:
        """Get or create a client for the workspace."""
        from ...workspace import canonical_workspace_root, workspace_id_for_path
        from ...agents.{agent_name}.client import {agent_name.capitalize()}Client

        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        handle = await self._ensure_handle(workspace_id, canonical_root)
        await self._ensure_started(handle)
        handle.last_used_at = time.monotonic()
        if handle.client is None:
            raise {agent_name.capitalize()}SupervisorError("Client not initialized")
        return handle.client

    async def close_all(self) -> None:
        """Close all handles."""
        async with self._lock:
            handles = list(self._handles.values())
            self._handles = {{}}
        for handle in handles:
            await self._close_handle(handle, reason="close_all")

    async def mark_turn_started(self, workspace_root: Path) -> None:
        from ...workspace import canonical_workspace_root, workspace_id_for_path

        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        async with self._lock:
            handle = self._handles.get(workspace_id)
            if handle is None:
                return
            handle.active_turns += 1
            handle.last_used_at = time.monotonic()

    async def mark_turn_finished(self, workspace_root: Path) -> None:
        from ...workspace import canonical_workspace_root, workspace_id_for_path

        canonical_root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_path(canonical_root)
        async with self._lock:
            handle = self._handles.get(workspace_id)
            if handle is None:
                return
            if handle.active_turns > 0:
                handle.active_turns -= 1
            handle.last_used_at = time.monotonic()

    async def _close_handle(
        self, handle: {agent_name.capitalize()}Handle, *, reason: str
    ) -> None:
        if handle.client is not None:
            await handle.client.close()
        if handle.process and handle.process.returncode is None:
            handle.process.terminate()
            try:
                await asyncio.wait_for(handle.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                handle.process.kill()
                await handle.process.wait()

    async def _ensure_handle(
        self, workspace_id: str, workspace_root: Path
    ) -> {agent_name.capitalize()}Handle:
        handles_to_close: list[{agent_name.capitalize()}Handle] = []
        evicted_id: Optional[str] = None
        async with self._lock:
            existing = self._handles.get(workspace_id)
            if existing is not None:
                existing.last_used_at = time.monotonic()
                return existing
            evicted = self._evict_lru_handle_locked()
            if evicted is not None:
                evicted_id = evicted.workspace_id
                handles_to_close.append(evicted)
            handle = {agent_name.capitalize()}Handle(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                process=None,
                client=None,
                start_lock=asyncio.Lock(),
                last_used_at=time.monotonic(),
            )
            self._handles[workspace_id] = handle
        for h in handles_to_close:
            await self._close_handle(
                h,
                reason=("max_handles" if h.workspace_id == evicted_id else "idle_ttl"),
            )
        return handle

    async def _ensure_started(self, handle: {agent_name.capitalize()}Handle) -> None:
        """Ensure the agent process/server is started."""
        async with handle.start_lock:
            if handle.started:
                return
            if self._base_url:
                await self._ensure_started_base_url(handle)
            else:
                await self._start_process(handle)

    async def _ensure_started_base_url(self, handle: {agent_name.capitalize()}Handle) -> None:
        """Connect to external agent server."""
        import httpx

        base_url = self._base_url
        if not base_url:
            return

        # TODO: Implement health check for your agent
        health_url = f"{{base_url.rstrip('/')}}/health"
        async with httpx.AsyncClient(timeout=self._request_timeout or 10.0) as client:
            response = await client.get(health_url)
            response.raise_for_status()

        handle.started = True
        self._logger.info("Connected to external {display_name} at %s", base_url)

    async def _start_process(self, handle: {agent_name.capitalize()}Handle) -> None:
        """Start the agent subprocess."""
        # TODO: Customize for your agent's protocol
        self._logger.info("Starting {display_name}: %s", " ".join(self._command))
        handle.process = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # TODO: Parse startup output to get connection URL
        # Example: "listening on http://localhost:1234"
        # You may need to read stdout and extract the URL
        handle.started = True

    def _evict_lru_handle_locked(self) -> Optional[{agent_name.capitalize()}Handle]:
        """Evict least-recently-used handle if max handles reached."""
        if self._max_handles is None or len(self._handles) < self._max_handles:
            return None
        sorted_handles = sorted(
            self._handles.values(),
            key=lambda h: h.last_used_at,
        )
        return sorted_handles[0]


__all__ = ["{agent_name.capitalize()}Supervisor", "{agent_name.capitalize()}SupervisorError"]
'''


def create_orchestrator(agent_name: str, display_name: str) -> str:
    """Generate orchestrator.py content."""
    return f'''from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..orchestrator import AgentOrchestrator, TurnStatus
from .harness import {agent_name.capitalize()}Harness


class {agent_name.capitalize()}Orchestrator(AgentOrchestrator):
    def __init__(self, harness: {agent_name.capitalize()}Harness):
        self._harness = harness

    async def create_or_resume_conversation(
        self,
        workspace_root: Path,
        agent_id: str,
        *,
        conversation_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Any:  # ConversationRef
        if conversation_id:
            return await self._harness.resume_conversation(
                workspace_root, conversation_id
            )
        return await self._harness.new_conversation(workspace_root, title)

    async def run_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        turn_ref = await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )

        # Collect output from events
        output_lines = []
        async for event_str in self._harness.stream_events(
            workspace_root, turn_ref.conversation_id, turn_ref.turn_id
        ):
            # TODO: Parse events and extract output
            output_lines.append(event_str)

        return {{
            "turn_id": turn_ref.turn_id,
            "conversation_id": turn_ref.conversation_id,
            "status": TurnStatus.COMPLETED,
            "output": "\\n".join(output_lines),
        }}

    async def stream_turn_events(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        turn_ref = await self._harness.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )
        yield {{"type": "turn_started", "data": {{"turn_id": turn_ref.turn_id}}}}

        async for event_str in self._harness.stream_events(
            workspace_root, turn_ref.conversation_id, turn_ref.turn_id
        ):
            yield {{"type": "event", "data": event_str}}

        yield {{"type": "turn_completed", "data": {{"turn_id": turn_ref.turn_id}}}}

    async def interrupt_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str] = None,
        grace_seconds: float = 30.0,
    ) -> bool:
        await self._harness.interrupt(workspace_root, conversation_id, turn_id)
        return True

    async def start_review(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: Optional[str] = None,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Any:  # TurnRef
        return await self._harness.start_review(
            workspace_root,
            conversation_id,
            prompt or "",
            model,
            reasoning,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
        )


__all__ = ["{agent_name.capitalize()}Orchestrator"]
'''


def create_init(agent_name: str) -> str:
    """Generate __init__.py content."""
    return f'''from .harness import {agent_name.capitalize()}Harness
from .orchestrator import {agent_name.capitalize()}Orchestrator
from .supervisor import {agent_name.capitalize()}Supervisor, {agent_name.capitalize()}SupervisorError


__all__ = [
    "{agent_name.capitalize()}Harness",
    "{agent_name.capitalize()}Orchestrator",
    "{agent_name.capitalize()}Supervisor",
    "{agent_name.capitalize()}SupervisorError",
]
'''


def main():
    parser = argparse.ArgumentParser(
        description="Scaffold a new agent harness for Codex Autorunner"
    )
    parser.add_argument(
        "agent_name",
        help="Agent identifier (e.g., 'myagent')",
    )
    parser.add_argument(
        "display_name",
        help="Human-readable agent name (e.g., 'My Agent')",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("src/codex_autorunner/agents"),
        help="Output directory for generated files",
    )
    args = parser.parse_args()

    agent_name = validate_agent_name(args.agent_name)
    display_name = args.display_name
    output_dir = args.output_dir / agent_name

    if output_dir.exists():
        print(f"Error: Directory already exists: {output_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=False)

    # Create files
    files = {
        "harness.py": create_harness(agent_name, display_name),
        "supervisor.py": create_supervisor(agent_name, display_name),
        "orchestrator.py": create_orchestrator(agent_name, display_name),
        "__init__.py": create_init(agent_name),
    }

    for filename, content in files.items():
        file_path = output_dir / filename
        file_path.write_text(content)
        print(f"Created: {file_path}")

    # Create client.py stub
    client_path = output_dir / "client.py"
    client_stub = f'''from __future__ import annotations

import asyncio
import httpx
from typing import Any, AsyncIterator, Optional


class {agent_name.capitalize()}Client:
    """Client for {display_name} agent."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: Optional[tuple[str, str]] = None,
        request_timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._request_timeout = request_timeout

    async def close(self) -> None:
        """Close the client."""
        pass

    async def get_models(self) -> dict[str, Any]:
        """Get available models."""
        # TODO: Implement model listing
        raise NotImplementedError("get_models not implemented")

    async def create_conversation(self, title: Optional[str] = None) -> dict[str, Any]:
        """Create a new conversation."""
        # TODO: Implement conversation creation
        raise NotImplementedError("create_conversation not implemented")

    async def list_conversations(self) -> dict[str, Any]:
        """List conversations."""
        # TODO: Implement conversation listing
        raise NotImplementedError("list_conversations not implemented")

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Get conversation details."""
        # TODO: Implement conversation retrieval
        raise NotImplementedError("get_conversation not implemented")

    async def start_turn(
        self,
        conversation_id: str,
        prompt: str,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start a turn."""
        # TODO: Implement turn start
        raise NotImplementedError("start_turn not implemented")

    async def start_review(self, conversation_id: str, prompt: str) -> dict[str, Any]:
        """Start a review."""
        # TODO: Implement review start (if supported)
        raise NotImplementedError("start_review not implemented")

    async def interrupt_turn(self, turn_id: str, conversation_id: str) -> None:
        """Interrupt a running turn."""
        # TODO: Implement turn interruption
        raise NotImplementedError("interrupt_turn not implemented")

    async def stream_events(
        self, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        """Stream turn events."""
        # TODO: Implement event streaming
        raise NotImplementedError("stream_events not implemented")


__all__ = ["{agent_name.capitalize()}Client"]
'''
    client_path.write_text(client_stub)
    print(f"Created: {client_path}")

    print(f"\nScaffolded agent: {agent_name}")
    print(f"Display name: {display_name}")
    print(f"Output directory: {output_dir}")
    print("\nNext steps:")
    print("1. Implement the client.py stub for your agent's protocol")
    print("2. Customize supervisor.py startup logic")
    print("3. Test the agent: python -m pytest tests/")
    print("4. Add agent to src/codex_autorunner/agents/registry.py")
    print("5. Add configuration to src/codex_autorunner/core/config.py")
    print("6. See docs/adding-an-agent.md for full integration guide")


if __name__ == "__main__":
    main()
