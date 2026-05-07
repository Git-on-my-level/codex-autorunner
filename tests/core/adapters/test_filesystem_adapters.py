from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_autorunner.core.adapters import (
    FilesystemMemoryStore,
    FilesystemScopeResolver,
    FilesystemTicketStore,
)
from codex_autorunner.core.domain.refs import (
    MemoryRef,
    ScopeRef,
    ScopeRefError,
    TicketRef,
)
from codex_autorunner.core.ports.memory_store import MemoryDoc, MemoryDocs
from codex_autorunner.core.ports.scope_resolver import ResolvedScope
from codex_autorunner.core.ports.ticket_store import TicketRecord, TicketStatus
from codex_autorunner.manifest import (
    Manifest,
    ManifestRepo,
)


def _make_hub_tree(tmp_path: Path) -> tuple[Path, Manifest]:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    repo_dir = hub_root / "my-repo"
    repo_dir.mkdir()
    wt_dir = hub_root / "my-wt"
    wt_dir.mkdir()
    car_dir = repo_dir / ".codex-autorunner"
    car_dir.mkdir()
    (car_dir / "tickets").mkdir()
    (car_dir / "contextspace").mkdir()
    wt_car = wt_dir / ".codex-autorunner"
    wt_car.mkdir()
    (wt_car / "tickets").mkdir()
    (wt_car / "contextspace").mkdir()
    manifest = Manifest(
        version=3,
        repos=[
            ManifestRepo(
                id="repo-1",
                path=Path("my-repo"),
                kind="base",
                display_name="My Repo",
            ),
            ManifestRepo(
                id="wt-1",
                path=Path("my-wt"),
                kind="worktree",
                worktree_of="repo-1",
                display_name="My Worktree",
            ),
        ],
    )
    return hub_root, manifest


def _write_ticket(
    ticket_dir: Path,
    index: int,
    ticket_id: str,
    title: str = "Test",
    agent: str = "opencode",
    done: bool = False,
) -> Path:
    filename = f"TICKET-{index:03d}.md"
    path = ticket_dir / filename
    content = (
        f"---\nticket_id: {ticket_id}\ntitle: {title}\n"
        f"agent: {agent}\ndone: {done}\n---\n\nBody text.\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _write_contextspace_doc(repo_root: Path, kind: str, content: str) -> None:
    cs_dir = repo_root / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = cs_dir / f"{kind}.md"
    doc_path.write_text(content, encoding="utf-8")


class _NoWorkspaceResolver:
    def resolve(self, ref: ScopeRef) -> ResolvedScope:
        return ResolvedScope(scope=ref, display_name="No workspace")

    def resolve_parent(self, ref: ScopeRef) -> ScopeRef | None:
        return None

    def resolve_children(self, ref: ScopeRef) -> list[ScopeRef]:
        return []


class TestFilesystemScopeResolver:
    def test_resolve_hub(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        hub = ScopeRef(kind="hub")
        result = resolver.resolve(hub)
        assert result.workspace_root == str(hub_root)
        assert result.display_name == "Hub"

    def test_resolve_repo(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        repo = ScopeRef(kind="repo", id="repo-1")
        result = resolver.resolve(repo)
        assert result.workspace_root == str(hub_root / "my-repo")
        assert result.display_name == "My Repo"

    def test_resolve_unknown_repo_raises(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        with pytest.raises(ScopeRefError, match="Unknown repo scope"):
            resolver.resolve(ScopeRef(kind="repo", id="nope"))

    def test_resolve_repo_rejects_worktree_manifest_entry(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        with pytest.raises(ScopeRefError, match="not a repo scope"):
            resolver.resolve(ScopeRef(kind="repo", id="wt-1"))

    def test_resolve_worktree(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        wt = ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
        result = resolver.resolve(wt)
        assert result.workspace_root == str(hub_root / "my-wt")
        assert result.metadata.get("worktree_of") == "repo-1"

    def test_resolve_worktree_rejects_base_manifest_entry(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        wt = ScopeRef(kind="worktree", id="repo-1", parent_repo_id="repo-1")
        with pytest.raises(ScopeRefError, match="not a worktree scope"):
            resolver.resolve(wt)

    def test_resolve_filesystem(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        fs = ScopeRef(kind="filesystem", path="/tmp/foo")
        result = resolver.resolve(fs)
        assert result.workspace_root == "/tmp/foo"

    def test_resolve_parent(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        assert resolver.resolve_parent(ScopeRef(kind="hub")) is None
        assert resolver.resolve_parent(ScopeRef(kind="repo", id="r1")) == ScopeRef(
            kind="hub"
        )
        assert resolver.resolve_parent(
            ScopeRef(kind="worktree", id="w1", parent_repo_id="r1")
        ) == ScopeRef(kind="repo", id="r1")

    def test_resolve_children_hub(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        children = resolver.resolve_children(ScopeRef(kind="hub"))
        kinds = {(c.kind, c.id) for c in children}
        assert ("repo", "repo-1") in kinds

    def test_resolve_children_repo_returns_worktrees(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        children = resolver.resolve_children(ScopeRef(kind="repo", id="repo-1"))
        assert len(children) == 1
        assert children[0].kind == "worktree"
        assert children[0].id == "wt-1"

    def test_resolve_children_leaf_returns_empty(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        assert (
            resolver.resolve_children(
                ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
            )
            == []
        )

    def test_satisfies_protocol(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver: FilesystemScopeResolver = FilesystemScopeResolver(hub_root, manifest)
        assert hasattr(resolver, "resolve")
        assert hasattr(resolver, "resolve_parent")
        assert hasattr(resolver, "resolve_children")


class TestFilesystemMemoryStore:
    def test_load_scope_reads_hub_memory(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        _write_contextspace_doc(hub_root, "active_context", "hub memory")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)

        result = asyncio.get_event_loop().run_until_complete(
            store.load_scope(ScopeRef(kind="hub"))
        )
        assert {doc.key: doc.content for doc in result.docs} == {
            "active_context": "hub memory"
        }

    def test_load_existing_doc(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        _write_contextspace_doc(repo_root, "decisions", "# Decisions\n")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="decisions")

        doc = asyncio.get_event_loop().run_until_complete(store.load(ref))
        assert doc is not None
        assert doc.key == "decisions"
        assert "# Decisions" in doc.content

    def test_load_worktree_doc_does_not_read_repo_memory(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        wt_root = hub_root / "my-wt"
        _write_contextspace_doc(repo_root, "active_context", "repo memory")
        _write_contextspace_doc(wt_root, "active_context", "worktree memory")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
        ref = MemoryRef(scope=scope, key="active_context")

        doc = asyncio.get_event_loop().run_until_complete(store.load(ref))
        assert doc is not None
        assert doc.content == "worktree memory"

    def test_load_missing_doc_returns_none(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")

        doc = asyncio.get_event_loop().run_until_complete(store.load(ref))
        assert doc is None

    def test_load_scope(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        _write_contextspace_doc(repo_root, "active_context", "ctx")
        _write_contextspace_doc(repo_root, "decisions", "dec")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")

        result = asyncio.get_event_loop().run_until_complete(store.load_scope(scope))
        assert isinstance(result, MemoryDocs)
        keys = {d.key for d in result.docs}
        assert "active_context" in keys
        assert "decisions" in keys

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="active_context")

        doc = MemoryDoc(key="active_context", content="saved content")
        asyncio.get_event_loop().run_until_complete(store.save(ref, doc))

        loaded = asyncio.get_event_loop().run_until_complete(store.load(ref))
        assert loaded is not None
        assert loaded.content == "saved content"

    def test_delete_doc(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        _write_contextspace_doc(repo_root, "spec", "to be deleted")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")

        deleted = asyncio.get_event_loop().run_until_complete(store.delete(ref))
        assert deleted is True

        loaded = asyncio.get_event_loop().run_until_complete(store.load(ref))
        assert loaded is None

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemMemoryStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")

        deleted = asyncio.get_event_loop().run_until_complete(store.delete(ref))
        assert deleted is False

    def test_load_scope_without_workspace_root(self) -> None:
        store = FilesystemMemoryStore(_NoWorkspaceResolver())
        scope = ScopeRef(kind="hub")
        result = asyncio.get_event_loop().run_until_complete(store.load_scope(scope))
        assert result.docs == []

    def test_save_without_workspace_root_raises(self) -> None:
        store = FilesystemMemoryStore(_NoWorkspaceResolver())
        ref = MemoryRef(scope=ScopeRef(kind="hub"), key="active_context")

        with pytest.raises(ValueError, match="without workspace_root"):
            asyncio.get_event_loop().run_until_complete(
                store.save(ref, MemoryDoc(key="active_context", content="ctx"))
            )


class TestFilesystemTicketStore:
    def test_create_and_get(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="tkt_0001")

        record = TicketRecord(ref=tref, title="Do thing", agent="opencode")
        asyncio.get_event_loop().run_until_complete(store.create(record))

        fetched = asyncio.get_event_loop().run_until_complete(store.get(tref))
        assert fetched is not None
        assert fetched.title == "Do thing"
        assert fetched.status == TicketStatus.PENDING

    def test_list_by_scope(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        _write_ticket(ticket_dir, 1, "tkt_aaaa", title="A")
        _write_ticket(ticket_dir, 2, "tkt_bbbb", title="B", done=True)

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")

        records = asyncio.get_event_loop().run_until_complete(
            store.list_by_scope(scope)
        )
        assert len(records) == 2
        by_title = {r.title: r for r in records}
        assert "A" in by_title
        assert "B" in by_title
        assert by_title["A"].status == TicketStatus.PENDING
        assert by_title["B"].status == TicketStatus.DONE

    def test_list_by_scope_does_not_recurse_into_nested_workspaces(
        self, tmp_path: Path
    ) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        _write_ticket(ticket_dir, 1, "tkt_repo", title="Repo ticket")
        nested_ticket_dir = repo_root / "nested" / ".codex-autorunner" / "tickets"
        nested_ticket_dir.mkdir(parents=True)
        _write_ticket(nested_ticket_dir, 1, "tkt_nested", title="Nested ticket")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        records = asyncio.get_event_loop().run_until_complete(
            store.list_by_scope(ScopeRef(kind="repo", id="repo-1"))
        )

        assert [record.ref.ticket_id for record in records] == ["tkt_repo"]

    def test_worktree_tickets_do_not_include_repo_tickets(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_ticket_dir = hub_root / "my-repo" / ".codex-autorunner" / "tickets"
        wt_ticket_dir = hub_root / "my-wt" / ".codex-autorunner" / "tickets"
        _write_ticket(repo_ticket_dir, 1, "tkt_repo", title="Repo ticket")
        _write_ticket(wt_ticket_dir, 1, "tkt_worktree", title="Worktree ticket")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")

        records = asyncio.get_event_loop().run_until_complete(
            store.list_by_scope(scope)
        )
        assert [record.ref.ticket_id for record in records] == ["tkt_worktree"]

    def test_create_without_workspace_root_raises(self) -> None:
        store = FilesystemTicketStore(_NoWorkspaceResolver())
        record = TicketRecord(
            ref=TicketRef(scope=ScopeRef(kind="hub"), ticket_id="tkt_nowhere"),
            title="No workspace",
        )

        with pytest.raises(ValueError, match="without workspace_root"):
            asyncio.get_event_loop().run_until_complete(store.create(record))

    def test_update_status(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        _write_ticket(ticket_dir, 1, "tkt_mark1", title="Mark done")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="tkt_mark1")

        updated = asyncio.get_event_loop().run_until_complete(
            store.update_status(tref, TicketStatus.DONE)
        )
        assert updated is not None
        assert updated.status == TicketStatus.DONE

        fetched = asyncio.get_event_loop().run_until_complete(store.get(tref))
        assert fetched is not None
        assert fetched.status == TicketStatus.DONE

    def test_delete_ticket(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        _write_ticket(ticket_dir, 1, "tkt_dele")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="tkt_dele")

        deleted = asyncio.get_event_loop().run_until_complete(store.delete(tref))
        assert deleted is True

        fetched = asyncio.get_event_loop().run_until_complete(store.get(tref))
        assert fetched is None

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="nope")

        fetched = asyncio.get_event_loop().run_until_complete(store.get(tref))
        assert fetched is None

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="ghost")

        deleted = asyncio.get_event_loop().run_until_complete(store.delete(tref))
        assert deleted is False

    def test_update_status_missing_returns_none(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="ghost")

        result = asyncio.get_event_loop().run_until_complete(
            store.update_status(tref, TicketStatus.DONE)
        )
        assert result is None

    def test_list_empty_scope(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")

        records = asyncio.get_event_loop().run_until_complete(
            store.list_by_scope(scope)
        )
        assert records == []

    def test_create_auto_indexes(self, tmp_path: Path) -> None:
        hub_root, manifest = _make_hub_tree(tmp_path)
        repo_root = hub_root / "my-repo"
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        _write_ticket(ticket_dir, 1, "tkt_exst")

        resolver = FilesystemScopeResolver(hub_root, manifest)
        store = FilesystemTicketStore(resolver)
        scope = ScopeRef(kind="repo", id="repo-1")
        tref = TicketRef(scope=scope, ticket_id="tkt_new1")

        record = TicketRecord(ref=tref, title="New ticket")
        asyncio.get_event_loop().run_until_complete(store.create(record))

        new_path = ticket_dir / "TICKET-002.md"
        assert new_path.exists()
