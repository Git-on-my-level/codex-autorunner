"""
Contract tests for TicketStore implementations.

Every adapter registered in ``TICKET_STORE_FACTORIES`` must satisfy these
contracts.  These tests intentionally assert adapter registration coverage so
new exported ticket stores cannot bypass the shared behavior checks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import pytest

from codex_autorunner.core import adapters as adapter_exports
from codex_autorunner.core.adapters import FilesystemTicketStore
from codex_autorunner.core.domain.refs import ScopeRef, ScopeRefError, TicketRef
from codex_autorunner.core.ports.ticket_store import TicketRecord, TicketStatus
from tests.contracts.conftest import TICKET_STORE_FACTORIES


def _factory_name(class_name: str, suffix: str) -> str:
    assert class_name.endswith(suffix)
    return class_name[: -len(suffix)].lower()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestTicketStoreContract:
    """Invariants every TicketStore must satisfy."""

    def test_exported_ticket_store_adapters_have_contract_factories(self) -> None:
        registered = {name for name, _ in TICKET_STORE_FACTORIES}
        expected = {
            _factory_name(name, "TicketStore")
            for name in adapter_exports.__all__
            if name.endswith("TicketStore")
        }
        assert registered == expected

    def test_create_then_get_roundtrip(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = TicketRef(scope=scope, ticket_id="pma-001")
        record = TicketRecord(
            ref=ref,
            title="Add scope support",
            status=TicketStatus.PENDING,
            agent="codex",
            description="Implement the repo scoped work.",
        )

        returned = _run(store.create(record))
        loaded = _run(store.get(ref))

        assert returned == record
        assert loaded is not None
        assert loaded.ref == ref
        assert loaded.title == "Add scope support"
        assert loaded.status == TicketStatus.PENDING
        assert loaded.agent == "codex"
        assert loaded.description == "Implement the repo scoped work."

    def test_list_by_scope_returns_only_that_scope(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        repo_scope = ScopeRef(kind="repo", id="repo-1")
        wt_scope = ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
        _run(
            store.create(
                TicketRecord(
                    ref=TicketRef(scope=repo_scope, ticket_id="repo-ticket"),
                    title="Repo ticket",
                )
            )
        )
        _run(
            store.create(
                TicketRecord(
                    ref=TicketRef(scope=wt_scope, ticket_id="wt-ticket"),
                    title="Worktree ticket",
                )
            )
        )

        repo_records = _run(store.list_by_scope(repo_scope))
        wt_records = _run(store.list_by_scope(wt_scope))

        assert [record.ref.ticket_id for record in repo_records] == ["repo-ticket"]
        assert [record.ref.ticket_id for record in wt_records] == ["wt-ticket"]

    def test_update_status_roundtrip(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = TicketRef(scope=scope, ticket_id="pma-002")
        _run(store.create(TicketRecord(ref=ref, title="Finish work")))

        updated = _run(store.update_status(ref, TicketStatus.DONE))
        loaded = _run(store.get(ref))

        assert updated is not None
        assert updated.status == TicketStatus.DONE
        assert loaded is not None
        assert loaded.status == TicketStatus.DONE

    def test_update_missing_ticket_returns_none(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        ref = TicketRef(scope=ScopeRef(kind="repo", id="repo-1"), ticket_id="missing")
        assert _run(store.update_status(ref, TicketStatus.DONE)) is None

    def test_delete_existing_then_get_returns_none(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = TicketRef(scope=scope, ticket_id="pma-003")
        _run(store.create(TicketRecord(ref=ref, title="Delete me")))

        assert _run(store.delete(ref)) is True
        assert _run(store.get(ref)) is None

    def test_delete_missing_ticket_returns_false(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        ref = TicketRef(scope=ScopeRef(kind="repo", id="repo-1"), ticket_id="missing")
        assert _run(store.delete(ref)) is False

    def test_missing_scope_raises_scope_error(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="missing-repo")
        ref = TicketRef(scope=scope, ticket_id="pma-004")
        with pytest.raises(ScopeRefError, match="Unknown repo scope"):
            _run(store.get(ref))

    def test_has_required_protocol_methods(
        self,
        ticket_store_factory: Callable[[Path], FilesystemTicketStore],
        tmp_path: Path,
    ) -> None:
        store = ticket_store_factory(tmp_path)
        assert callable(getattr(store, "create", None))
        assert callable(getattr(store, "get", None))
        assert callable(getattr(store, "list_by_scope", None))
        assert callable(getattr(store, "update_status", None))
        assert callable(getattr(store, "delete", None))
