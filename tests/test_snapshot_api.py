from pathlib import Path

import pytest

from codex_autorunner.server import create_hub_app

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


def test_get_snapshot_when_missing(hub_env, repo: Path) -> None:
    client = TestClient(create_hub_app(hub_env.hub_root))
    res = client.get(f"/repos/{hub_env.repo_id}/api/snapshot")
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["exists"] is False
    assert payload["content"] == ""
    assert isinstance(payload["state"], dict)


def test_post_snapshot_persists_and_loads(
    hub_env, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.core.snapshot import SnapshotResult, SnapshotService

    async def mock_generate(self) -> SnapshotResult:
        path = self.engine.repo_root / ".codex-autorunner" / "SNAPSHOT.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Snapshot\n\nHello\n", encoding="utf-8")
        return SnapshotResult(
            content="# Snapshot\n\nHello\n", truncated=False, state={"truncated": False}
        )

    monkeypatch.setattr(SnapshotService, "generate_snapshot", mock_generate)

    client = TestClient(create_hub_app(hub_env.hub_root))
    res = client.post(f"/repos/{hub_env.repo_id}/api/snapshot", json={})
    assert res.status_code == 200, res.text
    payload = res.json()
    assert "Hello" in payload["content"]
    assert payload["state"].get("truncated") is False

    res2 = client.get(f"/repos/{hub_env.repo_id}/api/snapshot")
    assert res2.status_code == 200, res2.text
    payload2 = res2.json()
    assert payload2["exists"] is True
    assert "Hello" in payload2["content"]


def test_post_snapshot_ignores_legacy_params(
    hub_env, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.core.snapshot import SnapshotResult, SnapshotService

    async def mock_generate(self) -> SnapshotResult:
        return SnapshotResult(content="# Snapshot\n\nHi\n", truncated=False, state={})

    monkeypatch.setattr(SnapshotService, "generate_snapshot", mock_generate)
    client = TestClient(create_hub_app(hub_env.hub_root))
    res = client.post(
        f"/repos/{hub_env.repo_id}/api/snapshot",
        json={"mode": "nope", "max_chars": 1, "audience": "onboarding"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert "Hi" in payload["content"]
