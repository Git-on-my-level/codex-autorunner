from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.agents.types import RUNTIME_CAPABILITIES


def _read(rel_path: str) -> str:
    return Path(rel_path).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    pattern = rf"^### {re.escape(heading)}\n(.*?)(?=^### |^## |\Z)"
    match = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, f"missing section: {heading}"
    return match.group(1)


def _listed_capabilities(section_text: str) -> set[str]:
    return set(re.findall(r"- `([a-z_]+)`:", section_text))


def test_hub_manifest_docs_describe_typed_resource_model() -> None:
    text = _read("docs/reference/hub-manifest-schema.md")

    required_snippets = [
        "typed hub resource catalog",
        "`agent_workspaces[]` model CAR-managed durable runtime state",
        "CAR chat surfaces bind",
        "durable CAR thread under that resource",
        "The manifest does not install that runtime for you.",
    ]
    for snippet in required_snippets:
        assert snippet in text


def test_runtime_docs_explain_agent_workspace_contract() -> None:
    plugin_text = _read("docs/plugin-api.md")
    add_agent_text = _read("docs/adding-an-agent.md")
    zeroclaw_text = _read("docs/ops/zeroclaw-dogfood.md")
    pma_text = _read("docs/ops/pma-managed-thread-status.md")

    assert "Use `agent_workspace` semantics" in plugin_text
    assert "CAR does not install runtimes for plugins." in plugin_text
    assert "Choose The Right Resource Model" in add_agent_text
    assert "first-class CAR-managed `agent_workspace`" in add_agent_text
    assert "ZeroClaw support in CAR is detect-only" in zeroclaw_text
    assert "car hub agent-workspace create" in zeroclaw_text
    assert "`zeroclaw agent --session-state-file`" in zeroclaw_text
    assert "volatile wrapper-only launches" in zeroclaw_text
    assert "resource_kind: agent_workspace" in pma_text
    assert "consistent durable CAR thread under the workspace" in pma_text


def test_telegram_docs_describe_authoritative_binding_storage() -> None:
    architecture_text = _read("docs/telegram/architecture.md")
    security_text = _read("docs/telegram/security.md")

    assert "Authoritative binding and durable-thread metadata live in hub" in (
        architecture_text
    )
    assert "`.codex-autorunner/orchestration.sqlite3`" in architecture_text
    assert "Authoritative binding and durable-thread metadata live in hub" in (
        security_text
    )


def test_plugin_api_doc_stays_consistent_with_capability_and_descriptor_surface() -> (
    None
):
    plugin_text = _read("docs/plugin-api.md")

    required_capabilities = _listed_capabilities(
        _section(plugin_text, "Required Capabilities")
    )
    optional_capabilities = _listed_capabilities(
        _section(plugin_text, "Optional Capabilities")
    )
    documented_capabilities = required_capabilities | optional_capabilities
    canonical_capabilities = {str(capability) for capability in RUNTIME_CAPABILITIES}

    assert optional_capabilities <= canonical_capabilities
    assert documented_capabilities == canonical_capabilities

    for field in fields(AgentDescriptor):
        assert f"{field.name}=" in plugin_text
