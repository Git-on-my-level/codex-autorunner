from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_autorunner.core.archive import (
    ArchiveEntrySpec,
    _build_meta,
    _contextspace_is_dirty,
    _contextspace_source,
    _copy_entry,
    _directory_has_any_entries,
    _flow_summary,
    _is_within,
    _json_state_file_is_dirty,
    _log_file_is_dirty,
    _planned_reset_car_state_paths,
    _remove_source_entry,
    _sanitize_branch,
    _snapshot_timestamp,
    _tickets_are_dirty,
    _tree_has_payload,
    build_common_car_archive_entries,
    build_snapshot_id,
    dirty_car_state_paths,
    execute_archive_entries,
    has_car_state,
    resolve_workspace_archive_target,
    resolve_worktree_archive_intent,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestSanitizeBranch:
    def test_normal_branch(self) -> None:
        assert _sanitize_branch("feature/my-branch") == "feature-my-branch"

    def test_none_returns_unknown(self) -> None:
        assert _sanitize_branch(None) == "unknown"

    def test_empty_returns_unknown(self) -> None:
        assert _sanitize_branch("") == "unknown"

    def test_strips_dashes(self) -> None:
        assert _sanitize_branch("--branch--") == "branch"

    def test_all_special_chars(self) -> None:
        assert _sanitize_branch("a!@#$b") == "a-b"

    def test_only_special_chars_returns_unknown(self) -> None:
        assert _sanitize_branch("!@#$") == "unknown"


class TestIsWithin:
    def test_child_is_within(self, tmp_path: Path) -> None:
        child = tmp_path / "sub" / "file.txt"
        child.parent.mkdir(parents=True)
        child.write_text("x")
        assert _is_within(root=tmp_path, target=child) is True

    def test_outside_is_not_within(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside"
        outside.mkdir()
        assert _is_within(root=tmp_path, target=outside) is False

    def test_child_path_is_within(self, tmp_path: Path) -> None:
        assert _is_within(root=tmp_path, target=tmp_path / "nonexistent") is True

    def test_copies_directory(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "mydir"
        src_dir.mkdir()
        (src_dir / "inner.txt").write_text("inner")
        dest_dir = tmp_path / "mydir_copy"
        stats: dict[str, int] = {"file_count": 0, "total_bytes": 0}
        result = _copy_entry(
            src_dir,
            dest_dir,
            tmp_path,
            stats,
            visited=set(),
            skipped_symlinks=[],
        )
        assert result is True
        assert (dest_dir / "inner.txt").read_text() == "inner"

    def test_skips_escaping_symlink(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        link = worktree / "link.txt"
        link.symlink_to(secret)
        dest = tmp_path / "dest_link.txt"
        skipped: list[str] = []
        result = _copy_entry(
            link,
            dest,
            worktree,
            {"file_count": 0, "total_bytes": 0},
            visited=set(),
            skipped_symlinks=skipped,
        )
        assert result is False
        assert len(skipped) == 1

    def test_nonexistent_returns_false(self, tmp_path: Path) -> None:
        src = tmp_path / "nonexistent"
        dest = tmp_path / "dest"
        stats: dict[str, int] = {"file_count": 0, "total_bytes": 0}
        result = _copy_entry(
            src,
            dest,
            tmp_path,
            stats,
            visited=set(),
            skipped_symlinks=[],
        )
        assert result is False

    def test_symlink_to_dir_within_worktree(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        real_dir = worktree / "real"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("data")
        link_dir = worktree / "link"
        link_dir.symlink_to(real_dir)
        dest = tmp_path / "dest_link"
        stats: dict[str, int] = {"file_count": 0, "total_bytes": 0}
        result = _copy_entry(
            link_dir,
            dest,
            worktree,
            stats,
            visited=set(),
            skipped_symlinks=[],
        )
        assert result is True
        assert (dest / "file.txt").read_text() == "data"


class TestRemoveSourceEntry:
    def test_removes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("x")
        _remove_source_entry(f)
        assert not f.exists()

    def test_removes_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "inner.txt").write_text("x")
        _remove_source_entry(d)
        assert not d.exists()

    def test_removes_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        _remove_source_entry(link)
        assert not link.exists()
        assert target.exists()


class TestDirectoryHasAnyEntries:
    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert _directory_has_any_entries(d) is False

    def test_nonexistent(self, tmp_path: Path) -> None:
        assert _directory_has_any_entries(tmp_path / "nope") is False

    def test_with_file(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "file.txt").write_text("x")
        assert _directory_has_any_entries(d) is True


class TestTreeHasPayload:
    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert _tree_has_payload(d) is False

    def test_nonexistent(self, tmp_path: Path) -> None:
        assert _tree_has_payload(tmp_path / "nope") is False

    def test_with_file(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "file.txt").write_text("x")
        assert _tree_has_payload(d) is True

    def test_with_symlink(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        target = tmp_path / "target"
        target.write_text("x")
        (d / "link").symlink_to(target)
        assert _tree_has_payload(d) is True


class TestContextspaceIsDirty:
    def test_nonexistent(self, tmp_path: Path) -> None:
        assert _contextspace_is_dirty(tmp_path / "nope") is False

    def test_empty_default_docs_not_dirty(self, tmp_path: Path) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "active_context.md").write_text("")
        (d / "decisions.md").write_text("")
        (d / "spec.md").write_text("")
        assert _contextspace_is_dirty(d) is False

    def test_nonempty_doc_is_dirty(self, tmp_path: Path) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "active_context.md").write_text("has content")
        assert _contextspace_is_dirty(d) is True

    def test_extra_file_is_dirty(self, tmp_path: Path) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "active_context.md").write_text("")
        (d / "extra.md").write_text("extra")
        assert _contextspace_is_dirty(d) is True


class TestTicketsAreDirty:
    def test_nonexistent(self, tmp_path: Path) -> None:
        assert _tickets_are_dirty(tmp_path / "nope") is False

    def test_only_agents_md_not_dirty(self, tmp_path: Path) -> None:
        d = tmp_path / "tickets"
        d.mkdir()
        (d / "AGENTS.md").write_text("agents")
        assert _tickets_are_dirty(d) is False

    def test_ticket_file_is_dirty(self, tmp_path: Path) -> None:
        d = tmp_path / "tickets"
        d.mkdir()
        (d / "TICKET-001.md").write_text("ticket")
        assert _tickets_are_dirty(d) is True


class TestJsonStateFileIsDirty:
    def test_nonexistent(self, tmp_path: Path) -> None:
        assert _json_state_file_is_dirty(tmp_path / "nope") is False

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text("")
        assert _json_state_file_is_dirty(f) is False

    def test_empty_object(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text("{}")
        assert _json_state_file_is_dirty(f) is False

    def test_empty_array(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text("[]")
        assert _json_state_file_is_dirty(f) is False

    def test_null(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text("null")
        assert _json_state_file_is_dirty(f) is False

    def test_nonempty_object_is_dirty(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text('{"key": "value"}')
        assert _json_state_file_is_dirty(f) is True


class TestLogFileIsDirty:
    def test_nonexistent(self, tmp_path: Path) -> None:
        assert _log_file_is_dirty(tmp_path / "nope") is False

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("")
        assert _log_file_is_dirty(f) is False

    def test_nonempty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("log entry")
        assert _log_file_is_dirty(f) is True


class TestContextspaceSource:
    def test_prefers_contextspace(self, tmp_path: Path) -> None:
        ctx = tmp_path / "contextspace"
        ctx.mkdir()
        result = _contextspace_source(tmp_path)
        assert result == ctx

    def test_falls_back_to_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = _contextspace_source(tmp_path)
        assert result == ws

    def test_default_contextspace_if_neither(self, tmp_path: Path) -> None:
        result = _contextspace_source(tmp_path)
        assert result == tmp_path / "contextspace"

    def test_contextspace_symlink(self, tmp_path: Path) -> None:
        ctx = tmp_path / "contextspace"
        target = tmp_path / "real_contextspace"
        target.mkdir()
        ctx.symlink_to(target)
        result = _contextspace_source(tmp_path)
        assert result == ctx


class TestBuildSnapshotId:
    def test_format(self) -> None:
        sid = build_snapshot_id("main", "abcdef1234567890")
        assert "main" in sid
        assert "abcdef1" in sid

    def test_unknown_sha(self) -> None:
        sid = build_snapshot_id("main", "unknown")
        assert "unknown" in sid

    def test_none_branch(self) -> None:
        sid = build_snapshot_id(None, "abcdef1234567890")
        assert "unknown" in sid


class TestSnapshotTimestamp:
    def test_format(self) -> None:
        ts = _snapshot_timestamp()
        assert len(ts) == 16
        assert ts.endswith("Z")


class TestResolveWorktreeArchiveIntent:
    def test_portable_review(self) -> None:
        assert resolve_worktree_archive_intent() == "review_snapshot"

    def test_full_review(self) -> None:
        assert resolve_worktree_archive_intent(profile="full") == "review_snapshot_full"

    def test_portable_cleanup(self) -> None:
        assert resolve_worktree_archive_intent(cleanup=True) == "cleanup_snapshot"

    def test_full_cleanup(self) -> None:
        assert (
            resolve_worktree_archive_intent(profile="full", cleanup=True)
            == "cleanup_snapshot_full"
        )

    def test_invalid_profile(self) -> None:
        with pytest.raises(ValueError, match="Unsupported archive profile"):
            resolve_worktree_archive_intent(profile="invalid")


class TestDirtyCarStatePaths:
    def test_no_car_root(self, tmp_path: Path) -> None:
        assert dirty_car_state_paths(tmp_path) == ()

    def test_seeded_defaults_not_dirty(self, tmp_path: Path) -> None:
        from codex_autorunner.bootstrap import seed_repo_files

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        seed_repo_files(repo_root, git_required=False)
        assert dirty_car_state_paths(repo_root) == ()

    def test_ticket_makes_dirty(self, tmp_path: Path) -> None:
        from codex_autorunner.bootstrap import seed_repo_files

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        seed_repo_files(repo_root, git_required=False)
        _write(repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md", "ticket")
        dirty = dirty_car_state_paths(repo_root)
        assert "tickets" in dirty


class TestHasCarState:
    def test_no_car_root(self, tmp_path: Path) -> None:
        assert has_car_state(tmp_path) is False

    def test_with_dirty_state(self, tmp_path: Path) -> None:
        from codex_autorunner.bootstrap import seed_repo_files

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        seed_repo_files(repo_root, git_required=False)
        _write(repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md", "ticket")
        assert has_car_state(repo_root) is True


class TestExecuteArchiveEntries:
    def test_copies_file(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        src.write_text("hello")
        dest = tmp_path / "out" / "dest.txt"

        entries = [
            ArchiveEntrySpec(label="test", source=src, dest=dest),
        ]
        result = execute_archive_entries(entries, worktree_root=tmp_path)
        assert dest.exists()
        assert result.file_count == 1
        assert "test" in result.copied_paths

    def test_missing_required_is_error(self, tmp_path: Path) -> None:
        src = tmp_path / "nonexistent.txt"
        dest = tmp_path / "out" / "dest.txt"

        entries = [
            ArchiveEntrySpec(label="missing", source=src, dest=dest, required=True),
        ]
        result = execute_archive_entries(entries, worktree_root=tmp_path)
        assert "missing" in result.missing_paths
        assert result.file_count == 0

    def test_missing_optional_skipped(self, tmp_path: Path) -> None:
        src = tmp_path / "nonexistent.txt"
        dest = tmp_path / "out" / "dest.txt"

        entries = [
            ArchiveEntrySpec(label="optional", source=src, dest=dest, required=False),
        ]
        result = execute_archive_entries(entries, worktree_root=tmp_path)
        assert result.missing_paths == ()
        assert result.copied_paths == ()

    def test_move_mode(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        src.write_text("hello")
        dest = tmp_path / "out" / "dest.txt"

        entries = [
            ArchiveEntrySpec(label="moved", source=src, dest=dest, mode="move"),
        ]
        result = execute_archive_entries(entries, worktree_root=tmp_path)
        assert dest.exists()
        assert not src.exists()
        assert "moved" in result.moved_paths


class TestBuildCommonCarArchiveEntries:
    def test_default_entries(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"

        entries = build_common_car_archive_entries(source, dest)
        assert any(e.label == "contextspace" for e in entries)

    def test_all_includes(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"

        entries = build_common_car_archive_entries(
            source,
            dest,
            include_contextspace=True,
            include_flow_store=True,
            include_config=True,
            include_runtime_state=True,
            include_logs=True,
            include_github_context=True,
        )
        labels = {e.label for e in entries}
        assert "contextspace" in labels
        assert "flows.db" in labels
        assert "config.yml" in labels
        assert "state.sqlite3" in labels
        assert "app_server_threads.json" in labels
        assert "codex-autorunner.log" in labels
        assert "codex-server.log" in labels
        assert "github_context" in labels

    def test_no_includes(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"

        entries = build_common_car_archive_entries(
            source,
            dest,
            include_contextspace=False,
            include_flow_store=False,
            include_config=False,
            include_runtime_state=False,
            include_logs=False,
            include_github_context=False,
        )
        assert entries == []


class TestFlowSummary:
    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "flows"
        d.mkdir()
        count, latest = _flow_summary(d)
        assert count == 0
        assert latest is None

    def test_nonexistent(self, tmp_path: Path) -> None:
        count, latest = _flow_summary(tmp_path / "nope")
        assert count == 0
        assert latest is None

    def test_with_runs(self, tmp_path: Path) -> None:
        d = tmp_path / "flows"
        d.mkdir()
        run_a = d / "run-a"
        run_a.mkdir()
        (run_a / "meta.json").write_text("{}")
        run_b = d / "run-b"
        run_b.mkdir()
        (run_b / "meta.json").write_text("{}")
        os.utime(run_a, (100000, 100000))
        os.utime(run_b, (200000, 200000))
        count, latest = _flow_summary(d)
        assert count == 2
        assert latest == "run-b"

    def test_ignores_files(self, tmp_path: Path) -> None:
        d = tmp_path / "flows"
        d.mkdir()
        (d / "readme.txt").write_text("not a run")
        count, latest = _flow_summary(d)
        assert count == 0
        assert latest is None


class TestBuildMeta:
    def test_builds_valid_meta(self, tmp_path: Path) -> None:
        meta = _build_meta(
            snapshot_id="snap-1",
            archive_intent="review_snapshot",
            created_at="2026-01-01T00:00:00Z",
            status="complete",
            base_repo_id="base",
            worktree_repo_id="worktree",
            worktree_of="base",
            branch="main",
            head_sha="abc123",
            source_path=tmp_path,
            copied_paths=["tickets"],
            missing_paths=[],
            skipped_symlinks=[],
            summary={"file_count": 1},
        )
        assert meta["schema_version"] == 1
        assert meta["snapshot_id"] == "snap-1"
        assert meta["status"] == "complete"
        assert "note" not in meta
        assert "error" not in meta

    def test_with_note_and_error(self) -> None:
        meta = _build_meta(
            snapshot_id="snap-1",
            archive_intent="review_snapshot",
            created_at="2026-01-01T00:00:00Z",
            status="failed",
            base_repo_id="base",
            worktree_repo_id="worktree",
            worktree_of="base",
            branch="main",
            head_sha="abc123",
            source_path=Path("/tmp"),
            copied_paths=[],
            missing_paths=[],
            skipped_symlinks=[],
            summary={},
            note="a note",
            error="some error",
        )
        assert meta["note"] == "a note"
        assert meta["error"] == "some error"


class TestResolveWorkspaceArchiveTarget:
    def test_without_manifest(self, tmp_path: Path) -> None:
        workspace = tmp_path / "my-repo"
        workspace.mkdir()
        target = resolve_workspace_archive_target(workspace)
        assert target.base_repo_root == workspace.resolve()
        assert target.workspace_repo_id == "my-repo"
        assert target.worktree_of == "my-repo"

    def test_with_manifest_entry(self, tmp_path: Path) -> None:
        from codex_autorunner.manifest import load_manifest, save_manifest

        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        manifest_path = hub_root / "manifest.yml"
        manifest = load_manifest(manifest_path, hub_root)
        manifest.ensure_repo(
            hub_root,
            hub_root / "repos" / "repo-a",
            repo_id="repo-a",
            display_name="Repo A",
        )
        save_manifest(manifest_path, manifest, hub_root)

        workspace = hub_root / "repos" / "repo-a"
        workspace.mkdir(parents=True)
        target = resolve_workspace_archive_target(
            workspace,
            hub_root=hub_root,
            manifest_path=manifest_path,
        )
        assert target.workspace_repo_id == "repo-a"

    def test_with_missing_manifest_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / "my-repo"
        workspace.mkdir()
        target = resolve_workspace_archive_target(
            workspace,
            hub_root=tmp_path,
            manifest_path=tmp_path / "nonexistent.yml",
        )
        assert target.workspace_repo_id == "my-repo"


class TestPlannedResetCarStatePaths:
    def test_seeded_defaults(self, tmp_path: Path) -> None:
        from codex_autorunner.bootstrap import seed_repo_files

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        seed_repo_files(repo_root, git_required=False)
        paths = _planned_reset_car_state_paths(repo_root)
        assert isinstance(paths, tuple)

    def test_empty_repo(self, tmp_path: Path) -> None:
        paths = _planned_reset_car_state_paths(tmp_path)
        assert paths == ()


class TestCopyTreeCycleDetection:
    def test_symlink_cycle_does_not_recurse_infinitely(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "file.txt").write_text("data")
        (src_dir / "cycle").symlink_to(src_dir)

        dest_dir = tmp_path / "dest"
        stats: dict[str, int] = {"file_count": 0, "total_bytes": 0}
        from codex_autorunner.core.archive import _copy_tree

        _copy_tree(
            src_dir,
            dest_dir,
            tmp_path,
            stats,
            visited=set(),
            skipped_symlinks=[],
        )
        assert (dest_dir / "file.txt").exists()
        assert stats["file_count"] >= 1
