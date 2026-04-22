from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.chat import (
    managed_thread_startup_recovery as recovery_module,
)


@pytest.mark.anyio
async def test_startup_recovery_selects_owned_binding_among_same_surface_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_surface_keys: list[str] = []

    class FakeThreadStore:
        def list_thread_ids_with_running_executions(self, limit=None):
            _ = limit
            return ["thread-1"]

        def get_running_turn(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return {"client_turn_id": "telegram:200:300:run-1"}

    class FakeOrchestrationService:
        thread_store = FakeThreadStore()

        def get_thread_target(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return SimpleNamespace(backend_thread_id="backend-thread-1")

        def get_running_execution(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return SimpleNamespace(execution_id="turn-1")

        def list_bindings(self, **kwargs: object):
            assert kwargs["surface_kind"] == "telegram"
            return [
                SimpleNamespace(surface_key="100:200"),
                SimpleNamespace(surface_key="200:300"),
            ]

        async def recover_running_execution_from_harness(
            self,
            managed_thread_id: str,
            *,
            default_error: str,
        ):
            assert managed_thread_id == "thread-1"
            assert default_error == "Telegram PMA turn failed"
            return SimpleNamespace(
                status="ok",
                output_text="done",
                error=None,
                execution_id="turn-1",
            )

        def recover_running_execution_after_restart(self, managed_thread_id: str):
            _ = managed_thread_id
            return None

    async def _handoff(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    monkeypatch.setattr(
        recovery_module,
        "handoff_managed_thread_final_delivery",
        _handoff,
    )

    service = SimpleNamespace(_logger=logging.getLogger("test.startup_recovery"))
    orchestration_service = FakeOrchestrationService()

    await recovery_module.recover_managed_thread_executions_on_startup(
        service,
        surface_kind="telegram",
        build_orchestration_service=lambda _service: orchestration_service,
        build_durable_delivery=lambda _service, surface_key, *_args: (
            captured_surface_keys.append(surface_key) or object()
        ),
        public_execution_error="Telegram PMA turn failed",
        failure_event_name="telegram.turn.startup_execution_recovery_failed",
        finished_event_name="telegram.turn.startup_execution_recovery_finished",
    )

    assert captured_surface_keys == ["200:300"]


def test_find_surface_key_for_running_execution_returns_none_without_matching_binding() -> (
    None
):
    class FakeBindingStore:
        def list_bindings(self, **kwargs: object):
            assert kwargs["surface_kind"] == "discord"
            return [
                SimpleNamespace(surface_key="channel-other"),
                SimpleNamespace(surface_key="channel-older"),
            ]

    class FakeThreadStore:
        def get_running_turn(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return {"client_turn_id": "discord:channel-owned:run-1"}

    assert (
        recovery_module.find_surface_key_for_running_execution(
            FakeBindingStore(),
            FakeThreadStore(),
            managed_thread_id="thread-1",
            surface_kind="discord",
        )
        is None
    )


@pytest.mark.anyio
async def test_startup_recovery_rearms_pending_queue_for_single_owned_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered_pending: list[tuple[str, str]] = []

    class FakeThreadStore:
        def list_thread_ids_with_running_executions(self, limit=None):
            _ = limit
            return []

        def list_pending_turn_queue_items(self, managed_thread_id: str, limit=200):
            assert managed_thread_id == "thread-1"
            assert limit == recovery_module._PENDING_QUEUE_SCAN_LIMIT
            return [{"managed_turn_id": "turn-queued-1"}]

        def get_turn(self, managed_thread_id: str, managed_turn_id: str):
            assert managed_thread_id == "thread-1"
            assert managed_turn_id == "turn-queued-1"
            return {"client_turn_id": "discord:channel-1:queued-1"}

    class FakeOrchestrationService:
        thread_store = FakeThreadStore()

        def list_bindings(self, **kwargs: object):
            assert kwargs["surface_kind"] == "discord"
            if "thread_target_id" in kwargs:
                return [SimpleNamespace(surface_key="channel-1")]
            return [SimpleNamespace(thread_target_id="thread-1")]

        def get_thread_target(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return SimpleNamespace(workspace_root="/workspace/thread-1")

    async def _handoff(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    monkeypatch.setattr(
        recovery_module,
        "handoff_managed_thread_final_delivery",
        _handoff,
    )

    service = SimpleNamespace(
        _logger=logging.getLogger("test.startup_recovery.pending")
    )
    orchestration_service = FakeOrchestrationService()

    await recovery_module.recover_managed_thread_executions_on_startup(
        service,
        surface_kind="discord",
        build_orchestration_service=lambda _service: orchestration_service,
        build_durable_delivery=lambda *_args: None,
        recover_pending_queue=lambda _service, _orch, surface_key, managed_thread_id, _thread: (
            recovered_pending.append((surface_key, managed_thread_id)) or True
        ),
        public_execution_error="Discord PMA turn failed",
        failure_event_name="discord.turn.startup_execution_recovery_failed",
        finished_event_name="discord.turn.startup_execution_recovery_finished",
    )

    assert recovered_pending == [("channel-1", "thread-1")]


@pytest.mark.anyio
async def test_startup_recovery_skips_pending_queue_when_surface_ownership_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered_pending: list[tuple[str, str]] = []

    class FakeThreadStore:
        def list_thread_ids_with_running_executions(self, limit=None):
            _ = limit
            return []

        def list_pending_turn_queue_items(self, managed_thread_id: str, limit=200):
            assert managed_thread_id == "thread-1"
            assert limit == recovery_module._PENDING_QUEUE_SCAN_LIMIT
            return [
                {"managed_turn_id": "turn-queued-1"},
                {"managed_turn_id": "turn-queued-2"},
            ]

        def get_turn(self, managed_thread_id: str, managed_turn_id: str):
            assert managed_thread_id == "thread-1"
            return {
                "turn-queued-1": {
                    "client_turn_id": "telegram:100:200:queued-1",
                },
                "turn-queued-2": {
                    "client_turn_id": "telegram:300:400:queued-2",
                },
            }[managed_turn_id]

    class FakeOrchestrationService:
        thread_store = FakeThreadStore()

        def list_bindings(self, **kwargs: object):
            assert kwargs["surface_kind"] == "telegram"
            if "thread_target_id" in kwargs:
                return [
                    SimpleNamespace(surface_key="100:200"),
                    SimpleNamespace(surface_key="300:400"),
                ]
            return [SimpleNamespace(thread_target_id="thread-1")]

        def get_thread_target(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return SimpleNamespace(workspace_root="/workspace/thread-1")

    async def _handoff(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    monkeypatch.setattr(
        recovery_module,
        "handoff_managed_thread_final_delivery",
        _handoff,
    )

    service = SimpleNamespace(
        _logger=logging.getLogger("test.startup_recovery.pending")
    )
    orchestration_service = FakeOrchestrationService()

    await recovery_module.recover_managed_thread_executions_on_startup(
        service,
        surface_kind="telegram",
        build_orchestration_service=lambda _service: orchestration_service,
        build_durable_delivery=lambda *_args: None,
        recover_pending_queue=lambda _service, _orch, surface_key, managed_thread_id, _thread: (
            recovered_pending.append((surface_key, managed_thread_id)) or True
        ),
        public_execution_error="Telegram PMA turn failed",
        failure_event_name="telegram.turn.startup_execution_recovery_failed",
        finished_event_name="telegram.turn.startup_execution_recovery_finished",
    )

    assert recovered_pending == []


@pytest.mark.anyio
async def test_startup_recovery_scans_past_legacy_pending_queue_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered_pending: list[tuple[str, str]] = []

    class FakeThreadStore:
        def list_thread_ids_with_running_executions(self, limit=None):
            _ = limit
            return []

        def list_pending_turn_queue_items(self, managed_thread_id: str, limit=200):
            assert managed_thread_id == "thread-1"
            assert limit == recovery_module._PENDING_QUEUE_SCAN_LIMIT
            items = [
                {"managed_turn_id": f"turn-queued-{index}"} for index in range(250)
            ]
            items.append({"managed_turn_id": "turn-owned"})
            return items

        def get_turn(self, managed_thread_id: str, managed_turn_id: str):
            assert managed_thread_id == "thread-1"
            if managed_turn_id == "turn-owned":
                return {"client_turn_id": "discord:channel-owned:queued-owned"}
            return {"client_turn_id": f"discord:channel-other:{managed_turn_id}"}

    class FakeOrchestrationService:
        thread_store = FakeThreadStore()

        def list_bindings(self, **kwargs: object):
            assert kwargs["surface_kind"] == "discord"
            if "thread_target_id" in kwargs:
                return [SimpleNamespace(surface_key="channel-owned")]
            return [SimpleNamespace(thread_target_id="thread-1")]

        def get_thread_target(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return SimpleNamespace(workspace_root="/workspace/thread-1")

    async def _handoff(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    monkeypatch.setattr(
        recovery_module,
        "handoff_managed_thread_final_delivery",
        _handoff,
    )

    service = SimpleNamespace(
        _logger=logging.getLogger("test.startup_recovery.pending.scan_limit")
    )
    orchestration_service = FakeOrchestrationService()

    await recovery_module.recover_managed_thread_executions_on_startup(
        service,
        surface_kind="discord",
        build_orchestration_service=lambda _service: orchestration_service,
        build_durable_delivery=lambda *_args: None,
        recover_pending_queue=lambda _service, _orch, surface_key, managed_thread_id, _thread: (
            recovered_pending.append((surface_key, managed_thread_id)) or True
        ),
        public_execution_error="Discord PMA turn failed",
        failure_event_name="discord.turn.startup_execution_recovery_failed",
        finished_event_name="discord.turn.startup_execution_recovery_finished",
    )

    assert recovered_pending == [("channel-owned", "thread-1")]
