import json
from pathlib import Path
from typing import Optional

import yaml
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.server import create_hub_app


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _enable_pma(
    hub_root: Path, *, model: Optional[str] = None, reasoning: Optional[str] = None
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    _write_config(hub_root / CONFIG_FILENAME, cfg)


def _disable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = False
    _write_config(hub_root / CONFIG_FILENAME, cfg)


def test_pma_agents_endpoint(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.get("/hub/pma/agents")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("agents"), list)
    assert payload.get("default") in {agent.get("id") for agent in payload["agents"]}


def test_pma_chat_requires_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={})
    assert resp.status_code == 400


def test_pma_routes_enabled_by_default(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code == 200
    assert client.post("/hub/pma/chat", json={}).status_code == 400


def test_pma_routes_disabled_by_config(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    assert client.get("/hub/pma/agents").status_code == 404
    assert client.post("/hub/pma/chat", json={"message": "hi"}).status_code == 404


def test_pma_chat_applies_model_reasoning_defaults(hub_env) -> None:
    _enable_pma(hub_env.hub_root, model="test-model", reasoning="high")

    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_kwargs = None

        async def thread_resume(self, thread_id: str) -> None:
            return None

        async def thread_start(self, root: str) -> dict:
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            self.turn_kwargs = turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert app.state.app_server_supervisor.client.turn_kwargs == {
        "model": "test-model",
        "effort": "high",
    }


def test_pma_thread_reset_clears_registry(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id(PMA_KEY, "thread-codex")
    registry.set_thread_id(PMA_OPENCODE_KEY, "thread-opencode")

    client = TestClient(app)
    resp = client.post("/hub/pma/thread/reset", json={"agent": "opencode"})
    assert resp.status_code == 200
    assert registry.get_thread_id(PMA_KEY) == "thread-codex"
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None

    resp = client.post("/hub/pma/thread/reset", json={"agent": "all"})
    assert resp.status_code == 200
    assert registry.get_thread_id(PMA_KEY) is None


def test_pma_files_list_empty(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []
    assert payload["outbox"] == []


def test_pma_files_upload_list_download_delete(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to inbox
    files = {"file.txt": ("file.txt", b"Hello, PMA!", "text/plain")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["inbox"]) == 1
    assert payload["inbox"][0]["name"] == "file.txt"
    assert payload["inbox"][0]["box"] == "inbox"
    assert payload["inbox"][0]["size"] == 11
    assert payload["inbox"][0]["source"] == "pma"
    assert "/hub/pma/files/inbox/file.txt" in payload["inbox"][0]["url"]
    assert payload["outbox"] == []

    # Download file
    resp = client.get("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200
    assert resp.content == b"Hello, PMA!"

    # Delete file
    resp = client.delete("/hub/pma/files/inbox/file.txt")
    assert resp.status_code == 200

    # Verify file is gone
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["inbox"] == []


def test_pma_files_invalid_box(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Try to upload to invalid box
    files = {"file.txt": ("file.txt", b"test", "text/plain")}
    resp = client.post("/hub/pma/files/invalid", files=files)
    assert resp.status_code == 400

    # Try to download from invalid box
    resp = client.get("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400

    # Try to delete from invalid box
    resp = client.delete("/hub/pma/files/invalid/file.txt")
    assert resp.status_code == 400


def test_pma_files_outbox(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Upload a file to outbox
    files = {"output.txt": ("output.txt", b"Output content", "text/plain")}
    resp = client.post("/hub/pma/files/outbox", files=files)
    assert resp.status_code == 200

    # List files
    resp = client.get("/hub/pma/files")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["outbox"]) == 1
    assert payload["outbox"][0]["name"] == "output.txt"
    assert payload["outbox"][0]["box"] == "outbox"
    assert "/hub/pma/files/outbox/output.txt" in payload["outbox"][0]["url"]
    assert payload["inbox"] == []

    # Download from outbox
    resp = client.get("/hub/pma/files/outbox/output.txt")
    assert resp.status_code == 200
    assert resp.content == b"Output content"


def test_pma_files_rejects_invalid_filenames(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Test traversal attempts - upload rejects invalid filenames
    for filename in ["../x", "..", "a/b", "a\\b", ".", ""]:
        files = {"file": (filename, b"test", "text/plain")}
        resp = client.post("/hub/pma/files/inbox", files=files)
        assert resp.status_code == 400, f"Should reject filename: {filename}"
        assert "Invalid filename" in resp.json()["detail"]


def test_pma_files_size_limit(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    max_upload_bytes = DEFAULT_HUB_CONFIG["pma"]["max_upload_bytes"]

    # Upload a file that exceeds the size limit
    large_content = b"x" * (max_upload_bytes + 1)
    files = {"large.bin": ("large.bin", large_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()

    # Upload a file that is exactly at the limit
    limit_content = b"y" * max_upload_bytes
    files = {"limit.bin": ("limit.bin", limit_content, "application/octet-stream")}
    resp = client.post("/hub/pma/files/inbox", files=files)
    assert resp.status_code == 200
    assert "limit.bin" in resp.json()["saved"]


def test_pma_files_returns_404_for_nonexistent_files(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Download non-existent file
    resp = client.get("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]

    # Delete non-existent file
    resp = client.delete("/hub/pma/files/inbox/nonexistent.txt")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]
