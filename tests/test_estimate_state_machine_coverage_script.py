from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "estimate_state_machine_coverage.py"
)
SPEC = importlib.util.spec_from_file_location(
    "estimate_state_machine_coverage", SCRIPT_PATH
)
assert SPEC is not None
estimate_state_machine_coverage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = estimate_state_machine_coverage
assert SPEC.loader is not None
SPEC.loader.exec_module(estimate_state_machine_coverage)


def test_path_classification_counts_whole_state_machine_module(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "core" / "flows" / "lifecycle_reducer.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            # comment
            def reduce_flow(state, event):
                return state
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_ticket_package_is_state_machine_owned(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "tickets" / "runner.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def select_ticket():
                return None
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_shared_chat_kernel_package_is_state_machine_owned(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "adapters" / "chat" / "managed_thread_lifecycle.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def lifecycle_step():
                return "queued"
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_acp_agent_package_is_state_machine_owned(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "agents" / "acp" / "client.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def send_prompt_rpc():
                return "pending"
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_core_pma_package_is_state_machine_owned(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "core" / "pma" / "policies.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def normalize_busy_policy(value):
                return value or "queue"
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_hub_control_plane_package_is_state_machine_owned(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "core" / "hub_control_plane" / "_executions.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def record_execution():
                return "recorded"
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_managed_processes_package_is_state_machine_owned(tmp_path: Path) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "core" / "managed_processes" / "reaper.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def reap_managed_processes():
                return 0
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 2
    assert result["state_machine_loc"] == 2
    assert result["coverage_percent"] == 100.0


def test_symbol_classification_counts_only_marked_top_level_blocks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "src" / "codex_autorunner"
    module = root / "surfaces" / "web" / "routes" / "messages.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        textwrap.dedent("""
            def render_message():
                return "ok"

            def update_turn_state():
                value = "state"
                return value
            """).lstrip(),
        encoding="utf-8",
    )

    result = estimate_state_machine_coverage.estimate(root)

    assert result["total_loc"] == 5
    assert result["state_machine_loc"] == 3
    assert result["coverage_percent"] == 60.0
