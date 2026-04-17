from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from codex_autorunner.surfaces.web.services import hub_gather as hub_gather_service


def _make_context(tmp_path: Path, **overrides):
    defaults = dict(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=SimpleNamespace(root=tmp_path),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestInvalidateHubMessageSnapshotCache:
    def test_clear_all_with_none_context(self, tmp_path) -> None:
        context = _make_context(tmp_path)
        hub_gather_service._hub_snapshot_cache[(id(context), "scope-1")] = (
            hub_gather_service._HubSnapshotCacheEntry(
                fingerprint=("fp",),
                expires_at=9999999.0,
                snapshot={"test": True},
            )
        )
        hub_gather_service.invalidate_hub_message_snapshot_cache(context=None)
        assert len(hub_gather_service._hub_snapshot_cache) == 0

    def test_clear_all_with_none_context_and_repo_hints(self, tmp_path) -> None:
        hub_gather_service._repo_capability_hint_cache[
            ("root-a", "repo-a", "1", "A")
        ] = hub_gather_service._RepoCapabilityHintCacheEntry(
            fingerprint=("fp",),
            expires_at=9999999.0,
            items=[{"hint_id": "h1"}],
        )
        hub_gather_service.invalidate_hub_message_snapshot_cache(
            context=None,
            include_repo_capability_hints=True,
        )
        assert len(hub_gather_service._repo_capability_hint_cache) == 0

    def test_clears_only_matching_context(self, tmp_path) -> None:
        context_a = _make_context(tmp_path)
        context_b = _make_context(tmp_path)
        entry = hub_gather_service._HubSnapshotCacheEntry(
            fingerprint=("fp",),
            expires_at=9999999.0,
            snapshot={"test": True},
        )
        hub_gather_service._hub_snapshot_cache[(id(context_a), "s1")] = entry
        hub_gather_service._hub_snapshot_cache[(id(context_b), "s2")] = entry
        hub_gather_service.invalidate_hub_message_snapshot_cache(context=context_a)
        assert (id(context_a), "s1") not in hub_gather_service._hub_snapshot_cache
        assert (id(context_b), "s2") in hub_gather_service._hub_snapshot_cache
        hub_gather_service._hub_snapshot_cache.clear()

    def test_clears_matching_repo_capability_hints_and_projection_namespace(
        self, tmp_path
    ) -> None:
        projection_store = MagicMock()
        context_a = _make_context(
            tmp_path / "root-a", projection_store=projection_store
        )
        context_b = _make_context(tmp_path / "root-b")
        entry = hub_gather_service._RepoCapabilityHintCacheEntry(
            fingerprint=("fp",),
            expires_at=9999999.0,
            items=[{"hint_id": "h1"}],
        )
        hub_gather_service._repo_capability_hint_cache[
            (str(context_a.config.root), "repo-a", "1", "A")
        ] = entry
        hub_gather_service._repo_capability_hint_cache[
            (str(context_b.config.root), "repo-b", "2", "B")
        ] = entry

        hub_gather_service.invalidate_hub_message_snapshot_cache(
            context=context_a,
            include_repo_capability_hints=True,
        )

        assert (
            str(context_a.config.root),
            "repo-a",
            "1",
            "A",
        ) not in hub_gather_service._repo_capability_hint_cache
        assert (
            str(context_b.config.root),
            "repo-b",
            "2",
            "B",
        ) in hub_gather_service._repo_capability_hint_cache
        projection_store.delete.assert_any_call(
            namespace=hub_gather_service.HUB_SNAPSHOT_PROJECTION_NAMESPACE
        )
        projection_store.delete.assert_any_call(
            namespace=hub_gather_service.REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE
        )
        hub_gather_service._repo_capability_hint_cache.clear()

    def test_handles_missing_projection_store_gracefully(self, tmp_path) -> None:
        context = _make_context(tmp_path)
        hub_gather_service.invalidate_hub_message_snapshot_cache(context=context)


class TestLatestDispatchFallbackOrder:
    def test_prefers_handoff_over_turn_summary(self, tmp_path) -> None:
        repo_root = Path(tmp_path)
        run_id = "11111111-1111-1111-1111-111111111111"
        entry_dir = (
            repo_root
            / ".codex-autorunner"
            / "runs"
            / run_id
            / "dispatch_history"
            / "0001"
        )
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "DISPATCH.md").write_text(
            "---\nmode: turn_summary\ntitle: Summary\n---\n\nBody\n",
            encoding="utf-8",
        )
        entry_dir2 = (
            repo_root
            / ".codex-autorunner"
            / "runs"
            / run_id
            / "dispatch_history"
            / "0002"
        )
        entry_dir2.mkdir(parents=True, exist_ok=True)
        (entry_dir2 / "DISPATCH.md").write_text(
            "---\nmode: pause\ntitle: Need Input\n---\n\nBody\n",
            encoding="utf-8",
        )
        latest = hub_gather_service.latest_dispatch(
            repo_root,
            run_id,
            {"workspace_root": str(repo_root), "runs_dir": ".codex-autorunner/runs"},
        )
        assert latest is not None
        assert latest["dispatch"]["mode"] == "pause"
        assert latest["turn_summary"]["mode"] == "turn_summary"
