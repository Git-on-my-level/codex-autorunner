from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.hub_projection_store import HubProjectionStore


def test_projection_store_round_trip(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fingerprint = ("v1", 123, {"nested": True})
    payload = {"repo_id": "alpha", "count": 3}

    store.set_cache("repo_runtime:alpha", fingerprint, payload)

    assert store.get_cache("repo_runtime:alpha", fingerprint) == payload
    assert store.get_cache("repo_runtime:alpha", ("v2", 123, {"nested": True})) is None


def test_projection_store_persists_across_instances(tmp_path: Path) -> None:
    fingerprint = ("projection", "stable")
    payload = {"chat_bound": True, "count": 4}

    HubProjectionStore(tmp_path, durable=False).set_cache(
        "chat_binding_counts_by_source",
        fingerprint,
        payload,
    )

    reloaded = HubProjectionStore(tmp_path, durable=False)
    assert reloaded.get_cache("chat_binding_counts_by_source", fingerprint) == payload
