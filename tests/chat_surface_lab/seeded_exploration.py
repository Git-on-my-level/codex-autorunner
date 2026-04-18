from __future__ import annotations

import json
import random
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from tests.chat_surface_lab.scenario_models import SurfaceKind
from tests.chat_surface_lab.scenario_runner import (
    ChatSurfaceScenarioRunner,
    ScenarioDefinition,
    ScenarioFaultSpec,
    ScenarioRunResult,
    load_scenario,
    load_scenario_by_id,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPLORATION_ARTIFACT_DIR = (
    _REPO_ROOT / ".codex-autorunner" / "diagnostics" / "chat-seeded-exploration"
)
DEFAULT_SEED_VALUES = (1701, 1702, 1703)
DEFAULT_PERTURBATION_IDS = (
    "duplicate_delivery",
    "delayed_terminal_event",
    "delete_failure",
    "retry_after_backpressure",
    "restart_window",
    "queued_submission",
)


@dataclass(frozen=True)
class SeededExplorationTrial:
    trial_id: str
    seed: int
    perturbation_id: str
    scenario: ScenarioDefinition
    scenario_path: Path


@dataclass(frozen=True)
class SeededExplorationFailure:
    trial_id: str
    seed: int
    perturbation_id: str
    failure_path: Path
    scenario_path: Path
    run_output_dir: Path
    error_type: str
    error_message: str


@dataclass(frozen=True)
class SeededExplorationCampaignResult:
    run_id: str
    passed: bool
    output_dir: Path
    summary_path: Path
    trial_count: int
    failure_count: int
    failures: tuple[SeededExplorationFailure, ...]
    payload: dict[str, Any]


async def run_seeded_exploration_campaign(
    *,
    output_dir: Path = DEFAULT_EXPLORATION_ARTIFACT_DIR,
    seeds: Sequence[int] = DEFAULT_SEED_VALUES,
    perturbation_ids: Sequence[str] = DEFAULT_PERTURBATION_IDS,
    apply_runtime_patch: Optional[Callable[[Any], None]] = None,
    result_validator: Optional[
        Callable[[SeededExplorationTrial, ScenarioRunResult], None]
    ] = None,
) -> SeededExplorationCampaignResult:
    normalized_seeds = _normalize_seed_values(seeds)
    normalized_perturbations = _normalize_perturbation_ids(perturbation_ids)
    run_id = _build_run_id()
    run_dir = output_dir / "runs" / run_id
    scenarios_dir = run_dir / "scenarios"
    failures_dir = run_dir / "failures"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    failures_dir.mkdir(parents=True, exist_ok=True)

    runner = ChatSurfaceScenarioRunner(
        output_root=run_dir / "scenario_runs",
        apply_runtime_patch=apply_runtime_patch,
    )

    trial_payloads: list[dict[str, Any]] = []
    failures: list[SeededExplorationFailure] = []
    trial_count = 0

    for seed in normalized_seeds:
        for perturbation_id in normalized_perturbations:
            trial_count += 1
            trial_id = f"seed-{seed}-{perturbation_id}"
            trial_rng = random.Random(seed)
            scenario = _build_perturbed_scenario(
                perturbation_id=perturbation_id,
                rng=trial_rng,
            )
            scenario = replace(
                scenario,
                scenario_id=trial_id,
                tags=_with_unique_tag(scenario.tags, "seeded_exploration"),
            )
            scenario_path = scenarios_dir / f"{trial_id}.json"
            _write_json(
                scenario_path,
                scenario_definition_to_payload(scenario),
            )
            trial = SeededExplorationTrial(
                trial_id=trial_id,
                seed=seed,
                perturbation_id=perturbation_id,
                scenario=scenario,
                scenario_path=scenario_path,
            )
            run_output_dir = run_dir / "scenario_runs" / trial_id
            try:
                result = await runner.run_scenario(scenario)
                if result_validator is not None:
                    result_validator(trial, result)
                trial_payloads.append(
                    {
                        "trial_id": trial_id,
                        "seed": seed,
                        "perturbation_id": perturbation_id,
                        "status": "passed",
                        "scenario_path": _display_path(scenario_path, base=run_dir),
                        "run_output_dir": _display_path(run_output_dir, base=run_dir),
                        "summary_path": (
                            _display_path(result.summary_path, base=run_dir)
                            if result.summary_path is not None
                            else None
                        ),
                    }
                )
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                failure = _persist_failure_seed(
                    run_dir=run_dir,
                    failures_dir=failures_dir,
                    trial=trial,
                    run_output_dir=run_output_dir,
                    exc=exc,
                )
                failures.append(failure)
                trial_payloads.append(
                    {
                        "trial_id": trial_id,
                        "seed": seed,
                        "perturbation_id": perturbation_id,
                        "status": "failed",
                        "scenario_path": _display_path(scenario_path, base=run_dir),
                        "run_output_dir": _display_path(run_output_dir, base=run_dir),
                        "failure_path": _display_path(
                            failure.failure_path, base=run_dir
                        ),
                        "error_type": failure.error_type,
                        "error_message": failure.error_message,
                    }
                )

    payload: dict[str, Any] = {
        "version": 1,
        "suite": "chat_surface_seeded_exploration",
        "run_id": run_id,
        "artifact_root": _display_path(output_dir, base=_REPO_ROOT),
        "run_dir": _display_path(run_dir, base=_REPO_ROOT),
        "seed_values": list(normalized_seeds),
        "perturbation_ids": list(normalized_perturbations),
        "trial_count": trial_count,
        "failure_count": len(failures),
        "passed": len(failures) == 0,
        "trials": trial_payloads,
    }
    summary_path = run_dir / "campaign_summary.json"
    _write_json(summary_path, payload)

    latest_path = output_dir / "latest.json"
    _write_json(latest_path, payload)

    return SeededExplorationCampaignResult(
        run_id=run_id,
        passed=len(failures) == 0,
        output_dir=run_dir,
        summary_path=summary_path,
        trial_count=trial_count,
        failure_count=len(failures),
        failures=tuple(failures),
        payload=payload,
    )


async def replay_preserved_failure_seed(
    *,
    failure_path: Path,
    output_dir: Path,
    apply_runtime_patch: Optional[Callable[[Any], None]] = None,
    result_validator: Optional[
        Callable[[SeededExplorationTrial, ScenarioRunResult], None]
    ] = None,
) -> ScenarioRunResult:
    payload = json.loads(failure_path.read_text(encoding="utf-8"))
    scenario_payload = payload.get("scenario")
    if not isinstance(scenario_payload, dict):
        raise ValueError(f"{failure_path}: missing scenario payload")
    scenario_path = output_dir / "replay_scenario.json"
    _write_json(scenario_path, scenario_payload)
    scenario = load_scenario(scenario_path)
    scenario = replace(
        scenario,
        scenario_id=f"{scenario.scenario_id}-replay",
    )
    runner = ChatSurfaceScenarioRunner(
        output_root=output_dir / "replay_runs",
        apply_runtime_patch=apply_runtime_patch,
    )
    result = await runner.run_scenario(scenario)
    if result_validator is not None:
        trial = SeededExplorationTrial(
            trial_id=str(payload.get("trial_id") or scenario.scenario_id),
            seed=int(payload.get("seed") or 0),
            perturbation_id=str(payload.get("perturbation_id") or ""),
            scenario=scenario,
            scenario_path=scenario_path,
        )
        result_validator(trial, result)
    return result


def available_seeded_perturbations() -> tuple[str, ...]:
    return DEFAULT_PERTURBATION_IDS


def scenario_definition_to_payload(scenario: ScenarioDefinition) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "scenario_id": scenario.scenario_id,
        "description": scenario.description,
        "surfaces": [surface.value for surface in scenario.surfaces],
        "runtime_fixture": {
            "kind": scenario.runtime.kind.value,
            "scenario": scenario.runtime.scenario,
        },
    }
    if scenario.actions:
        payload["actions"] = [
            {
                "kind": action.kind,
                "actor": action.actor,
                "payload": dict(action.payload),
                "delay_ms": int(action.delay_ms),
                **(
                    {"surfaces": [surface.value for surface in action.surfaces]}
                    if action.surfaces
                    else {}
                ),
            }
            for action in scenario.actions
        ]
    if scenario.faults:
        payload["faults"] = [
            {
                "kind": fault.kind,
                "target": fault.target,
                "parameters": dict(fault.parameters),
                **(
                    {"surfaces": [surface.value for surface in fault.surfaces]}
                    if fault.surfaces
                    else {}
                ),
            }
            for fault in scenario.faults
        ]
    if scenario.transcript_invariants:
        payload["transcript_invariants"] = [
            {
                "kind": invariant.kind,
                "params": dict(invariant.params),
                **(
                    {"surfaces": [surface.value for surface in invariant.surfaces]}
                    if invariant.surfaces
                    else {}
                ),
            }
            for invariant in scenario.transcript_invariants
        ]
    if scenario.latency_budgets:
        payload["latency_budgets"] = [
            {
                "budget_id": budget.budget_id,
                "event_name": budget.event_name,
                "field": budget.field,
                "surfaces": [budget.surface.value],
                **({"max_ms": budget.max_ms} if budget.max_ms is not None else {}),
            }
            for budget in scenario.latency_budgets
        ]
    if scenario.expected_terminal.status is not None:
        payload["expected_terminal"] = {"status": scenario.expected_terminal.status}
    if scenario.contract_links.regression_ids or scenario.contract_links.references:
        payload["contract_links"] = {
            "regression_ids": list(scenario.contract_links.regression_ids),
            "latency_budget_ids": list(scenario.contract_links.latency_budget_ids),
            "references": list(scenario.contract_links.references),
        }
    if scenario.tags:
        payload["tags"] = list(scenario.tags)
    if scenario.notes:
        payload["notes"] = scenario.notes
    if scenario.execution_mode != "surface_harness":
        payload["execution_mode"] = scenario.execution_mode
    if scenario.harness_timeouts:
        payload["harness_timeouts"] = {
            surface.value: timeout
            for surface, timeout in sorted(
                scenario.harness_timeouts.items(),
                key=lambda entry: entry[0].value,
            )
        }
    if scenario.approval_mode:
        payload["approval_mode"] = scenario.approval_mode
    return payload


def _build_perturbed_scenario(
    *,
    perturbation_id: str,
    rng: random.Random,
) -> ScenarioDefinition:
    if perturbation_id == "duplicate_delivery":
        return load_scenario_by_id("duplicate_delivery")
    if perturbation_id == "delayed_terminal_event":
        base = load_scenario_by_id("first_visible_feedback")
        return replace(
            base,
            runtime=replace(
                base.runtime,
                scenario="official_request_return_after_terminal",
            ),
            notes=(
                "Seeded exploration variant: backend terminal event arrives before "
                "request-return completion."
            ),
        )
    if perturbation_id == "delete_failure":
        base = load_scenario_by_id("first_visible_feedback")
        return replace(
            base,
            faults=base.faults
            + (
                ScenarioFaultSpec(
                    kind="fail_progress_delete",
                    target="surface",
                    parameters={"message_id": "msg-1"},
                    surfaces=(SurfaceKind.DISCORD,),
                ),
                ScenarioFaultSpec(
                    kind="fail_progress_delete",
                    target="surface",
                    parameters={"message_id": 1},
                    surfaces=(SurfaceKind.TELEGRAM,),
                ),
            ),
            notes=(
                "Seeded exploration variant: progress cleanup delete fails on both "
                "surfaces."
            ),
        )
    if perturbation_id == "retry_after_backpressure":
        base = load_scenario_by_id("first_visible_feedback")
        retry_after_seconds = 1 + rng.randint(0, 1)
        return replace(
            base,
            faults=base.faults
            + (
                ScenarioFaultSpec(
                    kind="retry_after",
                    target="surface",
                    parameters={
                        "operation": "create_channel_message",
                        "seconds": [retry_after_seconds],
                    },
                    surfaces=(SurfaceKind.DISCORD,),
                ),
                ScenarioFaultSpec(
                    kind="retry_after",
                    target="surface",
                    parameters={
                        "operation": "send_message",
                        "seconds": [retry_after_seconds],
                    },
                    surfaces=(SurfaceKind.TELEGRAM,),
                ),
            ),
            notes=(
                "Seeded exploration variant: transient retry-after/backpressure "
                "faults injected on initial user-visible sends."
            ),
        )
    if perturbation_id == "restart_window":
        return load_scenario_by_id("restart_window_duplicate_delivery")
    if perturbation_id == "queued_submission":
        return load_scenario_by_id("queued_visibility")
    raise ValueError(f"unsupported perturbation id: {perturbation_id}")


def _persist_failure_seed(
    *,
    run_dir: Path,
    failures_dir: Path,
    trial: SeededExplorationTrial,
    run_output_dir: Path,
    exc: BaseException,
) -> SeededExplorationFailure:
    failure_dir = failures_dir / trial.trial_id
    failure_dir.mkdir(parents=True, exist_ok=True)
    scenario_path = failure_dir / "scenario.json"
    _write_json(
        scenario_path,
        scenario_definition_to_payload(trial.scenario),
    )
    failure_path = failure_dir / "failure.json"
    failure_payload = {
        "schema_version": 1,
        "trial_id": trial.trial_id,
        "seed": trial.seed,
        "perturbation_id": trial.perturbation_id,
        "scenario": scenario_definition_to_payload(trial.scenario),
        "scenario_path": _display_path(scenario_path, base=run_dir),
        "run_output_dir": _display_path(run_output_dir, base=run_dir),
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
        "traceback": traceback.format_exc(limit=12),
    }
    _write_json(failure_path, failure_payload)
    return SeededExplorationFailure(
        trial_id=trial.trial_id,
        seed=trial.seed,
        perturbation_id=trial.perturbation_id,
        failure_path=failure_path,
        scenario_path=scenario_path,
        run_output_dir=run_output_dir,
        error_type=exc.__class__.__name__,
        error_message=str(exc),
    )


def _normalize_seed_values(seeds: Sequence[int]) -> tuple[int, ...]:
    values: list[int] = []
    seen: set[int] = set()
    for raw in seeds:
        seed = int(raw)
        if seed in seen:
            continue
        seen.add(seed)
        values.append(seed)
    if not values:
        return DEFAULT_SEED_VALUES
    return tuple(values)


def _normalize_perturbation_ids(perturbation_ids: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in perturbation_ids:
        candidate = str(raw or "").strip()
        if not candidate or candidate in seen:
            continue
        if candidate not in DEFAULT_PERTURBATION_IDS:
            raise ValueError(f"unknown perturbation id: {candidate}")
        seen.add(candidate)
        values.append(candidate)
    if not values:
        return DEFAULT_PERTURBATION_IDS
    return tuple(values)


def _with_unique_tag(tags: tuple[str, ...], tag: str) -> tuple[str, ...]:
    normalized = str(tag or "").strip()
    if not normalized:
        return tags
    if normalized in tags:
        return tags
    return tags + (normalized,)


def _build_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _display_path(path: Optional[Path], *, base: Path) -> Optional[str]:
    if path is None:
        return None
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(base.resolve()))
    except ValueError:
        return str(resolved)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DEFAULT_EXPLORATION_ARTIFACT_DIR",
    "DEFAULT_PERTURBATION_IDS",
    "DEFAULT_SEED_VALUES",
    "SeededExplorationCampaignResult",
    "SeededExplorationFailure",
    "SeededExplorationTrial",
    "available_seeded_perturbations",
    "replay_preserved_failure_seed",
    "run_seeded_exploration_campaign",
    "scenario_definition_to_payload",
]
