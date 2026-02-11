import asyncio
import json
from pathlib import Path
from typing import Optional

import anyio
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import pma_active_context_content, seed_hub_files
from codex_autorunner.core.app_server_threads import PMA_KEY, PMA_OPENCODE_KEY
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes import pma as pma_routes


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _enable_pma(
    hub_root: Path,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    max_text_chars: Optional[int] = None,
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    if max_text_chars is not None:
        cfg["pma"]["max_text_chars"] = max_text_chars
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


def test_pma_chat_rejects_oversize_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root, max_text_chars=5)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "toolong"})
    assert resp.status_code == 400
    payload = resp.json()
    assert "max_text_chars" in (payload.get("detail") or "")


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


@pytest.mark.anyio
async def test_pma_chat_idempotency_key_uses_full_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
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
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        prefix = "a" * 100
        message_one = f"{prefix}one"
        message_two = f"{prefix}two"
        task_one = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": message_one})
        )
        await anyio.sleep(0.05)
        task_two = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": message_two})
        )
        await anyio.sleep(0.05)
        assert not task_two.done()
        blocker.set()
        with anyio.fail_after(2):
            resp_one = await task_one
            resp_two = await task_two

    assert resp_one.status_code == 200
    assert resp_two.status_code == 200
    assert resp_two.json().get("deduped") is not True


@pytest.mark.anyio
async def test_pma_active_updates_during_running_turn(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = "turn-1"

        async def wait(self, timeout=None):
            await blocker.wait()
            return type(
                "Result",
                (),
                {"agent_messages": ["ok"], "raw_events": [], "errors": []},
            )()

    class FakeClient:
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
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        chat_task = asyncio.create_task(
            client.post("/hub/pma/chat", json={"message": "hi"})
        )
        try:
            with anyio.fail_after(2):
                while True:
                    resp = await client.get("/hub/pma/active")
                    assert resp.status_code == 200
                    payload = resp.json()
                    current = payload.get("current") or {}
                    if (
                        payload.get("active")
                        and current.get("thread_id")
                        and current.get("turn_id")
                    ):
                        break
                    await anyio.sleep(0.05)
            assert payload["active"] is True
            assert payload["current"]["lane_id"] == "pma:default"
        finally:
            blocker.set()
        resp = await chat_task
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


def test_pma_active_clears_on_prompt_build_error(hub_env, monkeypatch) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    async def _boom(*args, **kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(pma_routes, "build_hub_snapshot", _boom)

    client = TestClient(app)
    resp = client.post("/hub/pma/chat", json={"message": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "error"
    assert "snapshot failed" in (payload.get("detail") or "")

    active = client.get("/hub/pma/active").json()
    assert active["active"] is False
    assert active["current"] == {}


def test_pma_thread_reset_clears_registry(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    registry = app.state.app_server_threads
    registry.set_thread_id(PMA_KEY, "thread-codex")
    registry.set_thread_id(PMA_OPENCODE_KEY, "thread-opencode")

    client = TestClient(app)
    resp = client.post("/hub/pma/thread/reset", json={"agent": "opencode"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) == "thread-codex"
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None

    resp = client.post("/hub/pma/thread/reset", json={"agent": "all"})
    assert resp.status_code == 200
    payload = resp.json()
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert registry.get_thread_id(PMA_KEY) is None


def test_pma_reset_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post("/hub/pma/reset", json={"agent": "all"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()


def test_pma_stop_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post("/hub/pma/stop", json={"lane_id": "pma:default"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()
    assert payload["details"]["lane_id"] == "pma:default"


def test_pma_new_creates_artifact(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.post(
        "/hub/pma/new", json={"agent": "codex", "lane_id": "pma:default"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("status") == "ok"
    artifact_path = Path(payload["artifact_path"])
    assert artifact_path.exists()


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


def test_pma_docs_list(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200
    payload = resp.json()
    assert "docs" in payload
    docs = payload["docs"]
    assert isinstance(docs, list)
    doc_names = [doc["name"] for doc in docs]
    assert doc_names == [
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    ]
    for doc in docs:
        assert "name" in doc
        assert "exists" in doc
        if doc["exists"]:
            assert "size" in doc
            assert "mtime" in doc
        if doc["name"] == "active_context.md":
            assert "line_count" in doc


def test_pma_docs_get(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert "content" in payload
    assert isinstance(payload["content"], str)


def test_pma_docs_get_nonexistent(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    # Delete the canonical doc, then try to get it
    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_agents_path = pma_dir / "docs" / "AGENTS.md"
    if docs_agents_path.exists():
        docs_agents_path.unlink()

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_pma_docs_list_migrates_legacy_doc_into_canonical(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_path = pma_dir / "docs" / "active_context.md"
    legacy_path = pma_dir / "active_context.md"

    docs_path.unlink(missing_ok=True)
    legacy_content = "# Legacy copy\n\n- migrated\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 200

    assert docs_path.exists()
    assert docs_path.read_text(encoding="utf-8") == legacy_content
    assert not legacy_path.exists()

    resp = client.get("/hub/pma/docs/active_context.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == legacy_content


def test_pma_docs_put(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    new_content = "# AGENTS\n\nNew content"
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": new_content})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "AGENTS.md"
    assert payload["status"] == "ok"

    # Verify the content was saved
    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 200
    assert resp.json()["content"] == new_content


def test_pma_docs_put_invalid_name(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/invalid.md", json={"content": "test"})
    assert resp.status_code == 400
    assert "Unknown doc name" in resp.json()["detail"]


def test_pma_docs_put_too_large(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    large_content = "x" * 500_001
    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": large_content})
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_pma_docs_put_invalid_content_type(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.put("/hub/pma/docs/AGENTS.md", json={"content": 123})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI returns a list of validation errors
    if isinstance(detail, list):
        assert any("content" in str(err) for err in detail)
    else:
        assert "content" in str(detail)


def test_pma_context_snapshot(hub_env) -> None:
    seed_hub_files(hub_env.hub_root, force=True)
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    pma_dir = hub_env.hub_root / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_path = docs_dir / "active_context.md"
    active_content = "# Active Context\n\n- alpha\n- beta\n"
    active_path.write_text(active_content, encoding="utf-8")

    resp = client.post("/hub/pma/context/snapshot", json={"reset": True})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["active_context_line_count"] == len(active_content.splitlines())
    assert payload["reset"] is True

    log_content = (docs_dir / "context_log.md").read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content
    assert active_content in log_content
    assert active_path.read_text(encoding="utf-8") == pma_active_context_content()


def test_pma_docs_disabled(hub_env) -> None:
    _disable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    resp = client.get("/hub/pma/docs")
    assert resp.status_code == 404

    resp = client.get("/hub/pma/docs/AGENTS.md")
    assert resp.status_code == 404
