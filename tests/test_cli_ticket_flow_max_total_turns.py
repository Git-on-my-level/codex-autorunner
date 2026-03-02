from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from codex_autorunner.surfaces.cli.commands import flow as flow_module


def _write_valid_ticket(repo_root: Path) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        """---
agent: codex
done: false
title: "Test ticket"
goal: "Run tests"
---

body
""",
        encoding="utf-8",
    )


def _build_ticket_flow_app(monkeypatch, repo_root: Path, start_calls: list[dict]) -> typer.Typer:
    class _FakeConfig:
        durable_writes = False
        app_server = SimpleNamespace(command=["python"])

    engine = SimpleNamespace(repo_root=repo_root, config=_FakeConfig())

    class _FakeFlowController:
        def __init__(self, **_kwargs) -> None:
            pass

        def initialize(self) -> None:
            return

        async def start_flow(self, input_data, run_id, metadata=None):  # type: ignore[no-untyped-def]
            start_calls.append(
                {
                    "input_data": dict(input_data or {}),
                    "run_id": run_id,
                    "metadata": dict(metadata or {}),
                }
            )
            return SimpleNamespace(id=run_id)

        def shutdown(self) -> None:
            return

    class _FakeDefinition:
        def validate(self) -> None:
            return

    class _FakeAgentPool:
        async def close_all(self) -> None:
            return

    monkeypatch.setattr(flow_module, "FlowController", _FakeFlowController)
    monkeypatch.setattr(
        flow_module, "ensure_worker", lambda *_args, **_kwargs: {"status": "reused"}
    )

    flow_app = typer.Typer(add_completion=False)
    ticket_flow_app = typer.Typer(add_completion=False)
    flow_module.register_flow_commands(
        flow_app,
        ticket_flow_app,
        require_repo_config=lambda _repo, _hub: engine,
        raise_exit=lambda msg, **_kw: (_ for _ in ()).throw(RuntimeError(msg)),
        build_agent_pool=lambda _cfg: _FakeAgentPool(),
        build_ticket_flow_definition=lambda **_kw: _FakeDefinition(),
        guard_unregistered_hub_repo=lambda *_args, **_kwargs: None,
        parse_bool_text=lambda *_args, **_kwargs: True,
        parse_duration=lambda *_args, **_kwargs: None,
        cleanup_stale_flow_runs=lambda **_kwargs: 0,
        archive_flow_run_artifacts=lambda **_kwargs: {},
    )
    return ticket_flow_app


def test_ticket_flow_start_forwards_max_total_turns(monkeypatch, tmp_path: Path) -> None:
    _write_valid_ticket(tmp_path)
    start_calls: list[dict] = []
    ticket_flow_app = _build_ticket_flow_app(monkeypatch, tmp_path, start_calls)

    result = CliRunner().invoke(
        ticket_flow_app, ["start", "--max-total-turns", "200"]
    )

    assert result.exit_code == 0, result.output
    assert len(start_calls) == 1
    assert start_calls[0]["input_data"]["workspace_root"] == str(tmp_path)
    assert start_calls[0]["input_data"]["max_total_turns"] == 200


def test_ticket_flow_bootstrap_forwards_max_total_turns(
    monkeypatch, tmp_path: Path
) -> None:
    _write_valid_ticket(tmp_path)
    start_calls: list[dict] = []
    ticket_flow_app = _build_ticket_flow_app(monkeypatch, tmp_path, start_calls)

    result = CliRunner().invoke(
        ticket_flow_app, ["bootstrap", "--max-total-turns", "120"]
    )

    assert result.exit_code == 0, result.output
    assert len(start_calls) == 1
    assert start_calls[0]["input_data"]["max_total_turns"] == 120
    assert start_calls[0]["metadata"] == {"seeded_ticket": False}
