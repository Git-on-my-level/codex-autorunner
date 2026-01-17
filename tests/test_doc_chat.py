import asyncio
import difflib
import os
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.doc_chat import (
    DocChatConflictError,
    DocChatDraftState,
    DocChatRequest,
    DocChatService,
    DocChatValidationError,
)
from codex_autorunner.core.engine import Engine
from codex_autorunner.core.locks import FileLock
from codex_autorunner.integrations.app_server.client import TurnResult
from codex_autorunner.server import create_app


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seed_hub_files(repo, force=True)
    seed_repo_files(repo, force=True, git_required=False)
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


class FakeHandle:
    def __init__(self, result: TurnResult, event: asyncio.Event) -> None:
        self.turn_id = "turn-1"
        self.thread_id = "thread-1"
        self._result = result
        self._event = event

    async def wait(self, *, timeout: Optional[float] = None) -> TurnResult:
        if timeout is None:
            await self._event.wait()
        else:
            await asyncio.wait_for(self._event.wait(), timeout)
        return self._result


class FakeClient:
    def __init__(self, result: TurnResult, event: asyncio.Event) -> None:
        self._result = result
        self._event = event
        self.interrupt_calls = []

    async def thread_resume(self, thread_id: str) -> dict:
        return {"id": thread_id}

    async def thread_start(self, _repo_root: str) -> dict:
        return {"id": "thread-1"}

    async def turn_start(self, *_args, **_kwargs) -> FakeHandle:
        return FakeHandle(self._result, self._event)

    async def turn_interrupt(
        self, turn_id: str, *, thread_id: Optional[str] = None
    ) -> dict:
        self.interrupt_calls.append((turn_id, thread_id))
        self._event.set()
        return {"turn_id": turn_id, "thread_id": thread_id}


class FakeSupervisor:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def get_client(self, _workspace_root) -> FakeClient:
        return self._client

    async def close_all(self) -> None:
        return None


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

    res = client.post("/api/docs/chat", json={"message": "hi", "targets": ["specs"]})
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid doc kind"


def test_parse_request_enforces_kind_targets(repo: Path) -> None:
    engine = Engine(repo)
    service = DocChatService(engine)

    req = service.parse_request({"message": "hi", "targets": ["todo"]}, kind="todo")
    assert req.targets == ("todo",)

    with pytest.raises(DocChatValidationError):
        service.parse_request({"message": "hi", "targets": ["spec"]}, kind="todo")


def test_chat_repo_lock_conflict(repo: Path):
    lock_path = repo / ".codex-autorunner" / "lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "run it"})
    assert res.status_code == 409
    assert "Autorunner is running" in res.json()["detail"]


def test_chat_busy_conflict(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        DocChatService, "doc_busy", lambda self, *_args, **_kwargs: True
    )
    client = _client(repo)
    res = client.post("/api/docs/todo/chat", json={"message": "hi"})
    assert res.status_code == 409
    assert "already running" in res.json()["detail"]


def test_chat_success_writes_doc_and_returns_agent_message(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    async def fake_execute(self, request: DocChatRequest) -> dict:  # type: ignore[override]
        target_path = self.engine.config.doc_path("todo")
        before = target_path.read_text(encoding="utf-8")
        after = "- [ ] rewritten task\n- [x] done task\n"
        rel_path = str(target_path.relative_to(self.engine.repo_root))
        patch_text = _make_patch(rel_path, before, after)
        created_at = "2024-01-01T00:00:00Z"
        draft = DocChatDraftState(
            content=after,
            patch=patch_text,
            agent_message="cleaned",
            created_at=created_at,
            base_hash=self._hash_content(before),
        )
        self._save_drafts({"todo": draft})
        return {
            "status": "ok",
            "agent_message": "cleaned",
            "updated": ["todo"],
            "drafts": {"todo": draft.to_dict()},
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
    assert data["updated"] == ["todo"]
    assert "- [ ] rewritten task" in data["drafts"]["todo"]["patch"]

    assert doc_path.read_text(encoding="utf-8") == original

    res_apply = client.post("/api/docs/todo/chat/apply")
    assert res_apply.status_code == 200, res_apply.text
    applied = res_apply.json()
    assert applied["content"].strip().splitlines() == [
        "- [ ] rewritten task",
        "- [x] done task",
    ]
    assert applied["agent_message"] == "cleaned"
    assert applied["base_hash"]
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
        target_path = self.engine.config.doc_path("summary")
        before = target_path.read_text(encoding="utf-8")
        after = "summary updated\n"
        rel_path = str(target_path.relative_to(self.engine.repo_root))
        patch_text = _make_patch(rel_path, before, after)
        created_at = "2024-01-01T00:00:00Z"
        draft = DocChatDraftState(
            content=after,
            patch=patch_text,
            agent_message="summarized",
            created_at=created_at,
            base_hash=self._hash_content(before),
        )
        self._save_drafts({"summary": draft})
        return {
            "status": "ok",
            "agent_message": "summarized",
            "updated": ["summary"],
            "drafts": {"summary": draft.to_dict()},
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
    assert "summary updated" in data["drafts"]["summary"]["patch"]

    assert doc_path.read_text(encoding="utf-8") == original

    res_apply = client.post("/api/docs/summary/chat/apply")
    assert res_apply.status_code == 200, res_apply.text
    applied = res_apply.json()
    assert applied["content"].strip() == "summary updated"
    assert applied["agent_message"] == "summarized"


@pytest.mark.anyio
async def test_doc_chat_interrupt_skips_patch(repo: Path) -> None:
    engine = Engine(repo)
    target_path = engine.config.doc_path("todo")
    before = target_path.read_text(encoding="utf-8")
    after = "- [ ] interrupted update\n- [x] done task\n"
    rel_path = str(target_path.relative_to(engine.repo_root))
    patch_text = _make_patch(rel_path, before, after)

    event = asyncio.Event()
    result = TurnResult(
        turn_id="turn-1",
        status=None,
        agent_messages=[patch_text],
        errors=[],
        raw_events=[],
    )
    client = FakeClient(result, event)
    supervisor = FakeSupervisor(client)
    service = DocChatService(engine, app_server_supervisor=supervisor)

    request = DocChatRequest(message="interrupt me", targets=("todo",))
    async with service.doc_lock():
        task = asyncio.create_task(service.execute(request))
        await service.interrupt("todo")
        response = await task

    assert response["status"] == "interrupted"
    assert not service._drafts_path.exists()
    assert client.interrupt_calls


@pytest.mark.anyio
async def test_doc_chat_interrupt_ignores_external_lock(repo: Path) -> None:
    engine = Engine(repo)
    service = DocChatService(engine)
    lock = FileLock(service._doc_lock_path())
    lock.acquire(blocking=False)
    try:
        response = await service.interrupt("todo")
    finally:
        lock.release()

    assert response["status"] == "interrupted"
    with service._active_turn_lock:
        assert not service._pending_interrupt


def test_chat_validation_failure_does_not_write(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    existing = (repo / ".codex-autorunner" / "TODO.md").read_text(encoding="utf-8")

    async def fake_execute(self, request: DocChatRequest) -> dict:  # type: ignore[override]
        target_path = self.engine.config.doc_path("todo")
        before = target_path.read_text(encoding="utf-8")
        after = "bad content\n"
        rel_path = str(target_path.relative_to(self.engine.repo_root))
        patch_text = _make_patch(rel_path, before, after)
        draft = DocChatDraftState(
            content=after,
            patch=patch_text,
            agent_message="nope",
            created_at="2024-01-01T00:00:00Z",
            base_hash=self._hash_content(before),
        )
        self._save_drafts({"todo": draft})
        return {
            "status": "ok",
            "agent_message": "nope",
            "updated": ["todo"],
            "drafts": {"todo": draft.to_dict()},
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


def test_chat_apply_rejects_stale_draft(repo: Path) -> None:
    engine = Engine(repo)
    service = DocChatService(engine)
    target_path = engine.config.doc_path("todo")
    before = target_path.read_text(encoding="utf-8")
    after = "- [ ] draft update\n"
    rel_path = str(target_path.relative_to(engine.repo_root))
    patch_text = _make_patch(rel_path, before, after)
    draft = DocChatDraftState(
        content=after,
        patch=patch_text,
        agent_message="drafted",
        created_at="2024-01-01T00:00:00Z",
        base_hash=service._hash_content(before),
    )
    service._save_drafts({"todo": draft})
    target_path.write_text(before + "\nextra edit\n", encoding="utf-8")

    client = _client(repo)
    res = client.post("/api/docs/todo/chat/apply")
    assert res.status_code == 409, res.text

    pending = client.get("/api/docs/todo/chat/pending")
    assert pending.status_code == 200, pending.text


def test_doc_chat_preserves_base_hash_on_draft_updates(repo: Path) -> None:
    engine = Engine(repo)
    service = DocChatService(engine)
    target_path = engine.config.doc_path("todo")
    before = target_path.read_text(encoding="utf-8")
    rel_path = str(target_path.relative_to(engine.repo_root))
    draft_content = before + "- [ ] draft task\n"
    base_hash = service._hash_content(before)
    draft = DocChatDraftState(
        content=draft_content,
        patch=_make_patch(rel_path, before, draft_content),
        agent_message="drafted",
        created_at="2024-01-01T00:00:00Z",
        base_hash=base_hash,
    )
    drafts = {"todo": draft}
    docs = service._doc_bases(drafts)
    updated_content = draft_content + "- [ ] followup\n"
    patch_text = _make_patch(rel_path, draft_content, updated_content)
    updated, _kinds, _payloads = service._apply_patch_to_drafts(
        patch_text_raw=patch_text,
        drafts=drafts,
        docs=docs,
        agent_message="refined",
        allowed_kinds=("todo",),
    )

    assert updated["todo"].base_hash == base_hash


def test_doc_chat_apply_allows_stacked_drafts(repo: Path) -> None:
    engine = Engine(repo)
    service = DocChatService(engine)
    target_path = engine.config.doc_path("todo")
    before = target_path.read_text(encoding="utf-8")
    rel_path = str(target_path.relative_to(engine.repo_root))
    first_after = before + "- [ ] draft task\n"
    first_patch = _make_patch(rel_path, before, first_after)
    draft = DocChatDraftState(
        content=first_after,
        patch=first_patch,
        agent_message="drafted",
        created_at="2024-01-01T00:00:00Z",
        base_hash=service._hash_content(before),
    )
    drafts = {"todo": draft}
    service._save_drafts(drafts)

    docs = service._doc_bases(drafts)
    second_after = first_after + "- [ ] followup\n"
    second_patch = _make_patch(rel_path, first_after, second_after)
    updated, _kinds, _payloads = service._apply_patch_to_drafts(
        patch_text_raw=second_patch,
        drafts=drafts,
        docs=docs,
        agent_message="refined",
        allowed_kinds=("todo",),
    )
    service._save_drafts(updated)

    applied = service.apply_saved_patch("todo")
    assert "- [ ] followup" in applied


def test_doc_chat_apply_detects_mid_run_edits(repo: Path) -> None:
    engine = Engine(repo)
    service = DocChatService(engine)
    target_path = engine.config.doc_path("todo")
    before = target_path.read_text(encoding="utf-8")
    rel_path = str(target_path.relative_to(engine.repo_root))
    after = before + "- [ ] new task\n"
    patch_text = _make_patch(rel_path, before, after)
    docs = service._doc_bases({})

    target_path.write_text(before + "- [ ] unrelated edit\n", encoding="utf-8")

    updated, _kinds, _payloads = service._apply_patch_to_drafts(
        patch_text_raw=patch_text,
        drafts={},
        docs=docs,
        agent_message="drafted",
        allowed_kinds=("todo",),
    )
    service._save_drafts(updated)

    with pytest.raises(DocChatConflictError):
        service.apply_saved_patch("todo")


def test_prompt_includes_all_docs_and_recent_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    engine = Engine(repo)
    service = DocChatService(engine)
    monkeypatch.setattr(service, "_recent_run_summary", lambda: "recent notes")
    request = DocChatRequest(message="summarize", context_doc="progress")
    docs = service._doc_bases(service._load_drafts())
    prompt = service._build_app_server_prompt(request, docs)
    assert (
        "<USER_VIEWING>\nThe user is currently looking at PROGRESS.\n</USER_VIEWING>"
        in prompt
    )
    assert "User request:\nsummarize" in prompt
    assert "<RECENT_RUN_SUMMARY>\nrecent notes\n</RECENT_RUN_SUMMARY>" in prompt
    assert "TODO: .codex-autorunner/TODO.md" in prompt
    assert "OPINIONS: .codex-autorunner/OPINIONS.md" in prompt
    assert "SPEC: .codex-autorunner/SPEC.md" in prompt
    assert ".codex-autorunner/PROGRESS.md" in prompt
    assert "PROGRESS" in prompt and "progress body" in prompt
    assert "<DOC_BASES>" in prompt and "</DOC_BASES>" in prompt
