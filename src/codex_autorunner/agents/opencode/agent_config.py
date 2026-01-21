from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


async def ensure_agent_config(
    workspace_root: Path,
    agent_id: str,
    model: Optional[str],
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    """Ensure .opencode/agent/<agent_id>.md exists with frontmatter config.

    Args:
        workspace_root: Path to the workspace root
        agent_id: Agent ID (e.g., "subagent")
        model: Model ID in format "providerID/modelID" (e.g., "zai-coding-plan/glm-4.7-flashx")
        title: Optional title for the agent
        description: Optional description for the agent
    """
    if model is None:
        logger.debug(f"Skipping agent config for {agent_id}: no model configured")
        return

    agent_dir = workspace_root / ".opencode" / "agent"
    agent_file = agent_dir / f"{agent_id}.md"

    # Check if file already exists and has the correct model
    if agent_file.exists():
        existing_content = agent_file.read_text(encoding="utf-8")
        existing_model = _extract_model_from_frontmatter(existing_content)
        if existing_model == model:
            logger.debug(f"Agent config already exists for {agent_id}: {agent_file}")
            return

    # Create agent directory if needed
    await asyncio.to_thread(agent_dir.mkdir, parents=True, exist_ok=True)

    # Build agent markdown with frontmatter
    content = _build_agent_md(
        agent_id=agent_id,
        model=model,
        title=title or agent_id,
        description=description or f"Subagent for {agent_id} tasks",
    )

    # Write atomically
    await asyncio.to_thread(agent_file.write_text, content, encoding="utf-8")
    logger.info(f"Created agent config: {agent_file} with model {model}")


def _build_agent_md(
    agent_id: str,
    model: str,
    title: str,
    description: str,
) -> str:
    """Generate markdown with YAML frontmatter.

    Frontmatter format per OpenCode config schema:
    ---
    agent: <agent_id>
    title: "<title>"
    description: "<description>"
    model: <providerID>/<modelID>
    ---

    <Optional agent instructions go here>
    """
    return f"""---
agent: {agent_id}
title: "{title}"
description: "{description}"
model: {model}
---
"""


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


__all__ = ["ensure_agent_config"]
