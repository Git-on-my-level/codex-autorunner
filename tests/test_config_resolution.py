import json
import os
from pathlib import Path

import pytest
import yaml

from codex_autorunner.bootstrap import GENERATED_CONFIG_HEADER
from codex_autorunner.core.config import (
    ACTIVE_HUB_ROOT_ENV,
    CONFIG_FILENAME,
    DEFAULT_REPO_CONFIG,
    REPO_OVERRIDE_FILENAME,
    ConfigError,
    default_housekeeping_rule_named,
    load_hub_config,
    load_repo_config,
    resolve_env_for_root,
    resolve_housekeeping_rule,
)
from codex_autorunner.housekeeping import HousekeepingConfig, HousekeepingRule
from tests.conftest import write_test_config


def test_load_hub_config_prefers_config_over_root_overrides(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    write_test_config(hub_root / "codex-autorunner.yml", {"server": {"port": 5000}})
    write_test_config(
        hub_root / "codex-autorunner.override.yml", {"server": {"port": 6000}}
    )

    config_dir = hub_root / ".codex-autorunner"
    config_dir.mkdir()
    write_test_config(
        config_dir / "config.yml",
        {"mode": "hub", "server": {"port": 7000}},
    )

    config = load_hub_config(hub_root)
    assert config.server_port == 7000


def test_load_hub_config_uses_root_override_when_config_missing(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    write_test_config(hub_root / "codex-autorunner.yml", {"server": {"port": 5000}})
    write_test_config(
        hub_root / "codex-autorunner.override.yml", {"server": {"port": 6000}}
    )

    config_dir = hub_root / ".codex-autorunner"
    config_dir.mkdir()
    write_test_config(config_dir / "config.yml", {"mode": "hub"})

    config = load_hub_config(hub_root)
    assert config.server_port == 6000


def test_load_hub_config_upgrades_stale_generated_pma_default(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    config_path = hub_root / CONFIG_FILENAME
    write_test_config(
        config_path,
        {"mode": "hub", "pma": {"max_text_chars": 800}},
    )
    config_path.write_text(
        GENERATED_CONFIG_HEADER + config_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    config = load_hub_config(hub_root)

    assert config.pma.max_text_chars == 10_000
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted == {"version": 2, "mode": "hub"}


def test_load_hub_config_preserves_explicit_root_pma_max_text_chars(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / "codex-autorunner.yml",
        {"pma": {"max_text_chars": 800}},
    )

    config_path = hub_root / CONFIG_FILENAME
    write_test_config(
        config_path,
        {"mode": "hub", "pma": {"max_text_chars": 800}},
    )
    config_path.write_text(
        GENERATED_CONFIG_HEADER + config_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    config = load_hub_config(hub_root)

    assert config.pma.max_text_chars == 800
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted == {"version": 2, "mode": "hub"}


def test_load_repo_config_inherits_hub_shared_settings(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "agents": {"opencode": {"binary": "/opt/opencode"}},
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.agent_binary("opencode") == "/opt/opencode"


def test_load_repo_config_uses_tighter_default_opencode_retention(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)

    assert config.opencode.server_scope == "workspace"
    assert config.opencode.max_handles == 4
    assert config.opencode.idle_ttl_seconds == 900


def test_load_repo_config_parses_typed_core_sections(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "security": {"redact_run_logs": False},
                "notifications": {
                    "enabled": "auto",
                    "tui_idle_seconds": 45.0,
                },
                "voice": {
                    "enabled": True,
                    "provider": "local_whisper",
                    "latency_mode": "low",
                },
            },
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.security["redact_run_logs"] is False
    assert config.notifications["enabled"] == "auto"
    assert config.notifications["tui_idle_seconds"] == 45
    assert config.voice["enabled"] is True
    assert config.voice["provider"] == "local_whisper"
    assert config.voice["latency_mode"] == "low"
    assert config.effective_destination["kind"] == "local"


def test_load_repo_config_accepts_github_mutation_policy_bool_and_enum_values(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "github": {
                    "automation": {
                        "policy": {
                            "post_pr_comment": True,
                            "merge_pr": "require_approval",
                        }
                    }
                }
            },
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)

    assert config.raw["github"]["automation"]["policy"]["post_pr_comment"] is True
    assert (
        config.raw["github"]["automation"]["policy"]["merge_pr"] == "require_approval"
    )


def test_resolve_housekeeping_rule_matches_by_name() -> None:
    config = HousekeepingConfig(
        enabled=True,
        interval_seconds=60,
        min_file_age_seconds=10,
        dry_run=False,
        rules=[
            HousekeepingRule(
                name="update_cache",
                kind="directory",
                path="~/.codex-autorunner/update_cache",
                recursive=True,
                max_files=77,
            )
        ],
    )

    rule = resolve_housekeeping_rule(config, "UPDATE_CACHE")

    assert rule is not None
    assert rule.max_files == 77


@pytest.mark.parametrize(
    ("rule_name", "include_repo_review_runs", "include_hub_update_rules", "path"),
    [
        ("run_logs", True, False, ".codex-autorunner/runs"),
        (
            "terminal_image_uploads",
            True,
            False,
            ".codex-autorunner/uploads/terminal-images",
        ),
        (
            "telegram_images",
            True,
            False,
            ".codex-autorunner/uploads/telegram-images",
        ),
        (
            "telegram_voice",
            True,
            False,
            ".codex-autorunner/uploads/telegram-voice",
        ),
        (
            "telegram_files",
            True,
            False,
            ".codex-autorunner/uploads/telegram-files",
        ),
        ("github_context", True, False, ".codex-autorunner/github_context"),
        ("review_runs", True, False, ".codex-autorunner/review/runs"),
        ("update_cache", False, True, "~/.codex-autorunner/update_cache"),
        ("update_log", False, True, "~/.codex-autorunner/update-standalone.log"),
    ],
)
def test_default_housekeeping_rule_named_exposes_canonical_defaults(
    rule_name: str,
    include_repo_review_runs: bool,
    include_hub_update_rules: bool,
    path: str,
) -> None:
    rule = default_housekeeping_rule_named(
        rule_name,
        include_repo_review_runs=include_repo_review_runs,
        include_hub_update_rules=include_hub_update_rules,
    )

    assert rule is not None
    assert rule.path == path


@pytest.mark.parametrize("field", ["max_payload_bytes", "max_raw_payload_bytes"])
def test_load_repo_config_rejects_boolean_github_webhook_size_limits(
    tmp_path: Path, field: str
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "github": {
                    "automation": {
                        "webhook_ingress": {
                            field: True,
                        }
                    }
                }
            },
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(
        ConfigError,
        match=(f"github\\.automation\\.webhook_ingress\\.{field} must be an integer"),
    ):
        load_repo_config(repo_root, hub_path=hub_root)


@pytest.mark.parametrize(
    "field",
    [
        "watch_window_minutes",
        "interval_seconds",
        "discovery_interval_seconds",
        "discovery_workspace_limit",
        "post_open_boost_minutes",
        "post_open_boost_interval_seconds",
    ],
)
def test_load_repo_config_rejects_boolean_github_polling_size_limits(
    tmp_path: Path, field: str
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "github": {
                    "automation": {
                        "polling": {
                            field: True,
                        }
                    }
                }
            },
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(
        ConfigError,
        match=(f"github\\.automation\\.polling\\.{field} must be an integer"),
    ):
        load_repo_config(repo_root, hub_path=hub_root)


def test_load_repo_config_rejects_invalid_github_reaction_profile(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "github": {
                    "automation": {
                        "reactions": {
                            "profile": "chatty",
                        }
                    }
                }
            },
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(
        ConfigError,
        match=(
            "github\\.automation\\.reactions\\.profile must be 'all' or 'minimal_noise'"
        ),
    ):
        load_repo_config(repo_root, hub_path=hub_root)


def test_load_repo_config_rejects_invalid_github_mutation_policy_value(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {
                "github": {
                    "automation": {
                        "policy": {
                            "post_pr_comment": "sometimes",
                        }
                    }
                }
            },
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(
        ConfigError,
        match=(
            "github\\.automation\\.policy\\.post_pr_comment must be boolean or one of: "
            "allow, deny, require_approval"
        ),
    ):
        load_repo_config(repo_root, hub_path=hub_root)


def test_update_backend_null_defaults_to_auto(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "update": {"backend": None}},
    )

    config = load_hub_config(hub_root)
    assert config.update_backend == "auto"


def test_repo_override_file_overrides_repo_defaults(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {"runner": {"sleep_seconds": 5}},
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    write_test_config(
        repo_root / REPO_OVERRIDE_FILENAME,
        {"runner": {"sleep_seconds": 11}},
    )

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.runner_sleep_seconds == 11


def test_repo_override_rejects_mode_and_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub"},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    write_test_config(
        repo_root / REPO_OVERRIDE_FILENAME,
        {"mode": "repo", "version": 2},
    )

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)


def test_load_hub_config_accepts_collaboration_policy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "collaboration_policy": {
                "discord": {
                    "default_mode": "denied",
                    "destinations": [
                        {
                            "guild_id": "123",
                            "channel_id": "456",
                            "mode": "active",
                            "plain_text_trigger": "mentions",
                        }
                    ],
                }
            },
        },
    )

    config = load_hub_config(hub_root)
    assert isinstance(config.raw, dict)
    assert config.raw["collaboration_policy"]["discord"]["default_mode"] == "denied"


def test_load_hub_config_rejects_invalid_collaboration_policy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "collaboration_policy": {
                "telegram": {
                    "destinations": [
                        {
                            "chat_id": -1001,
                            "mode": "invalid",
                        }
                    ]
                }
            },
        },
    )

    with pytest.raises(
        ConfigError,
        match="collaboration_policy.telegram.destinations\\[0\\]\\.mode",
    ):
        load_hub_config(hub_root)


def test_repo_env_overrides_hub_env(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub"},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    (hub_root / ".env").write_text("CAR_DOTENV_TEST=hub\n", encoding="utf-8")
    (repo_root / ".env").write_text("CAR_DOTENV_TEST=repo\n", encoding="utf-8")

    previous = os.environ.get("CAR_DOTENV_TEST")
    try:
        load_repo_config(repo_root, hub_path=hub_root)
        assert os.environ.get("CAR_DOTENV_TEST") == "repo"
    finally:
        if previous is None:
            os.environ.pop("CAR_DOTENV_TEST", None)
        else:
            os.environ["CAR_DOTENV_TEST"] = previous


def test_resolve_env_for_root_isolated(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env").write_text("CAR_DOTENV_TEST=repo\n", encoding="utf-8")

    base_env = {"CAR_DOTENV_TEST": "hub"}
    previous = os.environ.get("CAR_DOTENV_TEST")
    env = resolve_env_for_root(repo_root, base_env=base_env)
    assert env["CAR_DOTENV_TEST"] == "repo"
    assert os.environ.get("CAR_DOTENV_TEST") == previous


def test_repo_docs_reject_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["docs"]["active_context"] = "/tmp/active_context.md"
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"docs": cfg["docs"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_docs_reject_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["docs"]["spec"] = "../spec.md"
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"docs": cfg["docs"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_log_rejects_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["log"]["path"] = "/tmp/codex.log"
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"log": cfg["log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_log_rejects_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["log"]["path"] = "../codex.log"
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"log": cfg["log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_server_log_rejects_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["server_log"] = {"path": "/tmp/server.log"}
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"server_log": cfg["server_log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="server_log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_server_log_rejects_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    cfg = json.loads(json.dumps(DEFAULT_REPO_CONFIG))
    cfg["server_log"] = {"path": "../server.log"}
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "repo_defaults": {"server_log": cfg["server_log"]}},
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigError, match="server_log.path"):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_log_accepts_valid_relative_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {"log": {"path": ".codex-autorunner/codex.log"}},
        },
    )

    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.log.path.name == "codex.log"


def test_hub_log_rejects_absolute_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "hub": {
                "repos_root": "repos",
                "worktrees_root": "worktrees",
                "manifest": "manifest.yml",
                "discover_depth": 1,
                "auto_init_missing": False,
                "log": {
                    "path": "/tmp/codex.log",
                    "max_bytes": 10485760,
                    "backup_count": 5,
                },
            },
        },
    )

    with pytest.raises(ConfigError, match="log.path"):
        load_hub_config(hub_root)


def test_hub_server_log_rejects_parent_segments(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "hub": {
                "repos_root": "repos",
                "worktrees_root": "worktrees",
                "manifest": "manifest.yml",
                "discover_depth": 1,
                "auto_init_missing": False,
                "log": {
                    "path": ".codex-autorunner/codex.log",
                    "max_bytes": 10485760,
                    "backup_count": 5,
                },
            },
            "server_log": {
                "path": "../server.log",
                "max_bytes": 10485760,
                "backup_count": 5,
            },
        },
    )

    with pytest.raises(ConfigError, match="server_log.path"):
        load_hub_config(hub_root)


def test_static_assets_validation_rejects_negative_max_cache_entries(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "static_assets": {
                "cache_root": ".codex-autorunner/static-cache",
                "max_cache_entries": -1,
                "max_cache_age_days": 30,
            },
        },
    )

    with pytest.raises(
        ConfigError, match="static_assets.max_cache_entries must be >= 0"
    ):
        load_hub_config(hub_root)


def test_static_assets_validation_rejects_negative_max_cache_age_days(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "static_assets": {
                "cache_root": ".codex-autorunner/static-cache",
                "max_cache_entries": 5,
                "max_cache_age_days": -1,
            },
        },
    )

    with pytest.raises(
        ConfigError, match="static_assets.max_cache_age_days must be >= 0"
    ):
        load_hub_config(hub_root)


def test_static_assets_validation_allows_null_max_cache_age_days(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "static_assets": {
                "cache_root": ".codex-autorunner/static-cache",
                "max_cache_entries": 5,
                "max_cache_age_days": None,
            },
        },
    )

    load_hub_config(hub_root)


def test_housekeeping_validation_rejects_invalid_interval_seconds(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "housekeeping": {
                "enabled": True,
                "interval_seconds": 0,
            },
        },
    )

    with pytest.raises(ConfigError, match="housekeeping.interval_seconds must be > 0"):
        load_hub_config(hub_root)


def test_housekeeping_validation_rejects_negative_min_file_age_seconds(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "housekeeping": {
                "enabled": True,
                "interval_seconds": 3600,
                "min_file_age_seconds": -1,
            },
        },
    )

    with pytest.raises(
        ConfigError, match="housekeeping.min_file_age_seconds must be >= 0"
    ):
        load_hub_config(hub_root)


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("report_max_history_files", "bad"),
        ("report_max_total_bytes", "bad"),
        ("app_server_workspace_max_age_days", "bad"),
    ),
)
def test_pma_validation_rejects_invalid_state_cleanup_knobs(
    tmp_path: Path, key: str, value: object
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "pma": {
                key: value,
            },
        },
    )

    with pytest.raises(ConfigError, match=key):
        load_hub_config(hub_root)


def test_repo_ticket_flow_invalid_approval_mode_fails_fast(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {"ticket_flow": {"approval_mode": "invalid"}},
        },
    )
    repo_root = hub_root / "repo"
    repo_root.mkdir()

    with pytest.raises(
        ConfigError,
        match="ticket_flow.approval_mode must be one of: yolo, review, safe",
    ):
        load_repo_config(repo_root, hub_path=hub_root)


def test_repo_ticket_flow_safe_alias_normalizes_to_review(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {
            "mode": "hub",
            "repo_defaults": {"ticket_flow": {"approval_mode": "safe"}},
        },
    )
    repo_root = hub_root / "repo"
    repo_root.mkdir()

    config = load_repo_config(repo_root, hub_path=hub_root)
    assert config.ticket_flow.approval_mode == "review"


def test_load_repo_config_fails_when_manifest_version_is_unsupported(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
    repo_root = hub_root / "repo"
    repo_root.mkdir()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "version: 1\nrepos: []\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="Failed to resolve effective destination from hub manifest"
    ):
        load_repo_config(repo_root, hub_path=hub_root)


def test_load_repo_config_fails_when_manifest_yaml_is_invalid(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
    repo_root = hub_root / "repo"
    repo_root.mkdir()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "version: 2\nrepos:\n  - id: base\n    path: workspace/base\n    kind: [\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="Failed to resolve effective destination from hub manifest"
    ):
        load_repo_config(repo_root, hub_path=hub_root)


def test_load_repo_config_preserves_explicit_empty_app_server_command(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
    repo_root = hub_root / "repo"
    repo_root.mkdir()
    write_test_config(
        repo_root / REPO_OVERRIDE_FILENAME,
        {"app_server": {"command": []}},
    )

    config = load_repo_config(repo_root, hub_path=hub_root)

    assert config.app_server.command == []


def test_load_hub_config_raises_when_no_config_in_git_repo(tmp_path: Path) -> None:
    """Test that load_hub_config raises ConfigError without creating hub config."""
    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()

    config_path = git_repo / ".codex-autorunner" / "config.yml"
    assert not config_path.exists(), "Precondition: no config should exist"

    with pytest.raises(ConfigError, match="Missing hub config file"):
        load_hub_config(git_repo)

    assert not config_path.exists(), "load_hub_config should not create config file"


def test_load_hub_config_raises_without_seeding_in_empty_dir(tmp_path: Path) -> None:
    """Test that load_hub_config raises ConfigError in empty directory."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    config_path = empty_dir / ".codex-autorunner" / "config.yml"
    assert not config_path.exists(), "Precondition: no config should exist"

    with pytest.raises(ConfigError, match="Missing hub config file"):
        load_hub_config(empty_dir)

    assert not config_path.exists(), "load_hub_config should not create config file"


def test_load_repo_config_raises_when_no_hub_config(tmp_path: Path) -> None:
    """Test that load_repo_config raises ConfigError when no hub config exists."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    (hub_root / ".git").mkdir()

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    config_path = hub_root / ".codex-autorunner" / "config.yml"
    assert not config_path.exists(), "Precondition: no hub config should exist"

    with pytest.raises(ConfigError, match="Hub config not found"):
        load_repo_config(repo_root, hub_path=hub_root)

    assert not config_path.exists(), "load_repo_config should not create hub config"


def test_load_repo_config_uses_active_hub_root_env_for_out_of_tree_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

    repo_root = tmp_path / "external-repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    monkeypatch.setenv(ACTIVE_HUB_ROOT_ENV, str(hub_root))

    config = load_repo_config(repo_root)

    assert config.root == repo_root.resolve()


def test_load_repo_config_prefers_nearest_hub_over_active_hub_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_hub_root = tmp_path / "env-hub"
    env_hub_root.mkdir()
    write_test_config(
        env_hub_root / CONFIG_FILENAME,
        {"mode": "hub", "agents": {"opencode": {"binary": "/env/opencode"}}},
    )

    local_hub_root = tmp_path / "local-hub"
    local_hub_root.mkdir()
    write_test_config(
        local_hub_root / CONFIG_FILENAME,
        {"mode": "hub", "agents": {"opencode": {"binary": "/local/opencode"}}},
    )

    repo_root = local_hub_root / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    monkeypatch.setenv(ACTIVE_HUB_ROOT_ENV, str(env_hub_root))

    config = load_repo_config(repo_root)

    assert config.agent_binary("opencode") == "/local/opencode"


def test_ensure_hub_config_at_seeds_when_missing(tmp_path: Path) -> None:
    """Test that ensure_hub_config_at seeds hub config when missing."""
    from codex_autorunner.core.config import ensure_hub_config_at

    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()

    config_path = git_repo / ".codex-autorunner" / "config.yml"
    assert not config_path.exists(), "Precondition: no config should exist"

    result_path, did_init = ensure_hub_config_at(git_repo)

    assert did_init is True, "Should have initialized hub config"
    assert result_path == config_path
    assert config_path.exists(), "ensure_hub_config_at should create config file"
    data = yaml.safe_load(config_path.read_text())
    assert data.get("mode") == "hub"


def test_ensure_hub_config_at_returns_existing_without_seeding(tmp_path: Path) -> None:
    """Test that ensure_hub_config_at returns existing config without seeding."""
    from codex_autorunner.core.config import ensure_hub_config_at

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    config_path = hub_root / CONFIG_FILENAME
    write_test_config(config_path, {"mode": "hub"})

    result_path, did_init = ensure_hub_config_at(hub_root)

    assert did_init is False, "Should not have initialized when config exists"
    assert result_path == config_path


class TestPrecedenceBoundaries:
    """Characterization tests locking down config precedence invariants.

    Precedence order (lowest to highest):
      built-in defaults < codex-autorunner.yml < codex-autorunner.override.yml
      < .codex-autorunner/config.yml < env-dependent overrides
    """

    def test_built_in_defaults_used_when_no_user_config(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

        config = load_hub_config(hub_root)
        assert config.mode == "hub"

    def test_root_config_overrides_built_in_defaults(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / "codex-autorunner.yml",
            {"server": {"port": 5000}},
        )
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

        config = load_hub_config(hub_root)
        assert config.server_port == 5000

    def test_root_override_overrides_root_config(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / "codex-autorunner.yml",
            {"server": {"port": 5000}},
        )
        write_test_config(
            hub_root / "codex-autorunner.override.yml",
            {"server": {"port": 6000}},
        )
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

        config = load_hub_config(hub_root)
        assert config.server_port == 6000

    def test_generated_config_overrides_root_override(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / "codex-autorunner.yml",
            {"server": {"port": 5000}},
        )
        write_test_config(
            hub_root / "codex-autorunner.override.yml",
            {"server": {"port": 6000}},
        )
        config_dir = hub_root / ".codex-autorunner"
        config_dir.mkdir()
        write_test_config(
            config_dir / "config.yml",
            {"mode": "hub", "server": {"port": 7000}},
        )

        config = load_hub_config(hub_root)
        assert config.server_port == 7000

    def test_repo_derivation_layering_hub_shared_then_repo_defaults_then_override(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / CONFIG_FILENAME,
            {
                "mode": "hub",
                "agents": {"opencode": {"binary": "/hub-opencode"}},
                "repo_defaults": {
                    "runner": {"sleep_seconds": 3},
                    "security": {"redact_run_logs": True},
                },
            },
        )

        repo_root = hub_root / "repo"
        repo_root.mkdir()
        write_test_config(
            repo_root / REPO_OVERRIDE_FILENAME,
            {"runner": {"sleep_seconds": 7}},
        )

        config = load_repo_config(repo_root, hub_path=hub_root)
        assert config.agent_binary("opencode") == "/hub-opencode"
        assert config.runner_sleep_seconds == 7
        assert config.security["redact_run_logs"] is True

    def test_repo_override_rejects_mode_key(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
        repo_root = hub_root / "repo"
        repo_root.mkdir()
        write_test_config(
            repo_root / REPO_OVERRIDE_FILENAME,
            {"mode": "repo"},
        )

        with pytest.raises(ConfigError, match="must not set mode or version"):
            load_repo_config(repo_root, hub_path=hub_root)


class TestGeneratedHubConfigNormalization:
    """Characterization tests for sparse generated config normalization."""

    def test_stale_generated_pma_800_upgraded_to_10000(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        from codex_autorunner.bootstrap import GENERATED_CONFIG_HEADER

        config_path = hub_root / CONFIG_FILENAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": 800},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        config = load_hub_config(hub_root)
        assert config.pma.max_text_chars == 10_000

        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert persisted == {"version": 2, "mode": "hub"}

    def test_explicit_root_pma_800_is_preserved(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        from codex_autorunner.bootstrap import GENERATED_CONFIG_HEADER

        write_test_config(
            hub_root / "codex-autorunner.yml",
            {"pma": {"max_text_chars": 800}},
        )
        config_path = hub_root / CONFIG_FILENAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": 2,
            "mode": "hub",
            "pma": {"max_text_chars": 800},
        }
        config_path.write_text(
            GENERATED_CONFIG_HEADER + yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

        config = load_hub_config(hub_root)
        assert config.pma.max_text_chars == 800


class TestCompatibilityBehaviorLocking:
    """Lock compatibility invariants that must not regress."""

    def test_safe_approval_mode_normalizes_to_review(self, tmp_path: Path) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(
            hub_root / CONFIG_FILENAME,
            {
                "mode": "hub",
                "repo_defaults": {"ticket_flow": {"approval_mode": "safe"}},
            },
        )
        repo_root = hub_root / "repo"
        repo_root.mkdir()

        config = load_repo_config(repo_root, hub_path=hub_root)
        assert config.ticket_flow.approval_mode == "review"

    def test_explicit_empty_app_server_command_is_distinct_from_unset(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
        repo_root = hub_root / "repo"
        repo_root.mkdir()
        write_test_config(
            repo_root / REPO_OVERRIDE_FILENAME,
            {"app_server": {"command": []}},
        )

        config = load_repo_config(repo_root, hub_path=hub_root)
        assert config.app_server.command == []

    def test_repo_config_forces_mode_repo_regardless_of_hub(
        self, tmp_path: Path
    ) -> None:
        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
        repo_root = hub_root / "repo"
        repo_root.mkdir()

        config = load_repo_config(repo_root, hub_path=hub_root)
        assert config.mode == "repo"

    def test_repo_config_forces_version_to_current(self, tmp_path: Path) -> None:
        from codex_autorunner.core.config_contract import CONFIG_VERSION

        hub_root = tmp_path / "hub"
        hub_root.mkdir()
        write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})
        repo_root = hub_root / "repo"
        repo_root.mkdir()

        config = load_repo_config(repo_root, hub_path=hub_root)
        assert config.version == CONFIG_VERSION
