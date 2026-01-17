from codex_autorunner.codex_cli import apply_codex_options


def test_apply_codex_options_does_not_prepend_before_binary_when_no_subcommand():
    argv = ["codex", "--yolo"]
    updated = apply_codex_options(argv, model="gpt-5.2-codex")
    assert updated[0] == "codex"
    assert "--model" in updated
    assert "gpt-5.2-codex" in updated
