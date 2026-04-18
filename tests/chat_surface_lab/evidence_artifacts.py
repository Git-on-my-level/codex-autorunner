from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from codex_autorunner.browser.models import DEFAULT_VIEWPORT, Viewport
from codex_autorunner.browser.runtime import BrowserRuntime

from .artifact_manifests import ArtifactKind, ArtifactManifest, ArtifactRecord
from .transcript_models import TranscriptTimeline
from .transcript_renderer import render_transcript_html, transcript_event_payload


def write_surface_evidence_artifacts(
    *,
    output_dir: Path,
    scenario_id: str,
    run_id: str,
    surface_kind: str,
    surface_timeline: Sequence[Mapping[str, Any]],
    transcript: TranscriptTimeline,
    log_records: Sequence[Mapping[str, Any]],
    browser_runtime: Optional[BrowserRuntime] = None,
    viewport: Viewport = DEFAULT_VIEWPORT,
) -> ArtifactManifest:
    """Write a complete, diffable artifact bundle for one surface run."""
    output_dir.mkdir(parents=True, exist_ok=True)

    timeline_payload = _timeline_payload(
        scenario_id=scenario_id,
        run_id=run_id,
        surface_kind=surface_kind,
        events=surface_timeline,
    )
    transcript_payload = _transcript_payload(
        scenario_id=scenario_id,
        run_id=run_id,
        surface_kind=surface_kind,
        transcript=transcript,
    )
    rendered_html = render_transcript_html(transcript)
    logs_payload = _structured_logs_payload(log_records=log_records)
    timing_payload = _timing_report_payload(log_records=log_records)

    timeline_path = output_dir / "timeline.json"
    transcript_path = output_dir / "transcript.json"
    html_path = output_dir / "transcript.html"
    logs_path = output_dir / "logs.json"
    timing_report_path = output_dir / "timing_report.json"
    a11y_snapshot_path = output_dir / "a11y_snapshot.json"
    manifest_path = output_dir / "manifest.json"

    _write_json(path=timeline_path, payload=timeline_payload)
    _write_json(path=transcript_path, payload=transcript_payload)
    html_path.write_text(rendered_html, encoding="utf-8")
    _write_json(path=logs_path, payload=logs_payload)
    _write_json(path=timing_report_path, payload=timing_payload)

    active_browser_runtime = browser_runtime or BrowserRuntime()
    capture_status: dict[str, Any] = {}
    records: list[ArtifactRecord] = [
        ArtifactRecord(
            kind=ArtifactKind.SURFACE_TIMELINE_JSON,
            path=timeline_path,
            description=f"{surface_kind} simulator operation timeline",
            content_type="application/json",
        ),
        ArtifactRecord(
            kind=ArtifactKind.NORMALIZED_TRANSCRIPT_JSON,
            path=transcript_path,
            description=f"{surface_kind} normalized transcript schema",
            content_type="application/json",
        ),
        ArtifactRecord(
            kind=ArtifactKind.RENDERED_HTML,
            path=html_path,
            description="Deterministic rendered transcript view",
            content_type="text/html",
        ),
        ArtifactRecord(
            kind=ArtifactKind.STRUCTURED_LOG_JSON,
            path=logs_path,
            description="Structured log records captured during scenario execution",
            content_type="application/json",
        ),
        ArtifactRecord(
            kind=ArtifactKind.TIMING_REPORT_JSON,
            path=timing_report_path,
            description="Timing summary derived from chat UX telemetry fields",
            content_type="application/json",
        ),
    ]

    transcript_url = html_path.resolve().as_uri()
    screenshot_result = active_browser_runtime.capture_screenshot(
        base_url=transcript_url,
        out_dir=output_dir,
        viewport=viewport,
        output_name="screenshot.png",
    )
    capture_status["screenshot"] = _capture_status_payload(screenshot_result)
    screenshot_path = screenshot_result.artifacts.get("capture")
    if (
        screenshot_result.ok
        and screenshot_path is not None
        and screenshot_path.exists()
        and screenshot_path.name == "screenshot.png"
    ):
        records.append(
            ArtifactRecord(
                kind=ArtifactKind.SCREENSHOT_PNG,
                path=screenshot_path,
                description="Browser runtime screenshot of transcript.html",
                content_type="image/png",
            )
        )

    observe_tmp_dir = output_dir / ".browser-observe-tmp"
    observe_result = active_browser_runtime.capture_observe(
        base_url=transcript_url,
        out_dir=observe_tmp_dir,
        viewport=viewport,
        include_html=False,
    )
    capture_status["observe"] = _capture_status_payload(observe_result)
    observe_snapshot_path = observe_result.artifacts.get("snapshot")
    if (
        observe_result.ok
        and observe_snapshot_path is not None
        and observe_snapshot_path.exists()
    ):
        a11y_snapshot_path.write_text(
            observe_snapshot_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        records.append(
            ArtifactRecord(
                kind=ArtifactKind.ACCESSIBILITY_SNAPSHOT_JSON,
                path=a11y_snapshot_path,
                description="Accessibility snapshot captured from rendered transcript",
                content_type="application/json",
            )
        )
    shutil.rmtree(observe_tmp_dir, ignore_errors=True)

    manifest_payload = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "run_id": run_id,
        "surface_kind": surface_kind,
        "generated_at_epoch_ms": int(time.time() * 1000),
        "artifacts": [
            {
                "kind": record.kind.value,
                "path": record.path.name,
                "description": record.description,
                "content_type": record.content_type,
            }
            for record in records
        ],
        "capture_status": capture_status,
    }
    _write_json(path=manifest_path, payload=manifest_payload)
    records.append(
        ArtifactRecord(
            kind=ArtifactKind.ARTIFACT_MANIFEST_JSON,
            path=manifest_path,
            description="Index of all emitted scenario evidence artifacts",
            content_type="application/json",
        )
    )

    return ArtifactManifest(
        scenario_id=scenario_id,
        run_id=run_id,
        artifacts=tuple(records),
        metadata={
            "surface_kind": surface_kind,
            "capture_status": capture_status,
        },
    )


def _timeline_payload(
    *,
    scenario_id: str,
    run_id: str,
    surface_kind: str,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized = [dict(event) for event in events]
    timestamps = [
        int(event.get("timestamp_ms"))
        for event in normalized
        if isinstance(event.get("timestamp_ms"), int)
    ]
    start_ts = min(timestamps) if timestamps else 0
    end_ts = max(timestamps) if timestamps else 0
    event_payload = []
    for index, event in enumerate(normalized, start=1):
        timestamp_ms = event.get("timestamp_ms")
        normalized_ts = int(timestamp_ms) if isinstance(timestamp_ms, int) else 0
        event_payload.append(
            {
                "index": index,
                "timestamp_ms": normalized_ts,
                "delta_ms": max(normalized_ts - start_ts, 0),
                "kind": str(event.get("kind") or "status"),
                "party": str(event.get("party") or "platform"),
                "text": str(event.get("text") or ""),
                "metadata": (
                    dict(event.get("metadata"))
                    if isinstance(event.get("metadata"), dict)
                    else {}
                ),
            }
        )
    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "run_id": run_id,
        "surface_kind": surface_kind,
        "event_count": len(event_payload),
        "start_timestamp_ms": start_ts,
        "end_timestamp_ms": end_ts,
        "duration_ms": max(end_ts - start_ts, 0) if event_payload else 0,
        "events": event_payload,
    }


def _transcript_payload(
    *,
    scenario_id: str,
    run_id: str,
    surface_kind: str,
    transcript: TranscriptTimeline,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "run_id": run_id,
        "surface_kind": surface_kind,
        "metadata": dict(transcript.metadata),
        "event_count": len(transcript.events),
        "events": transcript_event_payload(transcript),
    }


def _structured_logs_payload(
    *,
    log_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_count": len(log_records),
        "records": [dict(record) for record in log_records],
    }


def _timing_report_payload(
    *,
    log_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metrics: dict[str, list[float]] = {}
    event_counts: dict[str, int] = {}
    for record in log_records:
        event_name = record.get("event")
        if isinstance(event_name, str) and event_name.strip():
            event_counts[event_name] = event_counts.get(event_name, 0) + 1
        for key, value in record.items():
            if not key.startswith("chat_ux_delta_"):
                continue
            if not isinstance(value, (int, float)):
                continue
            metrics.setdefault(key, []).append(float(value))

    metric_payload = {
        key: {
            "count": len(values),
            "min_ms": round(min(values), 1),
            "max_ms": round(max(values), 1),
            "avg_ms": round(sum(values) / len(values), 1),
        }
        for key, values in sorted(metrics.items())
        if values
    }
    return {
        "schema_version": 1,
        "record_count": len(log_records),
        "timing_metrics": metric_payload,
        "event_counts": dict(sorted(event_counts.items())),
    }


def _capture_status_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": bool(getattr(result, "ok", False)),
        "error_type": getattr(result, "error_type", None),
        "error_message": getattr(result, "error_message", None),
    }


def _write_json(*, path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
