from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import codex_autorunner.core.chat_bindings as chat_bindings_module
import codex_autorunner.core.hub_projection_store as projection_store_module
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import LockStatus, RepoSnapshot, RepoStatus
from codex_autorunner.core.hub_projection_store import HubProjectionStore
from codex_autorunner.surfaces.web.routes.hub_repo_routes import (
    channels as hub_channels_module,
)
from codex_autorunner.surfaces.web.routes.hub_repo_routes import (
    repo_listing as hub_repo_listing_module,
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

        for _ in range(50):
            if calls["enrich_repo"] >= 2:
                break
            await asyncio.sleep(0.01)

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
