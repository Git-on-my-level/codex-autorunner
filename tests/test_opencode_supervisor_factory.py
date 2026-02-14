import logging
from pathlib import Path

from tests.conftest import write_test_config

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    REPO_OVERRIDE_FILENAME,
    load_repo_config,
)
from codex_autorunner.integrations.agents import opencode_supervisor_factory


def test_build_opencode_supervisor_from_repo_config(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    write_test_config(
        repo_root / REPO_OVERRIDE_FILENAME,
        {
            "app_server": {
                "request_timeout": 91,
                "max_handles": 3,
                "idle_ttl_seconds": 120,
            },
            "opencode": {
                "session_stall_timeout_seconds": 55,
                "max_text_chars": 9999,
            },
            "agents": {
                "opencode": {
                    "binary": "/bin/opencode",
                    "serve_command": ["opencode", "serve", "--port", "0"],
                    "subagent_models": {"subagent": "model-x", "helper": "model-y"},
                }
            },
        },
    )

    config = load_repo_config(repo_root, hub_path=hub_root)
    captured: dict = {}

    def _fake_build_opencode_supervisor(**kwargs):
        captured.update(kwargs)
        return "supervisor"

    monkeypatch.setattr(
        opencode_supervisor_factory,
        "build_opencode_supervisor",
        _fake_build_opencode_supervisor,
    )

    logger = logging.getLogger("test.opencode")
    env = {"OPENCODE_SERVER_USERNAME": "alice"}
    supervisor = opencode_supervisor_factory.build_opencode_supervisor_from_repo_config(
        config,
        workspace_root=repo_root,
        logger=logger,
        base_env=env,
    )

    assert supervisor == "supervisor"
    assert captured["opencode_command"] == ["opencode", "serve", "--port", "0"]
    assert captured["opencode_binary"] == "/bin/opencode"
    assert captured["workspace_root"] == repo_root
    assert captured["logger"] is logger
    assert captured["request_timeout"] == 91
    assert captured["max_handles"] == 3
    assert captured["idle_ttl_seconds"] == 120
    assert captured["session_stall_timeout_seconds"] == 55
    assert captured["max_text_chars"] == 9999
    assert captured["base_env"] is env
    assert captured["subagent_models"] == {"subagent": "model-x", "helper": "model-y"}
