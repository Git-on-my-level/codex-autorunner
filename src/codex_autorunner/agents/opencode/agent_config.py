from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_CAR_MANAGED_KEY = "car_managed"
_CAR_MANAGED_VALUE = "codex-autorunner"
_LEGACY_CAR_KEYS = {"agent", "title", "description", "model"}


async def ensure_agent_config(
    workspace_root: Path,
    agent_id: str,
    model: Optional[str],
    title: Optional[str] = None,
    description: Optional[str] = None,
    mode: Optional[str] = None,
    permission: Optional[Mapping[str, Any]] = None,
    steps: Optional[int] = None,
    body: Optional[str] = None,
    car_managed: bool = True,
) -> None:
    """Ensure .opencode/agent/<agent_id>.md exists with frontmatter config.

    Args:
        workspace_root: Path to the workspace root
        agent_id: Agent ID (e.g., "subagent")
        model: Model ID in format "providerID/modelID" (e.g., "zai-coding-plan/glm-5.1")
        title: Optional title for the agent
        description: Optional description for the agent
        mode: Optional OpenCode agent mode ("primary" or "subagent")
        permission: Optional OpenCode permission policy frontmatter
        steps: Optional max step count for the OpenCode agent
        body: Optional instruction body after frontmatter
        car_managed: Whether to mark the file as CAR-owned for future updates
    """
    if model is None:
        logger.debug(f"Skipping agent config for {agent_id}: no model configured")
        return

    agent_dir = workspace_root / ".opencode" / "agent"
    agent_file = agent_dir / f"{agent_id}.md"

    # Check if file already exists and has the correct model
    if agent_file.exists():
        existing_content = agent_file.read_text(encoding="utf-8")
        if not _can_update_existing_agent_config(existing_content):
            logger.info(
                "OpenCode agent config is user-managed, leaving unchanged: %s",
                agent_file,
            )
            return
        content = _build_agent_md(
            agent_id=agent_id,
            model=model,
            title=title or agent_id,
            description=description or f"Subagent for {agent_id} tasks",
            mode=mode,
            permission=permission,
            steps=steps,
            body=body,
            car_managed=car_managed,
        )
        if existing_content == content:
            logger.debug(f"Agent config already exists for {agent_id}: {agent_file}")
            return
        if (
            permission is None
            and mode is None
            and steps is None
            and body is None
            and _extract_model_from_frontmatter(existing_content) == model
        ):
            logger.debug(
                f"Agent config model already matches for {agent_id}: {agent_file}"
            )
            return
    else:
        content = _build_agent_md(
            agent_id=agent_id,
            model=model,
            title=title or agent_id,
            description=description or f"Subagent for {agent_id} tasks",
            mode=mode,
            permission=permission,
            steps=steps,
            body=body,
            car_managed=car_managed,
        )

    # Create agent directory if needed
    await asyncio.to_thread(agent_dir.mkdir, parents=True, exist_ok=True)

    # Write atomically
    await asyncio.to_thread(agent_file.write_text, content, encoding="utf-8")
    logger.info(f"Created agent config: {agent_file} with model {model}")


def _build_agent_md(
    agent_id: str,
    model: str,
    title: str,
    description: str,
    mode: Optional[str] = None,
    permission: Optional[Mapping[str, Any]] = None,
    steps: Optional[int] = None,
    body: Optional[str] = None,
    car_managed: bool = False,
) -> str:
    """Generate markdown with YAML frontmatter.

    Frontmatter format per OpenCode config schema:
    ---
    agent: <agent_id>
    title: "<title>"
    description: "<description>"
    model: <providerID>/<modelID>
    mode: <primary|subagent>
    ---

    <Optional agent instructions go here>
    """
    lines = [
        "---",
        f"agent: {_yaml_scalar(agent_id)}",
        f"title: {_yaml_scalar(title)}",
        f"description: {_yaml_scalar(description)}",
        f"model: {_yaml_scalar(model)}",
    ]
    if mode:
        lines.append(f"mode: {_yaml_scalar(mode)}")
    if isinstance(steps, int) and steps > 0:
        lines.append(f"steps: {steps}")
    if car_managed:
        lines.append(f"{_CAR_MANAGED_KEY}: {_yaml_scalar(_CAR_MANAGED_VALUE)}")
    if permission:
        lines.append("permission:")
        lines.extend(_yaml_mapping_lines(permission, indent=2))
    lines.append("---")
    content = "\n".join(lines) + "\n"
    if body:
        content += body.rstrip() + "\n"
    return content


def _extract_model_from_frontmatter(content: str) -> Optional[str]:
    """Extract model value from YAML frontmatter.

    Returns None if frontmatter or model field is not found.
    """
    lines = content.splitlines()
    if not lines or not lines[0].startswith("---"):
        return None

    for _i, line in enumerate(lines[1:], start=1):
        if line.startswith("---"):
            break
        if line.startswith("model:"):
            model = line.split(":", 1)[1].strip()
            return model if model else None

    return None


def _can_update_existing_agent_config(content: str) -> bool:
    fields, body = _parse_frontmatter_fields(content)
    if not fields:
        return False
    if fields.get(_CAR_MANAGED_KEY) == _CAR_MANAGED_VALUE:
        return True
    # Files produced by older CAR versions contained only simple frontmatter and
    # no body. Treat those as migratable while preserving user-authored agents.
    return _looks_like_legacy_car_agent(fields, body)


def _looks_like_legacy_car_agent(fields: dict[str, str], body: str) -> bool:
    description = fields.get("description", "")
    return (
        not body.strip()
        and set(fields).issubset(_LEGACY_CAR_KEYS)
        and set(fields).issuperset(_LEGACY_CAR_KEYS)
        and description.startswith("Subagent for ")
    )


def _parse_frontmatter_fields(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines()
    if not lines or not lines[0].startswith("---"):
        return {}, content
    fields: dict[str, str] = {}
    body_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.startswith("---"):
            body_start = index + 1
            break
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    body = "\n".join(lines[body_start:])
    return fields, body


def _yaml_scalar(value: str) -> str:
    if not value:
        return '""'
    if all(ch.isalnum() or ch in "-_./" for ch in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_mapping_lines(mapping: Mapping[str, Any], *, indent: int) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in mapping.items():
        key_text = _yaml_scalar(str(key))
        if isinstance(value, Mapping):
            lines.append(f"{prefix}{key_text}:")
            lines.extend(_yaml_mapping_lines(value, indent=indent + 2))
        else:
            lines.append(f"{prefix}{key_text}: {_yaml_scalar(str(value))}")
    return lines


__all__ = ["ensure_agent_config"]
