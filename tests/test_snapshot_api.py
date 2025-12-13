from pathlib import Path

import pytest


fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from codex_autorunner.server import create_app


def _client(repo_root: Path) -> TestClient:
    app = create_app(repo_root)
    return TestClient(app)


def test_get_snapshot_when_missing(repo: Path) -> None:
    client = _client(repo)
    res = client.get("/api/snapshot")
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["exists"] is False
    assert payload["content"] == ""
    assert isinstance(payload["state"], dict)


def test_post_snapshot_persists_and_loads(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner import snapshot as snapshot_mod

    monkeypatch.setattr(
        snapshot_mod, "_run_codex", lambda *a, **k: "# Snapshot\n\nHello\n"
    )

    client = _client(repo)
    res = client.post(
        "/api/snapshot",
        json={"mode": "from_scratch", "max_chars": 2000, "audience": "overview"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert "Hello" in payload["content"]
    assert payload["state"]["mode"] == "from_scratch"

    res2 = client.get("/api/snapshot")
    assert res2.status_code == 200, res2.text
    payload2 = res2.json()
    assert payload2["exists"] is True
    assert "Hello" in payload2["content"]


def test_post_snapshot_invalid_mode(repo: Path) -> None:
    client = _client(repo)
    res = client.post("/api/snapshot", json={"mode": "nope"})
    assert res.status_code == 400, res.text


def test_post_snapshot_incremental_falls_back_when_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner import snapshot as snapshot_mod

    monkeypatch.setattr(
        snapshot_mod, "_run_codex", lambda *a, **k: "# Snapshot\n\nHi\n"
    )

    client = _client(repo)
    res = client.post(
        "/api/snapshot",
        json={"mode": "incremental", "max_chars": 2000, "audience": "overview"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["state"]["mode"] == "from_scratch"
