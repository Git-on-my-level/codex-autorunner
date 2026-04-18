from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SurfaceKind(str, Enum):
    """Supported chat surfaces for shared lab scenarios."""

    DISCORD = "discord"
    TELEGRAM = "telegram"


class RuntimeFixtureKind(str, Enum):
    """Backend fixture families the lab will normalize behind one seam."""

    ACP = "acp"
    APP_SERVER = "app_server"
    HERMES = "hermes"
    OPENCODE = "opencode"


@dataclass(frozen=True)
class ScenarioAction:
    """One inbound action or step in a declarative chat-surface scenario."""

    kind: str
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    delay_ms: int = 0


@dataclass(frozen=True)
class FaultInjection:
    """A deterministic perturbation applied while running a scenario."""

    kind: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetExpectation:
    """Named latency or delivery budget asserted by a scenario."""

    metric: str
    max_ms: int
    description: str = ""


@dataclass(frozen=True)
class ArtifactExpectation:
    """Artifact kinds a scenario expects to be emitted by the lab."""

    required_kinds: tuple[str, ...] = ()
    optional_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminalExpectation:
    """Expected terminal outcome for a scenario run."""

    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatSurfaceScenario:
    """Normalized scenario contract shared across chat surfaces."""

    scenario_id: str
    surfaces: tuple[SurfaceKind, ...]
    runtime_fixture: RuntimeFixtureKind
    runtime_scenario: str
    actions: tuple[ScenarioAction, ...] = ()
    faults: tuple[FaultInjection, ...] = ()
    budgets: tuple[BudgetExpectation, ...] = ()
    artifacts: ArtifactExpectation = field(default_factory=ArtifactExpectation)
    expected_terminal: TerminalExpectation = field(
        default_factory=lambda: TerminalExpectation(status="completed")
    )
    tags: tuple[str, ...] = ()
    notes: str = ""
