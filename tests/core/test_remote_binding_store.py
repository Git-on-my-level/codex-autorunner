from __future__ import annotations

import pytest

from codex_autorunner.core.hub_control_plane import (
    Binding,
    HubControlPlaneError,
    RemoteSurfaceBindingStore,
    SurfaceBindingListResponse,
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


def test_remote_surface_binding_store_preserves_non_transport_hub_errors() -> None:
    class _RejectedListingClient:
        async def list_surface_bindings(self, request):
            raise HubControlPlaneError("hub_rejected", "invalid filter payload")

    store = RemoteSurfaceBindingStore(_RejectedListingClient())

    with pytest.raises(HubControlPlaneError) as exc_info:
        store.list_bindings(limit=1)

    assert exc_info.value.code == "hub_rejected"
    assert str(exc_info.value) == "invalid filter payload"
