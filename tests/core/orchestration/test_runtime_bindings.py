from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.core.orchestration.runtime_bindings import (
    BACKEND_BINDING_BOUND,
    BACKEND_BINDING_FRESH_REQUIRED,
    BACKEND_BINDING_INVALID,
    BACKEND_BINDING_SUSPECT,
    BackendConversationBindingService,
    RuntimeThreadBinding,
    get_runtime_thread_binding,
    mark_thread_store_runtime_binding_state,
    runtime_thread_binding_allows_resume,
    set_runtime_thread_binding,
)


def test_backend_conversation_binding_service_marks_existing_state(
    tmp_path: Path,
) -> None:
    service = BackendConversationBindingService(tmp_path)
    service.set(
        "thread-1",
        backend_thread_id="backend-1",
        backend_runtime_instance_id="runtime-1",
    )

    marked = service.mark_state(
        "thread-1",
        binding_state=BACKEND_BINDING_SUSPECT,
        state_reason="startup_lost_backend_binding",
    )

    assert marked == RuntimeThreadBinding(
        backend_thread_id="backend-1",
        backend_runtime_instance_id="runtime-1",
        binding_state=BACKEND_BINDING_SUSPECT,
        state_reason="startup_lost_backend_binding",
    )
    assert service.get("thread-1") == marked


def test_backend_conversation_binding_service_logs_state_transition(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = BackendConversationBindingService(tmp_path)
    service.set(
        "thread-1",
        backend_thread_id="backend-1",
        backend_runtime_instance_id="runtime-1",
    )

    with caplog.at_level(
        logging.INFO,
        logger="codex_autorunner.core.orchestration.runtime_bindings",
    ):
        marked = service.mark_fresh_required(
            "thread-1",
            state_reason="fresh_conversation_required",
        )

    assert marked == RuntimeThreadBinding(
        backend_thread_id="backend-1",
        backend_runtime_instance_id="runtime-1",
        binding_state=BACKEND_BINDING_FRESH_REQUIRED,
        state_reason="fresh_conversation_required",
    )
    payloads = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "codex_autorunner.core.orchestration.runtime_bindings"
    ]
    assert any(
        payload.get("event") == "orchestration.thread.binding_transition"
        and payload.get("previous_state") == BACKEND_BINDING_BOUND
        and payload.get("next_state") == BACKEND_BINDING_FRESH_REQUIRED
        and payload.get("reason") == "fresh_conversation_required"
        for payload in payloads
    )


def test_runtime_thread_binding_allows_resume_for_bound_and_suspect_only() -> None:
    assert runtime_thread_binding_allows_resume(
        RuntimeThreadBinding("backend-1", binding_state=BACKEND_BINDING_BOUND)
    )
    assert runtime_thread_binding_allows_resume(
        RuntimeThreadBinding("backend-1", binding_state=BACKEND_BINDING_SUSPECT)
    )
    assert not runtime_thread_binding_allows_resume(
        RuntimeThreadBinding("backend-1", binding_state=BACKEND_BINDING_INVALID)
    )
    assert not runtime_thread_binding_allows_resume(
        RuntimeThreadBinding("backend-1", binding_state=BACKEND_BINDING_FRESH_REQUIRED)
    )
    assert not runtime_thread_binding_allows_resume(RuntimeThreadBinding(None))


def test_mark_thread_store_runtime_binding_state_falls_back_to_setter() -> None:
    class _Store:
        def __init__(self) -> None:
            self.binding: Optional[RuntimeThreadBinding] = None

        def mark_thread_runtime_binding_state(
            self,
            thread_target_id: str,
            *,
            binding_state: str,
            state_reason: Optional[str] = None,
        ) -> Optional[RuntimeThreadBinding]:
            assert thread_target_id == "thread-1"
            return None

        def set_thread_backend_binding(
            self,
            thread_target_id: str,
            backend_thread_id: Optional[str],
            *,
            binding_state: str = BACKEND_BINDING_BOUND,
            state_reason: Optional[str] = None,
        ) -> None:
            assert thread_target_id == "thread-1"
            self.binding = RuntimeThreadBinding(
                backend_thread_id=backend_thread_id,
                binding_state=binding_state,
                state_reason=state_reason,
            )

        def get_thread_runtime_binding(
            self, thread_target_id: str
        ) -> Optional[RuntimeThreadBinding]:
            assert thread_target_id == "thread-1"
            return self.binding

    store = _Store()

    marked = mark_thread_store_runtime_binding_state(
        store,
        "thread-1",
        backend_thread_id="backend-1",
        binding_state=BACKEND_BINDING_INVALID,
        state_reason="interrupt_thread_not_found",
    )

    assert marked == RuntimeThreadBinding(
        backend_thread_id="backend-1",
        binding_state=BACKEND_BINDING_INVALID,
        state_reason="interrupt_thread_not_found",
    )


def test_existing_runtime_binding_functions_delegate_to_service(tmp_path: Path) -> None:
    set_runtime_thread_binding(
        tmp_path,
        "thread-1",
        backend_thread_id="backend-1",
        binding_state=BACKEND_BINDING_BOUND,
    )

    assert get_runtime_thread_binding(tmp_path, "thread-1") == RuntimeThreadBinding(
        backend_thread_id="backend-1",
        binding_state=BACKEND_BINDING_BOUND,
    )
