from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

_CHECKS: list[tuple[str, str, str]] = [
    (
        "config_validation",
        "import codex_autorunner.core.config_validation as _m",
        "_m._normalize_ticket_flow_approval_mode('review', scope='x')",
    ),
    (
        "config_parsers",
        "import codex_autorunner.core.config_parsers as _m",
        "_m.normalize_base_path('/foo/bar')",
    ),
    (
        "config_env",
        "import codex_autorunner.core.config_env as _m",
        "_m.DOTENV_AVAILABLE is not None",
    ),
    (
        "config_layering",
        "import codex_autorunner.core.config_layering as _m",
        "_m.DEFAULT_HUB_CONFIG['mode']",
    ),
    (
        "config_defaults",
        "import codex_autorunner.core.config_defaults as _m",
        "_m.DEFAULT_HUB_CONFIG['mode']",
    ),
    (
        "config_sources",
        "import codex_autorunner.core.config_sources as _m",
        "_m.CONFIG_FILENAME",
    ),
    (
        "config_yaml",
        "import codex_autorunner.core.config_yaml as _m",
        "_m._merge_defaults({'a': 1}, {'b': 2})['b']",
    ),
    (
        "config_generated",
        "import codex_autorunner.core.config_generated as _m",
        "_m.GENERATED_CONFIG_HEADER.startswith('# GENERATED')",
    ),
    (
        "config_types",
        "import codex_autorunner.core.config_types as _m",
        "_m.RepoConfig.__name__",
    ),
    (
        "agent_config",
        "import codex_autorunner.core.agent_config as _m",
        "_m.AgentConfig.__name__",
    ),
    (
        "config_builders",
        "import codex_autorunner.core.config_builders as _m",
        "_m.build_repo_config.__name__",
    ),
    (
        "config_facade",
        "import codex_autorunner.core.config as _m",
        "_m.ConfigError.__name__",
    ),
]


def _build_batch_script() -> str:
    lines = ["import json", "_r = {}"]
    for key, imp, expr in _CHECKS:
        lines.append("try:")
        lines.append(f"    {imp}")
        lines.append(f"    _r[{key!r}] = str({expr})")
        lines.append("except Exception as _e:")
        lines.append(f"    _r[{key!r}] = 'IMPORT_ERROR: ' + str(_e)")
    lines.append("print(json.dumps(_r))")
    return "\n".join(lines)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    import_paths = [str(Path(__file__).resolve().parents[1] / "src")]
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        import_paths.append(user_site)
    else:
        import_paths.extend(str(path) for path in user_site)
    existing = env.get("PYTHONPATH")
    if existing:
        import_paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(import_paths)
    return env


@pytest.fixture(scope="module")
def _batch_results():
    script = _build_batch_script()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        env=_subprocess_env(),
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip())


def test_config_validation_imports_without_circular_dependency(
    _batch_results: dict[str, str],
) -> None:
    value = _batch_results["config_validation"]
    assert not value.startswith("IMPORT_ERROR:"), value
    assert value == "review"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("config_parsers", "/foo/bar"),
        ("config_env", "True"),
        ("config_layering", "hub"),
        ("config_defaults", "hub"),
        ("config_sources", ".codex-autorunner/config.yml"),
        ("config_yaml", "2"),
        ("config_generated", "True"),
        ("config_types", "RepoConfig"),
        ("agent_config", "AgentConfig"),
        ("config_builders", "build_repo_config"),
        ("config_facade", "ConfigError"),
    ],
)
def test_config_module_imports_without_circular_dependency(
    _batch_results: dict[str, str],
    key: str,
    expected: str,
) -> None:
    value = _batch_results[key]
    assert not value.startswith("IMPORT_ERROR:"), value
    assert value == expected
