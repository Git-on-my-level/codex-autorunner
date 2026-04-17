from __future__ import annotations

import asyncio
import threading
import time

import pytest

from codex_autorunner.core.hub_control_plane import (
    Binding,
    HubControlPlaneError,
    RemoteSurfaceBindingStore,
    SurfaceBindingListResponse,
)
from codex_autorunner.core.hub_control_plane.background_runner import (
    BoundedBackgroundRunner,
)


def _binding_from_mapping(**overrides: object) -> Binding:
    payload = {
        "binding_id": "binding-1",
        "surface_kind": "discord",
        "surface_key": "channel:1",
        "thread_target_id": "thread-1",
        "agent_id": "codex",
        "repo_id": "repo-1",
        "resource_kind": "repo",
        "resource_id": "repo-1",
        "mode": "reuse",
        "created_at": "2026-04-13T00:00:00Z",
        "updated_at": "2026-04-13T00:00:01Z",
        "disabled_at": None,
    }
    payload.update(overrides)
    return Binding.from_mapping(payload)


class _ListingClient:
    def __init__(self, bindings: list[Binding]) -> None:
        self.bindings = bindings
        self.calls: list[object] = []

    async def list_surface_bindings(self, request):
        self.calls.append(request)
        return SurfaceBindingListResponse(bindings=tuple(self.bindings))


class _UnavailableListingClient:
    async def list_surface_bindings(self, request):
        raise HubControlPlaneError("hub_unavailable", "hub offline")


class _UnavailableLookupClient:
    async def get_surface_binding(self, request):
        raise HubControlPlaneError("hub_unavailable", "hub offline")


class _LookupClient:
    def __init__(self, binding: Binding | None) -> None:
        self.binding = binding

    async def get_surface_binding(self, request):
        from codex_autorunner.core.hub_control_plane import SurfaceBindingResponse

        return SurfaceBindingResponse(binding=self.binding)


class _MissThenUnavailableLookupClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_surface_binding(self, request):
        from codex_autorunner.core.hub_control_plane import SurfaceBindingResponse

        self.calls += 1
        if self.calls == 1:
            return SurfaceBindingResponse(binding=None)
        raise HubControlPlaneError("hub_unavailable", "hub offline")


class _LoopBoundListingClient(_ListingClient):
    def __init__(self, bindings: list[Binding]) -> None:
        super().__init__(bindings)
        self._owner_thread_id = threading.get_ident()

    def clone_for_background_loop(self):
        return _ListingClient(list(self.bindings))

    async def list_surface_bindings(self, request):
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("Event loop is closed")
        return await super().list_surface_bindings(request)


class _SlowLookupClient:
    def __init__(self) -> None:
        self.finished = threading.Event()

    async def get_surface_binding(self, request):
        try:
            await asyncio.sleep(0.2)
        finally:
            self.finished.set()


class _BlockingLookupClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    async def get_surface_binding(self, request):
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        from codex_autorunner.core.hub_control_plane import SurfaceBindingResponse

        return SurfaceBindingResponse(binding=None)


def test_remote_surface_binding_store_lists_bindings_from_hub_authoritatively() -> None:
    client = _ListingClient(
        [
            _binding_from_mapping(
                binding_id="binding-hub",
                surface_kind="telegram",
                surface_key="chat:1",
                thread_target_id="thread-hub",
                repo_id="repo-2",
                resource_kind="agent_workspace",
                resource_id="wksp-1",
                agent_id="opencode",
            )
        ]
    )
    store = RemoteSurfaceBindingStore(client)
    store._remember(
        _binding_from_mapping(
            binding_id="binding-cache",
            surface_kind="telegram",
            surface_key="chat:1",
            thread_target_id="thread-cache",
            repo_id="repo-2",
            resource_kind="agent_workspace",
            resource_id="wksp-1",
            agent_id="opencode",
        )
    )

    listed = store.list_bindings(
        repo_id="repo-2",
        resource_kind="agent_workspace",
        resource_id="wksp-1",
        agent_id="opencode",
        surface_kind="telegram",
        limit=5,
    )

    assert [binding.binding_id for binding in listed] == ["binding-hub"]
    assert client.calls[0].to_dict() == {
        "thread_target_id": None,
        "repo_id": "repo-2",
        "resource_kind": "agent_workspace",
        "resource_id": "wksp-1",
        "agent_id": "opencode",
        "surface_kind": "telegram",
        "include_disabled": False,
        "limit": 5,
    }


def test_remote_surface_binding_store_falls_back_to_cache_when_hub_unavailable() -> (
    None
):
    store = RemoteSurfaceBindingStore(_UnavailableListingClient())
    store._remember(_binding_from_mapping(binding_id="binding-enabled"))
    store._remember(
        _binding_from_mapping(
            binding_id="binding-disabled",
            surface_key="channel:2",
            disabled_at="2026-04-13T01:00:00Z",
        )
    )

    listed = store.list_bindings(
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        agent_id="codex",
        surface_kind="discord",
        limit=10,
    )
    include_disabled = store.list_bindings(
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        agent_id="codex",
        surface_kind="discord",
        include_disabled=True,
        limit=10,
    )

    assert [binding.binding_id for binding in listed] == ["binding-enabled"]
    assert [binding.binding_id for binding in include_disabled] == [
        "binding-enabled",
        "binding-disabled",
    ]


def test_remote_surface_binding_store_get_binding_falls_back_to_cache_when_hub_unavailable() -> (
    None
):
    store = RemoteSurfaceBindingStore(_UnavailableLookupClient())
    cached = _binding_from_mapping(binding_id="binding-cache")
    store._remember(cached)

    binding = store.get_binding(surface_kind="discord", surface_key="channel:1")

    assert binding is cached


def test_remote_surface_binding_store_get_binding_requires_fresh_cache_for_fallback() -> (
    None
):
    store = RemoteSurfaceBindingStore(
        _UnavailableLookupClient(),
        cache_fallback_ttl_seconds=60.0,
    )
    cached = _binding_from_mapping(binding_id="binding-cache")
    store._remember(cached)
    store._binding_cached_at_by_key[("discord", "channel:1")] -= 61.0

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="discord", surface_key="channel:1")

    assert exc_info.value.code == "hub_unavailable"


def test_remote_surface_binding_store_get_binding_evicts_cache_on_successful_miss() -> (
    None
):
    store = RemoteSurfaceBindingStore(_MissThenUnavailableLookupClient())
    cached = _binding_from_mapping(binding_id="binding-cache")
    store._remember(cached)

    first = store.get_binding(surface_kind="discord", surface_key="channel:1")

    assert first is None
    assert ("discord", "channel:1") not in store._bindings_by_key

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="discord", surface_key="channel:1")

    assert exc_info.value.code == "hub_unavailable"


def test_remote_surface_binding_store_preserves_non_transport_hub_errors() -> None:
    class _RejectedListingClient:
        async def list_surface_bindings(self, request):
            raise HubControlPlaneError("hub_rejected", "invalid filter payload")

    store = RemoteSurfaceBindingStore(_RejectedListingClient())

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.list_bindings(limit=1)

    assert exc_info.value.code == "hub_rejected"
    assert str(exc_info.value) == "invalid filter payload"


def test_remote_surface_binding_store_clones_loop_bound_client_for_background_calls() -> (
    None
):
    store = RemoteSurfaceBindingStore(
        _LoopBoundListingClient([_binding_from_mapping(binding_id="binding-hub")])
    )

    listed = store.list_bindings(limit=5)

    assert [binding.binding_id for binding in listed] == ["binding-hub"]


def test_remote_surface_binding_store_timeout_does_not_wait_for_background_shutdown() -> (
    None
):
    client = _SlowLookupClient()
    store = RemoteSurfaceBindingStore(client, timeout_seconds=0.01)

    started = time.monotonic()
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="discord", surface_key="channel:1")
    elapsed = time.monotonic() - started

    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.details["operation"] == "get_surface_binding"
    assert exc_info.value.details["timeout_seconds"] == 0.01
    assert elapsed < 0.1
    assert client.finished.wait(timeout=1.0)


def test_remote_surface_binding_store_bounds_background_timeout_fallout() -> None:
    runner = BoundedBackgroundRunner(
        max_workers=1,
        saturation_wait_seconds=0.0,
        thread_name_prefix="test-hub-binding",
    )
    client = _BlockingLookupClient()
    store = RemoteSurfaceBindingStore(
        client,
        timeout_seconds=0.5,
        background_runner=runner,
    )

    errors: list[BaseException] = []

    def _first_call() -> None:
        try:
            store.get_binding(surface_kind="discord", surface_key="channel:1")
        except BaseException as exc:  # pragma: no cover - test assertion below
            errors.append(exc)

    worker = threading.Thread(target=_first_call)
    worker.start()
    assert client.started.wait(timeout=1.0)

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="discord", surface_key="channel:2")

    client.release.set()
    worker.join(timeout=1.0)
    runner.close()

    assert not errors
    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.details["operation"] == "get_surface_binding"
    assert exc_info.value.details["max_workers"] == 1
