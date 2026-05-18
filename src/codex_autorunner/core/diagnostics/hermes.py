from __future__ import annotations

from ..config import HubConfig
from .agent_registry import (
    configured_agent_execution_targets as _configured_agent_execution_targets,
)
from .agent_registry import (
    descriptor_runtime_kind as _descriptor_runtime_kind,
)
from .agent_registry import (
    get_agent_descriptor as _get_agent_descriptor,
)
from .agent_registry import (
    run_agent_runtime_preflight as _run_agent_runtime_preflight,
)
from .types import DoctorCheck


def hermes_doctor_checks(hub_config: HubConfig) -> list[DoctorCheck]:
    """Report Hermes runtime compatibility when managed Hermes usage exists."""
    checks: list[DoctorCheck] = []

    enabled_workspaces: list[str] = []

    workspace_suffix = ""
    if enabled_workspaces:
        workspace_suffix = f" for enabled workspaces: {', '.join(enabled_workspaces)}"

    configured_hermes_agents: list[str] = []
    for target in _configured_agent_execution_targets(hub_config):
        descriptor = _get_agent_descriptor(target.runtime_agent_id, hub_config)
        if descriptor is None:
            continue
        if _descriptor_runtime_kind(target.runtime_agent_id, descriptor) != "hermes":
            continue
        configured_hermes_agents.append(target.requested_agent_id)
        agent_cfg = getattr(hub_config, "agents", {}).get(target.requested_agent_id)
        explicit_backend = getattr(agent_cfg, "backend", None)
        explicit_backend_value = (
            str(explicit_backend).strip().lower()
            if isinstance(explicit_backend, str) and explicit_backend.strip()
            else None
        )
        if explicit_backend_value == "hermes":
            continue
        if not (
            target.requested_agent_id.startswith("hermes-")
            or target.requested_agent_id.startswith("hermes_")
        ):
            continue
        checks.append(
            DoctorCheck(
                name=f"Hermes alias metadata ({target.requested_agent_id})",
                passed=False,
                message=(
                    f"Agent {target.requested_agent_id!r} looks like a Hermes alias "
                    "(id prefix) but has no explicit backend: hermes metadata."
                ),
                severity="warning",
                check_id=f"hub.hermes.alias_metadata.{target.requested_agent_id}",
                fix="Set backend: hermes for this agent in hub configuration.",
            )
        )

    configured_hermes_agents = sorted(set(configured_hermes_agents))

    if not configured_hermes_agents:
        configured_hermes_agents = ["hermes"]

    should_report = bool(enabled_workspaces)
    agent_ids_to_check: list[str] = []
    for agent_id in configured_hermes_agents:
        try:
            configured_binary = hub_config.agent_binary(agent_id).strip()
        except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
            configured_binary = ""
        is_alias = agent_id != "hermes"
        explicit_binary_override = bool(
            configured_binary and configured_binary != "hermes"
        )
        if enabled_workspaces or is_alias or explicit_binary_override:
            should_report = True
            agent_ids_to_check.append(agent_id)

    if not should_report:
        return checks
    if not agent_ids_to_check:
        agent_ids_to_check = configured_hermes_agents

    for agent_id in agent_ids_to_check:
        result = _run_agent_runtime_preflight(agent_id, context=hub_config)
        check_name = (
            "Hermes runtime availability"
            if agent_id == "hermes"
            else f"Hermes runtime availability ({agent_id})"
        )
        check_id = (
            "hub.hermes.binary"
            if agent_id == "hermes"
            else f"hub.hermes.binary.{agent_id}"
        )
        severity = (
            "info"
            if result.status == "ready"
            else ("error" if enabled_workspaces else "warning")
        )
        fix = result.fix
        message = result.message
        if workspace_suffix:
            message = f"{message.rstrip('.')}{workspace_suffix}."
        if result.status == "ready":
            detail_parts = []
            if result.version:
                detail_parts.append(result.version)
            if result.launch_mode:
                detail_parts.append(f"launch_mode={result.launch_mode}")
            suffix = f" ({', '.join(detail_parts)})" if detail_parts else ""
            checks.append(
                DoctorCheck(
                    name=check_name,
                    passed=True,
                    message=f"{message.rstrip('.')}{suffix}.",
                    severity="info",
                    check_id=check_id,
                )
            )
            continue

        checks.append(
            DoctorCheck(
                name=check_name,
                passed=False,
                message=message,
                severity=severity,
                check_id=check_id,
                fix=fix,
            )
        )
    return checks


__all__ = ["hermes_doctor_checks"]
