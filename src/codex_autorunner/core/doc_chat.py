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

from ..agents.opencode.supervisor import OpenCodeSupervisor
from ..integrations.app_server.client import CodexAppServerError
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .app_server_events import AppServerEventBuffer
from .app_server_prompts import build_doc_chat_prompt
from .app_server_threads import (
    DOC_CHAT_KEY,
    DOC_CHAT_OPENCODE_KEY,
    DOC_CHAT_PREFIX,
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .config import RepoConfig
from .docs import validate_todo_markdown
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
    context_doc: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None


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
        opencode_supervisor: Optional[OpenCodeSupervisor] = None,
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
        self._opencode_supervisor = opencode_supervisor
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
            if not hasattr(active.client, "turn_interrupt"):
                return
            await asyncio.wait_for(
                active.client.turn_interrupt(
                    active.turn_id, thread_id=active.thread_id
                ),
                timeout=DOC_CHAT_INTERRUPT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            self._log(
                chat_id,
                'result=error detail="interrupt_timeout" backend=app_server',
            )
        except CodexAppServerError as exc:
            self._log(
                chat_id,
                "result=error "
                f'detail="interrupt_failed:{self._compact_message(str(exc))}" '
                "backend=app_server",
            )

    async def _abort_opencode(self, active: ActiveDocChatTurn, thread_id: str) -> None:
        if active.interrupt_sent:
            return
        active.interrupt_sent = True
        chat_id = self._chat_id()
        try:
            if not hasattr(active.client, "abort"):
                return
            await asyncio.wait_for(
                active.client.abort(thread_id),
                timeout=DOC_CHAT_INTERRUPT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            self._log(
                chat_id,
                'result=error detail="abort_timeout" backend=opencode',
            )
        except Exception as exc:
            self._log(
                chat_id,
                "result=error "
                f'detail="abort_failed:{self._compact_message(str(exc))}" '
                "backend=opencode",
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
        raw_context = (
            payload.get("context_doc")
            or payload.get("contextDoc")
            or payload.get("viewing")
        )
        raw_agent = payload.get("agent")
        raw_model = payload.get("model")
        raw_reasoning = payload.get("reasoning")
        context_doc: Optional[str] = None
        if isinstance(raw_context, str) and raw_context.strip():
            try:
                context_doc = _normalize_kind(raw_context)
            except DocChatValidationError:
                raise
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
            if context_doc is None:
                context_doc = normalized_kind
            elif context_doc != normalized_kind:
                raise DocChatValidationError("context_doc must match doc kind")
        return DocChatRequest(
            message=message,
            stream=stream,
            targets=targets,
            context_doc=context_doc,
            agent=str(raw_agent).strip() if isinstance(raw_agent, str) else None,
            model=str(raw_model).strip() if isinstance(raw_model, str) else None,
            reasoning=(
                str(raw_reasoning).strip() if isinstance(raw_reasoning, str) else None
            ),
        )

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

    def _log_output(
        self, chat_id: str, text: Optional[str], label: str = "stdout"
    ) -> None:
        if text is None:
            return
        lines = text.splitlines()
        if not lines:
            self._log(chat_id, f"{label}: ")
            return
        for line in lines:
            self._log(chat_id, f"{label}: {line}")

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

    def _prepare_docs_for_run(
        self, drafts: dict[str, DocChatDraftState]
    ) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str]]:
        config = self._repo_config()
        docs = self._doc_bases(drafts)
        backups: dict[str, str] = {}
        working: dict[str, str] = {}
        for kind in ALLOWED_DOC_KINDS:
            path = config.doc_path(kind)
            current = path.read_text(encoding="utf-8")
            backups[kind] = current
            desired = docs.get(kind, {}).get("content", current)
            working[kind] = desired
            if desired != current:
                atomic_write(path, desired)
        return docs, backups, working

    def _restore_docs(self, backups: dict[str, str]) -> None:
        config = self._repo_config()
        for kind, content in backups.items():
            path = config.doc_path(kind)
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                current = ""
            if current != content:
                atomic_write(path, content)

    def _build_app_server_prompt(
        self, request: DocChatRequest, docs: dict[str, dict[str, str]]
    ) -> str:
        return build_doc_chat_prompt(
            self.engine.config,
            message=request.message,
            recent_summary=self._recent_run_summary(),
            docs=docs,
            context_doc=request.context_doc,
        )

    def _ensure_app_server(self) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is None:
            raise DocChatError("App-server backend is not configured")
        return self._app_server_supervisor

    def _ensure_opencode(self) -> OpenCodeSupervisor:
        if self._opencode_supervisor is None:
            raise DocChatError("OpenCode backend is not configured")
        return self._opencode_supervisor

    def _thread_key(self, agent: Optional[str]) -> str:
        if (agent or "").strip().lower() == "opencode":
            return DOC_CHAT_OPENCODE_KEY
        return DOC_CHAT_KEY

    def _legacy_thread_id(self, agent: Optional[str]) -> Optional[str]:
        if (agent or "").strip().lower() == "opencode":
            return None
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
    def _looks_like_patch(cls, text: str) -> bool:
        if not text:
            return False
        markers = (
            "*** Begin Patch",
            "--- ",
            "diff --git ",
            "Index: ",
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _extract_fenced_patch(cls, output: str) -> Optional[Tuple[str, str]]:
        for match in re.finditer(
            r"```[^\n]*\n(.*?)```", output, flags=re.DOTALL | re.IGNORECASE
        ):
            candidate = (match.group(1) or "").strip()
            if not cls._looks_like_patch(candidate):
                continue
            before = output[: match.start()].strip()
            after = output[match.end() :].strip()
            message_text = "\n".join(part for part in [before, after] if part)
            return message_text, candidate
        return None

    @staticmethod
    def _strip_trailing_fence(text: str) -> str:
        lines = text.strip().splitlines()
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @classmethod
    def _split_patch_from_output(cls, output: str) -> Tuple[str, str]:
        if not output:
            return "", ""
        match = re.search(
            r"<(PATCH|APPLY_PATCH)>(.*?)</\1>",
            output,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            patch_text = cls._strip_code_fences(match.group(2))
            before = output[: match.start()].strip()
            after = output[match.end() :].strip()
            message_text = "\n".join(part for part in [before, after] if part)
            return message_text, patch_text
        fenced = cls._extract_fenced_patch(output)
        if fenced:
            message_text, patch_text = fenced
            return message_text, patch_text
        lines = output.splitlines()
        start_idx = None
        for idx, line in enumerate(lines):
            if (
                line.startswith("--- ")
                or line.startswith("*** Begin Patch")
                or line.startswith("diff --git ")
                or line.startswith("Index: ")
            ):
                start_idx = idx
                break
        if start_idx is None:
            return output.strip(), ""
        message_text = "\n".join(lines[:start_idx]).strip()
        patch_text = "\n".join(lines[start_idx:]).strip()
        patch_text = cls._strip_trailing_fence(cls._strip_code_fences(patch_text))
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
        docs: dict[str, dict[str, str]] = {}
        backups: dict[str, str] = {}
        working: dict[str, str] = {}
        self._log(
            chat_id,
            f'start targets={targets_label} path={doc_pointer} message="{message_for_log}"',
        )
        try:
            docs, backups, working = self._prepare_docs_for_run(drafts)
            supervisor = self._ensure_app_server()
            client = await supervisor.get_client(self.engine.repo_root)
            key = self._thread_key(request.agent)
            thread_id = self._app_server_threads.get_thread_id(key)
            if not thread_id:
                legacy = self._legacy_thread_id(request.agent)
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
            turn_kwargs: dict[str, Any] = {}
            if request.model:
                turn_kwargs["model"] = request.model
            if request.reasoning:
                turn_kwargs["effort"] = request.reasoning
            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy="on-request",
                sandbox_policy="dangerFullAccess",
                **turn_kwargs,
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
            return self._finalize_doc_chat_output(
                output=output,
                drafts=drafts,
                docs=docs,
                backups=backups,
                working=working,
                started_at=started_at,
                chat_id=chat_id,
                targets_label=targets_label,
                doc_pointer=doc_pointer,
                message_for_log=message_for_log,
                thread_id=thread_id,
                turn_id=turn_id,
                backend="app_server",
            )
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
        finally:
            if backups:
                self._restore_docs(backups)

    async def _execute_opencode(
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
        docs: dict[str, dict[str, str]] = {}
        backups: dict[str, str] = {}
        working: dict[str, str] = {}
        self._log(
            chat_id,
            f'start targets={targets_label} path={doc_pointer} message="{message_for_log}"',
        )
        try:
            docs, backups, working = self._prepare_docs_for_run(drafts)
            supervisor = self._ensure_opencode()
            client = await supervisor.get_client(self.engine.repo_root)
            key = self._thread_key(request.agent)
            thread_id = self._app_server_threads.get_thread_id(key)
            if thread_id:
                try:
                    await client.get_session(thread_id)
                except Exception:
                    self._app_server_threads.reset_thread(key)
                    thread_id = None
            if not thread_id:
                session = await client.create_session(
                    directory=str(self.engine.repo_root)
                )
                thread_id = self._extract_opencode_session_id(session)
                if not isinstance(thread_id, str) or not thread_id:
                    raise DocChatError("OpenCode did not return a session id")
                self._app_server_threads.set_thread_id(key, thread_id)
            prompt = self._build_app_server_prompt(request, docs)
            model_payload = self._split_opencode_model(request.model)
            result = await client.send_message(
                thread_id,
                message=prompt,
                model=model_payload,
                variant=request.reasoning,
            )
            turn_id = self._extract_opencode_turn_id(thread_id, result)
            active = self._register_active_turn(client, turn_id, thread_id)
            await self._handle_turn_start(
                thread_id,
                turn_id,
                on_turn_start=on_turn_start,
            )
            output_task = asyncio.create_task(
                self._collect_opencode_output(client, thread_id, active)
            )
            timeout_task = asyncio.create_task(asyncio.sleep(DOC_CHAT_TIMEOUT_SECONDS))
            interrupt_task = asyncio.create_task(active.interrupt_event.wait())
            try:
                tasks = {output_task, timeout_task, interrupt_task}
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                if timeout_task in done:
                    output_task.add_done_callback(lambda task: task.exception())
                    raise asyncio.TimeoutError()
                if interrupt_task in done:
                    active.interrupted = True
                    active.interrupt_event.set()
                    await self._abort_opencode(active, thread_id)
                    done, _pending = await asyncio.wait(
                        {output_task}, timeout=DOC_CHAT_INTERRUPT_GRACE_SECONDS
                    )
                    if not done:
                        output_task.add_done_callback(lambda task: task.exception())
                        duration_ms = int((time.time() - started_at) * 1000)
                        self._log(
                            chat_id,
                            "result=interrupted "
                            f"targets={targets_label} path={doc_pointer} "
                            f"duration_ms={duration_ms} "
                            f'message="{message_for_log}" backend=opencode',
                        )
                        return {
                            "status": "interrupted",
                            "detail": "Doc chat interrupted",
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                        }
                output = await output_task
            finally:
                self._clear_active_turn(turn_id)
                timeout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timeout_task
                interrupt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await interrupt_task
            return self._finalize_doc_chat_output(
                output=output,
                drafts=drafts,
                docs=docs,
                backups=backups,
                working=working,
                started_at=started_at,
                chat_id=chat_id,
                targets_label=targets_label,
                doc_pointer=doc_pointer,
                message_for_log=message_for_log,
                thread_id=thread_id,
                turn_id=turn_id,
                backend="opencode",
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.time() - started_at) * 1000)
            self._log(
                chat_id,
                "result=error "
                f"targets={targets_label} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="timeout" backend=opencode',
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
                f'message="{message_for_log}" detail="{detail}" backend=opencode',
            )
            return {
                "status": "error",
                "detail": str(exc),
                "thread_id": thread_id,
                "turn_id": turn_id,
            }
        except Exception as exc:
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error "
                f"targets={targets_label} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="{detail}" backend=opencode',
            )
            return {
                "status": "error",
                "detail": "Doc chat failed",
                "thread_id": thread_id,
                "turn_id": turn_id,
            }
        finally:
            if backups:
                self._restore_docs(backups)

    def _split_opencode_model(self, model: Optional[str]) -> Optional[dict[str, str]]:
        if not model or "/" not in model:
            return None
        provider_id, model_id = model.split("/", 1)
        provider_id = provider_id.strip()
        model_id = model_id.strip()
        if not provider_id or not model_id:
            return None
        return {"providerID": provider_id, "modelID": model_id}

    def _extract_opencode_turn_id(self, session_id: str, payload: Any) -> str:
        # Fallback: placeholder for tracking since events filter by session_id only
        if isinstance(payload, dict):
            for key in ("id", "messageId", "message_id", "turn_id", "turnId"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        return f"{session_id}:{int(time.time() * 1000)}"

    def _extract_opencode_session_id(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        for key in ("sessionID", "sessionId", "session_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        properties = payload.get("properties")
        if isinstance(properties, dict):
            value = properties.get("sessionID")
            if isinstance(value, str) and value:
                return value
            part = properties.get("part")
            if isinstance(part, dict):
                value = part.get("sessionID")
                if isinstance(value, str) and value:
                    return value
        session = payload.get("session")
        if isinstance(session, dict):
            return self._extract_opencode_session_id(session)
        return None

    async def _collect_opencode_output(
        self, client: Any, session_id: str, active: ActiveDocChatTurn
    ) -> str:
        text_parts: list[str] = []
        async for event in client.stream_events(directory=str(self.engine.repo_root)):
            raw = event.data or ""
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            event_session_id = self._extract_opencode_session_id(payload)
            if event_session_id and event_session_id != session_id:
                continue
            if event.event == "permission.asked":
                properties = (
                    payload.get("properties") if isinstance(payload, dict) else {}
                )
                request_id = None
                if isinstance(properties, dict):
                    request_id = properties.get("id") or properties.get("requestID")
                if isinstance(request_id, str) and request_id:
                    try:
                        await client.respond_permission(
                            request_id=request_id, reply="reject"
                        )
                    except Exception:
                        pass
            if event.event == "message.part.updated":
                properties = (
                    payload.get("properties") if isinstance(payload, dict) else None
                )
                if isinstance(properties, dict):
                    part = properties.get("part")
                    delta = properties.get("delta")
                else:
                    part = payload.get("part")
                    delta = payload.get("delta")
                if isinstance(delta, dict):
                    delta = delta.get("text")
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
            if event.event == "session.idle" and event_session_id == session_id:
                break
            if active.interrupted:
                break
        return "".join(text_parts).strip()

    def _finalize_doc_chat_output(
        self,
        *,
        output: str,
        drafts: dict[str, DocChatDraftState],
        docs: dict[str, dict[str, str]],
        backups: dict[str, str],
        working: dict[str, str],
        started_at: float,
        chat_id: str,
        targets_label: str,
        doc_pointer: str,
        message_for_log: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
        backend: str,
    ) -> dict:
        message_text, patch_text_raw = self._split_patch_from_output(output)
        agent_message = self._parse_agent_message(message_text or output)
        response_text = message_text.strip() or output.strip() or agent_message
        updated = dict(drafts)
        updated_kinds: list[str] = []
        payloads: dict[str, dict] = {}
        created_at = now_iso()
        allowed_kinds = ALLOWED_DOC_KINDS
        unexpected: list[str] = []
        config = self._repo_config()
        self._log_output(chat_id, response_text or output)
        for kind in ALLOWED_DOC_KINDS:
            path = config.doc_path(kind)
            after = path.read_text(encoding="utf-8")
            before = working.get(kind, backups.get(kind, ""))
            if kind not in allowed_kinds:
                if after != before:
                    unexpected.append(kind)
                continue
            if after == before:
                continue
            rel_path = str(path.relative_to(self.engine.repo_root))
            patch_for_doc = self._build_patch(rel_path, before, after)
            if not patch_for_doc.strip():
                continue
            base_hash = self._hash_content(backups.get(kind, before))
            existing = drafts.get(kind)
            if existing and docs.get(kind, {}).get("source") == "draft":
                if existing.base_hash:
                    base_hash = existing.base_hash
            updated[kind] = DocChatDraftState(
                content=after,
                patch=patch_for_doc,
                agent_message=agent_message,
                created_at=created_at,
                base_hash=base_hash,
            )
            updated_kinds.append(kind)
            payloads[kind] = updated[kind].to_dict()
        if unexpected:
            raise DocChatError(
                "Doc chat updated unexpected docs: " + ", ".join(unexpected)
            )
        if patch_text_raw.strip() and not payloads:
            try:
                updated, updated_kinds, payloads = self._apply_patch_to_drafts(
                    patch_text_raw=patch_text_raw,
                    drafts=updated,
                    docs=docs,
                    agent_message=agent_message,
                    allowed_kinds=allowed_kinds,
                )
            except PatchError as exc:
                raise DocChatError(str(exc)) from exc
            if not payloads:
                raise DocChatError("Doc chat patch did not produce updates")
        if "todo" in payloads:
            todo_content = payloads["todo"].get("content", "")
            if not isinstance(todo_content, str):
                raise DocChatError("Invalid TODO draft content")
            todo_errors = validate_todo_markdown(todo_content)
            if todo_errors:
                raise DocChatError("Invalid TODO format: " + "; ".join(todo_errors))
        if payloads:
            self._save_drafts(updated)
        duration_ms = int((time.time() - started_at) * 1000)
        self._log(
            chat_id,
            "result=success "
            f"targets={targets_label} path={doc_pointer} "
            f"duration_ms={duration_ms} "
            f'message="{message_for_log}" backend={backend}',
        )
        return {
            "status": "ok",
            "agent_message": agent_message,
            "message": response_text,
            "updated": updated_kinds,
            "drafts": payloads,
            "thread_id": thread_id,
            "turn_id": turn_id,
        }

    async def execute(
        self,
        request: DocChatRequest,
        *,
        on_turn_start: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> dict:
        if (request.agent or "").strip().lower() == "opencode":
            if on_turn_start is None:
                return await self._execute_opencode(request)
            return await self._execute_opencode(request, on_turn_start=on_turn_start)
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
                        if request.agent:
                            payload["agent"] = request.agent
                        if request.model:
                            payload["model"] = request.model
                        if request.reasoning:
                            payload["reasoning"] = request.reasoning
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
                        yield format_sse("status", {"status": "running"})
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
