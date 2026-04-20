from codex_autorunner.core.scm_reaction_types import ScmReactionConfig


def test_scm_reaction_config_minimal_noise_profile_disables_lower_signal_reactions() -> (
    None
):
    config = ScmReactionConfig.from_mapping({"reactions": {"profile": "minimal_noise"}})

    assert config.ci_failed is True
    assert config.changes_requested is True
    assert config.review_comment is True
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
    assert config.review_comment is True
    assert config.merged is False


def test_scm_reaction_config_includes_ci_batch_defaults_and_overrides() -> None:
    defaults = ScmReactionConfig.from_mapping({})
    overridden = ScmReactionConfig.from_mapping(
        {
            "reactions": {
                "ci_failed_batch_window_seconds": 90,
                "ci_failed_batch_max_window_seconds": 240,
            }
        }
    )

    assert defaults.ci_failed_batch_window_seconds == 60
    assert defaults.ci_failed_batch_max_window_seconds == 180
    assert overridden.ci_failed_batch_window_seconds == 90
    assert overridden.ci_failed_batch_max_window_seconds == 240


def test_scm_reaction_config_normalizes_and_deduplicates_login_lists() -> None:
    config = ScmReactionConfig.from_mapping(
        {
            "reactions": {
                "github_login_whitelist": [" Reviewer ", "reviewer", "", "Approver"],
                "github_login_blacklist": ["bot[bot]", " BOT[bot] "],
                "github_login_allowlist": ["trusted-reviewer"],
                "github_login_denylist": ["noisy-reviewer"],
            }
        }
    )

    assert config.github_login_whitelist == (
        "reviewer",
        "approver",
        "trusted-reviewer",
    )
    assert config.github_login_blacklist == ("bot[bot]", "noisy-reviewer")


def test_scm_reaction_config_github_login_allowed_respects_allow_and_deny_lists() -> (
    None
):
    allow_and_deny = ScmReactionConfig.from_mapping(
        {
            "reactions": {
                "github_login_whitelist": ["reviewer", "trusted-user"],
                "github_login_blacklist": ["trusted-user"],
            }
        }
    )
    deny_only = ScmReactionConfig.from_mapping(
        {"reactions": {"github_login_blacklist": ["blocked-user"]}}
    )

    assert allow_and_deny.github_login_allowed("reviewer") is True
    assert allow_and_deny.github_login_allowed("trusted-user") is False
    assert allow_and_deny.github_login_allowed("someone-else") is False
    assert allow_and_deny.github_login_allowed(None) is False

    assert deny_only.github_login_allowed("ok-user") is True
    assert deny_only.github_login_allowed("blocked-user") is False
