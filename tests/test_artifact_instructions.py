from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.artifact_instructions import (
    ArtifactDeliveryContext,
    current_artifact_target_available,
    current_artifact_target_failure_message,
    render_agent_artifact_instructions,
    render_human_artifact_overview,
)


def test_agent_artifact_instructions_use_default_send_command(tmp_path: Path) -> None:
    rendered = render_agent_artifact_instructions(
        ArtifactDeliveryContext(
            surface="telegram",
            conversation_key="chat:1/thread:2",
            scope_label="repo/worktree artifact target",
            workspace_scope=f"repo:{tmp_path}",
            user_upload_inbox=tmp_path / ".codex-autorunner" / "filebox" / "inbox",
        )
    )

    assert "Artifact delivery (this turn):" in rendered
    assert "car artifacts send <file>" in rendered
    assert "--to current" not in rendered
    assert "chat:1/thread:2" in rendered
    assert "filebox/inbox" in rendered
    assert "--to explicit" not in rendered
    assert "import-legacy" not in rendered
    assert "filebox/outbox" not in rendered
    assert "outbox/pending" not in rendered
    assert "Compatibility" not in rendered
    assert "Legacy" not in rendered


def test_agent_artifact_instructions_omit_inbox_when_absent() -> None:
    rendered = render_agent_artifact_instructions(
        ArtifactDeliveryContext(
            surface="discord",
            conversation_key="channel:123",
            scope_label="repo/worktree artifact target",
        )
    )

    assert "car artifacts send <file>" in rendered
    assert "--to current" not in rendered
    assert "User uploads may appear under" not in rendered


def test_human_artifact_overview_uses_same_current_command() -> None:
    rendered = render_human_artifact_overview(include_upload_inbox=True)

    assert "## Artifact delivery" in rendered
    assert "car artifacts send <file>" in rendered
    assert "--to current" not in rendered
    assert "car artifacts list" in rendered
    assert ".codex-autorunner/filebox/inbox/" in rendered
    assert "filebox/outbox" not in rendered


def test_current_target_preflight_reports_missing_target() -> None:
    assert not current_artifact_target_available({})
    message = current_artifact_target_failure_message({})

    assert message is not None
    assert "CAR_ARTIFACT_TARGET_SURFACE" in message
    assert "CAR_ARTIFACT_TARGET_CONVERSATION_KEY" in message
    assert "--to explicit" not in message
