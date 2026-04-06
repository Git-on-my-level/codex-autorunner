from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_safe_refresh_local_mac_hub_script_runs_past_parse_stage(
    tmp_path: Path,
) -> None:
    script_path = Path("scripts/safe-refresh-local-mac-hub.sh").resolve()
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
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
