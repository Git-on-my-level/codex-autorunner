from codex_autorunner.core.scm_reaction_types import ScmReactionConfig


def test_scm_reaction_config_minimal_noise_profile_disables_lower_signal_reactions() -> (
    None
):
    config = ScmReactionConfig.from_mapping({"reactions": {"profile": "minimal_noise"}})

    assert config.ci_failed is True
    assert config.changes_requested is True
    assert config.approved_and_green is False
    assert config.merged is False


def test_scm_reaction_config_profile_allows_explicit_overrides() -> None:
    config = ScmReactionConfig.from_mapping(
        {
            "reactions": {
                "profile": "minimal_noise",
                "approved_and_green": True,
            }
        }
    )

    assert config.approved_and_green is True
    assert config.merged is False
