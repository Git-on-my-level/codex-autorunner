import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import threading
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any, Iterator, Optional

import yaml

from ..integrations.app_server.client import CodexAppServerError
from ..integrations.app_server.env import build_app_server_env
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ..manifest import MANIFEST_VERSION
from ..web.static_assets import missing_static_assets, resolve_static_dir
from .about_car import ensure_about_car_file
from .app_server_prompts import build_autorunner_prompt
from .app_server_threads import AppServerThreadRegistry, default_app_server_threads_path
from .config import ConfigError, HubConfig, RepoConfig, _is_loopback_host, load_config
from .docs import DocsManager
from .git_utils import GitError, run_git
from .locks import (
    FileLock,
    FileLockBusy,
    process_alive,
    read_lock_info,
    write_lock_info,
)
from .notifications import NotificationManager
from .optional_dependencies import missing_optional_dependencies
from .prompt import build_final_summary_prompt
from .state import RunnerState, load_state, now_iso, save_state, state_lock
from .utils import (
    atomic_write,
    ensure_executable,
    find_repo_root,
)


class LockError(Exception):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SUMMARY_FINALIZED_MARKER = "CAR:SUMMARY_FINALIZED"
SUMMARY_FINALIZED_MARKER_PREFIX = f"<!-- {SUMMARY_FINALIZED_MARKER}"
AUTORUNNER_APP_SERVER_MESSAGE = (
    "Continue working through TODO items from top to bottom."
)
AUTORUNNER_STOP_POLL_SECONDS = 1.0
AUTORUNNER_INTERRUPT_GRACE_SECONDS = 30.0


class Engine:
    def __init__(self, repo_root: Path):
        config = load_config(repo_root)
        if not isinstance(config, RepoConfig):
            raise ConfigError("Engine requires repo mode configuration")
        self.config: RepoConfig = config
        self.repo_root = self.config.root
        self.docs = DocsManager(self.config)
        self.notifier = NotificationManager(self.config)
        self.state_path = self.repo_root / ".codex-autorunner" / "state.json"
        self.log_path = self.config.log.path
        self.run_index_path = self.repo_root / ".codex-autorunner" / "run_index.json"
        self.lock_path = self.repo_root / ".codex-autorunner" / "lock"
        self.stop_path = self.repo_root / ".codex-autorunner" / "stop"
        self._active_global_handler: Optional[RotatingFileHandler] = None
        self._active_run_log: Optional[IO[str]] = None
        self._app_server_threads = AppServerThreadRegistry(
            default_app_server_threads_path(self.repo_root)
        )
        self._app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None
        self._app_server_logger = logging.getLogger("codex_autorunner.app_server")
        self._last_run_interrupted = False
        self._lock_handle: Optional[FileLock] = None
        # Ensure the interactive TUI briefing doc exists (for web Terminal "New").
        try:
            ensure_about_car_file(self.config)
        except Exception:
            # Never fail Engine creation due to a best-effort helper doc.
            pass

    @staticmethod
    def from_cwd(repo: Optional[Path] = None) -> "Engine":
        root = find_repo_root(repo or Path.cwd())
        return Engine(root)

    def acquire_lock(self, force: bool = False) -> None:
        self._lock_handle = FileLock(self.lock_path)
        try:
            self._lock_handle.acquire(blocking=False)
        except FileLockBusy as exc:
            info = read_lock_info(self.lock_path)
            pid = info.pid
            if pid and process_alive(pid):
                raise LockError(
                    f"Another autorunner is active (pid={pid}); stop it before continuing"
                ) from exc
            raise LockError(
                "Another autorunner is active; stop it before continuing"
            ) from exc
        info = read_lock_info(self.lock_path)
        pid = info.pid
        if pid and process_alive(pid) and not force:
            self._lock_handle.release()
            self._lock_handle = None
            raise LockError(
                f"Another autorunner is active (pid={pid}); use --force to override"
            )
        write_lock_info(
            self.lock_path,
            os.getpid(),
            started_at=now_iso(),
            lock_file=self._lock_handle.file,
        )

    def release_lock(self) -> None:
        if self._lock_handle is not None:
            self._lock_handle.release()
            self._lock_handle = None
        if self.lock_path.exists():
            self.lock_path.unlink()

    def repo_busy_reason(self) -> Optional[str]:
        if self.lock_path.exists():
            info = read_lock_info(self.lock_path)
            pid = info.pid
            if pid and process_alive(pid):
                host = f" on {info.host}" if info.host else ""
                return f"Autorunner is running (pid={pid}{host}); try again later."
            return "Autorunner lock present; clear or resume before continuing."

        state = load_state(self.state_path)
        if state.status == "running":
            return "Autorunner is currently running; try again later."
        return None

    def request_stop(self) -> None:
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.stop_path, f"{now_iso()}\n")

    def clear_stop_request(self) -> None:
        self.stop_path.unlink(missing_ok=True)

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def _should_stop(self, external_stop_flag: Optional[threading.Event]) -> bool:
        if external_stop_flag and external_stop_flag.is_set():
            return True
        return self.stop_requested()

    def kill_running_process(self) -> Optional[int]:
        """Force-kill the process holding the lock, if any. Returns pid if killed."""
        if not self.lock_path.exists():
            return None
        info = read_lock_info(self.lock_path)
        pid = info.pid
        if pid and process_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                return pid
            except OSError:
                return None
        # stale lock
        self.lock_path.unlink(missing_ok=True)
        return None

    def runner_pid(self) -> Optional[int]:
        state = load_state(self.state_path)
        pid = state.runner_pid
        if pid and process_alive(pid):
            return pid
        info = read_lock_info(self.lock_path)
        if info.pid and process_alive(info.pid):
            return info.pid
        return None

    def todos_done(self) -> bool:
        return self.docs.todos_done()

    def summary_finalized(self) -> bool:
        """Return True if SUMMARY.md contains the finalization marker."""
        try:
            text = self.docs.read_doc("summary")
        except Exception:
            return False
        return SUMMARY_FINALIZED_MARKER in (text or "")

    def _stamp_summary_finalized(self, run_id: int) -> None:
        """
        Append an idempotency marker to SUMMARY.md so the final summary job runs only once.
        Users may remove the marker to force regeneration.
        """
        path = self.config.doc_path("summary")
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            existing = ""
        if SUMMARY_FINALIZED_MARKER in existing:
            return
        stamp = f"{SUMMARY_FINALIZED_MARKER_PREFIX} run_id={int(run_id)} -->\n"
        new_text = existing
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        # Keep a blank line before the marker for readability.
        if new_text and not new_text.endswith("\n\n"):
            new_text += "\n"
        new_text += stamp
        atomic_write(path, new_text)

    async def _execute_run_step(
        self,
        prompt: str,
        run_id: int,
        *,
        external_stop_flag: Optional[threading.Event] = None,
    ) -> int:
        """
        Execute a single run step:
        1. Update state to 'running'
        2. Log start
        3. Run Codex CLI
        4. Log end
        5. Update state to 'idle' or 'error'
        6. Commit if successful and auto-commit is enabled
        """
        self._update_state("running", run_id, None, started=True)
        self._last_run_interrupted = False
        with self._run_log_context(run_id):
            self._write_run_marker(run_id, "start")
            exit_code = await self._run_codex_app_server_async(
                prompt,
                run_id,
                external_stop_flag=external_stop_flag,
            )
            self._write_run_marker(run_id, "end", exit_code=exit_code)

        self._update_state(
            "error" if exit_code != 0 else "idle",
            run_id,
            exit_code,
            finished=True,
        )
        if exit_code != 0:
            self.notifier.notify_run_finished(run_id=run_id, exit_code=exit_code)

        if exit_code == 0 and self.config.git_auto_commit:
            if self._last_run_interrupted:
                return exit_code
            self.maybe_git_commit(run_id)

        return exit_code

    async def _run_final_summary_job(
        self, run_id: int, *, external_stop_flag: Optional[threading.Event] = None
    ) -> int:
        """
        Run a dedicated Codex invocation that produces/updates SUMMARY.md as the final user report.
        """
        prev_output = self.extract_prev_output(run_id - 1)
        prompt = build_final_summary_prompt(self.config, self.docs, prev_output)

        exit_code = await self._execute_run_step(
            prompt, run_id, external_stop_flag=external_stop_flag
        )

        if exit_code == 0:
            self._stamp_summary_finalized(run_id)
            self.notifier.notify_run_finished(run_id=run_id, exit_code=exit_code)
            # Commit is already handled by _execute_run_step if auto-commit is enabled.
        return exit_code

    def extract_prev_output(self, run_id: int) -> Optional[str]:
        if run_id <= 0:
            return None
        run_log = self._run_log_path(run_id)
        if run_log.exists():
            try:
                text = run_log.read_text(encoding="utf-8")
            except Exception:
                text = ""
            if text:
                lines = [
                    line
                    for line in text.splitlines()
                    if not line.startswith("=== run ")
                ]
                text = _strip_log_prefixes("\n".join(lines))
                max_chars = self.config.prompt_prev_run_max_chars
                return text[-max_chars:] if text else None
        if not self.log_path.exists():
            return None
        start = f"=== run {run_id} start ==="
        end = f"=== run {run_id} end"
        # NOTE: do NOT read the full log file into memory. Logs can be very large
        # (especially with verbose Codex output) and this can OOM the server/runner.
        text = _read_tail_text(self.log_path, max_bytes=250_000)
        lines = text.splitlines()
        collecting = False
        collected = []
        for line in lines:
            if line.strip() == start:
                collecting = True
                continue
            if collecting and line.startswith(end):
                break
            if collecting:
                collected.append(line)
        if not collected:
            return None
        text = "\n".join(collected)
        text = _strip_log_prefixes(text)
        max_chars = self.config.prompt_prev_run_max_chars
        return text[-max_chars:]

    def read_run_block(self, run_id: int) -> Optional[str]:
        """Return a single run block from the log."""
        index_entry = self._load_run_index().get(str(run_id))
        run_log = None
        if index_entry:
            run_log_raw = index_entry.get("run_log_path")
            if isinstance(run_log_raw, str) and run_log_raw:
                run_log = Path(run_log_raw)
        if run_log is None:
            run_log = self._run_log_path(run_id)
        if run_log.exists():
            try:
                return run_log.read_text(encoding="utf-8")
            except Exception:
                return None
        if index_entry:
            block = self._read_log_range(index_entry)
            if block is not None:
                return block
        if not self.log_path.exists():
            return None
        start = f"=== run {run_id} start"
        end = f"=== run {run_id} end"
        # Avoid reading entire log into memory; prefer tail scan.
        max_bytes = 1_000_000
        text = _read_tail_text(self.log_path, max_bytes=max_bytes)
        lines = text.splitlines()
        buf = []
        printing = False
        for line in lines:
            if line.startswith(start):
                printing = True
                buf.append(line)
                continue
            if printing and line.startswith(end):
                buf.append(line)
                break
            if printing:
                buf.append(line)
        if buf:
            return "\n".join(buf)
        # If file is small, fall back to full read (safe).
        try:
            if self.log_path.stat().st_size <= max_bytes:
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
                buf = []
                printing = False
                for line in lines:
                    if line.startswith(start):
                        printing = True
                        buf.append(line)
                        continue
                    if printing and line.startswith(end):
                        buf.append(line)
                        break
                    if printing:
                        buf.append(line)
                return "\n".join(buf) if buf else None
        except Exception:
            return None
        return None

    def tail_log(self, tail: int) -> str:
        if not self.log_path.exists():
            return ""
        # Bound memory usage: only read a chunk from the end.
        text = _read_tail_text(self.log_path, max_bytes=400_000)
        lines = text.splitlines()
        return "\n".join(lines[-tail:])

    def log_line(self, run_id: int, message: str) -> None:
        line = f"[{timestamp()}] run={run_id} {message}\n"
        if self._active_global_handler is not None:
            self._emit_global_line(line.rstrip("\n"))
        else:
            self._ensure_log_path()
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        if self._active_run_log is not None:
            try:
                self._active_run_log.write(line)
                self._active_run_log.flush()
            except Exception:
                pass
        else:
            run_log = self._run_log_path(run_id)
            self._ensure_run_log_dir()
            with run_log.open("a", encoding="utf-8") as f:
                f.write(line)

    def _ensure_log_path(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _run_log_path(self, run_id: int) -> Path:
        return self.log_path.parent / "runs" / f"run-{run_id}.log"

    def _ensure_run_log_dir(self) -> None:
        (self.log_path.parent / "runs").mkdir(parents=True, exist_ok=True)

    def _write_run_marker(
        self, run_id: int, marker: str, exit_code: Optional[int] = None
    ) -> None:
        suffix = ""
        if marker == "end":
            suffix = f" (code {exit_code})"
        text = f"=== run {run_id} {marker}{suffix} ==="
        offset = self._emit_global_line(text)
        if self._active_run_log is not None:
            try:
                self._active_run_log.write(f"{text}\n")
                self._active_run_log.flush()
            except Exception:
                pass
        else:
            self._ensure_run_log_dir()
            run_log = self._run_log_path(run_id)
            with run_log.open("a", encoding="utf-8") as f:
                f.write(f"{text}\n")
        self._update_run_index(run_id, marker, offset, exit_code)

    def _emit_global_line(self, text: str) -> Optional[tuple[int, int]]:
        if self._active_global_handler is None:
            self._ensure_log_path()
            try:
                with self.log_path.open("a", encoding="utf-8") as f:
                    start = f.tell()
                    f.write(f"{text}\n")
                    f.flush()
                    return (start, f.tell())
            except Exception:
                return None
        handler = self._active_global_handler
        record = logging.LogRecord(
            name="codex_autorunner.engine",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=text,
            args=(),
            exc_info=None,
        )
        handler.acquire()
        try:
            if handler.shouldRollover(record):
                handler.doRollover()
            if handler.stream is None:
                handler.stream = handler._open()
            start_offset = handler.stream.tell()
            logging.FileHandler.emit(handler, record)
            handler.flush()
            end_offset = handler.stream.tell()
            return (start_offset, end_offset)
        except Exception:
            return None
        finally:
            handler.release()

    @contextlib.contextmanager
    def _run_log_context(self, run_id: int) -> Iterator[None]:
        self._ensure_log_path()
        self._ensure_run_log_dir()
        max_bytes = getattr(self.config.log, "max_bytes", None) or 0
        backup_count = getattr(self.config.log, "backup_count", 0) or 0
        handler = RotatingFileHandler(
            self.log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        run_log = self._run_log_path(run_id)
        with run_log.open("a", encoding="utf-8") as run_handle:
            self._active_global_handler = handler
            self._active_run_log = run_handle
            try:
                yield
            finally:
                self._active_global_handler = None
                self._active_run_log = None
                try:
                    handler.close()
                except Exception:
                    pass

    def _load_run_index(self) -> dict[str, dict]:
        if not self.run_index_path.exists():
            return {}
        try:
            raw = self.run_index_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
        return {}

    def _save_run_index(self, index: dict[str, dict]) -> None:
        try:
            self.run_index_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(index, ensure_ascii=True, indent=2)
            atomic_write(self.run_index_path, f"{payload}\n")
        except Exception:
            pass

    def _update_run_index(
        self,
        run_id: int,
        marker: str,
        offset: Optional[tuple[int, int]],
        exit_code: Optional[int],
    ) -> None:
        index = self._load_run_index()
        key = str(run_id)
        entry = index.get(key, {})
        if marker == "start":
            entry["start_offset"] = offset[0] if offset else None
            entry["started_at"] = now_iso()
            entry["log_path"] = str(self.log_path)
            entry["run_log_path"] = str(self._run_log_path(run_id))
        elif marker == "end":
            entry["end_offset"] = offset[1] if offset else None
            entry["finished_at"] = now_iso()
            entry["exit_code"] = exit_code
            entry.setdefault("log_path", str(self.log_path))
            entry.setdefault("run_log_path", str(self._run_log_path(run_id)))
        index[key] = entry
        self._save_run_index(index)

    def _read_log_range(self, entry: dict) -> Optional[str]:
        start = entry.get("start_offset")
        end = entry.get("end_offset")
        if start is None or end is None:
            return None
        try:
            start_offset = int(start)
            end_offset = int(end)
        except (TypeError, ValueError):
            return None
        if end_offset < start_offset:
            return None
        log_path = Path(entry.get("log_path", self.log_path))
        if not log_path.exists():
            return None
        try:
            size = log_path.stat().st_size
            if size < end_offset:
                return None
            with log_path.open("rb") as f:
                f.seek(start_offset)
                data = f.read(end_offset - start_offset)
            return data.decode("utf-8", errors="replace")
        except Exception:
            return None

    def _build_app_server_prompt(self, prev_output: Optional[str]) -> str:
        return build_autorunner_prompt(
            self.config,
            message=AUTORUNNER_APP_SERVER_MESSAGE,
            prev_run_summary=prev_output,
        )

    def run_codex_app_server(
        self,
        prompt: str,
        run_id: int,
        *,
        external_stop_flag: Optional[threading.Event] = None,
    ) -> int:
        try:
            return asyncio.run(
                self._run_codex_app_server_async(
                    prompt,
                    run_id,
                    external_stop_flag=external_stop_flag,
                    reuse_supervisor=False,
                )
            )
        except RuntimeError as exc:
            if "asyncio.run" in str(exc):
                self.log_line(
                    run_id,
                    "error: app-server backend cannot run inside an active event loop",
                )
                return 1
            raise

    async def _run_codex_app_server_async(
        self,
        prompt: str,
        run_id: int,
        *,
        external_stop_flag: Optional[threading.Event] = None,
        reuse_supervisor: bool = True,
    ) -> int:
        config = self.config
        if not config.app_server.command:
            self.log_line(
                run_id,
                "error: app-server backend requires app_server.command to be configured",
            )
            return 1

        def _env_builder(
            workspace_root: Path, _workspace_id: str, state_dir: Path
        ) -> dict[str, str]:
            state_dir.mkdir(parents=True, exist_ok=True)
            return build_app_server_env(
                config.app_server.command,
                workspace_root,
                state_dir,
                logger=self._app_server_logger,
                event_prefix="autorunner",
            )

        supervisor = (
            self._ensure_app_server_supervisor(_env_builder)
            if reuse_supervisor
            else self._build_app_server_supervisor(_env_builder)
        )
        try:
            client = await supervisor.get_client(self.repo_root)
            thread_id = self._app_server_threads.get_thread_id("autorunner")
            if thread_id:
                try:
                    resume_result = await client.thread_resume(thread_id)
                    resumed = resume_result.get("id")
                    if isinstance(resumed, str) and resumed:
                        thread_id = resumed
                        self._app_server_threads.set_thread_id("autorunner", thread_id)
                except CodexAppServerError:
                    self._app_server_threads.reset_thread("autorunner")
                    thread_id = None
            if not thread_id:
                thread = await client.thread_start(str(self.repo_root))
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    self.log_line(
                        run_id, "error: app-server did not return a thread id"
                    )
                    return 1
                self._app_server_threads.set_thread_id("autorunner", thread_id)
            turn_kwargs: dict[str, Any] = {}
            if config.codex_model:
                turn_kwargs["model"] = str(config.codex_model)
            if config.codex_reasoning:
                turn_kwargs["effort"] = str(config.codex_reasoning)
            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy="never",
                sandbox_policy="dangerFullAccess",
                **turn_kwargs,
            )
            turn_timeout = config.app_server.turn_timeout_seconds
            turn_result, interrupted = await self._wait_for_turn_with_stop(
                client,
                handle,
                run_id,
                timeout=turn_timeout,
                external_stop_flag=external_stop_flag,
                supervisor=supervisor,
            )
            self._last_run_interrupted = interrupted
            self._log_app_server_output(run_id, turn_result.agent_messages)
            if turn_result.errors:
                for error in turn_result.errors:
                    self.log_line(run_id, f"error: {error}")
                return 1
            return 0
        except asyncio.TimeoutError:
            self.log_line(run_id, "error: app-server turn timed out")
            return 1
        except CodexAppServerError as exc:
            self.log_line(run_id, f"error: {exc}")
            return 1
        except Exception as exc:  # pragma: no cover - defensive
            self.log_line(run_id, f"error: app-server failed: {exc}")
            return 1
        finally:
            if not reuse_supervisor:
                await supervisor.close_all()

    def _log_app_server_output(self, run_id: int, messages: list[str]) -> None:
        if not messages:
            return
        for message in messages:
            text = str(message)
            lines = text.splitlines() or [""]
            for line in lines:
                self.log_line(run_id, f"stdout: {line}" if line else "stdout: ")

    def maybe_git_commit(self, run_id: int) -> None:
        msg = self.config.git_commit_message_template.replace(
            "{run_id}", str(run_id)
        ).replace("#{run_id}", str(run_id))
        paths = [
            self.config.doc_path("todo"),
            self.config.doc_path("progress"),
            self.config.doc_path("opinions"),
            self.config.doc_path("spec"),
            self.config.doc_path("summary"),
        ]
        add_paths = [str(p.relative_to(self.repo_root)) for p in paths if p.exists()]
        if not add_paths:
            return
        try:
            add_proc = run_git(["add", *add_paths], self.repo_root, check=False)
            if add_proc.returncode != 0:
                detail = (
                    add_proc.stderr or add_proc.stdout or ""
                ).strip() or f"exit {add_proc.returncode}"
                self.log_line(run_id, f"git add failed: {detail}")
                return
        except GitError as exc:
            self.log_line(run_id, f"git add failed: {exc}")
            return
        try:
            commit_proc = run_git(
                ["commit", "-m", msg],
                self.repo_root,
                check=False,
                timeout_seconds=120,
            )
            if commit_proc.returncode != 0:
                detail = (
                    commit_proc.stderr or commit_proc.stdout or ""
                ).strip() or f"exit {commit_proc.returncode}"
                self.log_line(run_id, f"git commit failed: {detail}")
        except GitError as exc:
            self.log_line(run_id, f"git commit failed: {exc}")

    def _build_app_server_supervisor(
        self, env_builder: Any
    ) -> WorkspaceAppServerSupervisor:
        config = self.config.app_server
        return WorkspaceAppServerSupervisor(
            config.command,
            state_root=config.state_root,
            env_builder=env_builder,
            logger=self._app_server_logger,
            max_handles=config.max_handles,
            idle_ttl_seconds=config.idle_ttl_seconds,
            request_timeout=config.request_timeout,
        )

    def _ensure_app_server_supervisor(
        self, env_builder: Any
    ) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is None:
            self._app_server_supervisor = self._build_app_server_supervisor(env_builder)
        return self._app_server_supervisor

    async def _close_app_server_supervisor(self) -> None:
        if self._app_server_supervisor is None:
            return
        supervisor = self._app_server_supervisor
        self._app_server_supervisor = None
        try:
            await supervisor.close_all()
        except Exception as exc:
            self._app_server_logger.warning(
                "app-server supervisor close failed: %s", exc
            )

    async def _wait_for_stop(
        self, external_stop_flag: Optional[threading.Event]
    ) -> None:
        while not self._should_stop(external_stop_flag):
            await asyncio.sleep(AUTORUNNER_STOP_POLL_SECONDS)

    async def _wait_for_turn_with_stop(
        self,
        client: Any,
        handle: Any,
        run_id: int,
        *,
        timeout: Optional[float],
        external_stop_flag: Optional[threading.Event],
        supervisor: Optional[WorkspaceAppServerSupervisor] = None,
    ) -> tuple[Any, bool]:
        stop_task = asyncio.create_task(self._wait_for_stop(external_stop_flag))
        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task: Optional[asyncio.Task] = (
            asyncio.create_task(asyncio.sleep(timeout)) if timeout else None
        )
        interrupted = False
        try:
            tasks = {stop_task, turn_task}
            if timeout_task is not None:
                tasks.add(timeout_task)
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if turn_task in done:
                result = await turn_task
                return result, interrupted
            timed_out = timeout_task is not None and timeout_task in done
            stopped = stop_task in done
            if timed_out:
                self.log_line(
                    run_id, "error: app-server turn timed out; interrupting app-server"
                )
            if stopped and not turn_task.done():
                interrupted = True
                self.log_line(run_id, "info: stop requested; interrupting app-server")
            if not turn_task.done():
                try:
                    await client.turn_interrupt(
                        handle.turn_id, thread_id=handle.thread_id
                    )
                except CodexAppServerError as exc:
                    self.log_line(run_id, f"error: app-server interrupt failed: {exc}")
                    if interrupted:
                        self.kill_running_process()
                    raise
                done, _pending = await asyncio.wait(
                    {turn_task}, timeout=AUTORUNNER_INTERRUPT_GRACE_SECONDS
                )
                if not done:
                    self.log_line(
                        run_id,
                        "error: app-server interrupt timed out; cleaning up",
                    )
                    if interrupted:
                        self.kill_running_process()
                        raise CodexAppServerError("App-server interrupt timed out")
                    if supervisor is not None:
                        await supervisor.close_all()
                    raise asyncio.TimeoutError()
            result = await turn_task
            if timed_out:
                raise asyncio.TimeoutError()
            return result, interrupted
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            if timeout_task is not None:
                timeout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timeout_task

    async def _run_loop_async(
        self,
        stop_after_runs: Optional[int] = None,
        external_stop_flag: Optional[threading.Event] = None,
    ) -> None:
        state = load_state(self.state_path)
        run_id = (state.last_run_id or 0) + 1
        last_exit_code: Optional[int] = state.last_exit_code
        start_wallclock = time.time()
        target_runs = (
            stop_after_runs
            if stop_after_runs is not None
            else self.config.runner_stop_after_runs
        )

        try:
            while True:
                if self._should_stop(external_stop_flag):
                    self.clear_stop_request()
                    self._update_state(
                        "idle", run_id - 1, last_exit_code, finished=True
                    )
                    break
                if self.config.runner_max_wallclock_seconds is not None:
                    if (
                        time.time() - start_wallclock
                        > self.config.runner_max_wallclock_seconds
                    ):
                        self._update_state(
                            "idle", run_id - 1, state.last_exit_code, finished=True
                        )
                        break

                if self.todos_done():
                    if not self.summary_finalized():
                        exit_code = await self._run_final_summary_job(
                            run_id, external_stop_flag=external_stop_flag
                        )
                        last_exit_code = exit_code
                    else:
                        current = load_state(self.state_path)
                        last_exit_code = current.last_exit_code
                        self._update_state(
                            "idle", run_id - 1, last_exit_code, finished=True
                        )
                    break

                prev_output = self.extract_prev_output(run_id - 1)
                prompt = self._build_app_server_prompt(prev_output)

                exit_code = await self._execute_run_step(
                    prompt, run_id, external_stop_flag=external_stop_flag
                )
                last_exit_code = exit_code

                if exit_code != 0:
                    break

                # If TODO is now complete, run the final report job once and stop.
                if self.todos_done() and not self.summary_finalized():
                    exit_code = await self._run_final_summary_job(
                        run_id + 1, external_stop_flag=external_stop_flag
                    )
                    last_exit_code = exit_code
                    break

                if target_runs is not None and run_id >= target_runs:
                    break

                run_id += 1
                if self._should_stop(external_stop_flag):
                    self.clear_stop_request()
                    self._update_state("idle", run_id - 1, exit_code, finished=True)
                    break
                await asyncio.sleep(self.config.runner_sleep_seconds)
        except Exception as exc:
            # Never silently die: persist the reason to the agent log and surface in state.
            try:
                self.log_line(run_id, f"FATAL: run_loop crashed: {exc!r}")
                tb = traceback.format_exc()
                for line in tb.splitlines():
                    self.log_line(run_id, f"traceback: {line}")
            except Exception:
                pass
            try:
                self._update_state("error", run_id, 1, finished=True)
            except Exception:
                pass
        finally:
            await self._close_app_server_supervisor()
        # IMPORTANT: lock ownership is managed by the caller (CLI/Hub/Server runner).
        # Engine.run_loop must never unconditionally mutate the lock file.

    def run_loop(
        self,
        stop_after_runs: Optional[int] = None,
        external_stop_flag: Optional[threading.Event] = None,
    ) -> None:
        try:
            asyncio.run(self._run_loop_async(stop_after_runs, external_stop_flag))
        except RuntimeError as exc:
            if "asyncio.run" in str(exc):
                raise
            raise

    def run_once(self) -> None:
        self.run_loop(stop_after_runs=1)

    def _update_state(
        self,
        status: str,
        run_id: int,
        exit_code: Optional[int],
        *,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        with state_lock(self.state_path):
            current = load_state(self.state_path)
            last_run_started_at = current.last_run_started_at
            last_run_finished_at = current.last_run_finished_at
            runner_pid = current.runner_pid
            if started:
                last_run_started_at = now_iso()
                last_run_finished_at = None
                runner_pid = os.getpid()
            if finished:
                last_run_finished_at = now_iso()
                runner_pid = None
            new_state = RunnerState(
                last_run_id=run_id,
                status=status,
                last_exit_code=exit_code,
                last_run_started_at=last_run_started_at,
                last_run_finished_at=last_run_finished_at,
                runner_pid=runner_pid,
                sessions=current.sessions,
                repo_to_session=current.repo_to_session,
            )
            save_state(self.state_path, new_state)


def clear_stale_lock(lock_path: Path) -> None:
    if lock_path.exists():
        info = read_lock_info(lock_path)
        pid = info.pid
        if not pid or not process_alive(pid):
            lock_path.unlink(missing_ok=True)


def _strip_log_prefixes(text: str) -> str:
    """Strip log prefixes and clip to content after token-usage marker if present."""
    lines = text.splitlines()
    cleaned_lines = []
    token_marker_idx = None
    for idx, line in enumerate(lines):
        if "stdout: tokens used" in line:
            token_marker_idx = idx
            break
    if token_marker_idx is not None:
        lines = lines[token_marker_idx + 1 :]

    for line in lines:
        if "] run=" in line and "stdout:" in line:
            try:
                _, remainder = line.split("stdout:", 1)
                cleaned_lines.append(remainder.strip())
                continue
            except ValueError:
                pass
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _read_tail_text(path: Path, *, max_bytes: int) -> str:
    """
    Read at most the last `max_bytes` bytes from a UTF-8-ish text file.
    Returns decoded text with errors replaced.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= 0:
        return ""
    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


@dataclasses.dataclass(frozen=True)
class DoctorCheck:
    check_id: str
    status: str
    message: str
    fix: Optional[str] = None

    def to_dict(self) -> dict:
        payload = {
            "id": self.check_id,
            "status": self.status,
            "message": self.message,
        }
        if self.fix:
            payload["fix"] = self.fix
        return payload


@dataclasses.dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]

    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warnings": sum(1 for check in self.checks if check.status == "warning"),
            "errors": sum(1 for check in self.checks if check.status == "error"),
            "checks": [check.to_dict() for check in self.checks],
        }


def _append_check(
    checks: list[DoctorCheck],
    check_id: str,
    status: str,
    message: str,
    fix: Optional[str] = None,
) -> None:
    checks.append(
        DoctorCheck(check_id=check_id, status=status, message=message, fix=fix)
    )


def _parse_manifest_version(manifest_path: Path) -> Optional[int]:
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    return int(version) if isinstance(version, int) else None


def _manifest_has_worktrees(manifest_path: Path) -> bool:
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    repos = raw.get("repos")
    if not isinstance(repos, list):
        return False
    for entry in repos:
        if isinstance(entry, dict) and entry.get("kind") == "worktree":
            return True
    return False


def doctor(start_path: Path) -> DoctorReport:
    config = load_config(start_path)
    checks: list[DoctorCheck] = []
    root = config.root

    if isinstance(config, RepoConfig):
        missing = []
        for key in ("todo", "progress", "opinions"):
            path = config.doc_path(key)
            if not path.exists():
                missing.append(path)
        if missing:
            names = ", ".join(str(p) for p in missing)
            _append_check(
                checks,
                "docs.required",
                "error",
                f"Missing doc files: {names}",
                "Run `car init` or create the missing files.",
            )
        else:
            _append_check(
                checks,
                "docs.required",
                "ok",
                "Required doc files are present.",
            )

        if ensure_executable(config.codex_binary):
            _append_check(
                checks,
                "codex.binary",
                "ok",
                f"Codex binary resolved: {config.codex_binary}",
            )
        else:
            _append_check(
                checks,
                "codex.binary",
                "error",
                f"Codex binary not found in PATH: {config.codex_binary}",
                "Install Codex or set codex.binary to a full path.",
            )

        voice_enabled = bool(config.voice.get("enabled", True))
        if voice_enabled:
            missing_voice = missing_optional_dependencies(
                (
                    ("httpx", "httpx"),
                    (("multipart", "python_multipart"), "python-multipart"),
                )
            )
            if missing_voice:
                deps_list = ", ".join(missing_voice)
                _append_check(
                    checks,
                    "voice.dependencies",
                    "error",
                    f"Voice is enabled but missing optional deps: {deps_list}",
                    "Install with `pip install codex-autorunner[voice]`.",
                )
            else:
                _append_check(
                    checks,
                    "voice.dependencies",
                    "ok",
                    "Voice dependencies are installed.",
                )

    env_candidates = [
        root / ".env",
        root / ".codex-autorunner" / ".env",
    ]
    env_found = [str(path) for path in env_candidates if path.exists()]
    if env_found:
        _append_check(
            checks,
            "dotenv.locations",
            "ok",
            f"Found .env files: {', '.join(env_found)}",
        )
    else:
        _append_check(
            checks,
            "dotenv.locations",
            "warning",
            "No .env files found in repo root or .codex-autorunner/.env.",
            "Create one of these files if you rely on env vars.",
        )

    host = str(config.server_host or "")
    if not _is_loopback_host(host):
        if not config.server_auth_token_env:
            _append_check(
                checks,
                "server.auth",
                "error",
                f"Non-loopback host {host} requires server.auth_token_env.",
                "Set server.auth_token_env or bind to 127.0.0.1.",
            )
        else:
            token_val = os.environ.get(config.server_auth_token_env)
            if not token_val:
                _append_check(
                    checks,
                    "server.auth",
                    "warning",
                    f"Auth token env var {config.server_auth_token_env} is not set.",
                    "Export the env var or add it to .env.",
                )
            else:
                _append_check(
                    checks,
                    "server.auth",
                    "ok",
                    "Server auth token env var is set for non-loopback host.",
                )

    static_dir, static_context = resolve_static_dir()
    try:
        missing_assets = missing_static_assets(static_dir)
        if missing_assets:
            _append_check(
                checks,
                "static.assets",
                "error",
                f"Static UI assets missing in {static_dir}: {', '.join(missing_assets)}",
                "Reinstall the package or rebuild the UI assets.",
            )
        else:
            _append_check(
                checks,
                "static.assets",
                "ok",
                f"Static UI assets present in {static_dir}",
            )
    finally:
        if static_context is not None:
            static_context.close()

    if isinstance(config, HubConfig):
        if config.manifest_path.exists():
            version = _parse_manifest_version(config.manifest_path)
            if version is None:
                _append_check(
                    checks,
                    "hub.manifest.version",
                    "error",
                    f"Failed to read manifest version from {config.manifest_path}.",
                    "Fix the manifest YAML or regenerate it with `car hub scan`.",
                )
            elif version != MANIFEST_VERSION:
                _append_check(
                    checks,
                    "hub.manifest.version",
                    "error",
                    f"Hub manifest version {version} unsupported (expected {MANIFEST_VERSION}).",
                    "Regenerate the manifest (delete it and run `car hub scan`).",
                )
            else:
                _append_check(
                    checks,
                    "hub.manifest.version",
                    "ok",
                    f"Hub manifest version {version} is supported.",
                )
        else:
            _append_check(
                checks,
                "hub.manifest.exists",
                "warning",
                f"Hub manifest missing at {config.manifest_path}.",
                "Run `car hub scan` or `car hub create` to generate it.",
            )

        if not config.repos_root.exists():
            _append_check(
                checks,
                "hub.repos_root",
                "error",
                f"Hub repos_root does not exist: {config.repos_root}",
                "Create the directory or update hub.repos_root in config.",
            )
        elif not config.repos_root.is_dir():
            _append_check(
                checks,
                "hub.repos_root",
                "error",
                f"Hub repos_root is not a directory: {config.repos_root}",
                "Point hub.repos_root at a directory.",
            )
        else:
            _append_check(
                checks,
                "hub.repos_root",
                "ok",
                f"Hub repos_root exists: {config.repos_root}",
            )

        manifest_has_worktrees = (
            config.manifest_path.exists()
            and _manifest_has_worktrees(config.manifest_path)
        )
        worktrees_enabled = config.worktrees_root.exists() or manifest_has_worktrees
        if worktrees_enabled:
            if ensure_executable("git"):
                _append_check(
                    checks,
                    "hub.git",
                    "ok",
                    "git is available for hub worktrees.",
                )
            else:
                _append_check(
                    checks,
                    "hub.git",
                    "error",
                    "git is not available but hub worktrees are enabled.",
                    "Install git or disable worktrees.",
                )

    telegram_cfg = None
    if isinstance(config.raw, dict):
        telegram_cfg = config.raw.get("telegram_bot")
    if isinstance(telegram_cfg, dict) and telegram_cfg.get("enabled") is True:
        missing_telegram = missing_optional_dependencies((("httpx", "httpx"),))
        if missing_telegram:
            deps_list = ", ".join(missing_telegram)
            _append_check(
                checks,
                "telegram.dependencies",
                "error",
                f"Telegram is enabled but missing optional deps: {deps_list}",
                "Install with `pip install codex-autorunner[telegram]`.",
            )
        else:
            _append_check(
                checks,
                "telegram.dependencies",
                "ok",
                "Telegram dependencies are installed.",
            )

    return DoctorReport(checks=checks)
