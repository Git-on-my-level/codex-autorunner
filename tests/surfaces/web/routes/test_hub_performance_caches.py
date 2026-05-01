from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import codex_autorunner.core.chat_bindings as chat_bindings_module
import codex_autorunner.core.hub_projection_store as projection_store_module
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import RepoSnapshot
from codex_autorunner.core.hub_projection_store import HubProjectionStore
from codex_autorunner.core.hub_topology import LockStatus, RepoStatus
from codex_autorunner.surfaces.web.routes.hub_repo_routes import (
    channels as hub_channels_module,
)
from codex_autorunner.surfaces.web.routes.hub_repo_routes import (
    repo_listing as hub_repo_listing_module,
)
from codex_autorunner.surfaces.web.routes.hub_repo_routes.channel_source_readers import (
    parse_topic_identity,
    read_discord_bindings,
    read_orchestration_bindings,
    read_telegram_bindings,
)
from codex_autorunner.surfaces.web.routes.hub_repo_routes.channels import (
    HubChannelService,
)
from codex_autorunner.surfaces.web.routes.hub_repo_routes.repo_listing import (
    HubRepoListingService,
)
from codex_autorunner.surfaces.web.routes.hub_repo_routes.services import (
    HubRepoEnricher,
)
from codex_autorunner.surfaces.web.services import hub_gather as hub_gather_service


class _MountManager:
    def add_mount_info(self, repo_dict: dict) -> dict:
        repo_dict["mounted"] = True
        return repo_dict


def _repo_snapshot(repo_root: Path, repo_id: str = "demo") -> RepoSnapshot:
    return RepoSnapshot(
        id=repo_id,
        path=repo_root,
        display_name=repo_id,
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
        last_run_id=1,
        last_run_started_at=None,
        last_run_finished_at=None,
        last_exit_code=0,
        runner_pid=None,
    )


def test_hub_repo_enricher_reuses_cached_repo_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _repo_snapshot(repo_root)
    context = SimpleNamespace(
        config=SimpleNamespace(
            root=hub_root,
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(unbound_repo_thread_counts=lambda: {"demo": 0}),
    )
    enricher = HubRepoEnricher(context, _MountManager())  # type: ignore[arg-type]
    calls = {
        "has_car_state": 0,
        "ticket_flow_summary": 0,
        "run_state": 0,
        "canonical_state": 0,
    }

    def fake_has_car_state(_path: Path) -> bool:
        calls["has_car_state"] += 1
        return True

    def fake_ticket_flow_summary(
        _path: Path, *, include_failure: bool, store=None
    ) -> dict[str, object]:
        assert include_failure is True
        calls["ticket_flow_summary"] += 1
        return {
            "status": "running",
            "done_count": 1,
            "total_count": 2,
            "run_id": "r1",
        }

    def fake_run_state(
        _repo_root: Path, _repo_id: str, *, store=None
    ) -> tuple[dict[str, object], None]:
        calls["run_state"] += 1
        return ({"state": "running", "flow_status": "running", "run_id": "r1"}, None)

    def fake_canonical_state(**_kwargs) -> dict[str, object]:
        calls["canonical_state"] += 1
        return {"status": "running"}

    monkeypatch.setattr(
        "codex_autorunner.core.archive.has_car_state", fake_has_car_state
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_summary.build_ticket_flow_summary",
        fake_ticket_flow_summary,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_context.get_latest_ticket_flow_run_state_with_record",
        fake_run_state,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection.build_canonical_state_v1",
        fake_canonical_state,
    )

    first = enricher.enrich_repo(snapshot)
    second = enricher.enrich_repo(snapshot)

    assert first["canonical_state_v1"] == {"status": "running"}
    assert second["canonical_state_v1"] == {"status": "running"}
    assert calls == {
        "has_car_state": 1,
        "ticket_flow_summary": 1,
        "run_state": 1,
        "canonical_state": 1,
    }


def test_hub_repo_enricher_reuses_durable_repo_state_across_instances(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _repo_snapshot(repo_root)
    calls = {
        "has_car_state": 0,
        "ticket_flow_summary": 0,
        "run_state": 0,
        "canonical_state": 0,
    }

    def build_context() -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(
                root=hub_root,
                pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
            ),
            projection_store=HubProjectionStore(hub_root, durable=False),
            supervisor=SimpleNamespace(unbound_repo_thread_counts=lambda: {"demo": 0}),
        )

    def fake_has_car_state(_path: Path) -> bool:
        calls["has_car_state"] += 1
        return True

    def fake_ticket_flow_summary(
        _path: Path, *, include_failure: bool, store=None
    ) -> dict[str, object]:
        assert include_failure is True
        calls["ticket_flow_summary"] += 1
        return {
            "status": "running",
            "done_count": 1,
            "total_count": 2,
            "run_id": "r1",
        }

    def fake_run_state(
        _repo_root: Path, _repo_id: str, *, store=None
    ) -> tuple[dict[str, object], None]:
        calls["run_state"] += 1
        return ({"state": "running", "flow_status": "running", "run_id": "r1"}, None)

    def fake_canonical_state(**_kwargs) -> dict[str, object]:
        calls["canonical_state"] += 1
        return {"status": "running"}

    monkeypatch.setattr(
        "codex_autorunner.core.archive.has_car_state", fake_has_car_state
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_summary.build_ticket_flow_summary",
        fake_ticket_flow_summary,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_context.get_latest_ticket_flow_run_state_with_record",
        fake_run_state,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection.build_canonical_state_v1",
        fake_canonical_state,
    )

    first = HubRepoEnricher(build_context(), _MountManager())  # type: ignore[arg-type]
    second = HubRepoEnricher(build_context(), _MountManager())  # type: ignore[arg-type]

    assert first.enrich_repo(snapshot)["canonical_state_v1"] == {"status": "running"}
    assert second.enrich_repo(snapshot)["canonical_state_v1"] == {"status": "running"}
    assert calls == {
        "has_car_state": 1,
        "ticket_flow_summary": 1,
        "run_state": 1,
        "canonical_state": 1,
    }


def test_hub_repo_enricher_expires_durable_repo_state_across_instances(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _repo_snapshot(repo_root)
    calls = {
        "has_car_state": 0,
        "ticket_flow_summary": 0,
        "run_state": 0,
        "canonical_state": 0,
    }

    def build_context() -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(
                root=hub_root,
                pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
            ),
            projection_store=HubProjectionStore(hub_root, durable=False),
            supervisor=SimpleNamespace(unbound_repo_thread_counts=lambda: {"demo": 0}),
        )

    def fake_has_car_state(_path: Path) -> bool:
        calls["has_car_state"] += 1
        return True

    def fake_ticket_flow_summary(
        _path: Path, *, include_failure: bool, store=None
    ) -> dict[str, object]:
        assert include_failure is True
        calls["ticket_flow_summary"] += 1
        return {
            "status": "running",
            "done_count": 1,
            "total_count": 2,
            "run_id": f"r{calls['ticket_flow_summary']}",
        }

    def fake_run_state(
        _repo_root: Path, _repo_id: str, *, store=None
    ) -> tuple[dict[str, object], None]:
        calls["run_state"] += 1
        return ({"state": "running", "flow_status": "running", "run_id": "r1"}, None)

    def fake_canonical_state(**_kwargs) -> dict[str, object]:
        calls["canonical_state"] += 1
        return {"status": "running"}

    monkeypatch.setattr(
        "codex_autorunner.core.archive.has_car_state", fake_has_car_state
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_summary.build_ticket_flow_summary",
        fake_ticket_flow_summary,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_context.get_latest_ticket_flow_run_state_with_record",
        fake_run_state,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection.build_canonical_state_v1",
        fake_canonical_state,
    )
    monkeypatch.setattr(
        projection_store_module,
        "now_iso",
        lambda: "1970-01-01T00:16:40+00:00",
    )
    monkeypatch.setattr(projection_store_module, "_current_utc_ts", lambda: 1000.0)
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.hub_repo_routes.services._REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS",
        1.0,
    )

    first = HubRepoEnricher(build_context(), _MountManager())  # type: ignore[arg-type]
    assert first.enrich_repo(snapshot)["ticket_flow"]["run_id"] == "r1"

    monkeypatch.setattr(projection_store_module, "_current_utc_ts", lambda: 1002.0)
    second = HubRepoEnricher(build_context(), _MountManager())  # type: ignore[arg-type]
    assert second.enrich_repo(snapshot)["ticket_flow"]["run_id"] == "r2"
    assert calls == {
        "has_car_state": 2,
        "ticket_flow_summary": 2,
        "run_state": 2,
        "canonical_state": 2,
    }


def test_hub_repo_enricher_keeps_cache_when_flow_db_mtime_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    car_root = repo_root / ".codex-autorunner"
    tickets_dir = car_root / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    flows_db = car_root / "flows.db"
    flows_db.write_text("v1", encoding="utf-8")
    snapshot = _repo_snapshot(repo_root)
    context = SimpleNamespace(
        config=SimpleNamespace(
            root=hub_root,
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(unbound_repo_thread_counts=lambda: {"demo": 0}),
    )
    enricher = HubRepoEnricher(context, _MountManager())  # type: ignore[arg-type]
    calls = {"ticket_flow_summary": 0}

    def fake_ticket_flow_summary(
        _path: Path, *, include_failure: bool, store=None
    ) -> dict[str, object]:
        assert include_failure is True
        calls["ticket_flow_summary"] += 1
        return {
            "status": "running",
            "done_count": 1,
            "total_count": 2,
            "run_id": f"r{calls['ticket_flow_summary']}",
        }

    monkeypatch.setattr(
        "codex_autorunner.core.archive.has_car_state", lambda _path: True
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_summary.build_ticket_flow_summary",
        fake_ticket_flow_summary,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_context.get_latest_ticket_flow_run_state_with_record",
        lambda _repo_root, _repo_id, store=None: (
            {"state": "running", "flow_status": "running"},
            None,
        ),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection.build_canonical_state_v1",
        lambda **_kwargs: {"status": "running"},
    )

    enricher.enrich_repo(snapshot)
    flows_db.write_text("v2", encoding="utf-8")
    enricher.enrich_repo(snapshot)

    assert calls["ticket_flow_summary"] == 1


def test_hub_repo_enricher_reuses_single_flow_store_per_repo_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    car_root = repo_root / ".codex-autorunner"
    (car_root / "tickets").mkdir(parents=True, exist_ok=True)
    with FlowStore(car_root / "flows.db") as store:
        store.create_flow_run(
            "r1",
            "ticket_flow",
            input_data={},
            state={},
            metadata={},
        )
    snapshot = _repo_snapshot(repo_root)
    context = SimpleNamespace(
        config=SimpleNamespace(
            root=hub_root,
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(unbound_repo_thread_counts=lambda: {"demo": 0}),
    )
    enricher = HubRepoEnricher(context, _MountManager())  # type: ignore[arg-type]
    store_ids: dict[str, int] = {}

    monkeypatch.setattr(
        "codex_autorunner.core.archive.has_car_state", lambda _path: True
    )

    def fake_ticket_flow_summary(
        _path: Path, *, include_failure: bool, store=None
    ) -> dict[str, object]:
        assert include_failure is True
        assert store is not None
        store_ids["summary"] = id(store)
        return {
            "status": "running",
            "done_count": 1,
            "total_count": 2,
            "run_id": "r1",
        }

    def fake_run_state(
        _repo_root: Path, _repo_id: str, *, store=None
    ) -> tuple[dict[str, object], None]:
        assert store is not None
        store_ids["run_state"] = id(store)
        return ({"state": "running", "flow_status": "running", "run_id": "r1"}, None)

    def fake_canonical_state(**kwargs) -> dict[str, object]:
        store = kwargs.get("store")
        assert store is not None
        store_ids["canonical"] = id(store)
        return {"status": "running"}

    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_summary.build_ticket_flow_summary",
        fake_ticket_flow_summary,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_context.get_latest_ticket_flow_run_state_with_record",
        fake_run_state,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection.build_canonical_state_v1",
        fake_canonical_state,
    )

    enriched = enricher.enrich_repo(snapshot)

    assert enriched["canonical_state_v1"] == {"status": "running"}
    assert store_ids["summary"] == store_ids["run_state"] == store_ids["canonical"]


def test_hub_repo_listing_service_enriches_repos_in_parallel(tmp_path: Path) -> None:
    class _AsyncMountManager:
        async def refresh_mounts(self, _snapshots) -> None:
            return None

    snapshots = [
        _repo_snapshot(tmp_path / "repo-1", repo_id="repo-1"),
        _repo_snapshot(tmp_path / "repo-2", repo_id="repo-2"),
    ]
    barrier = threading.Barrier(2, timeout=0.5)
    failures: list[Exception] = []
    thread_ids: set[int] = set()

    def enrich_repo(
        snapshot, chat_binding_counts: dict[str, int], chat_binding_counts_by_source
    ) -> dict[str, object]:
        assert chat_binding_counts == {}
        assert chat_binding_counts_by_source == {}
        thread_ids.add(threading.get_ident())
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError as exc:
            failures.append(exc)
        return {"repo_id": snapshot.id}

    context = SimpleNamespace(
        config=SimpleNamespace(
            root=tmp_path,
            raw={},
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(
            list_repos=lambda: snapshots,
            state=SimpleNamespace(last_scan_at=None, pinned_parent_repo_ids=[]),
        ),
        logger=logging.getLogger(__name__),
    )
    listing_service = HubRepoListingService(
        context,
        _AsyncMountManager(),  # type: ignore[arg-type]
        SimpleNamespace(
            enrich_repo=enrich_repo, repo_state_fingerprint=lambda *_a, **_kw: ()
        ),
    )

    payload = asyncio.run(listing_service.list_repos(sections={"repos"}))

    assert failures == []
    assert len(thread_ids) == 2
    assert [repo["repo_id"] for repo in payload["repos"]] == ["repo-1", "repo-2"]


def test_hub_repo_listing_service_reuses_unbound_thread_counts_per_listing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _AsyncMountManager:
        async def refresh_mounts(self, _snapshots) -> None:
            return None

        def add_mount_info(self, repo_dict: dict) -> dict:
            return repo_dict

    snapshots = [
        _repo_snapshot(tmp_path / "repo-1", repo_id="repo-1"),
        _repo_snapshot(tmp_path / "repo-2", repo_id="repo-2"),
    ]
    calls = {"unbound": 0}

    def fake_unbound_repo_thread_counts() -> dict[str, int]:
        calls["unbound"] += 1
        return {"repo-1": 1, "repo-2": 2}

    monkeypatch.setattr(
        "codex_autorunner.core.archive.has_car_state",
        lambda _path: True,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_summary.build_ticket_flow_summary",
        lambda _path, *, include_failure, store=None: None,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.pma_context.get_latest_ticket_flow_run_state_with_record",
        lambda _repo_root, _repo_id, *, store=None: ({}, None),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.ticket_flow_projection.build_canonical_state_v1",
        lambda **_kwargs: {},
    )

    context = SimpleNamespace(
        config=SimpleNamespace(
            root=tmp_path,
            raw={},
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(
            list_repos=lambda: snapshots,
            state=SimpleNamespace(last_scan_at=None, pinned_parent_repo_ids=[]),
            unbound_repo_thread_counts=fake_unbound_repo_thread_counts,
        ),
        logger=logging.getLogger(__name__),
    )
    enricher = HubRepoEnricher(context, _AsyncMountManager())  # type: ignore[arg-type]
    listing_service = HubRepoListingService(
        context,
        _AsyncMountManager(),  # type: ignore[arg-type]
        enricher,
    )
    listing_service._active_chat_binding_counts_by_source = lambda: {}

    payload = asyncio.run(listing_service.list_repos(sections={"repos"}))

    repos_by_id = {repo["id"]: repo for repo in payload["repos"]}
    assert repos_by_id["repo-1"]["unbound_managed_thread_count"] == 1
    assert repos_by_id["repo-2"]["unbound_managed_thread_count"] == 2
    assert calls["unbound"] == 1


def test_hub_repo_listing_service_reuses_stale_response_while_refreshing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _AsyncMountManager:
        async def refresh_mounts(self, _snapshots) -> None:
            return None

    snapshot = _repo_snapshot(tmp_path / "repo-1", repo_id="repo-1")
    now = {"value": 100.0}
    calls = {"enrich_repo": 0}

    monkeypatch.setattr(
        hub_repo_listing_module,
        "_monotonic",
        lambda: now["value"],
    )

    def enrich_repo(
        _snapshot, chat_binding_counts: dict[str, int], chat_binding_counts_by_source
    ) -> dict[str, object]:
        assert chat_binding_counts == {}
        assert chat_binding_counts_by_source == {}
        calls["enrich_repo"] += 1
        return {"repo_id": "repo-1", "call": calls["enrich_repo"]}

    context = SimpleNamespace(
        config=SimpleNamespace(
            root=tmp_path,
            raw={},
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(
            list_repos=lambda: [snapshot],
            state=SimpleNamespace(
                last_scan_at="2026-04-05T00:00:00Z",
                pinned_parent_repo_ids=[],
            ),
        ),
        logger=logging.getLogger(__name__),
    )
    listing_service = HubRepoListingService(
        context,
        _AsyncMountManager(),  # type: ignore[arg-type]
        SimpleNamespace(
            enrich_repo=enrich_repo, repo_state_fingerprint=lambda *_a, **_kw: ()
        ),
    )

    async def run_scenario() -> None:
        first = await listing_service.list_repos(sections={"repos"})
        second = await listing_service.list_repos(sections={"repos"})

        assert first["repos"][0]["call"] == 1
        assert second["repos"][0]["call"] == 1
        assert calls["enrich_repo"] == 1

        now["value"] = 121.0
        stale = await listing_service.list_repos(sections={"repos"})
        assert stale["repos"][0]["call"] == 1

        refresh_task = listing_service._response_refresh_tasks.get(("repos",))
        assert refresh_task is not None
        await refresh_task
        assert calls["enrich_repo"] == 2
        refreshed = await listing_service.list_repos(sections={"repos"})
        assert refreshed["repos"][0]["call"] == 2

    asyncio.run(run_scenario())


def test_hub_repo_listing_service_invalidates_cache_when_manifest_changes(
    tmp_path: Path,
) -> None:
    class _AsyncMountManager:
        async def refresh_mounts(self, _snapshots) -> None:
            return None

    snapshot = _repo_snapshot(tmp_path / "repo-1", repo_id="repo-1")
    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "version: 3\nrepos:\n  - id: repo-1\n    path: repo-1\n",
        encoding="utf-8",
    )
    calls = {"enrich_repo": 0}

    def enrich_repo(
        _snapshot, chat_binding_counts: dict[str, int], chat_binding_counts_by_source
    ) -> dict[str, object]:
        assert chat_binding_counts == {}
        assert chat_binding_counts_by_source == {}
        calls["enrich_repo"] += 1
        return {"repo_id": "repo-1", "call": calls["enrich_repo"]}

    context = SimpleNamespace(
        config=SimpleNamespace(
            root=tmp_path,
            raw={},
            manifest_path=manifest_path,
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        supervisor=SimpleNamespace(
            list_repos=lambda: [snapshot],
            state=SimpleNamespace(
                last_scan_at="2026-04-05T00:00:00Z",
                pinned_parent_repo_ids=[],
            ),
        ),
        logger=logging.getLogger(__name__),
    )
    listing_service = HubRepoListingService(
        context,
        _AsyncMountManager(),  # type: ignore[arg-type]
        SimpleNamespace(
            enrich_repo=enrich_repo, repo_state_fingerprint=lambda *_a, **_kw: ()
        ),
    )

    first = asyncio.run(listing_service.list_repos(sections={"repos"}))
    manifest_path.write_text(
        "repos:\n  - id: repo-1\n  - id: repo-2\n", encoding="utf-8"
    )
    second = asyncio.run(listing_service.list_repos(sections={"repos"}))

    assert first["repos"][0]["call"] == 1
    assert second["repos"][0]["call"] == 2
    assert calls["enrich_repo"] == 2


def test_hub_repo_listing_service_reuses_durable_projection_across_instances(
    tmp_path: Path,
) -> None:
    class _AsyncMountManager:
        async def refresh_mounts(self, _snapshots) -> None:
            return None

    snapshot = _repo_snapshot(tmp_path / "repo-1", repo_id="repo-1")
    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("repos:\n  - id: repo-1\n", encoding="utf-8")
    calls = {"enrich_repo": 0}

    def enrich_repo(
        _snapshot, chat_binding_counts: dict[str, int], chat_binding_counts_by_source
    ) -> dict[str, object]:
        assert chat_binding_counts == {}
        assert chat_binding_counts_by_source == {}
        calls["enrich_repo"] += 1
        return {"repo_id": "repo-1", "call": calls["enrich_repo"]}

    def build_context() -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(
                root=tmp_path,
                raw={},
                manifest_path=manifest_path,
                pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
            ),
            projection_store=HubProjectionStore(tmp_path, durable=False),
            supervisor=SimpleNamespace(
                list_repos=lambda: [snapshot],
                state=SimpleNamespace(
                    last_scan_at="2026-04-05T00:00:00Z",
                    pinned_parent_repo_ids=[],
                    repos=[snapshot],
                    agent_workspaces=[],
                ),
            ),
            logger=logging.getLogger(__name__),
        )

    first_service = HubRepoListingService(
        build_context(),
        _AsyncMountManager(),  # type: ignore[arg-type]
        SimpleNamespace(
            enrich_repo=enrich_repo,
            repo_state_fingerprint=lambda *_args, **_kwargs: ("repo-1",),
        ),
    )
    second_service = HubRepoListingService(
        build_context(),
        _AsyncMountManager(),  # type: ignore[arg-type]
        SimpleNamespace(
            enrich_repo=enrich_repo,
            repo_state_fingerprint=lambda *_args, **_kwargs: ("repo-1",),
        ),
    )

    first = asyncio.run(first_service.list_repos(sections={"repos"}))
    second = asyncio.run(second_service.list_repos(sections={"repos"}))

    assert first["repos"][0]["call"] == 1
    assert second["repos"][0]["call"] == 1
    assert calls["enrich_repo"] == 1


def test_active_chat_binding_counts_by_source_reuses_durable_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    calls = {"pma": 0, "orchestration": 0, "discord": 0, "telegram": 0}

    monkeypatch.setattr(
        chat_bindings_module,
        "_repo_id_by_workspace_path",
        lambda _hub_root, _raw_config: {},
    )

    def fake_pma(_hub_root: Path, _repo_id_by_workspace) -> dict[str, int]:
        calls["pma"] += 1
        return {"demo": 1}

    def fake_orchestration(*, hub_root: Path, repo_id_by_workspace):
        calls["orchestration"] += 1
        return {"demo": {"discord": 2}}

    def fake_discord(*, db_path: Path, repo_id_by_workspace):
        calls["discord"] += 1
        return {"demo": 5}

    def fake_telegram(*, db_path: Path, repo_id_by_workspace):
        calls["telegram"] += 1
        return {"demo": 3}

    monkeypatch.setattr(chat_bindings_module, "_active_pma_thread_counts", fake_pma)
    monkeypatch.setattr(
        chat_bindings_module,
        "_orchestration_binding_counts_by_source",
        fake_orchestration,
    )
    monkeypatch.setattr(chat_bindings_module, "_read_discord_repo_counts", fake_discord)
    monkeypatch.setattr(
        chat_bindings_module,
        "_read_current_telegram_repo_counts",
        fake_telegram,
    )

    first = chat_bindings_module.active_chat_binding_counts_by_source(
        hub_root=hub_root,
        raw_config={},
    )
    second = chat_bindings_module.active_chat_binding_counts_by_source(
        hub_root=hub_root,
        raw_config={},
    )

    assert first == {"demo": {"pma": 1, "discord": 2, "telegram": 3}}
    assert second == first
    assert calls == {"pma": 1, "orchestration": 1, "discord": 1, "telegram": 1}


def test_hub_channel_service_reuses_ttl_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    context = SimpleNamespace(
        config=SimpleNamespace(root=hub_root, raw={}),
        supervisor=SimpleNamespace(list_repos=lambda: []),
        logger=logging.getLogger(__name__),
    )
    service = HubChannelService(context)  # type: ignore[arg-type]
    calls = {"build_rows": 0}
    now = {"value": 100.0}

    monkeypatch.setattr(
        hub_channels_module.time,
        "monotonic",
        lambda: now["value"],
    )

    async def fake_build_channel_rows() -> list[dict[str, object]]:
        calls["build_rows"] += 1
        return [
            {
                "key": "discord:chan-123:guild-1",
                "display": "CAR HQ / #ops",
                "seen_at": "2026-04-01T00:00:00Z",
                "meta": {},
                "entry": {},
                "source": "discord",
                "provenance": {"source": "discord"},
            }
        ]

    monkeypatch.setattr(service, "_build_channel_rows", fake_build_channel_rows)

    first = asyncio.run(service.list_chat_channels(limit=100))
    second = asyncio.run(service.list_chat_channels(query="ops", limit=10))
    now["value"] = 161.0
    third = asyncio.run(service.list_chat_channels(limit=100))

    assert [entry["key"] for entry in first["entries"]] == ["discord:chan-123:guild-1"]
    assert [entry["key"] for entry in second["entries"]] == ["discord:chan-123:guild-1"]
    assert [entry["key"] for entry in third["entries"]] == ["discord:chan-123:guild-1"]
    assert calls["build_rows"] == 2


def test_gather_hub_message_snapshot_reuses_short_ttl_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    calls = {"list_repos": 0}

    def list_repos() -> list[object]:
        calls["list_repos"] += 1
        return []

    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=list_repos,
            state=SimpleNamespace(last_scan_at="2026-04-05T00:00:00Z"),
        ),
        config=SimpleNamespace(root=hub_root),
    )

    monkeypatch.setattr(
        hub_gather_service, "_gather_inbox", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_hub_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_repo_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "load_hub_inbox_dismissals", lambda _root: {}
    )

    first = hub_gather_service.gather_hub_message_snapshot(context, sections={"inbox"})
    second = hub_gather_service.gather_hub_message_snapshot(context, sections={"inbox"})

    assert first["items"] == []
    assert second["items"] == []
    assert calls["list_repos"] == 1


def test_gather_hub_message_snapshot_reuses_repo_hint_cache_across_snapshot_misses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    snapshot = _repo_snapshot(repo_root)
    state = SimpleNamespace(last_scan_at="2026-04-05T00:00:00Z")
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: [snapshot], state=state),
        config=SimpleNamespace(root=hub_root),
    )
    calls = {"repo_hints": 0}

    def fake_repo_hints(**_kwargs) -> list[dict[str, object]]:
        calls["repo_hints"] += 1
        return []

    monkeypatch.setattr(
        hub_gather_service, "_gather_inbox", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_hub_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_repo_capability_hints", fake_repo_hints
    )
    monkeypatch.setattr(
        hub_gather_service, "load_hub_inbox_dismissals", lambda _root: {}
    )

    first = hub_gather_service.gather_hub_message_snapshot(context, sections={"inbox"})
    state.last_scan_at = "2026-04-05T00:00:01Z"
    second = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert first["items"] == []
    assert second["items"] == []
    assert calls["repo_hints"] == 1


def test_gather_hub_message_snapshot_refreshes_repo_hint_cache_when_repo_inputs_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    snapshot = _repo_snapshot(repo_root)
    state = SimpleNamespace(last_scan_at="2026-04-05T00:00:00Z")
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: [snapshot], state=state),
        config=SimpleNamespace(root=hub_root),
    )
    calls = {"repo_hints": 0}

    def fake_repo_hints(**_kwargs) -> list[dict[str, object]]:
        calls["repo_hints"] += 1
        return []

    monkeypatch.setattr(
        hub_gather_service, "_gather_inbox", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_hub_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_repo_capability_hints", fake_repo_hints
    )
    monkeypatch.setattr(
        hub_gather_service, "load_hub_inbox_dismissals", lambda _root: {}
    )

    first = hub_gather_service.gather_hub_message_snapshot(context, sections={"inbox"})
    repo_override = repo_root / ".codex-autorunner" / "repo.override.yml"
    repo_override.parent.mkdir(parents=True, exist_ok=True)
    repo_override.write_text("voice:\n  enabled: false\n", encoding="utf-8")
    state.last_scan_at = "2026-04-05T00:00:01Z"
    second = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert first["items"] == []
    assert second["items"] == []
    assert calls["repo_hints"] == 2


def test_gather_hub_message_snapshot_reuses_durable_projection_across_contexts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    calls = {"list_repos": 0}

    def list_repos() -> list[object]:
        calls["list_repos"] += 1
        return []

    def build_context() -> SimpleNamespace:
        return SimpleNamespace(
            supervisor=SimpleNamespace(
                list_repos=list_repos,
                state=SimpleNamespace(last_scan_at="2026-04-05T00:00:00Z"),
            ),
            config=SimpleNamespace(root=hub_root),
            projection_store=HubProjectionStore(hub_root, durable=False),
        )

    monkeypatch.setattr(
        hub_gather_service, "_gather_inbox", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_hub_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_repo_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "load_hub_inbox_dismissals", lambda _root: {}
    )

    first_ctx = build_context()
    hub_gather_service.gather_hub_message_snapshot(first_ctx, sections={"inbox"})
    hub_gather_service._hub_snapshot_cache.clear()

    second_ctx = build_context()
    result = hub_gather_service.gather_hub_message_snapshot(
        second_ctx, sections={"inbox"}
    )

    assert result["items"] == []
    assert calls["list_repos"] == 1


def test_repo_capability_hint_durable_projection_reuses_across_contexts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    snapshot = _repo_snapshot(repo_root)
    calls = {"repo_hints": 0}

    def fake_repo_hints(**_kwargs) -> list[dict[str, object]]:
        calls["repo_hints"] += 1
        return []

    monkeypatch.setattr(
        hub_gather_service, "_gather_inbox", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_hub_capability_hints", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        hub_gather_service, "build_repo_capability_hints", fake_repo_hints
    )
    monkeypatch.setattr(
        hub_gather_service, "load_hub_inbox_dismissals", lambda _root: {}
    )

    def build_context() -> SimpleNamespace:
        return SimpleNamespace(
            supervisor=SimpleNamespace(
                list_repos=lambda: [snapshot],
                state=SimpleNamespace(last_scan_at="2026-04-05T00:00:00Z"),
            ),
            config=SimpleNamespace(root=hub_root),
            projection_store=HubProjectionStore(hub_root, durable=False),
        )

    first_ctx = build_context()
    hub_gather_service.gather_hub_message_snapshot(first_ctx, sections={"inbox"})
    hub_gather_service._repo_capability_hint_cache.clear()

    second_ctx = build_context()
    hub_gather_service.gather_hub_message_snapshot(second_ctx, sections={"inbox"})

    assert calls["repo_hints"] == 1


def test_hub_projection_store_fingerprint_mismatch_returns_none(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    store.set_cache("key-1", {"version": 1}, {"data": "original"}, namespace="test_ns")
    hit = store.get_cache("key-1", {"version": 1}, namespace="test_ns")
    assert hit == {"data": "original"}

    miss = store.get_cache("key-1", {"version": 2}, namespace="test_ns")
    assert miss is None


def test_hub_projection_store_ttl_expiry_returns_none(
    tmp_path: Path, monkeypatch
) -> None:
    import codex_autorunner.core.hub_projection_store as store_module

    store = HubProjectionStore(tmp_path, durable=False)

    now_ts = {"value": 1000.0}
    monkeypatch.setattr(store_module, "_current_utc_ts", lambda: now_ts["value"])
    monkeypatch.setattr(store_module, "now_iso", lambda: "1970-01-01T00:16:40Z")
    store.set_cache("key-ttl", {"v": 1}, {"data": "fresh"}, namespace="test_ttl")

    hit = store.get_cache(
        "key-ttl", {"v": 1}, namespace="test_ttl", max_age_seconds=60.0
    )
    assert hit == {"data": "fresh"}

    now_ts["value"] = 1100.0
    expired = store.get_cache(
        "key-ttl", {"v": 1}, namespace="test_ttl", max_age_seconds=60.0
    )
    assert expired is None


def test_hub_projection_store_invalidate_clears_entry(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    store.set_cache("key-inv", {"v": 1}, {"data": "x"}, namespace="test_inv")

    hit = store.get_cache("key-inv", {"v": 1}, namespace="test_inv")
    assert hit == {"data": "x"}

    store.invalidate_cache("key-inv", namespace="test_inv")
    miss = store.get_cache("key-inv", {"v": 1}, namespace="test_inv")
    assert miss is None


def test_hub_projection_store_delete_namespace_clears_all_keys(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    store.set_cache("a", {"v": 1}, "data-a", namespace="test_ns_bulk")
    store.set_cache("b", {"v": 1}, "data-b", namespace="test_ns_bulk")

    assert store.get_cache("a", {"v": 1}, namespace="test_ns_bulk") == "data-a"
    assert store.get_cache("b", {"v": 1}, namespace="test_ns_bulk") == "data-b"

    store.delete(namespace="test_ns_bulk")

    assert store.get_cache("a", {"v": 1}, namespace="test_ns_bulk") is None
    assert store.get_cache("b", {"v": 1}, namespace="test_ns_bulk") is None


def test_hub_projection_store_get_put_aliases(tmp_path: Path) -> None:
    store = HubProjectionStore(tmp_path, durable=False)
    store.put(namespace="alias_ns", key="k", fingerprint={"f": 1}, payload={"v": 42})
    result = store.get(namespace="alias_ns", key="k", fingerprint={"f": 1})
    assert result == {"v": 42}


def test_hub_projection_store_survives_corrupt_payload(tmp_path: Path) -> None:
    import sqlite3

    store = HubProjectionStore(tmp_path, durable=False)
    db_path = store.path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS projection_cache "
            "(namespace TEXT, cache_key TEXT, fingerprint TEXT, payload TEXT, "
            "updated_at TEXT, PRIMARY KEY(namespace, cache_key))"
        )
        conn.execute(
            "INSERT INTO projection_cache VALUES (?, ?, ?, ?, ?)",
            (
                "corrupt_ns",
                "corrupt_key",
                '"fp"',
                "not-valid-json",
                "2026-01-01T00:00:00Z",
            ),
        )

    result = store.get_cache("corrupt_key", "fp", namespace="corrupt_ns")
    assert result is None


def test_path_stat_fingerprint_on_nonexistent_path() -> None:
    from codex_autorunner.core.hub_projection_store import path_stat_fingerprint

    exists, mtime_ns, size = path_stat_fingerprint(Path("/nonexistent/path/file.txt"))
    assert exists is False
    assert mtime_ns is None
    assert size is None


def test_path_stat_fingerprint_on_real_file(tmp_path: Path) -> None:
    from codex_autorunner.core.hub_projection_store import path_stat_fingerprint

    f = tmp_path / "test.txt"
    f.write_text("hello", encoding="utf-8")
    exists, mtime_ns, size = path_stat_fingerprint(f)
    assert exists is True
    assert mtime_ns is not None
    assert size == 5


def test_hub_projection_store_namespace_constants_are_stable() -> None:
    from codex_autorunner.core.hub_projection_store import (
        CHAT_BINDING_PROJECTION_KEY,
        CHAT_BINDING_PROJECTION_NAMESPACE,
        HUB_LISTING_PROJECTION_NAMESPACE,
        HUB_SNAPSHOT_PROJECTION_NAMESPACE,
        REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE,
        REPO_RUNTIME_PROJECTION_NAMESPACE,
    )

    assert REPO_RUNTIME_PROJECTION_NAMESPACE == "repo_runtime_v1"
    assert HUB_LISTING_PROJECTION_NAMESPACE == "hub_listing_v1"
    assert CHAT_BINDING_PROJECTION_NAMESPACE == "chat_binding_counts_v1"
    assert CHAT_BINDING_PROJECTION_KEY == "active_by_source"
    assert HUB_SNAPSHOT_PROJECTION_NAMESPACE == "hub_snapshot_v1"
    assert REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE == "repo_capability_hints_v1"


def _make_context(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            root=tmp_path,
            raw={},
            pma=SimpleNamespace(freshness_stale_threshold_seconds=None),
        ),
        logger=logging.getLogger(__name__),
    )


def _create_discord_db(db_path: Path, rows: list[dict[str, Any]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS channel_bindings ("
                "channel_id TEXT, guild_id TEXT, workspace_path TEXT, "
                "repo_id TEXT, resource_kind TEXT, resource_id TEXT, "
                "pma_enabled INTEGER, agent TEXT, agent_profile TEXT, "
                "updated_at TEXT)"
            )
            for row in rows:
                cols = ", ".join(row.keys())
                placeholders = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO channel_bindings ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
    finally:
        conn.close()


def _create_telegram_db(
    db_path: Path,
    rows: list[dict[str, Any]],
    *,
    include_scope_table: bool = False,
    scope_rows: Optional[list[dict[str, Any]]] = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            all_keys: set[str] = set()
            for row in rows:
                all_keys.update(row.keys())
            col_defs = ", ".join(f"{col} TEXT" for col in sorted(all_keys))
            conn.execute(f"CREATE TABLE IF NOT EXISTS telegram_topics ({col_defs})")
            for row in rows:
                cols = ", ".join(row.keys())
                placeholders = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO telegram_topics ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
            if include_scope_table:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS telegram_topic_scopes "
                    "(chat_id INTEGER, thread_id INTEGER, scope TEXT)"
                )
                for sr in scope_rows or []:
                    conn.execute(
                        "INSERT INTO telegram_topic_scopes (chat_id, thread_id, scope) VALUES (?, ?, ?)",
                        (sr.get("chat_id"), sr.get("thread_id"), sr.get("scope")),
                    )
    finally:
        conn.close()


def _create_orchestration_db(
    db_path: Path,
    rows: list[dict[str, Any]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS orch_bindings ("
                "surface_key TEXT, target_id TEXT, agent_id TEXT, "
                "repo_id TEXT, resource_kind TEXT, resource_id TEXT, "
                "mode TEXT, updated_at TEXT, disabled_at TEXT, "
                "target_kind TEXT, surface_kind TEXT)"
            )
            for row in rows:
                cols = ", ".join(row.keys())
                placeholders = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO orch_bindings ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
    finally:
        conn.close()


class TestParseTopicIdentity:
    def test_valid_colon_separated_key(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(None, None, "123456:root")
        assert chat_id == 123456
        assert thread_id is None
        assert scope is None

    def test_valid_with_thread_and_scope(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(
            None, None, "123456:789:my-scope"
        )
        assert chat_id == 123456
        assert thread_id == 789
        assert scope == "my-scope"

    def test_valid_direct_chat_id(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(123456, None, "ignored")
        assert chat_id == 123456
        assert thread_id is None
        assert scope is None

    def test_valid_direct_chat_and_thread(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(123456, 789, "ignored")
        assert chat_id == 123456
        assert thread_id == 789
        assert scope is None

    def test_malformed_single_token_returns_none(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(None, None, "abc")
        assert chat_id is None
        assert thread_id is None
        assert scope is None

    def test_malformed_non_numeric_chat_id_returns_none(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(None, None, "abc:root")
        assert chat_id is None

    def test_malformed_empty_string_returns_none(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(None, None, "")
        assert chat_id is None

    def test_malformed_non_string_topic_returns_none(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(None, None, 123)
        assert chat_id is None

    def test_malformed_bool_chat_id_falls_through_to_topic(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(True, None, "123:root")
        assert chat_id == 123
        assert thread_id is None
        assert scope is None

    def test_malformed_bool_chat_id_without_topic_returns_none(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(True, None, "bad")
        assert chat_id is None

    def test_malformed_bool_thread_id_rejected(self) -> None:
        chat_id, thread_id, scope = parse_topic_identity(123, True, "123:root")
        assert chat_id == 123
        assert thread_id is None


class TestReadDiscordBindings:
    def test_valid_row_produces_binding(self, tmp_path: Path) -> None:
        db_path = tmp_path / "discord.sqlite3"
        _create_discord_db(
            db_path,
            [
                {"channel_id": "chan-1", "repo_id": "demo"},
            ],
        )
        bindings = read_discord_bindings(db_path, {}, context=_make_context(tmp_path))
        assert "discord:chan-1" in bindings
        assert bindings["discord:chan-1"]["platform"] == "discord"
        assert bindings["discord:chan-1"]["chat_id"] == "chan-1"

    def test_nonexistent_db_returns_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "missing.sqlite3"
        bindings = read_discord_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_malformed_non_string_channel_id_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "discord.sqlite3"
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE channel_bindings (channel_id INTEGER, repo_id TEXT)"
                )
                conn.execute("INSERT INTO channel_bindings VALUES (12345, 'demo')")
        finally:
            conn.close()
        bindings = read_discord_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_malformed_empty_channel_id_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "discord.sqlite3"
        _create_discord_db(
            db_path,
            [
                {"channel_id": "   ", "repo_id": "demo"},
            ],
        )
        bindings = read_discord_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_malformed_null_channel_id_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "discord.sqlite3"
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE channel_bindings (channel_id TEXT, repo_id TEXT)"
                )
                conn.execute("INSERT INTO channel_bindings VALUES (NULL, 'demo')")
        finally:
            conn.close()
        bindings = read_discord_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_mixed_valid_and_malformed_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "discord.sqlite3"
        _create_discord_db(
            db_path,
            [
                {"channel_id": "good-channel", "repo_id": "demo"},
                {"channel_id": None, "repo_id": "bad"},
            ],
        )
        bindings = read_discord_bindings(db_path, {}, context=_make_context(tmp_path))
        assert "discord:good-channel" in bindings
        assert len(bindings) == 1


class TestReadTelegramBindings:
    def test_valid_row_produces_binding(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {
                    "topic_key": "123456:root",
                    "chat_id": "123456",
                    "thread_id": None,
                },
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert "telegram:123456" in bindings
        assert bindings["telegram:123456"]["platform"] == "telegram"

    def test_malformed_topic_key_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {"topic_key": "bad-key", "chat_id": None, "thread_id": None},
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_malformed_empty_topic_key_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {"topic_key": "", "chat_id": None, "thread_id": None},
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_malformed_invalid_json_payload_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {
                    "topic_key": "123456:root",
                    "chat_id": "123456",
                    "thread_id": None,
                    "payload_json": "not-valid-json{{{",
                },
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_malformed_non_dict_json_payload_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {
                    "topic_key": "123456:root",
                    "chat_id": "123456",
                    "thread_id": None,
                    "payload_json": "[1, 2, 3]",
                },
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert bindings == {}

    def test_valid_payload_json_produces_binding(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        payload = json.dumps({"workspace_path": "/tmp/repo", "repo_id": "demo"})
        _create_telegram_db(
            db_path,
            [
                {
                    "topic_key": "123456:root",
                    "chat_id": "123456",
                    "thread_id": None,
                    "payload_json": payload,
                },
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert "telegram:123456" in bindings

    def test_missing_payload_json_column_still_works(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {
                    "topic_key": "123456:root",
                    "chat_id": "123456",
                    "thread_id": None,
                },
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert "telegram:123456" in bindings

    def test_mixed_valid_and_malformed_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telegram.sqlite3"
        _create_telegram_db(
            db_path,
            [
                {
                    "topic_key": "123456:root",
                    "chat_id": "123456",
                    "thread_id": None,
                },
                {
                    "topic_key": "bad-key",
                    "chat_id": None,
                    "thread_id": None,
                },
            ],
        )
        bindings = read_telegram_bindings(db_path, {}, context=_make_context(tmp_path))
        assert "telegram:123456" in bindings
        assert len(bindings) == 1


class TestReadOrchestrationBindings:
    def test_valid_row_produces_binding(self, tmp_path: Path) -> None:
        orch_dir = tmp_path / ".codex-autorunner"
        orch_dir.mkdir(parents=True, exist_ok=True)
        db_path = orch_dir / "orchestration.sqlite3"
        _create_orchestration_db(
            db_path,
            [
                {
                    "surface_key": "discord:chan-1",
                    "target_id": "thread-abc",
                    "agent_id": "codex",
                    "repo_id": "demo",
                    "resource_kind": "repo",
                    "resource_id": "demo",
                    "mode": None,
                    "updated_at": None,
                    "disabled_at": None,
                    "target_kind": "thread",
                    "surface_kind": "discord",
                },
            ],
        )
        bindings = read_orchestration_bindings(
            tmp_path, surface_kind="discord", context=_make_context(tmp_path)
        )
        assert "discord:chan-1" in bindings

    def test_malformed_empty_surface_key_skipped(self, tmp_path: Path) -> None:
        orch_dir = tmp_path / ".codex-autorunner"
        orch_dir.mkdir(parents=True, exist_ok=True)
        db_path = orch_dir / "orchestration.sqlite3"
        _create_orchestration_db(
            db_path,
            [
                {
                    "surface_key": "",
                    "target_id": "thread-abc",
                    "agent_id": "codex",
                    "repo_id": None,
                    "resource_kind": None,
                    "resource_id": None,
                    "mode": None,
                    "updated_at": None,
                    "disabled_at": None,
                    "target_kind": "thread",
                    "surface_kind": "discord",
                },
            ],
        )
        bindings = read_orchestration_bindings(
            tmp_path, surface_kind="discord", context=_make_context(tmp_path)
        )
        assert bindings == {}

    def test_malformed_empty_target_id_skipped(self, tmp_path: Path) -> None:
        orch_dir = tmp_path / ".codex-autorunner"
        orch_dir.mkdir(parents=True, exist_ok=True)
        db_path = orch_dir / "orchestration.sqlite3"
        _create_orchestration_db(
            db_path,
            [
                {
                    "surface_key": "discord:chan-1",
                    "target_id": "   ",
                    "agent_id": "codex",
                    "repo_id": None,
                    "resource_kind": None,
                    "resource_id": None,
                    "mode": None,
                    "updated_at": None,
                    "disabled_at": None,
                    "target_kind": "thread",
                    "surface_kind": "discord",
                },
            ],
        )
        bindings = read_orchestration_bindings(
            tmp_path, surface_kind="discord", context=_make_context(tmp_path)
        )
        assert bindings == {}

    def test_malformed_null_surface_key_skipped(self, tmp_path: Path) -> None:
        orch_dir = tmp_path / ".codex-autorunner"
        orch_dir.mkdir(parents=True, exist_ok=True)
        db_path = orch_dir / "orchestration.sqlite3"
        _create_orchestration_db(
            db_path,
            [
                {
                    "surface_key": None,
                    "target_id": "thread-abc",
                    "agent_id": "codex",
                    "repo_id": None,
                    "resource_kind": None,
                    "resource_id": None,
                    "mode": None,
                    "updated_at": None,
                    "disabled_at": None,
                    "target_kind": "thread",
                    "surface_kind": "discord",
                },
            ],
        )
        bindings = read_orchestration_bindings(
            tmp_path, surface_kind="discord", context=_make_context(tmp_path)
        )
        assert bindings == {}

    def test_nonexistent_db_returns_empty(self, tmp_path: Path) -> None:
        bindings = read_orchestration_bindings(
            tmp_path / "missing",
            surface_kind="discord",
            context=_make_context(tmp_path),
        )
        assert bindings == {}
