from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Request

from .....agents.hermes.supervisor import build_hermes_supervisor_from_config
from .....bootstrap import ensure_pma_docs
from .....core.orchestration import OrchestrationBindingStore
from .....core.orchestration.cold_trace_store import ColdTraceStore
from .....core.pma_context import build_hub_snapshot
from .....core.pma_thread_store import PmaThreadStore
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.utils import atomic_write
from .....integrations.github.context_injection import maybe_inject_github_context
from .common import pma_config_from_raw


@dataclass
class PmaRoutePorts:
    build_hub_snapshot: Any = build_hub_snapshot
    maybe_inject_github_context: Any = maybe_inject_github_context
    ensure_pma_docs: Callable[[Path], None] = ensure_pma_docs
    atomic_write: Callable[[Path, str], None] = atomic_write
    build_hermes_supervisor: Any = build_hermes_supervisor_from_config


@dataclass
class PmaApplicationContainer:
    runtime_state: Any
    host_state: Any = None
    ports: PmaRoutePorts = field(default_factory=PmaRoutePorts)
    hermes_supervisors_by_profile: dict[str, Any] = field(default_factory=dict)
    managed_thread_harness_cache: dict[tuple[str, str], Any] = field(
        default_factory=dict
    )
    managed_thread_tasks: set[Any] = field(default_factory=set)
    managed_thread_queue_tasks: dict[str, Any] = field(default_factory=dict)

    def bind_host_state(self, host_state: Any) -> PmaApplicationContainer:
        self.host_state = host_state
        return self

    @property
    def config(self) -> Any:
        if self.host_state is None:
            raise RuntimeError("PMA application container is not bound to app state")
        return getattr(self.host_state, "config", None)

    @property
    def hub_root(self) -> Path:
        config = self.config
        hub_root = getattr(config, "root", None)
        if isinstance(hub_root, Path):
            return hub_root
        if isinstance(hub_root, str) and hub_root.strip():
            return Path(hub_root)
        raise RuntimeError("PMA application container is missing config.root")

    @property
    def raw_config(self) -> dict[str, Any]:
        raw = getattr(self.config, "raw", {})
        return raw if isinstance(raw, dict) else {}

    @property
    def pma_config(self) -> dict[str, Any]:
        return pma_config_from_raw(self.raw_config)

    @property
    def agent_context(self) -> Any:
        return self.host_state

    def get_host_attr(self, name: str, default: Any = None) -> Any:
        if self.host_state is None:
            return default
        return getattr(self.host_state, name, default)

    def request_context(self, request: Request) -> PmaRequestContext:
        self.bind_host_state(request.app.state)
        return PmaRequestContext(request=request, container=self)


@dataclass(frozen=True)
class PmaRequestContext:
    request: Request
    container: PmaApplicationContainer

    @property
    def app(self) -> Any:
        return getattr(self.request, "app", None)

    @property
    def config(self) -> Any:
        return self.container.config

    @property
    def hub_root(self) -> Path:
        return self.container.hub_root

    @property
    def raw_config(self) -> dict[str, Any]:
        return self.container.raw_config

    @property
    def pma_config(self) -> dict[str, Any]:
        return self.container.pma_config

    @property
    def agent_context(self) -> Any:
        return self.container.agent_context

    @property
    def ports(self) -> PmaRoutePorts:
        return self.container.ports

    @property
    def runtime_state(self) -> Any:
        return self.container.runtime_state

    @property
    def root_path(self) -> str:
        scope = getattr(self.request, "scope", {}) or {}
        root_path = scope.get("root_path", "")
        return root_path if isinstance(root_path, str) else ""

    @property
    def hub_supervisor(self) -> Any:
        return self.container.get_host_attr("hub_supervisor")

    @property
    def hermes_supervisor(self) -> Any:
        return self.container.get_host_attr("hermes_supervisor")

    @property
    def app_server_supervisor(self) -> Any:
        return self.container.get_host_attr("app_server_supervisor")

    @property
    def app_server_threads(self) -> Any:
        return self.container.get_host_attr("app_server_threads")

    @property
    def app_server_events(self) -> Any:
        return self.container.get_host_attr("app_server_events")

    @property
    def opencode_supervisor(self) -> Any:
        return self.container.get_host_attr("opencode_supervisor")

    @property
    def runtime_services(self) -> Any:
        return self.container.get_host_attr("runtime_services")

    @property
    def hub_client(self) -> Any:
        return self.container.get_host_attr("hub_client")

    @property
    def zeroclaw_supervisor(self) -> Any:
        return self.container.get_host_attr("zeroclaw_supervisor")

    @property
    def hermes_supervisors_by_profile(self) -> dict[str, Any]:
        return self.container.hermes_supervisors_by_profile

    @property
    def managed_thread_harness_cache(self) -> dict[tuple[str, str], Any]:
        return self.container.managed_thread_harness_cache

    @property
    def managed_thread_tasks(self) -> set[Any]:
        return self.container.managed_thread_tasks

    @property
    def managed_thread_queue_tasks(self) -> dict[str, Any]:
        return self.container.managed_thread_queue_tasks

    def thread_store(self) -> PmaThreadStore:
        return PmaThreadStore(self.hub_root)

    def transcript_store(self) -> PmaTranscriptStore:
        return PmaTranscriptStore(self.hub_root)

    def cold_trace_store(self) -> ColdTraceStore:
        return ColdTraceStore(self.hub_root)

    def orchestration_binding_store(self) -> OrchestrationBindingStore:
        return OrchestrationBindingStore(self.hub_root)

    def state_store(self) -> Any:
        return self.runtime_state.get_state_store(self.hub_root)

    def pma_queue(self) -> Any:
        return self.runtime_state.get_pma_queue(self.hub_root)

    def safety_checker(self) -> Any:
        return self.runtime_state.get_safety_checker(self.hub_root, self)


def create_pma_application_container(
    *,
    runtime_state: Any,
    host_state: Any = None,
    ports: Optional[PmaRoutePorts] = None,
) -> PmaApplicationContainer:
    return PmaApplicationContainer(
        runtime_state=runtime_state,
        host_state=host_state,
        ports=ports or PmaRoutePorts(),
    )


def get_pma_request_context(request: Request) -> PmaRequestContext:
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    container = getattr(state, "pma_container", None)
    if not isinstance(container, PmaApplicationContainer):
        container = create_pma_application_container(
            runtime_state=getattr(state, "pma_runtime_state", None),
            host_state=state,
        )
        if state is not None:
            state.pma_container = container
    return container.request_context(request)


__all__ = [
    "PmaApplicationContainer",
    "PmaRequestContext",
    "PmaRoutePorts",
    "create_pma_application_container",
    "get_pma_request_context",
]
