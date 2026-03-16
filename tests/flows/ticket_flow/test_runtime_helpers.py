from __future__ import annotations

import uuid
from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.flows.ticket_flow.runtime_helpers import (
    build_ticket_flow_controller,
    normalize_ticket_flow_input_data,
)


def _init_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
    seed_repo_files(repo_root, git_required=False)
    return repo_root


def test_build_ticket_flow_controller_smoke(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path)
    controller = build_ticket_flow_controller(repo_root)
    try:
        run_id = str(uuid.uuid4())
        created = controller.store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={},
            state={},
        )
        assert created.id == run_id
        runs = controller.store.list_flow_runs(flow_type="ticket_flow")
        assert len(runs) == 1
        assert runs[0].id == run_id
    finally:
        controller.shutdown()


def test_normalize_ticket_flow_input_data_canonicalizes_relative_workspace_root(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    normalized = normalize_ticket_flow_input_data(
        repo_root,
        {"workspace_root": "workspace"},
    )

    assert normalized["workspace_root"] == str((repo_root / "workspace").resolve())


def test_normalize_ticket_flow_input_data_defaults_to_repo_root(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    normalized = normalize_ticket_flow_input_data(repo_root, {})

    assert normalized["workspace_root"] == str(repo_root.resolve())
