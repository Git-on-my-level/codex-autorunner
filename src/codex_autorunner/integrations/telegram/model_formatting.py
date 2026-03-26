from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .constants import (
    DEFAULT_MODEL_LIST_LIMIT,
    DEFAULT_SKILLS_LIST_LIMIT,
    REASONING_EFFORT_VALUES,
)


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    label: str
    efforts: tuple[str, ...]
    default_effort: Optional[str] = None


def _coerce_model_entries(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [entry for entry in result if isinstance(entry, dict)]
    if isinstance(result, dict):
        for key in ("data", "models", "items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _display_name_is_model_alias(model: str, display_name: Any) -> bool:
    if not isinstance(display_name, str) or not display_name:
        return False
    return _normalize_model_name(display_name) == _normalize_model_name(model)


def _coerce_model_options(
    result: Any, *, include_efforts: bool = True
) -> list[ModelOption]:
    entries = _coerce_model_entries(result)
    options: list[ModelOption] = []
    for entry in entries:
        model = entry.get("model") or entry.get("id")
        if not isinstance(model, str) or not model:
            continue
        display_name = entry.get("displayName")
        label = model
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(model, display_name)
        ):
            label = f"{model} ({display_name})"
        default_effort = None
        efforts: list[str] = []
        if include_efforts:
            default_effort = entry.get("defaultReasoningEffort")
            if not isinstance(default_effort, str):
                default_effort = None
            efforts_raw = entry.get("supportedReasoningEfforts")
            if isinstance(efforts_raw, list):
                for effort in efforts_raw:
                    if isinstance(effort, dict):
                        value = effort.get("reasoningEffort")
                        if isinstance(value, str):
                            efforts.append(value)
                    elif isinstance(effort, str):
                        efforts.append(effort)
            if default_effort and default_effort not in efforts:
                efforts.append(default_effort)
            efforts = [effort for effort in efforts if effort]
            if not efforts:
                efforts = list(REASONING_EFFORT_VALUES)
            efforts = list(dict.fromkeys(efforts))
            if default_effort:
                label = f"{label} (default {default_effort})"
        options.append(
            ModelOption(
                model_id=model,
                label=label,
                efforts=tuple(efforts),
                default_effort=default_effort,
            )
        )
    return options


def _format_model_list(
    result: Any,
    *,
    include_efforts: bool = True,
    set_hint: Optional[str] = None,
) -> str:
    entries = _coerce_model_entries(result)
    if not entries:
        return "No models found."
    lines = ["Available models:"]
    for entry in entries[:DEFAULT_MODEL_LIST_LIMIT]:
        model = entry.get("model") or entry.get("id") or "(unknown)"
        display_name = entry.get("displayName")
        label = str(model)
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(label, display_name)
        ):
            label = f"{model} ({display_name})"
        if include_efforts:
            efforts = entry.get("supportedReasoningEfforts")
            effort_values: list[str] = []
            if isinstance(efforts, list):
                for effort in efforts:
                    if isinstance(effort, dict):
                        value = effort.get("reasoningEffort")
                        if isinstance(value, str):
                            effort_values.append(value)
                    elif isinstance(effort, str):
                        effort_values.append(effort)
            if effort_values:
                label = f"{label} [effort: {', '.join(effort_values)}]"
            default_effort = entry.get("defaultReasoningEffort")
            if isinstance(default_effort, str):
                label = f"{label} (default {default_effort})"
        lines.append(label)
    if len(entries) > DEFAULT_MODEL_LIST_LIMIT:
        lines.append(f"...and {len(entries) - DEFAULT_MODEL_LIST_LIMIT} more.")
    if set_hint is None:
        set_hint = "Use /model <id> [effort] to set." if include_efforts else None
    if set_hint:
        lines.append(set_hint)
    return "\n".join(lines)


def _format_skills_list(result: Any, workspace_path: Optional[str]) -> str:
    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    skills: list[tuple[str, str]] = []
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(workspace_path, str) and isinstance(cwd, str):
            if (
                Path(cwd).expanduser().resolve()
                != Path(workspace_path).expanduser().resolve()
            ):
                continue
        items = entry.get("skills")
        if isinstance(items, list):
            for skill in items:
                if not isinstance(skill, dict):
                    continue
                name = skill.get("name")
                if not isinstance(name, str) or not name:
                    continue
                description = skill.get("shortDescription") or skill.get("description")
                desc_text = (
                    description.strip()
                    if isinstance(description, str) and description
                    else ""
                )
                skills.append((name, desc_text))
    if not skills:
        return "No skills found."
    lines = ["Skills:"]
    for name, desc in skills[:DEFAULT_SKILLS_LIST_LIMIT]:
        if desc:
            lines.append(f"{name} - {desc}")
        else:
            lines.append(name)
    if len(skills) > DEFAULT_SKILLS_LIST_LIMIT:
        lines.append(f"...and {len(skills) - DEFAULT_SKILLS_LIST_LIMIT} more.")
    lines.append("Use $<SkillName> in your next message to invoke a skill.")
    return "\n".join(lines)


def _format_mcp_list(result: Any) -> str:
    entries: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    if not entries:
        return "No MCP servers found."
    lines = ["MCP servers:"]
    for entry in entries:
        name = entry.get("name") or "(unknown)"
        auth = entry.get("authStatus") or "unknown"
        tools = entry.get("tools")
        tool_names: list[str] = []
        if isinstance(tools, dict):
            tool_names = sorted(tools.keys())
        elif isinstance(tools, list):
            tool_names = [str(item) for item in tools]
        line = f"{name} ({auth})"
        if tool_names:
            line = f"{line} - tools: {', '.join(tool_names)}"
        lines.append(line)
    return "\n".join(lines)


def _format_feature_flags(result: Any) -> str:
    config = result.get("config") if isinstance(result, dict) else None
    if config is None and isinstance(result, dict):
        config = result
    if not isinstance(config, dict):
        return "No feature flags found."
    features = config.get("features")
    if not isinstance(features, dict) or not features:
        return "No feature flags found."
    lines = ["Feature flags:"]
    for key in sorted(features.keys()):
        value = features.get(key)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)
