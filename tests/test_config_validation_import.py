from __future__ import annotations

import subprocess
import sys


def test_config_validation_imports_without_circular_dependency() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import codex_autorunner.core.config_validation as m; "
            "print(m._normalize_ticket_flow_approval_mode('safe', scope='x'))"
        ),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "review"
