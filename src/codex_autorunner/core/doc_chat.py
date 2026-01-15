import asyncio
import contextlib
import json
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from ..integrations.app_server.client import CodexAppServerError
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .app_server_prompts import build_doc_chat_prompt
from .app_server_threads import (
    DOC_CHAT_PREFIX,
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .config import RepoConfig
from .engine import Engine, timestamp
from .locks import FileLock, FileLockBusy, FileLockError
from .patch_utils import (
    PatchError,
    apply_patch_file,
    ensure_patch_targets_allowed,
    normalize_patch_text,
    preview_patch,
)
from .state import load_state

ALLOWED_DOC_KINDS = ("todo", "progress", "opinions", "spec", "summary")
DOC_CHAT_TIMEOUT_SECONDS = 180
DOC_CHAT_INTERRUPT_GRACE_SECONDS = 10
DOC_CHAT_PATCH_NAME = "doc-chat.patch"


@dataclass
class DocChatRequest:
    kind: str
    message: str
    stream: bool = False


@dataclass
class ActiveDocChatTurn:
    kind: str
    thread_id: str
    turn_id: str
    client: Any
    interrupted: bool = False
    interrupt_sent: bool = False
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)


class DocChatError(Exception):
    """Base error for doc chat failures."""


class DocChatValidationError(DocChatError):
    """Raised when a request payload is invalid."""


class DocChatBusyError(DocChatError):
    """Raised when a doc chat is already running for the target doc."""


def _normalize_kind(kind: str) -> str:
    key = (kind or "").lower()
    if key not in ALLOWED_DOC_KINDS:
        raise DocChatValidationError("invalid doc kind")
    return key


def _normalize_message(message: str) -> str:
    msg = (message or "").strip()
    if not msg:
        raise DocChatValidationError("message is required")
    return msg


def format_sse(event: str, data: object) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    lines = payload.splitlines() or [""]
    parts = [f"event: {event}"]
    for line in lines:
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"


class DocChatService:
    def __init__(
        self,
        engine: Engine,
        *,
        app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
        app_server_threads: Optional[AppServerThreadRegistry] = None,
    ):
        self.engine = engine
        self._locks: Dict[str, asyncio.Lock] = {}
        self._recent_summary_cache: Optional[str] = None
        self.patch_path = (
            self.engine.repo_root / ".codex-autorunner" / DOC_CHAT_PATCH_NAME
        )
        self.last_agent_message: Optional[str] = None
        self._lock_root = self.engine.repo_root / ".codex-autorunner" / "locks"
        self._app_server_supervisor = app_server_supervisor
        self._app_server_threads = app_server_threads or AppServerThreadRegistry(
            default_app_server_threads_path(self.engine.repo_root)
        )
        self._active_turns: Dict[str, ActiveDocChatTurn] = {}
        self._active_turns_lock = threading.Lock()
        self._pending_interrupts: set[str] = set()

    def _repo_config(self) -> RepoConfig:
        if not isinstance(self.engine.config, RepoConfig):
            raise DocChatError("Doc chat requires repo mode config")
        return self.engine.config

    def _get_active_turn(self, kind: str) -> Optional[ActiveDocChatTurn]:
        with self._active_turns_lock:
            return self._active_turns.get(kind)

    def _clear_active_turn(self, kind: str, turn_id: str) -> None:
        with self._active_turns_lock:
            current = self._active_turns.get(kind)
            if current and current.turn_id == turn_id:
                self._active_turns.pop(kind, None)

    def _register_active_turn(
        self, kind: str, client: Any, turn_id: str, thread_id: str
    ) -> ActiveDocChatTurn:
        interrupt_event = asyncio.Event()
        active = ActiveDocChatTurn(
            kind=kind,
            thread_id=thread_id,
            turn_id=turn_id,
            client=client,
            interrupted=False,
            interrupt_sent=False,
            interrupt_event=interrupt_event,
        )
        with self._active_turns_lock:
            self._active_turns[kind] = active
            if kind in self._pending_interrupts:
                self._pending_interrupts.remove(kind)
                active.interrupted = True
                interrupt_event.set()
        return active

    async def _interrupt_turn(self, active: ActiveDocChatTurn) -> None:
        if active.interrupt_sent:
            return
        active.interrupt_sent = True
        chat_id = self._chat_id()
        try:
            await asyncio.wait_for(
                active.client.turn_interrupt(
                    active.turn_id, thread_id=active.thread_id
                ),
                timeout=DOC_CHAT_INTERRUPT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            self._log(
                chat_id,
                "result=error " 'detail="interrupt_timeout" backend=app_server',
            )
        except CodexAppServerError as exc:
            self._log(
                chat_id,
                "result=error "
                f'detail="interrupt_failed:{self._compact_message(str(exc))}" '
                "backend=app_server",
            )

    async def interrupt(self, kind: str) -> Dict[str, str]:
        key = _normalize_kind(kind)
        active = self._get_active_turn(key)
        if active is None:
            with self._active_turns_lock:
                self._pending_interrupts.add(key)
            return {"status": "interrupted", "kind": key, "detail": "No active turn"}
        active.interrupted = True
        active.interrupt_event.set()
        await self._interrupt_turn(active)
        return {"status": "interrupted", "kind": key, "detail": "Doc chat interrupted"}

    def parse_request(self, kind: str, payload: Optional[dict]) -> DocChatRequest:
        if payload is None or not isinstance(payload, dict):
            raise DocChatValidationError("invalid payload")
        key = _normalize_kind(kind)
        message = _normalize_message(str(payload.get("message", "")))
        stream = bool(payload.get("stream", False))
        return DocChatRequest(kind=key, message=message, stream=stream)

    def repo_blocked_reason(self) -> Optional[str]:
        return self.engine.repo_busy_reason()

    def doc_busy(self, kind: str) -> bool:
        if self._lock_for(kind).locked():
            return True
        lock = FileLock(self._doc_lock_path(kind))
        try:
            lock.acquire(blocking=False)
        except FileLockBusy:
            return True
        except FileLockError:
            return True
        finally:
            lock.release()
        return False

    @asynccontextmanager
    async def doc_lock(self, kind: str):
        lock = self._lock_for(kind)
        if lock.locked():
            raise DocChatBusyError(f"Doc chat already running for {kind}")
        await lock.acquire()
        file_lock = FileLock(self._doc_lock_path(kind))
        try:
            try:
                file_lock.acquire(blocking=False)
            except FileLockBusy as exc:
                raise DocChatBusyError(f"Doc chat already running for {kind}") from exc
            except FileLockError as exc:
                raise DocChatError(str(exc)) from exc
            yield
        finally:
            file_lock.release()
            lock.release()

    def _lock_for(self, kind: str) -> asyncio.Lock:
        key = _normalize_kind(kind)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _doc_lock_path(self, kind: str) -> Path:
        key = _normalize_kind(kind)
        return self._lock_root / f"doc_chat.{key}.lock"

    def _chat_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def _log(self, chat_id: str, message: str) -> None:
        line = f"[{timestamp()}] doc-chat id={chat_id} {message}\n"
        self.engine.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def _doc_pointer(self, kind: str) -> str:
        config = self._repo_config()
        path = config.doc_path(kind)
        try:
            return str(path.relative_to(self.engine.repo_root))
        except ValueError:
            return str(path)

    @staticmethod
    def _compact_message(message: str, limit: int = 240) -> str:
        compact = " ".join((message or "").split()).replace('"', "'")
        if len(compact) > limit:
            return compact[: limit - 3] + "..."
        return compact

    def _recent_run_summary(self) -> Optional[str]:
        if self._recent_summary_cache is not None:
            return self._recent_summary_cache
        state = load_state(self.engine.state_path)
        if not state.last_run_id:
            return None
        summary = self.engine.extract_prev_output(state.last_run_id)
        self._recent_summary_cache = summary
        return summary

    def _build_app_server_prompt(self, request: DocChatRequest) -> str:
        return build_doc_chat_prompt(
            self.engine.config,
            kind=request.kind,
            message=request.message,
            recent_summary=self._recent_run_summary(),
        )

    def _ensure_app_server(self) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is None:
            raise DocChatError("App-server backend is not configured")
        return self._app_server_supervisor

    def _thread_key(self, kind: str) -> str:
        return f"{DOC_CHAT_PREFIX}{kind}"

    @staticmethod
    def _parse_agent_message(output: str, kind: str) -> str:
        text = (output or "").strip()
        if not text:
            return f"Updated {kind.upper()} via doc chat."
        for line in text.splitlines():
            if line.lower().startswith("agent:"):
                return (
                    line[len("agent:") :].strip()
                    or f"Updated {kind.upper()} via doc chat."
                )
        return text.splitlines()[0].strip()

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        lines = text.strip().splitlines()
        if (
            len(lines) >= 2
            and lines[0].startswith("```")
            and lines[-1].startswith("```")
        ):
            return "\n".join(lines[1:-1]).strip()
        return text.strip()

    @classmethod
    def _split_patch_from_output(cls, output: str) -> Tuple[str, str]:
        if not output:
            return "", ""
        match = re.search(
            r"<PATCH>(.*?)</PATCH>", output, flags=re.IGNORECASE | re.DOTALL
        )
        if match:
            patch_text = cls._strip_code_fences(match.group(1))
            before = output[: match.start()].strip()
            after = output[match.end() :].strip()
            message_text = "\n".join(part for part in [before, after] if part)
            return message_text, patch_text
        lines = output.splitlines()
        start_idx = None
        for idx, line in enumerate(lines):
            if line.startswith("--- ") or line.startswith("*** Begin Patch"):
                start_idx = idx
                break
        if start_idx is None:
            return output.strip(), ""
        message_text = "\n".join(lines[:start_idx]).strip()
        patch_text = "\n".join(lines[start_idx:]).strip()
        patch_text = cls._strip_code_fences(patch_text)
        return message_text, patch_text

    def _cleanup_patch(self) -> None:
        if self.patch_path.exists():
            try:
                self.patch_path.unlink()
            except OSError:
                pass

    def _read_patch(self) -> str:
        if not self.patch_path.exists():
            raise DocChatError("Agent did not produce a patch file")
        text = self.patch_path.read_text(encoding="utf-8")
        if not text.strip():
            raise DocChatError("Agent produced an empty patch")
        return text

    def _apply_app_server_patch(self, kind: str) -> str:
        config = self._repo_config()
        target_path = config.doc_path(kind)
        expected = str(target_path.relative_to(self.engine.repo_root))
        patch_text_raw = self._read_patch()
        try:
            normalized_patch, targets = normalize_patch_text(
                patch_text_raw, default_target=expected
            )
            ensure_patch_targets_allowed(targets, [expected])
        except PatchError as exc:
            raise DocChatError(str(exc)) from exc
        self.patch_path.write_text(normalized_patch, encoding="utf-8")
        try:
            apply_patch_file(self.engine.repo_root, self.patch_path, targets)
        except PatchError as exc:
            raise DocChatError(f"Failed to apply patch: {exc}") from exc
        self._cleanup_patch()
        return target_path.read_text(encoding="utf-8")

    def apply_saved_patch(self, kind: str) -> str:
        return self._apply_app_server_patch(kind)

    def discard_patch(self, kind: str) -> str:
        config = self._repo_config()
        target_path = config.doc_path(kind)
        self._cleanup_patch()
        return target_path.read_text(encoding="utf-8")

    def pending_patch(self, kind: str) -> Optional[dict]:
        if not self.patch_path.exists():
            return None
        config = self._repo_config()
        expected = str(config.doc_path(kind).relative_to(self.engine.repo_root))
        try:
            patch_text_raw = self._read_patch()
            patch_text, targets = normalize_patch_text(
                patch_text_raw, default_target=expected
            )
            normalized_targets = ensure_patch_targets_allowed(targets, [expected])
            preview = preview_patch(self.engine.repo_root, patch_text, targets)
            content = preview.get(
                normalized_targets[0], config.doc_path(kind).read_text(encoding="utf-8")
            )
        except (DocChatError, PatchError):
            return None
        return {
            "status": "ok",
            "kind": kind,
            "patch": patch_text,
            "agent_message": self.last_agent_message
            or f"Pending patch for {kind.upper()}",
            "content": content,
        }

    async def _execute_app_server(self, request: DocChatRequest) -> dict:
        chat_id = self._chat_id()
        started_at = time.time()
        doc_pointer = self._doc_pointer(request.kind)
        message_for_log = self._compact_message(request.message)
        self._log(
            chat_id,
            f'start kind={request.kind} path={doc_pointer} message="{message_for_log}"',
        )
        try:
            self._cleanup_patch()
            supervisor = self._ensure_app_server()
            client = await supervisor.get_client(self.engine.repo_root)
            key = self._thread_key(request.kind)
            thread_id = self._app_server_threads.get_thread_id(key)
            if thread_id:
                try:
                    resume_result = await client.thread_resume(thread_id)
                    resumed = resume_result.get("id")
                    if isinstance(resumed, str) and resumed:
                        thread_id = resumed
                        self._app_server_threads.set_thread_id(key, thread_id)
                except CodexAppServerError:
                    self._app_server_threads.reset_thread(key)
                    thread_id = None
            if not thread_id:
                thread = await client.thread_start(str(self.engine.repo_root))
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise DocChatError("App-server did not return a thread id")
                self._app_server_threads.set_thread_id(key, thread_id)
            prompt = self._build_app_server_prompt(request)
            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy="never",
                sandbox_policy="readOnly",
            )
            active = self._register_active_turn(
                request.kind, client, handle.turn_id, handle.thread_id
            )
            turn_task = asyncio.create_task(handle.wait(timeout=None))
            timeout_task = asyncio.create_task(asyncio.sleep(DOC_CHAT_TIMEOUT_SECONDS))
            interrupt_task = asyncio.create_task(active.interrupt_event.wait())
            try:
                tasks = {turn_task, timeout_task, interrupt_task}
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                if timeout_task in done:
                    turn_task.add_done_callback(lambda task: task.exception())
                    raise asyncio.TimeoutError()
                if interrupt_task in done:
                    active.interrupted = True
                    await self._interrupt_turn(active)
                    done, _pending = await asyncio.wait(
                        {turn_task}, timeout=DOC_CHAT_INTERRUPT_GRACE_SECONDS
                    )
                    if not done:
                        turn_task.add_done_callback(lambda task: task.exception())
                        duration_ms = int((time.time() - started_at) * 1000)
                        self._log(
                            chat_id,
                            "result=interrupted "
                            f"kind={request.kind} path={doc_pointer} "
                            f"duration_ms={duration_ms} "
                            f'message="{message_for_log}" backend=app_server',
                        )
                        self._cleanup_patch()
                        return {
                            "status": "interrupted",
                            "detail": "Doc chat interrupted",
                        }
                turn_result = await turn_task
            finally:
                self._clear_active_turn(request.kind, handle.turn_id)
                timeout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timeout_task
                interrupt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await interrupt_task
            if active.interrupted:
                duration_ms = int((time.time() - started_at) * 1000)
                self._log(
                    chat_id,
                    "result=interrupted "
                    f"kind={request.kind} path={doc_pointer} "
                    f"duration_ms={duration_ms} "
                    f'message="{message_for_log}" backend=app_server',
                )
                self._cleanup_patch()
                return {"status": "interrupted", "detail": "Doc chat interrupted"}
            if turn_result.errors:
                raise DocChatError(turn_result.errors[-1])
            output = "\n".join(turn_result.agent_messages).strip()
            message_text, patch_text_raw = self._split_patch_from_output(output)
            if not patch_text_raw.strip():
                raise DocChatError("App-server output missing a patch")
            agent_message = self._parse_agent_message(
                message_text or output, request.kind
            )
            config = self._repo_config()
            expected = str(
                config.doc_path(request.kind).relative_to(self.engine.repo_root)
            )
            try:
                patch_text, targets = normalize_patch_text(
                    patch_text_raw, default_target=expected
                )
                normalized_targets = ensure_patch_targets_allowed(targets, [expected])
                preview = preview_patch(self.engine.repo_root, patch_text, targets)
                content = preview.get(normalized_targets[0], "")
            except PatchError as exc:
                raise DocChatError(str(exc)) from exc
            self.patch_path.write_text(patch_text, encoding="utf-8")
            self.last_agent_message = agent_message
            duration_ms = int((time.time() - started_at) * 1000)
            self._log(
                chat_id,
                "result=success "
                f"kind={request.kind} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" backend=app_server',
            )
            return {
                "status": "ok",
                "kind": request.kind,
                "patch": patch_text,
                "content": content,
                "agent_message": agent_message,
            }
        except asyncio.TimeoutError:
            duration_ms = int((time.time() - started_at) * 1000)
            self._log(
                chat_id,
                "result=error "
                f"kind={request.kind} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="timeout" backend=app_server',
            )
            self._cleanup_patch()
            return {"status": "error", "detail": "Doc chat agent timed out"}
        except DocChatError as exc:
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error "
                f"kind={request.kind} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="{detail}" backend=app_server',
            )
            self._cleanup_patch()
            return {"status": "error", "detail": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error kind={kind} path={path} duration_ms={duration_ms} "
                'message="{message}" detail="{detail}" backend=app_server'.format(
                    kind=request.kind,
                    path=doc_pointer,
                    duration_ms=duration_ms,
                    message=message_for_log,
                    detail=detail,
                ),
            )
            return {"status": "error", "detail": "Doc chat failed"}

    async def execute(self, request: DocChatRequest) -> dict:
        return await self._execute_app_server(request)

    async def stream(self, request: DocChatRequest) -> AsyncIterator[str]:
        try:
            async with self.doc_lock(request.kind):
                yield format_sse("status", {"status": "queued"})
                try:
                    result = await self.execute(request)
                except DocChatError as exc:
                    yield format_sse("error", {"detail": str(exc)})
                    return
                if result.get("status") == "ok":
                    yield format_sse("update", result)
                    yield format_sse("done", {"status": "ok"})
                elif result.get("status") == "interrupted":
                    yield format_sse(
                        "interrupted",
                        {"detail": result.get("detail") or "Doc chat interrupted"},
                    )
                else:
                    detail = result.get("detail") or "Doc chat failed"
                    yield format_sse("error", {"detail": detail})
        except DocChatBusyError as exc:
            yield format_sse("error", {"detail": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            yield format_sse("error", {"detail": str(exc)})
