"""Characterization tests for hub single-owner, control-plane, worktree, and cache invariants.

This file locks down behavioral contracts so later convergence tickets in the 1400 band
cannot silently break them:

1. Error taxonomy: closed code set, default retryability, round-trip serialization.
2. Remote store fallback: binding store uses cache only on hub_unavailable;
   execution store never falls back.
3. Topology persistence: load tolerates missing/corrupt data; save is crash-safe;
   PMA artifact refresh is non-fatal.
4. Worktree cleanup safety: archive-required gate, chat-binding gate, force escape
   hatch.
5. Side-process import boundary: surfaces and side-processes must not import hub
   shared-state owners without an allowlist entry (extends architecture_boundaries).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from codex_autorunner.core.hub_control_plane.errors import (
    HubControlPlaneError,
    HubControlPlaneErrorInfo,
    default_retryable,
)

# ---------------------------------------------------------------------------
# 1. Error taxonomy characterization
# ---------------------------------------------------------------------------

_ALL_ERROR_CODES = [
    "hub_unavailable",
    "hub_incompatible",
    "hub_rejected",
    "transport_failure",
    "protocol_failure",
]


@pytest.mark.parametrize("code", _ALL_ERROR_CODES)
def test_default_retryable_mapping_is_closed_and_complete(code: str) -> None:
    is_retryable = default_retryable(code)
    if code in {"hub_unavailable", "transport_failure"}:
        assert is_retryable is True
    else:
        assert is_retryable is False


def test_error_info_rejects_unknown_code() -> None:
    with pytest.raises(ValueError, match="Unknown hub control-plane error code"):
        HubControlPlaneErrorInfo.from_mapping(
            {"code": "bogus_code", "message": "test", "retryable": True}
        )


def test_error_info_rejects_empty_message() -> None:
    with pytest.raises(ValueError, match="require a message"):
        HubControlPlaneErrorInfo.from_mapping(
            {"code": "hub_unavailable", "message": "   "}
        )


@pytest.mark.parametrize("code", _ALL_ERROR_CODES)
def test_hub_control_plane_error_defaults_retryable_from_code(code: str) -> None:
    err = HubControlPlaneError(code, "msg")
    assert err.retryable == default_retryable(code)


@pytest.mark.parametrize("code", _ALL_ERROR_CODES)
def test_hub_control_plane_error_explicit_retryable_overrides(code: str) -> None:
    err = HubControlPlaneError(code, "msg", retryable=False)
    assert err.retryable is False
    err2 = HubControlPlaneError(code, "msg", retryable=True)
    assert err2.retryable is True


@pytest.mark.parametrize("code", _ALL_ERROR_CODES)
def test_error_info_round_trip(code: str) -> None:
    info = HubControlPlaneErrorInfo(
        code=code,
        message="test message",
        retryable=default_retryable(code),
        details={"key": "value"},
    )
    serialized = info.to_dict()
    restored = HubControlPlaneErrorInfo.from_mapping(serialized)
    assert restored.code == code
    assert restored.message == "test message"
    assert restored.retryable == default_retryable(code)
    assert restored.details == {"key": "value"}


def test_error_info_defaults_retryable_when_missing() -> None:
    info = HubControlPlaneErrorInfo.from_mapping(
        {"code": "hub_unavailable", "message": "test"}
    )
    assert info.retryable is True
    info2 = HubControlPlaneErrorInfo.from_mapping(
        {"code": "hub_incompatible", "message": "test"}
    )
    assert info2.retryable is False


def test_error_details_are_defensive_copies() -> None:
    original_details = {"k": "v"}
    err = HubControlPlaneError("hub_unavailable", "msg", details=original_details)
    err.details["k"] = "mutated"
    assert original_details["k"] == "v"


def test_error_info_details_are_defensive_copies() -> None:
    details = {"k": "v"}
    info = HubControlPlaneErrorInfo.from_mapping(
        {"code": "hub_unavailable", "message": "test", "details": details}
    )
    info.details["k"] = "mutated"
    assert details["k"] == "v"


def test_hub_control_plane_error_from_info_round_trip() -> None:
    info = HubControlPlaneErrorInfo(
        code="hub_rejected",
        message="bad request",
        retryable=False,
        details={"field": "x"},
    )
    err = HubControlPlaneError.from_info(info)
    assert err.code == "hub_rejected"
    assert str(err) == "bad request"
    assert err.retryable is False
    assert err.info == info


# ---------------------------------------------------------------------------
# 2. Remote store fallback characterization
# ---------------------------------------------------------------------------


def _fake_binding(**overrides: Any) -> SimpleNamespace:
    base = SimpleNamespace(
        binding_id="b1",
        surface_kind="telegram",
        surface_key="chat:1",
        thread_target_id="t1",
        agent_id="codex",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        mode="reuse",
        disabled_at=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class _FakeClient:
    def __init__(self, *, side_effect: Exception | None = None) -> None:
        self._side_effect = side_effect

    async def get_surface_binding(self, request: Any) -> Any:
        if self._side_effect:
            raise self._side_effect
        return SimpleNamespace(binding=_fake_binding())

    async def list_surface_bindings(self, request: Any) -> Any:
        if self._side_effect:
            raise self._side_effect
        return SimpleNamespace(bindings=[_fake_binding()])

    async def upsert_surface_binding(self, request: Any) -> Any:
        if self._side_effect:
            raise self._side_effect
        return SimpleNamespace(binding=_fake_binding())


def _make_binding_store(
    client: _FakeClient,
    *,
    cache_fallback_ttl_seconds: float = 300.0,
) -> Any:
    from codex_autorunner.core.hub_control_plane.remote_binding_store import (
        RemoteSurfaceBindingStore,
    )

    return RemoteSurfaceBindingStore(
        client,
        timeout_seconds=5.0,
        cache_fallback_ttl_seconds=cache_fallback_ttl_seconds,
    )


def test_binding_store_maps_connection_error_to_hub_unavailable() -> None:
    store = _make_binding_store(_FakeClient(side_effect=ConnectionError("refused")))
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.retryable is True
    assert "operation" in exc_info.value.details


def test_binding_store_maps_os_error_to_hub_unavailable() -> None:
    store = _make_binding_store(_FakeClient(side_effect=OSError("broken pipe")))
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.retryable is True


def test_binding_store_passes_hub_incompatible_through() -> None:
    store = _make_binding_store(
        _FakeClient(
            side_effect=HubControlPlaneError(
                "hub_incompatible", "version mismatch", retryable=False
            )
        )
    )
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert exc_info.value.code == "hub_incompatible"
    assert exc_info.value.retryable is False


def test_binding_store_passes_hub_rejected_through() -> None:
    store = _make_binding_store(
        _FakeClient(
            side_effect=HubControlPlaneError(
                "hub_rejected", "bad payload", retryable=False
            )
        )
    )
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert exc_info.value.code == "hub_rejected"
    assert exc_info.value.retryable is False


def test_binding_store_maps_transport_failure_to_hub_unavailable() -> None:
    store = _make_binding_store(
        _FakeClient(
            side_effect=HubControlPlaneError(
                "transport_failure", "timeout", retryable=True
            )
        )
    )
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.retryable is True
    assert exc_info.value.details.get("cause_code") == "transport_failure"


def test_binding_store_upsert_has_no_cache_fallback() -> None:
    store = _make_binding_store(_FakeClient(side_effect=ConnectionError("down")))
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.upsert_binding(
            surface_kind="telegram",
            surface_key="chat:1",
            thread_target_id="t1",
            agent_id="codex",
            repo_id="repo-1",
        )
    assert exc_info.value.code == "hub_unavailable"


def test_binding_store_get_falls_back_to_cache_on_hub_unavailable() -> None:
    client = _FakeClient()
    store = _make_binding_store(client, cache_fallback_ttl_seconds=600.0)
    result = store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert result is not None
    assert getattr(result, "binding_id", None) == "b1"

    client._side_effect = ConnectionError("now down")

    fallback = store.get_binding(surface_kind="telegram", surface_key="chat:1")
    assert fallback is not None
    assert getattr(fallback, "binding_id", None) == "b1"


def test_binding_store_list_falls_back_to_empty_on_hub_unavailable_with_no_cache() -> (
    None
):
    store = _make_binding_store(_FakeClient(side_effect=ConnectionError("down")))
    result = store.list_bindings(surface_kind="telegram", limit=10)
    assert result == []


def test_execution_store_has_no_cache_fallback_on_hub_unavailable() -> None:
    from codex_autorunner.core.hub_control_plane.remote_execution_store import (
        RemoteThreadExecutionStore,
    )

    class _FailClient:
        async def get_thread_target(self, request: Any) -> Any:
            raise ConnectionError("hub unreachable")

    store = RemoteThreadExecutionStore(_FailClient(), timeout_seconds=2.0)
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_thread_target(thread_target_id="t1")
    assert exc_info.value.code == "hub_unavailable"
    assert exc_info.value.retryable is True


def test_execution_store_passes_hub_incompatible_through() -> None:
    from codex_autorunner.core.hub_control_plane.remote_execution_store import (
        RemoteThreadExecutionStore,
    )

    class _IncompatibleClient:
        async def get_thread_target(self, request: Any) -> Any:
            raise HubControlPlaneError(
                "hub_incompatible", "schema mismatch", retryable=False
            )

    store = RemoteThreadExecutionStore(_IncompatibleClient(), timeout_seconds=2.0)
    with pytest.raises(HubControlPlaneError) as exc_info:
        store.get_thread_target(thread_target_id="t1")
    assert exc_info.value.code == "hub_incompatible"
    assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# 3. Topology persistence characterization
# ---------------------------------------------------------------------------


def test_load_hub_state_returns_empty_for_missing_file(tmp_path: Path) -> None:
    from codex_autorunner.core.hub_topology import load_hub_state

    state = load_hub_state(tmp_path / "nonexistent.json", tmp_path)
    assert state.last_scan_at is None
    assert state.repos == []
    assert state.agent_workspaces == []
    assert state.pinned_parent_repo_ids == []


def test_load_hub_state_returns_empty_for_invalid_json(tmp_path: Path) -> None:
    from codex_autorunner.core.hub_topology import load_hub_state

    state_path = tmp_path / "hub_state.json"
    state_path.write_text("not valid json {{{", encoding="utf-8")
    state = load_hub_state(state_path, tmp_path)
    assert state.repos == []


def test_load_hub_state_skips_bad_entries_but_loads_good_ones(tmp_path: Path) -> None:
    from codex_autorunner.core.hub_topology import load_hub_state

    payload = {
        "repos": [
            {
                "id": "good-repo",
                "path": "good-repo",
                "kind": "base",
                "status": "idle",
            },
            {
                "id": "bad-repo",
                "path": "bad-repo",
                "status": "not_a_real_status_value",
            },
        ],
        "agent_workspaces": [],
    }
    state_path = tmp_path / "hub_state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    state = load_hub_state(state_path, tmp_path)
    repo_ids = [r.id for r in state.repos]
    assert "good-repo" in repo_ids
    assert "bad-repo" not in repo_ids


def test_load_hub_state_normalizes_pinned_repo_ids(tmp_path: Path) -> None:
    from codex_autorunner.core.hub_topology import load_hub_state

    payload = {
        "repos": [],
        "agent_workspaces": [],
        "pinned_parent_repo_ids": [
            "  alpha  ",
            "beta",
            "",
            42,
            "alpha",
        ],
    }
    state_path = tmp_path / "hub_state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    state = load_hub_state(state_path, tmp_path)
    assert state.pinned_parent_repo_ids == ["alpha", "beta"]


def test_save_hub_state_writes_atomic_json(tmp_path: Path) -> None:
    from codex_autorunner.core.hub_topology import (
        HubState,
        LockStatus,
        RepoSnapshot,
        RepoStatus,
        save_hub_state,
    )

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    state_path = hub_root / ".codex-autorunner" / "hub_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    repo = RepoSnapshot(
        id="demo",
        path=hub_root / "demo",
        display_name="demo",
        enabled=True,
        auto_run=False,
        worktree_setup_commands=None,
        kind="base",
        worktree_of=None,
        branch="main",
        exists_on_disk=True,
        is_clean=True,
        initialized=True,
        init_error=None,
        status=RepoStatus.IDLE,
        lock_status=LockStatus.UNLOCKED,
        last_run_id=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        last_exit_code=None,
        runner_pid=None,
    )
    state = HubState(last_scan_at="2026-01-01T00:00:00Z", repos=[repo])
    save_hub_state(state_path, state, hub_root)

    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert loaded["last_scan_at"] == "2026-01-01T00:00:00Z"
    assert loaded["repos"][0]["id"] == "demo"


def test_refresh_pma_threads_artifact_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.core.hub_topology import (
        HubState,
        refresh_pma_threads_artifact,
        save_hub_state,
    )

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    state_path = hub_root / ".codex-autorunner" / "hub_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = HubState(last_scan_at="2026-01-01T00:00:00Z", repos=[])

    save_hub_state(state_path, state, hub_root)
    assert state_path.exists(), "main state file must still be written"

    import codex_autorunner.core.pma_context as pma_ctx

    monkeypatch.setattr(
        pma_ctx,
        "_snapshot_pma_threads",
        lambda _root: (_ for _ in ()).throw(OSError("pma artifact write failed")),
    )
    refresh_pma_threads_artifact(hub_root)

    artifact_path = hub_root / ".codex-autorunner" / "pma_threads.json"
    assert not artifact_path.exists(), "artifact should not be written on failure"


# ---------------------------------------------------------------------------
# 4. Worktree cleanup safety gate characterization
# ---------------------------------------------------------------------------


def _setup_worktree_manifest(hub_root: Path) -> None:
    from codex_autorunner.manifest import ManifestRepo, load_manifest, save_manifest

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    (hub_root / "base").mkdir(parents=True, exist_ok=True)
    (hub_root / "worktrees" / "base--feature-test").mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(manifest_path, hub_root)
    manifest.repos = [
        ManifestRepo(id="base", path=Path("base"), kind="base"),
        ManifestRepo(
            id="base--feature-test",
            path=Path("worktrees/base--feature-test"),
            kind="worktree",
            worktree_of="base",
            branch="feature/test",
        ),
    ]
    save_manifest(manifest_path, manifest, hub_root)


def test_cleanup_worktree_rejects_when_archive_required_but_not_requested(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.config import (
        CONFIG_FILENAME,
        DEFAULT_HUB_CONFIG,
        load_hub_config,
    )
    from codex_autorunner.core.hub_worktree_manager import WorktreeManager
    from tests.conftest import write_test_config

    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    ctx = MagicMock()
    manager = WorktreeManager(
        hub_config=load_hub_config(hub_root),
        ctx=ctx,
    )
    with pytest.raises(ValueError, match="cleanup requires archiving"):
        manager._validate_cleanup_worktree(
            worktree_repo_id="wt-1",
            archive=False,
            force=False,
            force_archive=False,
            force_attestation=None,
        )


def test_cleanup_worktree_blocks_on_chat_binding_check_failure_without_force(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.config import (
        CONFIG_FILENAME,
        DEFAULT_HUB_CONFIG,
        load_hub_config,
    )
    from codex_autorunner.core.hub_worktree_manager import WorktreeManager
    from tests.conftest import write_test_config

    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["pma"]["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    _setup_worktree_manifest(hub_root)

    ctx = MagicMock()
    ctx.invalidate_cache.return_value = None
    manager = WorktreeManager(
        hub_config=load_hub_config(hub_root),
        ctx=ctx,
    )

    def _binding_check_raises(repo_id: str) -> bool:
        raise RuntimeError("db temporarily unavailable")

    manager._has_active_chat_binding = _binding_check_raises

    with pytest.raises(ValueError, match="Unable to verify active chat bindings"):
        manager._validate_cleanup_worktree(
            worktree_repo_id="base--feature-test",
            archive=False,
            force=False,
            force_archive=False,
            force_attestation=None,
        )


def test_cleanup_worktree_allows_force_to_proceed_past_chat_binding_check_failure(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.config import (
        CONFIG_FILENAME,
        DEFAULT_HUB_CONFIG,
        load_hub_config,
    )
    from codex_autorunner.core.hub_worktree_manager import WorktreeManager
    from tests.conftest import write_test_config

    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["pma"]["cleanup_require_archive"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    _setup_worktree_manifest(hub_root)

    ctx = MagicMock()
    ctx.invalidate_cache.return_value = None
    manager = WorktreeManager(
        hub_config=load_hub_config(hub_root),
        ctx=ctx,
    )

    def _binding_check_raises(repo_id: str) -> bool:
        raise RuntimeError("db temporarily unavailable")

    manager._has_active_chat_binding = _binding_check_raises

    result = manager._validate_cleanup_worktree(
        worktree_repo_id="base--feature-test",
        archive=False,
        force=True,
        force_archive=False,
        force_attestation={
            "phrase": (
                "the user has explicitly asked me to perform a dangerous action "
                "and I'm confident I'm not misunderstanding them"
            ),
            "user_request": "cleanup base--feature-test",
            "target_scope": "hub.cleanup_worktree:base--feature-test",
        },
    )
    assert result is not None


# ---------------------------------------------------------------------------
# 5. WorktreeCleanupReport characterization
# ---------------------------------------------------------------------------


def test_cleanup_report_completed_steps_filters_by_ok_status() -> None:
    from codex_autorunner.core.hub_worktree_lifecycle import WorktreeCleanupReport

    report = WorktreeCleanupReport()
    report.add_step("stop_runner", "ok")
    report.add_step("telemetry", "error", "timeout")
    report.add_step("archive", "ok")
    assert report.completed_steps == ["stop_runner", "archive"]


def test_cleanup_report_failed_step_returns_first_error() -> None:
    from codex_autorunner.core.hub_worktree_lifecycle import WorktreeCleanupReport

    report = WorktreeCleanupReport()
    report.add_step("stop_runner", "ok")
    report.add_step("telemetry", "error", "timeout")
    report.add_step("archive", "error", "disk full")
    assert report.failed_step == "telemetry"


def test_cleanup_report_failed_step_returns_none_when_all_ok() -> None:
    from codex_autorunner.core.hub_worktree_lifecycle import WorktreeCleanupReport

    report = WorktreeCleanupReport()
    report.add_step("stop_runner", "ok")
    report.add_step("archive", "ok")
    assert report.failed_step is None
