"""Shared contracts for the chat-surface lab test package."""

from .artifact_manifests import ArtifactKind, ArtifactManifest, ArtifactRecord
from .backend_runtime import (
    ACPFixtureRuntime,
    BackendRuntimeCapabilities,
    BackendRuntimeEvent,
    CodexAppServerFixtureRuntime,
    HermesFixtureRuntime,
    OpenCodeFixtureRuntime,
    app_server_fixture_command,
    fake_acp_command,
    fake_opencode_server_command,
)
from .discord_simulator import DiscordSimulatorFaults, DiscordSurfaceSimulator
from .scenario_models import (
    ArtifactExpectation,
    BudgetExpectation,
    ChatSurfaceScenario,
    FaultInjection,
    RuntimeFixtureKind,
    ScenarioAction,
    SurfaceKind,
    TerminalExpectation,
)
from .telegram_simulator import TelegramSimulatorFaults, TelegramSurfaceSimulator
from .transcript_models import (
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptParty,
    TranscriptTimeline,
)

__all__ = [
    "ArtifactExpectation",
    "ArtifactKind",
    "ArtifactManifest",
    "ArtifactRecord",
    "ACPFixtureRuntime",
    "BackendRuntimeCapabilities",
    "BackendRuntimeEvent",
    "BudgetExpectation",
    "ChatSurfaceScenario",
    "CodexAppServerFixtureRuntime",
    "DiscordSimulatorFaults",
    "DiscordSurfaceSimulator",
    "FaultInjection",
    "HermesFixtureRuntime",
    "OpenCodeFixtureRuntime",
    "RuntimeFixtureKind",
    "ScenarioAction",
    "SurfaceKind",
    "TerminalExpectation",
    "TelegramSimulatorFaults",
    "TelegramSurfaceSimulator",
    "TranscriptEvent",
    "TranscriptEventKind",
    "TranscriptParty",
    "TranscriptTimeline",
    "app_server_fixture_command",
    "fake_acp_command",
    "fake_opencode_server_command",
]
