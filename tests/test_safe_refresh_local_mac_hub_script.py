from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
