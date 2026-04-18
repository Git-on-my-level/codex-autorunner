from __future__ import annotations

import subprocess
import sys

import pytest


def _import_module_subprocess(import_stmt: str, validate_stmt: str) -> str:
    command = [
        sys.executable,
        "-c",
        f"{import_stmt}; {validate_stmt}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_config_validation_imports_without_circular_dependency() -> None:
    output = _import_module_subprocess(
        "import codex_autorunner.core.config_validation as m",
        "print(m._normalize_ticket_flow_approval_mode('safe', scope='x'))",
    )
    assert output == "review"


@pytest.mark.parametrize(
    ("module_path", "import_stmt", "validate_stmt", "expected"),
    [
        (
            "config_parsers",
            "import codex_autorunner.core.config_parsers as m",
            "print(m.normalize_base_path('/foo/bar'))",
            "/foo/bar",
        ),
        (
            "config_env",
            "import codex_autorunner.core.config_env as m",
            "print(m.DOTENV_AVAILABLE is not None)",
            "True",
        ),
        (
            "config_layering",
            "import codex_autorunner.core.config_layering as m",
            "print(m.DEFAULT_HUB_CONFIG['mode'])",
            "hub",
        ),
        (
            "config_types",
            "import codex_autorunner.core.config_types as m",
            "print(m.RepoConfig.__name__)",
            "RepoConfig",
        ),
        (
            "agent_config",
            "import codex_autorunner.core.agent_config as m",
            "print(m.AgentConfig.__name__)",
            "AgentConfig",
        ),
        (
            "config_builders",
            "import codex_autorunner.core.config_builders as m",
            "print(m.build_repo_config.__name__)",
            "build_repo_config",
        ),
        (
            "config (facade)",
            "import codex_autorunner.core.config as m",
            "print(m.ConfigError.__name__)",
            "ConfigError",
        ),
    ],
)
def test_config_module_imports_without_circular_dependency(
    module_path: str,
    import_stmt: str,
    validate_stmt: str,
    expected: str,
) -> None:
    output = _import_module_subprocess(import_stmt, validate_stmt)
    assert output == expected
