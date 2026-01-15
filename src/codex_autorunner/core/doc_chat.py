import asyncio
import contextlib
import difflib
import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple

from ..integrations.app_server.client import CodexAppServerError
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .app_server_events import AppServerEventBuffer
from .app_server_prompts import build_doc_chat_prompt
from .app_server_threads import (
    DOC_CHAT_KEY,
    DOC_CHAT_PREFIX,
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .config import RepoConfig
from .engine import Engine, timestamp
from .locks import FileLock, FileLockBusy, FileLockError
from .patch_utils import (
    PatchError,
    ensure_patch_targets_allowed,
    normalize_patch_text,
    preview_patch,
)
from .state import load_state, now_iso
from .utils import atomic_write

ALLOWED_DOC_KINDS = ("todo", "progress", "opinions", "spec", "summary")
DOC_CHAT_TIMEOUT_SECONDS = 180
DOC_CHAT_INTERRUPT_GRACE_SECONDS = 10
DOC_CHAT_STATE_NAME = "doc_chat_state.json"
DOC_CHAT_STATE_VERSION = 1


@dataclass
class DocChatRequest:
    message: str
    stream: bool = False
    targets: Optional[tuple[str, ...]] = None


@dataclass
class ActiveDocChatTurn:
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


class DocChatConflictError(DocChatError):
    """Raised when a doc draft conflicts with newer edits."""


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


@dataclass
class DocChatDraftState:
    content: str
    patch: str
    agent_message: str
    created_at: str
    base_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "content": self.content,
            "patch": self.patch,
            "agent_message": self.agent_message,
            "created_at": self.created_at,
            "base_hash": self.base_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Optional["DocChatDraftState"]:
        if not isinstance(payload, dict):
            return None
        content = payload.get("content")
        patch = payload.get("patch")
        agent_message = payload.get("agent_message")
        created_at = payload.get("created_at")
        base_hash = payload.get("base_hash")
        if not isinstance(content, str) or not isinstance(patch, str):
            return None
        if not isinstance(agent_message, str):
            agent_message = ""
        if not isinstance(created_at, str):
            created_at = ""
        if not isinstance(base_hash, str):
            base_hash = ""
        return cls(
            content=content,
            patch=patch,
            agent_message=agent_message,
            created_at=created_at,
            base_hash=base_hash,
        )


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
        app_server_events: Optional[AppServerEventBuffer] = None,
    ):
        self.engine = engine
        self._recent_summary_cache: Optional[str] = None
        self._drafts_path = (
            self.engine.repo_root / ".codex-autorunner" / DOC_CHAT_STATE_NAME
        )
        self._lock_root = self.engine.repo_root / ".codex-autorunner" / "locks"
        self._app_server_supervisor = app_server_supervisor
        self._app_server_threads = app_server_threads or AppServerThreadRegistry(
            default_app_server_threads_path(self.engine.repo_root)
        )
        self._app_server_events = app_server_events
        self._lock: Optional[asyncio.Lock] = None
        self._thread_lock = threading.Lock()
        self._active_turn: Optional[ActiveDocChatTurn] = None
        self._active_turn_lock = threading.Lock()
        self._pending_interrupt = False

    def _repo_config(self) -> RepoConfig:
        if not isinstance(self.engine.config, RepoConfig):
            raise DocChatError("Doc chat requires repo mode config")
        return self.engine.config

    def _get_active_turn(self) -> Optional[ActiveDocChatTurn]:
        with self._active_turn_lock:
            return self._active_turn

    def _clear_active_turn(self, turn_id: str) -> None:
        with self._active_turn_lock:
            if self._active_turn and self._active_turn.turn_id == turn_id:
                self._active_turn = None

    def _register_active_turn(
        self, client: Any, turn_id: str, thread_id: str
    ) -> ActiveDocChatTurn:
        interrupt_event = asyncio.Event()
        active = ActiveDocChatTurn(
            thread_id=thread_id,
            turn_id=turn_id,
            client=client,
            interrupted=False,
            interrupt_sent=False,
            interrupt_event=interrupt_event,
        )
        with self._active_turn_lock:
            self._active_turn = active
            if self._pending_interrupt:
                self._pending_interrupt = False
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

    async def interrupt(self, _kind: Optional[str] = None) -> Dict[str, str]:
        active = self._get_active_turn()
        if active is None:
            pending = self._local_busy()
            with self._active_turn_lock:
                self._pending_interrupt = pending
            return {
                "status": "interrupted",
                "detail": "No active turn",
            }
        active.interrupted = True
        active.interrupt_event.set()
        await self._interrupt_turn(active)
        return {"status": "interrupted", "detail": "Doc chat interrupted"}

    def parse_request(
        self, payload: Optional[dict], *, kind: Optional[str] = None
    ) -> DocChatRequest:
        if payload is None or not isinstance(payload, dict):
            raise DocChatValidationError("invalid payload")
        message = _normalize_message(str(payload.get("message", "")))
        stream = bool(payload.get("stream", False))
        raw_targets = payload.get("targets") or payload.get("target")
        targets: Optional[tuple[str, ...]] = None
        if isinstance(raw_targets, (list, tuple)):
            normalized = []
            for entry in raw_targets:
                try:
                    normalized.append(_normalize_kind(str(entry)))
                except DocChatValidationError:
                    raise
            if normalized:
                targets = tuple(dict.fromkeys(normalized))
            else:
                raise DocChatValidationError("target is required")
        elif isinstance(raw_targets, str) and raw_targets.strip():
            try:
                targets = (_normalize_kind(raw_targets),)
            except DocChatValidationError:
                raise
        if kind:
            normalized_kind = _normalize_kind(kind)
            if targets is None:
                targets = (normalized_kind,)
            else:
                if any(target != normalized_kind for target in targets):
                    raise DocChatValidationError("target must match doc kind")
                targets = (normalized_kind,)
        return DocChatRequest(message=message, stream=stream, targets=targets)

    def repo_blocked_reason(self) -> Optional[str]:
        return self.engine.repo_busy_reason()

    def doc_busy(self, _kind: Optional[str] = None) -> bool:
        lock = self._ensure_lock()
        if lock.locked():
            return True
        file_lock = FileLock(self._doc_lock_path())
        try:
            file_lock.acquire(blocking=False)
        except FileLockBusy:
            return True
        except FileLockError:
            return True
        finally:
            file_lock.release()
        return False

    def _local_busy(self) -> bool:
        if self._thread_lock.locked():
            return True
        lock = self._lock
        return bool(lock and lock.locked())

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            try:
                self._lock = asyncio.Lock()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
                self._lock = asyncio.Lock()
        return self._lock

    @asynccontextmanager
    async def doc_lock(self, _kind: Optional[str] = None):
        if not self._thread_lock.acquire(blocking=False):
            raise DocChatBusyError("Doc chat already running")
        lock = self._ensure_lock()
        if lock.locked():
            self._thread_lock.release()
            raise DocChatBusyError("Doc chat already running")
        await lock.acquire()
        file_lock = FileLock(self._doc_lock_path())
        try:
            try:
                file_lock.acquire(blocking=False)
            except FileLockBusy as exc:
                raise DocChatBusyError("Doc chat already running") from exc
            except FileLockError as exc:
                raise DocChatError(str(exc)) from exc
            yield
        finally:
            file_lock.release()
            lock.release()
            self._thread_lock.release()
            with self._active_turn_lock:
                self._pending_interrupt = False

    def _doc_lock_path(self) -> Path:
        return self._lock_root / "doc_chat.lock"

    def _chat_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def _log(self, chat_id: str, message: str) -> None:
        line = f"[{timestamp()}] doc-chat id={chat_id} {message}\n"
        self.engine.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def _doc_pointer(self, targets: Optional[tuple[str, ...]]) -> str:
        config = self._repo_config()
        if not targets:
            return "auto"
        paths = []
        for kind in targets:
            path = config.doc_path(kind)
            try:
                paths.append(str(path.relative_to(self.engine.repo_root)))
            except ValueError:
                paths.append(str(path))
        return ",".join(paths) if paths else "auto"

    @staticmethod
    def _compact_message(message: str, limit: int = 240) -> str:
        compact = " ".join((message or "").split()).replace('"', "'")
        if len(compact) > limit:
            return compact[: limit - 3] + "..."
        return compact

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _handle_turn_start(
        self,
        thread_id: str,
        turn_id: str,
        *,
        on_turn_start: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        if self._app_server_events is not None:
            try:
                await self._app_server_events.register_turn(thread_id, turn_id)
            except Exception:
                pass
        if on_turn_start is None:
            return
        try:
            await self._maybe_await(on_turn_start(thread_id, turn_id))
        except Exception:
            pass

    def _recent_run_summary(self) -> Optional[str]:
        if self._recent_summary_cache is not None:
            return self._recent_summary_cache
        state = load_state(self.engine.state_path)
        if not state.last_run_id:
            return None
        summary = self.engine.extract_prev_output(state.last_run_id)
        self._recent_summary_cache = summary
        return summary

    def _doc_bases(
        self, drafts: dict[str, DocChatDraftState]
    ) -> dict[str, dict[str, str]]:
        config = self._repo_config()
        bases: dict[str, dict[str, str]] = {}
        for kind in ALLOWED_DOC_KINDS:
            draft = drafts.get(kind)
            if draft is not None:
                bases[kind] = {"content": draft.content, "source": "draft"}
            else:
                bases[kind] = {
                    "content": config.doc_path(kind).read_text(encoding="utf-8"),
                    "source": "disk",
                }
        return bases

    def _build_app_server_prompt(
        self, request: DocChatRequest, docs: dict[str, dict[str, str]]
    ) -> str:
        return build_doc_chat_prompt(
            self.engine.config,
            message=request.message,
            recent_summary=self._recent_run_summary(),
            docs=docs,
            targets=request.targets,
        )

    def _ensure_app_server(self) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is None:
            raise DocChatError("App-server backend is not configured")
        return self._app_server_supervisor

    def _thread_key(self) -> str:
        return DOC_CHAT_KEY

    def _legacy_thread_id(self) -> Optional[str]:
        try:
            threads = self._app_server_threads.load()
        except Exception:
            return None
        for key, value in threads.items():
            if not key.startswith(DOC_CHAT_PREFIX):
                continue
            if isinstance(value, str) and value:
                return value
        return None

    def _apply_patch_to_drafts(
        self,
        *,
        patch_text_raw: str,
        drafts: dict[str, DocChatDraftState],
        docs: dict[str, dict[str, str]],
        agent_message: str,
        allowed_kinds: Optional[tuple[str, ...]] = None,
    ) -> tuple[dict[str, DocChatDraftState], list[str], dict[str, dict]]:
        config = self._repo_config()
        targets = self._doc_targets()
        if allowed_kinds:
            targets = {
                kind: path for kind, path in targets.items() if kind in allowed_kinds
            }
        allowed_paths = list(targets.values())
        patch_text, raw_targets = normalize_patch_text(patch_text_raw)
        normalized_targets = ensure_patch_targets_allowed(raw_targets, allowed_paths)
        path_to_kind = {path: kind for kind, path in targets.items()}
        base_content = {path: docs[kind]["content"] for kind, path in targets.items()}
        preview = preview_patch(
            self.engine.repo_root,
            patch_text,
            raw_targets,
            base_content=base_content,
        )
        updated = dict(drafts)
        updated_kinds: list[str] = []
        payloads: dict[str, dict] = {}
        created_at = now_iso()
        for target in normalized_targets:
            kind = path_to_kind.get(target)
            if kind is None:
                continue
            before = base_content.get(target, "")
            after = preview.get(target, before)
            patch_for_doc = self._build_patch(target, before, after)
            if not patch_for_doc.strip():
                continue
            base_hash = self._hash_content(before)
            existing = drafts.get(kind)
            if existing and docs.get(kind, {}).get("source") == "draft":
                if existing.base_hash:
                    base_hash = existing.base_hash
                else:
                    try:
                        base_hash = self._hash_content(
                            config.doc_path(kind).read_text(encoding="utf-8")
                        )
                    except OSError:
                        base_hash = self._hash_content(before)
            updated[kind] = DocChatDraftState(
                content=after,
                patch=patch_for_doc,
                agent_message=agent_message,
                created_at=created_at,
                base_hash=base_hash,
            )
            updated_kinds.append(kind)
            payloads[kind] = updated[kind].to_dict()
        return updated, updated_kinds, payloads

    @staticmethod
    def _parse_agent_message(output: str) -> str:
        text = (output or "").strip()
        if not text:
            return "Updated docs via doc chat."
        for line in text.splitlines():
            if line.lower().startswith("agent:"):
                return line[len("agent:") :].strip() or "Updated docs via doc chat."
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

    def _load_drafts(self) -> dict[str, DocChatDraftState]:
        if not self._drafts_path.exists():
            return {}
        try:
            payload = json.loads(self._drafts_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        raw_drafts = payload.get("drafts")
        if not isinstance(raw_drafts, dict):
            return {}
        drafts: dict[str, DocChatDraftState] = {}
        for kind, entry in raw_drafts.items():
            if kind not in ALLOWED_DOC_KINDS:
                continue
            draft = DocChatDraftState.from_dict(entry)
            if draft is not None:
                drafts[kind] = draft
        return drafts

    def _save_drafts(self, drafts: dict[str, DocChatDraftState]) -> None:
        payload = {
            "version": DOC_CHAT_STATE_VERSION,
            "drafts": {kind: draft.to_dict() for kind, draft in drafts.items()},
        }
        atomic_write(self._drafts_path, json.dumps(payload, indent=2) + "\n")

    def _doc_targets(self) -> dict[str, str]:
        config = self._repo_config()
        targets = {}
        for kind in ALLOWED_DOC_KINDS:
            targets[kind] = str(
                config.doc_path(kind).relative_to(self.engine.repo_root)
            )
        return targets

    def _hash_content(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _build_patch(self, rel_path: str, before: str, after: str) -> str:
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
        return "\n".join(diff)

    def apply_saved_patch(self, kind: str) -> str:
        key = _normalize_kind(kind)
        drafts = self._load_drafts()
        draft = drafts.get(key)
        if draft is None:
            raise DocChatError("No pending patch")
        config = self._repo_config()
        target_path = config.doc_path(key)
        current = target_path.read_text(encoding="utf-8")
        if draft.base_hash and self._hash_content(current) != draft.base_hash:
            raise DocChatConflictError(
                "Doc changed since draft created; reload before applying."
            )
        atomic_write(target_path, draft.content)
        drafts.pop(key, None)
        self._save_drafts(drafts)
        return target_path.read_text(encoding="utf-8")

    def discard_patch(self, kind: str) -> str:
        key = _normalize_kind(kind)
        drafts = self._load_drafts()
        drafts.pop(key, None)
        self._save_drafts(drafts)
        config = self._repo_config()
        return config.doc_path(key).read_text(encoding="utf-8")

    def pending_patch(self, kind: str) -> Optional[dict]:
        key = _normalize_kind(kind)
        drafts = self._load_drafts()
        draft = drafts.get(key)
        if draft is None:
            return None
        return {
            "status": "ok",
            "kind": key,
            "patch": draft.patch,
            "agent_message": draft.agent_message or "Draft ready",
            "content": draft.content,
            "created_at": draft.created_at,
            "base_hash": draft.base_hash,
        }

    async def _execute_app_server(
        self,
        request: DocChatRequest,
        *,
        on_turn_start: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> dict:
        chat_id = self._chat_id()
        started_at = time.time()
        doc_pointer = self._doc_pointer(request.targets)
        message_for_log = self._compact_message(request.message)
        turn_id: Optional[str] = None
        thread_id: Optional[str] = None
        targets_label = ",".join(request.targets or ()) or "auto"
        drafts = self._load_drafts()
        docs = self._doc_bases(drafts)
        self._log(
            chat_id,
            f'start targets={targets_label} path={doc_pointer} message="{message_for_log}"',
        )
        try:
            supervisor = self._ensure_app_server()
            client = await supervisor.get_client(self.engine.repo_root)
            key = self._thread_key()
            thread_id = self._app_server_threads.get_thread_id(key)
            if not thread_id:
                legacy = self._legacy_thread_id()
                if legacy:
                    thread_id = legacy
                    try:
                        self._app_server_threads.set_thread_id(key, thread_id)
                    except Exception:
                        pass
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
            prompt = self._build_app_server_prompt(request, docs)
            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy="never",
                sandbox_policy="readOnly",
            )
            turn_id = handle.turn_id
            thread_id = handle.thread_id
            active = self._register_active_turn(client, turn_id, thread_id)
            await self._handle_turn_start(
                thread_id,
                turn_id,
                on_turn_start=on_turn_start,
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
                            f"targets={targets_label} path={doc_pointer} "
                            f"duration_ms={duration_ms} "
                            f'message="{message_for_log}" backend=app_server',
                        )
                        return {
                            "status": "interrupted",
                            "detail": "Doc chat interrupted",
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                        }
                turn_result = await turn_task
            finally:
                self._clear_active_turn(handle.turn_id)
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
                    f"targets={targets_label} path={doc_pointer} "
                    f"duration_ms={duration_ms} "
                    f'message="{message_for_log}" backend=app_server',
                )
                return {
                    "status": "interrupted",
                    "detail": "Doc chat interrupted",
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
            if turn_result.errors:
                raise DocChatError(turn_result.errors[-1])
            output = "\n".join(turn_result.agent_messages).strip()
            message_text, patch_text_raw = self._split_patch_from_output(output)
            if not patch_text_raw.strip():
                raise DocChatError("App-server output missing a patch")
            agent_message = self._parse_agent_message(message_text or output)
            try:
                updated_drafts, updated_kinds, payloads = self._apply_patch_to_drafts(
                    patch_text_raw=patch_text_raw,
                    drafts=drafts,
                    docs=docs,
                    agent_message=agent_message,
                    allowed_kinds=request.targets,
                )
            except PatchError as exc:
                raise DocChatError(str(exc)) from exc
            self._save_drafts(updated_drafts)
            duration_ms = int((time.time() - started_at) * 1000)
            self._log(
                chat_id,
                "result=success "
                f"targets={targets_label} path={doc_pointer} "
                f"duration_ms={duration_ms} "
                f'message="{message_for_log}" backend=app_server',
            )
            return {
                "status": "ok",
                "agent_message": agent_message,
                "updated": updated_kinds,
                "drafts": payloads,
                "thread_id": thread_id,
                "turn_id": turn_id,
            }
        except asyncio.TimeoutError:
            duration_ms = int((time.time() - started_at) * 1000)
            self._log(
                chat_id,
                "result=error "
                f"targets={targets_label} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="timeout" backend=app_server',
            )
            return {
                "status": "error",
                "detail": "Doc chat agent timed out",
                "thread_id": thread_id,
                "turn_id": turn_id,
            }
        except DocChatError as exc:
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error "
                f"targets={targets_label} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="{detail}" backend=app_server',
            )
            return {
                "status": "error",
                "detail": str(exc),
                "thread_id": thread_id,
                "turn_id": turn_id,
            }
        except Exception as exc:  # pragma: no cover - defensive
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error kind={kind} path={path} duration_ms={duration_ms} "
                'message="{message}" detail="{detail}" backend=app_server'.format(
                    kind=targets_label,
                    path=doc_pointer,
                    duration_ms=duration_ms,
                    message=message_for_log,
                    detail=detail,
                ),
            )
            return {
                "status": "error",
                "detail": "Doc chat failed",
                "thread_id": thread_id,
                "turn_id": turn_id,
            }

    async def execute(
        self,
        request: DocChatRequest,
        *,
        on_turn_start: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> dict:
        if on_turn_start is None:
            return await self._execute_app_server(request)
        return await self._execute_app_server(request, on_turn_start=on_turn_start)

    async def stream(self, request: DocChatRequest) -> AsyncIterator[str]:
        try:
            async with self.doc_lock():
                yield format_sse("status", {"status": "queued"})
                try:
                    turn_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)

                    async def _on_turn_start(thread_id: str, turn_id: str) -> None:
                        payload = {
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "targets": list(request.targets or ()),
                        }
                        if turn_queue.full():
                            return
                        await turn_queue.put(payload)

                    execute_task = asyncio.create_task(
                        self.execute(request, on_turn_start=_on_turn_start)
                    )
                    turn_task = asyncio.create_task(turn_queue.get())
                    done, pending = await asyncio.wait(
                        {execute_task, turn_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if turn_task in done:
                        yield format_sse("turn", turn_task.result())
                    else:
                        turn_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await turn_task
                    if execute_task in done:
                        result = execute_task.result()
                    else:
                        result = await execute_task
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
