from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.surfaces.web.services import hub_gather as hub_gather_service


def _write_dispatch(
    repo_root: Path, run_id: str, seq: int, *, mode: str, title: str = "T"
) -> None:
    entry_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / f"{seq:04d}"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "DISPATCH.md").write_text(
        f"---\nmode: {mode}\ntitle: {title}\n---\n\nBody\n",
        encoding="utf-8",
    )


def test_latest_dispatch_prefers_handoff_and_keeps_turn_summary(tmp_path) -> None:
    repo_root = Path(tmp_path)
    run_id = "11111111-1111-1111-1111-111111111111"
    _write_dispatch(repo_root, run_id, seq=2, mode="turn_summary", title="Summary")
    _write_dispatch(repo_root, run_id, seq=1, mode="pause", title="Need Input")

    latest = hub_gather_service.latest_dispatch(
        repo_root,
        run_id,
        {"workspace_root": str(repo_root), "runs_dir": ".codex-autorunner/runs"},
    )
    assert latest is not None
    assert latest["seq"] == 1
    assert latest["dispatch"]["mode"] == "pause"
    assert latest["turn_summary_seq"] == 2
    assert latest["turn_summary"]["mode"] == "turn_summary"


def test_latest_dispatch_surfaces_unreadable_latest_dispatch(tmp_path) -> None:
    repo_root = Path(tmp_path)
    run_id = "22222222-2222-2222-2222-222222222222"
    _write_dispatch(repo_root, run_id, seq=1, mode="pause", title="Older")
    entry_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0002"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "DISPATCH.md").write_text(
        "---\nmode: invalid_mode\ntitle: Broken\n---\n\nBody\n",
        encoding="utf-8",
    )

    latest = hub_gather_service.latest_dispatch(
        repo_root,
        run_id,
        {"workspace_root": str(repo_root), "runs_dir": ".codex-autorunner/runs"},
    )
    assert latest is not None
    assert latest["seq"] == 2
    assert latest["dispatch"] is None
    assert latest["errors"]


def test_gather_hub_message_snapshot_returns_empty_items_on_supervisor_error() -> None:
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(RuntimeError)
        )
    )
    snapshot = hub_gather_service.gather_hub_message_snapshot(  # type: ignore[arg-type]
        context,
        sections={"inbox"},
    )
    assert snapshot["items"] == []


def test_gather_hub_message_snapshot_keeps_other_sections_on_supervisor_error(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    PmaThreadStore(hub_root).create_thread(
        "codex",
        repo_root,
        repo_id="repo",
        name="snapshot-thread",
    )
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(RuntimeError)
        ),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(  # type: ignore[arg-type]
        context,
        sections={"inbox", "pma_threads"},
    )

    assert snapshot["items"] == []
    assert snapshot["pma_threads"][0]["name"] == "snapshot-thread"


def test_gather_hub_message_snapshot_only_serializes_requested_sections(
    tmp_path,
) -> None:
    hub_root = Path(tmp_path)
    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    PmaThreadStore(hub_root).create_thread(
        "codex",
        repo_root,
        repo_id="repo",
        name="requested-only-thread",
    )
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(RuntimeError)
        ),
        config=SimpleNamespace(root=hub_root),
    )

    snapshot = hub_gather_service.gather_hub_message_snapshot(  # type: ignore[arg-type]
        context,
        sections={"pma_threads"},
    )

    assert set(snapshot) == {"generated_at", "pma_threads"}
    assert snapshot["pma_threads"][0]["name"] == "requested-only-thread"
