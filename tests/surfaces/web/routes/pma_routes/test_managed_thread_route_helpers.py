from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from codex_autorunner.surfaces.web.routes.pma_routes.managed_thread_route_helpers import (
    _apply_chat_binding_fields,
    _attach_latest_execution_fields,
    _build_operator_status_fields,
    _normalize_resource_owner,
    _normalize_workspace_root_input,
    _serialize_managed_thread,
    resolve_managed_thread_list_query,
    serialize_managed_thread_turn_summary,
)


class TestNormalizeWorkspaceRootInput:
    @pytest.mark.parametrize(
        "invalid_input",
        [
            "",
            "  ",
            "C:\\Users\\test",
            "D:/path",
            "path/with\x00null",
            "path/to/../secret",
            "a/../b",
        ],
    )
    def test_rejects_invalid_inputs(self, invalid_input: str) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _normalize_workspace_root_input(invalid_input)
        assert exc_info.value.status_code == 400

    def test_accepts_clean_absolute_path(self) -> None:
        result = _normalize_workspace_root_input("/repos/my-repo")
        assert str(result) == "/repos/my-repo"

    def test_accepts_clean_relative_path(self) -> None:
        result = _normalize_workspace_root_input("repos/my-repo")
        assert str(result) == "repos/my-repo"

    def test_accepts_path_with_dots_in_names(self) -> None:
        result = _normalize_workspace_root_input("repos/my.repo.v2")
        assert "my.repo.v2" in str(result)


class TestNormalizeResourceOwner:
    def test_both_none_is_ok(self) -> None:
        kind, rid, repo_id = _normalize_resource_owner(
            resource_kind=None, resource_id=None
        )
        assert kind is None
        assert rid is None
        assert repo_id is None

    def test_valid_repo_pair(self) -> None:
        kind, rid, repo_id = _normalize_resource_owner(
            resource_kind="repo", resource_id="base"
        )
        assert kind == "repo"
        assert rid == "base"
        assert repo_id == "base"

    def test_valid_agent_workspace_pair(self) -> None:
        kind, rid, repo_id = _normalize_resource_owner(
            resource_kind="agent_workspace", resource_id="zc-main"
        )
        assert kind == "agent_workspace"
        assert rid == "zc-main"
        assert repo_id is None

    def test_resource_id_without_kind_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _normalize_resource_owner(resource_kind=None, resource_id="base")
        assert exc_info.value.status_code == 400
        assert "resource_kind is required" in exc_info.value.detail

    def test_kind_without_resource_id_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _normalize_resource_owner(resource_kind="repo", resource_id=None)
        assert exc_info.value.status_code == 400
        assert "resource_id is required" in exc_info.value.detail

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _normalize_resource_owner(resource_kind="cluster", resource_id="c1")
        assert exc_info.value.status_code == 400
        assert "resource_kind must be one of" in exc_info.value.detail

    def test_whitespace_is_normalized(self) -> None:
        kind, rid, repo_id = _normalize_resource_owner(
            resource_kind="  repo  ", resource_id="  base  "
        )
        assert kind == "repo"
        assert rid == "base"


class TestBuildOperatorStatusFields:
    def test_idle_status_is_reusable(self) -> None:
        fields = _build_operator_status_fields(
            normalized_status="idle", lifecycle_status="active"
        )
        assert fields["operator_status"] in {"idle", "reusable"}
        assert fields["is_reusable"] is True

    def test_running_status_is_not_reusable(self) -> None:
        fields = _build_operator_status_fields(
            normalized_status="running", lifecycle_status="active"
        )
        assert fields["is_reusable"] is False


class TestSerializeManagedThread:
    def test_includes_all_required_fields(self) -> None:
        raw = {
            "managed_thread_id": "thread-1",
            "agent": "codex",
            "lifecycle_status": "active",
            "normalized_status": "idle",
            "workspace_root": "/repos/base",
            "metadata": {},
        }
        payload = _serialize_managed_thread(raw)
        for key in (
            "lifecycle_status",
            "normalized_status",
            "status",
            "status_reason",
            "status_changed_at",
            "status_terminal",
            "status_turn_id",
            "accepts_messages",
            "resource_kind",
            "resource_id",
            "context_profile",
            "approval_mode",
            "agent_profile",
            "operator_status",
            "is_reusable",
        ):
            assert key in payload, f"Missing key: {key}"

    def test_active_thread_accepts_messages(self) -> None:
        raw = {
            "managed_thread_id": "thread-1",
            "lifecycle_status": "active",
            "normalized_status": "idle",
            "metadata": {},
        }
        payload = _serialize_managed_thread(raw)
        assert payload["accepts_messages"] is True

    def test_archived_thread_does_not_accept_messages(self) -> None:
        raw = {
            "managed_thread_id": "thread-1",
            "lifecycle_status": "archived",
            "normalized_status": "archived",
            "metadata": {},
        }
        payload = _serialize_managed_thread(raw)
        assert payload["accepts_messages"] is False

    def test_context_profile_from_metadata(self) -> None:
        raw = {
            "managed_thread_id": "thread-1",
            "lifecycle_status": "active",
            "metadata": {"context_profile": "car_core"},
        }
        payload = _serialize_managed_thread(raw)
        assert payload["context_profile"] == "car_core"

    def test_approval_mode_from_metadata(self) -> None:
        raw = {
            "managed_thread_id": "thread-1",
            "lifecycle_status": "active",
            "metadata": {"approval_mode": "read-only"},
        }
        payload = _serialize_managed_thread(raw)
        assert payload["approval_mode"] == "read-only"

    def test_status_falls_back_to_lifecycle_status(self) -> None:
        raw = {
            "managed_thread_id": "thread-1",
            "lifecycle_status": "active",
            "metadata": {},
        }
        payload = _serialize_managed_thread(raw)
        assert payload["status"] == "active"
        assert payload["normalized_status"] == "active"


class TestApplyChatBindingFields:
    def test_defaults_when_no_thread_id(self) -> None:
        payload: dict[str, Any] = {"managed_thread_id": None}
        result = _apply_chat_binding_fields(payload, managed_thread_id=None)
        assert result["chat_bound"] is False
        assert result["binding_count"] == 0
        assert result["binding_ids"] == []

    def test_binds_metadata_to_payload(self) -> None:
        payload: dict[str, Any] = {"managed_thread_id": "t-1"}
        metadata = {
            "t-1": {
                "chat_bound": True,
                "binding_kind": "discord",
                "binding_id": "b-1",
                "binding_count": 1,
                "binding_kinds": ["discord"],
                "binding_ids": ["b-1"],
                "cleanup_protected": True,
            }
        }
        result = _apply_chat_binding_fields(
            payload,
            managed_thread_id="t-1",
            binding_metadata_by_thread=metadata,
        )
        assert result["chat_bound"] is True
        assert result["binding_kind"] == "discord"
        assert result["binding_count"] == 1
        assert result["cleanup_protected"] is True


class TestAttachLatestExecutionFields:
    def test_no_execution_yields_none_fields(self) -> None:
        service = SimpleNamespace(
            get_running_execution=lambda _tid: None,
            get_latest_execution=lambda _tid: None,
        )
        payload: dict[str, Any] = {}
        _attach_latest_execution_fields(
            payload, service=service, managed_thread_id="t-1"
        )
        assert payload["latest_turn_id"] is None
        assert payload["latest_turn_status"] is None
        assert payload["latest_assistant_text"] == ""
        assert payload["latest_output_excerpt"] == ""

    def test_running_execution_takes_priority(self) -> None:
        running = SimpleNamespace(
            execution_id="turn-running",
            status="running",
            output_text="partial output",
        )
        latest = SimpleNamespace(
            execution_id="turn-latest",
            status="ok",
            output_text="latest output",
        )
        service = SimpleNamespace(
            get_running_execution=lambda _tid: running,
            get_latest_execution=lambda _tid: latest,
        )
        payload: dict[str, Any] = {}
        _attach_latest_execution_fields(
            payload, service=service, managed_thread_id="t-1"
        )
        assert payload["latest_turn_id"] == "turn-running"
        assert payload["latest_turn_status"] == "running"
        assert payload["latest_assistant_text"] == "partial output"

    def test_falls_back_to_latest_when_no_running(self) -> None:
        latest = SimpleNamespace(
            execution_id="turn-latest",
            status="ok",
            output_text="done",
        )
        service = SimpleNamespace(
            get_running_execution=lambda _tid: None,
            get_latest_execution=lambda _tid: latest,
        )
        payload: dict[str, Any] = {}
        _attach_latest_execution_fields(
            payload, service=service, managed_thread_id="t-1"
        )
        assert payload["latest_turn_id"] == "turn-latest"


class TestSerializeManagedThreadTurnSummary:
    def test_truncates_long_prompt(self) -> None:
        turn = {
            "managed_turn_id": "turn-1",
            "request_kind": "message",
            "status": "ok",
            "prompt": "x" * 200,
            "assistant_text": "y" * 200,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "error": None,
        }
        summary = serialize_managed_thread_turn_summary(turn)
        assert len(summary["prompt_preview"]) <= 120
        assert len(summary["assistant_preview"]) <= 120

    def test_handles_missing_fields(self) -> None:
        turn = {"managed_turn_id": "turn-1"}
        summary = serialize_managed_thread_turn_summary(turn)
        assert summary["prompt_preview"] == ""
        assert summary["assistant_preview"] == ""


class TestResolveManagedThreadListQuery:
    def test_active_status_remapped_to_lifecycle(self) -> None:
        query = resolve_managed_thread_list_query(
            agent=None,
            status="active",
            lifecycle_status=None,
            resource_kind=None,
            resource_id=None,
            limit=10,
        )
        assert query.lifecycle_status == "active"
        assert query.runtime_status is None

    def test_archived_status_remapped_to_lifecycle(self) -> None:
        query = resolve_managed_thread_list_query(
            agent=None,
            status="archived",
            lifecycle_status=None,
            resource_kind=None,
            resource_id=None,
            limit=10,
        )
        assert query.lifecycle_status == "archived"
        assert query.runtime_status is None

    def test_zero_limit_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_managed_thread_list_query(
                agent=None,
                status=None,
                lifecycle_status=None,
                resource_kind=None,
                resource_id=None,
                limit=0,
            )
        assert exc_info.value.status_code == 400
