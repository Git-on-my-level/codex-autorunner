from __future__ import annotations

import uuid
from typing import Any

from codex_autorunner.agents.runtime_options import resolve_opencode_model_payload
from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import (
    TurnExecutionOrigin,
    TurnExecutionRequest,
)


def build_test_turn_request(
    *,
    managed_thread_id: str,
    workspace_root: str,
    agent: str = "codex",
    prompt: str,
    request_kind: str = "message",
    busy_policy: str = "reject",
    model: str | None = None,
    reasoning: str | None = None,
    client_turn_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TurnExecutionRequest:
    resolved_model = model
    if agent == "opencode" and resolved_model is None:
        resolved_model = "openai/gpt-5"
    model_payload = (
        dict(resolve_opencode_model_payload(resolved_model) or {})
        if agent == "opencode"
        else {}
    )
    return TurnExecutionRequest(
        request_id=client_turn_id or uuid.uuid4().hex,
        target_id=managed_thread_id,
        target_kind="thread",
        workspace_root=workspace_root,
        request_kind=request_kind,  # type: ignore[arg-type]
        busy_policy=busy_policy,  # type: ignore[arg-type]
        prompt_text=prompt,
        agent=agent,
        model=resolved_model,
        model_payload=model_payload,
        reasoning=reasoning,
        approval_policy="never",
        sandbox_policy="dangerFullAccess",
        client_request_id=client_turn_id,
        idempotency_key=client_turn_id,
        origin=TurnExecutionOrigin(kind="system", source_id="test"),
        metadata=dict(metadata or {}),
    )


def create_test_turn(
    store: ManagedThreadStore,
    managed_thread_id: str,
    *,
    prompt: str,
    request_kind: str = "message",
    busy_policy: str = "reject",
    model: str | None = None,
    reasoning: str | None = None,
    client_turn_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    queue_payload: dict[str, Any] | None = None,
    force_queue: bool = False,
) -> dict[str, Any]:
    thread = store.get_thread(managed_thread_id)
    if thread is None:
        raise AssertionError(f"missing test thread: {managed_thread_id}")
    agent = str(thread.get("agent_id") or thread.get("agent") or "codex")
    turn_request = build_test_turn_request(
        managed_thread_id=managed_thread_id,
        workspace_root=str(thread.get("workspace_root") or ""),
        agent=agent,
        prompt=prompt,
        request_kind=request_kind,
        busy_policy=busy_policy,
        model=model,
        reasoning=reasoning,
        client_turn_id=client_turn_id,
        metadata=dict(metadata or {}),
    )
    return store.create_turn(
        managed_thread_id,
        prompt=prompt,
        request_kind=request_kind,
        busy_policy=busy_policy,
        model=turn_request.model,
        reasoning=reasoning,
        client_turn_id=client_turn_id,
        metadata=metadata,
        queue_payload=queue_payload,
        turn_request=turn_request,
        force_queue=force_queue,
    )
