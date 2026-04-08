from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from codex_autorunner.core.config import FlowRetentionConfig
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.surfaces.cli.commands import flow as flow_module


def _build_flow_app(monkeypatch, repo_root: Path, retention: FlowRetentionConfig):
    engine = SimpleNamespace(
        repo_root=repo_root,
        config=SimpleNamespace(
            durable_writes=False,
            flow_retention=retention,
        ),
    )

    flow_app = typer.Typer(add_completion=False)
    ticket_flow_app = typer.Typer(add_completion=False)
    telemetry_app = typer.Typer(add_completion=False)
    flow_module.register_flow_commands(
        flow_app,
        ticket_flow_app,
        telemetry_app,
        require_repo_config=lambda _repo, _hub: engine,
        raise_exit=lambda msg, **_kw: (_ for _ in ()).throw(RuntimeError(msg)),
        build_agent_pool=lambda _cfg: None,
        build_ticket_flow_definition=lambda **_kw: None,
        guard_unregistered_hub_repo=lambda *_args, **_kwargs: None,
        parse_bool_text=lambda *_args, **_kwargs: True,
        parse_duration=lambda *_args, **_kwargs: None,
        cleanup_stale_flow_runs=lambda **_kwargs: 0,
        archive_flow_run_artifacts=lambda **_kwargs: {},
    )
    return flow_app


def test_flow_housekeep_uses_repo_retention_config(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()

    captured: dict[str, object] = {}

    class _FakeResult:
        runs_processed = 0
        events_exported = 0
        events_pruned = 0
        exported_bytes = 0
        errors: list[str] = []
        vacuum_performed = False
        db_size_before_bytes = 0
        db_size_after_bytes = 0

        def to_dict(self) -> dict[str, object]:
            return {"runs_processed": self.runs_processed}

    def _fake_execute_housekeep(
        repo_root: Path,
        store: FlowStore,
        target_db_path: Path,
        retention_config: FlowRetentionConfig,
        *,
        run_ids=None,
        vacuum: bool = False,
        dry_run: bool = False,
    ) -> _FakeResult:
        captured["repo_root"] = repo_root
        captured["store"] = store
        captured["db_path"] = target_db_path
        captured["retention_config"] = retention_config
        captured["run_ids"] = run_ids
        captured["vacuum"] = vacuum
        captured["dry_run"] = dry_run
        return _FakeResult()

    monkeypatch.setattr(flow_module, "execute_housekeep", _fake_execute_housekeep)

    retention = FlowRetentionConfig(retention_days=30, vacuum_after_prune=True)
    flow_app = _build_flow_app(monkeypatch, tmp_path, retention)

    result = CliRunner().invoke(flow_app, ["housekeep"])

    assert result.exit_code == 0, result.output
    assert captured["repo_root"] == tmp_path
    assert captured["db_path"] == db_path
    assert captured["retention_config"] == retention
    assert captured["vacuum"] is True
    assert captured["dry_run"] is False
