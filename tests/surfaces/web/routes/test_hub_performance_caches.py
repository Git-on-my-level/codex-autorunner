from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.hub import LockStatus, RepoSnapshot, RepoStatus
from codex_autorunner.surfaces.web.routes.hub_repo_routes import (
    channels as hub_channels_module,
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


def test_hub_repo_enricher_invalidates_cache_on_flow_db_change(
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

    assert calls["ticket_flow_summary"] == 2


def test_hub_repo_enricher_reuses_single_flow_store_per_repo_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "demo"
    car_root = repo_root / ".codex-autorunner"
    (car_root / "tickets").mkdir(parents=True, exist_ok=True)
    (car_root / "flows.db").touch()
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
    monkeypatch.setattr(
        "codex_autorunner.core.config.load_repo_config",
        lambda _repo_root: SimpleNamespace(durable_writes=False),
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
        SimpleNamespace(enrich_repo=enrich_repo),
    )

    payload = asyncio.run(listing_service.list_repos(sections={"repos"}))

    assert failures == []
    assert len(thread_ids) == 2
    assert [repo["repo_id"] for repo in payload["repos"]] == ["repo-1", "repo-2"]


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
    now["value"] = 111.0
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
