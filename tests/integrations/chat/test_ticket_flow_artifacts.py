from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.apps import AppArtifactCandidate
from codex_autorunner.core.filebox import outbox_dir
from codex_autorunner.integrations.chat.ticket_flow_artifacts import (
    publish_terminal_wrapup_artifacts_to_outbox,
)


def _candidate(
    app_id: str, relative_path: str, absolute_path: Path
) -> AppArtifactCandidate:
    return AppArtifactCandidate(
        app_id=app_id,
        app_version="1.0.0",
        kind="markdown",
        label="Summary",
        relative_path=relative_path,
        absolute_path=absolute_path,
        hook_point="before_chat_wrapup",
    )


def test_publish_terminal_wrapup_artifacts_to_filebox_outbox_with_safe_names(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first" / "summary.md"
    second = tmp_path / "second" / "summary.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")

    published = publish_terminal_wrapup_artifacts_to_outbox(
        workspace,
        (
            _candidate("local.wrapup", "artifacts/summary.md", first),
            _candidate("local/wrapup", "artifacts/summary.md", second),
        ),
    )

    assert [path.name for path in published] == [
        "local.wrapup-summary.md",
        "local-wrapup-summary.md",
    ]
    assert (outbox_dir(workspace) / "local.wrapup-summary.md").read_text(
        encoding="utf-8"
    ) == "first\n"
    assert (outbox_dir(workspace) / "local-wrapup-summary.md").read_text(
        encoding="utf-8"
    ) == "second\n"


def test_publish_terminal_wrapup_artifacts_uses_collision_safe_names(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "summary.md"
    source.write_text("new\n", encoding="utf-8")
    outbox_dir(workspace).mkdir(parents=True)
    (outbox_dir(workspace) / "local.wrapup-summary.md").write_text(
        "existing\n",
        encoding="utf-8",
    )

    published = publish_terminal_wrapup_artifacts_to_outbox(
        workspace,
        (_candidate("local.wrapup", "artifacts/summary.md", source),),
    )

    assert [path.name for path in published] == ["local.wrapup-summary-2.md"]
    assert (outbox_dir(workspace) / "local.wrapup-summary.md").read_text(
        encoding="utf-8"
    ) == "existing\n"
    assert (outbox_dir(workspace) / "local.wrapup-summary-2.md").read_text(
        encoding="utf-8"
    ) == "new\n"
