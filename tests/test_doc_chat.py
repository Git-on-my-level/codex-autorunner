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
    prompts: list[str] = []

    async def fake_run(self, prompt: str, chat_id: str) -> str:  # type: ignore[override]
        prompts.append(prompt)
        path = self.engine.config.doc_path("todo")
        path.write_text("- [ ] rewritten task\n- [x] done task\n", encoding="utf-8")
        return "Agent: cleaned"

    monkeypatch.setattr(DocChatService, "_run_codex_cli", fake_run)
    monkeypatch.setattr(
        DocChatService, "_recent_run_summary", lambda self: "last run summary"
    )

    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "rewrite the todo"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "ok"
    assert data["agent_message"] == "cleaned"
    assert "- [ ] rewritten task" in data["patch"]

    doc_path = repo / ".codex-autorunner" / "TODO.md"
    assert "rewritten" in doc_path.read_text(encoding="utf-8")

    res_apply = client.post("/api/docs/todo/chat/apply")
    assert res_apply.status_code == 200, res_apply.text
    applied = res_apply.json()
    assert applied["content"].strip().splitlines() == [
        "- [ ] rewritten task",
        "- [x] done task",
    ]
    assert applied["agent_message"] == "cleaned"

    prompt = prompts[0]
    assert "User request: rewrite the todo" in prompt
    assert "<TARGET_DOC>" in prompt and "</TARGET_DOC>" in prompt
    assert "last run summary" in prompt


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
    prompts: list[str] = []

    async def fake_run(self, prompt: str, chat_id: str) -> str:  # type: ignore[override]
        prompts.append(prompt)
        path = self.engine.config.doc_path("summary")
        path.write_text("summary updated\n", encoding="utf-8")
        return "Agent: summarized"

    monkeypatch.setattr(DocChatService, "_run_codex_cli", fake_run)
    monkeypatch.setattr(
        DocChatService, "_recent_run_summary", lambda self: "last run summary"
    )

    client = _client(repo)
    res = client.post("/api/docs/summary/chat", json={"message": "rewrite the summary"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "ok"
    assert data["agent_message"] == "summarized"
    assert "summary updated" in data["patch"]

    doc_path = repo / ".codex-autorunner" / "SUMMARY.md"
    assert "summary updated" in doc_path.read_text(encoding="utf-8")

    res_apply = client.post("/api/docs/summary/chat/apply")
    assert res_apply.status_code == 200, res_apply.text
    applied = res_apply.json()
    assert applied["content"].strip() == "summary updated"
    assert applied["agent_message"] == "summarized"

    prompt = prompts[0]
    assert "Target doc: SUMMARY" in prompt
    assert "User request: rewrite the summary" in prompt


def test_chat_validation_failure_does_not_write(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    existing = (repo / ".codex-autorunner" / "TODO.md").read_text(encoding="utf-8")

    async def fake_run(self, prompt: str, chat_id: str) -> str:  # type: ignore[override]
        # overwrite with bad content
        (repo / ".codex-autorunner" / "TODO.md").write_text(
            "bad content\n", encoding="utf-8"
        )
        return "Agent: nope"

    monkeypatch.setattr(DocChatService, "_run_codex_cli", fake_run)
    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "break it"})
    assert res.status_code == 200
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
    prompt = service._build_prompt(request)
    assert "Target doc: PROGRESS" in prompt
    assert "User request: summarize" in prompt
    assert "<RECENT_RUN>\nrecent notes\n</RECENT_RUN>" in prompt
    assert "first task" in prompt
    assert "progress body" in prompt
    assert "opinions body" in prompt
    assert "spec body" in prompt
    assert "<TARGET_DOC>" in prompt and "</TARGET_DOC>" in prompt
