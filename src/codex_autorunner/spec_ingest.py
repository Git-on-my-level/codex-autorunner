import asyncio
import contextlib
import re
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .core.app_server_events import AppServerEventBuffer
from .core.app_server_prompts import (
    build_spec_ingest_prompt as build_app_server_spec_ingest_prompt,
)
from .core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .core.engine import Engine
from .core.locks import FileLock, FileLockBusy, FileLockError
from .core.patch_utils import (
    PatchError,
    apply_patch_file,
    ensure_patch_targets_allowed,
    normalize_patch_text,
    preview_patch,
)
from .core.utils import atomic_write
from .integrations.app_server.client import CodexAppServerError
from .integrations.app_server.supervisor import WorkspaceAppServerSupervisor

SPEC_INGEST_TIMEOUT_SECONDS = 240
SPEC_INGEST_INTERRUPT_GRACE_SECONDS = 10
SPEC_INGEST_PATCH_NAME = "spec-ingest.patch"


class SpecIngestError(Exception):
    """Raised when ingesting a SPEC fails."""


@dataclass
class ActiveSpecIngestTurn:
    thread_id: str
    turn_id: str
    client: Any
    interrupted: bool = False
    interrupt_sent: bool = False
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)


def ensure_can_overwrite(engine: Engine, force: bool) -> None:
    if force:
        return
    for key in ("todo", "progress", "opinions"):
        existing = engine.docs.read_doc(key).strip()
        if existing:
            raise SpecIngestError(
                "TODO/PROGRESS/OPINIONS already contain content; rerun with --force to overwrite"
            )


def clear_work_docs(engine: Engine) -> Dict[str, str]:
    defaults = {
        "todo": "# TODO\n\n",
        "progress": "# Progress\n\n",
        "opinions": "# Opinions\n\n",
    }
    for key, content in defaults.items():
        atomic_write(engine.config.doc_path(key), content)
    # Read back to reflect actual on-disk content.
    return {k: engine.docs.read_doc(k) for k in defaults.keys()}


class SpecIngestService:
    def __init__(
        self,
        engine: Engine,
        *,
        app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
        app_server_threads: Optional[AppServerThreadRegistry] = None,
        app_server_events: Optional[AppServerEventBuffer] = None,
    ) -> None:
        self.engine = engine
        self._app_server_supervisor = app_server_supervisor
        self._app_server_threads = app_server_threads or AppServerThreadRegistry(
            default_app_server_threads_path(self.engine.repo_root)
        )
        self._app_server_events = app_server_events
        self.patch_path = (
            self.engine.repo_root / ".codex-autorunner" / SPEC_INGEST_PATCH_NAME
        )
        self.last_agent_message: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_path = (
            self.engine.repo_root / ".codex-autorunner" / "locks" / "spec_ingest.lock"
        )
        self._thread_lock = threading.Lock()
        self._active_turn: Optional[ActiveSpecIngestTurn] = None
        self._active_turn_lock = threading.Lock()
        self._pending_interrupt = False

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            try:
                self._lock = asyncio.Lock()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
                self._lock = asyncio.Lock()
        return self._lock

    def _ingest_busy(self) -> bool:
        lock = self._ensure_lock()
        if lock.locked():
            return True
        file_lock = FileLock(self._lock_path)
        try:
            file_lock.acquire(blocking=False)
        except FileLockBusy:
            return True
        except FileLockError:
            return True
        finally:
            file_lock.release()
        return False

    @asynccontextmanager
    async def ingest_lock(self):
        if not self._thread_lock.acquire(blocking=False):
            raise SpecIngestError("Spec ingest is already running")
        lock = self._ensure_lock()
        if lock.locked():
            self._thread_lock.release()
            raise SpecIngestError("Spec ingest is already running")
        await lock.acquire()
        file_lock = FileLock(self._lock_path)
        try:
            try:
                file_lock.acquire(blocking=False)
            except FileLockBusy as exc:
                raise SpecIngestError("Spec ingest is already running") from exc
            except FileLockError as exc:
                raise SpecIngestError(str(exc)) from exc
            yield
        finally:
            file_lock.release()
            lock.release()
            self._thread_lock.release()
            with self._active_turn_lock:
                self._pending_interrupt = False

    @contextmanager
    def _patch_lock(self):
        if not self._thread_lock.acquire(blocking=False):
            raise SpecIngestError("Spec ingest is already running")
        lock = self._ensure_lock()
        if lock.locked():
            self._thread_lock.release()
            raise SpecIngestError("Spec ingest is already running")
        file_lock = FileLock(self._lock_path)
        try:
            file_lock.acquire(blocking=False)
        except FileLockBusy as exc:
            self._thread_lock.release()
            raise SpecIngestError("Spec ingest is already running") from exc
        except FileLockError as exc:
            self._thread_lock.release()
            raise SpecIngestError(str(exc)) from exc
        try:
            yield
        finally:
            file_lock.release()
            self._thread_lock.release()

    def _ensure_app_server(self) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is None:
            raise SpecIngestError("App-server backend is not configured")
        return self._app_server_supervisor

    def _get_active_turn(self) -> Optional[ActiveSpecIngestTurn]:
        with self._active_turn_lock:
            return self._active_turn

    def _clear_active_turn(self, turn_id: str) -> None:
        with self._active_turn_lock:
            if self._active_turn and self._active_turn.turn_id == turn_id:
                self._active_turn = None

    def _register_active_turn(
        self, client: Any, turn_id: str, thread_id: str
    ) -> ActiveSpecIngestTurn:
        interrupt_event = asyncio.Event()
        active = ActiveSpecIngestTurn(
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

    async def _interrupt_turn(self, active: ActiveSpecIngestTurn) -> None:
        if active.interrupt_sent:
            return
        active.interrupt_sent = True
        try:
            await asyncio.wait_for(
                active.client.turn_interrupt(
                    active.turn_id, thread_id=active.thread_id
                ),
                timeout=SPEC_INGEST_INTERRUPT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
        except CodexAppServerError:
            pass

    async def interrupt(self) -> Dict[str, str]:
        active = self._get_active_turn()
        if active is None:
            pending = self._ingest_busy()
            with self._active_turn_lock:
                self._pending_interrupt = pending
            return self._assemble_response(
                {},
                status="interrupted",
                agent_message="Spec ingest interrupted",
            )
        active.interrupted = True
        active.interrupt_event.set()
        await self._interrupt_turn(active)
        return self._assemble_response(
            {},
            status="interrupted",
            agent_message="Spec ingest interrupted",
        )

    def _allowed_targets(self) -> Dict[str, str]:
        config = self.engine.config
        rel = {}
        for key in ("todo", "progress", "opinions"):
            rel[key] = str(config.doc_path(key).relative_to(self.engine.repo_root))
        return rel

    def _spec_path(self, spec_path: Optional[Path]) -> Path:
        target = spec_path or self.engine.config.doc_path("spec")
        if not target.exists():
            raise SpecIngestError(f"SPEC not found at {target}")
        text = target.read_text(encoding="utf-8")
        if not text.strip():
            raise SpecIngestError(f"SPEC at {target} is empty")
        return target

    def _assemble_response(
        self,
        docs: Dict[str, str],
        *,
        patch: Optional[str] = None,
        agent_message: Optional[str] = None,
        status: str = "ok",
    ) -> Dict[str, str]:
        return {
            "status": status,
            "todo": docs.get("todo", self.engine.docs.read_doc("todo")),
            "progress": docs.get("progress", self.engine.docs.read_doc("progress")),
            "opinions": docs.get("opinions", self.engine.docs.read_doc("opinions")),
            "spec": self.engine.docs.read_doc("spec"),
            "summary": self.engine.docs.read_doc("summary"),
            "patch": patch or "",
            "agent_message": agent_message or "",
        }

    def pending_patch(self) -> Optional[Dict[str, str]]:
        with self._patch_lock():
            if not self.patch_path.exists():
                return None
            patch_text_raw = self.patch_path.read_text(encoding="utf-8")
            targets = self._allowed_targets()
            try:
                patch_text, raw_targets = normalize_patch_text(patch_text_raw)
                ensure_patch_targets_allowed(raw_targets, targets.values())
                preview = preview_patch(self.engine.repo_root, patch_text, raw_targets)
            except PatchError:
                return None
            docs = {
                key: preview.get(path, self.engine.docs.read_doc(key))
                for key, path in targets.items()
            }
            return self._assemble_response(
                docs, patch=patch_text, agent_message=self.last_agent_message
            )

    def apply_patch(self) -> Dict[str, str]:
        with self._patch_lock():
            if not self.patch_path.exists():
                raise SpecIngestError("No pending spec ingest patch")
            patch_text_raw = self.patch_path.read_text(encoding="utf-8")
            targets = self._allowed_targets()
            try:
                patch_text, raw_targets = normalize_patch_text(patch_text_raw)
                ensure_patch_targets_allowed(raw_targets, targets.values())
                self.patch_path.write_text(patch_text, encoding="utf-8")
                apply_patch_file(self.engine.repo_root, self.patch_path, raw_targets)
            except PatchError as exc:
                raise SpecIngestError(str(exc)) from exc
            self.patch_path.unlink(missing_ok=True)
            return self._assemble_response(
                {
                    key: self.engine.docs.read_doc(key)
                    for key in ("todo", "progress", "opinions")
                }
            )

    def discard_patch(self) -> Dict[str, str]:
        with self._patch_lock():
            if self.patch_path.exists():
                self.patch_path.unlink(missing_ok=True)
            return self._assemble_response(
                {
                    key: self.engine.docs.read_doc(key)
                    for key in ("todo", "progress", "opinions")
                }
            )

    async def _execute_app_server(
        self,
        *,
        force: bool,
        spec_path: Optional[Path],
        message: Optional[str],
    ) -> Dict[str, str]:
        if not force:
            ensure_can_overwrite(self.engine, force=False)
        spec_target = self._spec_path(spec_path)
        prompt = build_app_server_spec_ingest_prompt(
            self.engine.config,
            message=message or "Ingest SPEC into TODO/PROGRESS/OPINIONS.",
            spec_path=spec_target,
        )
        supervisor = self._ensure_app_server()
        client = await supervisor.get_client(self.engine.repo_root)
        key = "spec_ingest"
        thread_id = self._app_server_threads.get_thread_id(key)
        if thread_id:
            try:
                result = await client.thread_resume(thread_id)
                resumed = result.get("id")
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
                raise SpecIngestError("App-server did not return a thread id")
            self._app_server_threads.set_thread_id(key, thread_id)
        handle = await client.turn_start(
            thread_id,
            prompt,
            approval_policy="never",
            sandbox_policy="readOnly",
        )
        active = self._register_active_turn(client, handle.turn_id, handle.thread_id)
        if self._app_server_events is not None:
            try:
                await self._app_server_events.register_turn(
                    handle.thread_id, handle.turn_id
                )
            except Exception:
                pass
        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(SPEC_INGEST_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(active.interrupt_event.wait())
        try:
            tasks = {turn_task, timeout_task, interrupt_task}
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if timeout_task in done:
                turn_task.add_done_callback(lambda task: task.exception())
                raise SpecIngestError("Spec ingest agent timed out")
            if interrupt_task in done:
                active.interrupted = True
                await self._interrupt_turn(active)
                done, _pending = await asyncio.wait(
                    {turn_task}, timeout=SPEC_INGEST_INTERRUPT_GRACE_SECONDS
                )
                if not done:
                    turn_task.add_done_callback(lambda task: task.exception())
                    return self._assemble_response(
                        {},
                        status="interrupted",
                        agent_message="Spec ingest interrupted",
                    )
            result = await turn_task
        finally:
            self._clear_active_turn(handle.turn_id)
            timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timeout_task
            interrupt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await interrupt_task
        if active.interrupted:
            return self._assemble_response(
                {},
                status="interrupted",
                agent_message="Spec ingest interrupted",
            )
        if result.errors:
            raise SpecIngestError(result.errors[-1])
        output = "\n".join(result.agent_messages).strip()
        message_text, patch_text_raw = SpecIngestPatchParser.split_patch(output)
        if not patch_text_raw.strip():
            raise SpecIngestError("App-server output missing a patch")
        agent_message = SpecIngestPatchParser.parse_agent_message(
            message_text or output
        )
        targets = self._allowed_targets()
        try:
            patch_text, raw_targets = normalize_patch_text(patch_text_raw)
            ensure_patch_targets_allowed(raw_targets, targets.values())
            preview = preview_patch(self.engine.repo_root, patch_text, raw_targets)
        except PatchError as exc:
            raise SpecIngestError(str(exc)) from exc
        self.patch_path.write_text(patch_text, encoding="utf-8")
        self.last_agent_message = agent_message
        docs = {
            key: preview.get(path, self.engine.docs.read_doc(key))
            for key, path in targets.items()
        }
        return self._assemble_response(
            docs, patch=patch_text, agent_message=agent_message
        )

    async def execute(
        self,
        *,
        force: bool,
        spec_path: Optional[Path] = None,
        message: Optional[str] = None,
    ) -> Dict[str, str]:
        async with self.ingest_lock():
            return await self._execute_app_server(
                force=force, spec_path=spec_path, message=message
            )


class SpecIngestPatchParser:
    @staticmethod
    def parse_agent_message(text: str) -> str:
        clean = (text or "").strip()
        if not clean:
            return "Updated docs via spec ingest."
        for line in clean.splitlines():
            if line.lower().startswith("agent:"):
                return line[len("agent:") :].strip() or "Updated docs via spec ingest."
        return clean.splitlines()[0].strip()

    @staticmethod
    def strip_code_fences(text: str) -> str:
        lines = text.strip().splitlines()
        if (
            len(lines) >= 2
            and lines[0].startswith("```")
            and lines[-1].startswith("```")
        ):
            return "\n".join(lines[1:-1]).strip()
        return text.strip()

    @classmethod
    def split_patch(cls, output: str) -> tuple[str, str]:
        if not output:
            return "", ""
        match = re.search(
            r"<PATCH>(.*?)</PATCH>", output, flags=re.IGNORECASE | re.DOTALL
        )
        if match:
            patch_text = cls.strip_code_fences(match.group(1))
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
        patch_text = cls.strip_code_fences(patch_text)
        return message_text, patch_text
