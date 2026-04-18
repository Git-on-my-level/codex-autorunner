from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ArtifactKind(str, Enum):
    """Stable artifact identities for chat-surface lab evidence bundles."""

    ACCESSIBILITY_SNAPSHOT_JSON = "accessibility_snapshot_json"
    ARTIFACT_MANIFEST_JSON = "artifact_manifest_json"
    NORMALIZED_TRANSCRIPT_JSON = "normalized_transcript_json"
    RENDERED_HTML = "rendered_html"
    SCREENSHOT_PNG = "screenshot_png"
    STRUCTURED_LOG_JSON = "structured_log_json"
    SURFACE_TIMELINE_JSON = "surface_timeline_json"
    TIMING_REPORT_JSON = "timing_report_json"


@dataclass(frozen=True)
class ArtifactRecord:
    """One file emitted as part of a scenario artifact bundle."""

    kind: ArtifactKind
    path: Path
    description: str = ""
    content_type: str = ""


@dataclass(frozen=True)
class ArtifactManifest:
    """Artifact inventory for one scenario run."""

    scenario_id: str
    run_id: str
    artifacts: tuple[ArtifactRecord, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
