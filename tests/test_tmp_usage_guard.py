from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "check_test_tmp_usage.py"


def _run_guard(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "--repo-root",
            str(repo_root),
            "--allowlist",
            "scripts/test_tmp_usage_allowlist.json",
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_allowlist(repo_root: Path, *, violations: list[dict[str, str]]) -> None:
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "test_tmp_usage_allowlist.json").write_text(
        json.dumps({"violations": violations}, indent=2) + "\n",
        encoding="utf-8",
    )


def test_guard_rejects_tmp_workspace_root_without_allowlist(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_guard_target.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_case():\n" "    build_thread(workspace_root='/tmp/repo')\n",
        encoding="utf-8",
    )
    _write_allowlist(tmp_path, violations=[])

    result = _run_guard(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tmp-root-kwarg (workspace_root)" in result.stdout
    assert "tests/test_guard_target.py:2:tmp-root-kwarg:workspace_root" in result.stdout


def test_guard_allows_allowlisted_tmp_workspace_root(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_guard_target.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_case():\n" "    build_thread(workspace_root='/tmp/repo')\n",
        encoding="utf-8",
    )
    _write_allowlist(
        tmp_path,
        violations=[
            {
                "key": "tests/test_guard_target.py:2:tmp-root-kwarg:workspace_root",
                "reason": "resolver_only: validates serialization shape",
            }
        ],
    )

    result = _run_guard(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_rejects_direct_tmp_write(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_guard_target.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_case():\n" "    open('/tmp/out.txt', 'w')\n",
        encoding="utf-8",
    )
    _write_allowlist(tmp_path, violations=[])

    result = _run_guard(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tmp-direct-open-write (open)" in result.stdout


def test_guard_rejects_tmp_copytree_destination(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_guard_target.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "import shutil\n\n"
        "def test_case():\n"
        "    shutil.copytree('/safe/src', '/tmp/dest')\n",
        encoding="utf-8",
    )
    _write_allowlist(tmp_path, violations=[])

    result = _run_guard(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tmp-module-write-call (shutil.copytree)" in result.stdout


def test_guard_rejects_tmp_tempfile_dir_via_positional_argument(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "test_guard_target.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "import tempfile\n\n"
        "def test_case():\n"
        "    tempfile.mkdtemp(None, None, '/tmp/bad')\n",
        encoding="utf-8",
    )
    _write_allowlist(tmp_path, violations=[])

    result = _run_guard(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tmp-tempfile-dir (tempfile.mkdtemp)" in result.stdout
