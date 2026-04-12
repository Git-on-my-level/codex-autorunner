from __future__ import annotations

from codex_autorunner.core.hub_control_plane import (
    ControlPlaneVersion,
    HandshakeRequest,
    HandshakeResponse,
    HubControlPlaneError,
    HubControlPlaneErrorInfo,
    NotificationRecordResponse,
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
