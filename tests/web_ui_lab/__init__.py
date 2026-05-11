"""Importable contracts for Web Hub UI regression scenarios."""

from tests.web_ui_lab.corpus import WEB_UI_SCENARIOS, iter_scenarios
from tests.web_ui_lab.scenario_models import (
    EvidenceArtifactSet,
    ExpectedReadModelFixture,
    ExpectedScreenBehavior,
    SeedFixtureKind,
    Viewport,
    ViewportName,
    WebUiScenario,
)

__all__ = [
    "EvidenceArtifactSet",
    "ExpectedReadModelFixture",
    "ExpectedScreenBehavior",
    "SeedFixtureKind",
    "Viewport",
    "ViewportName",
    "WEB_UI_SCENARIOS",
    "WebUiScenario",
    "iter_scenarios",
]
