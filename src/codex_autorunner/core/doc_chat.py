import asyncio
import difflib
import json
import re
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Tuple

from ..integrations.app_server.client import CodexAppServerError
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .app_server_prompts import build_doc_chat_prompt
from .app_server_threads import (
    DOC_CHAT_PREFIX,
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .codex_runner import (
    CodexTimeoutError,
    build_codex_command,
    resolve_codex_binary,
    run_codex_capture_async,
)
from .config import ConfigError, RepoConfig
from .engine import Engine, timestamp
from .locks import process_alive, read_lock_info
from .patch_utils import (
    PatchError,
    apply_patch_file,
    ensure_patch_targets_allowed,
    normalize_patch_text,
    preview_patch,
)
from .prompts import DOC_CHAT_PROMPT_TEMPLATE
from .state import load_state
from .utils import atomic_write

ALLOWED_DOC_KINDS = ("todo", "progress", "opinions", "spec", "summary")
DOC_CHAT_TIMEOUT_SECONDS = 180
DOC_CHAT_PATCH_NAME = "doc-chat.patch"
DOC_CHAT_BACKUP_NAME = "doc-chat.backup"
DOC_CHAT_BACKEND_CLI = "cli"
DOC_CHAT_BACKEND_APP_SERVER = "app_server"


@dataclass
class DocChatRequest:
    kind: str
    message: str
    stream: bool = False


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
        backend: Optional[str] = None,
    ):
        self.engine = engine
        self._locks: Dict[str, asyncio.Lock] = {}
        self._recent_summary_cache: Optional[str] = None
        self.patch_path = (
            self.engine.repo_root / ".codex-autorunner" / DOC_CHAT_PATCH_NAME
        )
        self.backup_path = (
            self.engine.repo_root / ".codex-autorunner" / DOC_CHAT_BACKUP_NAME
        )
        self.last_agent_message: Optional[str] = None
        raw_backend = (
            backend
            or getattr(engine.config, "doc_chat_backend", None)
            or DOC_CHAT_BACKEND_CLI
        )
        self._backend = str(raw_backend).strip().lower()
        self._app_server_supervisor = app_server_supervisor
        self._app_server_threads = app_server_threads or AppServerThreadRegistry(
            default_app_server_threads_path(self.engine.repo_root)
        )

    def _repo_config(self) -> RepoConfig:
        if not isinstance(self.engine.config, RepoConfig):
            raise DocChatError("Doc chat requires repo mode config")
        return self.engine.config

    def _using_app_server(self) -> bool:
        return self._backend == DOC_CHAT_BACKEND_APP_SERVER

    def parse_request(self, kind: str, payload: Optional[dict]) -> DocChatRequest:
        if payload is None or not isinstance(payload, dict):
            raise DocChatValidationError("invalid payload")
        key = _normalize_kind(kind)
        message = _normalize_message(str(payload.get("message", "")))
        stream = bool(payload.get("stream", False))
        return DocChatRequest(kind=key, message=message, stream=stream)

    def repo_blocked_reason(self) -> Optional[str]:
        lock_path = self.engine.lock_path
        if lock_path.exists():
            info = read_lock_info(lock_path)
            pid = info.pid
            if pid and process_alive(pid):
                host = f" on {info.host}" if info.host else ""
                return f"Autorunner is running (pid={pid}{host}); try again later."
            return "Autorunner lock present; clear or resume before using doc chat."

        state = load_state(self.engine.state_path)
        if state.status == "running":
            return "Autorunner is currently running; try again later."
        return None

    def doc_busy(self, kind: str) -> bool:
        return self._lock_for(kind).locked()

    @asynccontextmanager
    async def doc_lock(self, kind: str):
        lock = self._lock_for(kind)
        if lock.locked():
            raise DocChatBusyError(f"Doc chat already running for {kind}")
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _lock_for(self, kind: str) -> asyncio.Lock:
        key = _normalize_kind(kind)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

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

    def _build_prompt(self, request: DocChatRequest) -> str:
        config = self._repo_config()
        docs = {key: self.engine.docs.read_doc(key) for key in ALLOWED_DOC_KINDS}
        target_doc = docs.get(request.kind, "")
        recent_block = self._recent_run_summary()
        recent_section = (
            f"<RECENT_RUN>\n{recent_block}\n</RECENT_RUN>"
            if recent_block
            else "<RECENT_RUN>No recent run summary available.</RECENT_RUN>"
        )
        return DOC_CHAT_PROMPT_TEMPLATE.format(
            doc_title=request.kind.upper(),
            message=request.message,
            todo=docs.get("todo", ""),
            progress=docs.get("progress", ""),
            opinions=docs.get("opinions", ""),
            spec=docs.get("spec", ""),
            recent_run_block=recent_section,
            target_doc=target_doc,
            target_path=str(config.doc_path(request.kind)),
            patch_path=str(self.patch_path),
        )

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

    async def _run_codex_cli(self, prompt: str, chat_id: str) -> str:
        try:
            config = self._repo_config()
            resolved = resolve_codex_binary(config)
            cmd = build_codex_command(config, prompt, resolved_binary=resolved)
        except ConfigError as exc:
            raise DocChatError(str(exc)) from exc

        self._log(chat_id, f"cmd={' '.join(cmd[:-1])} prompt_chars={len(prompt)}")

        try:
            exit_code, output = await run_codex_capture_async(
                config,
                self.engine.repo_root,
                prompt,
                timeout_seconds=DOC_CHAT_TIMEOUT_SECONDS,
                cmd=cmd,
            )
        except CodexTimeoutError as exc:
            self._log(chat_id, "timed out waiting for codex process")
            raise DocChatError("Doc chat agent timed out") from exc
        except ConfigError as exc:
            raise DocChatError(str(exc)) from exc

        output = (output or "").strip()
        for line in output.splitlines():
            self._log(chat_id, f"stdout: {line}")
        self._log(chat_id, f"exit_code={exit_code}")
        if exit_code != 0:
            raise DocChatError(f"Codex CLI exited with code {exit_code}")
        if not output:
            raise DocChatError("Codex CLI produced no output")
        return output

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
        if self.backup_path.exists():
            try:
                self.backup_path.unlink()
            except OSError:
                pass

    def _read_patch(self) -> str:
        if not self.patch_path.exists():
            raise DocChatError("Agent did not produce a patch file")
        text = self.patch_path.read_text(encoding="utf-8")
        if not text.strip():
            raise DocChatError("Agent produced an empty patch")
        return text

    def _normalize_apply_patch_format(
        self, patch_text: str
    ) -> Tuple[str, Optional[str]]:
        if "*** begin patch" not in patch_text.lower():
            return patch_text, None
        lines = patch_text.splitlines()
        target_path: Optional[str] = None
        body_lines: List[str] = []
        for line in lines:
            if line.lower().startswith("*** update file:"):
                target_path = line.split(":", 1)[1].strip()
                continue
            if line.startswith("***"):
                continue
            if line.startswith("@@"):
                body_lines.append(line)
                continue
            if line.startswith("+"):
                payload = line[1:]
                if payload.startswith("+"):
                    payload = payload[1:]
                body_lines.append("+" + payload)
                continue
            if line.startswith("-"):
                payload = line[1:]
                if payload.startswith("-"):
                    payload = payload[1:]
                body_lines.append("-" + payload)
                continue
            if line.startswith(" "):
                payload = line[1:]
                if payload.startswith(" "):
                    payload = payload[1:]
                body_lines.append(" " + payload)
                continue
        if not target_path:
            raise DocChatError("Patch missing target path")
        header = f"--- a/{target_path}\n+++ b/{target_path}\n"
        return (
            header + "\n".join(body_lines) + ("\n" if body_lines else ""),
            target_path,
        )

    def _normalize_patch(self, patch_text: str, kind: str) -> Tuple[str, Optional[str]]:
        normalized, target = self._normalize_apply_patch_format(patch_text)
        normalized = self._ensure_headers(normalized, kind)
        return normalized, target

    def _ensure_headers(self, patch_text: str, kind: str) -> str:
        lines = patch_text.splitlines()
        has_headers = any(line.startswith("--- ") for line in lines) and any(
            line.startswith("+++ ") for line in lines
        )
        if has_headers:
            return patch_text
        config = self._repo_config()
        target_path = str(config.doc_path(kind).relative_to(self.engine.repo_root))
        header = f"--- a/{target_path}\n+++ b/{target_path}\n"
        return (
            header
            + ("\n" if lines and not lines[0].startswith("@@") else "")
            + patch_text
        )

    def _patch_targets(self, patch_text: str) -> List[str]:
        targets: List[str] = []
        for line in patch_text.splitlines():
            if line.startswith("--- ") or line.startswith("+++ "):
                parts = line.split()
                if len(parts) >= 2:
                    target = parts[1]
                    if target != "/dev/null":
                        targets.append(target)
        return targets

    def _apply_patch(self, patch_text: str, kind: str) -> None:
        targets = self._patch_targets(patch_text)
        if not targets:
            raise DocChatError("Patch file missing file headers")
        config = self._repo_config()
        target_path = config.doc_path(kind)
        normalized_target = str(target_path.relative_to(self.engine.repo_root))
        normalized = []
        for path in targets:
            p = path
            if p.startswith("a/") or p.startswith("b/"):
                p = p[2:]
            normalized.append(p)
        if any(p != normalized_target for p in normalized):
            raise DocChatError(
                f"Patch referenced unexpected files: {', '.join(targets)}"
            )

        strip = 1 if all(t.startswith(("a/", "b/")) for t in targets) else 0
        cmd = [
            "patch",
            f"-p{strip}",
            "--batch",
            "--quiet",
            "-i",
            str(self.patch_path),
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(self.engine.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.returncode != 0:
            raise DocChatError(
                f"Failed to apply patch: {proc.stdout.strip() or proc.returncode}"
            )
        self._cleanup_patch()

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
        if self._using_app_server():
            return self._apply_app_server_patch(kind)
        # Agent already wrote to the file. Apply = finalize and clean backups.
        patch_text_raw = self._read_patch()
        patch_text, _ = self._normalize_patch(patch_text_raw, kind)
        targets = self._patch_targets(patch_text)
        config = self._repo_config()
        target_path = config.doc_path(kind)
        normalized_target = str(target_path.relative_to(self.engine.repo_root))
        normalized = []
        for path in targets:
            p = path
            if p.startswith("a/") or p.startswith("b/"):
                p = p[2:]
            normalized.append(p)
        if any(p != normalized_target for p in normalized):
            raise DocChatError(
                f"Patch referenced unexpected files: {', '.join(targets)}"
            )
        if self.backup_path.exists():
            try:
                self.backup_path.unlink()
            except OSError:
                pass
        self._cleanup_patch()
        content = target_path.read_text(encoding="utf-8")
        return content

    def discard_patch(self, kind: str) -> str:
        config = self._repo_config()
        target_path = config.doc_path(kind)
        if not self._using_app_server():
            if self.backup_path.exists():
                atomic_write(target_path, self.backup_path.read_text(encoding="utf-8"))
        self._cleanup_patch()
        return target_path.read_text(encoding="utf-8")

    def pending_patch(self, kind: str) -> Optional[dict]:
        if not self.patch_path.exists():
            return None
        try:
            patch_text_raw = self._read_patch()
            patch_text, target = self._normalize_patch(patch_text_raw, kind)
        except DocChatError:
            return None
        targets = self._patch_targets(patch_text)
        if not targets:
            return None
        target_path = targets[0]
        normalized_target = target_path
        if normalized_target.startswith(("a/", "b/")):
            normalized_target = normalized_target[2:]
        config = self._repo_config()
        expected = str(config.doc_path(kind).relative_to(self.engine.repo_root))
        if normalized_target != expected:
            return None
        content = config.doc_path(kind).read_text(encoding="utf-8")
        if self._using_app_server():
            try:
                normalized_targets = ensure_patch_targets_allowed(targets, [expected])
                preview = preview_patch(self.engine.repo_root, patch_text, targets)
                content = preview.get(normalized_targets[0], content)
            except PatchError:
                return None
        return {
            "status": "ok",
            "kind": kind,
            "patch": patch_text,
            "agent_message": self.last_agent_message
            or f"Pending patch for {kind.upper()}",
            "content": content,
        }

    async def _execute_cli(self, request: DocChatRequest) -> dict:
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
            # Backup current doc before the agent edits it.
            config = self._repo_config()
            target_doc_path = config.doc_path(request.kind)
            if target_doc_path.exists():
                self.backup_path.write_text(
                    target_doc_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            else:
                self.backup_path.write_text("", encoding="utf-8")
            prompt = self._build_prompt(request)
            output = await self._run_codex_cli(prompt, chat_id)
            agent_message = self._parse_agent_message(output, request.kind)
            # Generate patch from backup vs current file after agent edits.
            after_text = target_doc_path.read_text(encoding="utf-8")
            before_text = (
                self.backup_path.read_text(encoding="utf-8")
                if self.backup_path.exists()
                else ""
            )
            rel_path = str(target_doc_path.relative_to(self.engine.repo_root))
            patch_text = "\n".join(
                difflib.unified_diff(
                    before_text.splitlines(),
                    after_text.splitlines(),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                    lineterm="",
                )
            )
            if not patch_text.strip():
                patch_text = "--- a/{path}\n+++ b/{path}\n@@\n".format(path=rel_path)
            self.patch_path.write_text(patch_text, encoding="utf-8")
            self.last_agent_message = agent_message
            duration_ms = int((time.time() - started_at) * 1000)
            self._log(
                chat_id,
                "result=success "
                f"kind={request.kind} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}"',
            )
            return {
                "status": "ok",
                "kind": request.kind,
                "patch": patch_text,
                "content": after_text,
                "agent_message": agent_message,
            }
        except DocChatError as exc:
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error "
                f"kind={request.kind} path={doc_pointer} duration_ms={duration_ms} "
                f'message="{message_for_log}" detail="{detail}"',
            )
            # Restore backup on error
            if self.backup_path.exists():
                config = self._repo_config()
                target_doc_path = config.doc_path(request.kind)
                atomic_write(
                    target_doc_path, self.backup_path.read_text(encoding="utf-8")
                )
            self._cleanup_patch()
            return {"status": "error", "detail": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            duration_ms = int((time.time() - started_at) * 1000)
            detail = self._compact_message(str(exc))
            self._log(
                chat_id,
                "result=error kind={kind} path={path} duration_ms={duration_ms} "
                'message="{message}" detail="{detail}"'.format(
                    kind=request.kind,
                    path=doc_pointer,
                    duration_ms=duration_ms,
                    message=message_for_log,
                    detail=detail,
                ),
            )
            return {"status": "error", "detail": "Doc chat failed"}

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
            turn_result = await handle.wait(timeout=DOC_CHAT_TIMEOUT_SECONDS)
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
        if self._using_app_server():
            return await self._execute_app_server(request)
        return await self._execute_cli(request)

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
                else:
                    detail = result.get("detail") or "Doc chat failed"
                    yield format_sse("error", {"detail": detail})
        except DocChatBusyError as exc:
            yield format_sse("error", {"detail": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            yield format_sse("error", {"detail": str(exc)})
