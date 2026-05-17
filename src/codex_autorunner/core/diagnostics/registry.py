from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..config import HubConfig
from .hermes import hermes_doctor_checks
from .hub import hub_destination_doctor_checks, hub_worktree_doctor_checks
from .pma import pma_doctor_checks
from .repository import doctor
from .types import DoctorCheck, DoctorReport


@dataclass(frozen=True)
class DoctorProvider:
    name: str
    collect: Callable[[], list[DoctorCheck]]


def runtime_doctor_providers(
    *,
    start_path: Path,
    hub_config: HubConfig,
    repo_root: Optional[Path],
) -> list[DoctorProvider]:
    return [
        DoctorProvider(
            name="repository",
            collect=lambda: doctor(start_path).checks,
        ),
        DoctorProvider(
            name="pma",
            collect=lambda: pma_doctor_checks(hub_config, repo_root=repo_root),
        ),
        DoctorProvider(
            name="hub_worktree",
            collect=lambda: hub_worktree_doctor_checks(hub_config),
        ),
        DoctorProvider(
            name="hub_destination",
            collect=lambda: hub_destination_doctor_checks(hub_config),
        ),
        DoctorProvider(
            name="hermes",
            collect=lambda: hermes_doctor_checks(hub_config),
        ),
    ]


def collect_doctor_report(providers: list[DoctorProvider]) -> DoctorReport:
    checks: list[DoctorCheck] = []
    for provider in providers:
        checks.extend(provider.collect())
    return DoctorReport(checks=checks)


def runtime_doctor_report(
    *,
    start_path: Path,
    hub_config: HubConfig,
    repo_root: Optional[Path],
) -> DoctorReport:
    return collect_doctor_report(
        runtime_doctor_providers(
            start_path=start_path,
            hub_config=hub_config,
            repo_root=repo_root,
        )
    )


__all__ = [
    "DoctorProvider",
    "collect_doctor_report",
    "runtime_doctor_providers",
    "runtime_doctor_report",
]
