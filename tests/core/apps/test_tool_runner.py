from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import (
    AppToolNotFoundError,
    AppToolRunnerError,
    AppToolTimeoutError,
    install_app,
    list_installed_app_tools,
    run_installed_app_tool,
)
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.state_roots import resolve_repo_state_root


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    run_git(["checkout", "-b", "main"], repo_path, check=True)


def _commit_repo(repo_path: Path, message: str) -> None:
    run_git(["add", "."], repo_path, check=True)
    run_git(["commit", "-m", message], repo_path, check=True)


def _configure_apps_repo(
    hub_root: Path, app_repo: Path, *, trusted: bool = True
) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": True,
        "repos": [
            {
                "id": "local",
                "url": str(app_repo),
                "trusted": trusted,
                "default_ref": "main",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _write_runner_app(app_repo: Path) -> None:
    app_root = app_repo / "apps" / "runner"
    (app_root / "scripts").mkdir(parents=True, exist_ok=True)
    (app_root / "car-app.yaml").write_text(
        """schema_version: 1
id: local.runner
name: Runner App
version: 1.0.0
description: Tool runner fixture.
tools:
  echo-env:
    description: Dump CAR env vars and argv.
    argv: ["python3", "scripts/echo_env.py"]
    timeout_seconds: 5
    outputs:
      - kind: json
        path: artifacts/result.json
        label: "Runner output"
""",
        encoding="utf-8",
    )
    (app_root / "scripts" / "echo_env.py").write_text(
        """import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
state_dir = Path(os.environ["CAR_APP_STATE_DIR"])
artifact_dir = Path(os.environ["CAR_APP_ARTIFACT_DIR"])
state_dir.mkdir(parents=True, exist_ok=True)
artifact_dir.mkdir(parents=True, exist_ok=True)
payload = {
    "argv": args,
    "env": {
        key: value
        for key, value in os.environ.items()
        if key.startswith("CAR_")
    },
}
(state_dir / "env.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
(artifact_dir / "result.json").write_text(
    json.dumps({"argv": args}, indent=2),
    encoding="utf-8",
)
if "--sleep" in args:
    time.sleep(float(args[args.index("--sleep") + 1]))
if "--exit-code" in args:
    sys.exit(int(args[args.index("--exit-code") + 1]))
print(json.dumps({"argv": args}))
if "--stderr" in args:
    print("stderr-line", file=sys.stderr)
""",
        encoding="utf-8",
    )


def _install_runner_app(tmp_path: Path):
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    app_repo = tmp_path / "app_repo"

    seed_hub_files(hub_root, force=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    _write_runner_app(app_repo)
    _commit_repo(app_repo, "add runner app")

    hub_config = load_hub_config(hub_root)
    install_result = install_app(hub_config, hub_root, repo_root, "local:apps/runner")
    return repo_root, install_result


def _install_runner_app_with_trust(tmp_path: Path, *, trusted: bool):
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    app_repo = tmp_path / "app_repo"

    seed_hub_files(hub_root, force=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo, trusted=trusted)
    _write_runner_app(app_repo)
    _commit_repo(app_repo, "add runner app")

    hub_config = load_hub_config(hub_root)
    install_result = install_app(hub_config, hub_root, repo_root, "local:apps/runner")
    return repo_root, install_result


def test_list_installed_app_tools(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)

    tools = list_installed_app_tools(repo_root, "local.runner")

    assert len(tools) == 1
    assert tools[0].tool_id == "echo-env"
    assert tools[0].description == "Dump CAR env vars and argv."
    assert tools[0].bundle_verified is True
    assert tools[0].outputs[0].path == "artifacts/result.json"


def test_run_installed_app_tool_success(tmp_path: Path) -> None:
    repo_root, install_result = _install_runner_app(tmp_path)
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-1.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text(
        "\n".join(
            [
                "---",
                'ticket_id: "tkt_runner_success"',
                'agent: "codex"',
                "done: false",
                'app: "local.runner"',
                f'app_version: "{install_result.app.app_version}"',
                f'app_manifest_sha: "{install_result.app.lock.manifest_sha}"',
                f'app_bundle_sha: "{install_result.app.lock.bundle_sha}"',
                "---",
                "",
                "runner ticket",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = run_installed_app_tool(
        repo_root,
        "local.runner",
        "echo-env",
        extra_argv=["alpha", "beta", "--stderr"],
        flow_run_id="run-123",
        ticket_id="ticket-123",
        ticket_path=ticket_path,
        hook_point="after_ticket_done",
    )

    state_payload = json.loads(
        (
            resolve_repo_state_root(repo_root)
            / "apps"
            / "local.runner"
            / "state"
            / "env.json"
        ).read_text(encoding="utf-8")
    )

    assert result.exit_code == 0
    assert result.argv[0] == "python3"
    assert result.argv[-3:] == ("alpha", "beta", "--stderr")
    assert (
        str(
            resolve_repo_state_root(repo_root)
            / "apps"
            / "local.runner"
            / "bundle"
            / "scripts"
            / "echo_env.py"
        )
        == result.argv[1]
    )
    assert '"alpha"' in result.stdout_excerpt
    assert "stderr-line" in result.stderr_excerpt
    assert result.stdout_log_path.exists()
    assert result.stderr_log_path.exists()
    assert len(result.outputs) == 1
    assert result.outputs[0].relative_path == "artifacts/result.json"
    assert state_payload["argv"] == ["alpha", "beta", "--stderr"]
    assert state_payload["env"]["CAR_APP_ID"] == "local.runner"
    assert state_payload["env"]["CAR_APP_VERSION"] == "1.0.0"
    assert state_payload["env"]["CAR_FLOW_RUN_ID"] == "run-123"
    assert state_payload["env"]["CAR_TICKET_ID"] == "ticket-123"
    assert state_payload["env"]["CAR_HOOK_POINT"] == "after_ticket_done"
    assert state_payload["env"]["CAR_REPO_ROOT"] == str(repo_root.resolve())
    assert state_payload["env"]["CAR_WORKSPACE_ROOT"] == str(repo_root.resolve())


def test_extra_argv_forwarding_after_double_dash(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)

    run_installed_app_tool(
        repo_root,
        "local.runner",
        "echo-env",
        extra_argv=["alpha", "--flag", "value"],
    )

    payload = json.loads(
        (
            resolve_repo_state_root(repo_root)
            / "apps"
            / "local.runner"
            / "state"
            / "env.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["argv"] == ["alpha", "--flag", "value"]


def test_expected_car_app_env_vars(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)

    run_installed_app_tool(repo_root, "local.runner", "echo-env")

    env_payload = json.loads(
        (
            resolve_repo_state_root(repo_root)
            / "apps"
            / "local.runner"
            / "state"
            / "env.json"
        ).read_text(encoding="utf-8")
    )["env"]

    assert env_payload["CAR_APP_ROOT"].endswith(".codex-autorunner/apps/local.runner")
    assert env_payload["CAR_APP_BUNDLE_DIR"].endswith(
        ".codex-autorunner/apps/local.runner/bundle"
    )
    assert env_payload["CAR_APP_STATE_DIR"].endswith(
        ".codex-autorunner/apps/local.runner/state"
    )
    assert env_payload["CAR_APP_ARTIFACT_DIR"].endswith(
        ".codex-autorunner/apps/local.runner/artifacts"
    )
    assert env_payload["CAR_APP_LOG_DIR"].endswith(
        ".codex-autorunner/apps/local.runner/logs"
    )
    assert "CAR_FLOW_RUN_ID" not in env_payload
    assert "CAR_TICKET_ID" not in env_payload
    assert "CAR_HOOK_POINT" not in env_payload


def test_non_zero_exit_handling(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)

    result = run_installed_app_tool(
        repo_root,
        "local.runner",
        "echo-env",
        extra_argv=["--exit-code", "7"],
    )

    assert result.exit_code == 7
    assert result.stdout_log_path.exists()


def test_timeout_handling(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)

    with pytest.raises(AppToolTimeoutError, match="timed out"):
        run_installed_app_tool(
            repo_root,
            "local.runner",
            "echo-env",
            extra_argv=["--sleep", "1"],
            timeout_seconds=0.1,
        )

    logs_root = resolve_repo_state_root(repo_root) / "apps" / "local.runner" / "logs"
    assert list(logs_root.glob("*.stdout.log"))
    assert list(logs_root.glob("*.stderr.log"))


def test_dirty_bundle_refusal(tmp_path: Path) -> None:
    repo_root, install_result = _install_runner_app(tmp_path)
    script_path = install_result.app.paths.bundle_root / "scripts" / "echo_env.py"
    script_path.write_text(script_path.read_text(encoding="utf-8") + "\n# dirty\n")

    with pytest.raises(AppToolRunnerError, match="does not match app.lock.json"):
        run_installed_app_tool(repo_root, "local.runner", "echo-env")


def test_untrusted_app_refusal(tmp_path: Path) -> None:
    repo_root, install_result = _install_runner_app_with_trust(tmp_path, trusted=False)

    assert install_result.app.lock.trusted is False
    with pytest.raises(AppToolRunnerError, match="Refusing to execute tools"):
        run_installed_app_tool(repo_root, "local.runner", "echo-env")


def test_ticket_app_version_mismatch_refusal(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-2.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text(
        "\n".join(
            [
                "---",
                'ticket_id: "tkt_runner_mismatch"',
                'agent: "codex"',
                "done: false",
                'app: "local.runner"',
                'app_version: "9.9.9"',
                "---",
                "",
                "runner ticket",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(AppToolRunnerError, match="app_version"):
        run_installed_app_tool(
            repo_root,
            "local.runner",
            "echo-env",
            ticket_path=ticket_path,
        )


def test_unknown_app_and_tool_errors(tmp_path: Path) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)

    with pytest.raises(AppToolNotFoundError, match="Installed app not found"):
        run_installed_app_tool(repo_root, "missing.app", "echo-env")

    with pytest.raises(AppToolNotFoundError, match="Unknown tool"):
        run_installed_app_tool(repo_root, "local.runner", "missing-tool")


def test_run_installed_app_tool_uses_shell_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, _install_result = _install_runner_app(tmp_path)
    import codex_autorunner.core.apps.tool_runner as tool_runner_module

    real_popen = tool_runner_module.subprocess.Popen
    seen: dict[str, object] = {}

    def _wrapped_popen(*args, **kwargs):
        seen["shell"] = kwargs.get("shell")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(tool_runner_module.subprocess, "Popen", _wrapped_popen)

    result = run_installed_app_tool(repo_root, "local.runner", "echo-env")

    assert result.exit_code == 0
    assert seen["shell"] is False
