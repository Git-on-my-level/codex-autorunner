from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .injected_context import wrap_injected_context

CarContextProfile = Literal["car_core", "car_ambient", "none"]

CAR_CONTEXT_PROFILE_VALUES: tuple[CarContextProfile, ...] = (
    "car_core",
    "car_ambient",
    "none",
)
DEFAULT_PMA_CONTEXT_PROFILE: CarContextProfile = "car_core"
DEFAULT_TICKET_FLOW_CONTEXT_PROFILE: CarContextProfile = "car_core"
DEFAULT_REPO_THREAD_CONTEXT_PROFILE: CarContextProfile = "car_ambient"
DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE: CarContextProfile = "none"

_CAR_TRIGGER_RE = re.compile(
    r"\b(ticket|tickets|dispatch|resume|contextspace|codex-autorunner|car)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CarContextBundle:
    declared_profile: CarContextProfile
    effective_profile: CarContextProfile
    target_path: Optional[str] = None
    initiated_by_ticket_flow: bool = False


def normalize_car_context_profile(
    value: object,
    *,
    default: Optional[CarContextProfile] = None,
) -> Optional[CarContextProfile]:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    for candidate in CAR_CONTEXT_PROFILE_VALUES:
        if normalized == candidate:
            return candidate
    return default


def default_managed_thread_context_profile(
    *,
    resource_kind: object = None,
) -> CarContextProfile:
    if str(resource_kind or "").strip().lower() == "agent_workspace":
        return DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE
    return DEFAULT_REPO_THREAD_CONTEXT_PROFILE


def is_car_artifact_path(path: object) -> bool:
    if not isinstance(path, str):
        return False
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        return False
    return normalized.startswith(".codex-autorunner/")


def prompt_requests_car_context(prompt_text: object) -> bool:
    if not isinstance(prompt_text, str):
        return False
    text = prompt_text.strip()
    if not text:
        return False
    if ".codex-autorunner/" in text:
        return True
    return bool(_CAR_TRIGGER_RE.search(text))


def should_escalate_ambient_car_context(
    *,
    prompt_text: object = None,
    target_path: object = None,
    initiated_by_ticket_flow: bool = False,
) -> bool:
    if initiated_by_ticket_flow:
        return True
    if is_car_artifact_path(target_path):
        return True
    return prompt_requests_car_context(prompt_text)


def resolve_effective_car_context_profile(
    declared_profile: object,
    *,
    prompt_text: object = None,
    target_path: object = None,
    initiated_by_ticket_flow: bool = False,
) -> CarContextProfile:
    normalized = normalize_car_context_profile(
        declared_profile,
        default=DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    )
    assert normalized is not None
    if normalized != "car_ambient":
        return normalized
    if should_escalate_ambient_car_context(
        prompt_text=prompt_text,
        target_path=target_path,
        initiated_by_ticket_flow=initiated_by_ticket_flow,
    ):
        return "car_core"
    return "none"


def build_car_context_bundle(
    declared_profile: object,
    *,
    prompt_text: object = None,
    target_path: object = None,
    initiated_by_ticket_flow: bool = False,
) -> CarContextBundle:
    normalized = normalize_car_context_profile(
        declared_profile,
        default=DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    )
    assert normalized is not None
    effective = resolve_effective_car_context_profile(
        normalized,
        prompt_text=prompt_text,
        target_path=target_path,
        initiated_by_ticket_flow=initiated_by_ticket_flow,
    )
    return CarContextBundle(
        declared_profile=normalized,
        effective_profile=effective,
        target_path=(
            target_path if isinstance(target_path, str) and target_path else None
        ),
        initiated_by_ticket_flow=initiated_by_ticket_flow,
    )


def render_car_context_text(bundle: CarContextBundle) -> str:
    if bundle.effective_profile == "none":
        return ""
    lines = [
        "You are operating inside a Codex Autorunner (CAR) managed repo.",
        "",
        "CAR's durable control-plane lives under `.codex-autorunner/`:",
        "- `.codex-autorunner/ABOUT_CAR.md` -- short repo-local briefing (ticket/contextspace conventions + helper scripts).",
        "- `.codex-autorunner/DESTINATION_QUICKSTART.md` -- local/docker runtime setup (custom image + mount/env/profile flags).",
        "- `.codex-autorunner/tickets/` -- ordered ticket queue (`TICKET-###*.md`) used by the ticket flow runner.",
        "- `.codex-autorunner/contextspace/` -- shared context docs:",
        "  - `active_context.md` -- current north-star context.",
        "  - `spec.md` -- longer requirements and acceptance criteria.",
        "  - `decisions.md` -- durable tradeoffs and constraints.",
        "- `.codex-autorunner/filebox/` -- CAR attachment inbox/outbox when present.",
        "",
        "Intent signals: if the user mentions tickets, dispatch, resume, contextspace docs, or `.codex-autorunner/`, they are likely referring to CAR workflow artifacts.",
        "",
        "Use `.codex-autorunner/ABOUT_CAR.md` for operational details and `.codex-autorunner/DESTINATION_QUICKSTART.md` for runtime setup.",
    ]
    if bundle.target_path:
        lines.extend(
            [
                "",
                f"Current target path: `{bundle.target_path}`.",
            ]
        )
    if bundle.initiated_by_ticket_flow:
        lines.extend(
            [
                "",
                "This turn was initiated by CAR ticket flow, so CAR workflow semantics apply.",
            ]
        )
    return "\n".join(lines)


def render_injected_car_context(bundle: CarContextBundle) -> str:
    text = render_car_context_text(bundle)
    if not text:
        return ""
    return wrap_injected_context(text)


def render_runtime_compat_agents_md(bundle: CarContextBundle) -> str:
    if bundle.effective_profile == "none":
        return ""
    lines = [
        "<!-- Generated by Codex Autorunner; edit CAR source docs instead of this compatibility file. -->",
        "# AGENTS",
        "",
        "This workspace is managed by Codex Autorunner (CAR).",
        "",
        "Primary CAR artifacts:",
        "- `.codex-autorunner/ABOUT_CAR.md` for repo-local operating guidance",
        "- `.codex-autorunner/DESTINATION_QUICKSTART.md` for runtime setup",
        "- `.codex-autorunner/tickets/` for ticket-flow work items",
        "- `.codex-autorunner/contextspace/` for active context, spec, and decisions",
    ]
    if bundle.target_path:
        lines.extend(["", f"Current CAR target: `{bundle.target_path}`"])
    return "\n".join(lines) + "\n"


__all__ = [
    "CAR_CONTEXT_PROFILE_VALUES",
    "DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE",
    "DEFAULT_PMA_CONTEXT_PROFILE",
    "DEFAULT_REPO_THREAD_CONTEXT_PROFILE",
    "DEFAULT_TICKET_FLOW_CONTEXT_PROFILE",
    "CarContextBundle",
    "CarContextProfile",
    "build_car_context_bundle",
    "default_managed_thread_context_profile",
    "is_car_artifact_path",
    "normalize_car_context_profile",
    "prompt_requests_car_context",
    "render_car_context_text",
    "render_injected_car_context",
    "render_runtime_compat_agents_md",
    "resolve_effective_car_context_profile",
    "should_escalate_ambient_car_context",
]
