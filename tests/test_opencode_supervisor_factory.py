import logging
from pathlib import Path

from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    REPO_OVERRIDE_FILENAME,
    load_repo_config,
)
from codex_autorunner.core.utils import build_opencode_supervisor
from codex_autorunner.integrations.agents import opencode_supervisor_factory
from codex_autorunner.integrations.agents.destination_wrapping import WrappedCommand
from tests.conftest import write_test_config


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
                "server_scope": "global",
                "session_stall_timeout_seconds": 55,
                "max_text_chars": 9999,
                "max_handles": 7,
                "idle_ttl_seconds": 2222,
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
    assert captured["max_handles"] == 7
    assert captured["idle_ttl_seconds"] == 2222
    assert captured["server_scope"] == "global"
    assert captured["session_stall_timeout_seconds"] == 55
    assert captured["max_text_chars"] == 9999
    assert captured["base_env"] is env
    assert captured["subagent_models"] == {"subagent": "model-x", "helper": "model-y"}


def test_build_opencode_supervisor_from_repo_config_wraps_for_docker_destination(
    monkeypatch, tmp_path: Path
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    write_test_config(hub_root / CONFIG_FILENAME, {"mode": "hub"})

    repo_root = hub_root / "repo"
    repo_root.mkdir()
    write_test_config(repo_root / REPO_OVERRIDE_FILENAME, {})

    config = load_repo_config(repo_root, hub_path=hub_root)
    config.effective_destination = {"kind": "docker", "image": "busybox:latest"}
    captured: dict = {}

    monkeypatch.setattr(
        opencode_supervisor_factory,
        "wrap_command_for_destination",
        lambda **_: WrappedCommand(
            command=["docker", "exec", "ctr", "opencode", "serve"]
        ),
    )

    def _fake_build_opencode_supervisor(**kwargs):
        captured.update(kwargs)
        return "supervisor"

    monkeypatch.setattr(
        opencode_supervisor_factory,
        "build_opencode_supervisor",
        _fake_build_opencode_supervisor,
    )

    supervisor = opencode_supervisor_factory.build_opencode_supervisor_from_repo_config(
        config,
        workspace_root=repo_root,
        logger=logging.getLogger("test.opencode"),
        base_env={},
    )

    assert supervisor == "supervisor"
    assert captured["opencode_command"] == [
        "docker",
        "exec",
        "ctr",
        "opencode",
        "serve",
    ]


def test_build_opencode_supervisor_command_from_serve_command(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def _fake_supervisor_cls(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "supervisor_instance"

    monkeypatch.setattr(
        "codex_autorunner.core.utils._command_available",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.utils.resolve_opencode_binary",
        lambda *a, **kw: "/resolved/opencode",
    )

    class FakeModule:
        OpenCodeSupervisor = staticmethod(_fake_supervisor_cls)

    monkeypatch.setattr(
        "codex_autorunner.core.utils.importlib.import_module",
        lambda n: FakeModule(),
    )

    logger = logging.getLogger("test")
    env = {"OPENCODE_SERVER_USERNAME": "alice", "OPENCODE_SERVER_PASSWORD": "secret"}
    result = build_opencode_supervisor(
        opencode_command=["opencode", "serve", "--port", "0"],
        workspace_root=tmp_path,
        logger=logger,
        request_timeout=30.0,
        max_handles=5,
        idle_ttl_seconds=100.0,
        server_scope="global",
        session_stall_timeout_seconds=15.0,
        max_text_chars=5000,
        base_env=env,
        subagent_models={"helper": "model-z"},
    )

    assert result == "supervisor_instance"
    assert captured["args"][0] == ["/resolved/opencode", "serve", "--port", "0"]
    assert captured["kwargs"]["logger"] is logger
    assert captured["kwargs"]["request_timeout"] == 30.0
    assert captured["kwargs"]["max_handles"] == 5
    assert captured["kwargs"]["idle_ttl_seconds"] == 100.0
    assert captured["kwargs"]["server_scope"] == "global"
    assert captured["kwargs"]["session_stall_timeout_seconds"] == 15.0
    assert captured["kwargs"]["max_text_chars"] == 5000
    assert captured["kwargs"]["username"] == "alice"
    assert captured["kwargs"]["password"] == "secret"
    assert captured["kwargs"]["subagent_models"] == {"helper": "model-z"}


def test_build_opencode_supervisor_command_from_binary(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def _fake_supervisor_cls(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "supervisor_instance"

    monkeypatch.setattr(
        "codex_autorunner.core.utils._command_available",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.utils.resolve_opencode_binary",
        lambda *a, **kw: "/resolved/opencode",
    )

    class FakeModule:
        OpenCodeSupervisor = staticmethod(_fake_supervisor_cls)

    monkeypatch.setattr(
        "codex_autorunner.core.utils.importlib.import_module",
        lambda n: FakeModule(),
    )

    result = build_opencode_supervisor(
        opencode_binary="/usr/local/bin/opencode",
        workspace_root=tmp_path,
        logger=logging.getLogger("test"),
    )

    assert result == "supervisor_instance"
    assert captured["args"][0] == [
        "/resolved/opencode",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
    ]


def test_build_opencode_supervisor_returns_none_when_command_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.core.utils._command_available",
        lambda *a, **kw: False,
    )

    result = build_opencode_supervisor(
        opencode_command=["nonexistent", "serve"],
        workspace_root=tmp_path,
    )

    assert result is None


def test_build_opencode_supervisor_returns_none_when_no_command_or_binary(
    tmp_path: Path,
) -> None:
    result = build_opencode_supervisor(
        workspace_root=tmp_path,
    )

    assert result is None


def test_build_opencode_supervisor_defaults_username_when_only_password(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def _fake_supervisor_cls(*args, **kwargs):
        captured["kwargs"] = kwargs
        return "supervisor_instance"

    monkeypatch.setattr(
        "codex_autorunner.core.utils._command_available",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.utils.resolve_opencode_binary",
        lambda *a, **kw: "/resolved/opencode",
    )

    class FakeModule:
        OpenCodeSupervisor = staticmethod(_fake_supervisor_cls)

    monkeypatch.setattr(
        "codex_autorunner.core.utils.importlib.import_module",
        lambda n: FakeModule(),
    )

    env = {"OPENCODE_SERVER_PASSWORD": "secret_only"}
    result = build_opencode_supervisor(
        opencode_command=["opencode", "serve"],
        workspace_root=tmp_path,
        base_env=env,
    )

    assert result == "supervisor_instance"
    assert captured["kwargs"]["username"] == "opencode"
    assert captured["kwargs"]["password"] == "secret_only"


def test_build_opencode_supervisor_no_auth_when_no_password(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict = {}

    def _fake_supervisor_cls(*args, **kwargs):
        captured["kwargs"] = kwargs
        return "supervisor_instance"

    monkeypatch.setattr(
        "codex_autorunner.core.utils._command_available",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.utils.resolve_opencode_binary",
        lambda *a, **kw: "/resolved/opencode",
    )

    class FakeModule:
        OpenCodeSupervisor = staticmethod(_fake_supervisor_cls)

    monkeypatch.setattr(
        "codex_autorunner.core.utils.importlib.import_module",
        lambda n: FakeModule(),
    )

    env = {"OPENCODE_SERVER_USERNAME": "alice_only"}
    result = build_opencode_supervisor(
        opencode_command=["opencode", "serve"],
        workspace_root=tmp_path,
        base_env=env,
    )

    assert result == "supervisor_instance"
    assert captured["kwargs"]["username"] is None
    assert captured["kwargs"]["password"] is None
