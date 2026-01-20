from codex_autorunner.routes.agents import _build_opencode_model_catalog


def test_build_opencode_model_catalog_from_list_models() -> None:
    payload = {
        "default": {"openai": "gpt-4o-mini"},
        "providers": [
            {
                "id": "openai",
                "models": [
                    {"id": "gpt-4o", "name": "GPT-4o", "limit": {"context": 128000}},
                    {"id": "gpt-4o-mini"},
                ],
            }
        ],
    }

    catalog = _build_opencode_model_catalog(payload)

    model_ids = {model.id for model in catalog.models}
    assert catalog.default_model == "openai/gpt-4o-mini"
    assert "openai/gpt-4o" in model_ids
    assert "openai/gpt-4o-mini" in model_ids
