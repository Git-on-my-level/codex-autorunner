from __future__ import annotations

import inspect

from codex_autorunner.core.domain.refs import (
    AgentRef,
    ScopeRef,
    SurfaceRef,
    TicketRef,
)
from codex_autorunner.core.ports import (
    EngineCommand,
    InboundEvent,
    MemoryDoc,
    MemoryDocs,
    MemoryStore,
    OutboundDelivery,
    ResolvedScope,
    ScopeResolver,
    SurfaceCapabilities,
    SurfaceHealth,
    SurfaceHealthStatus,
    SurfacePort,
    ThreadRecord,
    ThreadStatus,
    ThreadStore,
    TicketRecord,
    TicketStatus,
    TicketStore,
)
from codex_autorunner.core.ports.memory_store import (
    MemoryDoc as MemoryDocDirect,
)
from codex_autorunner.core.ports.memory_store import (
    MemoryDocs as MemoryDocsDirect,
)
from codex_autorunner.core.ports.memory_store import (
    MemoryStore as MemoryStoreDirect,
)
from codex_autorunner.core.ports.scope_resolver import (
    ResolvedScope as ResolvedScopeDirect,
)
from codex_autorunner.core.ports.scope_resolver import (
    ScopeResolver as ScopeResolverDirect,
)
from codex_autorunner.core.ports.surface_port import (
    EngineCommand as EngineCommandDirect,
)
from codex_autorunner.core.ports.surface_port import (
    InboundEvent as InboundEventDirect,
)
from codex_autorunner.core.ports.surface_port import (
    OutboundDelivery as OutboundDeliveryDirect,
)
from codex_autorunner.core.ports.surface_port import (
    SurfaceCapabilities as SurfaceCapabilitiesDirect,
)
from codex_autorunner.core.ports.surface_port import (
    SurfaceHealth as SurfaceHealthDirect,
)
from codex_autorunner.core.ports.surface_port import (
    SurfaceHealthStatus as SurfaceHealthStatusDirect,
)
from codex_autorunner.core.ports.surface_port import (
    SurfacePort as SurfacePortDirect,
)
from codex_autorunner.core.ports.thread_store import (
    ThreadRecord as ThreadRecordDirect,
)
from codex_autorunner.core.ports.thread_store import (
    ThreadStatus as ThreadStatusDirect,
)
from codex_autorunner.core.ports.thread_store import (
    ThreadStore as ThreadStoreDirect,
)
from codex_autorunner.core.ports.ticket_store import (
    TicketRecord as TicketRecordDirect,
)
from codex_autorunner.core.ports.ticket_store import (
    TicketStatus as TicketStatusDirect,
)
from codex_autorunner.core.ports.ticket_store import (
    TicketStore as TicketStoreDirect,
)


class TestScopeResolverPort:
    def test_resolved_scope_construction(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        rs = ResolvedScope(scope=scope, display_name="my-repo")
        assert rs.scope == scope
        assert rs.display_name == "my-repo"
        assert rs.workspace_root is None
        assert rs.metadata == {}

    def test_scope_resolver_is_protocol(self) -> None:
        assert inspect.isclass(ScopeResolver)
        assert hasattr(ScopeResolver, "resolve")
        assert hasattr(ScopeResolver, "resolve_parent")
        assert hasattr(ScopeResolver, "resolve_children")

    def test_resolved_scope_reexport(self) -> None:
        assert ResolvedScope is ResolvedScopeDirect
        assert ScopeResolver is ScopeResolverDirect


class TestThreadStorePort:
    def test_thread_record_construction(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        agent = AgentRef(agent_id="a1")
        rec = ThreadRecord(thread_id="t1", scope=scope, agent=agent)
        assert rec.thread_id == "t1"
        assert rec.status == ThreadStatus.PENDING

    def test_thread_status_values(self) -> None:
        assert ThreadStatus.PENDING.value == "pending"
        assert ThreadStatus.ACTIVE.value == "active"
        assert ThreadStatus.COMPLETED.value == "completed"

    def test_thread_store_is_protocol(self) -> None:
        assert inspect.isclass(ThreadStore)
        assert hasattr(ThreadStore, "create")
        assert hasattr(ThreadStore, "get")
        assert hasattr(ThreadStore, "list_by_scope")
        assert hasattr(ThreadStore, "update_status")
        assert hasattr(ThreadStore, "delete")

    def test_reexports(self) -> None:
        assert ThreadRecord is ThreadRecordDirect
        assert ThreadStatus is ThreadStatusDirect
        assert ThreadStore is ThreadStoreDirect


class TestMemoryStorePort:
    def test_memory_doc_construction(self) -> None:
        doc = MemoryDoc(key="notes", content="hello")
        assert doc.key == "notes"
        assert doc.content_type == "text/plain"

    def test_memory_docs_construction(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        docs = MemoryDocs(scope=scope, docs=[])
        assert docs.scope == scope
        assert docs.docs == []

    def test_memory_store_is_protocol(self) -> None:
        assert inspect.isclass(MemoryStore)
        assert hasattr(MemoryStore, "load")
        assert hasattr(MemoryStore, "load_scope")
        assert hasattr(MemoryStore, "save")
        assert hasattr(MemoryStore, "delete")

    def test_reexports(self) -> None:
        assert MemoryDoc is MemoryDocDirect
        assert MemoryDocs is MemoryDocsDirect
        assert MemoryStore is MemoryStoreDirect


class TestTicketStorePort:
    def test_ticket_record_construction(self) -> None:
        scope = ScopeRef(kind="repo", id="r1")
        tref = TicketRef(scope=scope, ticket_id="TICKET-001")
        rec = TicketRecord(ref=tref, title="Do the thing")
        assert rec.ref.ticket_id == "TICKET-001"
        assert rec.status == TicketStatus.PENDING

    def test_ticket_status_values(self) -> None:
        assert TicketStatus.PENDING.value == "pending"
        assert TicketStatus.DONE.value == "done"
        assert TicketStatus.FAILED.value == "failed"

    def test_ticket_store_is_protocol(self) -> None:
        assert inspect.isclass(TicketStore)
        assert hasattr(TicketStore, "create")
        assert hasattr(TicketStore, "get")
        assert hasattr(TicketStore, "list_by_scope")
        assert hasattr(TicketStore, "update_status")
        assert hasattr(TicketStore, "delete")

    def test_reexports(self) -> None:
        assert TicketRecord is TicketRecordDirect
        assert TicketStatus is TicketStatusDirect
        assert TicketStore is TicketStoreDirect


class TestSurfacePort:
    def test_surface_capabilities_construction(self) -> None:
        surf = SurfaceRef(kind="telegram", key="ch1")
        cap = SurfaceCapabilities(surface=surf, supports_threads=True)
        assert cap.surface == surf
        assert cap.supports_threads is True

    def test_surface_health_construction(self) -> None:
        surf = SurfaceRef(kind="discord", key="guild1")
        h = SurfaceHealth(surface=surf, status=SurfaceHealthStatus.HEALTHY)
        assert h.status == SurfaceHealthStatus.HEALTHY

    def test_inbound_event_construction(self) -> None:
        surf = SurfaceRef(kind="telegram", key="ch1")
        evt = InboundEvent(
            surface=surf,
            event_id="e1",
            event_type="message",
            timestamp="2026-01-01T00:00:00Z",
        )
        assert evt.event_type == "message"

    def test_engine_command_construction(self) -> None:
        cmd = EngineCommand(command_type="send_message", payload={"text": "hi"})
        assert cmd.command_type == "send_message"

    def test_outbound_delivery_construction(self) -> None:
        surf = SurfaceRef(kind="telegram", key="ch1")
        d = OutboundDelivery(delivery_id="d1", surface=surf)
        assert d.status == "pending"

    def test_surface_port_is_protocol(self) -> None:
        assert inspect.isclass(SurfacePort)
        assert hasattr(SurfacePort, "capabilities")
        assert hasattr(SurfacePort, "health")
        assert hasattr(SurfacePort, "send")
        assert hasattr(SurfacePort, "receive")

    def test_reexports(self) -> None:
        assert EngineCommand is EngineCommandDirect
        assert InboundEvent is InboundEventDirect
        assert OutboundDelivery is OutboundDeliveryDirect
        assert SurfaceCapabilities is SurfaceCapabilitiesDirect
        assert SurfaceHealth is SurfaceHealthDirect
        assert SurfaceHealthStatus is SurfaceHealthStatusDirect
        assert SurfacePort is SurfacePortDirect


class TestPortDependencyBoundaries:
    def test_ports_import_only_domain_and_stdlib(self) -> None:
        from codex_autorunner.core.ports import (
            memory_store,
            scope_resolver,
            surface_port,
            thread_store,
            ticket_store,
        )

        for mod in [
            scope_resolver,
            thread_store,
            memory_store,
            ticket_store,
            surface_port,
        ]:
            for name, obj in inspect.getmembers(mod):
                if not isinstance(obj, type):
                    continue
                if obj.__module__ == "builtins":
                    continue
                src = obj.__module__
                assert (
                    src == mod.__name__
                    or src.startswith("codex_autorunner.core.domain")
                    or src.startswith("typing")
                    or src == "enum"
                    or src == "dataclasses"
                ), f"{mod.__name__}.{name} depends on {src}"

    def test_all_dataclasses_frozen(self) -> None:
        for cls in [
            ResolvedScope,
            ThreadRecord,
            MemoryDoc,
            MemoryDocs,
            TicketRecord,
            SurfaceCapabilities,
            SurfaceHealth,
            InboundEvent,
            EngineCommand,
            OutboundDelivery,
        ]:
            assert cls.__dataclass_params__.frozen, f"{cls.__name__} is not frozen"
