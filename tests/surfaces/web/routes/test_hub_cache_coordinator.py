from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codex_autorunner.surfaces.web.routes.hub_repo_routes.cache_coordinator import (
    HubCacheCoordinator,
)


def _make_context(*, supervisor_list_repos_side_effect=None) -> SimpleNamespace:
    supervisor = SimpleNamespace()
    if supervisor_list_repos_side_effect is not None:
        supervisor.list_repos = MagicMock(side_effect=supervisor_list_repos_side_effect)
    else:
        supervisor.list_repos = MagicMock(return_value=[])
    return SimpleNamespace(
        supervisor=supervisor,
        logger=MagicMock(),
        config=SimpleNamespace(root="/tmp/test-hub"),
    )


def _make_enricher() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_invalidate_caches_refreshes_topology() -> None:
    context = _make_context()
    enricher = _make_enricher()
    coordinator = HubCacheCoordinator(context, enricher)

    with patch(
        "codex_autorunner.surfaces.web.services.hub_gather"
        ".invalidate_hub_message_snapshot_cache",
    ):
        await coordinator.invalidate_caches()

    context.supervisor.list_repos.assert_called_once_with(use_cache=False)


@pytest.mark.asyncio
async def test_invalidate_caches_clears_enricher_runtime_caches() -> None:
    context = _make_context()
    enricher = _make_enricher()
    coordinator = HubCacheCoordinator(context, enricher)

    with patch(
        "codex_autorunner.surfaces.web.services.hub_gather"
        ".invalidate_hub_message_snapshot_cache",
    ):
        await coordinator.invalidate_caches()

    enricher.invalidate_runtime_caches.assert_called_once()


@pytest.mark.asyncio
async def test_invalidate_caches_clears_hub_snapshot_caches() -> None:
    context = _make_context()
    enricher = _make_enricher()
    coordinator = HubCacheCoordinator(context, enricher)

    with patch(
        "codex_autorunner.surfaces.web.services.hub_gather"
        ".invalidate_hub_message_snapshot_cache",
    ) as mock_snapshot_invalidate:
        await coordinator.invalidate_caches()

    mock_snapshot_invalidate.assert_called_once_with(context)


@pytest.mark.asyncio
async def test_invalidate_caches_survives_topology_refresh_failure() -> None:
    context = _make_context(
        supervisor_list_repos_side_effect=RuntimeError("scan failed")
    )
    enricher = _make_enricher()
    coordinator = HubCacheCoordinator(context, enricher)

    with patch(
        "codex_autorunner.surfaces.web.services.hub_gather"
        ".invalidate_hub_message_snapshot_cache",
    ):
        await coordinator.invalidate_caches()

    context.supervisor.list_repos.assert_called_once_with(use_cache=False)
    enricher.invalidate_runtime_caches.assert_called_once()


@pytest.mark.asyncio
async def test_invalidate_caches_ordering() -> None:
    context = _make_context()
    enricher = _make_enricher()
    coordinator = HubCacheCoordinator(context, enricher)
    call_order: list[str] = []

    context.supervisor.list_repos = MagicMock(
        side_effect=lambda **kw: call_order.append("topology") or []
    )

    def _enricher_invalidate():
        call_order.append("enricher")

    enricher.invalidate_runtime_caches = _enricher_invalidate

    with patch(
        "codex_autorunner.surfaces.web.services.hub_gather"
        ".invalidate_hub_message_snapshot_cache",
        side_effect=lambda ctx: call_order.append("snapshot"),
    ):
        await coordinator.invalidate_caches()

    assert call_order == ["topology", "enricher", "snapshot"]
