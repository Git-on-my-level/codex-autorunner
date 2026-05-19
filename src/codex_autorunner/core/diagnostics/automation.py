from __future__ import annotations

from pathlib import Path

from ..automation.migration_diagnostics import collect_automation_migration_read_model
from ..config import HubConfig
from .types import DoctorCheck


def automation_migration_doctor_checks(
    hub_config: HubConfig,
    *,
    repo_root: Path | None = None,
) -> list[DoctorCheck]:
    _ = repo_root
    report = collect_automation_migration_read_model(hub_config.root)
    if report.status == "ok":
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
        ]
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
    ]


__all__ = ["automation_migration_doctor_checks"]
