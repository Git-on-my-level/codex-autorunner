import asyncio  # noqa: F401
from pathlib import Path

import pytest

from codex_autorunner.agents.opencode.agent_config import (
    _build_agent_md,
    _extract_model_from_frontmatter,
    ensure_agent_config,
)
from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor


def test_build_agent_md():
    """Test agent markdown generation with frontmatter."""
    result = _build_agent_md(
        agent_id="subagent",
        model="zai-coding-plan/glm-4.7-flashx",
        title="Subagent",
        description="Subagent for subagent tasks",
    )

    assert result.startswith("---")
    assert "agent: subagent" in result
    assert 'title: "Subagent"' in result
    assert 'description: "Subagent for subagent tasks"' in result
    assert "model: zai-coding-plan/glm-4.7-flashx" in result
    assert result.endswith("---\n")


def test_extract_model_from_frontmatter():
    """Test extracting model from YAML frontmatter."""
    content = """---
agent: subagent
title: "Subagent"
description: "Subagent for subagent tasks"
model: zai-coding-plan/glm-4.7-flashx
---
Some content here
"""
    result = _extract_model_from_frontmatter(content)
    assert result == "zai-coding-plan/glm-4.7-flashx"


def test_extract_model_from_frontmatter_no_model():
    """Test extracting model from frontmatter without model field."""
    content = """---
agent: subagent
title: "Subagent"
---
Some content here
"""
    result = _extract_model_from_frontmatter(content)
    assert result is None


def test_extract_model_from_frontmatter_invalid():
    """Test extracting model from invalid frontmatter."""
    content = """No frontmatter here
agent: subagent
---
Some content
"""
    result = _extract_model_from_frontmatter(content)
    assert result is None


@pytest.mark.anyio
async def test_ensure_agent_config_creates_file(tmp_path: Path):
    """Test that ensure_agent_config creates agent config file."""
    agent_dir = tmp_path / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    await ensure_agent_config(
        workspace_root=tmp_path,
        agent_id="subagent",
        model="zai-coding-plan/glm-4.7-flashx",
        title="Subagent",
        description="Subagent for subagent tasks",
    )

    agent_file = agent_dir / "subagent.md"
    assert agent_file.exists()

    content = agent_file.read_text(encoding="utf-8")
    assert "agent: subagent" in content
    assert "model: zai-coding-plan/glm-4.7-flashx" in content


@pytest.mark.anyio
async def test_ensure_agent_config_skips_if_no_model(tmp_path: Path):
    """Test that ensure_agent_config skips if model is None."""
    agent_dir = tmp_path / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    await ensure_agent_config(
        workspace_root=tmp_path,
        agent_id="subagent",
        model=None,
    )

    agent_file = agent_dir / "subagent.md"
    assert not agent_file.exists()


@pytest.mark.anyio
async def test_ensure_agent_config_skips_if_model_unchanged(tmp_path: Path):
    """Test that ensure_agent_config skips if model hasn't changed."""
    agent_dir = tmp_path / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / "subagent.md"

    existing_content = """---
agent: subagent
title: "Subagent"
description: "Subagent for subagent tasks"
model: zai-coding-plan/glm-4.7-flashx
---
"""
    agent_file.write_text(existing_content, encoding="utf-8")

    await ensure_agent_config(
        workspace_root=tmp_path,
        agent_id="subagent",
        model="zai-coding-plan/glm-4.7-flashx",
        title="Subagent",
        description="Subagent for subagent tasks",
    )

    content = agent_file.read_text(encoding="utf-8")
    assert content == existing_content


@pytest.mark.anyio
async def test_ensure_agent_config_updates_if_model_changed(tmp_path: Path):
    """Test that ensure_agent_config updates if model has changed."""
    agent_dir = tmp_path / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / "subagent.md"

    existing_content = """---
agent: subagent
title: "Subagent"
description: "Subagent for subagent tasks"
model: zai-coding-plan/glm-4.7
---
"""
    agent_file.write_text(existing_content, encoding="utf-8")

    await ensure_agent_config(
        workspace_root=tmp_path,
        agent_id="subagent",
        model="zai-coding-plan/glm-4.7-flashx",
        title="Subagent",
        description="Subagent for subagent tasks",
    )

    content = agent_file.read_text(encoding="utf-8")
    assert "model: zai-coding-plan/glm-4.7-flashx" in content
    assert content.count("model: zai-coding-plan/glm-4.7-flashx") == 1


def test_supervisor_ensure_subagent_config():
    """Test OpenCodeSupervisor.ensure_subagent_config calls agent_config."""
    supervisor = OpenCodeSupervisor(
        command=["opencode", "serve"],
        subagent_models={
            "subagent": "zai-coding-plan/glm-4.7-flashx",
            "other": "zai-coding-plan/glm-4.7",
        },
    )

    assert supervisor._subagent_models == {
        "subagent": "zai-coding-plan/glm-4.7-flashx",
        "other": "zai-coding-plan/glm-4.7",
    }
