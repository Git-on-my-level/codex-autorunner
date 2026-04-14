from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.core.hub_control_plane import (
    ExecutionCancelAllResponse,
    ExecutionCancelResponse,
    ExecutionClaimNextResponse,
    ExecutionListResponse,
    ExecutionPromoteResponse,
    ExecutionResponse,
    HubControlPlaneError,
    QueueDepthResponse,
    RemoteThreadExecutionStore,
    ThreadTargetListResponse,
    ThreadTargetResponse,
)
from codex_autorunner.core.orchestration.interfaces import ThreadExecutionStore
from codex_autorunner.core.orchestration.models import ExecutionRecord, ThreadTarget


def _thread_payload() -> dict[str, Any]:
    return {
        "thread_target_id": "thread-1",
        "agent": "codex",
        "workspace_root": "/tmp/workspace",
        "repo_id": "repo-1",
        "resource_kind": "agent_workspace",
        "resource_id": "wksp-1",
        "name": "Workspace Thread",
        "status": "running",
        "lifecycle_status": "active",
        "last_execution_id": "exec-2",
        "last_message_preview": "latest preview",
        "compact_seed": "compact seed",
        "metadata": {
            "context_profile": "car_core",
            "agent_profile": "coder",
        },
    }


def _execution_payload(
    *,
    execution_id: str = "exec-1",
    status: str = "running",
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "thread_target_id": "thread-1",
        "status": status,
        "request_kind": "message",
        "backend_turn_id": "turn-1",
        "started_at": "2026-04-13T01:02:03Z",
        "finished_at": (
            None if status in {"running", "queued"} else "2026-04-13T01:04:05Z"
        ),
        "error": None if status != "failed" else "boom",
        "assistant_text": None if status == "running" else "assistant output",
    }


class _FakeHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.thread_response = ThreadTargetResponse.from_mapping(
            {"thread": _thread_payload()}
        )
        self.thread_list_response = ThreadTargetListResponse.from_mapping(
            {"threads": [_thread_payload()]}
        )
        self.execution_response = ExecutionResponse.from_mapping(
            {"execution": _execution_payload()}
        )
        self.execution_list_response = ExecutionListResponse.from_mapping(
            {
                "executions": [
                    _execution_payload(execution_id="exec-2", status="queued"),
                    _execution_payload(execution_id="exec-3", status="queued"),
                ]
            }
        )
        self.queue_depth_response = QueueDepthResponse.from_mapping(
            {"thread_target_id": "thread-1", "queue_depth": 2}
        )
        self.cancel_response = ExecutionCancelResponse.from_mapping(
            {
                "thread_target_id": "thread-1",
                "execution_id": "exec-2",
                "cancelled": True,
            }
        )
        self.promote_response = ExecutionPromoteResponse.from_mapping(
            {
                "thread_target_id": "thread-1",
                "execution_id": "exec-2",
                "promoted": True,
            }
        )
        self.claim_response = ExecutionClaimNextResponse.from_mapping(
            {
                "execution": _execution_payload(
                    execution_id="exec-2",
                    status="running",
                ),
                "queue_payload": {
                    "request": {
                        "target_id": "thread-1",
                        "target_kind": "thread",
                        "message_text": "queued prompt",
                        "kind": "message",
                        "busy_policy": "queue",
                    },
                    "client_request_id": "client-2",
                },
            }
        )
        self.cancel_all_response = ExecutionCancelAllResponse.from_mapping(
            {"thread_target_id": "thread-1", "cancelled_count": 3}
        )

    async def get_thread_target(self, request):
        self.calls.append(("get_thread_target", request))
        return self.thread_response

    async def list_thread_targets(self, request):
        self.calls.append(("list_thread_targets", request))
        return self.thread_list_response

    async def create_thread_target(self, request):
        self.calls.append(("create_thread_target", request))
        return self.thread_response

    async def resume_thread_target(self, request):
        self.calls.append(("resume_thread_target", request))
        return self.thread_response

    async def archive_thread_target(self, request):
        self.calls.append(("archive_thread_target", request))
        return self.thread_response

    async def set_thread_backend_id(self, request):
        self.calls.append(("set_thread_backend_id", request))
        return None

    async def record_thread_activity(self, request):
        self.calls.append(("record_thread_activity", request))
        return None

    async def create_execution(self, request):
        self.calls.append(("create_execution", request))
        return self.execution_response

    async def get_execution(self, request):
        self.calls.append(("get_execution", request))
        return self.execution_response

    async def get_running_execution(self, request):
        self.calls.append(("get_running_execution", request))
        return self.execution_response

    async def get_latest_execution(self, request):
        self.calls.append(("get_latest_execution", request))
        return self.execution_response

    async def list_queued_executions(self, request):
        self.calls.append(("list_queued_executions", request))
        return self.execution_list_response

    async def get_queue_depth(self, request):
        self.calls.append(("get_queue_depth", request))
        return self.queue_depth_response

    async def cancel_queued_execution(self, request):
        self.calls.append(("cancel_queued_execution", request))
        return self.cancel_response

    async def promote_queued_execution(self, request):
        self.calls.append(("promote_queued_execution", request))
        return self.promote_response

    async def claim_next_queued_execution(self, request):
        self.calls.append(("claim_next_queued_execution", request))
        return self.claim_response

    async def set_execution_backend_id(self, request):
        self.calls.append(("set_execution_backend_id", request))
        return None

    async def record_execution_result(self, request):
        self.calls.append(("record_execution_result", request))
        return ExecutionResponse.from_mapping(
            {"execution": _execution_payload(execution_id="exec-1", status="ok")}
        )

    async def record_execution_interrupted(self, request):
        self.calls.append(("record_execution_interrupted", request))
        return ExecutionResponse.from_mapping(
            {
                "execution": _execution_payload(
                    execution_id="exec-1",
                    status="interrupted",
                )
            }
        )

    async def cancel_queued_executions(self, request):
        self.calls.append(("cancel_queued_executions", request))
        return self.cancel_all_response


def test_remote_execution_store_implements_thread_execution_store() -> None:
    store = RemoteThreadExecutionStore(_FakeHubClient())
    assert isinstance(store, ThreadExecutionStore)


def test_remote_execution_store_delegates_to_hub_client_for_thread_and_execution_ops() -> (
    None
):
    client = _FakeHubClient()
    store = RemoteThreadExecutionStore(client)

    created_thread = store.create_thread_target(
        "codex",
        Path("/tmp/workspace"),
        repo_id="repo-1",
        resource_kind="agent_workspace",
        resource_id="wksp-1",
        display_name="Workspace Thread",
        backend_thread_id="conversation-1",
        context_profile="car_core",
        metadata={"agent_profile": "coder"},
    )
    fetched_thread = store.get_thread_target("thread-1")
    listed_threads = store.list_thread_targets(
        agent_id="codex",
        lifecycle_status="active",
        runtime_status="running",
        repo_id="repo-1",
        resource_kind="agent_workspace",
        resource_id="wksp-1",
        limit=10,
    )
    resumed_thread = store.resume_thread_target(
        "thread-1",
        backend_thread_id="conversation-2",
        backend_runtime_instance_id="runtime-1",
    )
    archived_thread = store.archive_thread_target("thread-1")
    store.set_thread_backend_id(
        "thread-1",
        "conversation-3",
        backend_runtime_instance_id="runtime-2",
    )

    created_execution = store.create_execution(
        "thread-1",
        prompt="Run this",
        request_kind="message",
        busy_policy="queue",
        model="gpt-5.4",
        reasoning="high",
        client_request_id="client-1",
        queue_payload={"priority": "high"},
    )
    fetched_execution = store.get_execution("thread-1", "exec-1")
    running_execution = store.get_running_execution("thread-1")
    latest_execution = store.get_latest_execution("thread-1")
    queued_executions = store.list_queued_executions("thread-1", limit=5)
    queue_depth = store.get_queue_depth("thread-1")
    cancelled = store.cancel_queued_execution("thread-1", "exec-2")
    promoted = store.promote_queued_execution("thread-1", "exec-2")
    claimed = store.claim_next_queued_execution("thread-1")
    store.set_execution_backend_id("exec-1", "turn-2")
    finished_execution = store.record_execution_result(
        "thread-1",
        "exec-1",
        status="ok",
        assistant_text="assistant output",
        backend_turn_id="turn-2",
        transcript_turn_id="transcript-1",
    )
    interrupted_execution = store.record_execution_interrupted("thread-1", "exec-1")
    cancelled_count = store.cancel_queued_executions("thread-1")
    store.record_thread_activity(
        "thread-1",
        execution_id="exec-1",
        message_preview="latest preview",
    )

    expected_thread = ThreadTarget.from_mapping(_thread_payload())
    expected_execution = ExecutionRecord.from_mapping(_execution_payload())

    assert created_thread == expected_thread
    assert fetched_thread == expected_thread
    assert listed_threads == [expected_thread]
    assert resumed_thread == expected_thread
    assert archived_thread == expected_thread
    assert created_execution == expected_execution
    assert fetched_execution == expected_execution
    assert running_execution == expected_execution
    assert latest_execution == expected_execution
    assert queued_executions == list(client.execution_list_response.executions)
    assert queue_depth == 2
    assert cancelled is True
    assert promoted is True
    assert claimed == (
        ExecutionRecord.from_mapping(_execution_payload(execution_id="exec-2")),
        dict(client.claim_response.queue_payload),
    )
    assert finished_execution.status == "ok"
    assert finished_execution.output_text == "assistant output"
    assert interrupted_execution.status == "interrupted"
    assert cancelled_count == 3

    call_names = [name for name, _request in client.calls]
    assert call_names == [
        "create_thread_target",
        "get_thread_target",
        "list_thread_targets",
        "resume_thread_target",
        "archive_thread_target",
        "set_thread_backend_id",
        "create_execution",
        "get_execution",
        "get_running_execution",
        "get_latest_execution",
        "list_queued_executions",
        "get_queue_depth",
        "cancel_queued_execution",
        "promote_queued_execution",
        "claim_next_queued_execution",
        "set_execution_backend_id",
        "record_execution_result",
        "record_execution_interrupted",
        "cancel_queued_executions",
        "record_thread_activity",
    ]

    create_thread_request = client.calls[0][1]
    assert create_thread_request.to_dict() == {
        "agent_id": "codex",
        "workspace_root": "/tmp/workspace",
        "repo_id": "repo-1",
        "resource_kind": "agent_workspace",
        "resource_id": "wksp-1",
        "display_name": "Workspace Thread",
        "backend_thread_id": "conversation-1",
        "metadata": {
            "agent_profile": "coder",
            "context_profile": "car_core",
        },
    }

    create_execution_request = client.calls[6][1]
    assert create_execution_request.to_dict() == {
        "thread_target_id": "thread-1",
        "prompt": "Run this",
        "request_kind": "message",
        "busy_policy": "queue",
        "model": "gpt-5.4",
        "reasoning": "high",
        "client_request_id": "client-1",
        "queue_payload": {"priority": "high"},
    }

    set_thread_backend_request = client.calls[5][1]
    assert set_thread_backend_request.to_dict() == {
        "thread_target_id": "thread-1",
        "backend_thread_id": "conversation-3",
        "backend_runtime_instance_id": "runtime-2",
    }

    record_result_request = client.calls[16][1]
    assert record_result_request.to_dict() == {
        "thread_target_id": "thread-1",
        "execution_id": "exec-1",
        "status": "ok",
        "assistant_text": "assistant output",
        "error": None,
        "backend_turn_id": "turn-2",
        "transcript_turn_id": "transcript-1",
    }

    record_activity_request = client.calls[19][1]
    assert record_activity_request.to_dict() == {
        "thread_target_id": "thread-1",
        "execution_id": "exec-1",
        "message_preview": "latest preview",
    }


class _TransportFailingClient(_FakeHubClient):
    async def create_execution(self, request):
        self.calls.append(("create_execution", request))
        raise HubControlPlaneError(
            "transport_failure",
            "dial tcp 127.0.0.1:8765: connection refused",
            details={
                "path": "/hub/api/control-plane/thread-targets/thread-1/executions"
            },
        )


class _RejectedClient(_FakeHubClient):
    async def get_execution(self, request):
        self.calls.append(("get_execution", request))
        raise HubControlPlaneError("hub_rejected", "execution_id is invalid")


class _ConnectionErrorClient(_FakeHubClient):
    async def get_thread_target(self, request):
        self.calls.append(("get_thread_target", request))
        raise OSError("hub socket missing")


class _LoopBoundThreadClient(_FakeHubClient):
    def __init__(self) -> None:
        super().__init__()
        self._owner_thread_id = threading.get_ident()

    def clone_for_background_loop(self):
        return _FakeHubClient()

    async def get_thread_target(self, request):
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("Event loop is closed")
        return await super().get_thread_target(request)


def test_remote_execution_store_translates_transport_failures_to_hub_unavailable() -> (
    None
):
    store = RemoteThreadExecutionStore(_TransportFailingClient())

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.create_execution("thread-1", prompt="Run this")

    assert exc_info.value.code == "hub_unavailable"
    assert "create_execution" in str(exc_info.value)
    assert exc_info.value.details["operation"] == "create_execution"
    assert exc_info.value.details["cause_code"] == "transport_failure"


def test_remote_execution_store_preserves_non_transport_hub_errors() -> None:
    store = RemoteThreadExecutionStore(_RejectedClient())

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_execution("thread-1", "exec-1")

    assert exc_info.value.code == "hub_rejected"
    assert str(exc_info.value) == "execution_id is invalid"


def test_remote_execution_store_translates_connection_errors_to_hub_unavailable() -> (
    None
):
    store = RemoteThreadExecutionStore(_ConnectionErrorClient())

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_thread_target("thread-1")

    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.details["operation"] == "get_thread_target"
    assert exc_info.value.details["cause_type"] == "OSError"


def test_remote_execution_store_clones_loop_bound_client_for_background_calls() -> None:
    store = RemoteThreadExecutionStore(_LoopBoundThreadClient())

    thread = store.get_thread_target("thread-1")

    assert thread is not None
    assert thread.thread_target_id == "thread-1"
