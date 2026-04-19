from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.browser.runtime import BrowserRuntime
from codex_autorunner.core.pma_automation_store import PmaAutomationStore
from codex_autorunner.integrations.chat.ux_regression_contract import (
    CHAT_UX_LATENCY_BUDGETS,
)
from codex_autorunner.integrations.discord import message_turns as discord_message_turns
from codex_autorunner.integrations.telegram.adapter import TelegramUpdate
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as telegram_execution,
)
from codex_autorunner.surfaces.web.routes.pma_routes.managed_threads import (
    build_automation_routes,
)
from tests.chat_surface_integration.harness import (
    DEFAULT_DISCORD_CHANNEL_ID,
    DEFAULT_TELEGRAM_THREAD_ID,
    DiscordSurfaceHarness,
    FakeDiscordRest,
    FakeTelegramBot,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    build_telegram_message,
    drain_telegram_spawned_tasks,
)
from tests.chat_surface_lab.artifact_manifests import ArtifactManifest
from tests.chat_surface_lab.discord_simulator import DiscordSimulatorFaults
from tests.chat_surface_lab.scenario_models import RuntimeFixtureKind, SurfaceKind
from tests.chat_surface_lab.telegram_simulator import TelegramSimulatorFaults
from tests.chat_surface_lab.transcript_models import (
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptTimeline,
)

SCENARIO_CORPUS_ROOT = Path(__file__).resolve().parent / "scenarios"
_LATENCY_BUDGETS_MS = {entry.id: entry.max_ms for entry in CHAT_UX_LATENCY_BUDGETS}


@dataclass(frozen=True)
class ScenarioRuntimeSpec:
    kind: RuntimeFixtureKind
    scenario: str


@dataclass(frozen=True)
class ScenarioActionSpec:
    kind: str
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    delay_ms: int = 0
    surfaces: tuple[SurfaceKind, ...] = ()


@dataclass(frozen=True)
class ScenarioFaultSpec:
    kind: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)
    surfaces: tuple[SurfaceKind, ...] = ()


@dataclass(frozen=True)
class TranscriptInvariantSpec:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    surfaces: tuple[SurfaceKind, ...] = ()


@dataclass(frozen=True)
class LatencyBudgetAssertionSpec:
    budget_id: str
    event_name: str
    field: str
    surface: SurfaceKind
    max_ms: Optional[float] = None


@dataclass(frozen=True)
class ScenarioContractLinks:
    regression_ids: tuple[str, ...] = ()
    latency_budget_ids: tuple[str, ...] = ()
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpectedTerminalSpec:
    status: Optional[str] = None


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: str
    description: str
    surfaces: tuple[SurfaceKind, ...]
    runtime: ScenarioRuntimeSpec
    actions: tuple[ScenarioActionSpec, ...] = ()
    faults: tuple[ScenarioFaultSpec, ...] = ()
    transcript_invariants: tuple[TranscriptInvariantSpec, ...] = ()
    latency_budgets: tuple[LatencyBudgetAssertionSpec, ...] = ()
    expected_terminal: ExpectedTerminalSpec = field(
        default_factory=ExpectedTerminalSpec
    )
    contract_links: ScenarioContractLinks = field(default_factory=ScenarioContractLinks)
    tags: tuple[str, ...] = ()
    notes: str = ""
    execution_mode: str = "surface_harness"
    harness_timeouts: dict[SurfaceKind, float] = field(default_factory=dict)
    approval_mode: Optional[str] = None


@dataclass(frozen=True)
class ObservedLatencyBudget:
    budget_id: str
    surface: SurfaceKind
    event_name: str
    field: str
    observed_ms: float
    max_ms: float


@dataclass(frozen=True)
class ScenarioSurfaceRunResult:
    surface: SurfaceKind
    execution_status: Optional[str]
    execution_error: Optional[str]
    transcript: TranscriptTimeline
    artifact_manifest: ArtifactManifest
    log_records: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ScenarioRunResult:
    scenario_id: str
    output_dir: Path
    execution_mode: str
    skipped: bool
    surface_results: tuple[ScenarioSurfaceRunResult, ...] = ()
    observed_budgets: tuple[ObservedLatencyBudget, ...] = ()
    summary_path: Optional[Path] = None


@dataclass
class _SurfaceRunContext:
    surface: SurfaceKind
    runtime: HermesFixtureRuntime
    harness: DiscordSurfaceHarness | TelegramSurfaceHarness
    rest_client: Optional[FakeDiscordRest] = None
    bot_client: Optional[FakeTelegramBot] = None
    active_task: Optional[asyncio.Task[Any]] = None
    result: Optional[Any] = None
    thread_target_id: Optional[str] = None
    execution_id: Optional[str] = None
    telegram_thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID
    surface_metadata: dict[str, Any] = field(default_factory=dict)


class ChatSurfaceScenarioRunner:
    def __init__(
        self,
        *,
        output_root: Path,
        apply_runtime_patch: Optional[Callable[[HermesFixtureRuntime], None]] = None,
        browser_runtime: Optional[BrowserRuntime] = None,
    ) -> None:
        self._output_root = output_root
        self._apply_runtime_patch = apply_runtime_patch
        self._browser_runtime = browser_runtime or BrowserRuntime()

    async def run_scenario(self, scenario: ScenarioDefinition) -> ScenarioRunResult:
        scenario_output_dir = self._output_root / scenario.scenario_id
        scenario_output_dir.mkdir(parents=True, exist_ok=True)
        if scenario.execution_mode == "reference_only":
            summary_path = scenario_output_dir / "run_summary.json"
            summary_payload = {
                "scenario_id": scenario.scenario_id,
                "description": scenario.description,
                "execution_mode": scenario.execution_mode,
                "skipped": True,
                "reason": "reference_only scenario",
                "references": list(scenario.contract_links.references),
            }
            summary_path.write_text(
                json.dumps(summary_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return ScenarioRunResult(
                scenario_id=scenario.scenario_id,
                output_dir=scenario_output_dir,
                execution_mode=scenario.execution_mode,
                skipped=True,
                summary_path=summary_path,
            )

        surface_results: list[ScenarioSurfaceRunResult] = []
        observed_budgets: list[ObservedLatencyBudget] = []
        for surface in scenario.surfaces:
            surface_result = await self._run_surface(
                scenario=scenario,
                surface=surface,
                output_dir=scenario_output_dir / surface.value,
            )
            self._assert_expected_terminal_status(
                scenario=scenario,
                surface_result=surface_result,
            )
            self._assert_invariants(scenario=scenario, surface_result=surface_result)
            surface_budgets = self._assert_latency_budgets(
                scenario=scenario,
                surface_result=surface_result,
            )
            observed_budgets.extend(surface_budgets)
            surface_results.append(surface_result)

        summary_path = scenario_output_dir / "run_summary.json"
        summary_payload = {
            "scenario_id": scenario.scenario_id,
            "description": scenario.description,
            "execution_mode": scenario.execution_mode,
            "surfaces": [surface.value for surface in scenario.surfaces],
            "runtime": {
                "kind": scenario.runtime.kind.value,
                "scenario": scenario.runtime.scenario,
            },
            "contract_links": {
                "regression_ids": list(scenario.contract_links.regression_ids),
                "latency_budget_ids": list(scenario.contract_links.latency_budget_ids),
                "references": list(scenario.contract_links.references),
            },
            "surface_results": [
                {
                    "surface": result.surface.value,
                    "execution_status": result.execution_status,
                    "execution_error": result.execution_error,
                    "event_count": len(result.transcript.events),
                    "artifact_paths": [
                        str(record.path)
                        for record in result.artifact_manifest.artifacts
                    ],
                }
                for result in surface_results
            ],
            "observed_budgets": [
                {
                    "budget_id": budget.budget_id,
                    "surface": budget.surface.value,
                    "event_name": budget.event_name,
                    "field": budget.field,
                    "observed_ms": budget.observed_ms,
                    "max_ms": budget.max_ms,
                }
                for budget in observed_budgets
            ],
        }
        summary_path.write_text(
            json.dumps(summary_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return ScenarioRunResult(
            scenario_id=scenario.scenario_id,
            output_dir=scenario_output_dir,
            execution_mode=scenario.execution_mode,
            skipped=False,
            surface_results=tuple(surface_results),
            observed_budgets=tuple(observed_budgets),
            summary_path=summary_path,
        )

    async def _run_surface(
        self,
        *,
        scenario: ScenarioDefinition,
        surface: SurfaceKind,
        output_dir: Path,
    ) -> ScenarioSurfaceRunResult:
        if scenario.runtime.kind != RuntimeFixtureKind.HERMES:
            raise NotImplementedError(
                f"unsupported runtime kind: {scenario.runtime.kind.value}"
            )

        runtime = HermesFixtureRuntime(scenario.runtime.scenario)
        if self._apply_runtime_patch is not None:
            self._apply_runtime_patch(runtime)
        timeout_restorers = self._apply_timeout_faults(
            surface=surface,
            faults=scenario.faults,
        )
        harness = self._build_harness(
            scenario=scenario,
            surface=surface,
            output_dir=output_dir,
        )
        context = _SurfaceRunContext(
            surface=surface,
            runtime=runtime,
            harness=harness,
        )
        self._configure_fault_clients(context=context, scenario=scenario)
        try:
            await self._setup_harness(context=context, scenario=scenario)

            for action in scenario.actions:
                if action.surfaces and surface not in action.surfaces:
                    continue
                if action.delay_ms > 0:
                    await asyncio.sleep(max(action.delay_ms, 0) / 1000)
                await self._execute_action(
                    context=context,
                    scenario=scenario,
                    output_dir=output_dir,
                    action=action,
                )

            if context.result is None and context.active_task is not None:
                try:
                    context.result = await context.active_task
                except asyncio.CancelledError:
                    context.result = None
            await self._await_surface_settled(context)
            context.result = self._refresh_surface_result_metadata(context)
            if context.result is None:
                raise AssertionError(
                    f"scenario {scenario.scenario_id} produced no surface result for {surface.value}"
                )

            transcript = context.result.to_normalized_transcript(
                scenario_id=scenario.scenario_id,
                metadata=context.surface_metadata,
            )
            artifact_manifest = context.result.write_artifacts(
                output_dir=output_dir / "artifacts",
                scenario_id=scenario.scenario_id,
                run_id=f"{scenario.scenario_id}-{surface.value}",
                browser_runtime=self._browser_runtime,
            )
            log_records = tuple(getattr(context.result, "log_records", []) or [])
            return ScenarioSurfaceRunResult(
                surface=surface,
                execution_status=self._normalize_optional_text(
                    getattr(context.result, "execution_status", None)
                ),
                execution_error=self._normalize_optional_text(
                    getattr(context.result, "execution_error", None)
                ),
                transcript=transcript,
                artifact_manifest=artifact_manifest,
                log_records=log_records,
            )
        finally:
            if context.active_task is not None and not context.active_task.done():
                context.active_task.cancel()
                with contextlib.suppress(Exception):
                    await context.active_task
            await context.harness.close()
            await runtime.close()
            while timeout_restorers:
                restore = timeout_restorers.pop()
                restore()

    def _build_harness(
        self,
        *,
        scenario: ScenarioDefinition,
        surface: SurfaceKind,
        output_dir: Path,
    ) -> DiscordSurfaceHarness | TelegramSurfaceHarness:
        timeout_seconds = scenario.harness_timeouts.get(surface, 8.0)
        harness_root = output_dir / "harness_root"
        if surface == SurfaceKind.DISCORD:
            return DiscordSurfaceHarness(harness_root, timeout_seconds=timeout_seconds)
        if surface == SurfaceKind.TELEGRAM:
            return TelegramSurfaceHarness(harness_root, timeout_seconds=timeout_seconds)
        raise NotImplementedError(f"unsupported surface: {surface.value}")

    async def _setup_harness(
        self,
        *,
        context: _SurfaceRunContext,
        scenario: ScenarioDefinition,
    ) -> None:
        await context.harness.setup(
            agent="hermes",
            approval_mode=scenario.approval_mode,
        )

    def _resolve_current_thread_target_id(
        self,
        context: _SurfaceRunContext,
    ) -> Optional[str]:
        if context.thread_target_id:
            return context.thread_target_id
        if context.surface == SurfaceKind.DISCORD:
            assert isinstance(context.harness, DiscordSurfaceHarness)
            binding = context.harness.orchestration_service().get_binding(
                surface_kind="discord",
                surface_key=DEFAULT_DISCORD_CHANNEL_ID,
            )
        else:
            assert isinstance(context.harness, TelegramSurfaceHarness)
            binding = context.harness.orchestration_service().get_binding(
                surface_kind="telegram",
                surface_key=f"chat-1:{context.telegram_thread_id or DEFAULT_TELEGRAM_THREAD_ID}",
            )
        thread_target_id = (
            str(getattr(binding, "thread_target_id", "") or "").strip() or None
        )
        if thread_target_id:
            context.thread_target_id = thread_target_id
        return thread_target_id

    async def _await_surface_settled(self, context: _SurfaceRunContext) -> None:
        if context.surface != SurfaceKind.DISCORD:
            return
        assert isinstance(context.harness, DiscordSurfaceHarness)
        service = context.harness.service
        if service is None:
            return
        deadline = asyncio.get_running_loop().time() + context.harness.timeout_seconds
        while service._background_tasks:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "DiscordSurfaceHarness background tasks did not drain"
                )
            await asyncio.sleep(0.01)

    def _refresh_surface_result_metadata(
        self,
        context: _SurfaceRunContext,
    ) -> FakeDiscordRest | FakeTelegramBot | None:
        if context.surface == SurfaceKind.DISCORD:
            assert isinstance(context.harness, DiscordSurfaceHarness)
            rest = context.harness.rest
            if rest is None:
                if isinstance(context.result, FakeDiscordRest):
                    rest = context.result
                    context.harness.rest = rest
                else:
                    return None
            context.harness._apply_discord_runtime_metadata(rest)
            return rest
        assert isinstance(context.harness, TelegramSurfaceHarness)
        bot = context.harness.bot
        if bot is None:
            if isinstance(context.result, FakeTelegramBot):
                bot = context.result
                context.harness.bot = bot
            else:
                return None
        context.harness._apply_telegram_runtime_metadata(
            bot,
            thread_id=context.telegram_thread_id,
            message_start_index=0,
        )
        return bot

    def _configure_fault_clients(
        self,
        *,
        context: _SurfaceRunContext,
        scenario: ScenarioDefinition,
    ) -> None:
        active_faults = [
            fault
            for fault in scenario.faults
            if not fault.surfaces or context.surface in fault.surfaces
        ]
        if context.surface == SurfaceKind.DISCORD:
            fail_ids: set[str] = set()
            for fault in active_faults:
                if fault.kind != "fail_progress_delete":
                    continue
                message_id = str(fault.parameters.get("message_id") or "msg-1")
                fail_ids.add(message_id)
            retry_after_schedule = _collect_retry_after_schedule(active_faults)
            attachment_data_by_url = _collect_discord_attachment_data(scenario.actions)
            if fail_ids or retry_after_schedule or attachment_data_by_url:
                context.rest_client = FakeDiscordRest(
                    attachment_data_by_url=attachment_data_by_url,
                    faults=DiscordSimulatorFaults(
                        fail_delete_message_ids=fail_ids,
                        retry_after_schedule=retry_after_schedule,
                    ),
                )
            return
        fail_ids_int: set[int] = set()
        for fault in active_faults:
            if fault.kind != "fail_progress_delete":
                continue
            raw_message_id = fault.parameters.get("message_id", 1)
            try:
                message_id = int(raw_message_id)
            except (TypeError, ValueError):
                message_id = 1
            fail_ids_int.add(message_id)
        retry_after_schedule = _collect_retry_after_schedule(active_faults)
        if fail_ids_int or retry_after_schedule:
            context.bot_client = FakeTelegramBot(
                faults=TelegramSimulatorFaults(
                    fail_delete_message_ids=fail_ids_int,
                    retry_after_schedule=retry_after_schedule,
                )
            )

    async def _execute_action(
        self,
        *,
        context: _SurfaceRunContext,
        scenario: ScenarioDefinition,
        output_dir: Path,
        action: ScenarioActionSpec,
    ) -> None:
        if action.kind == "send_message":
            text = str(action.payload.get("text") or "").strip()
            if not text:
                raise AssertionError("send_message requires payload.text")
            if context.surface == SurfaceKind.DISCORD:
                assert isinstance(context.harness, DiscordSurfaceHarness)
                attachments = _build_discord_attachment_payloads(
                    action.payload.get("attachments")
                )
                context.result = await context.harness.run_message(
                    text,
                    attachments=attachments,
                    rest_client=context.rest_client,
                )
                return
            assert isinstance(context.harness, TelegramSurfaceHarness)
            context.telegram_thread_id = _optional_int(
                action.payload.get("thread_id"),
                default=DEFAULT_TELEGRAM_THREAD_ID,
            )
            context.result = await context.harness.run_message(
                text,
                thread_id=context.telegram_thread_id,
                bot_client=context.bot_client,
            )
            return

        if action.kind == "start_message":
            text = str(action.payload.get("text") or "").strip()
            if not text:
                raise AssertionError("start_message requires payload.text")
            if context.surface == SurfaceKind.DISCORD:
                assert isinstance(context.harness, DiscordSurfaceHarness)
                attachments = _build_discord_attachment_payloads(
                    action.payload.get("attachments")
                )
                context.active_task = context.harness.start_message(
                    text,
                    attachments=attachments,
                    rest_client=context.rest_client,
                )
                return
            assert isinstance(context.harness, TelegramSurfaceHarness)
            context.telegram_thread_id = _optional_int(
                action.payload.get("thread_id"),
                default=DEFAULT_TELEGRAM_THREAD_ID,
            )
            context.active_task = context.harness.start_message(
                text,
                thread_id=context.telegram_thread_id,
                bot_client=context.bot_client,
            )
            return

        if action.kind == "wait_for_running_execution":
            timeout_seconds = _optional_float(
                action.payload.get("timeout_seconds"),
                default=2.0,
            )
            if context.surface == SurfaceKind.DISCORD:
                assert isinstance(context.harness, DiscordSurfaceHarness)
                thread_target_id, execution_id = (
                    await context.harness.wait_for_running_execution(
                        timeout_seconds=timeout_seconds
                    )
                )
            else:
                assert isinstance(context.harness, TelegramSurfaceHarness)
                thread_target_id, execution_id = (
                    await context.harness.wait_for_running_execution(
                        timeout_seconds=timeout_seconds
                    )
                )
            context.thread_target_id = thread_target_id
            context.execution_id = execution_id
            return

        if action.kind == "submit_active_message":
            text = str(action.payload.get("text") or "").strip()
            if not text:
                raise AssertionError("submit_active_message requires payload.text")
            if context.surface == SurfaceKind.DISCORD:
                assert isinstance(context.harness, DiscordSurfaceHarness)
                attachments = _build_discord_attachment_payloads(
                    action.payload.get("attachments")
                )
                await context.harness.submit_active_message(
                    text,
                    attachments=attachments,
                    message_id=str(action.payload.get("message_id") or "m-2"),
                )
                return
            assert isinstance(context.harness, TelegramSurfaceHarness)
            context.telegram_thread_id = _optional_int(
                action.payload.get("thread_id"),
                default=DEFAULT_TELEGRAM_THREAD_ID,
            )
            await context.harness.submit_active_message(
                text,
                thread_id=context.telegram_thread_id,
                message_id=_optional_int(action.payload.get("message_id"), default=2),
                update_id=_optional_int(action.payload.get("update_id"), default=2),
            )
            return

        if action.kind == "wait_for_log_event":
            event_name = str(action.payload.get("event_name") or "").strip()
            if not event_name:
                raise AssertionError("wait_for_log_event requires payload.event_name")
            timeout_seconds = _optional_float(
                action.payload.get("timeout_seconds"),
                default=2.0,
            )
            field_name = self._normalize_optional_text(action.payload.get("field"))
            expected_value = action.payload.get("equals")
            if context.surface == SurfaceKind.DISCORD:
                assert isinstance(context.harness, DiscordSurfaceHarness)
                await context.harness.wait_for_log_event(
                    event_name,
                    timeout_seconds=timeout_seconds,
                    predicate=(
                        None
                        if field_name is None
                        else lambda record: record.get(field_name) == expected_value
                    ),
                )
            else:
                assert isinstance(context.harness, TelegramSurfaceHarness)
                await context.harness.wait_for_log_event(
                    event_name,
                    timeout_seconds=timeout_seconds,
                    predicate=(
                        None
                        if field_name is None
                        else lambda record: record.get(field_name) == expected_value
                    ),
                )
            return

        if action.kind == "create_automation_subscription":
            origin_thread_id = self._resolve_current_thread_target_id(context)
            if origin_thread_id is None:
                raise AssertionError(
                    "create_automation_subscription requires an active surface thread"
                )
            runtime_state = SimpleNamespace(
                pma_current={
                    "thread_id": origin_thread_id,
                    "lane_id": context.surface.value,
                }
            )
            with _build_automation_route_client(
                context.harness.root,
                runtime_state=runtime_state,
            ) as client:
                response = client.post(
                    "/subscriptions",
                    json=dict(action.payload),
                )
            assert (
                response.status_code == 200
            ), f"create_automation_subscription failed: {response.status_code} {response.text}"
            payload = response.json()
            subscription = payload.get("subscription") or {}
            delivery_target = (
                subscription.get("metadata", {}).get("delivery_target")
                if isinstance(subscription.get("metadata"), dict)
                else None
            ) or {}
            context.surface_metadata["subscription_lane_id"] = subscription.get(
                "lane_id"
            )
            context.surface_metadata["subscription_delivery_surface_kind"] = (
                delivery_target.get("surface_kind")
            )
            context.surface_metadata["subscription_delivery_surface_key"] = (
                delivery_target.get("surface_key")
            )
            context.surface_metadata["subscription_deduped"] = payload.get("deduped")
            return

        if action.kind == "emit_automation_transition":
            store = PmaAutomationStore(context.harness.root)
            payload = dict(action.payload)
            result = store.notify_transition(payload)
            pending = store.list_pending_wakeups(limit=10)
            event_type = self._normalize_optional_text(payload.get("event_type"))
            repo_id = self._normalize_optional_text(payload.get("repo_id"))
            run_id = self._normalize_optional_text(payload.get("run_id"))
            matching_wakeup = next(
                (
                    wakeup
                    for wakeup in reversed(pending)
                    if (
                        event_type is None
                        or self._normalize_optional_text(wakeup.get("event_type"))
                        == event_type
                    )
                    and (
                        repo_id is None
                        or self._normalize_optional_text(wakeup.get("repo_id"))
                        == repo_id
                    )
                    and (
                        run_id is None
                        or self._normalize_optional_text(wakeup.get("run_id")) == run_id
                    )
                ),
                None,
            )
            if matching_wakeup is None:
                raise AssertionError(
                    f"emit_automation_transition created no pending wakeup for {payload!r}"
                )
            wakeup_metadata = matching_wakeup.get("metadata")
            delivery_target = (
                wakeup_metadata.get("delivery_target")
                if isinstance(wakeup_metadata, dict)
                else None
            ) or {}
            context.surface_metadata["transition_created_wakeups"] = result.get(
                "created"
            )
            context.surface_metadata["wakeup_lane_id"] = matching_wakeup.get("lane_id")
            context.surface_metadata["wakeup_delivery_surface_kind"] = (
                delivery_target.get("surface_kind")
            )
            context.surface_metadata["wakeup_delivery_surface_key"] = (
                delivery_target.get("surface_key")
            )
            return

        if action.kind == "stop_active_thread":
            if context.thread_target_id is None:
                raise AssertionError(
                    "stop_active_thread requires thread_target_id from wait_for_running_execution"
                )
            await context.harness.orchestration_service().stop_thread(
                context.thread_target_id
            )
            return

        if action.kind == "interrupt_active_turn":
            if context.surface == SurfaceKind.DISCORD:
                assert isinstance(context.harness, DiscordSurfaceHarness)
                if context.thread_target_id is None or context.execution_id is None:
                    raise AssertionError(
                        "interrupt_active_turn for discord requires thread/execution ids"
                    )
                await context.harness.interrupt_active_turn_via_component(
                    thread_target_id=context.thread_target_id,
                    execution_id=context.execution_id,
                )
                return
            assert isinstance(context.harness, TelegramSurfaceHarness)
            context.telegram_thread_id = _optional_int(
                action.payload.get("thread_id"),
                default=DEFAULT_TELEGRAM_THREAD_ID,
            )
            await context.harness.interrupt_active_turn_via_callback(
                thread_id=context.telegram_thread_id,
            )
            return

        if action.kind == "queue_interrupt_send":
            if context.surface != SurfaceKind.DISCORD:
                return
            assert isinstance(context.harness, DiscordSurfaceHarness)
            await context.harness.queue_interrupt_send_via_component(
                source_message_id=str(action.payload.get("source_message_id") or "m-2"),
                interaction_id=str(
                    action.payload.get("interaction_id") or "queue-interrupt-1"
                ),
                interaction_token=str(
                    action.payload.get("interaction_token") or "queue-interrupt-token-1"
                ),
                user_id=str(action.payload.get("user_id") or "user-1"),
                message_id=self._normalize_optional_text(
                    action.payload.get("message_id")
                ),
                timeout_seconds=_optional_float(
                    action.payload.get("timeout_seconds"),
                    default=2.0,
                ),
            )
            return

        if action.kind == "await_active_message":
            if context.active_task is None:
                raise AssertionError("await_active_message requires an active task")
            try:
                context.result = await context.active_task
            except asyncio.CancelledError:
                context.result = self._refresh_surface_result_metadata(context)
            return

        if action.kind == "restart_surface_harness":
            if context.active_task is not None and not context.active_task.done():
                raise AssertionError(
                    "restart_surface_harness requires no active in-flight task"
                )
            await context.harness.close()
            context.thread_target_id = None
            context.execution_id = None
            context.result = None
            context.rest_client = None
            context.bot_client = None
            context.harness = self._build_harness(
                scenario=scenario,
                surface=context.surface,
                output_dir=output_dir,
            )
            self._configure_fault_clients(context=context, scenario=scenario)
            await self._setup_harness(context=context, scenario=scenario)
            return

        if action.kind == "run_status_interaction":
            if context.surface != SurfaceKind.DISCORD:
                return
            assert isinstance(context.harness, DiscordSurfaceHarness)
            interaction_id = str(action.payload.get("interaction_id") or "inter-1")
            rest_client = context.rest_client or FakeDiscordRest()
            payload = _discord_status_interaction(interaction_id)
            context.result = await context.harness.run_gateway_events(
                [("INTERACTION_CREATE", payload)],
                rest_client=rest_client,
            )
            return

        if action.kind == "run_duplicate_status_interaction":
            if context.surface != SurfaceKind.DISCORD:
                return
            assert isinstance(context.harness, DiscordSurfaceHarness)
            interaction_id = str(action.payload.get("interaction_id") or "inter-dup-1")
            rest_client = context.rest_client or FakeDiscordRest()
            rest_client.enable_duplicate_interaction(interaction_id)
            payload = _discord_status_interaction(interaction_id)
            events = [
                ("INTERACTION_CREATE", delivered)
                for delivered in rest_client.expand_interaction_delivery(payload)
            ]
            context.result = await context.harness.run_gateway_events(
                events,
                rest_client=rest_client,
            )
            return

        if action.kind == "run_status_update":
            if context.surface != SurfaceKind.TELEGRAM:
                return
            assert isinstance(context.harness, TelegramSurfaceHarness)
            if context.harness.service is None or context.harness.bot is None:
                raise AssertionError("Telegram harness is not initialized")
            update_id = _optional_int(action.payload.get("update_id"), default=77)
            thread_id = _optional_int(
                action.payload.get("thread_id"),
                default=DEFAULT_TELEGRAM_THREAD_ID,
            )
            context.telegram_thread_id = thread_id
            update = TelegramUpdate(
                update_id=update_id,
                message=build_telegram_message(
                    "/status",
                    thread_id=thread_id,
                    message_id=update_id,
                    update_id=update_id,
                ),
                callback=None,
            )
            await context.harness.service._dispatch_update(update)
            await drain_telegram_spawned_tasks(context.harness.service)
            context.harness._apply_telegram_runtime_metadata(
                context.harness.bot,
                thread_id=thread_id,
                message_start_index=0,
            )
            context.result = context.harness.bot
            return

        if action.kind == "run_duplicate_status_update":
            if context.surface != SurfaceKind.TELEGRAM:
                return
            assert isinstance(context.harness, TelegramSurfaceHarness)
            if context.harness.service is None or context.harness.bot is None:
                raise AssertionError("Telegram harness is not initialized")
            update_id = _optional_int(action.payload.get("update_id"), default=77)
            thread_id = _optional_int(
                action.payload.get("thread_id"),
                default=DEFAULT_TELEGRAM_THREAD_ID,
            )
            context.telegram_thread_id = thread_id
            update = TelegramUpdate(
                update_id=update_id,
                message=build_telegram_message(
                    "/status",
                    thread_id=thread_id,
                    message_id=update_id,
                    update_id=update_id,
                ),
                callback=None,
            )
            context.harness.bot.enable_duplicate_update(update_id)
            for delivered in context.harness.bot.expand_update_delivery(update):
                await context.harness.service._dispatch_update(delivered)
            await drain_telegram_spawned_tasks(context.harness.service)
            context.harness._apply_telegram_runtime_metadata(
                context.harness.bot,
                thread_id=thread_id,
                message_start_index=0,
            )
            context.result = context.harness.bot
            return

        raise AssertionError(f"unsupported scenario action: {action.kind}")

    def _assert_expected_terminal_status(
        self,
        *,
        scenario: ScenarioDefinition,
        surface_result: ScenarioSurfaceRunResult,
    ) -> None:
        expected_status = self._normalize_optional_text(
            scenario.expected_terminal.status
        )
        if expected_status is None:
            return
        actual = self._normalize_optional_text(surface_result.execution_status)
        assert (
            actual == expected_status
        ), f"{scenario.scenario_id}/{surface_result.surface.value}: expected terminal status {expected_status!r}, got {actual!r}"

    def _assert_invariants(
        self,
        *,
        scenario: ScenarioDefinition,
        surface_result: ScenarioSurfaceRunResult,
    ) -> None:
        transcript_events = list(surface_result.transcript.events)
        for invariant in scenario.transcript_invariants:
            if invariant.surfaces and surface_result.surface not in invariant.surfaces:
                continue
            if invariant.kind == "contains_text":
                needle = str(invariant.params.get("text") or "")
                if not needle:
                    raise AssertionError("contains_text invariant requires text")
                assert any(
                    needle in (event.text or "") for event in transcript_events
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: transcript missing text {needle!r}"
                continue

            if invariant.kind == "text_count_equals":
                needle = str(invariant.params.get("text") or "")
                expected = _optional_int(invariant.params.get("count"), default=0)
                actual = sum(
                    1 for event in transcript_events if needle in str(event.text or "")
                )
                assert (
                    actual == expected
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: text {needle!r} count {actual} != {expected}"
                continue

            if invariant.kind == "event_kind_at_least":
                kind = str(invariant.params.get("event_kind") or "").strip()
                minimum = _optional_int(invariant.params.get("min_count"), default=1)
                actual = sum(
                    1 for event in transcript_events if event.kind.value == kind
                )
                assert (
                    actual >= minimum
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: event kind {kind!r} count {actual} < {minimum}"
                continue

            if invariant.kind == "text_order":
                first_text = str(invariant.params.get("first_text") or "")
                second_text = str(invariant.params.get("second_text") or "")
                first_index = _first_text_index(transcript_events, first_text)
                second_index = _first_text_index(transcript_events, second_text)
                assert (
                    first_index is not None and second_index is not None
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: missing ordered texts"
                assert (
                    first_index < second_index
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: text order violated for {first_text!r} before {second_text!r}"
                continue

            if invariant.kind == "surface_attr_equals":
                attr = str(invariant.params.get("attr") or "").strip()
                expected = invariant.params.get("value")
                if not attr:
                    raise AssertionError("surface_attr_equals requires attr")
                actual = _lookup_surface_attr(
                    surface_result=surface_result,
                    attr=attr,
                )
                assert (
                    actual == expected
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: attr {attr!r} expected {expected!r}, got {actual!r}"
                continue

            if invariant.kind == "log_event_exists":
                event_name = str(invariant.params.get("event_name") or "").strip()
                field = self._normalize_optional_text(invariant.params.get("field"))
                expected = invariant.params.get("value")
                if not event_name:
                    raise AssertionError("log_event_exists requires event_name")
                assert any(
                    record.get("event") == event_name
                    and (field is None or record.get(field) == expected)
                    for record in surface_result.log_records
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: missing log event {event_name!r}"
                continue

            if invariant.kind == "log_event_field_contains":
                event_name = str(invariant.params.get("event_name") or "").strip()
                field = str(invariant.params.get("field") or "").strip()
                needle = str(invariant.params.get("contains") or "")
                if not event_name or not field:
                    raise AssertionError(
                        "log_event_field_contains requires event_name and field"
                    )
                matched = [
                    record
                    for record in surface_result.log_records
                    if record.get("event") == event_name
                    and needle in str(record.get(field) or "")
                ]
                assert (
                    matched
                ), f"{scenario.scenario_id}/{surface_result.surface.value}: missing log field {field!r} containing {needle!r}"
                continue

            raise AssertionError(f"unsupported invariant kind: {invariant.kind}")

    def _assert_latency_budgets(
        self,
        *,
        scenario: ScenarioDefinition,
        surface_result: ScenarioSurfaceRunResult,
    ) -> list[ObservedLatencyBudget]:
        observed: list[ObservedLatencyBudget] = []
        for assertion in scenario.latency_budgets:
            if assertion.surface != surface_result.surface:
                continue
            record = _latest_log_record_with_numeric_field(
                surface_result.log_records,
                event_name=assertion.event_name,
                field=assertion.field,
            )
            assert (
                record is not None
            ), f"{scenario.scenario_id}/{surface_result.surface.value}: missing timing event {assertion.event_name!r} field {assertion.field!r}"
            observed_ms = float(record[assertion.field])
            max_ms = assertion.max_ms
            if max_ms is None:
                max_ms = _LATENCY_BUDGETS_MS.get(assertion.budget_id)
            if max_ms is None:
                raise AssertionError(
                    f"{scenario.scenario_id}: unknown latency budget id {assertion.budget_id!r}"
                )
            assert (
                observed_ms <= max_ms
            ), f"{scenario.scenario_id}/{surface_result.surface.value}: latency {assertion.budget_id} {observed_ms}ms exceeds {max_ms}ms"
            observed.append(
                ObservedLatencyBudget(
                    budget_id=assertion.budget_id,
                    surface=assertion.surface,
                    event_name=assertion.event_name,
                    field=assertion.field,
                    observed_ms=observed_ms,
                    max_ms=max_ms,
                )
            )
        return observed

    def _apply_timeout_faults(
        self,
        *,
        surface: SurfaceKind,
        faults: tuple[ScenarioFaultSpec, ...],
    ) -> list[Callable[[], None]]:
        restorers: list[Callable[[], None]] = []
        for fault in faults:
            if fault.kind != "set_surface_timeout":
                continue
            if fault.surfaces and surface not in fault.surfaces:
                continue
            timeout_seconds = _optional_float(
                fault.parameters.get("seconds"),
                default=0.05,
            )
            if surface == SurfaceKind.DISCORD:
                previous = discord_message_turns.DISCORD_PMA_TIMEOUT_SECONDS
                discord_message_turns.DISCORD_PMA_TIMEOUT_SECONDS = timeout_seconds
                restorers.append(
                    lambda prev=previous: setattr(
                        discord_message_turns, "DISCORD_PMA_TIMEOUT_SECONDS", prev
                    )
                )
                continue
            if surface == SurfaceKind.TELEGRAM:
                previous = telegram_execution.TELEGRAM_PMA_TIMEOUT_SECONDS
                telegram_execution.TELEGRAM_PMA_TIMEOUT_SECONDS = timeout_seconds
                restorers.append(
                    lambda prev=previous: setattr(
                        telegram_execution, "TELEGRAM_PMA_TIMEOUT_SECONDS", prev
                    )
                )
        return restorers

    @staticmethod
    def _normalize_optional_text(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None


def iter_scenario_files(
    root: Path = SCENARIO_CORPUS_ROOT,
) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(path for path in root.rglob("*.json") if path.is_file()))


def load_scenario(path: Path) -> ScenarioDefinition:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: scenario JSON must be an object")

    scenario_id = _required_string(raw, "scenario_id", path=path)
    description = str(raw.get("description") or "").strip()
    runtime = _parse_runtime(raw.get("runtime_fixture"), path=path)
    surfaces = _parse_surface_values(raw.get("surfaces"), path=path, field="surfaces")
    actions = _parse_actions(raw.get("actions"), path=path)
    faults = _parse_faults(raw.get("faults"), path=path)
    invariants = _parse_invariants(raw.get("transcript_invariants"), path=path)
    budgets = _parse_budget_assertions(raw.get("latency_budgets"), path=path)
    expected_terminal = _parse_expected_terminal(raw.get("expected_terminal"))
    contract_links = _parse_contract_links(raw.get("contract_links"))
    tags = _parse_string_tuple(raw.get("tags"))
    notes = str(raw.get("notes") or "").strip()
    execution_mode = str(raw.get("execution_mode") or "surface_harness").strip()
    harness_timeouts = _parse_harness_timeouts(raw.get("harness_timeouts"), path=path)
    approval_mode = _normalize_optional_string(raw.get("approval_mode"))

    return ScenarioDefinition(
        scenario_id=scenario_id,
        description=description,
        surfaces=surfaces,
        runtime=runtime,
        actions=actions,
        faults=faults,
        transcript_invariants=invariants,
        latency_budgets=budgets,
        expected_terminal=expected_terminal,
        contract_links=contract_links,
        tags=tags,
        notes=notes,
        execution_mode=execution_mode,
        harness_timeouts=harness_timeouts,
        approval_mode=approval_mode,
    )


def load_scenario_by_id(
    scenario_id: str,
    *,
    root: Path = SCENARIO_CORPUS_ROOT,
) -> ScenarioDefinition:
    normalized_id = str(scenario_id or "").strip()
    if not normalized_id:
        raise ValueError("scenario_id must be non-empty")
    for path in iter_scenario_files(root):
        scenario = load_scenario(path)
        if scenario.scenario_id == normalized_id:
            return scenario
    raise FileNotFoundError(f"scenario id not found: {normalized_id}")


def load_scenario_corpus(
    root: Path = SCENARIO_CORPUS_ROOT,
) -> tuple[ScenarioDefinition, ...]:
    return tuple(load_scenario(path) for path in iter_scenario_files(root))


def _parse_runtime(raw: Any, *, path: Path) -> ScenarioRuntimeSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: runtime_fixture must be an object")
    kind_value = _required_string(raw, "kind", path=path)
    scenario = _required_string(raw, "scenario", path=path)
    try:
        kind = RuntimeFixtureKind(kind_value)
    except ValueError as exc:
        raise ValueError(
            f"{path}: unsupported runtime fixture kind {kind_value!r}"
        ) from exc
    return ScenarioRuntimeSpec(kind=kind, scenario=scenario)


def _parse_actions(raw: Any, *, path: Path) -> tuple[ScenarioActionSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path}: actions must be a list")
    actions: list[ScenarioActionSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each action must be an object")
        kind = _required_string(item, "kind", path=path)
        actor = str(item.get("actor") or "user")
        payload = item.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError(f"{path}: action payload for {kind} must be an object")
        delay_ms = _optional_int(item.get("delay_ms"), default=0)
        surfaces = _parse_surface_values(
            item.get("surfaces"),
            path=path,
            field=f"actions[{kind}].surfaces",
            required=False,
        )
        actions.append(
            ScenarioActionSpec(
                kind=kind,
                actor=actor,
                payload=dict(payload),
                delay_ms=delay_ms,
                surfaces=surfaces,
            )
        )
    return tuple(actions)


def _parse_faults(raw: Any, *, path: Path) -> tuple[ScenarioFaultSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path}: faults must be a list")
    faults: list[ScenarioFaultSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each fault must be an object")
        kind = _required_string(item, "kind", path=path)
        target = str(item.get("target") or "surface")
        parameters = item.get("parameters")
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ValueError(f"{path}: fault parameters for {kind} must be an object")
        surfaces = _parse_surface_values(
            item.get("surfaces"),
            path=path,
            field=f"faults[{kind}].surfaces",
            required=False,
        )
        faults.append(
            ScenarioFaultSpec(
                kind=kind,
                target=target,
                parameters=dict(parameters),
                surfaces=surfaces,
            )
        )
    return tuple(faults)


def _parse_invariants(raw: Any, *, path: Path) -> tuple[TranscriptInvariantSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path}: transcript_invariants must be a list")
    invariants: list[TranscriptInvariantSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each transcript invariant must be an object")
        kind = _required_string(item, "kind", path=path)
        params = item.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError(
                f"{path}: transcript invariant params for {kind} must be an object"
            )
        surfaces = _parse_surface_values(
            item.get("surfaces"),
            path=path,
            field=f"transcript_invariants[{kind}].surfaces",
            required=False,
        )
        invariants.append(
            TranscriptInvariantSpec(
                kind=kind,
                params=dict(params),
                surfaces=surfaces,
            )
        )
    return tuple(invariants)


def _parse_budget_assertions(
    raw: Any,
    *,
    path: Path,
) -> tuple[LatencyBudgetAssertionSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path}: latency_budgets must be a list")
    budgets: list[LatencyBudgetAssertionSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each latency budget assertion must be an object")
        budget_id = _required_string(item, "budget_id", path=path)
        event_name = _required_string(item, "event_name", path=path)
        field = _required_string(item, "field", path=path)
        surfaces = _parse_surface_values(
            item.get("surfaces"),
            path=path,
            field=f"latency_budgets[{budget_id}].surfaces",
            required=True,
        )
        if len(surfaces) != 1:
            raise ValueError(
                f"{path}: latency budget assertion {budget_id!r} must target exactly one surface"
            )
        max_ms = item.get("max_ms")
        budget_max_ms = _optional_float(max_ms, default=None)
        budgets.append(
            LatencyBudgetAssertionSpec(
                budget_id=budget_id,
                event_name=event_name,
                field=field,
                surface=surfaces[0],
                max_ms=budget_max_ms,
            )
        )
    return tuple(budgets)


def _parse_expected_terminal(raw: Any) -> ExpectedTerminalSpec:
    if raw is None:
        return ExpectedTerminalSpec(status=None)
    if not isinstance(raw, dict):
        raise ValueError("expected_terminal must be an object")
    status = _normalize_optional_string(raw.get("status"))
    return ExpectedTerminalSpec(status=status)


def _parse_contract_links(raw: Any) -> ScenarioContractLinks:
    if raw is None:
        return ScenarioContractLinks()
    if not isinstance(raw, dict):
        raise ValueError("contract_links must be an object")
    return ScenarioContractLinks(
        regression_ids=_parse_string_tuple(raw.get("regression_ids")),
        latency_budget_ids=_parse_string_tuple(raw.get("latency_budget_ids")),
        references=_parse_string_tuple(raw.get("references")),
    )


def _parse_harness_timeouts(raw: Any, *, path: Path) -> dict[SurfaceKind, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: harness_timeouts must be an object")
    parsed: dict[SurfaceKind, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"{path}: harness_timeouts keys must be surface strings")
        try:
            surface = SurfaceKind(key)
        except ValueError as exc:
            raise ValueError(f"{path}: unsupported surface {key!r}") from exc
        parsed[surface] = _optional_float(value, default=8.0)
    return parsed


def _parse_surface_values(
    raw: Any,
    *,
    path: Path,
    field: str,
    required: bool = True,
) -> tuple[SurfaceKind, ...]:
    if raw is None:
        if required:
            raise ValueError(f"{path}: {field} is required")
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path}: {field} must be a list")
    parsed: list[SurfaceKind] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{path}: {field} entries must be strings")
        try:
            parsed.append(SurfaceKind(item))
        except ValueError as exc:
            raise ValueError(f"{path}: unsupported surface {item!r}") from exc
    return tuple(parsed)


def _parse_string_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("expected a list of strings")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("expected string entries")
        stripped = item.strip()
        if stripped:
            values.append(stripped)
    return tuple(values)


def _required_string(raw: dict[str, Any], key: str, *, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{path}: {key} must be a non-empty string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{path}: {key} must be a non-empty string")
    return stripped


def _collect_retry_after_schedule(
    faults: Sequence[ScenarioFaultSpec],
) -> dict[str, list[int]]:
    schedule: dict[str, list[int]] = {}
    for fault in faults:
        if fault.kind != "retry_after":
            continue
        raw_schedule = fault.parameters.get("schedule")
        if isinstance(raw_schedule, dict):
            for raw_operation, raw_values in raw_schedule.items():
                operation = str(raw_operation or "").strip()
                if not operation:
                    continue
                values = _normalize_retry_after_values(raw_values)
                if values:
                    schedule.setdefault(operation, []).extend(values)
            continue
        operation = str(fault.parameters.get("operation") or "").strip()
        if not operation:
            continue
        raw_values = fault.parameters.get("seconds")
        if raw_values is None:
            raw_values = fault.parameters.get("retry_after")
        values = _normalize_retry_after_values(raw_values)
        if values:
            schedule.setdefault(operation, []).extend(values)
    return schedule


def _collect_discord_attachment_data(
    actions: Sequence[ScenarioActionSpec],
) -> dict[str, bytes]:
    data_by_url: dict[str, bytes] = {}
    for action in actions:
        attachments = _build_discord_attachment_payloads(
            action.payload.get("attachments")
        )
        for index, payload in enumerate(attachments, start=1):
            url = str(payload.get("url") or "").strip()
            if not url:
                continue
            body = payload.get("_fixture_body_bytes")
            if isinstance(body, bytes):
                data_by_url[url] = body
                continue
            data_by_url[url] = f"attachment-{index}".encode("utf-8")
    return data_by_url


def _build_discord_attachment_payloads(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AssertionError("discord attachment payloads must be a list")
    payloads: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise AssertionError("each discord attachment payload must be an object")
        attachment_id = str(item.get("id") or f"att-{index}").strip()
        filename = str(item.get("filename") or f"upload-{index}.txt").strip()
        content_type = str(
            item.get("content_type")
            or item.get("mime_type")
            or "application/octet-stream"
        ).strip()
        url = str(
            item.get("url")
            or f"https://cdn.discordapp.com/attachments/{attachment_id}/{filename}"
        ).strip()
        body_text = str(
            item.get("body_text")
            or item.get("text")
            or f"attachment fixture {attachment_id}"
        )
        body_bytes = body_text.encode("utf-8")
        payloads.append(
            {
                "id": attachment_id,
                "filename": filename,
                "content_type": content_type,
                "size": int(item.get("size") or len(body_bytes)),
                "url": url,
                "kind": str(item.get("kind") or "document"),
                "_fixture_body_bytes": body_bytes,
            }
        )
    return payloads


def _normalize_retry_after_values(raw: Any) -> list[int]:
    if isinstance(raw, list):
        source = raw
    elif raw is None:
        source = [1]
    else:
        source = [raw]

    normalized: list[int] = []
    for item in source:
        seconds = _optional_int(item, default=None)
        if seconds is None or seconds < 0:
            continue
        normalized.append(seconds)
    return normalized


def _optional_int(value: Any, *, default: Optional[int]) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any, *, default: Optional[float]) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_optional_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _latest_log_record_with_numeric_field(
    records: tuple[dict[str, Any], ...],
    *,
    event_name: str,
    field: str,
) -> Optional[dict[str, Any]]:
    for record in reversed(records):
        if record.get("event") != event_name:
            continue
        value = record.get(field)
        if isinstance(value, (int, float)):
            return record
    return None


def _first_text_index(events: list[TranscriptEvent], needle: str) -> Optional[int]:
    if not needle:
        return None
    for index, event in enumerate(events):
        if needle in str(event.text or ""):
            return index
    return None


def _lookup_surface_attr(
    *,
    surface_result: ScenarioSurfaceRunResult,
    attr: str,
) -> Any:
    if attr == "execution_status":
        return surface_result.execution_status
    if attr == "execution_error":
        return surface_result.execution_error
    if attr == "event_count":
        return len(surface_result.transcript.events)
    if attr == "has_delete_event":
        return any(
            event.kind == TranscriptEventKind.DELETE
            for event in surface_result.transcript.events
        )
    if attr == "has_edit_event":
        return any(
            event.kind == TranscriptEventKind.EDIT
            for event in surface_result.transcript.events
        )
    if attr == "has_callback_event":
        return any(
            event.kind == TranscriptEventKind.CALLBACK
            for event in surface_result.transcript.events
        )
    if attr in surface_result.transcript.metadata:
        return surface_result.transcript.metadata.get(attr)
    raise AssertionError(f"unsupported surface attribute assertion: {attr}")


def _build_automation_route_client(
    hub_root: Path,
    *,
    runtime_state: object,
) -> TestClient:
    app = FastAPI()
    app.state.config = SimpleNamespace(root=hub_root, raw={"pma": {"enabled": True}})
    router = app.router
    build_automation_routes(router, lambda: runtime_state)
    return TestClient(app)


def _discord_status_interaction(interaction_id: str) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "token": f"{interaction_id}-token",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [{"type": 1, "name": "status", "options": []}],
        },
    }


__all__ = [
    "ChatSurfaceScenarioRunner",
    "ExpectedTerminalSpec",
    "LatencyBudgetAssertionSpec",
    "ObservedLatencyBudget",
    "SCENARIO_CORPUS_ROOT",
    "ScenarioActionSpec",
    "ScenarioContractLinks",
    "ScenarioDefinition",
    "ScenarioFaultSpec",
    "ScenarioRunResult",
    "ScenarioRuntimeSpec",
    "ScenarioSurfaceRunResult",
    "TranscriptInvariantSpec",
    "iter_scenario_files",
    "load_scenario",
    "load_scenario_by_id",
    "load_scenario_corpus",
]
