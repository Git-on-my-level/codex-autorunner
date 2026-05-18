from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.surfaces.web.schemas import ManagedThreadMessageRequest
from codex_autorunner.surfaces.web.services.pma import managed_thread_runtime


def _request(tmp_path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(root=tmp_path, raw={}),
                hub_client=None,
            )
        )
    )


class _FakeStore:
    def __init__(self, root):
        self.root = root

    def get_thread(self, managed_thread_id: str) -> dict[str, Any] | None:
        return {
            "managed_thread_id": managed_thread_id,
            "thread_target_id": managed_thread_id,
            "lifecycle_status": "active",
            "metadata": {},
        }


@pytest.mark.anyio
async def test_send_runtime_wires_queued_send_to_service_ports(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}
    service = SimpleNamespace()
    options = SimpleNamespace(live_backend_thread_id="backend-1")

    async def _run_send(**kwargs):
        captured.update(kwargs)
        return {"send_state": "enqueued", "execution_state": "queued"}

    monkeypatch.setattr(managed_thread_runtime, "ManagedThreadStore", _FakeStore)
    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: service,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "resolve_managed_thread_message_options",
        lambda *args, **kwargs: options,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "run_managed_thread_message_send",
        _run_send,
    )

    payload = ManagedThreadMessageRequest(
        message="queued",
        wait_for_confirmation=False,
    )
    result = await managed_thread_runtime.send_managed_thread_message_runtime(
        "thread-1",
        _request(tmp_path),
        payload,
        get_runtime_state=lambda: None,
    )

    assert result["execution_state"] == "queued"
    assert captured["payload"].wait_for_confirmation is False
    assert captured["service"] is service
    assert captured["options"] is options
    assert captured["ports"].ensure_queue_worker is (
        managed_thread_runtime.ensure_managed_thread_queue_worker
    )


@pytest.mark.anyio
async def test_send_runtime_wires_wait_for_confirmation_send(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    async def _run_send(**kwargs):
        captured.update(kwargs)
        return {"send_state": "accepted", "execution_state": "completed"}

    monkeypatch.setattr(managed_thread_runtime, "ManagedThreadStore", _FakeStore)
    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "resolve_managed_thread_message_options",
        lambda *args, **kwargs: SimpleNamespace(live_backend_thread_id="backend-1"),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "run_managed_thread_message_send",
        _run_send,
    )

    payload = ManagedThreadMessageRequest(
        message="confirmed",
        wait_for_confirmation=True,
    )
    result = await managed_thread_runtime.send_managed_thread_message_runtime(
        "thread-1",
        _request(tmp_path),
        payload,
        get_runtime_state=lambda: None,
    )

    assert result["execution_state"] == "completed"
    assert captured["payload"].wait_for_confirmation is True
    assert (
        captured["ports"].begin_execution
        is managed_thread_runtime.begin_runtime_thread_execution
    )


@pytest.mark.anyio
async def test_recovery_runtime_passes_bound_progress_hook(monkeypatch):
    calls: list[str] = []

    async def _recover_bound_progress(*args, **kwargs):
        calls.append("hook")
        return True

    async def _recover_orphans(app, *, ports, recover_orphaned_executions):
        assert (
            recover_orphaned_executions
            is managed_thread_runtime.recover_orphaned_executions
        )
        await ports.recover_bound_progress_execution(
            app,
            service=SimpleNamespace(),
            thread_store=SimpleNamespace(),
            managed_thread_id="thread-1",
            thread=SimpleNamespace(),
            execution=SimpleNamespace(),
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_recover_pma_bound_chat_execution",
        _recover_bound_progress,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "recover_orphaned_pma_managed_thread_executions",
        _recover_orphans,
    )

    await managed_thread_runtime.recover_orphaned_managed_thread_executions(
        SimpleNamespace()
    )

    assert calls == ["hook"]


@pytest.mark.anyio
async def test_interrupt_runtime_maps_through_control_service(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    async def _interrupt(**kwargs):
        captured.update(kwargs)
        return {"status": "error", "interrupt_state": "failed"}

    monkeypatch.setattr(
        managed_thread_runtime,
        "interrupt_managed_thread_via_orchestration",
        _interrupt,
    )

    result = await managed_thread_runtime.interrupt_managed_thread_runtime(
        "thread-1",
        _request(tmp_path),
    )

    assert result["interrupt_state"] == "failed"
    assert captured["managed_thread_id"] == "thread-1"
    assert captured["build_service"] is (
        managed_thread_runtime._build_managed_thread_orchestration_service
    )
    assert captured["get_live_thread_runtime_binding"] is (
        managed_thread_runtime._get_live_thread_runtime_binding
    )
