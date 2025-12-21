import os
import time
from pathlib import Path

import json
import pytest
import yaml
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.server import create_hub_app


class FakePTYSession:
    def __init__(self, cmd: list[str], cwd: str, env=None):
        self._rfd, self._wfd = os.pipe()
        self.fd = self._rfd
        self.closed = False
        self.last_active = time.time()
        self._alive = True

    def resize(self, cols: int, rows: int) -> None:
        self.last_active = time.time()

    def write(self, data: bytes) -> None:
        if self.closed:
            return
        os.write(self._wfd, data)
        self.last_active = time.time()

    def isalive(self) -> bool:
        return self._alive and not self.closed

    def exit_code(self):
        return None

    def terminate(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._alive = False
        os.close(self._wfd)
        os.close(self._rfd)


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _create_repo(root: Path, name: str) -> Path:
    repo_dir = root / name
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_dir, git_required=False)
    return repo_dir


def test_hub_terminal_sessions_stay_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    repos_root = cfg["hub"]["repos_root"]
    _write_config(hub_root / CONFIG_FILENAME, cfg)

    repo_root = hub_root / repos_root
    repo_root.mkdir(parents=True, exist_ok=True)
    alpha = _create_repo(repo_root, "alpha")
    beta = _create_repo(repo_root, "beta")

    app = create_hub_app(hub_root)
    monkeypatch.setattr("codex_autorunner.routes.base.PTYSession", FakePTYSession)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/repos/alpha/api/terminal"
        ) as ws_alpha, client.websocket_connect(
            "/repos/beta/api/terminal"
        ) as ws_beta:
            hello_alpha = ws_alpha.receive_json()
            hello_beta = ws_beta.receive_json()
            alpha_session = hello_alpha.get("session_id")
            beta_session = hello_beta.get("session_id")
            assert alpha_session
            assert beta_session
            assert alpha_session != beta_session

            ws_beta.send_json({"type": "ping"})
            assert ws_beta.receive_json().get("type") == "pong"

            ws_alpha.close()

            ws_beta.send_json({"type": "ping"})
            assert ws_beta.receive_json().get("type") == "pong"

        alpha_sessions = client.get("/repos/alpha/api/sessions").json()["sessions"]
        beta_sessions = client.get("/repos/beta/api/sessions").json()["sessions"]
        assert alpha_sessions[0]["session_id"] == alpha_session
        assert beta_sessions[0]["session_id"] == beta_session
        assert alpha_sessions[0]["repo_path"] == str(alpha.resolve())
        assert beta_sessions[0]["repo_path"] == str(beta.resolve())
