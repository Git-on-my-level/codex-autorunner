from __future__ import annotations

from codex_autorunner.agents.acp import (
    ACPClient,
    ACPMissingSessionError,
    build_missing_session_matcher,
)
from codex_autorunner.agents.acp.errors import ACPResponseError
from codex_autorunner.agents.acp.protocol import extract_model_catalog

# --------------------------------------------------------------------------- #
# G1: injectable missing-session matcher
# --------------------------------------------------------------------------- #


def _client(**kwargs: object) -> ACPClient:
    return ACPClient(["fake-acp"], **kwargs)  # type: ignore[arg-type]


def test_default_matcher_treats_only_canonical_code_as_missing() -> None:
    """Without a custom matcher, only -32004 is a missing-session signal."""
    client = _client()
    canonical = client._response_error(
        "session/load", {"code": -32004, "message": "session not found"}
    )
    assert isinstance(canonical, ACPMissingSessionError)

    # A generic internal-error code (OMP's -32603) stays a generic response
    # error so the matcher cannot accidentally swallow unrelated failures.
    generic = client._response_error(
        "session/load",
        {
            "code": -32603,
            "message": "Internal error",
            "data": {"details": "ACP session not found: nope"},
        },
    )
    assert type(generic) is ACPResponseError
    assert not isinstance(generic, ACPMissingSessionError)


def test_custom_matcher_classifies_runtime_missing_session_code() -> None:
    """A runtime supplying a matcher maps its missing-session signal cleanly."""
    client = _client(
        missing_session_matcher=build_missing_session_matcher(
            data_contains="session not found"
        )
    )

    mapped = client._response_error(
        "session/load",
        {
            "code": -32603,
            "message": "Internal error",
            "data": {"details": "ACP session not found: nope"},
        },
    )
    assert isinstance(mapped, ACPMissingSessionError)
    assert mapped.code == -32603

    # The same generic code without the missing-session signature stays generic,
    # so genuine internal errors are never misclassified.
    unrelated = client._response_error(
        "session/load",
        {"code": -32603, "message": "Internal error", "data": {"details": "disk full"}},
    )
    assert type(unrelated) is ACPResponseError

    # The canonical code keeps working under a custom matcher too.
    canonical = client._response_error(
        "session/load", {"code": -32004, "message": "session not found"}
    )
    assert isinstance(canonical, ACPMissingSessionError)


def test_missing_session_match_only_applies_to_session_load() -> None:
    """A missing-session matcher must not affect other methods."""
    client = _client(
        missing_session_matcher=build_missing_session_matcher(
            data_contains="session not found"
        )
    )
    other = client._response_error(
        "session/prompt",
        {"code": -32603, "message": "session not found"},
    )
    assert type(other) is ACPResponseError
    assert not isinstance(other, ACPMissingSessionError)


def test_build_missing_session_matcher_by_code() -> None:
    matcher = build_missing_session_matcher(codes=(-32603,))
    assert matcher("session/load", -32603, "x", None) is True
    assert matcher("session/load", -32004, "x", None) is False


def test_build_missing_session_matcher_by_data_substring_case_insensitive() -> None:
    matcher = build_missing_session_matcher(data_contains="Session NOT Found")
    assert (
        matcher("session/load", -32603, "Internal", {"details": "session not found: x"})
        is True
    )
    assert matcher("session/load", -32603, "session not found", None) is True
    assert (
        matcher("session/load", -32603, "disk full", {"details": "no space"}) is False
    )


# --------------------------------------------------------------------------- #
# G2: extract_model_catalog from configOptions
# --------------------------------------------------------------------------- #


def _omp_config_options() -> list[object]:
    return [
        {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": "default",
            "options": [
                {"value": "default", "name": "Default"},
                {"value": "plan", "name": "Plan"},
            ],
        },
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": "zai/glm-5.2",
            "options": [
                {
                    "value": "zai/glm-5.2",
                    "name": "GLM-5.2",
                    "description": "zai/glm-5.2",
                },
                {"value": "anthropic/claude-haiku-4-5", "name": "Claude Haiku 4.5"},
            ],
        },
        {
            "id": "thinking",
            "name": "Thinking",
            "category": "thought_level",
            "type": "select",
            "currentValue": "high",
            "options": [
                {"value": "off", "name": "Off"},
                {"value": "auto", "name": "Auto"},
                {"value": "minimal", "name": "minimal"},
                {"value": "low", "name": "low"},
                {"value": "medium", "name": "medium"},
                {"value": "high", "name": "high"},
                {"value": "xhigh", "name": "xhigh"},
            ],
        },
    ]


def test_extract_model_catalog_omp_shape() -> None:
    catalog = extract_model_catalog(_omp_config_options())
    assert catalog is not None
    assert catalog.default_model == "zai/glm-5.2"
    assert [m.id for m in catalog.models] == [
        "zai/glm-5.2",
        "anthropic/claude-haiku-4-5",
    ]
    glm = catalog.models[0]
    assert glm.display_name == "GLM-5.2"
    assert glm.supports_reasoning is True
    assert glm.reasoning_options == ["minimal", "low", "medium", "high", "xhigh"]


def test_extract_model_catalog_none_without_model_select() -> None:
    assert extract_model_catalog(None) is None
    assert extract_model_catalog([]) is None
    assert extract_model_catalog([{"id": "mode", "category": "mode"}]) is None


def test_extract_model_catalog_without_thought_level() -> None:
    options = [
        {
            "id": "model",
            "category": "model",
            "currentValue": "a",
            "options": [{"value": "a", "name": "A"}, {"value": "b", "name": "B"}],
        }
    ]
    catalog = extract_model_catalog(options)
    assert catalog is not None
    assert catalog.models[0].supports_reasoning is False
    assert catalog.models[0].reasoning_options == []


def test_extract_model_catalog_falls_back_to_first_when_no_default() -> None:
    options = [
        {
            "id": "model",
            "category": "model",
            "options": [{"value": "a", "name": "A"}, {"value": "b", "name": "B"}],
        }
    ]
    catalog = extract_model_catalog(options)
    assert catalog is not None
    assert catalog.default_model == "a"


def test_extract_model_catalog_uses_value_when_name_missing() -> None:
    options = [
        {
            "id": "model",
            "category": "model",
            "currentValue": "a/b",
            "options": [{"value": "a/b"}],
        }
    ]
    catalog = extract_model_catalog(options)
    assert catalog is not None
    assert catalog.models[0].display_name == "a/b"
