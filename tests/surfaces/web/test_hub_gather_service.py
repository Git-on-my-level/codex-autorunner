from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_gather_hub_messages_returns_empty_on_supervisor_error() -> None:
    context = SimpleNamespace(
        supervisor=SimpleNamespace(
            list_repos=lambda: (_ for _ in ()).throw(RuntimeError)
        )
    )
    items = hub_gather_service.gather_hub_messages(context)  # type: ignore[arg-type]
    assert items == []
