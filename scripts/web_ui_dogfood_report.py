#!/usr/bin/env python3
"""Build a ranked Web UI dogfood findings report from campaign artifacts."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_FAST_SUMMARY = Path(".codex-autorunner/diagnostics/web_ui_lab/latest.json")
DEFAULT_SCREEN_MANIFEST = Path(
    ".codex-autorunner/render/web_ui_samples/latest/manifest.json"
)
DEFAULT_SMOKE_MANIFEST = Path(
    ".codex-autorunner/render/web_ui_smoke/latest/manifest.json"
)
DEFAULT_OUT = Path(".codex-autorunner/diagnostics/web-ui-dogfood/latest.json")

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate Web UI fast, browser, and smoke diagnostics."
    )
    parser.add_argument("--fast-summary", type=Path, default=DEFAULT_FAST_SUMMARY)
    parser.add_argument("--screen-manifest", type=Path, default=DEFAULT_SCREEN_MANIFEST)
    parser.add_argument("--smoke-manifest", type=Path, default=DEFAULT_SMOKE_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        fast_summary_path=args.fast_summary,
        screen_manifest_path=args.screen_manifest,
        smoke_manifest_path=args.smoke_manifest,
        out_path=args.out,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Web UI dogfood report: {args.out}")
    print(
        f"Findings: {report['finding_count']} "
        f"({report['high_or_medium_count']} high/medium)"
    )
    return 0


def build_report(
    *,
    fast_summary_path: Path,
    screen_manifest_path: Path,
    smoke_manifest_path: Path,
    out_path: Path,
) -> dict[str, Any]:
    fast_summary = _read_json_if_exists(fast_summary_path)
    screen_manifest = _read_json_if_exists(screen_manifest_path)
    smoke_manifest = _read_json_if_exists(smoke_manifest_path)
    findings = _fast_findings(fast_summary, fast_summary_path)
    findings.extend(_screen_findings(screen_manifest, screen_manifest_path))
    findings.extend(_smoke_findings(smoke_manifest, smoke_manifest_path))
    findings = sorted(
        _dedupe_findings(findings),
        key=lambda item: (
            SEVERITY_RANK.get(str(item.get("severity")), 9),
            str(item.get("defect_class")),
            str(item.get("id")),
        ),
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        grouped[str(finding["defect_class"])].append(finding)
    command_statuses = {
        "web-ui-fast": _status(fast_summary),
        "web-ui-screens": _status(screen_manifest),
        "web-ui-smoke": _status(smoke_manifest),
    }
    return {
        "schema_version": 1,
        "kind": "web_ui_dogfood_campaign",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if findings else "passed",
        "diagnostics_path": str(out_path),
        "inputs": {
            "fast_summary": str(fast_summary_path),
            "screen_manifest": str(screen_manifest_path),
            "smoke_manifest": str(smoke_manifest_path),
        },
        "command_statuses": command_statuses,
        "finding_count": len(findings),
        "high_or_medium_count": sum(
            1 for item in findings if item.get("severity") in {"high", "medium"}
        ),
        "findings": findings,
        "groups": dict(sorted(grouped.items())),
        "non_actionable": _non_actionable_notes(screen_manifest),
    }


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _status(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "not_run"
    return str(payload.get("status", "unknown"))


def _fast_findings(
    summary: dict[str, Any] | None, summary_path: Path
) -> list[dict[str, Any]]:
    if summary is None:
        return [
            _finding(
                finding_id="fast-summary-missing",
                defect_class="data contract",
                severity="medium",
                title="Fast Web UI scenario summary is missing",
                scenario_id=None,
                route=None,
                viewport=None,
                failed_invariants=["fast_summary_missing"],
                evidence=[str(summary_path)],
                rerun="make web-ui-fast",
                reproduction_steps=[
                    "Run `make web-ui-fast` from the repo root.",
                    f"Inspect `{summary_path}`.",
                ],
            )
        ]
    return [
        _finding(
            finding_id=f"fast-{failure.get('scenario_id', 'unknown')}",
            defect_class="data contract",
            severity="high",
            title="Fast Web UI scenario invariant failed",
            scenario_id=failure.get("scenario_id"),
            route=failure.get("route_path"),
            viewport=None,
            failed_invariants=[
                item.get("id", "unknown")
                for item in failure.get("failed_invariants", [])
            ],
            evidence=[failure.get("report_path"), failure.get("fixture_payload_path")],
            rerun="make web-ui-fast",
            reproduction_steps=[
                "Run `make web-ui-fast` from the repo root.",
                f"Inspect `{failure.get('inspect_first') or summary_path}`.",
            ],
        )
        for failure in summary.get("failures", [])
    ]


def _screen_findings(
    manifest: dict[str, Any] | None, manifest_path: Path
) -> list[dict[str, Any]]:
    if manifest is None:
        return [
            _finding(
                finding_id="screen-manifest-missing",
                defect_class="loading/empty state",
                severity="medium",
                title="Browser evidence pack manifest is missing",
                scenario_id=None,
                route=None,
                viewport=None,
                failed_invariants=["screen_manifest_missing"],
                evidence=[str(manifest_path)],
                rerun="make web-ui-screens",
                reproduction_steps=[
                    "Run `make web-ui-screens` from the repo root.",
                    f"Inspect `{manifest_path}`.",
                ],
            )
        ]

    findings: list[dict[str, Any]] = []
    out_dir = Path(str(manifest.get("out_dir") or manifest_path.parent))
    for capture in manifest.get("captures", []):
        subsystems = capture.get("failure_subsystems") or []
        for subsystem in subsystems:
            finding = _capture_finding(capture, subsystem, out_dir, manifest_path)
            if finding is not None:
                findings.append(finding)
    return findings


def _capture_finding(
    capture: dict[str, Any],
    subsystem: str,
    out_dir: Path,
    manifest_path: Path,
) -> dict[str, Any] | None:
    route = capture.get("path")
    viewport = _viewport_label(capture.get("viewport", {}))
    evidence = _capture_evidence(capture, out_dir, manifest_path)
    rerun = (
        "make web-ui-screens "
        f"WEB_UI_SCREEN_OUT={out_dir} "
        f"WEB_UI_SCREEN_VIEWPORT={viewport} "
        f"WEB_UI_SCREEN_ARGS='--route {capture.get('name')}={route}'"
    )
    if subsystem == "console":
        errors = capture.get("console", {}).get("errors", [])
        if _only_optional_voice_config_noise(errors):
            return None
        return _finding(
            finding_id=f"console-{capture.get('name')}-{viewport}",
            defect_class="console/network noise",
            severity="medium",
            title="Route logs browser console errors",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["console_errors"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[
                f"Run `{rerun}`.",
                "Open the route and inspect browser console errors in the manifest.",
            ],
            details={"console_errors": errors},
        )
    if subsystem == "loading":
        return _finding(
            finding_id=f"loading-{capture.get('name')}-{viewport}",
            defect_class="loading/empty state",
            severity="high",
            title="Route renders a loading or error state after settling",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["loading_or_error_state"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[
                f"Run `{rerun}`.",
                "Inspect the screenshot and DOM summary for persistent loading or error text.",
            ],
            details={"loading_marker": capture.get("loading_marker")},
        )
    if subsystem == "navigation":
        return _finding(
            finding_id=f"navigation-{capture.get('name')}-{viewport}",
            defect_class="navigation",
            severity="high",
            title="Route navigation failed",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["navigation_failed"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[f"Run `{rerun}`.", "Inspect navigation status."],
            details={"navigation": capture.get("navigation")},
        )
    if subsystem == "layout":
        return _finding(
            finding_id=f"layout-{capture.get('name')}-{viewport}",
            defect_class="layout",
            severity="medium",
            title="Route has horizontal overflow",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["horizontal_overflow"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[
                f"Run `{rerun}`.",
                "Inspect layout diagnostics for overflowing elements.",
            ],
            details={"layout": capture.get("layout")},
        )
    if subsystem == "network":
        return _finding(
            finding_id=f"network-{capture.get('name')}-{viewport}",
            defect_class="console/network noise",
            severity="medium",
            title="Route records failed network requests",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["failed_network_requests"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[f"Run `{rerun}`.", "Inspect failed network requests."],
            details={"network": capture.get("network")},
        )
    if subsystem == "accessibility capture":
        return _finding(
            finding_id=f"a11y-capture-{capture.get('name')}-{viewport}",
            defect_class="accessibility",
            severity="low",
            title="Accessibility snapshot capture failed",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["accessibility_snapshot_capture_failed"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[f"Run `{rerun}`.", "Inspect accessibility error."],
            details={"accessibility": capture.get("accessibility")},
        )
    if subsystem == "screenshot":
        return _finding(
            finding_id=f"screenshot-{capture.get('name')}-{viewport}",
            defect_class="loading/empty state",
            severity="high",
            title="Screenshot artifact was not produced",
            scenario_id=capture.get("name"),
            route=route,
            viewport=viewport,
            failed_invariants=["screenshot_missing"],
            evidence=evidence,
            rerun=rerun,
            reproduction_steps=[f"Run `{rerun}`.", "Inspect screenshot artifact path."],
            details={"screenshot": capture.get("screenshot")},
        )
    return None


def _smoke_findings(
    manifest: dict[str, Any] | None, manifest_path: Path
) -> list[dict[str, Any]]:
    if manifest is None:
        return [
            _finding(
                finding_id="smoke-manifest-missing",
                defect_class="interaction",
                severity="low",
                title="Smoke journey manifest is missing",
                scenario_id=None,
                route=None,
                viewport="1440x960",
                failed_invariants=["smoke_manifest_missing"],
                evidence=[str(manifest_path)],
                rerun="make web-ui-smoke",
                reproduction_steps=[
                    "Run `make web-ui-smoke` from the repo root.",
                    f"Inspect `{manifest_path}`.",
                ],
            )
        ]
    if manifest.get("status") == "passed":
        return []
    return [
        _finding(
            finding_id="smoke-journey-failed",
            defect_class="interaction",
            severity="high",
            title="Critical Playwright smoke journey failed",
            scenario_id="web-ui-smoke",
            route=None,
            viewport="1440x960",
            failed_invariants=["smoke_journey_failed"],
            evidence=[
                str(manifest_path),
                *[
                    value
                    for value in (manifest.get("artifacts") or {}).values()
                    if isinstance(value, str) and value
                ],
            ],
            rerun="make web-ui-smoke",
            reproduction_steps=[
                "Run `make web-ui-smoke` from the repo root.",
                "Inspect the smoke manifest, trace, screenshot, and video artifacts.",
            ],
            details={"failures": manifest.get("failures", [])},
        )
    ]


def _finding(
    *,
    finding_id: str,
    defect_class: str,
    severity: str,
    title: str,
    scenario_id: str | None,
    route: str | None,
    viewport: str | None,
    failed_invariants: list[Any],
    evidence: list[Any],
    rerun: str,
    reproduction_steps: list[str],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "id": finding_id,
        "defect_class": defect_class,
        "severity": severity,
        "title": title,
        "scenario_id": scenario_id,
        "route": route,
        "viewport": viewport,
        "failed_invariants": [str(item) for item in failed_invariants if item],
        "evidence_artifacts": [str(item) for item in evidence if item],
        "rerun_command": rerun,
        "reproduction_steps": reproduction_steps,
    }
    if details:
        result["details"] = details
    return result


def _capture_evidence(
    capture: dict[str, Any], out_dir: Path, manifest_path: Path
) -> list[str]:
    evidence = [str(manifest_path)]
    for key in ("screenshot", "dom_summary", "accessibility", "layout"):
        value = capture.get(key)
        if not isinstance(value, dict):
            continue
        path = value.get("path")
        if path:
            evidence.append(str(path))
            continue
        relative = value.get("relative_path")
        if relative:
            evidence.append(str(out_dir / str(relative)))
    return evidence


def _viewport_label(viewport: dict[str, Any]) -> str:
    return f"{viewport.get('width')}x{viewport.get('height')}"


def _only_optional_voice_config_noise(errors: list[dict[str, Any]]) -> bool:
    if not errors:
        return False
    for error in errors:
        location = error.get("location") or {}
        if "/api/voice/config" not in str(location.get("url", "")):
            return False
    return True


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in findings:
        key = (
            finding.get("defect_class"),
            finding.get("scenario_id"),
            finding.get("route"),
            finding.get("viewport"),
            tuple(finding.get("failed_invariants", [])),
        )
        deduped.setdefault(key, finding)
    return list(deduped.values())


def _non_actionable_notes(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    if manifest is None:
        return []
    notes: list[dict[str, Any]] = []
    diagnostics = manifest.get("diagnostics") or {}
    aborted = [
        request
        for request in diagnostics.get("failed_requests", [])
        if request.get("failure") == "net::ERR_ABORTED"
    ]
    if aborted:
        notes.append(
            {
                "id": "aborted-eventsource-during-navigation",
                "rationale": (
                    "The browser evidence harness treats aborted EventSource "
                    "requests during route changes as non-actionable navigation "
                    "cleanup noise."
                ),
                "count": len(aborted),
            }
        )
    voice_config = [
        error
        for error in diagnostics.get("console_errors", [])
        if "/api/voice/config" in str((error.get("location") or {}).get("url", ""))
    ]
    if voice_config:
        notes.append(
            {
                "id": "optional-voice-config-404",
                "rationale": (
                    "The optional voice config endpoint is allowed to be absent "
                    "in fixture hubs and is not counted as a route defect."
                ),
                "count": len(voice_config),
            }
        )
    return notes


if __name__ == "__main__":
    raise SystemExit(main())
