from codex_autorunner.integrations.telegram.helpers import (
    CodexFeatureRow,
    derive_codex_features_command,
    format_codex_features,
    parse_codex_features_list,
)


def test_parse_codex_features_list_basic() -> None:
    stdout = "unified_exec\tbeta\tfalse\nshell_snapshot\tbeta\ttrue\nweb_search_request\tstable\ttrue\n"
    rows = parse_codex_features_list(stdout)
    assert [r.key for r in rows] == [
        "unified_exec",
        "shell_snapshot",
        "web_search_request",
    ]
    assert rows[0].stage == "beta"
    assert rows[1].enabled is True
    assert rows[2].stage == "stable"


def test_parse_codex_features_list_ignores_malformed_lines() -> None:
    stdout = "\ninvalid_line\nmissing\tcols\nok_flag\tbeta\tfalse\n"
    rows = parse_codex_features_list(stdout)
    assert [r.key for r in rows] == ["ok_flag"]
    assert rows[0].enabled is False


def test_format_codex_features_beta_only() -> None:
    rows = [
        CodexFeatureRow(key="shell_snapshot", stage="beta", enabled=True),
        CodexFeatureRow(key="unified_exec", stage="beta", enabled=False),
        CodexFeatureRow(key="web_search_request", stage="stable", enabled=True),
    ]
    text = format_codex_features(rows, stage_filter="beta")
    assert "Codex feature flags (beta):" in text
    # Sorted output
    assert "- shell_snapshot: True" in text
    assert "- unified_exec: False" in text
    assert "web_search_request" not in text


def test_format_codex_features_all() -> None:
    rows = [
        CodexFeatureRow(key="alpha", stage="stable", enabled=True),
        CodexFeatureRow(key="bravo", stage="beta", enabled=False),
    ]
    text = format_codex_features(rows, stage_filter=None)
    assert "Codex feature flags (all):" in text
    assert "- alpha: True" in text
    assert "- bravo: False" in text
    assert "/experimental all" not in text  # already listing all


def test_derive_codex_features_command_strips_app_server_suffix() -> None:
    command = derive_codex_features_command(["/opt/codex/bin/codex", "app-server"])
    assert command == ["/opt/codex/bin/codex", "features", "list"]


def test_derive_codex_features_command_keeps_flags() -> None:
    command = derive_codex_features_command(["codex", "--foo", "app-server"])
    assert command == ["codex", "--foo", "features", "list"]


def test_derive_codex_features_command_fallback() -> None:
    command = derive_codex_features_command([])
    assert command == ["codex", "features", "list"]
