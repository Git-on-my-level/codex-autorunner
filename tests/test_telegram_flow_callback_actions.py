from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.chat.callbacks import LogicalCallback
from codex_autorunner.integrations.telegram.adapter import (
    ApprovalCallback,
    FlowCallback,
    FlowRunCallback,
    TelegramCallbackQuery,
    build_approval_keyboard,
    encode_agent_callback,
    encode_approval_callback,
    encode_bind_callback,
    encode_cancel_callback,
    encode_compact_callback,
    encode_effort_callback,
    encode_flow_callback,
    encode_flow_run_callback,
    encode_model_callback,
    encode_page_callback,
    encode_question_cancel_callback,
    encode_question_custom_callback,
    encode_question_done_callback,
    encode_question_option_callback,
    encode_resume_callback,
    encode_review_commit_callback,
    encode_update_callback,
    encode_update_confirm_callback,
    parse_callback_data,
)
from codex_autorunner.integrations.telegram.chat_callbacks import TelegramCallbackCodec
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands
from codex_autorunner.integrations.telegram.handlers.selections import (
    TelegramSelectionHandlers,
)
from codex_autorunner.integrations.telegram.helpers import _format_approval_prompt


class _TopicStoreStub:
    def __init__(self, repo_root: Path | None) -> None:
        self._record = (
            SimpleNamespace(workspace_path=str(repo_root)) if repo_root else None
        )

    async def get_topic(self, _key: str) -> SimpleNamespace | None:
        return self._record


class _ControllerStub:
    def __init__(self) -> None:
        self.resume_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def resume_flow(self, run_id: str, *, force: bool = False) -> SimpleNamespace:
        self.resume_calls.append(run_id)
        return SimpleNamespace(id=run_id)

    async def stop_flow(self, run_id: str) -> SimpleNamespace:
        self.stop_calls.append(run_id)
        return SimpleNamespace(id=run_id, status=FlowRunStatus.STOPPED)


class _FlowCallbackHandler(FlowCommands):
    def __init__(
        self,
        repo_root: Path | None,
        *,
        resolved_repo_root: Path | None = None,
    ) -> None:
        self._repo_root = resolved_repo_root
        self._store = _TopicStoreStub(repo_root)
        self.answers: list[str] = []
        self.rendered: list[tuple[Path, str | None, str | None]] = []
        self.stopped_workers: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.answers.append(text)

    async def _render_flow_status_callback(
        self,
        _callback: TelegramCallbackQuery,
        repo_root: Path,
        run_id_raw: str | None,
        *,
        repo_id: str | None = None,
    ) -> None:
        self.rendered.append((repo_root, run_id_raw, repo_id))

    def _stop_flow_worker(self, _repo_root: Path, run_id: str) -> None:
        self.stopped_workers.append(run_id)

    def _resolve_workspace(self, arg: str) -> tuple[str, str] | None:
        if self._repo_root and arg in {
            "car-wt-3",
            "codex-autorunner--architecture-boundary-refactors",
        }:
            return str(self._repo_root), arg
        return None


def _init_store(repo_root: Path) -> FlowStore:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    return store


def _create_run(store: FlowStore, run_id: str, status: FlowRunStatus) -> None:
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, status)


def _callback() -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        update_id=1,
        callback_id="cb1",
        from_user_id=2,
        data=None,
        message_id=3,
        chat_id=10,
        thread_id=11,
    )


@pytest.mark.anyio
async def test_flow_callback_resume_latest_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(store, run_id, FlowRunStatus.PAUSED)
    store.close()

    controller = _ControllerStub()
    spawned: list[str] = []
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(
        flows_module, "_spawn_flow_worker", lambda _root, run: spawned.append(run)
    )

    handler = _FlowCallbackHandler(tmp_path)
    await handler._handle_flow_callback(_callback(), FlowCallback(action="resume"))

    assert controller.resume_calls == [run_id]
    assert spawned == [run_id]
    assert "Resumed." in handler.answers
    assert handler.rendered


@pytest.mark.anyio
async def test_flow_callback_stop_latest_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(store, run_id, FlowRunStatus.RUNNING)
    store.close()

    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )

    handler = _FlowCallbackHandler(tmp_path)
    await handler._handle_flow_callback(_callback(), FlowCallback(action="stop"))

    assert controller.stop_calls == [run_id]
    assert handler.stopped_workers == [run_id]
    assert "Stopped." in handler.answers
    assert handler.rendered


@pytest.mark.anyio
async def test_flow_callback_recover_latest_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _init_store(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(store, run_id, FlowRunStatus.RUNNING)
    store.close()

    recovered: list[str] = []

    def _reconcile(_repo_root: Path, record: object, _store: FlowStore):
        recovered.append(record.id)
        return record, True, False

    monkeypatch.setattr(flows_module, "reconcile_flow_run", _reconcile)

    handler = _FlowCallbackHandler(tmp_path)
    await handler._handle_flow_callback(_callback(), FlowCallback(action="recover"))

    assert recovered == [run_id]
    assert "Recovered." in handler.answers
    assert handler.rendered


@pytest.mark.anyio
async def test_flow_callback_refresh_uses_repo_id_when_topic_unbound(
    tmp_path: Path,
) -> None:
    handler = _FlowCallbackHandler(None, resolved_repo_root=tmp_path)
    await handler._handle_flow_callback(
        _callback(),
        FlowCallback(action="refresh", run_id="run-1", repo_id="car-wt-3"),
    )

    assert "Refreshing..." in handler.answers
    assert handler.rendered == [(tmp_path, "run-1", "car-wt-3")]


@pytest.mark.anyio
async def test_flow_callback_refresh_uses_cached_repo_context_when_repo_id_omitted(
    tmp_path: Path,
) -> None:
    handler = _FlowCallbackHandler(None, resolved_repo_root=tmp_path)
    repo_id = "codex-autorunner--architecture-boundary-refactors"
    handler._flow_repo_context = {"run-1": repo_id}

    await handler._handle_flow_callback(
        _callback(),
        FlowCallback(action="refresh", run_id="run-1"),
    )

    assert "Refreshing..." in handler.answers
    assert handler.rendered == [(tmp_path, "run-1", repo_id)]


class _SelectionFlowRunHandler(TelegramSelectionHandlers):
    def __init__(self) -> None:
        self._flow_run_options = {
            "topic": SimpleNamespace(
                items=[("run-1", "Run 1")], repo_id="car-wt-3", page=0
            )
        }
        self.forwarded: list[FlowCallback] = []
        self.answers: list[str] = []

    async def _answer_callback(
        self, _callback: TelegramCallbackQuery, text: str
    ) -> None:
        self.answers.append(text)

    async def _handle_flow_callback(
        self, _callback: TelegramCallbackQuery, parsed: FlowCallback
    ) -> None:
        self.forwarded.append(parsed)


@pytest.mark.anyio
async def test_flow_run_callback_forwards_repo_id() -> None:
    handler = _SelectionFlowRunHandler()
    await handler._handle_flow_run_callback(
        "topic", _callback(), FlowRunCallback(run_id="run-1")
    )

    assert not handler.answers
    assert handler.forwarded == [
        FlowCallback(action="status", run_id="run-1", repo_id="car-wt-3")
    ]


@pytest.mark.parametrize(
    ("logical", "expected_payload"),
    [
        (
            LogicalCallback(
                callback_id="approval",
                payload={"decision": "accept", "request_id": "req-1"},
            ),
            encode_approval_callback("accept", "req-1"),
        ),
        (
            LogicalCallback(
                callback_id="question_option",
                payload={"request_id": "req-2", "question_index": 1, "option_index": 3},
            ),
            encode_question_option_callback("req-2", 1, 3),
        ),
        (
            LogicalCallback(
                callback_id="question_done",
                payload={"request_id": "req-3"},
            ),
            encode_question_done_callback("req-3"),
        ),
        (
            LogicalCallback(
                callback_id="question_custom",
                payload={"request_id": "req-4"},
            ),
            encode_question_custom_callback("req-4"),
        ),
        (
            LogicalCallback(
                callback_id="question_cancel",
                payload={"request_id": "req-5"},
            ),
            encode_question_cancel_callback("req-5"),
        ),
        (
            LogicalCallback(callback_id="resume", payload={"thread_id": "thread-1"}),
            encode_resume_callback("thread-1"),
        ),
        (
            LogicalCallback(callback_id="bind", payload={"repo_id": "repo-1"}),
            encode_bind_callback("repo-1"),
        ),
        (
            LogicalCallback(callback_id="agent", payload={"agent": "gpt-5-codex"}),
            encode_agent_callback("gpt-5-codex"),
        ),
        (
            LogicalCallback(callback_id="model", payload={"model_id": "o3"}),
            encode_model_callback("o3"),
        ),
        (
            LogicalCallback(callback_id="effort", payload={"effort": "high"}),
            encode_effort_callback("high"),
        ),
        (
            LogicalCallback(callback_id="update", payload={"target": "telegram"}),
            encode_update_callback("telegram"),
        ),
        (
            LogicalCallback(
                callback_id="update_confirm",
                payload={"decision": "confirm"},
            ),
            encode_update_confirm_callback("confirm"),
        ),
        (
            LogicalCallback(
                callback_id="review_commit",
                payload={"sha": "abc1234"},
            ),
            encode_review_commit_callback("abc1234"),
        ),
        (
            LogicalCallback(callback_id="cancel", payload={"kind": "interrupt"}),
            encode_cancel_callback("interrupt"),
        ),
        (
            LogicalCallback(callback_id="compact", payload={"action": "apply"}),
            encode_compact_callback("apply"),
        ),
        (
            LogicalCallback(callback_id="page", payload={"kind": "repo", "page": 2}),
            encode_page_callback("repo", 2),
        ),
        (
            LogicalCallback(
                callback_id="flow",
                payload={"action": "status", "run_id": "run-9", "repo_id": "car-wt-9"},
            ),
            encode_flow_callback("status", "run-9", repo_id="car-wt-9"),
        ),
        (
            LogicalCallback(callback_id="flow_run", payload={"run_id": "run-8"}),
            encode_flow_run_callback("run-8"),
        ),
    ],
)
def test_telegram_callback_codec_roundtrip(
    logical: LogicalCallback, expected_payload: str
) -> None:
    codec = TelegramCallbackCodec()

    encoded = codec.encode(logical)
    decoded = codec.decode(encoded)

    assert encoded == expected_payload
    assert decoded == logical


@pytest.mark.parametrize(
    "legacy_payload",
    [
        "appr:accept:req-1",
        "qopt:3:1:req-2",
        "qdone:req-3",
        "qcustom:req-4",
        "qcancel:req-5",
        "resume:thread-1",
        "bind:repo-1",
        "agent:gpt-5-codex",
        "model:o3",
        "effort:high",
        "update:telegram",
        "update_confirm:confirm",
        "review_commit:abc1234",
        "cancel:interrupt",
        "compact:apply",
        "page:repo:2",
        "flow:status",
        "flow:resume:run-9",
        "flow:status:run-9:car-wt-9",
        "flow_run:run-8",
    ],
)
def test_telegram_callback_codec_decodes_existing_wire_payloads(
    legacy_payload: str,
) -> None:
    codec = TelegramCallbackCodec()

    decoded = codec.decode(legacy_payload)

    assert decoded is not None


def test_approval_prompt_and_keyboard_match_golden_fixtures() -> None:
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "telegram"
    expected_prompt = (
        fixture_dir / "approval_prompt_command_execution.txt"
    ).read_text()
    expected_keyboard = json.loads((fixture_dir / "approval_keyboard.json").read_text())
    message = {
        "method": "item/commandExecution/requestApproval",
        "params": {
            "reason": "Need to run diagnostics",
            "command": "make test",
        },
    }

    rendered_prompt = _format_approval_prompt(message)
    keyboard = build_approval_keyboard("req-approval-1", include_session=False)

    assert rendered_prompt == expected_prompt.strip()
    assert keyboard == expected_keyboard
    first_row = keyboard["inline_keyboard"][0]
    assert parse_callback_data(first_row[0]["callback_data"]) == ApprovalCallback(
        decision="accept",
        request_id="req-approval-1",
    )
    assert parse_callback_data(first_row[1]["callback_data"]) == ApprovalCallback(
        decision="decline",
        request_id="req-approval-1",
    )
