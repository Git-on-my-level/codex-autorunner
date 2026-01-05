from __future__ import annotations

import subprocess
from pathlib import Path

from codex_autorunner.routes.system import _system_update_check


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def test_update_check_uses_update_cache_when_no_local_git(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    local_cache = tmp_path / "update_cache"
    module_dir = tmp_path / "installed" / "site-packages" / "codex_autorunner"
    module_dir.mkdir(parents=True)

    remote.mkdir()
    _run(["git", "init", "-b", "main"], remote)
    _run(["git", "config", "user.email", "test@example.com"], remote)
    _run(["git", "config", "user.name", "Test"], remote)
    (remote / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], remote)
    _run(["git", "commit", "-m", "init"], remote)

    _run(["git", "clone", str(remote), str(local_cache)], tmp_path)

    result = _system_update_check(
        repo_url=str(remote),
        repo_ref="main",
        module_dir=module_dir,
        update_cache_dir=local_cache,
    )

    assert result["status"] == "ok"
    assert result["update_available"] is False
    assert result["message"].startswith("No update available")
