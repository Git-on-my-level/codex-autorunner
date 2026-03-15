from __future__ import annotations

from pathlib import Path


def _read(rel_path: str) -> str:
    return Path(rel_path).read_text(encoding="utf-8")


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
