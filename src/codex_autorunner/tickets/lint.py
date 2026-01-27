from __future__ import annotations

from typing import Any, Optional, Tuple

from ..agents.registry import validate_agent_id
from .models import TicketFrontmatter, normalize_requires


def _as_optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def lint_ticket_frontmatter(
    data: dict[str, Any],
) -> Tuple[Optional[TicketFrontmatter], list[str]]:
    """Validate and normalize ticket frontmatter.

    Required keys:
    - agent: string (or the special value "user")
    - done: bool
    """

    errors: list[str] = []
    if not isinstance(data, dict) or not data:
        return None, ["Missing or invalid YAML frontmatter (expected a mapping)."]

    extra = {k: v for k, v in data.items()}

    agent_raw = data.get("agent")
    agent = _as_optional_str(agent_raw)
    if not agent:
        errors.append("frontmatter.agent is required (e.g. 'codex' or 'opencode').")
    else:
        # Special built-in ticket handler.
        if agent != "user":
            try:
                validate_agent_id(agent)
            except ValueError as exc:
                errors.append(f"frontmatter.agent is invalid: {exc}")

    done_raw = data.get("done")
    done: Optional[bool]
    if isinstance(done_raw, bool):
        done = done_raw
    else:
        done = None
        errors.append("frontmatter.done is required and must be a boolean.")

    title = _as_optional_str(data.get("title"))
    goal = _as_optional_str(data.get("goal"))

    requires = normalize_requires(
        data.get("requires") if isinstance(data, dict) else None
    )

    # Optional model/reasoning overrides.
    model = _as_optional_str(data.get("model"))
    reasoning = _as_optional_str(data.get("reasoning"))

    # Remove normalized keys from extra.
    for key in ("agent", "done", "title", "goal", "requires", "model", "reasoning"):
        extra.pop(key, None)

    if errors:
        return None, errors

    assert agent is not None
    assert done is not None
    return (
        TicketFrontmatter(
            agent=agent,
            done=done,
            title=title,
            goal=goal,
            requires=requires,
            model=model,
            reasoning=reasoning,
            extra=extra,
        ),
        [],
    )


def lint_dispatch_frontmatter(
    data: dict[str, Any],
) -> Tuple[dict[str, Any], list[str]]:
    """Validate DISPATCH.md frontmatter.

    Keys:
    - mode: "notify" | "pause" | "turn_summary" (defaults to notify)
    """

    errors: list[str] = []
    if not isinstance(data, dict):
        return {}, ["Invalid YAML frontmatter (expected a mapping)."]

    mode_raw = data.get("mode")
    mode = mode_raw.strip().lower() if isinstance(mode_raw, str) else "notify"
    if mode not in ("notify", "pause", "turn_summary"):
        errors.append("frontmatter.mode must be 'notify', 'pause', or 'turn_summary'.")

    normalized = dict(data)
    normalized["mode"] = mode
    return normalized, errors
