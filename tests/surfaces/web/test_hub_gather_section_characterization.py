from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.surfaces.web.services import hub_gather as hub_gather_service


def _valid_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(root=root, pma=SimpleNamespace(enabled=True), raw={})


def test_gather_hub_message_snapshot_excludes_unrequested_inbox_items(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"pma_threads"},
    )

    assert "items" not in snapshot
    assert "pma_threads" in snapshot


def test_gather_hub_message_snapshot_excludes_unrequested_pma_threads(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    PmaThreadStore(hub_root).create_thread(
        "codex",
        repo_root,
        repo_id="repo",
        name="test-thread",
    )
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(RuntimeError)
        ),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert "pma_threads" not in snapshot


def test_gather_hub_message_snapshot_includes_generated_at_always(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert "generated_at" in snapshot
    assert snapshot["generated_at"]


def test_gather_hub_message_snapshot_does_not_fabricate_inbox_on_supervisor_error(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        ),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert snapshot["items"] == []
    assert "generated_at" in snapshot
    assert "unreadable_diagnostics" in snapshot
    diag = snapshot["unreadable_diagnostics"]
    assert any(d["section"] == "inbox" for d in diag)


def test_gather_hub_message_snapshot_returns_empty_pma_threads_not_missing(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"pma_threads"},
    )

    assert "pma_threads" in snapshot
    assert isinstance(snapshot["pma_threads"], list)


def test_gather_hub_message_snapshot_does_not_fabricate_automation_when_not_requested(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=_valid_config(hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert "automation" not in snapshot


def test_gather_hub_message_snapshot_does_not_fabricate_action_queue_when_not_requested(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=_valid_config(hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert "action_queue" not in snapshot


def test_gather_hub_message_snapshot_does_not_fabricate_pma_files_detail_when_not_requested(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=_valid_config(hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert "pma_files_detail" not in snapshot


def test_valid_empty_inbox_omits_unreadable_diagnostics(tmp_path) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=_valid_config(hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert snapshot["items"] == []
    assert "unreadable_diagnostics" not in snapshot


def test_unreadable_supervisor_includes_diagnostic_with_reason(tmp_path) -> None:
    hub_root = Path(tmp_path)
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(ValueError("corrupt config"))
        ),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(
        context,
        sections={"inbox"},
    )

    assert snapshot["items"] == []
    diagnostics = snapshot.get("unreadable_diagnostics", [])
    assert len(diagnostics) >= 1
    assert diagnostics[0]["section"] == "inbox"
    assert "corrupt config" in diagnostics[0]["reason"]
    assert diagnostics[0]["source"] == "supervisor.list_repos"


def test_unreadable_state_distinguishes_from_valid_empty(tmp_path) -> None:
    hub_root = Path(tmp_path)
    empty_context = SimpleNamespace(
        supervisor=SimpleNamespace(list_repos=lambda: []),
        config=_valid_config(hub_root),
    )
    error_context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(OSError("permission denied"))
        ),
        config=SimpleNamespace(root=hub_root),
    )

    valid_snapshot = hub_gather_service.gather_hub_message_snapshot(
        empty_context,
        sections={"inbox"},
    )
    error_snapshot = hub_gather_service.gather_hub_message_snapshot(
        error_context,
        sections={"inbox"},
    )

    assert valid_snapshot["items"] == []
    assert "unreadable_diagnostics" not in valid_snapshot

    assert error_snapshot["items"] == []
    assert "unreadable_diagnostics" in error_snapshot
    assert any(
        d["source"] == "supervisor.list_repos"
        for d in error_snapshot["unreadable_diagnostics"]
    )
