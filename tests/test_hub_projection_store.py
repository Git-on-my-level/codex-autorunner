from __future__ import annotations

import time
from pathlib import Path

import codex_autorunner.core.hub_projection_store as projection_store_module
from codex_autorunner.core.hub_projection_store import (
    HubProjectionStore,
    path_stat_fingerprint,
)


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


def test_projection_store_respects_max_age(tmp_path: Path, monkeypatch) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fingerprint = ("stable",)
    payload = {"value": 1}

    monkeypatch.setattr(
        projection_store_module,
        "now_iso",
        lambda: "1970-01-01T00:16:40+00:00",
    )
    monkeypatch.setattr(projection_store_module, "_current_utc_ts", lambda: 1000.0)
    store.set_cache("repo_runtime:alpha", fingerprint, payload)

    monkeypatch.setattr(projection_store_module, "_current_utc_ts", lambda: 1002.0)
    assert (
        store.get_cache("repo_runtime:alpha", fingerprint, max_age_seconds=1.0) is None
    )


def test_projection_store_namespace_isolation(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fingerprint = ("stable",)
    payload_a = {"repo_id": "alpha"}
    payload_b = {"repo_id": "beta"}

    store.set_cache("key-1", fingerprint, payload_a, namespace="ns-alpha")
    store.set_cache("key-1", fingerprint, payload_b, namespace="ns-beta")

    assert store.get_cache("key-1", fingerprint, namespace="ns-alpha") == payload_a
    assert store.get_cache("key-1", fingerprint, namespace="ns-beta") == payload_b
    assert store.get_cache("key-1", fingerprint, namespace="ns-missing") is None


def test_projection_store_fingerprint_mismatch_returns_none(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    store.set_cache("key-1", ("v1",), {"data": True})

    assert store.get_cache("key-1", ("v1",)) == {"data": True}
    assert store.get_cache("key-1", ("v2",)) is None
    assert store.get_cache("key-1", ("v1", "extra")) is None


def test_projection_store_invalidation(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fingerprint = ("stable",)
    store.set_cache("key-1", fingerprint, {"data": True})

    assert store.get_cache("key-1", fingerprint) == {"data": True}

    store.invalidate_cache("key-1")

    assert store.get_cache("key-1", fingerprint) is None


def test_projection_store_invalidation_is_namespace_scoped(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fingerprint = ("stable",)
    store.set_cache("key-1", fingerprint, {"a": 1}, namespace="ns-a")
    store.set_cache("key-1", fingerprint, {"b": 2}, namespace="ns-b")

    store.invalidate_cache("key-1", namespace="ns-a")

    assert store.get_cache("key-1", fingerprint, namespace="ns-a") is None
    assert store.get_cache("key-1", fingerprint, namespace="ns-b") == {"b": 2}


def test_projection_store_put_get_delete_aliases(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fp = ("fp",)
    store.put(namespace="ns1", key="k", fingerprint=fp, payload={"v": 1})

    assert store.get(namespace="ns1", key="k", fingerprint=fp) == {"v": 1}

    store.delete(namespace="ns1", key="k")
    assert store.get(namespace="ns1", key="k", fingerprint=fp) is None


def test_projection_store_delete_namespace_clears_all_keys(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    fp = ("fp",)
    store.put(namespace="ns1", key="k1", fingerprint=fp, payload={"a": 1})
    store.put(namespace="ns1", key="k2", fingerprint=fp, payload={"b": 2})
    store.put(namespace="ns2", key="k1", fingerprint=fp, payload={"c": 3})

    store.delete(namespace="ns1")

    assert store.get(namespace="ns1", key="k1", fingerprint=fp) is None
    assert store.get(namespace="ns1", key="k2", fingerprint=fp) is None
    assert store.get(namespace="ns2", key="k1", fingerprint=fp) == {"c": 3}


def test_path_stat_fingerprint_returns_exists_mtime_size_for_existing_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "testfile"
    target.write_text("hello world\n", encoding="utf-8")
    stat = target.stat()

    exists, mtime_ns, size = path_stat_fingerprint(target)

    assert exists is True
    assert mtime_ns == int(stat.st_mtime_ns)
    assert size == int(stat.st_size)


def test_path_stat_fingerprint_returns_false_for_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"

    exists, mtime_ns, size = path_stat_fingerprint(missing)

    assert exists is False
    assert mtime_ns is None
    assert size is None


def test_path_stat_fingerprint_changes_when_file_modified(tmp_path: Path) -> None:
    target = tmp_path / "mutable"
    target.write_text("v1\n", encoding="utf-8")
    _, mtime_before, size_before = path_stat_fingerprint(target)

    time.sleep(0.01)
    target.write_text("v2 with more content\n", encoding="utf-8")
    _, mtime_after, size_after = path_stat_fingerprint(target)

    assert size_after != size_before
    assert mtime_after != mtime_before
