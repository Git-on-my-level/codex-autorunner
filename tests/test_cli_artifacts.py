from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.artifact_delivery import ArtifactDeliveryService

runner = CliRunner()


def test_artifacts_send_list_inspect_retry_cancel(tmp_path: Path) -> None:
    source = tmp_path / "spec.md"
    source.write_text("# spec\n", encoding="utf-8")

    send_result = runner.invoke(
        app,
        [
            "artifacts",
            "send",
            str(source),
            "--root",
            str(tmp_path),
            "--to",
            "explicit",
            "--surface",
            "discord",
            "--conversation",
            "channel:123",
            "--workspace-scope",
            "repo:/example",
            "--json",
        ],
    )

    assert send_result.exit_code == 0, send_result.output
    payload = json.loads(send_result.output)
    delivery_id = payload["delivery_id"]
    assert payload["state"] == "pending"
    assert payload["artifact"]["filename"] == "spec.md"

    list_result = runner.invoke(
        app,
        ["artifacts", "list", "--root", str(tmp_path), "--json"],
    )
    assert list_result.exit_code == 0, list_result.output
    rows = json.loads(list_result.output)
    assert [row["delivery_id"] for row in rows] == [delivery_id]

    service = ArtifactDeliveryService(tmp_path)
    claimed = service.claim_next(target_surface="discord")
    assert claimed is not None
    service.mark_failed(
        delivery_id,
        error="upload failed",
        claim_token=claimed.claim_token,
    )

    retry_result = runner.invoke(
        app, ["artifacts", "retry", delivery_id, "--root", str(tmp_path)]
    )
    assert retry_result.exit_code == 0, retry_result.output
    assert json.loads(retry_result.output)["state"] == "pending"

    cancel_result = runner.invoke(
        app, ["artifacts", "cancel", delivery_id, "--root", str(tmp_path)]
    )
    assert cancel_result.exit_code == 0, cancel_result.output
    assert json.loads(cancel_result.output)["state"] == "cancelled"


def test_artifacts_diagnose_flags_legacy_hub_pending_file(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    stranded = hub / ".codex-autorunner" / "filebox" / "outbox" / "pending" / "spec.md"
    stranded.parent.mkdir(parents=True)
    stranded.write_text("# stranded\n", encoding="utf-8")
    repo.mkdir()

    result = runner.invoke(
        app,
        [
            "artifacts",
            "diagnose",
            "--root",
            str(repo),
            "--sibling-root",
            str(hub),
        ],
    )

    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)["findings"]
    assert any(
        finding["kind"] == "legacy_filebox_pending"
        and finding["filename"] == "spec.md"
        and finding["root"] == str(hub.resolve())
        for finding in findings
    )
