from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SeedFixtureKind(str, Enum):
    """Fixture-backed Web Hub states consumed by route/read-model tests."""

    EMPTY_HUB = "empty_hub"
    SEEDED_REPO_WORKTREE_TICKET = "seeded_repo_worktree_ticket"
    CHAT_LIST_DETAIL = "chat_list_detail"
    LARGE_LIST_WINDOWING = "large_list_windowing"
    PMA_PENDING = "pma_pending"
    PMA_RUNNING = "pma_running"
    PMA_FINAL = "pma_final"
    PMA_NEW_CHAT = "pma_new_chat"
    PMA_QUEUED = "pma_queued"
    PMA_ERROR = "pma_error"
    PMA_APPROVAL = "pma_approval"
    PMA_INTERRUPT = "pma_interrupt"
    PMA_ATTACHMENT = "pma_attachment"
    PMA_DUPLICATE_REPAIR = "pma_duplicate_repair"


class ViewportName(str, Enum):
    """Stable viewport labels shared with browser evidence filenames."""

    DESKTOP = "desktop"
    MOBILE = "mobile"


class LoadingBehavior(str, Enum):
    """Expected behavior for primary loading markers."""

    CLEARS = "clears"
    MAY_STREAM = "may_stream"
    NOT_RENDERED = "not_rendered"


class ScenarioTag(str, Enum):
    """Machine-readable grouping tags for selecting validation layers."""

    FAST = "fast"
    BROWSER = "browser"
    CRITICAL = "critical"
    LARGE_STATE = "large-state"


@dataclass(frozen=True)
class Viewport:
    name: ViewportName
    width: int
    height: int

    @property
    def artifact_label(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True)
class ExpectedScreenBehavior:
    """Route-level visible and loading-state expectations."""

    visible_landmarks: tuple[str, ...]
    loading_behavior: LoadingBehavior = LoadingBehavior.CLEARS
    loading_markers: tuple[str, ...] = ()
    empty_state_landmarks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpectedReadModelFixture:
    """Backend/API fixtures that should back a screen scenario."""

    fixture_kind: SeedFixtureKind
    read_model_routes: tuple[str, ...] = ()
    api_routes: tuple[str, ...] = ()
    frontend_mappers: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceArtifactSet:
    """Stable artifact names a future browser runner should emit."""

    screenshot: str
    accessibility_snapshot: str
    dom_summary: str
    console_log: str
    network_failures: str
    layout_diagnostics: str


@dataclass(frozen=True)
class WebUiScenario:
    """Durable contract for one Web Hub UI regression scenario."""

    scenario_id: str
    title: str
    route_name: str
    route_path: str
    viewports: tuple[Viewport, ...]
    seed_fixture: SeedFixtureKind
    screen: ExpectedScreenBehavior
    read_models: ExpectedReadModelFixture
    evidence: EvidenceArtifactSet
    tags: tuple[ScenarioTag, ...] = field(default_factory=tuple)
    notes: str = ""
