from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from codex_autorunner.agents.acp import ACPClient
from codex_autorunner.agents.acp.errors import ACPError, ACPMissingSessionError
from codex_autorunner.agents.hermes.supervisor import (
    build_hermes_supervisor_from_config,
    hermes_runtime_preflight,
)
from codex_autorunner.core.config import load_repo_config
from tests.chat_surface_lab.backend_runtime import fake_acp_command

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIR = Path(".codex-autorunner") / "diagnostics" / "acp-conformance"
DEFAULT_COMPLETION_PROMPT = (
    "Return only the token CONFORMANCE_OK. Do not add punctuation or extra words."
)


@dataclass(frozen=True)
class ACPConformanceTarget:
    id: str
    label: str
    command: tuple[str, ...]
    source: Literal["fixture", "repo_config"]
    env: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    scenario: Optional[str] = None
    agent_id: Optional[str] = None
    profile: Optional[str] = None


@dataclass(frozen=True)
class ACPCaseResult:
    case_id: str
    passed: bool
    status: str
    elapsed_ms: Optional[int]
    details: dict[str, Any] = field(default_factory=dict)
    error_type: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ACPTargetResult:
    target: ACPConformanceTarget
    passed: bool
    status: str
    summary: str
    cases: tuple[ACPCaseResult, ...]
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class ACPConformanceReport:
    run_id: str
    repo_root: str
    created_at: str
    portable_case_ids: tuple[str, ...]
    fixture_case_ids: tuple[str, ...]
    results: tuple[ACPTargetResult, ...]
    artifact_dir: str

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repo_root": self.repo_root,
            "created_at": self.created_at,
            "portable_case_ids": list(self.portable_case_ids),
            "fixture_case_ids": list(self.fixture_case_ids),
            "passed": self.passed,
            "results": [self._target_result_dict(result) for result in self.results],
            "artifact_dir": self.artifact_dir,
        }

    @staticmethod
    def _target_result_dict(result: ACPTargetResult) -> dict[str, Any]:
        payload = asdict(result)
        payload["target"]["command"] = list(result.target.command)
        payload["target"]["notes"] = list(result.target.notes)
        return payload


def discover_repo_acp_targets(repo_root: Path) -> list[ACPConformanceTarget]:
    repo_root = repo_root.resolve()
    config = load_repo_config(repo_root)
    targets: list[ACPConformanceTarget] = [
        ACPConformanceTarget(
            id="fixture.official",
            label="Fixture ACP (official)",
            command=tuple(fake_acp_command("official")),
            source="fixture",
            scenario="official",
            notes=("Deterministic fake ACP server used for CAR protocol coverage.",),
        )
    ]

    base_preflight = hermes_runtime_preflight(config, agent_id="hermes")
    if base_preflight.status == "ready":
        supervisor = build_hermes_supervisor_from_config(config, agent_id="hermes")
        if supervisor is not None:
            targets.append(
                ACPConformanceTarget(
                    id="hermes",
                    label="Hermes",
                    command=supervisor.launch_command,
                    source="repo_config",
                    agent_id="hermes",
                    notes=_notes_from_preflight(
                        base_preflight.message, base_preflight.version
                    ),
                )
            )

    hermes_cfg = getattr(config, "agents", {}).get("hermes")
    profiles = getattr(hermes_cfg, "profiles", {}) if hermes_cfg is not None else {}
    if isinstance(profiles, dict):
        for profile_name in sorted(profiles.keys()):
            preflight = hermes_runtime_preflight(
                config,
                agent_id="hermes",
                profile=profile_name,
            )
            if preflight.status != "ready":
                continue
            supervisor = build_hermes_supervisor_from_config(
                config,
                agent_id="hermes",
                profile=profile_name,
            )
            if supervisor is None:
                continue
            targets.append(
                ACPConformanceTarget(
                    id=f"hermes.{profile_name}",
                    label=f"Hermes [{profile_name}]",
                    command=supervisor.launch_command,
                    source="repo_config",
                    agent_id="hermes",
                    profile=profile_name,
                    notes=_notes_from_preflight(
                        preflight.message,
                        preflight.version,
                    ),
                )
            )
    return targets


async def run_acp_conformance_report(
    repo_root: Path,
    *,
    targets: Optional[Iterable[ACPConformanceTarget]] = None,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    portable_prompt: str = DEFAULT_COMPLETION_PROMPT,
) -> ACPConformanceReport:
    repo_root = repo_root.resolve()
    selected_targets = list(targets or discover_repo_acp_targets(repo_root))
    run_id = f"{_utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    run_root = artifact_dir / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    results = [
        await _run_target_conformance(
            target,
            portable_prompt=portable_prompt,
            artifact_root=run_root / target.id.replace("/", "_"),
        )
        for target in selected_targets
    ]
    report = ACPConformanceReport(
        run_id=run_id,
        repo_root=str(repo_root),
        created_at=_utc_now().isoformat(),
        portable_case_ids=(
            "initialize",
            "session_roundtrip",
            "prompt_completion",
            "prompt_reuse",
        ),
        fixture_case_ids=(
            "fixture.permission_flow",
            "fixture.interrupt_flow",
            "fixture.prompt_hang_timeout",
            "fixture.empty_load_result",
            "fixture.null_load_result",
        ),
        results=tuple(results),
        artifact_dir=str(run_root),
    )
    _write_report_artifacts(report, artifact_dir=artifact_dir, run_root=run_root)
    return report


def format_acp_conformance_report(report: ACPConformanceReport) -> str:
    lines = [
        f"ACP conformance run {report.run_id}: {'PASS' if report.passed else 'FAIL'}",
        f"repo_root={report.repo_root}",
        f"artifacts={report.artifact_dir}",
    ]
    for target_result in report.results:
        lines.append(
            f"- {target_result.target.label}: {target_result.status} ({target_result.summary})"
        )
        for case in target_result.cases:
            elapsed = f"{case.elapsed_ms}ms" if case.elapsed_ms is not None else "n/a"
            suffix = ""
            if case.error_message:
                suffix = f" error={case.error_message}"
            lines.append(
                f"  - {case.case_id}: {case.status} passed={case.passed} elapsed={elapsed}{suffix}"
            )
    return "\n".join(lines)


async def _run_target_conformance(
    target: ACPConformanceTarget,
    *,
    portable_prompt: str,
    artifact_root: Path,
) -> ACPTargetResult:
    started_at = _utc_now().isoformat()
    artifact_root.mkdir(parents=True, exist_ok=True)
    portable_results = await _run_portable_cases(
        target,
        artifact_root=artifact_root,
        portable_prompt=portable_prompt,
    )
    fixture_results: list[ACPCaseResult] = []
    if target.source == "fixture":
        fixture_results = await _run_fixture_cases(target, artifact_root=artifact_root)

    all_results = tuple([*portable_results, *fixture_results])
    passed = all(case.passed for case in all_results)
    status = "passed" if passed else "failed"
    failed = [case.case_id for case in all_results if not case.passed]
    summary = "all cases passed" if not failed else f"failed cases: {', '.join(failed)}"
    return ACPTargetResult(
        target=target,
        passed=passed,
        status=status,
        summary=summary,
        cases=all_results,
        started_at=started_at,
        finished_at=_utc_now().isoformat(),
    )


async def _run_portable_cases(
    target: ACPConformanceTarget,
    *,
    artifact_root: Path,
    portable_prompt: str,
) -> list[ACPCaseResult]:
    workspace_root = artifact_root / "portable_workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    client = ACPClient(
        list(target.command),
        cwd=workspace_root,
        env=target.env or None,
        request_timeout=120.0,
        permission_handler=_allow_permission_request,
    )
    try:
        initialize = await _measure_case(
            "initialize",
            lambda: _portable_initialize_case(client),
        )
        session_roundtrip = await _measure_case(
            "session_roundtrip",
            lambda: _portable_session_roundtrip_case(client, workspace_root),
        )
        prompt_completion = await _measure_case(
            "prompt_completion",
            lambda: _portable_prompt_completion_case(
                client,
                workspace_root,
                portable_prompt,
            ),
        )
        prompt_reuse = await _measure_case(
            "prompt_reuse",
            lambda: _portable_prompt_reuse_case(
                client,
                workspace_root,
                portable_prompt,
            ),
        )
        return [initialize, session_roundtrip, prompt_completion, prompt_reuse]
    finally:
        await client.close()


async def _run_fixture_cases(
    target: ACPConformanceTarget,
    *,
    artifact_root: Path,
) -> list[ACPCaseResult]:
    async def _run_case(case_id: str, scenario: str, func):
        case_root = artifact_root / case_id.replace(".", "_")
        case_root.mkdir(parents=True, exist_ok=True)
        client = ACPClient(
            list(fake_acp_command(scenario)),
            cwd=case_root,
            env=target.env or None,
            request_timeout=2.0,
            permission_handler=_allow_permission_request,
        )
        try:
            return await _measure_case(case_id, lambda: func(client, case_root))
        finally:
            await client.close()

    return [
        await _run_case(
            "fixture.permission_flow",
            "official",
            _fixture_permission_case,
        ),
        await _run_case(
            "fixture.interrupt_flow",
            "official",
            _fixture_interrupt_case,
        ),
        await _run_case(
            "fixture.prompt_hang_timeout",
            "official_prompt_hang",
            _fixture_prompt_hang_case,
        ),
        await _run_case(
            "fixture.empty_load_result",
            "official_empty_load_result",
            _fixture_empty_load_case,
        ),
        await _run_case(
            "fixture.null_load_result",
            "official_missing_load_result",
            _fixture_null_load_case,
        ),
    ]


async def _portable_initialize_case(client: ACPClient) -> dict[str, Any]:
    initialize = await client.start()
    capabilities = dict(initialize.capabilities or {})
    return {
        "server_name": initialize.server_name,
        "server_version": initialize.server_version,
        "protocol_version": initialize.protocol_version,
        "capabilities": capabilities,
        "advertised_commands": [command.name for command in client.advertised_commands],
        "session_capabilities": asdict(client.session_capabilities),
    }


async def _portable_session_roundtrip_case(
    client: ACPClient,
    workspace_root: Path,
) -> dict[str, Any]:
    created = await client.create_session(
        cwd=str(workspace_root), title="ACP Conformance"
    )
    loaded = await client.load_session(created.session_id)
    listed = await client.list_sessions()
    listed_ids = [session.session_id for session in listed]
    if created.session_id not in listed_ids:
        raise AssertionError("Created session id was missing from list_sessions()")
    return {
        "created_session_id": created.session_id,
        "loaded_session_id": loaded.session_id,
        "listed_session_count": len(listed_ids),
        "listed_contains_created": created.session_id in listed_ids,
    }


async def _portable_prompt_completion_case(
    client: ACPClient,
    workspace_root: Path,
    prompt: str,
) -> dict[str, Any]:
    created = await client.create_session(cwd=str(workspace_root))
    handle = await client.start_prompt(created.session_id, prompt)
    collector = _EventCollector()
    collector_task = asyncio.create_task(collector.collect(handle))
    try:
        result = await handle.wait(timeout=60.0)
    finally:
        await collector_task

    final_output = str(result.final_output or "")
    if result.status.lower() not in {
        "completed",
        "complete",
        "done",
        "success",
        "succeeded",
    }:
        raise AssertionError(f"Unexpected prompt status: {result.status}")
    if not final_output.strip():
        raise AssertionError("Prompt completed without any final output")

    return {
        "session_id": created.session_id,
        "turn_id": handle.turn_id,
        "status": result.status,
        "final_output": final_output,
        "contains_conformance_token": "CONFORMANCE_OK" in final_output.upper(),
        "event_methods": collector.event_methods,
        "event_kinds": collector.event_kinds,
        "first_event_latency_ms": collector.first_event_latency_ms,
    }


async def _portable_prompt_reuse_case(
    client: ACPClient,
    workspace_root: Path,
    prompt: str,
) -> dict[str, Any]:
    created = await client.create_session(cwd=str(workspace_root))
    turn_ids: list[str] = []
    outputs: list[str] = []
    for _ in range(2):
        handle = await client.start_prompt(created.session_id, prompt)
        result = await handle.wait(timeout=60.0)
        turn_ids.append(handle.turn_id)
        outputs.append(str(result.final_output or ""))
    if len(set(turn_ids)) != 2:
        raise AssertionError("Prompt reuse did not produce distinct turn ids")
    return {
        "session_id": created.session_id,
        "turn_ids": turn_ids,
        "outputs": outputs,
    }


async def _fixture_permission_case(
    client: ACPClient,
    workspace_root: Path,
) -> dict[str, Any]:
    created = await client.create_session(cwd=str(workspace_root))
    handle = await client.start_prompt(created.session_id, "needs permission")
    result = await handle.wait(timeout=5.0)
    if result.status.lower() not in {
        "completed",
        "complete",
        "done",
        "success",
        "succeeded",
    }:
        raise AssertionError(
            f"Permission flow did not complete cleanly: {result.status}"
        )
    return {
        "session_id": created.session_id,
        "turn_id": handle.turn_id,
        "status": result.status,
        "event_count": len(handle.snapshot_events()),
    }


async def _fixture_interrupt_case(
    client: ACPClient,
    workspace_root: Path,
) -> dict[str, Any]:
    created = await client.create_session(cwd=str(workspace_root))
    handle = await client.start_prompt(created.session_id, "cancel me")
    await asyncio.sleep(0.1)
    await client.cancel_prompt(created.session_id, handle.turn_id)
    result = await handle.wait(timeout=5.0)
    status = str(result.status or "").strip().lower()
    if status not in {"cancelled", "canceled", "interrupted"}:
        raise AssertionError(
            f"Interrupt flow returned unexpected status: {result.status}"
        )
    return {
        "session_id": created.session_id,
        "turn_id": handle.turn_id,
        "status": result.status,
    }


async def _fixture_prompt_hang_case(
    client: ACPClient,
    workspace_root: Path,
) -> dict[str, Any]:
    created = await client.create_session(cwd=str(workspace_root))
    handle = await client.start_prompt(created.session_id, "Reply with exactly OK.")
    try:
        await handle.wait(timeout=0.2)
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("Hanging prompt unexpectedly completed")
    return {
        "session_id": created.session_id,
        "turn_id": handle.turn_id,
        "snapshot_event_kinds": [event.kind for event in handle.snapshot_events()],
    }


async def _fixture_empty_load_case(
    client: ACPClient,
    workspace_root: Path,
) -> dict[str, Any]:
    created = await client.create_session(cwd=str(workspace_root))
    loaded = await client.load_session(created.session_id)
    return {
        "session_id": loaded.session_id,
        "raw": loaded.raw,
    }


async def _fixture_null_load_case(
    client: ACPClient,
    workspace_root: Path,
) -> dict[str, Any]:
    await client.start()
    try:
        await client.load_session("missing-session")
    except ACPMissingSessionError as exc:
        return {"error": str(exc)}
    raise AssertionError("Null load result unexpectedly succeeded")


async def _measure_case(
    case_id: str,
    run,
) -> ACPCaseResult:
    started = time.monotonic()
    try:
        details = await run()
        return ACPCaseResult(
            case_id=case_id,
            passed=True,
            status="passed",
            elapsed_ms=_elapsed_ms(started),
            details=details,
        )
    except (AssertionError, ACPError, asyncio.TimeoutError) as exc:
        return ACPCaseResult(
            case_id=case_id,
            passed=False,
            status="failed",
            elapsed_ms=_elapsed_ms(started),
            details={},
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    except Exception as exc:  # intentional: preserve unexpected crash type in report
        logger.exception("ACP conformance case crashed: %s", case_id)
        return ACPCaseResult(
            case_id=case_id,
            passed=False,
            status="crashed",
            elapsed_ms=_elapsed_ms(started),
            details={},
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def _write_report_artifacts(
    report: ACPConformanceReport,
    *,
    artifact_dir: Path,
    run_root: Path,
) -> None:
    payload = report.to_dict()
    run_root.mkdir(parents=True, exist_ok=True)
    report_path = run_root / "suite_report.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    latest_path = artifact_dir / "latest.json"
    latest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _notes_from_preflight(message: str, version: Optional[str]) -> tuple[str, ...]:
    notes = [str(message or "").strip()]
    if version:
        notes.append(version)
    return tuple(note for note in notes if note)


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _allow_permission_request(_event: Any) -> str:
    return "allow"


class _EventCollector:
    def __init__(self) -> None:
        self.event_methods: list[str] = []
        self.event_kinds: list[str] = []
        self.first_event_latency_ms: Optional[int] = None
        self._started = time.monotonic()

    async def collect(self, handle: Any) -> None:
        async for event in handle.events():
            if self.first_event_latency_ms is None:
                self.first_event_latency_ms = _elapsed_ms(self._started)
            self.event_methods.append(str(getattr(event, "method", "") or ""))
            self.event_kinds.append(str(getattr(event, "kind", "") or ""))


__all__ = [
    "ACPCaseResult",
    "ACPConformanceReport",
    "ACPConformanceTarget",
    "ACPTargetResult",
    "DEFAULT_ARTIFACT_DIR",
    "DEFAULT_COMPLETION_PROMPT",
    "discover_repo_acp_targets",
    "format_acp_conformance_report",
    "run_acp_conformance_report",
]
