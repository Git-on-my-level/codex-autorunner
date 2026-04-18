from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.surfaces.web.routes.pma_routes.publish import (
    build_publish_correlation_id,
    build_publish_message,
    enqueue_with_retry,
    normalize_optional_text,
    publish_automation_result,
    resolve_chat_state_path,
    resolve_publish_workspace_root,
)


class TestNormalizeOptionalText:
    @pytest.mark.parametrize(
        "input_val,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("hello", "hello"),
            ("  hello  ", "hello"),
            (123, "123"),
        ],
    )
    def test_normalizes_correctly(self, input_val: Any, expected: str | None) -> None:
        assert normalize_optional_text(input_val) == expected


class TestResolveChatStatePath:
    def test_default_state_file_resolved_against_hub_root(self) -> None:
        hub_root = Path("/tmp/hub_test_resolve").resolve()
        config = SimpleNamespace(
            root=hub_root,
            raw={},
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=config))
        )
        result = resolve_chat_state_path(
            request,
            section="pma",
            default_state_file="pma_state.sqlite3",
        )
        assert result == hub_root / "pma_state.sqlite3"

    def test_absolute_state_file_used_directly(self) -> None:
        config = SimpleNamespace(
            root=Path("/tmp/hub"),
            raw={"pma": {"state_file": "/custom/state.db"}},
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=config))
        )
        result = resolve_chat_state_path(
            request,
            section="pma",
            default_state_file="default.db",
        )
        assert result == Path("/custom/state.db")


class TestBuildPublishCorrelationId:
    def test_client_turn_id_priority(self) -> None:
        result = build_publish_correlation_id(
            result={"client_turn_id": "result-id", "turn_id": "turn-id"},
            client_turn_id="client-id",
            wake_up={"wakeup_id": "wake-id"},
        )
        assert result == "client-id"

    def test_result_client_turn_id_fallback(self) -> None:
        result = build_publish_correlation_id(
            result={"client_turn_id": "result-id", "turn_id": "turn-id"},
            client_turn_id=None,
            wake_up=None,
        )
        assert result == "result-id"

    def test_result_turn_id_fallback(self) -> None:
        result = build_publish_correlation_id(
            result={"turn_id": "turn-id"},
            client_turn_id=None,
            wake_up=None,
        )
        assert result == "turn-id"

    def test_wakeup_id_fallback(self) -> None:
        result = build_publish_correlation_id(
            result={},
            client_turn_id=None,
            wake_up={"wakeup_id": "wake-id"},
        )
        assert result == "wake-id"

    def test_generates_uuid_when_no_candidates(self) -> None:
        result = build_publish_correlation_id(
            result={},
            client_turn_id=None,
            wake_up=None,
        )
        assert result.startswith("pma-")
        assert len(result) == 16


class TestBuildPublishMessage:
    def test_ok_status_includes_output(self) -> None:
        msg = build_publish_message(
            result={"status": "ok", "message": "all done"},
            lifecycle_event={"event_type": "turn_completed"},
            wake_up=None,
            correlation_id="corr-1",
        )
        assert "all done" in msg
        assert "turn_completed" in msg
        assert "corr-1" in msg

    def test_error_status_includes_detail(self) -> None:
        msg = build_publish_message(
            result={"status": "error", "detail": "timeout"},
            lifecycle_event={"event_type": "turn_failed"},
            wake_up=None,
            correlation_id="corr-2",
        )
        assert "error" in msg
        assert "timeout" in msg
        assert "next_action" in msg

    def test_wakeup_source_fallback(self) -> None:
        msg = build_publish_message(
            result={"status": "ok", "message": "done"},
            lifecycle_event=None,
            wake_up={"source": "timer", "event_type": None},
            correlation_id="corr-3",
        )
        assert "timer" in msg

    def test_includes_repo_and_run_id(self) -> None:
        msg = build_publish_message(
            result={"status": "ok"},
            lifecycle_event={
                "event_type": "turn_completed",
                "repo_id": "base",
                "run_id": "run-1",
            },
            wake_up=None,
            correlation_id="corr-4",
        )
        assert "repo_id: base" in msg
        assert "run_id: run-1" in msg


class TestEnqueueWithRetry:
    @pytest.mark.anyio
    async def test_first_call_succeeds(self) -> None:
        call = AsyncMock()
        await enqueue_with_retry(call)
        call.assert_called_once()

    @pytest.mark.anyio
    async def test_retries_on_failure(self) -> None:
        call = AsyncMock(side_effect=[RuntimeError("fail"), None])
        await enqueue_with_retry(call)
        assert call.call_count == 2

    @pytest.mark.anyio
    async def test_raises_after_exhausting_retries(self) -> None:
        call = AsyncMock(side_effect=RuntimeError("permanent"))
        with pytest.raises(RuntimeError, match="permanent"):
            await enqueue_with_retry(call)
        assert call.call_count == 3


class TestResolvePublishWorkspaceRoot:
    def test_falls_back_to_managed_thread_workspace(self, tmp_path: Path) -> None:
        hub_root = (tmp_path / "hub").resolve()
        hub_root.mkdir(parents=True, exist_ok=True)
        workspace = (hub_root / "worktrees" / "repo-1").resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        thread = PmaThreadStore(hub_root).create_thread("codex", workspace)
        thread_id = str(thread["managed_thread_id"])
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(config=SimpleNamespace(root=hub_root, raw={}))
            )
        )

        result = resolve_publish_workspace_root(
            request=request,
            lifecycle_event=None,
            wake_up={"thread_id": thread_id},
        )

        assert result == workspace


class TestPublishAutomationResult:
    @pytest.mark.anyio
    async def test_forwards_delivery_target_and_thread_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hub_root = (tmp_path / "hub").resolve()
        hub_root.mkdir(parents=True, exist_ok=True)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(config=SimpleNamespace(root=hub_root, raw={}))
            )
        )
        captured: dict[str, Any] = {}

        async def _fake_deliver_pma_notification(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"route": "explicit", "targets": 1, "published": 1}

        monkeypatch.setattr(
            "codex_autorunner.surfaces.web.routes.pma_routes.publish.deliver_pma_notification",
            _fake_deliver_pma_notification,
        )

        result = await publish_automation_result(
            request=request,
            result={"status": "ok", "message": "done"},
            client_turn_id="client-1",
            lifecycle_event=None,
            wake_up={
                "wakeup_id": "wake-1",
                "thread_id": "thread-123",
                "delivery_target": {
                    "surface_kind": "discord",
                    "surface_key": "discord:preferred",
                },
                "metadata": {
                    "delivery_target": {
                        "surface_kind": "discord",
                        "surface_key": "discord:stale",
                    }
                },
            },
        )

        assert captured["managed_thread_id"] == "thread-123"
        assert captured["delivery_target"] == {
            "surface_kind": "discord",
            "surface_key": "discord:preferred",
        }
        assert result["delivery_status"] == "success"
        assert result["delivery_outcome"]["route"] == "explicit"
