from codex_autorunner.integrations.telegram.helpers import (
    _coerce_model_options,
    _format_model_list,
)


def test_coerce_model_options_uses_model_id_for_alias_display_name() -> None:
    options = _coerce_model_options(
        {
            "data": [
                {
                    "id": "gpt-5.3-codex-spark",
                    "displayName": "GPT-5.3-Codex-Spark",
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": ["low", "medium", "high"],
                }
            ]
        }
    )

    assert len(options) == 1
    assert options[0].label == "gpt-5.3-codex-spark (default medium)"


def test_format_model_list_uses_model_id_for_alias_display_name() -> None:
    result = _format_model_list(
        {
            "data": [
                {
                    "id": "gpt-5.3-codex-spark",
                    "displayName": "GPT-5.3-Codex-Spark",
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": ["low", "medium", "high"],
                }
            ]
        }
    )

    assert "gpt-5.3-codex-spark (GPT-5.3-Codex-Spark)" not in result
    assert "gpt-5.3-codex-spark [effort: low, medium, high] (default medium)" in result


def test_coerce_model_options_keeps_distinct_display_name() -> None:
    options = _coerce_model_options(
        {
            "data": [
                {
                    "id": "internal-preview-model",
                    "displayName": "Internal Preview (Fast)",
                    "defaultReasoningEffort": "medium",
                }
            ]
        }
    )

    assert len(options) == 1
    assert (
        options[0].label
        == "internal-preview-model (Internal Preview (Fast)) (default medium)"
    )
