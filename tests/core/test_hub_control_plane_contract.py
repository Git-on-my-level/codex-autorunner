from __future__ import annotations

import httpx
import pytest

from codex_autorunner.core.hub_control_plane import (
    ControlPlaneVersion,
    ExecutionBackendIdUpdateRequest,
    ExecutionClaimNextResponse,
    ExecutionCreateRequest,
    ExecutionListResponse,
    ExecutionResponse,
    HandshakeRequest,
    HandshakeResponse,
    HubControlPlaneError,
    HubControlPlaneErrorInfo,
    NotificationDeliveryMarkRequest,
    NotificationRecordResponse,
    NotificationReplyTargetLookupRequest,
    ThreadTargetResponse,
    evaluate_handshake_compatibility,
)


def test_control_plane_version_parsing_and_ordering() -> None:
    assert str(ControlPlaneVersion.parse("2")) == "2.0.0"
    assert str(ControlPlaneVersion.parse("2.3")) == "2.3.0"
    assert ControlPlaneVersion.parse("2.3.1") > ControlPlaneVersion.parse("2.3.0")


def test_handshake_request_and_response_round_trip() -> None:
    request = HandshakeRequest.from_mapping(
        {
            "client_name": "discord",
            "client_api_version": "1.2",
            "client_version": "2026.04.12",
            "expected_schema_generation": 17,
            "supported_capabilities": [
                "thread_targets",
                "notification_records",
                "thread_targets",
            ],
        }
    )
    assert request.to_dict() == {
        "client_name": "discord",
        "client_api_version": "1.2.0",
        "client_version": "2026.04.12",
        "expected_schema_generation": 17,
        "supported_capabilities": [
            "notification_records",
            "thread_targets",
        ],
    }

    response = HandshakeResponse.from_mapping(
        {
            "api_version": "1.4",
            "minimum_client_api_version": "1.2.0",
            "schema_generation": 17,
            "capabilities": [
                "thread_targets",
                "surface_bindings",
                "thread_targets",
            ],
            "hub_build_version": "2026.04.12+abc123",
            "hub_asset_version": "web-88",
        }
    )
    assert response.to_dict() == {
        "api_version": "1.4.0",
        "minimum_client_api_version": "1.2.0",
        "schema_generation": 17,
        "capabilities": ["surface_bindings", "thread_targets"],
        "hub_build_version": "2026.04.12+abc123",
        "hub_asset_version": "web-88",
    }


def test_handshake_compatibility_accepts_supported_client() -> None:
    response = HandshakeResponse(
        api_version="1.4.0",
        minimum_client_api_version="1.2.0",
        schema_generation=17,
        capabilities=("thread_targets",),
        hub_build_version="2026.04.12+abc123",
        hub_asset_version="web-88",
    )

    compatibility = evaluate_handshake_compatibility(
        response,
        client_api_version="1.3.0",
        expected_schema_generation=17,
    )

    assert compatibility.compatible is True
    assert compatibility.reason is None


def test_handshake_compatibility_rejects_old_client() -> None:
    response = HandshakeResponse(
        api_version="1.4.0",
        minimum_client_api_version="1.2.0",
        schema_generation=17,
        capabilities=(),
    )

    compatibility = evaluate_handshake_compatibility(
        response,
        client_api_version="1.1.9",
        expected_schema_generation=17,
    )

    assert compatibility.compatible is False
    assert compatibility.reason == "client API version is older than the hub minimum"


def test_handshake_compatibility_rejects_major_version_mismatch() -> None:
    response = HandshakeResponse(
        api_version="2.0.0",
        minimum_client_api_version="2.0.0",
        schema_generation=17,
        capabilities=(),
    )

    compatibility = evaluate_handshake_compatibility(
        response,
        client_api_version="1.9.0",
        expected_schema_generation=17,
    )

    assert compatibility.compatible is False
    assert compatibility.reason == "control-plane API major version mismatch"


def test_handshake_compatibility_rejects_schema_generation_mismatch() -> None:
    response = HandshakeResponse(
        api_version="1.4.0",
        minimum_client_api_version="1.2.0",
        schema_generation=19,
        capabilities=(),
    )

    compatibility = evaluate_handshake_compatibility(
        response,
        client_api_version="1.4.0",
        expected_schema_generation=17,
    )

    assert compatibility.compatible is False
    assert compatibility.reason == "orchestration schema generation mismatch"


def test_notification_record_response_round_trip() -> None:
    response = NotificationRecordResponse.from_mapping(
        {
            "record": {
                "notification_id": "notif-1",
                "correlation_id": "corr-1",
                "source_kind": "run",
                "delivery_mode": "chat",
                "surface_kind": "discord",
                "surface_key": "channel:123",
                "delivery_record_id": "delivery-1",
                "delivered_message_id": "msg-9",
                "repo_id": "repo-1",
                "workspace_root": "/tmp/repo",
                "run_id": "42",
                "managed_thread_id": "thread-1",
                "continuation_thread_target_id": "thread-2",
                "context": {"actor": "car"},
                "created_at": "2026-04-12T01:02:03Z",
                "updated_at": "2026-04-12T01:02:04Z",
            }
        }
    )

    assert response.to_dict()["record"] == {
        "notification_id": "notif-1",
        "correlation_id": "corr-1",
        "source_kind": "run",
        "delivery_mode": "chat",
        "surface_kind": "discord",
        "surface_key": "channel:123",
        "delivery_record_id": "delivery-1",
        "delivered_message_id": "msg-9",
        "repo_id": "repo-1",
        "workspace_root": "/tmp/repo",
        "run_id": "42",
        "managed_thread_id": "thread-1",
        "continuation_thread_target_id": "thread-2",
        "context": {"actor": "car"},
        "created_at": "2026-04-12T01:02:03Z",
        "updated_at": "2026-04-12T01:02:04Z",
    }


def test_notification_request_models_accept_numeric_message_ids() -> None:
    reply_lookup = NotificationReplyTargetLookupRequest.from_mapping(
        {
            "surface_kind": "telegram",
            "surface_key": "chat:1",
            "delivered_message_id": 88,
        }
    )
    delivery_mark = NotificationDeliveryMarkRequest.from_mapping(
        {
            "delivery_record_id": "delivery-1",
            "delivered_message_id": 88,
        }
    )

    assert reply_lookup.to_dict() == {
        "surface_kind": "telegram",
        "surface_key": "chat:1",
        "delivered_message_id": "88",
    }
    assert delivery_mark.to_dict() == {
        "delivery_record_id": "delivery-1",
        "delivered_message_id": "88",
    }


def test_thread_target_response_uses_existing_generic_thread_model() -> None:
    response = ThreadTargetResponse.from_mapping(
        {
            "thread": {
                "thread_target_id": "thread-1",
                "agent": "codex",
                "workspace_root": "/tmp/repo",
                "repo_id": "repo-1",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "name": "Main thread",
                "status": "idle",
                "lifecycle_status": "active",
                "created_at": "2026-04-12T01:02:03Z",
                "updated_at": "2026-04-12T01:02:04Z",
            }
        }
    )

    assert response.thread is not None
    assert response.thread.thread_target_id == "thread-1"
    assert response.to_dict()["thread"]["thread_target_id"] == "thread-1"


def test_thread_target_response_round_trips_agent_and_backend_ids() -> None:
    response = ThreadTargetResponse.from_mapping(
        {
            "thread": {
                "thread_target_id": "thread-1",
                "agent": "codex",
                "workspace_root": "/tmp/repo",
                "backend_thread_id": "backend-thread-1",
                "backend_runtime_instance_id": "runtime-1",
                "name": "Main thread",
            }
        }
    )

    payload = response.to_dict()
    round_tripped = ThreadTargetResponse.from_mapping(payload)

    assert payload["thread"]["agent"] == "codex"
    assert payload["thread"]["backend_thread_id"] == "backend-thread-1"
    assert payload["thread"]["backend_runtime_instance_id"] == "runtime-1"
    assert round_tripped.thread is not None
    assert round_tripped.thread.agent_id == "codex"
    assert round_tripped.thread.backend_thread_id == "backend-thread-1"
    assert round_tripped.thread.backend_runtime_instance_id == "runtime-1"


def test_execution_models_round_trip_with_existing_execution_record() -> None:
    create_request = ExecutionCreateRequest.from_mapping(
        {
            "thread_target_id": "thread-1",
            "prompt": "Summarize the latest run",
            "request_kind": "message",
            "busy_policy": "queue",
            "model": "gpt-5.4",
            "reasoning": "medium",
            "client_request_id": "client-77",
            "queue_payload": {"priority": "high"},
        }
    )
    execution_response = ExecutionResponse.from_mapping(
        {
            "execution": {
                "execution_id": "exec-1",
                "thread_target_id": "thread-1",
                "status": "running",
                "request_kind": "message",
                "backend_turn_id": "backend-1",
                "started_at": "2026-04-12T01:02:03Z",
            }
        }
    )
    execution_list = ExecutionListResponse.from_mapping(
        {
            "executions": [
                {
                    "execution_id": "exec-1",
                    "thread_target_id": "thread-1",
                    "status": "queued",
                    "request_kind": "message",
                }
            ]
        }
    )
    claim_response = ExecutionClaimNextResponse.from_mapping(
        {
            "execution": {
                "execution_id": "exec-2",
                "thread_target_id": "thread-1",
                "status": "running",
                "request_kind": "message",
            },
            "queue_payload": {"source": "discord"},
        }
    )
    backend_update = ExecutionBackendIdUpdateRequest.from_mapping(
        {"execution_id": "exec-2", "backend_id": "backend-2"}
    )

    assert create_request.to_dict() == {
        "thread_target_id": "thread-1",
        "prompt": "Summarize the latest run",
        "request_kind": "message",
        "busy_policy": "queue",
        "model": "gpt-5.4",
        "reasoning": "medium",
        "client_request_id": "client-77",
        "queue_payload": {"priority": "high"},
    }
    assert execution_response.execution is not None
    assert execution_response.execution.execution_id == "exec-1"
    assert execution_response.to_dict()["execution"]["backend_id"] == "backend-1"
    assert execution_list.executions[0].status == "queued"
    assert claim_response.queue_payload == {"source": "discord"}
    assert backend_update.to_dict() == {
        "execution_id": "exec-2",
        "backend_turn_id": "backend-2",
    }


def test_error_info_round_trip_preserves_taxonomy() -> None:
    error = HubControlPlaneError(
        "hub_incompatible",
        "schema generation mismatch",
        details={"expected_schema_generation": 17, "server_schema_generation": 19},
    )

    info = HubControlPlaneErrorInfo.from_mapping(error.info.to_dict())

    assert info.code == "hub_incompatible"
    assert info.retryable is False
    assert info.details["expected_schema_generation"] == 17


async def test_http_client_aclose_closes_owned_httpx_client() -> None:
    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )

    client = HttpHubControlPlaneClient(base_url="http://localhost:9999")
    assert not client._http_client.is_closed
    await client.aclose()
    assert client._http_client.is_closed


async def test_http_client_aclose_skips_injected_httpx_client() -> None:
    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )

    injected = httpx.AsyncClient(base_url="http://localhost:9999")
    client = HttpHubControlPlaneClient(
        base_url="http://localhost:9999", http_client=injected
    )
    await client.aclose()
    assert not injected.is_closed
    await injected.aclose()


async def test_http_client_context_manager_closes() -> None:
    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )

    client = HttpHubControlPlaneClient(base_url="http://localhost:9999")
    async with client:
        assert not client._http_client.is_closed
    assert client._http_client.is_closed


async def test_http_client_uses_extended_timeout_for_workspace_setup_commands() -> None:
    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )
    from codex_autorunner.core.hub_control_plane.models import (
        WorkspaceSetupCommandRequest,
    )

    class _FakeAsyncClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def request(
            self,
            method: str,
            path: str,
            *,
            json: dict[str, object] | None = None,
            params: dict[str, object] | None = None,
            timeout: float | None = None,
        ) -> httpx.Response:
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "json": json,
                    "params": params,
                    "timeout": timeout,
                }
            )
            return httpx.Response(
                200,
                json={
                    "workspace_root": (json or {}).get("workspace_root", ""),
                    "repo_id_hint": (json or {}).get("repo_id_hint"),
                    "setup_command_count": 1,
                },
            )

    fake_client = _FakeAsyncClient()
    client = HttpHubControlPlaneClient(
        base_url="http://localhost:9999",
        timeout=10.0,
        http_client=fake_client,  # type: ignore[arg-type]
    )

    result = await client.run_workspace_setup_commands(
        WorkspaceSetupCommandRequest(
            workspace_root="/workspace/repo-1",
            repo_id_hint="repo-1",
        )
    )

    assert result.setup_command_count == 1
    assert fake_client.calls == [
        {
            "method": "POST",
            "path": "/hub/api/control-plane/workspace-setup-commands",
            "json": {
                "workspace_root": "/workspace/repo-1",
                "repo_id_hint": "repo-1",
            },
            "params": None,
            "timeout": None,
        }
    ]


async def test_http_client_uses_extended_timeout_for_backend_id_updates() -> None:
    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )
    from codex_autorunner.core.hub_control_plane.models import (
        ExecutionBackendIdUpdateRequest,
    )

    class _FakeAsyncClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def request(
            self,
            method: str,
            path: str,
            *,
            json: dict[str, object] | None = None,
            params: dict[str, object] | None = None,
            timeout: float | None = None,
        ) -> httpx.Response:
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "json": json,
                    "params": params,
                    "timeout": timeout,
                }
            )
            return httpx.Response(204)

    fake_client = _FakeAsyncClient()
    client = HttpHubControlPlaneClient(
        base_url="http://localhost:9999",
        timeout=10.0,
        http_client=fake_client,  # type: ignore[arg-type]
    )

    await client.set_execution_backend_id(
        ExecutionBackendIdUpdateRequest(
            execution_id="exec-1",
            backend_turn_id="turn-2",
        )
    )

    assert fake_client.calls == [
        {
            "method": "PUT",
            "path": "/hub/api/control-plane/thread-executions/exec-1/backend-id",
            "json": {
                "execution_id": "exec-1",
                "backend_turn_id": "turn-2",
            },
            "params": None,
            "timeout": 30.0,
        }
    ]


async def test_http_client_includes_exception_class_for_blank_transport_errors() -> (
    None
):
    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )

    request = httpx.Request(
        "POST", "http://localhost:9999/hub/api/control-plane/handshake"
    )

    class _FakeAsyncClient:
        async def request(
            self,
            method: str,
            path: str,
            *,
            json: dict[str, object] | None = None,
            params: dict[str, object] | None = None,
            timeout: float | None = None,
        ) -> httpx.Response:
            raise httpx.ReadTimeout("", request=request)

    client = HttpHubControlPlaneClient(
        base_url="http://localhost:9999",
        http_client=_FakeAsyncClient(),  # type: ignore[arg-type]
    )

    with pytest.raises(HubControlPlaneError) as exc_info:
        await client.handshake(
            HandshakeRequest(
                client_name="discord",
                client_api_version="1.0.0",
                expected_schema_generation=1,
            )
        )

    assert exc_info.value.code == "transport_failure"
    assert (
        str(exc_info.value) == "Hub control-plane transport request failed: ReadTimeout"
    )
