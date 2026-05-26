from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Mapping, Optional

from ..runtime_identity import (
    RUNTIME_STAGE_EFFECTIVE,
    RUNTIME_STAGE_LAUNCH,
    RUNTIME_STAGE_PROJECTED,
    RUNTIME_STAGE_REQUESTED,
    RUNTIME_STAGE_RESOLVED,
    RuntimeIdentityContractError,
    RuntimeIdentityEnvelope,
    RuntimeIdentityStage,
)
from ..text_utils import _json_loads_object
from ..time_utils import now_iso
from .chat_surface_read_model import ChatSurfaceReadService
from .sqlite import open_orchestration_sqlite

RUNTIME_CHAIN_DRIFT = "RUNTIME_CHAIN_DRIFT"
RUNTIME_CHAIN_PROJECTED_UNKNOWN = "RUNTIME_CHAIN_PROJECTED_UNKNOWN"
RUNTIME_CHAIN_PROJECTION_PROVENANCE_MISSING = (
    "RUNTIME_CHAIN_PROJECTION_PROVENANCE_MISSING"
)
RUNTIME_CHAIN_AUTOMATION_MISMATCH_UNREFLECTED = (
    "RUNTIME_CHAIN_AUTOMATION_MISMATCH_UNREFLECTED"
)

_STAGE_ORDER = (
    RUNTIME_STAGE_REQUESTED,
    RUNTIME_STAGE_RESOLVED,
    RUNTIME_STAGE_LAUNCH,
    RUNTIME_STAGE_EFFECTIVE,
    RUNTIME_STAGE_PROJECTED,
)
_ACTIVE_STATUSES = {"queued", "pending", "claimed", "running", "in_progress", "started"}


@dataclasses.dataclass(frozen=True)
class RuntimeChainFinding:
    code: str
    severity: str
    message: str
    field: Optional[str] = None
    expected_stage: Optional[str] = None
    actual_stage: Optional[str] = None
    expected: Any = None
    actual: Any = None
    explanation: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in dataclasses.asdict(self).items()
            if value is not None
        }


@dataclasses.dataclass(frozen=True)
class RuntimeChainReport:
    lookup: dict[str, Any]
    row_identity: dict[str, Any]
    stages: dict[str, Optional[dict[str, Any]]]
    findings: tuple[RuntimeChainFinding, ...]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lookup": self.lookup,
            "row_identity": self.row_identity,
            "stages": self.stages,
            "findings": [finding.to_dict() for finding in self.findings],
            "generated_at": self.generated_at,
        }


def build_runtime_chain_diagnostic(
    hub_root: Path,
    *,
    managed_thread_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    automation_job_id: Optional[str] = None,
    automation_child_edge_id: Optional[str] = None,
    durable: bool = True,
) -> RuntimeChainReport:
    lookup = {
        "managed_thread_id": _clean(managed_thread_id),
        "chat_id": _clean(chat_id),
        "execution_id": _clean(execution_id),
        "automation_job_id": _clean(automation_job_id),
        "automation_child_edge_id": _clean(automation_child_edge_id),
    }
    stages: dict[str, Optional[RuntimeIdentityStage]] = {
        stage_name: None for stage_name in _STAGE_ORDER
    }
    identities: dict[str, Any] = {}
    findings: list[RuntimeChainFinding] = []

    with open_orchestration_sqlite(hub_root, durable=durable, migrate=True) as conn:
        edge = _edge_for_lookup(
            conn,
            edge_id=lookup["automation_child_edge_id"],
            job_id=lookup["automation_job_id"],
            execution_id=lookup["execution_id"],
        )
        if edge is not None:
            identities.update(_edge_identity(edge))
            _merge_stages(stages, _envelope_from_row(edge), prefer_existing=False)
            _merge_edge_runtime_fallbacks(stages, edge)
            if lookup["automation_job_id"] is None:
                lookup["automation_job_id"] = _clean(edge.get("parent_job_id"))
            if lookup["automation_child_edge_id"] is None:
                lookup["automation_child_edge_id"] = _clean(edge.get("edge_id"))
            if lookup["execution_id"] is None and _clean(edge.get("child_kind")) in {
                "agent_task",
                "managed_thread",
            }:
                lookup["execution_id"] = _clean(edge.get("child_id"))

        job = _job_for_lookup(conn, job_id=lookup["automation_job_id"])
        if job is not None:
            identities.update(_job_identity(job))
            if lookup["automation_job_id"] is None:
                lookup["automation_job_id"] = _clean(job.get("job_id"))
            _merge_job_requested_runtime(stages, job)

        execution = _execution_for_lookup(
            conn,
            execution_id=lookup["execution_id"],
            managed_thread_id=lookup["managed_thread_id"] or lookup["chat_id"],
        )
        if execution is not None:
            identities.update(_execution_identity(execution))
            lookup["execution_id"] = lookup["execution_id"] or _clean(
                execution.get("execution_id")
            )
            lookup["managed_thread_id"] = lookup["managed_thread_id"] or _clean(
                execution.get("thread_target_id")
            )
            _merge_stages(stages, _envelope_from_row(execution), prefer_existing=False)

        thread = _thread_for_lookup(
            conn,
            managed_thread_id=lookup["managed_thread_id"] or lookup["chat_id"],
            execution=execution,
        )
        if thread is not None:
            identities.update(_thread_identity(thread))
            lookup["managed_thread_id"] = lookup["managed_thread_id"] or _clean(
                thread.get("thread_target_id")
            )

    projection_row = _projected_chat_row(
        hub_root,
        managed_thread_id=lookup["managed_thread_id"],
        chat_id=lookup["chat_id"],
        durable=durable,
    )
    if projection_row is not None:
        identities.update(_projection_identity(projection_row))
        stages[RUNTIME_STAGE_PROJECTED] = _projected_stage(projection_row)

    findings.extend(_chain_findings(stages, identities))
    if edge is not None:
        findings.extend(_automation_mismatch_reflection_findings(edge, findings))

    return RuntimeChainReport(
        lookup={key: value for key, value in lookup.items() if value is not None},
        row_identity={
            key: value for key, value in identities.items() if value is not None
        },
        stages={
            stage_name: stage.to_dict() if stage is not None else None
            for stage_name, stage in stages.items()
        },
        findings=tuple(findings),
        generated_at=now_iso(),
    )


def collect_runtime_chain_invariant_diagnostics(
    hub_root: Path,
    *,
    durable: bool = True,
    limit: int = 100,
) -> tuple[RuntimeChainReport, ...]:
    reports: list[RuntimeChainReport] = []
    with open_orchestration_sqlite(hub_root, durable=durable, migrate=True) as conn:
        rows = conn.execute(
            """
            SELECT execution_id, thread_target_id
              FROM orch_thread_executions
             WHERE LOWER(status) IN ('queued', 'pending', 'claimed', 'running', 'in_progress', 'started')
             ORDER BY created_at DESC, execution_id ASC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    for row in rows:
        report = build_runtime_chain_diagnostic(
            hub_root,
            execution_id=str(row["execution_id"]),
            managed_thread_id=str(row["thread_target_id"]),
            durable=durable,
        )
        if report.findings:
            reports.append(report)
    return tuple(reports)


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _edge_for_lookup(
    conn: Any,
    *,
    edge_id: Optional[str],
    job_id: Optional[str],
    execution_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if edge_id:
        row = conn.execute(
            "SELECT * FROM orch_automation_child_execution_edges WHERE edge_id = ?",
            (edge_id,),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    if job_id:
        row = conn.execute(
            """
            SELECT * FROM orch_automation_child_execution_edges
             WHERE parent_job_id = ?
             ORDER BY authoritative_for_parent_completion DESC, updated_at DESC, edge_id ASC
             LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    if execution_id:
        row = conn.execute(
            """
            SELECT * FROM orch_automation_child_execution_edges
             WHERE child_id = ?
             ORDER BY updated_at DESC, edge_id ASC
             LIMIT 1
            """,
            (execution_id,),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    return None


def _job_for_lookup(conn: Any, *, job_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not job_id:
        return None
    row = conn.execute(
        "SELECT * FROM orch_automation_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return _row_dict(row) if row is not None else None


def _execution_for_lookup(
    conn: Any,
    *,
    execution_id: Optional[str],
    managed_thread_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if execution_id:
        row = conn.execute(
            "SELECT * FROM orch_thread_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    if managed_thread_id:
        row = conn.execute(
            """
            SELECT * FROM orch_thread_executions
             WHERE thread_target_id = ?
             ORDER BY created_at DESC, execution_id ASC
             LIMIT 1
            """,
            (managed_thread_id,),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    return None


def _thread_for_lookup(
    conn: Any,
    *,
    managed_thread_id: Optional[str],
    execution: Optional[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    thread_id = managed_thread_id or (execution or {}).get("thread_target_id")
    if not thread_id:
        return None
    row = conn.execute(
        "SELECT * FROM orch_thread_targets WHERE thread_target_id = ?",
        (str(thread_id),),
    ).fetchone()
    return _row_dict(row) if row is not None else None


def _envelope_from_row(row: Mapping[str, Any]) -> RuntimeIdentityEnvelope:
    raw = row.get("runtime_identity_json")
    text = _clean(raw)
    if text is None:
        return RuntimeIdentityEnvelope()
    try:
        return RuntimeIdentityEnvelope.from_json(text)
    except RuntimeIdentityContractError:
        return RuntimeIdentityEnvelope(
            metadata={"unknown_reason": "invalid_runtime_identity_json"}
        )


def _merge_stages(
    stages: dict[str, Optional[RuntimeIdentityStage]],
    envelope: RuntimeIdentityEnvelope,
    *,
    prefer_existing: bool,
) -> None:
    for stage_name in _STAGE_ORDER:
        stage = getattr(envelope, stage_name)
        if stage is None:
            continue
        if prefer_existing and stages.get(stage_name) is not None:
            continue
        stages[stage_name] = stage


def _stage_from_automation_runtime(
    payload: Any, *, stage: str, source: str
) -> Optional[RuntimeIdentityStage]:
    if not isinstance(payload, Mapping):
        return None
    try:
        base = RuntimeIdentityStage.from_automation_runtime(payload, stage=stage)
        return RuntimeIdentityStage(
            **{
                **base.to_dict(),
                "stage": stage,
                "source": source,
                "provenance": {
                    **base.provenance,
                    "compatibility_source": source,
                },
            }
        )
    except RuntimeIdentityContractError:
        return None


def _merge_edge_runtime_fallbacks(
    stages: dict[str, Optional[RuntimeIdentityStage]],
    edge: Mapping[str, Any],
) -> None:
    requested = _stage_from_automation_runtime(
        _json_loads_object(edge.get("requested_runtime_json")),
        stage=RUNTIME_STAGE_REQUESTED,
        source="automation_child_edge.requested_runtime_json",
    )
    actual = _stage_from_automation_runtime(
        _json_loads_object(edge.get("actual_runtime_json")),
        stage=RUNTIME_STAGE_EFFECTIVE,
        source="automation_child_edge.actual_runtime_json",
    )
    if stages[RUNTIME_STAGE_REQUESTED] is None and requested is not None:
        stages[RUNTIME_STAGE_REQUESTED] = requested
    if stages[RUNTIME_STAGE_EFFECTIVE] is None and actual is not None:
        stages[RUNTIME_STAGE_EFFECTIVE] = actual


def _merge_job_requested_runtime(
    stages: dict[str, Optional[RuntimeIdentityStage]], job: Mapping[str, Any]
) -> None:
    executor = _json_loads_object(job.get("executor_json") or job.get("executor"))
    requested = (
        executor.get("requested_runtime") if isinstance(executor, dict) else None
    )
    stage = _stage_from_automation_runtime(
        requested,
        stage=RUNTIME_STAGE_REQUESTED,
        source="automation_job.executor.requested_runtime",
    )
    if stages[RUNTIME_STAGE_REQUESTED] is None and stage is not None:
        stages[RUNTIME_STAGE_REQUESTED] = stage


def _projected_chat_row(
    hub_root: Path,
    *,
    managed_thread_id: Optional[str],
    chat_id: Optional[str],
    durable: bool,
) -> Optional[dict[str, Any]]:
    if not managed_thread_id and not chat_id:
        return None
    try:
        snapshot = ChatSurfaceReadService(
            hub_root, durable=durable
        ).chat_index_snapshot(view="all", limit=200)
    except Exception:
        return None
    for row in snapshot.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if managed_thread_id and row.get("managed_thread_id") == managed_thread_id:
            return row
        if chat_id and row.get("chat_id") == chat_id:
            return row
    return None


def _projected_stage(row: Mapping[str, Any]) -> RuntimeIdentityStage:
    runtime_raw = row.get("runtime")
    runtime: Mapping[str, Any] = runtime_raw if isinstance(runtime_raw, Mapping) else {}
    source = _clean(row.get("runtime_source")) or _clean(runtime.get("runtime_source"))
    return RuntimeIdentityStage(
        stage=RUNTIME_STAGE_PROJECTED,
        logical_agent=row.get("agent") or runtime.get("agent"),
        provider_id=runtime.get("provider_id"),
        canonical_model_label=row.get("model") or runtime.get("model"),
        provider_model_id=runtime.get("provider_model_id"),
        profile=row.get("agent_profile") or runtime.get("profile"),
        reasoning=row.get("reasoning") or runtime.get("reasoning"),
        backend_runtime_id=runtime.get("backend_runtime_id"),
        source=source or "unknown",
        provenance={
            "chat_id": row.get("chat_id"),
            "managed_thread_id": row.get("managed_thread_id"),
            "row_id": row.get("row_id"),
            "runtime_source": source,
            "model_source": row.get("model_source") or runtime.get("model_source"),
            "reasoning_source": row.get("reasoning_source")
            or runtime.get("reasoning_source"),
        },
        metadata={
            "effective_status": row.get("effective_status"),
            "status": row.get("status"),
            "model_unknown": runtime.get("model_unknown"),
            "agent_unknown": runtime.get("agent_unknown"),
            "runtime_stage": runtime.get("stage"),
        },
    )


def _identity_value(stage: RuntimeIdentityStage, field: str) -> Any:
    if field == "agent":
        return stage.logical_agent or stage.runtime_agent
    if field == "model":
        return stage.canonical_model_label
    if field == "reasoning":
        return stage.reasoning
    return getattr(stage, field)


def _chain_findings(
    stages: Mapping[str, Optional[RuntimeIdentityStage]],
    identities: Mapping[str, Any],
) -> list[RuntimeChainFinding]:
    findings: list[RuntimeChainFinding] = []
    present = [(name, stages.get(name)) for name in _STAGE_ORDER if stages.get(name)]
    for field in ("agent", "model", "reasoning"):
        previous_name: Optional[str] = None
        previous_value: Any = None
        for stage_name, stage in present:
            assert stage is not None
            value = _identity_value(stage, field)
            if value is None:
                continue
            if previous_value is not None and value != previous_value:
                findings.append(
                    RuntimeChainFinding(
                        code=RUNTIME_CHAIN_DRIFT,
                        severity="error",
                        field=field,
                        expected_stage=previous_name,
                        actual_stage=stage_name,
                        expected=previous_value,
                        actual=value,
                        message=(
                            f"Runtime {field} changed from {previous_value!r} "
                            f"at {previous_name} to {value!r} at {stage_name}."
                        ),
                    )
                )
            previous_name = stage_name
            previous_value = value

    projected = stages.get(RUNTIME_STAGE_PROJECTED)
    has_launch_or_effective = any(
        stages.get(name) is not None
        for name in (RUNTIME_STAGE_LAUNCH, RUNTIME_STAGE_EFFECTIVE)
    )
    status = _clean(
        identities.get("execution_status") or identities.get("runtime_status")
    )
    if (
        has_launch_or_effective
        and (status or "").lower() in _ACTIVE_STATUSES
        and (projected is None or projected.source == "unknown")
    ):
        findings.append(
            RuntimeChainFinding(
                code=RUNTIME_CHAIN_PROJECTED_UNKNOWN,
                severity="error",
                actual_stage=RUNTIME_STAGE_PROJECTED,
                message=(
                    "Active execution has persisted launch/effective runtime facts "
                    "but the projected runtime source is unknown."
                ),
                explanation="historical rows may be unknown only when no persisted launch/effective facts exist",
            )
        )
    if projected is not None:
        model_source = _clean(projected.provenance.get("model_source"))
        runtime_source = _clean(projected.provenance.get("runtime_source"))
        if model_source is None or runtime_source is None:
            findings.append(
                RuntimeChainFinding(
                    code=RUNTIME_CHAIN_PROJECTION_PROVENANCE_MISSING,
                    severity="error",
                    actual_stage=RUNTIME_STAGE_PROJECTED,
                    message="Chat projection is missing runtime provenance fields.",
                )
            )
    return findings


def _automation_mismatch_reflection_findings(
    edge: Mapping[str, Any], findings: list[RuntimeChainFinding]
) -> list[RuntimeChainFinding]:
    requested = _json_loads_object(edge.get("requested_runtime_json"))
    actual = _json_loads_object(edge.get("actual_runtime_json"))
    if not requested or not actual:
        return []
    unreflected: list[RuntimeChainFinding] = []
    reflected_fields = {
        finding.field for finding in findings if finding.code == RUNTIME_CHAIN_DRIFT
    }
    for field in ("agent", "model", "profile", "reasoning"):
        req = requested.get(field)
        act = actual.get(field)
        diagnostic_field = (
            "agent" if field == "agent" else "model" if field == "model" else field
        )
        if req and act and req != act and diagnostic_field not in reflected_fields:
            unreflected.append(
                RuntimeChainFinding(
                    code=RUNTIME_CHAIN_AUTOMATION_MISMATCH_UNREFLECTED,
                    severity="error",
                    field=field,
                    expected_stage=RUNTIME_STAGE_REQUESTED,
                    actual_stage=RUNTIME_STAGE_EFFECTIVE,
                    expected=req,
                    actual=act,
                    message=(
                        "Automation child-edge requested/actual runtime mismatch "
                        "was not reflected in runtime-chain drift findings."
                    ),
                )
            )
    return unreflected


def _edge_identity(edge: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "automation_child_edge_id": edge.get("edge_id"),
        "automation_job_id": edge.get("parent_job_id"),
        "child_kind": edge.get("child_kind"),
        "child_id": edge.get("child_id"),
        "child_terminal_state": edge.get("terminal_state"),
        "child_terminal_observed_at": edge.get("terminal_observed_at"),
        "edge_created_at": edge.get("created_at"),
        "edge_updated_at": edge.get("updated_at"),
    }


def _job_identity(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "automation_job_id": job.get("job_id"),
        "automation_rule_id": job.get("rule_id"),
        "automation_job_state": job.get("state"),
        "job_created_at": job.get("created_at"),
        "job_updated_at": job.get("updated_at"),
    }


def _execution_identity(execution: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_id": execution.get("execution_id"),
        "thread_target_id": execution.get("thread_target_id"),
        "backend_turn_id": execution.get("backend_turn_id"),
        "execution_status": execution.get("status"),
        "execution_created_at": execution.get("created_at"),
        "execution_started_at": execution.get("started_at"),
        "execution_finished_at": execution.get("finished_at"),
    }


def _thread_identity(thread: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "thread_target_id": thread.get("thread_target_id"),
        "backend_thread_id": thread.get("backend_thread_id"),
        "runtime_status": thread.get("runtime_status"),
        "thread_lifecycle_status": thread.get("lifecycle_status"),
        "thread_created_at": thread.get("created_at"),
        "thread_updated_at": thread.get("updated_at"),
    }


def _projection_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "projection_row_id": row.get("row_id"),
        "chat_id": row.get("chat_id"),
        "projection_status": row.get("status"),
        "projection_effective_status": row.get("effective_status"),
        "projection_updated_at": row.get("updated_at"),
        "projection_last_activity_at": row.get("last_activity_at"),
    }


__all__ = [
    "RUNTIME_CHAIN_AUTOMATION_MISMATCH_UNREFLECTED",
    "RUNTIME_CHAIN_DRIFT",
    "RUNTIME_CHAIN_PROJECTED_UNKNOWN",
    "RUNTIME_CHAIN_PROJECTION_PROVENANCE_MISSING",
    "RuntimeChainFinding",
    "RuntimeChainReport",
    "build_runtime_chain_diagnostic",
    "collect_runtime_chain_invariant_diagnostics",
]
