from __future__ import annotations

import json
import platform
import socket
import subprocess
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from codex_autorunner.core.utils import atomic_write
from codex_autorunner.integrations.chat.ux_regression_contract import (
    CHAT_UX_LATENCY_BUDGETS,
    REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS,
    REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS,
    campaign_north_star_status,
)
from tests.chat_surface_lab.scenario_runner import (
    ChatSurfaceScenarioRunner,
    ScenarioDefinition,
    load_scenario_by_id,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIR = (
    _REPO_ROOT / ".codex-autorunner" / "diagnostics" / "chat-latency-budgets"
)
DEFAULT_LATENCY_SCENARIO_IDS = (
    "first_visible_feedback",
    "queued_visibility",
    "interrupt_optimistic_acceptance",
    "duplicate_delivery",
    "interrupt_confirmation",
    "progress_anchor_reuse",
    "restart_recovery",
)
_BUDGET_REGISTRY_BY_ID = {entry.id: entry for entry in CHAT_UX_LATENCY_BUDGETS}
_HUB_PATCH_INSTALLED = False


@dataclass(frozen=True)
class LatencyBudgetSuiteResult:
    run_id: str
    passed: bool
    latest_path: Path
    history_path: Path
    run_report_path: Path
    payload: dict[str, Any]


def apply_hermes_runtime_patch_for_lab(runtime: Any) -> None:
    """Patch chat surface runtime lookups without pytest's monkeypatch fixture."""
    _install_inprocess_hub_client_stubs_for_lab()

    def _registered(_context: Any = None) -> dict[str, Any]:
        return runtime.registered_agents()

    import codex_autorunner.agents.registry as agent_registry
    import codex_autorunner.integrations.discord.message_turns as discord_turns
    import codex_autorunner.integrations.telegram.handlers.commands.execution as telegram_execution

    agent_registry.get_registered_agents = _registered
    discord_turns.get_registered_agents = _registered
    telegram_execution.get_registered_agents = _registered


async def run_chat_surface_latency_budget_suite(
    *,
    scenario_ids: Optional[Sequence[str]] = None,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    profile: str = "default",
    enforce_required_coverage: Optional[bool] = None,
    apply_runtime_patch: Optional[Callable[[Any], None]] = None,
    scenario_mutator: Optional[
        Callable[[ScenarioDefinition], ScenarioDefinition]
    ] = None,
) -> LatencyBudgetSuiteResult:
    selected_scenarios = _normalize_scenario_ids(scenario_ids)
    coverage_required = (
        enforce_required_coverage
        if enforce_required_coverage is not None
        else scenario_ids is None
    )
    started_at = _utc_now()
    run_id = _build_run_id(started_at)
    scenario_output_root = artifact_dir / "runs" / run_id

    runner = ChatSurfaceScenarioRunner(
        output_root=scenario_output_root,
        apply_runtime_patch=apply_runtime_patch or apply_hermes_runtime_patch_for_lab,
    )

    scenario_results: list[dict[str, Any]] = []
    observed_budgets: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for scenario_id in selected_scenarios:
        loaded = load_scenario_by_id(scenario_id)
        scenario = scenario_mutator(loaded) if scenario_mutator is not None else loaded
        scenario_started = time.monotonic()
        relative_output_dir = f"runs/{run_id}/{scenario.scenario_id}"

        try:
            result = await runner.run_scenario(scenario)
            elapsed_ms = round((time.monotonic() - scenario_started) * 1000, 1)
            observed_rows: list[dict[str, Any]] = []
            for budget in result.observed_budgets:
                observed_row = {
                    "scenario_id": scenario.scenario_id,
                    "surface": budget.surface.value,
                    "budget_id": budget.budget_id,
                    "event_name": budget.event_name,
                    "field": budget.field,
                    "observed_ms": round(float(budget.observed_ms), 1),
                    "max_ms": round(float(budget.max_ms), 1),
                }
                observed_rows.append(observed_row)
                observed_budgets.append(observed_row)

            scenario_results.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "status": "passed",
                    "elapsed_ms": elapsed_ms,
                    "output_dir": relative_output_dir,
                    "summary_path": (
                        _display_path(result.summary_path, base=artifact_dir)
                        if result.summary_path is not None
                        else None
                    ),
                    "observed_budget_count": len(observed_rows),
                    "observed_budgets": observed_rows,
                }
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            elapsed_ms = round((time.monotonic() - scenario_started) * 1000, 1)
            message = str(exc) or exc.__class__.__name__
            failures.append(
                {
                    "kind": "scenario_execution_failed",
                    "scenario_id": scenario.scenario_id,
                    "message": message,
                    "error_type": exc.__class__.__name__,
                }
            )
            scenario_results.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "status": "failed",
                    "elapsed_ms": elapsed_ms,
                    "output_dir": relative_output_dir,
                    "error_type": exc.__class__.__name__,
                    "error_message": message,
                    "traceback": traceback.format_exc(limit=8),
                    "observed_budget_count": 0,
                }
            )

    observed_budget_ids = {item["budget_id"] for item in observed_budgets}
    if coverage_required:
        for budget_id in REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS:
            if budget_id in observed_budget_ids:
                continue
            failures.append(
                {
                    "kind": "required_budget_missing_observation",
                    "budget_id": budget_id,
                    "message": (
                        f"required latency budget {budget_id!r} had no successful "
                        "observations in this suite run"
                    ),
                }
            )

    finished_at = _utc_now()
    coverage = _build_budget_coverage(observed_budgets)
    signoff_passed = not failures
    signoff_message = (
        f"chat latency budgets passed: {len(selected_scenarios)} scenarios, "
        f"{len(observed_budgets)} observed checks"
    )
    if not signoff_passed:
        signoff_message = (
            f"chat latency budgets failed: {len(failures)} issue(s) across "
            f"{len(selected_scenarios)} scenarios"
        )

    north_star = campaign_north_star_status(
        observed_budgets=observed_budgets,
        observed_scenario_ids=[
            r["scenario_id"] for r in scenario_results if r["status"] == "passed"
        ],
    )

    payload: dict[str, Any] = {
        "version": 1,
        "suite": "chat_surface_latency_budgets",
        "profile": profile,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "environment": _collect_environment(),
        "artifact_root": _display_path(artifact_dir, base=_REPO_ROOT),
        "scenario_output_root": _display_path(scenario_output_root, base=_REPO_ROOT),
        "scenario_ids": list(selected_scenarios),
        "enforce_required_coverage": coverage_required,
        "required_budget_ids": list(REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS),
        "required_scenario_ids": list(REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS),
        "campaign_north_star": {
            "green": north_star.green,
            "budget_statuses": [
                {
                    "budget_id": bs.budget_id,
                    "threshold_ms": bs.threshold_ms,
                    "observed_ms": bs.observed_ms,
                    "passed": bs.passed,
                    "observed": bs.observed,
                }
                for bs in north_star.budget_statuses
            ],
            "covered_scenario_ids": list(north_star.covered_scenario_ids),
            "missing_scenario_ids": list(north_star.missing_scenario_ids),
        },
        "budget_registry": [
            {
                "id": entry.id,
                "description": entry.description,
                "max_ms": round(float(entry.max_ms), 1),
            }
            for entry in CHAT_UX_LATENCY_BUDGETS
        ],
        "scenario_results": scenario_results,
        "observed_budgets": observed_budgets,
        "budget_coverage": coverage,
        "failures": failures,
        "signoff": {
            "passed": signoff_passed,
            "message": signoff_message,
        },
    }

    latest_path, history_path, run_report_path = _write_suite_artifacts(
        artifact_dir=artifact_dir,
        payload=payload,
        run_id=run_id,
        profile=profile,
        started_at=started_at,
    )
    return LatencyBudgetSuiteResult(
        run_id=run_id,
        passed=signoff_passed,
        latest_path=latest_path,
        history_path=history_path,
        run_report_path=run_report_path,
        payload=payload,
    )


def format_suite_summary(result: LatencyBudgetSuiteResult) -> str:
    payload = result.payload
    signoff = payload.get("signoff", {})
    north_star = payload.get("campaign_north_star", {})
    lines = [
        "CHAT SURFACE LATENCY BUDGET SUITE",
        f"run_id={result.run_id}",
        f"status={'PASS' if result.passed else 'FAIL'}",
        f"message={signoff.get('message', '')}",
        (
            f"scenarios={len(payload.get('scenario_results', []))} "
            f"observed_budgets={len(payload.get('observed_budgets', []))} "
            f"failures={len(payload.get('failures', []))}"
        ),
        f"latest={result.latest_path}",
        f"history={result.history_path}",
        "",
    ]
    lines.append("scenario results:")
    for sr in payload.get("scenario_results", []):
        sid = sr.get("scenario_id", "?")
        status = sr.get("status", "?")
        output_dir = sr.get("output_dir", "")
        budget_count = sr.get("observed_budget_count", 0)
        summary_path = sr.get("summary_path")
        lines.append(f"  {sid}: {status} (budgets={budget_count}, output={output_dir})")
        for obs in sr.get("observed_budgets", []):
            budget_id = obs.get("budget_id", "?")
            observed_ms = obs.get("observed_ms", 0)
            max_ms = obs.get("max_ms", 0)
            lines.append(f"    {budget_id}: {observed_ms:.1f}ms / {max_ms:.0f}ms")
        if summary_path:
            lines.append(f"    summary: {summary_path}")
        error_msg = sr.get("error_message")
        if error_msg:
            lines.append(f"    error: {error_msg}")
    failures = payload.get("failures", [])
    if failures:
        lines.append("")
        lines.append("failures:")
        for fail in failures:
            kind = fail.get("kind", "?")
            sid = fail.get("scenario_id") or fail.get("budget_id", "?")
            msg = fail.get("message", "")
            lines.append(f"  [{kind}] {sid}: {msg}")
    lines.append("")
    lines.append(
        f"campaign north star: {'GREEN' if north_star.get('green') else 'RED'}"
    )
    for bs in north_star.get("budget_statuses", []):
        if not bs.get("observed"):
            lines.append(
                f"  {bs['budget_id']}: NO_OBSERVATION (threshold <= {bs['threshold_ms']:.0f} ms)"
            )
        elif bs.get("passed"):
            lines.append(
                f"  {bs['budget_id']}: PASS ({bs['observed_ms']:.1f} ms <= {bs['threshold_ms']:.0f} ms)"
            )
        else:
            lines.append(
                f"  {bs['budget_id']}: FAIL ({bs['observed_ms']:.1f} ms > {bs['threshold_ms']:.0f} ms)"
            )
    missing = north_star.get("missing_scenario_ids", [])
    if missing:
        lines.append(f"  missing scenarios: {', '.join(missing)}")
    return "\n".join(lines)


def available_latency_budget_scenario_ids() -> tuple[str, ...]:
    return DEFAULT_LATENCY_SCENARIO_IDS


def _build_budget_coverage(
    observed_budgets: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    values_by_budget: dict[str, list[float]] = {}
    for item in observed_budgets:
        budget_id = str(item.get("budget_id") or "").strip()
        observed_ms = item.get("observed_ms")
        if not budget_id or not isinstance(observed_ms, (int, float)):
            continue
        values_by_budget.setdefault(budget_id, []).append(float(observed_ms))

    ordered_budget_ids = list(REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS)
    for budget_id in values_by_budget:
        if budget_id not in ordered_budget_ids:
            ordered_budget_ids.append(budget_id)

    coverage: dict[str, dict[str, float | int]] = {}
    for budget_id in ordered_budget_ids:
        values = sorted(values_by_budget.get(budget_id, []))
        if not values:
            continue
        registry_entry = _BUDGET_REGISTRY_BY_ID.get(budget_id)
        max_ms = (
            round(float(registry_entry.max_ms), 1)
            if registry_entry is not None
            else 0.0
        )
        coverage[budget_id] = {
            "count": len(values),
            "min_ms": round(min(values), 1),
            "avg_ms": round(sum(values) / len(values), 1),
            "p95_ms": _percentile(values, 95),
            "max_ms": round(max(values), 1),
            "budget_max_ms": max_ms,
        }
    return coverage


def _normalize_scenario_ids(scenario_ids: Optional[Sequence[str]]) -> tuple[str, ...]:
    if not scenario_ids:
        return DEFAULT_LATENCY_SCENARIO_IDS
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in scenario_ids:
        candidate = str(raw or "").strip()
        if not candidate or candidate in seen:
            continue
        normalized.append(candidate)
        seen.add(candidate)
    if not normalized:
        return DEFAULT_LATENCY_SCENARIO_IDS
    return tuple(normalized)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _build_run_id(timestamp: str) -> str:
    return (
        timestamp.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
    )


def _collect_environment() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "car_git_ref": _git_ref(),
    }


def _git_ref() -> Optional[str]:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        sha = rev.stdout.strip() if rev.returncode == 0 else ""
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
        if not sha:
            return None
        suffix = "-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else ""
        return f"{sha}{suffix}"
    except OSError:
        return None


def _write_suite_artifacts(
    *,
    artifact_dir: Path,
    payload: dict[str, Any],
    run_id: str,
    profile: str,
    started_at: str,
) -> tuple[Path, Path, Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    run_report_path = artifact_dir / "runs" / run_id / "suite_report.json"
    run_report_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write(run_report_path, serialized)

    latest_path = artifact_dir / "latest.json"
    atomic_write(latest_path, serialized)

    history_dir = artifact_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    compact_ts = (
        started_at.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
    )
    history_path = history_dir / f"{compact_ts}-{profile}.json"
    atomic_write(history_path, serialized)
    return latest_path, history_path, run_report_path


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = p / 100.0 * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return round(sorted_values[lower], 1)
    fraction = index - lower
    return round(
        sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower]),
        1,
    )


def _display_path(path: Path, *, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _install_inprocess_hub_client_stubs_for_lab() -> None:
    global _HUB_PATCH_INSTALLED
    if _HUB_PATCH_INSTALLED:
        return

    from codex_autorunner.core.hub_control_plane import HubSharedStateService
    from codex_autorunner.core.hub_control_plane.models import HandshakeCompatibility
    from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
    from codex_autorunner.core.pma_context import build_hub_snapshot
    from codex_autorunner.core.pma_thread_store import prepare_pma_thread_store
    from codex_autorunner.integrations.discord.service import DiscordBotService
    from codex_autorunner.integrations.telegram.service import TelegramBotService

    class _NoopSupervisor:
        def list_agent_workspaces(self, *, use_cache: bool = True) -> list[object]:
            _ = use_cache
            return []

        def get_agent_workspace_snapshot(self, workspace_id: str) -> object:
            raise ValueError(f"Unknown workspace id: {workspace_id}")

        def run_setup_commands_for_workspace(
            self, workspace_root: Path, *, repo_id_hint: Optional[str] = None
        ) -> int:
            _ = workspace_root, repo_id_hint
            return 0

        def process_pma_automation_now(
            self, *, include_timers: bool = True, limit: int = 100
        ) -> dict[str, int]:
            return {
                "timers_processed": 1 if include_timers else 0,
                "wakeups_dispatched": limit,
            }

    class _InProcessHubControlPlaneClient:
        def __init__(self, hub_root: Path) -> None:
            self._hub_root = Path(hub_root)
            prepare_orchestration_sqlite(self._hub_root, durable=False)
            prepare_pma_thread_store(self._hub_root, durable=False)
            self._service = HubSharedStateService(
                hub_root=self._hub_root,
                supervisor=_NoopSupervisor(),
                durable_writes=False,
            )

        async def handshake(self, request: Any) -> Any:
            return self._service.handshake(request)

        async def get_notification_record(self, request: Any) -> Any:
            return self._service.get_notification_record(request)

        async def get_notification_reply_target(self, request: Any) -> Any:
            return self._service.get_notification_reply_target(request)

        async def bind_notification_continuation(self, request: Any) -> Any:
            return self._service.bind_notification_continuation(request)

        async def mark_notification_delivered(self, request: Any) -> Any:
            return self._service.mark_notification_delivered(request)

        async def get_surface_binding(self, request: Any) -> Any:
            return self._service.get_surface_binding(request)

        async def upsert_surface_binding(self, request: Any) -> Any:
            return self._service.upsert_surface_binding(request)

        async def list_surface_bindings(self, request: Any) -> Any:
            return self._service.list_surface_bindings(request)

        async def get_thread_target(self, request: Any) -> Any:
            return self._service.get_thread_target(request)

        async def list_thread_targets(self, request: Any) -> Any:
            return self._service.list_thread_targets(request)

        async def create_thread_target(self, request: Any) -> Any:
            return self._service.create_thread_target(request)

        async def create_execution(self, request: Any) -> Any:
            return self._service.create_execution(request)

        async def get_execution(self, request: Any) -> Any:
            return self._service.get_execution(request)

        async def get_running_execution(self, request: Any) -> Any:
            return self._service.get_running_execution(request)

        async def list_thread_target_ids_with_running_executions(
            self, request: Any
        ) -> Any:
            return self._service.list_thread_target_ids_with_running_executions(request)

        async def get_latest_execution(self, request: Any) -> Any:
            return self._service.get_latest_execution(request)

        async def list_queued_executions(self, request: Any) -> Any:
            return self._service.list_queued_executions(request)

        async def get_queue_depth(self, request: Any) -> Any:
            return self._service.get_queue_depth(request)

        async def cancel_queued_execution(self, request: Any) -> Any:
            return self._service.cancel_queued_execution(request)

        async def promote_queued_execution(self, request: Any) -> Any:
            return self._service.promote_queued_execution(request)

        async def record_execution_result(self, request: Any) -> Any:
            return self._service.record_execution_result(request)

        async def record_execution_interrupted(self, request: Any) -> Any:
            return self._service.record_execution_interrupted(request)

        async def cancel_queued_executions(self, request: Any) -> Any:
            return self._service.cancel_queued_executions(request)

        async def set_execution_backend_id(self, request: Any) -> None:
            self._service.set_execution_backend_id(request)

        async def claim_next_queued_execution(self, request: Any) -> Any:
            return self._service.claim_next_queued_execution(request)

        async def persist_execution_timeline(self, request: Any) -> Any:
            return self._service.persist_execution_timeline(request)

        async def finalize_execution_cold_trace(self, request: Any) -> Any:
            return self._service.finalize_execution_cold_trace(request)

        async def resume_thread_target(self, request: Any) -> Any:
            return self._service.resume_thread_target(request)

        async def archive_thread_target(self, request: Any) -> Any:
            return self._service.archive_thread_target(request)

        async def set_thread_backend_id(self, request: Any) -> None:
            self._service.set_thread_backend_id(request)

        async def record_thread_activity(self, request: Any) -> None:
            self._service.record_thread_activity(request)

        async def update_thread_compact_seed(self, request: Any) -> Any:
            return self._service.update_thread_compact_seed(request)

        async def get_transcript_history(self, request: Any) -> Any:
            return self._service.get_transcript_history(request)

        async def write_transcript(self, request: Any) -> Any:
            return self._service.write_transcript(request)

        async def get_pma_snapshot(self) -> Any:
            return type(
                "PmaSnapshotResponse",
                (),
                {"snapshot": await build_hub_snapshot(None, hub_root=self._hub_root)},
            )()

        async def get_agent_workspace(self, request: Any) -> Any:
            return self._service.get_agent_workspace(request)

        async def list_agent_workspaces(self, request: Any) -> Any:
            return self._service.list_agent_workspaces(request)

        async def run_workspace_setup_commands(self, request: Any) -> Any:
            return self._service.run_workspace_setup_commands(request)

        async def request_automation(self, request: Any) -> Any:
            return self._service.request_automation(request)

        async def aclose(self) -> None:
            return None

    def _install_inprocess_hub_client(service: object, hub_root: Path) -> None:
        service._hub_client = _InProcessHubControlPlaneClient(hub_root)
        service._hub_handshake_compatibility = HandshakeCompatibility(
            state="compatible"
        )

    discord_init = DiscordBotService.__init__
    telegram_init = TelegramBotService.__init__

    def _discord_init(self: Any, *args: object, **kwargs: object) -> None:
        discord_init(self, *args, **kwargs)
        _install_inprocess_hub_client(self, Path(self._config.root))

    def _telegram_init(self: Any, *args: object, **kwargs: object) -> None:
        telegram_init(self, *args, **kwargs)
        _install_inprocess_hub_client(
            self, Path(getattr(self, "_hub_root", None) or self._config.root)
        )

    async def _discord_handshake_ok(self: Any) -> bool:
        _install_inprocess_hub_client(self, Path(self._config.root))
        return True

    async def _telegram_handshake_ok(self: Any) -> bool:
        _install_inprocess_hub_client(
            self,
            Path(getattr(self, "_hub_root", None) or self._config.root),
        )
        return True

    DiscordBotService.__init__ = _discord_init  # type: ignore[assignment]
    TelegramBotService.__init__ = _telegram_init  # type: ignore[assignment]
    DiscordBotService._perform_hub_handshake = _discord_handshake_ok  # type: ignore[assignment]
    TelegramBotService._perform_hub_handshake = _telegram_handshake_ok  # type: ignore[assignment]
    _HUB_PATCH_INSTALLED = True


__all__ = [
    "DEFAULT_ARTIFACT_DIR",
    "DEFAULT_LATENCY_SCENARIO_IDS",
    "LatencyBudgetSuiteResult",
    "apply_hermes_runtime_patch_for_lab",
    "available_latency_budget_scenario_ids",
    "format_suite_summary",
    "run_chat_surface_latency_budget_suite",
]
