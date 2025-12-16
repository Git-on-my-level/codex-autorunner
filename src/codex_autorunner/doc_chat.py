import asyncio
import difflib
import json
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple

from asyncio.subprocess import PIPE, STDOUT

from .engine import Engine, _process_alive, timestamp
from .state import load_state
from .utils import atomic_write
from .prompts import DOC_CHAT_PROMPT_TEMPLATE

ALLOWED_DOC_KINDS = ("todo", "progress", "opinions", "spec", "summary")
DOC_CHAT_TIMEOUT_SECONDS = 180
DOC_CHAT_PATCH_NAME = "doc-chat.patch"
DOC_CHAT_BACKUP_NAME = "doc-chat.backup"


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
    def __init__(self, engine: Engine):
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
            pid_text = lock_path.read_text(encoding="utf-8").strip()
            pid = int(pid_text) if pid_text.isdigit() else None
            if pid and _process_alive(pid):
                return f"Autorunner is running (pid={pid}); try again later."
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
        path = self.engine.config.doc_path(kind)
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
            target_path=str(self.engine.config.doc_path(request.kind)),
            patch_path=str(self.patch_path),
        )

    async def _run_codex_cli(self, prompt: str, chat_id: str) -> str:
        cmd = [self.engine.config.codex_binary, *self.engine.config.codex_args, prompt]
        self._log(chat_id, f"cmd={' '.join(cmd[:-1])} prompt_chars={len(prompt)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.engine.repo_root),
                stdout=PIPE,
                stderr=STDOUT,
            )
        except FileNotFoundError:
            raise DocChatError(
                f"Codex binary not found: {self.engine.config.codex_binary}"
            )

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=DOC_CHAT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._log(chat_id, "timed out waiting for codex process")
            raise DocChatError("Doc chat agent timed out")

        exit_code = proc.returncode
        output = (stdout.decode("utf-8", errors="ignore") if stdout else "").strip()
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
        target_path = str(
            self.engine.config.doc_path(kind).relative_to(self.engine.repo_root)
        )
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
        target_path = self.engine.config.doc_path(kind)
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

    def apply_saved_patch(self, kind: str) -> str:
        # Agent already wrote to the file. Apply = finalize and clean backups.
        patch_text_raw = self._read_patch()
        patch_text, _ = self._normalize_patch(patch_text_raw, kind)
        targets = self._patch_targets(patch_text)
        target_path = self.engine.config.doc_path(kind)
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
        target_path = self.engine.config.doc_path(kind)
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
        expected = str(
            self.engine.config.doc_path(kind).relative_to(self.engine.repo_root)
        )
        if normalized_target != expected:
            return None
        return {
            "status": "ok",
            "kind": kind,
            "patch": patch_text,
            "agent_message": self.last_agent_message
            or f"Pending patch for {kind.upper()}",
            "content": self.engine.config.doc_path(kind).read_text(encoding="utf-8"),
        }

    async def execute(self, request: DocChatRequest) -> dict:
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
            target_doc_path = self.engine.config.doc_path(request.kind)
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
                target_doc_path = self.engine.config.doc_path(request.kind)
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
