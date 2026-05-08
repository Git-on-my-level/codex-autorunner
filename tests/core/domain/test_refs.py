from __future__ import annotations

import pytest

from codex_autorunner.core.domain.refs import (
    AgentRef,
    MemoryRef,
    ParticipantRef,
    ScopeRef,
    ScopeRefError,
    SurfaceRef,
    TicketRef,
)
from codex_autorunner.core.domain.scope_urn import ScopeUrnKindError


class TestScopeRef:
    def test_hub(self) -> None:
        s = ScopeRef(kind="hub")
        assert s.kind == "hub"
        assert s.id is None
        assert s.parent_repo_id is None

    def test_repo(self) -> None:
        s = ScopeRef(kind="repo", id="r1")
        assert s.kind == "repo"
        assert s.id == "r1"

    def test_worktree(self) -> None:
        s = ScopeRef(kind="worktree", id="wt1", parent_repo_id="r1")
        assert s.kind == "worktree"
        assert s.id == "wt1"
        assert s.parent_repo_id == "r1"

    def test_filesystem(self) -> None:
        s = ScopeRef(kind="filesystem", path="/tmp/car repo")
        assert s.kind == "filesystem"
        assert s.path == "/tmp/car repo"

    def test_hub_with_id_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="must not have an id"):
            ScopeRef(kind="hub", id="x")

    def test_hub_with_parent_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="must not have a parent_repo_id"):
            ScopeRef(kind="hub", parent_repo_id="x")

    def test_repo_missing_id_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="requires an id"):
            ScopeRef(kind="repo")

    def test_repo_with_parent_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="must not have a parent_repo_id"):
            ScopeRef(kind="repo", id="r1", parent_repo_id="x")

    def test_repo_with_path_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="must not have a path"):
            ScopeRef(kind="repo", id="r1", path="/tmp/repo")

    def test_worktree_missing_id_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="requires an id"):
            ScopeRef(kind="worktree", parent_repo_id="r1")

    def test_worktree_missing_parent_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="requires a parent_repo_id"):
            ScopeRef(kind="worktree", id="wt1")

    def test_filesystem_missing_path_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="requires a path"):
            ScopeRef(kind="filesystem")

    def test_filesystem_with_id_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="must not have an id"):
            ScopeRef(kind="filesystem", id="fs1", path="/tmp/repo")

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="Unknown scope kind"):
            ScopeRef(kind="planet", id="earth")

    def test_frozen(self) -> None:
        s = ScopeRef(kind="hub")
        with pytest.raises(AttributeError):
            s.kind = "repo"  # type: ignore[misc]

    def test_to_dict(self) -> None:
        s = ScopeRef(kind="repo", id="r1")
        assert s.to_dict() == {
            "kind": "repo",
            "id": "r1",
            "parent_repo_id": None,
            "path": None,
        }

    def test_to_urn(self) -> None:
        assert ScopeRef(kind="hub").to_urn() == "hub"
        assert ScopeRef(kind="repo", id="r1").to_urn() == "repo:r1"
        assert (
            ScopeRef(kind="worktree", id="wt1", parent_repo_id="r1").to_urn()
            == "worktree:r1/wt1"
        )
        assert (
            ScopeRef(kind="filesystem", path="/tmp/car repo").to_urn()
            == "filesystem:%2Ftmp%2Fcar%20repo"
        )

    def test_from_urn(self) -> None:
        assert ScopeRef.from_urn("hub") == ScopeRef(kind="hub")
        assert ScopeRef.from_urn("repo:r1") == ScopeRef(kind="repo", id="r1")
        assert ScopeRef.from_urn("worktree:r1/wt1") == ScopeRef(
            kind="worktree", id="wt1", parent_repo_id="r1"
        )
        with pytest.raises(ScopeUrnKindError):
            ScopeRef.from_urn("legacy_scope_kind:ws1")
        assert ScopeRef.from_urn("filesystem:%2Ftmp%2Fcar%20repo") == ScopeRef(
            kind="filesystem", path="/tmp/car repo"
        )

    def test_from_mapping_with_urn(self) -> None:
        s = ScopeRef.from_mapping({"urn": "repo:r1"})
        assert s == ScopeRef(kind="repo", id="r1")

    def test_from_mapping_with_kind_and_id(self) -> None:
        s = ScopeRef.from_mapping({"kind": "repo", "id": "r1"})
        assert s == ScopeRef(kind="repo", id="r1")

    def test_from_mapping_with_repo_id_fallback(self) -> None:
        s = ScopeRef.from_mapping({"repo_id": "r1"})
        assert s == ScopeRef(kind="repo", id="r1")

    def test_from_mapping_with_workspace_root_fallback(self) -> None:
        s = ScopeRef.from_mapping({"workspace_root": "/tmp/repo"})
        assert s == ScopeRef(kind="filesystem", path="/tmp/repo")

    def test_from_mapping_with_filesystem_kind_and_workspace_root(self) -> None:
        s = ScopeRef.from_mapping({"kind": "filesystem", "workspace_root": "/tmp/repo"})
        assert s == ScopeRef(kind="filesystem", path="/tmp/repo")

    def test_from_mapping_missing_fields_raises(self) -> None:
        with pytest.raises(ScopeRefError, match="requires kind or repo_id"):
            ScopeRef.from_mapping({})

    def test_equality(self) -> None:
        assert ScopeRef(kind="repo", id="r1") == ScopeRef(kind="repo", id="r1")
        assert ScopeRef(kind="hub") != ScopeRef(kind="repo", id="hub")

    def test_hashable(self) -> None:
        s = {ScopeRef(kind="hub"), ScopeRef(kind="repo", id="r1")}
        assert len(s) == 2
        s.add(ScopeRef(kind="hub"))
        assert len(s) == 2


class TestSurfaceRef:
    def test_construction(self) -> None:
        s = SurfaceRef(kind="telegram", key="chat:123")
        assert s.kind == "telegram"
        assert s.key == "chat:123"

    def test_frozen(self) -> None:
        s = SurfaceRef(kind="telegram", key="chat:123")
        with pytest.raises(AttributeError):
            s.kind = "discord"  # type: ignore[misc]

    def test_to_dict(self) -> None:
        s = SurfaceRef(kind="discord", key="chan:456")
        assert s.to_dict() == {"kind": "discord", "key": "chan:456"}

    def test_from_mapping_surface_prefix(self) -> None:
        s = SurfaceRef.from_mapping({"surface_kind": "telegram", "surface_key": "t:1"})
        assert s == SurfaceRef(kind="telegram", key="t:1")

    def test_from_mapping_short_keys(self) -> None:
        s = SurfaceRef.from_mapping({"kind": "discord", "key": "c:1"})
        assert s == SurfaceRef(kind="discord", key="c:1")

    def test_from_mapping_missing_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a kind"):
            SurfaceRef.from_mapping({"key": "c:1"})

    def test_from_mapping_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a key"):
            SurfaceRef.from_mapping({"kind": "discord"})


class TestParticipantRef:
    def test_construction(self) -> None:
        p = ParticipantRef(kind="user", id="u1")
        assert p.kind == "user"
        assert p.id == "u1"
        assert p.display_name is None

    def test_with_display_name(self) -> None:
        p = ParticipantRef(kind="agent", id="a1", display_name="Codex")
        assert p.display_name == "Codex"

    def test_to_dict(self) -> None:
        p = ParticipantRef(kind="user", id="u1", display_name="Alice")
        d = p.to_dict()
        assert d == {"kind": "user", "id": "u1", "display_name": "Alice"}

    def test_from_mapping(self) -> None:
        p = ParticipantRef.from_mapping({"kind": "user", "id": "u1"})
        assert p == ParticipantRef(kind="user", id="u1")

    def test_from_mapping_missing_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a kind"):
            ParticipantRef.from_mapping({"id": "u1"})

    def test_from_mapping_missing_id_raises(self) -> None:
        with pytest.raises(ValueError, match="requires an id"):
            ParticipantRef.from_mapping({"kind": "user"})


class TestAgentRef:
    def test_construction(self) -> None:
        a = AgentRef(agent_id="codex")
        assert a.agent_id == "codex"
        assert a.profile is None

    def test_with_profile(self) -> None:
        a = AgentRef(agent_id="codex", profile="m4-pma")
        assert a.profile == "m4-pma"

    def test_to_dict(self) -> None:
        a = AgentRef(agent_id="hermes", profile="default")
        assert a.to_dict() == {"agent_id": "hermes", "profile": "default"}

    def test_from_mapping_agent_id(self) -> None:
        a = AgentRef.from_mapping({"agent_id": "codex"})
        assert a == AgentRef(agent_id="codex")

    def test_from_mapping_agent_alias(self) -> None:
        a = AgentRef.from_mapping({"agent": "hermes"})
        assert a == AgentRef(agent_id="hermes")

    def test_from_mapping_with_profile(self) -> None:
        a = AgentRef.from_mapping({"agent_id": "codex", "agent_profile": "m4"})
        assert a.profile == "m4"

    def test_from_mapping_missing_agent_id_raises(self) -> None:
        with pytest.raises(ValueError, match="requires an agent_id"):
            AgentRef.from_mapping({})


class TestMemoryRef:
    def test_construction(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        m = MemoryRef(scope=scope, key="context")
        assert m.scope == scope
        assert m.key == "context"

    def test_to_dict(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        m = MemoryRef(scope=scope, key="transcript")
        d = m.to_dict()
        assert d == {
            "scope": {
                "kind": "repo",
                "id": "r1",
                "parent_repo_id": None,
                "path": None,
            },
            "key": "transcript",
        }

    def test_from_mapping(self) -> None:
        m = MemoryRef.from_mapping(
            {"scope": {"kind": "repo", "id": "r1"}, "key": "context"}
        )
        assert m.scope == ScopeRef(kind="repo", id="r1")
        assert m.key == "context"

    def test_from_mapping_missing_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a scope"):
            MemoryRef.from_mapping({"key": "context"})

    def test_from_mapping_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a key"):
            MemoryRef.from_mapping({"scope": {"kind": "hub"}})


class TestTicketRef:
    def test_construction(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        t = TicketRef(scope=scope, ticket_id="TICKET-001")
        assert t.scope == scope
        assert t.ticket_id == "TICKET-001"

    def test_to_dict(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        t = TicketRef(scope=scope, ticket_id="TICKET-042")
        d = t.to_dict()
        assert d == {
            "scope": {
                "kind": "repo",
                "id": "r1",
                "parent_repo_id": None,
                "path": None,
            },
            "ticket_id": "TICKET-042",
        }

    def test_from_mapping(self) -> None:
        t = TicketRef.from_mapping(
            {"scope": {"kind": "repo", "id": "r1"}, "ticket_id": "TICKET-007"}
        )
        assert t.scope == ScopeRef(kind="repo", id="r1")
        assert t.ticket_id == "TICKET-007"

    def test_from_mapping_missing_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a scope"):
            TicketRef.from_mapping({"ticket_id": "TICKET-001"})

    def test_from_mapping_missing_ticket_id_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a ticket_id"):
            TicketRef.from_mapping({"scope": {"kind": "hub"}})
