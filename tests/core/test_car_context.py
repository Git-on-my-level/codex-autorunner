from __future__ import annotations

from codex_autorunner.core.car_context import (
    DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE,
    DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    build_car_context_bundle,
    default_managed_thread_context_profile,
    is_car_artifact_path,
    normalize_car_context_profile,
    render_runtime_compat_agents_md,
    resolve_effective_car_context_profile,
)


def test_default_managed_thread_context_profile_for_repo_threads() -> None:
    assert DEFAULT_REPO_THREAD_CONTEXT_PROFILE == "car_ambient"
    assert default_managed_thread_context_profile(resource_kind="repo") == "car_ambient"
    assert default_managed_thread_context_profile(resource_kind=None) == "car_ambient"


def test_default_managed_thread_context_profile_for_agent_workspaces() -> None:
    assert DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE == "none"
    assert (
        default_managed_thread_context_profile(resource_kind="agent_workspace")
        == "none"
    )


def test_ambient_profile_only_escalates_on_car_triggers() -> None:
    assert (
        resolve_effective_car_context_profile("car_ambient", prompt_text="fix a test")
        == "none"
    )
    assert (
        resolve_effective_car_context_profile(
            "car_ambient",
            prompt_text="please update .codex-autorunner/contextspace/spec.md",
        )
        == "car_core"
    )
    assert (
        resolve_effective_car_context_profile(
            "car_ambient",
            target_path=".codex-autorunner/tickets/TICKET-001.md",
        )
        == "car_core"
    )


def test_is_car_artifact_path_matches_codex_autorunner_tree() -> None:
    assert is_car_artifact_path(".codex-autorunner/tickets/TICKET-001.md")
    assert is_car_artifact_path(".codex-autorunner/contextspace/spec.md")
    assert not is_car_artifact_path("src/app.py")


def test_bundle_and_runtime_projection_for_none_profile_are_empty() -> None:
    bundle = build_car_context_bundle("none")
    assert bundle.declared_profile == "none"
    assert bundle.effective_profile == "none"
    assert render_runtime_compat_agents_md(bundle) == ""


def test_runtime_projection_for_core_profile_mentions_car_artifacts() -> None:
    bundle = build_car_context_bundle(
        "car_core", target_path=".codex-autorunner/tickets"
    )
    rendered = render_runtime_compat_agents_md(bundle)
    assert "# AGENTS" in rendered
    assert ".codex-autorunner/ABOUT_CAR.md" in rendered
    assert ".codex-autorunner/tickets/" in rendered


def test_normalize_car_context_profile_rejects_unknown_values() -> None:
    assert normalize_car_context_profile("car_core") == "car_core"
    assert normalize_car_context_profile("CAR_AMBIENT") == "car_ambient"
    assert normalize_car_context_profile("bogus") is None
