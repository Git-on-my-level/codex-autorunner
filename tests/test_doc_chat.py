import difflib
import json
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from codex_autorunner.core.config import DEFAULT_CONFIG
from codex_autorunner.core.doc_chat import DocChatRequest, DocChatService
from codex_autorunner.core.engine import Engine
from codex_autorunner.server import create_app


def _write_default_config(repo_root: Path) -> None:
    data = json.loads(json.dumps(DEFAULT_CONFIG))
    config_path = repo_root / ".codex-autorunner" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    _write_default_config(repo)
    work = repo / ".codex-autorunner"
    work.mkdir(exist_ok=True)
    (work / "TODO.md").write_text(
        "- [ ] first task\n- [x] done task\n", encoding="utf-8"
    )
    (work / "PROGRESS.md").write_text("progress body\n", encoding="utf-8")
    (work / "OPINIONS.md").write_text("opinions body\n", encoding="utf-8")
    (work / "SPEC.md").write_text("spec body\n", encoding="utf-8")
    (work / "SUMMARY.md").write_text("summary body\n", encoding="utf-8")
    return repo


def _make_patch(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return _seed_repo(tmp_path)


def _client(repo_root: Path) -> TestClient:
    app = create_app(repo_root)
    return TestClient(app)


def test_chat_rejects_invalid_payload(repo: Path):
    client = _client(repo)
    res = client.post("/api/docs/unknown/chat", json={"message": "hi"})
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid doc kind"

    res = client.post("/api/docs/todo/chat", json={"message": ""})
    assert res.status_code == 400
    assert res.json()["detail"] == "message is required"

    res = client.post("/api/docs/todo/chat", json=None)
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid payload"


def test_chat_repo_lock_conflict(repo: Path):
    lock_path = repo / ".codex-autorunner" / "lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "run it"})
    assert res.status_code == 409
    assert "Autorunner is running" in res.json()["detail"]


def test_chat_busy_conflict(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(DocChatService, "doc_busy", lambda self, kind: True)
    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "hi"})
    assert res.status_code == 409
    assert "already running" in res.json()["detail"]


def test_chat_success_writes_doc_and_returns_agent_message(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    async def fake_execute(self, request: DocChatRequest) -> dict:  # type: ignore[override]
        target_path = self.engine.config.doc_path(request.kind)
        before = target_path.read_text(encoding="utf-8")
        after = "- [ ] rewritten task\n- [x] done task\n"
        rel_path = str(target_path.relative_to(self.engine.repo_root))
        patch_text = _make_patch(rel_path, before, after)
        self.patch_path.write_text(patch_text, encoding="utf-8")
        self.last_agent_message = "cleaned"
        return {
            "status": "ok",
            "kind": request.kind,
            "patch": patch_text,
            "content": after,
            "agent_message": "cleaned",
        }

    monkeypatch.setattr(DocChatService, "_execute_app_server", fake_execute)

    client = _client(repo)
    doc_path = repo / ".codex-autorunner" / "TODO.md"
    original = doc_path.read_text(encoding="utf-8")
    res = client.post("/api/docs/todo/chat", json={"message": "rewrite the todo"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "ok"
    assert data["agent_message"] == "cleaned"
    assert "- [ ] rewritten task" in data["patch"]

    assert doc_path.read_text(encoding="utf-8") == original

    res_apply = client.post("/api/docs/todo/chat/apply")
    assert res_apply.status_code == 200, res_apply.text
    applied = res_apply.json()
    assert applied["content"].strip().splitlines() == [
        "- [ ] rewritten task",
        "- [x] done task",
    ]
    assert applied["agent_message"] == "cleaned"
    assert "rewritten" in doc_path.read_text(encoding="utf-8")


def test_api_docs_includes_summary(repo: Path):
    client = _client(repo)
    res = client.get("/api/docs")
    assert res.status_code == 200, res.text
    data = res.json()
    assert set(data.keys()) >= {"todo", "progress", "opinions", "spec", "summary"}
    assert data["summary"] == "summary body\n"


def test_api_docs_clear_returns_full_payload_and_resets_work_docs(repo: Path):
    client = _client(repo)
    res = client.post("/api/docs/clear")
    assert res.status_code == 200, res.text
    data = res.json()
    assert set(data.keys()) >= {"todo", "progress", "opinions", "spec", "summary"}
    assert data["todo"] == "# TODO\n\n"
    assert data["progress"] == "# Progress\n\n"
    assert data["opinions"] == "# Opinions\n\n"
    assert data["spec"] == "spec body\n"
    assert data["summary"] == "summary body\n"

    work_dir = repo / ".codex-autorunner"
    assert (work_dir / "TODO.md").read_text(encoding="utf-8") == "# TODO\n\n"
    assert (work_dir / "PROGRESS.md").read_text(encoding="utf-8") == "# Progress\n\n"
    assert (work_dir / "OPINIONS.md").read_text(encoding="utf-8") == "# Opinions\n\n"


def test_chat_accepts_summary_kind(repo: Path, monkeypatch: pytest.MonkeyPatch):
    async def fake_execute(self, request: DocChatRequest) -> dict:  # type: ignore[override]
        target_path = self.engine.config.doc_path(request.kind)
        before = target_path.read_text(encoding="utf-8")
        after = "summary updated\n"
        rel_path = str(target_path.relative_to(self.engine.repo_root))
        patch_text = _make_patch(rel_path, before, after)
        self.patch_path.write_text(patch_text, encoding="utf-8")
        self.last_agent_message = "summarized"
        return {
            "status": "ok",
            "kind": request.kind,
            "patch": patch_text,
            "content": after,
            "agent_message": "summarized",
        }

    monkeypatch.setattr(DocChatService, "_execute_app_server", fake_execute)

    client = _client(repo)
    doc_path = repo / ".codex-autorunner" / "SUMMARY.md"
    original = doc_path.read_text(encoding="utf-8")
    res = client.post("/api/docs/summary/chat", json={"message": "rewrite the summary"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "ok"
    assert data["agent_message"] == "summarized"
    assert "summary updated" in data["patch"]

    assert doc_path.read_text(encoding="utf-8") == original

    res_apply = client.post("/api/docs/summary/chat/apply")
    assert res_apply.status_code == 200, res_apply.text
    applied = res_apply.json()
    assert applied["content"].strip() == "summary updated"
    assert applied["agent_message"] == "summarized"
    assert "summary updated" in doc_path.read_text(encoding="utf-8")


def test_chat_validation_failure_does_not_write(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    existing = (repo / ".codex-autorunner" / "TODO.md").read_text(encoding="utf-8")

    async def fake_execute(self, request: DocChatRequest) -> dict:  # type: ignore[override]
        target_path = self.engine.config.doc_path(request.kind)
        before = target_path.read_text(encoding="utf-8")
        after = "bad content\n"
        rel_path = str(target_path.relative_to(self.engine.repo_root))
        patch_text = _make_patch(rel_path, before, after)
        self.patch_path.write_text(patch_text, encoding="utf-8")
        self.last_agent_message = "nope"
        return {
            "status": "ok",
            "kind": request.kind,
            "patch": patch_text,
            "content": after,
            "agent_message": "nope",
        }

    monkeypatch.setattr(DocChatService, "_execute_app_server", fake_execute)
    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "break it"})
    assert res.status_code == 200
    assert (repo / ".codex-autorunner" / "TODO.md").read_text(
        encoding="utf-8"
    ) == existing
    res_discard = client.post("/api/docs/todo/chat/discard")
    assert res_discard.status_code == 200
    assert (repo / ".codex-autorunner" / "TODO.md").read_text(
        encoding="utf-8"
    ) == existing


def test_prompt_includes_all_docs_and_recent_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    engine = Engine(repo)
    service = DocChatService(engine)
    monkeypatch.setattr(service, "_recent_run_summary", lambda: "recent notes")
    request = DocChatRequest(kind="progress", message="summarize", stream=False)
    prompt = service._build_app_server_prompt(request)
    assert "Target doc: PROGRESS" in prompt
    assert "User request:\nsummarize" in prompt
    assert "<RECENT_RUN_SUMMARY>\nrecent notes\n</RECENT_RUN_SUMMARY>" in prompt
    assert "Doc path: .codex-autorunner/PROGRESS.md" in prompt
    assert "TODO: .codex-autorunner/TODO.md" in prompt
    assert "OPINIONS: .codex-autorunner/OPINIONS.md" in prompt
    assert "SPEC: .codex-autorunner/SPEC.md" in prompt
    assert "progress body" in prompt
    assert "<TARGET_DOC_EXCERPT>" in prompt and "</TARGET_DOC_EXCERPT>" in prompt
