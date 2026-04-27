import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest

from codex_autorunner.core.pma_context import default_pma_prompt_state_path
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerResponseError,
)
from codex_autorunner.integrations.app_server.threads import (
    PMA_OPENCODE_KEY,
    AppServerThreadRegistry,
)
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    build_command_specs,
)
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    WorkspaceCommands,
)
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.handlers.selections import SelectionState
from codex_autorunner.integrations.telegram.helpers import _format_help_text
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
)
from codex_autorunner.integrations.telegram.state_types import ThreadSummary
from tests.telegram_pma_managed_thread_support import (
    _InProcessHubControlPlaneClient,
    _PMAClientStub,
    _PMAHandler,
    _PMARouterStub,
)


class _PMAWorkspaceRouter:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record


def _write_prompt_state_sessions(hub_root: Path, *keys: str) -> Path:
    state_path = default_pma_prompt_state_path(hub_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-20T00:00:00Z",
                "sessions": {key: {"version": 1} for key in keys},
            }
        ),
        encoding="utf-8",
    )
    return state_path


class _PMAWorkspaceHandler(WorkspaceCommands):
    def __init__(
        self,
        record: TelegramTopicRecord,
        registry: AppServerThreadRegistry,
        *,
        hub_root: Optional[Path] = None,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(require_topics=False)
        self._router = _PMAWorkspaceRouter(record)
        self._hub_thread_registry = registry
        self._hub_root = hub_root
        self._sent: list[str] = []
        self._record = record

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        reply_markup: Optional[object] = None,
    ) -> None:
        self._sent.append(text)

    def _pma_registry_key(
        self, record: "TelegramTopicRecord", message: Optional[TelegramMessage] = None
    ) -> str:
        from codex_autorunner.integrations.app_server.threads import pma_base_key

        agent = self._effective_agent(record)
        base_key = pma_base_key(agent)

        require_topics = getattr(self._config, "require_topics", False)
        if require_topics and message is not None:
            topic_key = f"{message.chat_id}:{message.thread_id or 'root'}"
            return f"{base_key}.{topic_key}"
        return base_key

    def _effective_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        if record and record.agent:
            return record.agent
        return "codex"

    def _effective_runtime_agent(self, _record: Optional["TelegramTopicRecord"]) -> str:
        return "codex"

    def _effective_agent_profile(
        self, _record: Optional["TelegramTopicRecord"]
    ) -> Optional[str]:
        return None


class _NewtRouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record
        self.update_snapshots: list[dict[str, object]] = []

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> TelegramTopicRecord:
        if callable(apply):
            apply(self._record)
        self.update_snapshots.append(self._record.to_dict())
        return self._record


class _NewtClientStub:
    runtime_instance_id = "runtime-test-1"

    async def start(self) -> None:
        return None

    async def thread_start(self, workspace_path: str, *, agent: str) -> dict[str, str]:
        _ = agent
        return {"thread_id": "new-thread-id", "workspace_path": workspace_path}


class _NewtSupervisorStub:
    def __init__(self) -> None:
        self.client = _NewtClientStub()

    async def get_client(self, _workspace_root: Path) -> _NewtClientStub:
        return self.client


class _NewtEventsStub:
    async def stream(self, conversation_id: str, turn_id: str):
        _ = conversation_id, turn_id
        if False:
            yield ""


class _NewtHandler(WorkspaceCommands):
    def __init__(self, record: TelegramTopicRecord, *, hub_root: Path) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace()
        self._router = _NewtRouterStub(record)
        self._hub_root = hub_root
        self._sent: list[str] = []
        self.app_server_supervisor = _NewtSupervisorStub()
        self.app_server_events = _NewtEventsStub()
        self.__hub_client: Optional[_InProcessHubControlPlaneClient] = None
        self._hub_handshake_compatibility = SimpleNamespace(compatible=True)

    @property
    def _hub_client(self) -> _InProcessHubControlPlaneClient:
        if self.__hub_client is None:
            self.__hub_client = _InProcessHubControlPlaneClient(self._hub_root)
        return self.__hub_client

    @_hub_client.setter
    def _hub_client(self, value: Optional[_InProcessHubControlPlaneClient]) -> None:
        self.__hub_client = value

    def _effective_agent(self, _record: Optional["TelegramTopicRecord"]) -> str:
        return "codex"

    def _effective_runtime_agent(self, _record: Optional["TelegramTopicRecord"]) -> str:
        return "codex"

    def _effective_agent_profile(
        self, _record: Optional["TelegramTopicRecord"]
    ) -> Optional[str]:
        return None

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        reply_markup: Optional[object] = None,
    ) -> None:
        self._sent.append(text)

    async def _client_for_workspace(self, _workspace_path: str) -> _NewtClientStub:
        return _NewtClientStub()

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not workspace_path:
            return None
        return Path(workspace_path).expanduser().resolve()

    def _workspace_id_for_path(self, _workspace_path: str) -> Optional[str]:
        return None


@pytest.mark.anyio
async def test_archive_uses_shared_fresh_start_and_resets_topic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(
        workspace_path=str(workspace_root),
        active_thread_id="thread-active",
        thread_ids=["thread-active", "thread-old"],
        rollout_path=str(workspace_root / "rollout.md"),
        pending_compact_seed="seed",
        pending_compact_seed_thread_id="thread-old",
    )
    record.thread_summaries = {
        "thread-active": ThreadSummary(
            user_preview="active",
            last_used_at="2026-03-19T00:00:00Z",
        )
    }
    handler = _NewtHandler(record, hub_root=hub_root)

    monkeypatch.setattr(
        "codex_autorunner.core.archive.resolve_workspace_archive_target",
        lambda workspace_root, **_kwargs: SimpleNamespace(
            base_repo_root=hub_root,
            base_repo_id="base",
            workspace_repo_id="repo",
            worktree_of="base",
            source_path="repo",
        ),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.archive.archive_workspace_for_fresh_start",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(
                snapshot_id=None,
                archived_paths=(),
                archived_thread_ids=("managed-thread-1",),
            )
        ),
    )
    reset_calls: list[dict[str, object]] = []

    async def _fake_reset_telegram_thread_binding(
        *_args: object, **kwargs: object
    ) -> tuple[bool, str]:
        reset_calls.append(dict(kwargs))
        return True, "thread-fresh"

    monkeypatch.setattr(
        execution_commands_module,
        "_reset_telegram_thread_binding",
        _fake_reset_telegram_thread_binding,
    )

    await handler._handle_archive(
        TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=10,
            thread_id=20,
            from_user_id=30,
            text="/archive",
            date=None,
            is_topic_message=True,
        )
    )

    assert calls
    assert calls[0]["hub_root"] == hub_root
    assert calls[0]["worktree_repo_id"] == "repo"
    assert reset_calls
    assert reset_calls[0]["mode"] == "repo"
    assert record.active_thread_id is None
    assert record.thread_ids == []
    assert record.thread_summaries == {}
    assert record.rollout_path is None
    assert record.pending_compact_seed is None
    assert record.pending_compact_seed_thread_id is None
    assert handler._sent
    assert "Workspace CAR state was already clean." in handler._sent[-1]
    assert "Archived 1 managed thread." in handler._sent[-1]


@pytest.mark.anyio
async def test_archive_without_hub_root_does_not_substitute_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(workspace_path=str(workspace_root))
    handler = _NewtHandler(record, hub_root=tmp_path / "unused")
    handler._hub_root = None

    monkeypatch.setattr(
        "codex_autorunner.core.archive.resolve_workspace_archive_target",
        lambda workspace_root, **_kwargs: SimpleNamespace(
            base_repo_root=workspace_root,
            base_repo_id="base",
            workspace_repo_id="repo",
            worktree_of="base",
            source_path="repo",
        ),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.archive.archive_workspace_for_fresh_start",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(
                snapshot_id="snap-1",
                archived_paths=("tickets",),
                archived_thread_ids=(),
            )
        ),
    )

    await handler._handle_archive(
        TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=10,
            thread_id=20,
            from_user_id=30,
            text="/archive",
            date=None,
            is_topic_message=True,
        )
    )

    assert calls
    assert calls[0]["hub_root"] is None


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_archives_after_lost_backend_recovery(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str]] = []

    class _FakeThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id))
            return SimpleNamespace(recovered_lost_backend=True)

        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id))
            assert workspace_root == workspace
            return "runtime-test-1"

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"])))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="repo"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
            ),
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-2",
            mode="repo",
            pma_enabled=False,
            replace_existing=True,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-2"
    assert calls == [
        ("stop", "thread-1"),
        ("archive", "thread-1"),
        ("resolve", "codex"),
        ("create", "codex"),
        ("bind", "thread-2"),
    ]


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_ignores_backend_id_in_pma_mode(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            raise AssertionError(
                "runtime instance lookup should be skipped in PMA mode"
            )

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            calls.append((thread_target_id, dict(kwargs)))
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="codex",
                workspace_root=str(workspace),
            )

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", dict(kwargs)))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"])))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
            ),
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-2",
            mode="pma",
            pma_enabled=True,
            replace_existing=False,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-1"
    assert calls == [
        (
            "thread-1",
            {
                "backend_runtime_instance_id": None,
            },
        ),
        ("bind", "thread-1"),
    ]


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_allows_missing_runtime_instance(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            assert workspace_root == workspace
            return None

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            metadata = kwargs.get("metadata") or {}
            calls.append(
                (
                    "create",
                    agent,
                    metadata.get("backend_runtime_instance_id"),
                )
            )
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            None,
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-2",
            mode="repo",
            pma_enabled=False,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-2"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("create", "opencode", None),
        ("bind", "thread-2", "repo"),
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_allows_missing_runtime_instance(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            assert workspace_root == workspace
            return None

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            metadata = kwargs.get("metadata") or {}
            calls.append(
                (
                    "create",
                    agent,
                    metadata.get("backend_runtime_instance_id"),
                )
            )
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-3",
                agent_id=agent,
                workspace_root=str(workspace_root),
                backend_thread_id=None,
                lifecycle_status="active",
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            None,
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._resolve_telegram_managed_thread(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            mode="repo",
            pma_enabled=False,
            backend_thread_id="backend-3",
            allow_new_thread=True,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-3"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("create", "opencode", None),
        ("bind", "thread-3", "repo"),
    ]


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_rejects_rebind_when_runtime_missing(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    current_thread = SimpleNamespace(
        thread_target_id="thread-existing",
        agent_id="opencode",
        workspace_root=str(workspace),
        backend_thread_id="backend-old",
    )

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            calls.append(
                (
                    "resume",
                    thread_target_id,
                    kwargs.get("backend_thread_id"),
                )
            )
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="opencode",
                workspace_root=str(workspace),
                backend_thread_id=kwargs.get("backend_thread_id"),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            current_thread,
        ),
    )
    try:
        (
            _service,
            thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-new",
            mode="repo",
            pma_enabled=False,
        )
    finally:
        monkeypatch.undo()

    assert thread.thread_target_id == "thread-existing"
    assert thread.backend_thread_id == "backend-old"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("resume", "thread-existing", "backend-old"),
        ("bind", "thread-existing", "repo"),
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_rejects_rebind_when_runtime_missing(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    thread = SimpleNamespace(
        thread_target_id="thread-existing",
        agent_id="opencode",
        agent_profile=None,
        workspace_root=str(workspace),
        backend_thread_id="backend-old",
        lifecycle_status="active",
    )

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            thread,
        ),
    )
    try:
        (
            _service,
            resolved_thread,
        ) = await execution_commands_module._resolve_telegram_managed_thread(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            mode="repo",
            pma_enabled=False,
            backend_thread_id="backend-new",
            allow_new_thread=True,
        )
    finally:
        monkeypatch.undo()

    assert resolved_thread.thread_target_id == "thread-existing"
    assert resolved_thread.backend_thread_id == "backend-old"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("bind", "thread-existing", "repo"),
    ]


@pytest.mark.anyio
async def test_resolve_telegram_managed_thread_keeps_requested_backend_thread_id_for_new_thread(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace-new").resolve()
    other_workspace = (tmp_path / "workspace-existing").resolve()
    calls: list[tuple[str, str, object]] = []

    thread = SimpleNamespace(
        thread_target_id="thread-existing",
        agent_id="opencode",
        agent_profile=None,
        workspace_root=str(other_workspace),
        backend_thread_id="backend-old",
        lifecycle_status="active",
    )

    class _FakeThreadService:
        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent, kwargs.get("backend_thread_id")))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-new",
                agent_id=agent,
                agent_profile=None,
                workspace_root=str(workspace_root),
                backend_thread_id=kwargs.get("backend_thread_id"),
                lifecycle_status="active",
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            None,
            thread,
        ),
    )
    try:
        (
            _service,
            resolved_thread,
        ) = await execution_commands_module._resolve_telegram_managed_thread(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="opencode",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            mode="repo",
            pma_enabled=False,
            backend_thread_id="backend-new",
            allow_new_thread=True,
        )
    finally:
        monkeypatch.undo()

    assert resolved_thread.thread_target_id == "thread-new"
    assert resolved_thread.backend_thread_id == "backend-new"
    assert calls == [
        ("resolve", "opencode", str(workspace)),
        ("create", "opencode", "backend-new"),
        ("bind", "thread-new", "repo"),
    ]


async def test_reset_telegram_thread_binding_archives_after_lost_backend_recovery(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str]] = []

    class _FakeThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id))
            return SimpleNamespace(recovered_lost_backend=True)

        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id))
            assert workspace_root == workspace
            return "runtime-test-1"

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"])))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="pma"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
            ),
        ),
    )
    try:
        (
            had_previous,
            new_thread_id,
        ) = await execution_commands_module._reset_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            mode="pma",
            pma_enabled=True,
        )
    finally:
        monkeypatch.undo()

    assert had_previous is True
    assert new_thread_id == "thread-2"
    assert calls == [
        ("stop", "thread-1"),
        ("archive", "thread-1"),
        ("create", "codex"),
        ("bind", "thread-2"),
    ]


@pytest.mark.anyio
async def test_resume_thread_by_id_rebinds_managed_thread_before_topic_mirror_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    record = TelegramTopicRecord(
        agent="codex",
        workspace_path=str(workspace),
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
    )
    resolve_calls: list[dict[str, Any]] = []

    class _RouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

        async def update_topic(
            self, _chat_id: int, _thread_id: Optional[int], apply
        ) -> TelegramTopicRecord:
            apply(record)
            return record

    class _StoreStub:
        async def update_topic(self, _key: str, apply) -> TelegramTopicRecord:
            apply(record)
            return record

    class _ClientStub:
        async def thread_resume(self, thread_id: str) -> dict[str, Any]:
            return {
                "thread_id": thread_id,
                "agent": "codex",
                "cwd": str(workspace),
                "path": str(workspace),
                "thread": {
                    "id": thread_id,
                    "path": str(workspace),
                    "agent": "codex",
                },
            }

    class _ResumeHandler(WorkspaceCommands):
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = _RouterStub()
            self._store = _StoreStub()
            self._resume_options: dict[str, SelectionState] = {}
            self._config = SimpleNamespace(
                root=workspace,
                defaults=SimpleNamespace(policies_for_mode=lambda _mode: (None, None)),
            )
            self._managed_thread_rebound = False
            self.apply_sync_flags: list[bool] = []
            self.sent: list[str] = []

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: Optional[int]
        ) -> str:
            return "123:root"

        async def _client_for_workspace(self, _workspace_path: str) -> _ClientStub:
            return _ClientStub()

        async def _refresh_workspace_id(
            self, _key: str, _record: TelegramTopicRecord
        ) -> Optional[str]:
            return None

        async def _find_thread_conflict(
            self, _thread_id: str, *, key: str
        ) -> Optional[str]:
            _ = key
            return None

        async def _finalize_selection(
            self, _key: str, _callback: object, text: str
        ) -> None:
            self.sent.append(text)

        async def _apply_thread_result(
            self,
            chat_id: int,
            thread_id: Optional[int],
            result: Any,
            *,
            active_thread_id: Optional[str] = None,
            overwrite_defaults: bool = False,
            sync_binding: bool = True,
        ) -> TelegramTopicRecord:
            _ = chat_id, thread_id, result, overwrite_defaults
            assert self._managed_thread_rebound is True
            self.apply_sync_flags.append(sync_binding)
            record.active_thread_id = active_thread_id
            if active_thread_id:
                record.thread_ids = [active_thread_id]
            return record

        def _effective_agent(self, _record: object) -> str:
            return "codex"

        def _effective_runtime_agent(self, _record: object) -> str:
            return "codex"

        def _effective_agent_profile(self, _record: object) -> Optional[str]:
            return None

    async def _fake_resolve_telegram_managed_thread(
        _handlers: Any, **kwargs: Any
    ) -> tuple[Any, Any]:
        resolve_calls.append(kwargs)
        handler._managed_thread_rebound = True
        return object(), SimpleNamespace(thread_target_id="managed-thread-2")

    monkeypatch.setattr(
        execution_commands_module,
        "_resolve_telegram_managed_thread",
        _fake_resolve_telegram_managed_thread,
    )

    handler = _ResumeHandler()
    await handler._resume_thread_by_id("123:root", "backend-thread-2")

    assert resolve_calls == [
        {
            "surface_key": "123:root",
            "workspace_root": workspace,
            "agent": "codex",
            "agent_profile": None,
            "repo_id": "repo-1",
            "resource_kind": "repo",
            "resource_id": "repo-1",
            "mode": "repo",
            "pma_enabled": False,
            "backend_thread_id": "backend-thread-2",
            "allow_new_thread": True,
        }
    ]
    assert handler.apply_sync_flags == [False]
    assert record.active_thread_id == "backend-thread-2"
    assert record.thread_ids == ["backend-thread-2"]


@pytest.mark.anyio
async def test_apply_compact_summary_uses_shared_lifecycle_before_topic_mirror_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = TelegramTopicRecord(
        agent="codex",
        workspace_path=str(workspace),
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        active_thread_id="backend-old",
    )
    lifecycle_calls: list[tuple[str, Any, Any]] = []
    compact_seed_calls: list[tuple[str, str]] = []

    class _RouterStub:
        async def update_topic(
            self, _chat_id: int, _thread_id: Optional[int], apply
        ) -> TelegramTopicRecord:
            apply(record)
            return record

    class _ThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            lifecycle_calls.append(("stop", thread_target_id, None))
            return SimpleNamespace(recovered_lost_backend=False)

        def archive_thread_target(self, thread_target_id: str) -> None:
            lifecycle_calls.append(("archive", thread_target_id, None))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            lifecycle_calls.append(("create", agent, kwargs.get("backend_thread_id")))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="managed-new",
                agent_id=agent,
                workspace_root=str(workspace_root),
                backend_thread_id=kwargs.get("backend_thread_id"),
                lifecycle_status="active",
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            lifecycle_calls.append(
                ("bind", kwargs["thread_target_id"], kwargs.get("mode"))
            )

    class _FakeHubClient:
        async def update_thread_compact_seed(self, request: Any) -> None:
            compact_seed_calls.append(
                (
                    getattr(request, "thread_target_id", ""),
                    str(getattr(request, "compact_seed", "") or ""),
                )
            )

    class _CompactHandler(TelegramCommandHandlers):
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = _RouterStub()
            self._hub_client = _FakeHubClient()
            self._config = SimpleNamespace(
                root=tmp_path,
                defaults=SimpleNamespace(policies_for_mode=lambda _mode: (None, None)),
            )
            self.sync_binding_flags: list[bool] = []
            self._spawn_task = lambda coro: None

        def _resolve_workspace_path(
            self, _record: TelegramTopicRecord, allow_pma: bool = False
        ) -> tuple[Optional[str], Optional[str]]:
            _ = allow_pma
            return str(workspace), None

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: Optional[int]
        ) -> str:
            return "123:root"

        def _effective_runtime_agent(self, _record: object) -> str:
            return "codex"

        def _effective_agent_profile(self, _record: object) -> Optional[str]:
            return None

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _ThreadService(),
            SimpleNamespace(thread_target_id="managed-old", mode="repo"),
            SimpleNamespace(
                thread_target_id="managed-old",
                agent_id="codex",
                workspace_root=str(workspace),
                lifecycle_status="active",
            ),
        ),
    )

    handler = _CompactHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="/compact",
        date=None,
        is_topic_message=False,
    )

    success, error = await handler._apply_compact_summary(
        message,
        record,
        "summary text",
    )

    assert success is True
    assert error is None
    assert lifecycle_calls == [
        ("stop", "managed-old", None),
        ("archive", "managed-old", None),
        ("create", "codex", None),
        ("bind", "managed-new", "repo"),
    ]
    assert compact_seed_calls == [("managed-new", "summary text")]
    assert handler.sync_binding_flags == []
    assert record.active_thread_id is None
    assert record.pending_compact_seed_thread_id == "managed-new"
    assert "summary text" in (record.pending_compact_seed or "")


@pytest.mark.anyio
@pytest.mark.parametrize("has_managed_thread_runtime", [False, True])
async def test_apply_compact_summary_preserves_pma_mode_for_replacement_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_managed_thread_runtime: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = TelegramTopicRecord(
        agent="codex",
        workspace_path=str(workspace),
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        active_thread_id="backend-old",
        pma_enabled=True,
    )
    binding_mode_calls: list[str] = []
    replace_calls: list[tuple[str, bool]] = []
    bind_calls: list[tuple[str, bool, Optional[str]]] = []
    compact_seed_calls: list[tuple[str, str]] = []
    apply_thread_result_flags: list[bool] = []

    class _RouterStub:
        async def update_topic(
            self, _chat_id: int, _thread_id: Optional[int], apply
        ) -> TelegramTopicRecord:
            apply(record)
            return record

    class _ClientStub:
        async def thread_start(self, _workspace_path: str, **_kwargs: Any) -> Any:
            return {"id": "backend-pma-new"}

    class _FakeHubClient:
        async def update_thread_compact_seed(self, request: Any) -> None:
            compact_seed_calls.append(
                (
                    getattr(request, "thread_target_id", ""),
                    str(getattr(request, "compact_seed", "") or ""),
                )
            )

    class _CompactHandler(TelegramCommandHandlers):
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = _RouterStub()
            self._hub_client = _FakeHubClient()
            self._config = SimpleNamespace(
                root=tmp_path,
                defaults=SimpleNamespace(policies_for_mode=lambda _mode: (None, None)),
            )
            self._spawn_task = (
                (lambda coro: None) if has_managed_thread_runtime else None
            )

        def _resolve_workspace_path(
            self, _record: TelegramTopicRecord, allow_pma: bool = False
        ) -> tuple[Optional[str], Optional[str]]:
            _ = allow_pma
            return str(workspace), None

        async def _resolve_topic_key(
            self, _chat_id: int, _thread_id: Optional[int]
        ) -> str:
            return "123:root"

        async def _client_for_workspace(self, _workspace_path: str) -> Any:
            return _ClientStub()

        async def _require_thread_workspace(
            self,
            _message: TelegramMessage,
            _workspace_path: str,
            _thread: Any,
            *,
            action: str,
        ) -> bool:
            _ = action
            return True

        async def _apply_thread_result(
            self,
            _chat_id: int,
            _thread_id: Optional[int],
            _thread: Any,
            *,
            active_thread_id: Optional[str] = None,
            sync_binding: bool = True,
        ) -> TelegramTopicRecord:
            apply_thread_result_flags.append(sync_binding)
            record.active_thread_id = active_thread_id
            return record

        def _effective_runtime_agent(self, _record: object) -> str:
            return "codex"

        def _effective_agent_profile(self, _record: object) -> Optional[str]:
            return None

    async def _fake_replace_surface_thread(
        _orchestration_service: Any,
        **kwargs: Any,
    ) -> Any:
        replace_calls.append(
            (
                str(kwargs["mode"]),
                bool((kwargs.get("binding_metadata") or {}).get("pma_enabled")),
            )
        )
        return SimpleNamespace(
            replacement_thread=SimpleNamespace(thread_target_id="managed-new")
        )

    def _fake_bind_surface_thread(
        _orchestration_service: Any,
        **kwargs: Any,
    ) -> Any:
        bind_calls.append(
            (
                str(kwargs["mode"]),
                bool((kwargs.get("metadata") or {}).get("pma_enabled")),
                kwargs.get("backend_thread_id"),
            )
        )
        return SimpleNamespace(thread_target_id="managed-new")

    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            (binding_mode_calls.append(str(kwargs["mode"])) or True)
            and (
                SimpleNamespace(),
                SimpleNamespace(thread_target_id="managed-old", mode="pma"),
                SimpleNamespace(
                    thread_target_id="managed-old",
                    agent_id="codex",
                    workspace_root=str(workspace),
                    lifecycle_status="active",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.managed_thread_lifecycle.replace_surface_thread",
        _fake_replace_surface_thread,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.managed_thread_lifecycle.bind_surface_thread",
        _fake_bind_surface_thread,
    )

    handler = _CompactHandler()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="/compact",
        date=None,
        is_topic_message=False,
    )

    success, error = await handler._apply_compact_summary(
        message,
        record,
        "summary text",
    )

    assert success is True
    assert error is None
    assert binding_mode_calls == ["pma"]
    assert replace_calls == [("pma", True)]
    assert compact_seed_calls == [("managed-new", "summary text")]
    if has_managed_thread_runtime:
        assert bind_calls == []
        assert apply_thread_result_flags == []
        assert record.active_thread_id is None
        assert record.pending_compact_seed_thread_id == "managed-new"
    else:
        assert bind_calls == [("pma", True, "backend-pma-new")]
        assert apply_thread_result_flags == [False]
        assert record.active_thread_id == "backend-pma-new"
        assert record.pending_compact_seed_thread_id == "backend-pma-new"
    assert "summary text" in (record.pending_compact_seed or "")


@pytest.mark.anyio
async def test_repo_managed_thread_turn_matches_pending_compact_seed_by_managed_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_input_items: list[dict[str, Any]] | None = None

    class _Coordinator:
        async def submit_execution(
            self,
            request: Any,
            *,
            client_request_id: Optional[str],
            sandbox_policy: Optional[Any],
            begin_execution: Any = None,
        ) -> Any:
            nonlocal captured_input_items
            _ = client_request_id, sandbox_policy, begin_execution
            captured_input_items = request.input_items
            started_execution = SimpleNamespace(
                thread=SimpleNamespace(
                    thread_target_id="managed-new",
                    backend_thread_id=None,
                ),
                execution=SimpleNamespace(status="queued"),
            )
            return SimpleNamespace(started_execution=started_execution, queued=True)

    class _Handler:
        def __init__(self) -> None:
            self._logger = logging.getLogger("test")
            self._router = SimpleNamespace(update_topic=_update_topic)

        async def _prepare_turn_placeholder(
            self,
            _message: TelegramMessage,
            *,
            placeholder_id: Optional[int],
            send_placeholder: bool,
            queued: bool,
        ) -> Optional[int]:
            _ = send_placeholder, queued
            return placeholder_id

        def _effective_agent_profile(
            self, _record: TelegramTopicRecord
        ) -> Optional[str]:
            return None

        def _effective_runtime_agent(self, _record: TelegramTopicRecord) -> str:
            return "codex"

    async def _update_topic(
        _chat_id: int, _thread_id: Optional[int], apply: Any
    ) -> TelegramTopicRecord:
        apply(record)
        return record

    async def _fake_resolve_managed_thread(*_args: Any, **_kwargs: Any) -> Any:
        return object(), SimpleNamespace(
            thread_target_id="managed-new",
            backend_thread_id=None,
        )

    monkeypatch.setattr(
        execution_commands_module,
        "_resolve_telegram_managed_thread",
        _fake_resolve_managed_thread,
    )
    monkeypatch.setattr(
        execution_commands_module,
        "_build_telegram_managed_thread_coordinator",
        lambda *_args, **_kwargs: _Coordinator(),
    )
    handler = _Handler()
    record = TelegramTopicRecord(
        workspace_path=str(tmp_path / "workspace"),
        pending_compact_seed="summary seed",
        pending_compact_seed_thread_id="managed-new",
    )
    result = await execution_commands_module._run_telegram_managed_thread_turn(
        handler,
        message=TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=123,
            thread_id=456,
            from_user_id=789,
            text="hello",
            date=None,
            is_topic_message=True,
        ),
        runtime=SimpleNamespace(),
        record=record,
        topic_key="123:456",
        prompt_text="hello",
        input_items=[{"type": "text", "text": "original"}],
        send_placeholder=False,
        send_failure_response=False,
        transcript_message_id=None,
        transcript_text=None,
        placeholder_id=99,
        mode="repo",
        pma_enabled=False,
        execution_prompt="runtime prompt",
    )

    assert isinstance(result, execution_commands_module._TurnRunResult)
    assert result.response == "Queued (waiting for available worker...)"
    assert captured_input_items is not None
    assert captured_input_items[0] == {"type": "text", "text": "summary seed"}
    assert captured_input_items[1] == {"type": "text", "text": "runtime prompt"}
    assert record.pending_compact_seed is None
    assert record.pending_compact_seed_thread_id is None


@pytest.mark.anyio
async def test_sync_telegram_thread_binding_keeps_requested_backend_thread_id_for_replacement(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    calls: list[tuple[str, str, object]] = []

    class _FakeThreadService:
        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id, None))
            return SimpleNamespace(recovered_lost_backend=False)

        async def resolve_backend_runtime_instance_id(
            self, agent_id: str, workspace_root: Path
        ) -> Optional[str]:
            calls.append(("resolve", agent_id, str(workspace_root)))
            return None

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id, None))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent, kwargs.get("backend_thread_id")))
            assert workspace_root == workspace
            return SimpleNamespace(
                thread_target_id="thread-2",
                agent_id=agent,
                workspace_root=str(workspace_root),
                backend_thread_id=kwargs.get("backend_thread_id"),
            )

        def upsert_binding(self, **kwargs: Any) -> None:
            calls.append(("bind", str(kwargs["thread_target_id"]), kwargs.get("mode")))

    handlers = SimpleNamespace(_logger=logging.getLogger("test"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        execution_commands_module,
        "_get_telegram_thread_binding",
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="repo"),
            SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(workspace),
                backend_thread_id="backend-old",
            ),
        ),
    )
    try:
        (
            _service,
            current_thread,
        ) = await execution_commands_module._sync_telegram_thread_binding(
            handlers,
            surface_key="topic-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-new",
            mode="repo",
            pma_enabled=False,
            replace_existing=True,
        )
    finally:
        monkeypatch.undo()

    assert current_thread.thread_target_id == "thread-2"
    assert current_thread.backend_thread_id == "backend-new"
    assert calls == [
        ("stop", "thread-1", None),
        ("archive", "thread-1", None),
        ("resolve", "codex", str(workspace)),
        ("create", "codex", "backend-new"),
        ("bind", "thread-2", "repo"),
    ]


@pytest.mark.anyio
async def test_pma_new_resets_session(tmp_path: Path) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    registry.set_thread_id(PMA_OPENCODE_KEY, "old-thread")
    state_path = _write_prompt_state_sessions(tmp_path, PMA_OPENCODE_KEY)
    record = TelegramTopicRecord(
        pma_enabled=True, workspace_path=None, agent="opencode"
    )
    handler = _PMAWorkspaceHandler(record, registry, hub_root=tmp_path)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/new",
        date=None,
        is_topic_message=True,
    )
    await handler._handle_new(message)
    assert registry.get_thread_id(PMA_OPENCODE_KEY) is None
    sessions = json.loads(state_path.read_text(encoding="utf-8")).get("sessions", {})
    assert PMA_OPENCODE_KEY not in sessions
    expected = "Started a fresh PMA session for `opencode` (new thread ready)."
    assert handler._sent[-1] == expected


@pytest.mark.anyio
async def test_pma_new_resets_managed_binding_when_runtime_threads_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")
    handler = _PMAWorkspaceHandler(record, registry, hub_root=tmp_path)
    handler._config = SimpleNamespace(require_topics=False, root=tmp_path)
    handler._spawn_task = lambda coro: None
    calls: list[dict[str, object]] = []

    async def _fake_reset_telegram_thread_binding(
        _handlers: Any, **kwargs: Any
    ) -> tuple[bool, str]:
        calls.append(kwargs)
        return True, "thread-2"

    monkeypatch.setattr(
        execution_commands_module,
        "_reset_telegram_thread_binding",
        _fake_reset_telegram_thread_binding,
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/new",
        date=None,
        is_topic_message=True,
    )
    await handler._handle_new(message)

    assert calls
    assert calls[-1]["surface_key"] == "-2002:333"
    assert calls[-1]["mode"] == "pma"
    assert calls[-1]["pma_enabled"] is True
    expected = "Started a fresh PMA session for `codex` (new thread ready)."
    assert handler._sent[-1] == expected


@pytest.mark.anyio
async def test_pma_new_resets_scoped_key_when_require_topics_enabled(
    tmp_path: Path,
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_topic_scoped_key

    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    scoped_key = pma_topic_scoped_key(
        agent="opencode",
        chat_id=-2002,
        thread_id=333,
        topic_key_fn=lambda c, t: f"{c}:{t or 'root'}",
    )
    registry.set_thread_id(scoped_key, "old-scoped-thread")
    state_path = _write_prompt_state_sessions(tmp_path, scoped_key)

    record = TelegramTopicRecord(
        pma_enabled=True, workspace_path=None, agent="opencode"
    )
    handler = _PMAWorkspaceHandlerWithScopedKey(
        record, registry, hub_root=tmp_path, require_topics=True
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/new",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_new(message)

    assert registry.get_thread_id(scoped_key) is None
    sessions = json.loads(state_path.read_text(encoding="utf-8")).get("sessions", {})
    assert scoped_key not in sessions
    assert (
        handler._sent
        and handler._sent[-1]
        == "Started a fresh PMA session for `opencode` (new thread ready)."
    )


@pytest.mark.anyio
async def test_pma_reset_resets_scoped_key_when_require_topics_enabled(
    tmp_path: Path,
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_topic_scoped_key

    registry = AppServerThreadRegistry(tmp_path / "threads.json")
    registry.reset_all()
    scoped_key = pma_topic_scoped_key(
        agent="codex",
        chat_id=-1001,
        thread_id=42,
        topic_key_fn=lambda c, t: f"{c}:{t or 'root'}",
    )
    registry.set_thread_id(scoped_key, "old-scoped-thread")
    state_path = _write_prompt_state_sessions(tmp_path, scoped_key)

    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")
    handler = _PMAWorkspaceHandlerWithScopedKey(
        record, registry, hub_root=tmp_path, require_topics=True
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-1001,
        thread_id=42,
        from_user_id=99,
        text="/reset",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_reset(message)

    assert registry.get_thread_id(scoped_key) is None
    sessions = json.loads(state_path.read_text(encoding="utf-8")).get("sessions", {})
    assert scoped_key not in sessions
    assert (
        handler._sent
        and handler._sent[-1] == "Reset PMA thread state (fresh state) for `codex`."
    )


class _PMAWorkspaceHandlerWithScopedKey(WorkspaceCommands):
    def __init__(
        self,
        record: TelegramTopicRecord,
        registry: AppServerThreadRegistry,
        *,
        hub_root: Optional[Path] = None,
        require_topics: bool = False,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._config = SimpleNamespace(require_topics=require_topics)
        self._router = _PMAWorkspaceRouter(record)
        self._hub_thread_registry = registry
        self._hub_root = hub_root
        self._sent: list[str] = []
        self._record = record

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        reply_markup: Optional[object] = None,
    ) -> None:
        self._sent.append(text)

    def _pma_registry_key(
        self, record: "TelegramTopicRecord", message: Optional[TelegramMessage] = None
    ) -> str:
        from codex_autorunner.integrations.app_server.threads import pma_base_key

        agent = (
            self._effective_agent(record)
            if hasattr(self, "_effective_agent")
            else record.agent or "codex"
        )
        base_key = pma_base_key(agent)

        require_topics = getattr(self._config, "require_topics", False)
        if require_topics and message is not None:
            topic_key = f"{message.chat_id}:{message.thread_id or 'root'}"
            return f"{base_key}.{topic_key}"
        return base_key

    def _effective_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        if record and record.agent:
            return record.agent
        return "codex"

    def _effective_runtime_agent(self, _record: Optional["TelegramTopicRecord"]) -> str:
        return "codex"

    def _effective_agent_profile(
        self, _record: Optional["TelegramTopicRecord"]
    ) -> Optional[str]:
        return None


@pytest.mark.anyio
async def test_pma_resume_uses_hub_root(tmp_path: Path) -> None:
    """Test that /resume works for PMA topics by using hub root."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None, agent="codex")

    class _ResumeClientStub:
        async def thread_list(self, cursor: Optional[str] = None, limit: int = 100):
            return {
                "entries": [
                    {
                        "id": "thread-1",
                        "workspace_path": str(hub_root),
                        "rollout_path": None,
                        "preview": {"user": "Test", "assistant": "Response"},
                    }
                ],
                "cursor": None,
            }

    class _ResumeRouterStub:
        def __init__(self, record: TelegramTopicRecord) -> None:
            self._record = record

        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return self._record

    class _ResumeHandler(WorkspaceCommands):
        def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace()
            self._router = _ResumeRouterStub(record)
            self._hub_root = hub_root
            self._resume_options: dict[str, SelectionState] = {}
            self._sent: list[str] = []

            async def _store_load():
                return SimpleNamespace(topics={})

            async def _store_update_topic(k, f):
                return None

            self._store = SimpleNamespace(
                load=_store_load,
                update_topic=_store_update_topic,
            )

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
            reply_markup: Optional[object] = None,
        ) -> None:
            self._sent.append(text)

        async def _client_for_workspace(self, workspace_path: str):
            return _ResumeClientStub()

    handler = _ResumeHandler(record, hub_root)
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-2002,
        thread_id=333,
        from_user_id=99,
        text="/resume",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_resume(message, "")

    # Should not send "Topic not bound" error - PMA should use hub root
    assert not any("Topic not bound" in msg for msg in handler._sent)


class _OpencodeResumeClientMissingSession:
    async def get_session(self, session_id: str) -> dict[str, object]:
        request = httpx.Request("GET", f"http://opencode.local/session/{session_id}")
        response = httpx.Response(
            404,
            request=request,
            json={"error": {"message": f"session not found: {session_id}"}},
        )
        raise httpx.HTTPStatusError(
            f"Client error '404 Not Found' for url '{request.url}'",
            request=request,
            response=response,
        )


class _OpencodeResumeSupervisorStub:
    def __init__(self, client: _OpencodeResumeClientMissingSession) -> None:
        self._client = client

    async def get_client(self, _root: Path) -> _OpencodeResumeClientMissingSession:
        return self._client


class _OpencodeResumeRouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> TelegramTopicRecord:
        if callable(apply):
            apply(self._record)
        return self._record


class _OpencodeResumeStoreStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def update_topic(self, _key: str, apply: object) -> None:
        if callable(apply):
            apply(self._record)


class _OpencodeResumeHandler(WorkspaceCommands):
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._logger = logging.getLogger("test")
        self._router = _OpencodeResumeRouterStub(record)
        self._store = _OpencodeResumeStoreStub(record)
        self._resume_options: dict[str, SelectionState] = {}
        self._config = SimpleNamespace()
        self._opencode_supervisor = _OpencodeResumeSupervisorStub(
            _OpencodeResumeClientMissingSession()
        )
        self.answers: list[str] = []
        self.final_messages: list[str] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _answer_callback(self, _callback: object, text: str) -> None:
        self.answers.append(text)

    async def _finalize_selection(
        self, _key: str, _callback: object, text: str
    ) -> None:
        self.final_messages.append(text)

    async def _find_thread_conflict(
        self, _thread_id: str, *, key: str
    ) -> Optional[str]:
        return None

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not workspace_path:
            return None
        return Path(workspace_path).expanduser().resolve()


@pytest.mark.anyio
async def test_resume_opencode_missing_session_clears_stale_topic_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    stale_session = "session-stale"
    record = TelegramTopicRecord(
        workspace_path=str(workspace),
        agent="opencode",
        active_thread_id=stale_session,
        thread_ids=[stale_session, "session-live"],
    )
    record.thread_summaries[stale_session] = ThreadSummary(
        user_preview="stale",
    )
    handler = _OpencodeResumeHandler(record)
    key = await handler._resolve_topic_key(-1001, 77)

    await handler._resume_opencode_thread_by_id(key, stale_session)

    assert record.active_thread_id is None
    assert stale_session not in record.thread_ids
    assert stale_session not in record.thread_summaries
    assert handler.answers == []
    assert any("Thread no longer exists." in text for text in handler.final_messages)


class _PmaTargetsRouterStub:
    def __init__(self, record: Optional[TelegramTopicRecord]) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> Optional[TelegramTopicRecord]:
        return self._record

    async def ensure_topic(
        self, _chat_id: int, _thread_id: Optional[int]
    ) -> TelegramTopicRecord:
        if self._record is None:
            self._record = TelegramTopicRecord()
        return self._record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply: object
    ) -> TelegramTopicRecord:
        if self._record is None:
            self._record = TelegramTopicRecord()
        if callable(apply):
            apply(self._record)
        return self._record


class _PmaTargetsHandler(TelegramCommandHandlers):
    def __init__(
        self,
        *,
        hub_root: Path,
        record: Optional[TelegramTopicRecord],
        pma_enabled: bool = True,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._hub_root = hub_root
        self._hub_supervisor = SimpleNamespace(
            hub_config=SimpleNamespace(pma=SimpleNamespace(enabled=pma_enabled))
        )
        self._router = _PmaTargetsRouterStub(record)
        self.sent: list[str] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        self.sent.append(text)


class _McpClientStub:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def request(self, _method: str, _params: dict[str, object]) -> object:
        raise self._exc


class _McpHandler(TelegramCommandHandlers):
    def __init__(self, record: TelegramTopicRecord, client: _McpClientStub) -> None:
        self._logger = logging.getLogger("test")
        self._router = _PMARouterStub(record)
        self._client = client
        self.sent: list[str] = []

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _client_for_workspace(self, _workspace_path: str) -> _McpClientStub:
        return self._client

    async def _refresh_workspace_id(
        self, _key: str, record: TelegramTopicRecord
    ) -> Optional[str]:
        return record.workspace_id

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> None:
        self.sent.append(text)


def _make_pma_message(
    *, chat_id: int = -1001, thread_id: Optional[int] = 55
) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=99,
        text="/pma",
        date=None,
        is_topic_message=thread_id is not None,
    )


@pytest.mark.anyio
async def test_mcp_lists_failure_message_on_app_server_response_error(
    tmp_path: Path,
) -> None:
    record = TelegramTopicRecord(
        workspace_path=str(tmp_path / "workspace"),
        workspace_id="workspace-1",
    )
    handler = _McpHandler(
        record,
        _McpClientStub(
            CodexAppServerResponseError(
                method="mcpServerStatus/list",
                code=-32601,
                message="method not found",
                data=None,
            )
        ),
    )
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=-1001,
        thread_id=55,
        from_user_id=99,
        text="/mcp",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_mcp(message, "", _RuntimeStub())

    assert handler.sent == [
        "Failed to list MCP servers; check logs for details. (conversation -1001:55)"
    ]


@pytest.mark.anyio
async def test_pma_on_enables_mode(tmp_path: Path) -> None:
    record = TelegramTopicRecord(
        pma_enabled=False,
        repo_id="repo-1",
        workspace_path=str(tmp_path / "repo"),
        workspace_id="workspace-1",
        active_thread_id="thread-1",
    )
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "on", _RuntimeStub())

    assert record.pma_enabled is True
    assert (
        handler.sent[-1]
        == "PMA mode enabled. Use /pma off to exit. Previous binding saved."
    )


@pytest.mark.anyio
async def test_pma_off_disables_mode(tmp_path: Path) -> None:
    record = TelegramTopicRecord(
        pma_enabled=True,
        pma_prev_repo_id="repo-2",
        pma_prev_workspace_path=str(tmp_path / "repo"),
        pma_prev_workspace_id="workspace-2",
        pma_prev_active_thread_id="thread-2",
    )
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "off", _RuntimeStub())

    assert record.pma_enabled is False
    assert (
        handler.sent[-1]
        == f"PMA mode disabled. Restored binding to {tmp_path / 'repo'}."
    )


@pytest.mark.anyio
async def test_pma_status_reports_disabled_mode(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "status", _RuntimeStub())

    assert handler.sent[-1] == "PMA mode: disabled\nCurrent workspace: unbound"


@pytest.mark.anyio
async def test_pma_status_reports_bound_workspace_when_disabled(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    record = TelegramTopicRecord(pma_enabled=False, workspace_path=str(workspace))
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "status", _RuntimeStub())

    assert handler.sent[-1] == f"PMA mode: disabled\nCurrent workspace: {workspace}"


@pytest.mark.anyio
async def test_pma_on_is_idempotent_when_already_enabled(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=True)
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "on", _RuntimeStub())

    assert (
        handler.sent[-1]
        == "PMA mode is already enabled for this topic. Use /pma off to exit."
    )


@pytest.mark.anyio
async def test_pma_without_subcommand_reports_status(tmp_path: Path) -> None:
    record = TelegramTopicRecord(pma_enabled=False)
    handler = _PmaTargetsHandler(hub_root=tmp_path / "hub", record=record)
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "", _RuntimeStub())

    assert handler.sent[-1] == "PMA mode: disabled\nCurrent workspace: unbound"


@pytest.mark.anyio
async def test_pma_targets_subcommand_uses_usage_text(tmp_path: Path) -> None:
    handler = _PmaTargetsHandler(
        hub_root=tmp_path / "hub", record=TelegramTopicRecord()
    )
    message = _make_pma_message(chat_id=-1001, thread_id=55)

    await handler._handle_pma(message, "targets", _RuntimeStub())

    assert handler.sent[-1] == "Usage:\n/pma [on|off|status]"


@pytest.mark.anyio
async def test_require_topics_uses_scoped_pma_registry_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_topic_scoped_key

    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="codex",
    )
    hub_root = tmp_path
    registry = AppServerThreadRegistry(hub_root / "threads.json")
    handler = _PMAHandler(record, _PMAClientStub(), hub_root, registry)
    handler._config = SimpleNamespace(
        root=hub_root,
        concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=False),
        agent_turn_timeout_seconds={"codex": None, "opencode": None},
        require_topics=True,
    )
    handler._pma_registry_key(record, None)

    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="test",
        date=None,
        is_topic_message=True,
    )

    pma_key = handler._pma_registry_key(record, message)
    assert pma_key == "pma.-1001:101"

    registry.set_thread_id(pma_key, "test-thread-id")
    assert registry.get_thread_id(pma_key) == "test-thread-id"

    def mock_topic_key(chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id or 'root'}"

    expected_key = pma_topic_scoped_key(
        agent="codex",
        chat_id=-1001,
        thread_id=101,
        topic_key_fn=mock_topic_key,
    )
    assert pma_key == expected_key


def test_pma_registry_key_matches_logical_hermes_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.app_server.threads import pma_base_key

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {
            "hermes-m4-pma": SimpleNamespace(name="Hermes M4 PMA"),
        },
    )
    hub_config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": SimpleNamespace(display_name="M4 PMA")}
            if agent_id == "hermes"
            else {}
        ),
        agent_binary=lambda agent_id, **kw: "hermes",
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry._resolve_runtime_agent_config",
        lambda ctx: hub_config,
    )

    record = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="hermes-m4-pma",
    )
    handler = _PMAHandler(
        record, _PMAClientStub(), tmp_path, AppServerThreadRegistry(tmp_path / "t.json")
    )
    expected = pma_base_key("hermes", "m4-pma")
    assert handler._pma_registry_key(record, None) == expected

    record_split = TelegramTopicRecord(
        pma_enabled=True,
        workspace_path=None,
        repo_id="repo-1",
        agent="hermes",
        agent_profile="m4-pma",
    )
    handler_split = _PMAHandler(
        record_split,
        _PMAClientStub(),
        tmp_path,
        AppServerThreadRegistry(tmp_path / "t2.json"),
    )
    assert handler_split._pma_registry_key(record_split, None) == expected
    assert expected != pma_base_key("hermes-m4-pma")


class _HelpHandlersStub:
    async def _noop(self, *args: object, **kwargs: object) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        if name.startswith("_handle_"):
            return self._noop
        raise AttributeError(name)


def test_help_text_mentions_pma_mode() -> None:
    specs = build_command_specs(_HelpHandlersStub())
    text = _format_help_text(specs)
    assert "/pma - PMA mode" in text
