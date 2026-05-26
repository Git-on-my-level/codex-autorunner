from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def test_safe_refresh_local_mac_hub_voice_provider_prefers_config_over_env(
    tmp_path: Path,
) -> None:
    script_path = Path("scripts/safe-refresh-local-mac-hub.sh").resolve()
    script = script_path.read_text(encoding="utf-8")
    helper_source = script.split("_resolve_pyenv_python()", 1)[0]
    helper_path = tmp_path / "safe-refresh-helpers.sh"
    hub_root = tmp_path / "hub"
    car_dir = hub_root / ".codex-autorunner"
    car_dir.mkdir(parents=True)
    helper_path.write_text(helper_source, encoding="utf-8")
    (car_dir / ".env").write_text(
        "CODEX_AUTORUNNER_VOICE_PROVIDER=local_whisper\n",
        encoding="utf-8",
    )
    (car_dir / "config.yml").write_text(
        "repo_defaults:\n" "  voice:\n" "    provider: mlx_whisper\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            f"{shlex.quote(str(helper_path))}; "
            f"_voice_provider_for_hub_root {shlex.quote(str(hub_root))}",
        ],
        cwd=script_path.parent.parent,
        env={
            "HOME": str(tmp_path),
            "HELPER_PYTHON": sys.executable,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mlx_whisper"


def test_safe_refresh_local_mac_hub_script_runs_past_parse_stage(
    tmp_path: Path,
) -> None:
    script_path = Path("scripts/safe-refresh-local-mac-hub.sh").resolve()
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PIPX_PYTHON": sys.executable,
        "USER": "tester",
    }

    result = subprocess.run(
        [str(script_path)],
        cwd=script_path.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 1
    assert "LaunchAgent plist not found" in output
    assert "syntax error near unexpected token" not in output


def test_safe_refresh_local_mac_hub_script_keeps_build_logs_out_of_wheel_path() -> None:
    script = Path("scripts/safe-refresh-local-mac-hub.sh").read_text(encoding="utf-8")

    assert "_build_web_static_assets >&2" in script
    assert 'wheel_path="$(_build_package_wheel ' in script
    assert "\"${wheel_path}\" == *$'\\n'*" in script
    assert '"${wheel_path}" != *.whl' in script
    assert '! -f "${wheel_path}"' in script
    assert "Invalid staged wheel path for pip install" in script


def test_safe_refresh_local_mac_hub_script_records_phase_timing() -> None:
    script = Path("scripts/safe-refresh-local-mac-hub.sh").read_text(encoding="utf-8")

    assert '"event": "update.phase_timing"' in script
    assert '"phase_timings"' in script
    assert "_run_timed_phase() {" in script
    assert '_run_timed_phase "pip_install"' in script
    assert '_run_timed_phase "hub_restart"' in script
    assert '_run_timed_phase "telegram_reload"' in script
    assert '_run_timed_phase "discord_reload"' in script


def test_safe_refresh_local_mac_hub_script_uses_pip_cache_for_dependencies() -> None:
    script = Path("scripts/safe-refresh-local-mac-hub.sh").read_text(encoding="utf-8")

    install_function = script.split("_install_package_from_wheel()", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "--no-cache-dir" not in install_function
