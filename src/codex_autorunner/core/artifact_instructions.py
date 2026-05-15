from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ARTIFACT_TARGET_SURFACE_ENV = "CAR_ARTIFACT_TARGET_SURFACE"
ARTIFACT_TARGET_CONVERSATION_ENV = "CAR_ARTIFACT_TARGET_CONVERSATION_KEY"
ARTIFACT_WORKSPACE_SCOPE_ENV = "CAR_ARTIFACT_WORKSPACE_SCOPE"


@dataclass(frozen=True)
class ArtifactDeliveryCommands:
    send_current: str = "car artifacts send <file> --to current"
    list_deliveries: str = "car artifacts list"
    inspect_delivery: str = "car artifacts inspect <delivery_id>"


@dataclass(frozen=True)
class ArtifactDeliveryContext:
    surface: str
    conversation_key: str
    scope_label: str
    workspace_scope: str | None = None
    user_upload_inbox: Path | None = None
    extra_agent_lines: tuple[str, ...] = ()


DEFAULT_ARTIFACT_DELIVERY_COMMANDS = ArtifactDeliveryCommands()


def missing_current_artifact_target_env(
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    values = os.environ if env is None else env
    missing: list[str] = []
    for key in (ARTIFACT_TARGET_SURFACE_ENV, ARTIFACT_TARGET_CONVERSATION_ENV):
        if not str(values.get(key, "")).strip():
            missing.append(key)
    return tuple(missing)


def current_artifact_target_available(
    env: Mapping[str, str] | None = None,
) -> bool:
    return not missing_current_artifact_target_env(env)


def current_artifact_target_failure_message(
    env: Mapping[str, str] | None = None,
) -> str | None:
    missing = missing_current_artifact_target_env(env)
    if not missing:
        return None
    return (
        "Current artifact delivery target is not configured; missing "
        f"{', '.join(missing)}. Fix the runtime target injection or use operator "
        "artifact diagnostics."
    )


def render_agent_artifact_instructions(
    context: ArtifactDeliveryContext,
    *,
    commands: ArtifactDeliveryCommands = DEFAULT_ARTIFACT_DELIVERY_COMMANDS,
) -> str:
    lines = [
        "Artifact delivery (this turn):",
        f"- Active scope: {context.scope_label}",
        f"- Target surface: {context.surface}",
        f"- Target conversation: {context.conversation_key}",
        f"- Workspace scope: {context.workspace_scope or '(none)'}",
        f"- Send user-facing files with: `{commands.send_current}`.",
        f"- Check delivery status with: `{commands.list_deliveries}`.",
    ]
    if context.user_upload_inbox is not None:
        lines.append(f"- User uploads may appear under: {context.user_upload_inbox}")
    lines.extend(
        line
        for line in (str(item).strip() for item in context.extra_agent_lines)
        if line
    )
    return "\n".join(lines)


def render_human_artifact_overview(
    *,
    include_upload_inbox: bool = False,
    include_lifecycle_ops: bool = True,
    commands: ArtifactDeliveryCommands = DEFAULT_ARTIFACT_DELIVERY_COMMANDS,
) -> str:
    lines = [
        "## Artifact delivery",
        f"- Send user-facing files with `{commands.send_current}` when a turn provides a current artifact target.",
    ]
    if include_lifecycle_ops:
        lines.append(
            "- Delivery records live in the artifact journal and can be inspected "
            f"with `{commands.list_deliveries}` and `{commands.inspect_delivery}`."
        )
    if include_upload_inbox:
        lines.append("- User uploads arrive in `.codex-autorunner/filebox/inbox/`.")
    lines.append(
        "- Note: ticket_flow uses per-run dispatch directories; do not confuse dispatch with artifact delivery."
    )
    return "\n".join(lines)


__all__ = [
    "ARTIFACT_TARGET_CONVERSATION_ENV",
    "ARTIFACT_TARGET_SURFACE_ENV",
    "ARTIFACT_WORKSPACE_SCOPE_ENV",
    "ArtifactDeliveryCommands",
    "ArtifactDeliveryContext",
    "DEFAULT_ARTIFACT_DELIVERY_COMMANDS",
    "current_artifact_target_available",
    "current_artifact_target_failure_message",
    "missing_current_artifact_target_env",
    "render_agent_artifact_instructions",
    "render_human_artifact_overview",
]
