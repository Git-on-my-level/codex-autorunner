from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..automation.migration_diagnostics import (
    AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE,
    collect_automation_migration_read_model,
)
from ..automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    LEGACY_EXECUTOR_KINDS,
    AutomationChildExecutionEdge,
)
from ..automation.store import AutomationStore
from ..config import HubConfig
from ..orchestration.sqlite import open_orchestration_sqlite
from ..sqlite_utils import table_exists
from .types import DoctorCheck

AUTOMATION_PARENT_STATE_STALE = "AUTOMATION_PARENT_STATE_STALE"
AUTOMATION_RUNTIME_MISMATCH = "AUTOMATION_RUNTIME_MISMATCH"
AUTOMATION_LEGACY_EXECUTOR_SHAPE = "AUTOMATION_LEGACY_EXECUTOR_SHAPE"
AUTOMATION_CHILD_EDGE_MISSING = "AUTOMATION_CHILD_EDGE_MISSING"
AUTOMATION_LEGACY_CHILD_COLUMN_POPULATED = "AUTOMATION_LEGACY_CHILD_COLUMN_POPULATED"

_LEGACY_JOB_CHILD_COLUMNS = (
    "managed_thread_target_id",
    "managed_thread_execution_id",
    "pma_lane_id",
    "pma_queue_item_id",
    "ticket_flow_repo_id",
    "ticket_flow_run_id",
    "ticket_flow_worktree_id",
    "publish_operation_id",
)


@dataclass(frozen=True)
class AutomationArchitectureDiagnostic:
    code: str
    message: str
    severity: str = "error"
    rule_id: str | None = None
    job_id: str | None = None
    child_id: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.rule_id is not None:
            payload["rule_id"] = self.rule_id
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.child_id is not None:
            payload["child_id"] = self.child_id
        if self.field is not None:
            payload["field"] = self.field
        return payload


def automation_migration_doctor_checks(
    hub_config: HubConfig,
    *,
    repo_root: Path | None = None,
) -> list[DoctorCheck]:
    _ = repo_root
    report = collect_automation_migration_read_model(hub_config.root)
    architecture_checks = automation_architecture_doctor_checks(hub_config)
    if report.status == "complete":
        return [
            DoctorCheck(
                name="Automation migration",
                passed=True,
                message=(
                    "Automation migration gate OK "
                    f"(schema={report.schema_version}/{report.target_schema_version}, "
                    f"mirror={report.mirror_health.status})"
                ),
                check_id="automation.migration",
                severity="info",
            )
        ] + architecture_checks
    codes = ", ".join(item.code for item in report.diagnostics[:5])
    if len(report.diagnostics) > 5:
        codes += f", +{len(report.diagnostics) - 5} more"
    return [
        DoctorCheck(
            name="Automation migration",
            passed=False,
            message=f"Automation migration blocked: {codes}",
            check_id="automation.migration",
            fix=(
                report.next_steps[0]
                if report.next_steps
                else "Run hub orchestration status --json and inspect automation_migration diagnostics."
            ),
        )
    ] + architecture_checks


def automation_architecture_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    diagnostics = collect_automation_architecture_diagnostics(hub_config.root)
    if not diagnostics:
        return [
            DoctorCheck(
                name="Automation architecture invariants",
                passed=True,
                message="Automation execution graph invariants OK",
                check_id="automation.architecture",
                severity="info",
            )
        ]
    codes = ", ".join(item.code for item in diagnostics[:5])
    if len(diagnostics) > 5:
        codes += f", +{len(diagnostics) - 5} more"
    return [
        DoctorCheck(
            name="Automation architecture invariants",
            passed=False,
            message=f"Automation architecture invariant violations: {codes}",
            check_id="automation.architecture",
            fix="Inspect hub orchestration status --json and repair the reported automation execution graph rows.",
        )
    ]


def collect_automation_architecture_diagnostics(
    hub_root: Path, *, durable: bool = True
) -> tuple[AutomationArchitectureDiagnostic, ...]:
    store = AutomationStore(hub_root, durable=durable)
    diagnostics: list[AutomationArchitectureDiagnostic] = []
    rules_by_id = {rule.rule_id: rule for rule in store.list_rules()}

    diagnostics.extend(_legacy_child_column_diagnostics(hub_root, durable=durable))

    for rule in rules_by_id.values():
        if rule.executor_kind in LEGACY_EXECUTOR_KINDS:
            diagnostics.append(
                AutomationArchitectureDiagnostic(
                    code=AUTOMATION_LEGACY_EXECUTOR_SHAPE,
                    rule_id=rule.rule_id,
                    message=(
                        f"Rule {rule.rule_id} still uses legacy executor "
                        f"{rule.executor_kind!r}."
                    ),
                )
            )

    for job in store.list_jobs(order="newest"):
        job_rule = rules_by_id.get(job.rule_id)
        job_kind = str(job.executor.get("kind") or "").strip()
        rule_kind = job_rule.executor_kind if job_rule is not None else None
        if job_kind in LEGACY_EXECUTOR_KINDS or rule_kind in LEGACY_EXECUTOR_KINDS:
            diagnostics.append(
                AutomationArchitectureDiagnostic(
                    code=AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE,
                    job_id=job.job_id,
                    rule_id=job.rule_id,
                    message=f"Job {job.job_id} still references a legacy executor shape.",
                )
            )

        edges = store.list_child_execution_edges(job.job_id)
        authoritative = [
            edge for edge in edges if edge.authoritative_for_parent_completion
        ]
        if _launched_child_work(job) and not edges:
            diagnostics.append(
                AutomationArchitectureDiagnostic(
                    code=AUTOMATION_CHILD_EDGE_MISSING,
                    job_id=job.job_id,
                    rule_id=job.rule_id,
                    message=(
                        f"Job {job.job_id} has child execution refs but no durable "
                        "child execution edge."
                    ),
                )
            )

        nonterminal_authoritative = [
            edge for edge in authoritative if edge.terminal_state is None
        ]
        if job.state == JOB_RUNNING and not nonterminal_authoritative:
            diagnostics.append(
                AutomationArchitectureDiagnostic(
                    code=AUTOMATION_PARENT_STATE_STALE,
                    job_id=job.job_id,
                    rule_id=job.rule_id,
                    message=(
                        f"Running job {job.job_id} has no nonterminal authoritative "
                        "child edge after reducer input is available."
                    ),
                )
            )

        for edge in authoritative:
            for field in _runtime_mismatch_fields(edge):
                if job.state in {JOB_RUNNING, JOB_SUCCEEDED}:
                    diagnostics.append(
                        AutomationArchitectureDiagnostic(
                            code=AUTOMATION_RUNTIME_MISMATCH,
                            job_id=job.job_id,
                            rule_id=job.rule_id,
                            child_id=edge.child_id,
                            field=field,
                            message=(
                                f"Job {job.job_id} is {job.state} while child "
                                f"{edge.child_id} has a requested/actual {field} mismatch."
                            ),
                        )
                    )

    return tuple(diagnostics)


def _runtime_mismatch_fields(edge: AutomationChildExecutionEdge) -> list[str]:
    if edge.child_kind != AUTOMATION_CHILD_KIND_AGENT_TASK:
        return []
    requested = edge.requested_runtime.to_dict()
    actual = edge.actual_runtime.to_dict() if edge.actual_runtime is not None else {}
    return [
        key
        for key in ("agent", "model", "profile", "reasoning")
        if requested.get(key) and requested.get(key) != actual.get(key)
    ]


def _launched_child_work(job: Any) -> bool:
    return getattr(job, "state", None) == JOB_RUNNING


def _legacy_child_column_diagnostics(
    hub_root: Path, *, durable: bool
) -> tuple[AutomationArchitectureDiagnostic, ...]:
    try:
        with open_orchestration_sqlite(
            hub_root, durable=durable, migrate=False
        ) as conn:
            if not table_exists(conn, "orch_automation_jobs"):
                return ()
            predicates = " OR ".join(
                f"{column} IS NOT NULL" for column in _LEGACY_JOB_CHILD_COLUMNS
            )
            rows = conn.execute(f"""
                SELECT job_id, rule_id, {", ".join(_LEGACY_JOB_CHILD_COLUMNS)}
                  FROM orch_automation_jobs
                 WHERE {predicates}
                 ORDER BY updated_at DESC, job_id ASC
                """).fetchall()
    except Exception as exc:
        return (
            AutomationArchitectureDiagnostic(
                code=AUTOMATION_LEGACY_CHILD_COLUMN_POPULATED,
                message=f"Legacy automation child column inspection failed: {exc}",
            ),
        )

    diagnostics: list[AutomationArchitectureDiagnostic] = []
    for row in rows:
        for column in _LEGACY_JOB_CHILD_COLUMNS:
            if row[column] is None:
                continue
            diagnostics.append(
                AutomationArchitectureDiagnostic(
                    code=AUTOMATION_LEGACY_CHILD_COLUMN_POPULATED,
                    job_id=str(row["job_id"]),
                    rule_id=str(row["rule_id"]),
                    field=column,
                    message=(
                        f"Job {row['job_id']} still populates legacy child column "
                        f"{column}; child lifecycle must live only in graph edges."
                    ),
                )
            )
    return tuple(diagnostics)


__all__ = [
    "AUTOMATION_CHILD_EDGE_MISSING",
    "AUTOMATION_LEGACY_CHILD_COLUMN_POPULATED",
    "AUTOMATION_LEGACY_EXECUTOR_SHAPE",
    "AUTOMATION_PARENT_STATE_STALE",
    "AUTOMATION_RUNTIME_MISMATCH",
    "AutomationArchitectureDiagnostic",
    "automation_architecture_doctor_checks",
    "automation_migration_doctor_checks",
    "collect_automation_architecture_diagnostics",
]
