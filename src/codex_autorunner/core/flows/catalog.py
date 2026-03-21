from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlowActionSpec:
    name: str
    description: str
    usage: str
    requires_run_picker: bool = False
    aliases: tuple[str, ...] = ()


FLOW_ACTION_SPECS: tuple[FlowActionSpec, ...] = (
    FlowActionSpec("status", "Show flow status", "[run_id]", requires_run_picker=True),
    FlowActionSpec("runs", "List flow runs", "[N]"),
    FlowActionSpec("issue", "Seed ISSUE.md from a GitHub issue", "<issue#|url>"),
    FlowActionSpec("plan", "Seed ISSUE.md from plan text", "<text>"),
    FlowActionSpec(
        "start",
        "Start flow (reuses active/paused run)",
        "[force_new]",
        aliases=("bootstrap",),
    ),
    FlowActionSpec(
        "restart",
        "Restart flow from a fresh run",
        "[run_id]",
        requires_run_picker=True,
    ),
    FlowActionSpec(
        "resume",
        "Resume a flow",
        "[run_id]",
        requires_run_picker=True,
    ),
    FlowActionSpec(
        "stop",
        "Stop a flow",
        "[run_id]",
        requires_run_picker=True,
    ),
    FlowActionSpec(
        "archive",
        "Archive a flow",
        "[run_id]",
        requires_run_picker=True,
    ),
    FlowActionSpec(
        "recover",
        "Recover a flow",
        "[run_id]",
        requires_run_picker=True,
    ),
    FlowActionSpec(
        "reply",
        "Reply to paused flow",
        "<text> [run_id]",
        requires_run_picker=True,
    ),
)

_FLOW_ACTION_BY_NAME = {spec.name: spec for spec in FLOW_ACTION_SPECS}
_FLOW_ACTION_ALIASES = {
    alias: spec.name for spec in FLOW_ACTION_SPECS for alias in spec.aliases
}

FLOW_ACTION_NAMES: tuple[str, ...] = tuple(spec.name for spec in FLOW_ACTION_SPECS)
FLOW_ACTION_TOKENS: frozenset[str] = frozenset(
    {*FLOW_ACTION_NAMES, *(_FLOW_ACTION_ALIASES.keys()), "help"}
)
FLOW_ACTIONS_WITH_RUN_PICKER: frozenset[str] = frozenset(
    spec.name for spec in FLOW_ACTION_SPECS if spec.requires_run_picker
)


def flow_action_spec(action: str) -> FlowActionSpec | None:
    normalized = (action or "").strip().lower()
    if not normalized:
        return None
    canonical = _FLOW_ACTION_ALIASES.get(normalized, normalized)
    return _FLOW_ACTION_BY_NAME.get(canonical)


def normalize_flow_action(action: str) -> str:
    normalized = (action or "").strip().lower()
    if not normalized:
        return "help"
    return _FLOW_ACTION_ALIASES.get(normalized, normalized)


def flow_action_label(action: str) -> str:
    spec = flow_action_spec(action)
    return spec.name if spec is not None else (action or "").strip().lower()


def flow_action_summary() -> str:
    return ", ".join(FLOW_ACTION_NAMES)


def flow_help_lines(
    *, prefix: str, usage_overrides: dict[str, str] | None = None
) -> list[str]:
    overrides = usage_overrides or {}
    lines = ["Flow commands:"]
    for spec in FLOW_ACTION_SPECS:
        usage = overrides.get(spec.name, spec.usage)
        lines.append(f"{prefix} {spec.name} {usage}".rstrip())
    return lines


__all__ = [
    "FLOW_ACTION_NAMES",
    "FLOW_ACTION_SPECS",
    "FLOW_ACTIONS_WITH_RUN_PICKER",
    "FLOW_ACTION_TOKENS",
    "FlowActionSpec",
    "flow_action_label",
    "flow_action_spec",
    "flow_action_summary",
    "flow_help_lines",
    "normalize_flow_action",
]
