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
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any, Dict, Iterator, Optional, Union

import yaml

from ..agents.factory import create_orchestrator
from ..agents.opencode.logging import OpenCodeEventFormatter
from ..agents.opencode.runtime import (
    OpenCodeTurnOutput,
    build_turn_id,
    collect_opencode_output,
    extract_session_id,
    map_approval_policy_to_permission,
    opencode_missing_env,
    parse_message_response,
    split_model_id,
)
from ..agents.opencode.supervisor import OpenCodeSupervisor, OpenCodeSupervisorError
from ..agents.registry import validate_agent_id
from ..integrations.app_server.client import (
    CodexAppServerError,
    _extract_thread_id,
    _extract_thread_id_for_turn,
    _extract_turn_id,
)
from ..integrations.app_server.env import build_app_server_env
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ..manifest import MANIFEST_VERSION
from ..web.static_assets import missing_static_assets, resolve_static_dir
from .about_car import ensure_about_car_file
from .app_server_events import AppServerEventBuffer
from .app_server_logging import AppServerEventFormatter
from .app_server_prompts import build_autorunner_prompt
from .app_server_threads import AppServerThreadRegistry, default_app_server_threads_path
from .config import (
    ConfigError,
    RepoConfig,
    _is_loopback_host,
    derive_repo_config,
    load_hub_config,
    load_repo_config,
)
from .docs import DocsManager, parse_todos
from .git_utils import GitError, run_git
from .locks import (
    DEFAULT_RUNNER_CMD_HINTS,
    FileLock,
    FileLockBusy,
    assess_lock,
    process_alive,
    read_lock_info,
    write_lock_info,
)
from .notifications import NotificationManager
from .optional_dependencies import missing_optional_dependencies
from .prompt import build_final_summary_prompt
from .review_context import build_spec_progress_review_context
from .run_index import RunIndexStore
from .state import RunnerState, load_state, now_iso, save_state, state_lock
from .utils import (
    RepoNotFoundError,
    atomic_write,
    build_opencode_supervisor,
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


@dataclasses.dataclass
class RunTelemetry:
    run_id: int
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    token_total: Optional[dict[str, Any]] = None
    plan: Optional[Any] = None
    diff: Optional[Any] = None


@dataclasses.dataclass
class ActiveOpencodeRun:
    session_id: str
    turn_id: str
    client: Any
    interrupted: bool
    interrupt_event: asyncio.Event


class Engine:
    def __init__(
        self,
        repo_root: Path,
        *,
        config: Optional[RepoConfig] = None,
        hub_path: Optional[Path] = None,
    ):
        if config is None:
            config = load_repo_config(repo_root, hub_path=hub_path)
        self.config = config
        self.repo_root = self.config.root
        self.docs = DocsManager(self.config)
        self.notifier = NotificationManager(self.config)
        self.state_path = self.repo_root / ".codex-autorunner" / "state.sqlite3"
        self.log_path = self.config.log.path
        self._run_index_store = RunIndexStore(self.state_path)
        self.lock_path = self.repo_root / ".codex-autorunner" / "lock"
        self.stop_path = self.repo_root / ".codex-autorunner" / "stop"
        self._active_global_handler: Optional[RotatingFileHandler] = None
        self._active_run_log: Optional[IO[str]] = None
        self._app_server_threads = AppServerThreadRegistry(
            default_app_server_threads_path(self.repo_root)
        )
        self._app_server_threads_lock = threading.Lock()
        self._app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None
        self._app_server_logger = logging.getLogger("codex_autorunner.app_server")
        self._app_server_event_formatter = AppServerEventFormatter()
        self._app_server_events = AppServerEventBuffer()
        self._opencode_event_formatter = OpenCodeEventFormatter()
        self._opencode_supervisor: Optional[OpenCodeSupervisor] = None
        self._run_telemetry_lock = threading.Lock()
        self._run_telemetry: Optional[RunTelemetry] = None
        self._last_telemetry_update_time: float = 0.0
        self._last_run_interrupted = False
        self._lock_handle: Optional[FileLock] = None
        # Ensure the interactive TUI briefing doc exists (for web Terminal "New").
        try:
            ensure_about_car_file(self.config)
        except (OSError, IOError) as exc:
            # Never fail Engine creation due to a best-effort helper doc.
            self._app_server_logger.debug(
                "Best-effort ABOUT_CAR.md creation failed: %s", exc
            )

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
            assessment = assess_lock(
                self.lock_path,
                expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
            )
            if assessment.freeable:
                return "Autorunner lock is stale; clear it before continuing."
            pid = assessment.pid
            if pid and process_alive(pid):
                host = f" on {assessment.host}" if assessment.host else ""
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
        except (FileNotFoundError, OSError) as exc:
            self._app_server_logger.debug("Failed to read SUMMARY.md: %s", exc)
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
        except (FileNotFoundError, OSError) as exc:
            self._app_server_logger.debug(
                "Failed to read SUMMARY.md for stamping: %s", exc
            )
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
        try:
            todo_before = self.docs.read_doc("todo")
        except (FileNotFoundError, OSError) as exc:
            self._app_server_logger.debug("Failed to read TODO.md before run: %s", exc)
            todo_before = ""
        state = load_state(self.state_path)
        selected_agent = (state.autorunner_agent_override or "codex").strip().lower()
        try:
            validated_agent = validate_agent_id(selected_agent)
        except ValueError:
            validated_agent = "codex"
            self.log_line(
                run_id,
                f"info: unknown agent '{selected_agent}', defaulting to codex",
            )
        self._update_state("running", run_id, None, started=True)
        self._last_run_interrupted = False
        self._start_run_telemetry(run_id)
        with self._run_log_context(run_id):
            self._write_run_marker(run_id, "start")
            if validated_agent == "opencode":
                exit_code = await self._run_opencode_app_server_async(
                    prompt,
                    run_id,
                    model=state.autorunner_model_override,
                    reasoning=state.autorunner_effort_override,
                    external_stop_flag=external_stop_flag,
                )
            else:
                exit_code = await self._run_codex_app_server_async(
                    prompt,
                    run_id,
                    external_stop_flag=external_stop_flag,
                )
            self._write_run_marker(run_id, "end", exit_code=exit_code)

        try:
            todo_after = self.docs.read_doc("todo")
        except (FileNotFoundError, OSError) as exc:
            self._app_server_logger.debug("Failed to read TODO.md after run: %s", exc)
            todo_after = ""
        todo_delta = self._compute_todo_attribution(todo_before, todo_after)
        todo_snapshot = self._build_todo_snapshot(todo_before, todo_after)
        run_updates: dict[str, Any] = {
            "todo": todo_delta,
            "todo_snapshot": todo_snapshot,
        }
        telemetry = self._snapshot_run_telemetry(run_id)
        if (
            telemetry
            and telemetry.thread_id
            and isinstance(telemetry.token_total, dict)
        ):
            baseline = None
            # OpenCode reports per-turn totals, so skip cross-run deltas.
            if validated_agent != "opencode":
                baseline = self._find_thread_token_baseline(
                    thread_id=telemetry.thread_id, run_id=run_id
                )
            delta = self._compute_token_delta(baseline, telemetry.token_total)
            run_updates["token_usage"] = {
                "delta": delta,
                "thread_total_before": baseline,
                "thread_total_after": telemetry.token_total,
            }
        artifacts: dict[str, str] = {}
        if telemetry and telemetry.plan is not None:
            try:
                plan_content = (
                    telemetry.plan
                    if isinstance(telemetry.plan, str)
                    else json.dumps(
                        telemetry.plan, ensure_ascii=True, indent=2, default=str
                    )
                )
            except (TypeError, ValueError) as exc:
                self._app_server_logger.debug(
                    "Failed to serialize plan to JSON: %s", exc
                )
                plan_content = json.dumps(
                    {"plan": str(telemetry.plan)}, ensure_ascii=True, indent=2
                )
            plan_path = self._write_run_artifact(run_id, "plan.json", plan_content)
            artifacts["plan_path"] = str(plan_path)
        if telemetry and telemetry.diff is not None:
            diff_content = (
                telemetry.diff
                if isinstance(telemetry.diff, str)
                else json.dumps(
                    telemetry.diff, ensure_ascii=True, indent=2, default=str
                )
            )
            diff_path = self._write_run_artifact(run_id, "diff.patch", diff_content)
            artifacts["diff_path"] = str(diff_path)
        if artifacts:
            run_updates["artifacts"] = artifacts
        if run_updates:
            self._merge_run_index_entry(run_id, run_updates)
        self._clear_run_telemetry(run_id)
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
            except (FileNotFoundError, OSError) as exc:
                self._app_server_logger.debug(
                    "Failed to read previous run log: %s", exc
                )
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
            except (FileNotFoundError, OSError) as exc:
                self._app_server_logger.debug("Failed to read run log block: %s", exc)
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
        except (FileNotFoundError, OSError, ValueError) as exc:
            self._app_server_logger.debug(
                "Failed to read full log for run block: %s", exc
            )
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
            except (OSError, IOError) as exc:
                self._app_server_logger.warning(
                    "Failed to write to active run log: %s", exc
                )
        else:
            run_log = self._run_log_path(run_id)
            self._ensure_run_log_dir()
            with run_log.open("a", encoding="utf-8") as f:
                f.write(line)

    def _emit_event(self, run_id: int, event: str, **payload: Any) -> None:
        import json as _json

        event_data = {
            "ts": timestamp(),
            "event": event,
            "run_id": run_id,
        }
        if payload:
            event_data.update(payload)
        events_path = self._events_log_path(run_id)
        self._ensure_run_log_dir()
        try:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(event_data) + "\n")
        except (OSError, IOError) as exc:
            self._app_server_logger.warning(
                "Failed to write event to events log: %s", exc
            )

    def _ensure_log_path(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _run_log_path(self, run_id: int) -> Path:
        return self.log_path.parent / "runs" / f"run-{run_id}.log"

    def _events_log_path(self, run_id: int) -> Path:
        return self.log_path.parent / "runs" / f"run-{run_id}.events.jsonl"

    def _ensure_run_log_dir(self) -> None:
        (self.log_path.parent / "runs").mkdir(parents=True, exist_ok=True)

    def _write_run_marker(
        self, run_id: int, marker: str, exit_code: Optional[int] = None
    ) -> None:
        suffix = ""
        if marker == "end":
            suffix = f" (code {exit_code})"
            self._emit_event(run_id, "run.finished", exit_code=exit_code)
        elif marker == "start":
            self._emit_event(run_id, "run.started")
        text = f"=== run {run_id} {marker}{suffix} ==="
        offset = self._emit_global_line(text)
        if self._active_run_log is not None:
            try:
                self._active_run_log.write(f"{text}\n")
                self._active_run_log.flush()
            except (OSError, IOError) as exc:
                self._app_server_logger.warning(
                    "Failed to write marker to active run log: %s", exc
                )
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
            except (OSError, IOError) as exc:
                self._app_server_logger.warning(
                    "Failed to write global log line: %s", exc
                )
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
        except (OSError, IOError, RuntimeError) as exc:
            self._app_server_logger.warning("Failed to emit log via handler: %s", exc)
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
                except (OSError, IOError) as exc:
                    self._app_server_logger.debug(
                        "Failed to close run log handler: %s", exc
                    )

    def _start_run_telemetry(self, run_id: int) -> None:
        with self._run_telemetry_lock:
            self._run_telemetry = RunTelemetry(run_id=run_id)
        self._app_server_event_formatter.reset()
        self._opencode_event_formatter.reset()

    def _update_run_telemetry(self, run_id: int, **updates: Any) -> None:
        with self._run_telemetry_lock:
            telemetry = self._run_telemetry
            if telemetry is None or telemetry.run_id != run_id:
                return
            for key, value in updates.items():
                if hasattr(telemetry, key):
                    setattr(telemetry, key, value)

    def _snapshot_run_telemetry(self, run_id: int) -> Optional[RunTelemetry]:
        with self._run_telemetry_lock:
            telemetry = self._run_telemetry
            if telemetry is None or telemetry.run_id != run_id:
                return None
            return dataclasses.replace(telemetry)

    def _clear_run_telemetry(self, run_id: int) -> None:
        with self._run_telemetry_lock:
            telemetry = self._run_telemetry
            if telemetry is None or telemetry.run_id != run_id:
                return
            self._run_telemetry = None

    def _maybe_update_run_index_telemetry(
        self, run_id: int, min_interval_seconds: float = 3.0
    ) -> None:
        import time as _time

        now = _time.time()
        if now - self._last_telemetry_update_time < min_interval_seconds:
            return
        telemetry = self._snapshot_run_telemetry(run_id)
        if telemetry is None:
            return
        if telemetry.thread_id and isinstance(telemetry.token_total, dict):
            with state_lock(self.state_path):
                state = load_state(self.state_path)
                selected_agent = (
                    (state.autorunner_agent_override or "codex").strip().lower()
                )
            baseline = None
            if selected_agent != "opencode":
                baseline = self._find_thread_token_baseline(
                    thread_id=telemetry.thread_id, run_id=run_id
                )
            delta = self._compute_token_delta(baseline, telemetry.token_total)
            self._merge_run_index_entry(
                run_id,
                {
                    "token_usage": {
                        "delta": delta,
                        "thread_total_before": baseline,
                        "thread_total_after": telemetry.token_total,
                    }
                },
            )
            self._last_telemetry_update_time = now

    async def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        method = message.get("method")
        params_raw = message.get("params")
        params = params_raw if isinstance(params_raw, dict) else {}
        thread_id = (
            _extract_thread_id_for_turn(params)
            or _extract_thread_id(params)
            or _extract_thread_id(message)
        )
        turn_id = _extract_turn_id(params) or _extract_turn_id(message)
        run_id: Optional[int] = None
        with self._run_telemetry_lock:
            telemetry = self._run_telemetry
            if telemetry is None:
                return
            if telemetry.thread_id and thread_id and telemetry.thread_id != thread_id:
                return
            if telemetry.turn_id and turn_id and telemetry.turn_id != turn_id:
                return
            if telemetry.thread_id is None and thread_id:
                telemetry.thread_id = thread_id
            if telemetry.turn_id is None and turn_id:
                telemetry.turn_id = turn_id
            run_id = telemetry.run_id
            if method == "thread/tokenUsage/updated":
                token_usage = (
                    params.get("token_usage") or params.get("tokenUsage") or {}
                )
                if isinstance(token_usage, dict):
                    total = token_usage.get("total") or token_usage.get("totals")
                    if isinstance(total, dict):
                        telemetry.token_total = total
                        self._maybe_update_run_index_telemetry(run_id)
                        self._emit_event(run_id, "token.updated", token_total=total)
            if method == "turn/plan/updated":
                telemetry.plan = params.get("plan") if "plan" in params else params
            if method == "turn/diff/updated":
                diff = (
                    params.get("diff")
                    or params.get("patch")
                    or params.get("content")
                    or params.get("value")
                )
                telemetry.diff = diff if diff is not None else params
        if run_id is None:
            return
        for line in self._app_server_event_formatter.format_event(message):
            self.log_line(run_id, f"stdout: {line}" if line else "stdout: ")

    def _load_run_index(self) -> dict[str, dict]:
        return self._run_index_store.load_all()

    def _merge_run_index_entry(self, run_id: int, updates: dict[str, Any]) -> None:
        self._run_index_store.merge_entry(run_id, updates)

    def _update_run_index(
        self,
        run_id: int,
        marker: str,
        offset: Optional[tuple[int, int]],
        exit_code: Optional[int],
    ) -> None:
        self._run_index_store.update_marker(
            run_id,
            marker,
            offset,
            exit_code,
            log_path=str(self.log_path),
            run_log_path=str(self._run_log_path(run_id)),
        )

    def _list_from_counts(self, source: list[str], counts: Counter[str]) -> list[str]:
        if not source or not counts:
            return []
        remaining = Counter(counts)
        items: list[str] = []
        for entry in source:
            if remaining[entry] > 0:
                items.append(entry)
                remaining[entry] -= 1
        return items

    def _compute_todo_attribution(
        self, before_text: str, after_text: str
    ) -> dict[str, Any]:
        before_out, before_done = parse_todos(before_text or "")
        after_out, after_done = parse_todos(after_text or "")
        before_out_counter = Counter(before_out)
        before_done_counter = Counter(before_done)
        after_out_counter = Counter(after_out)
        after_done_counter = Counter(after_done)

        completed_counts: Counter[str] = Counter()
        for item, count in after_done_counter.items():
            if before_out_counter[item] > 0:
                completed_counts[item] = min(before_out_counter[item], count)

        reopened_counts: Counter[str] = Counter()
        for item, count in after_out_counter.items():
            if before_done_counter[item] > 0:
                reopened_counts[item] = min(before_done_counter[item], count)

        new_outstanding_counts = after_out_counter - before_out_counter
        added_counts = new_outstanding_counts - reopened_counts

        completed = self._list_from_counts(after_done, completed_counts)
        reopened = self._list_from_counts(after_out, reopened_counts)
        added = self._list_from_counts(after_out, added_counts)

        return {
            "completed": completed,
            "added": added,
            "reopened": reopened,
            "counts": {
                "completed": len(completed),
                "added": len(added),
                "reopened": len(reopened),
            },
        }

    def _build_todo_snapshot(self, before_text: str, after_text: str) -> dict[str, Any]:
        before_out, before_done = parse_todos(before_text or "")
        after_out, after_done = parse_todos(after_text or "")
        return {
            "before": {
                "outstanding": before_out,
                "done": before_done,
                "counts": {
                    "outstanding": len(before_out),
                    "done": len(before_done),
                },
            },
            "after": {
                "outstanding": after_out,
                "done": after_done,
                "counts": {
                    "outstanding": len(after_out),
                    "done": len(after_done),
                },
            },
        }

    def _find_thread_token_baseline(
        self, *, thread_id: str, run_id: int
    ) -> Optional[dict[str, Any]]:
        index = self._load_run_index()
        best_run = -1
        baseline: Optional[dict[str, Any]] = None
        for key, entry in index.items():
            try:
                entry_id = int(key)
            except (TypeError, ValueError) as exc:
                self._app_server_logger.debug(
                    "Failed to parse run index key '%s': %s", key, exc
                )
                continue
            if entry_id >= run_id:
                continue
            app_server = entry.get("app_server")
            if not isinstance(app_server, dict):
                continue
            if app_server.get("thread_id") != thread_id:
                continue
            token_usage = entry.get("token_usage")
            if not isinstance(token_usage, dict):
                continue
            total = token_usage.get("thread_total_after")
            if isinstance(total, dict) and entry_id > best_run:
                best_run = entry_id
                baseline = total
        return baseline

    def _compute_token_delta(
        self,
        baseline: Optional[dict[str, Any]],
        final_total: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(final_total, dict):
            return None
        base = baseline if isinstance(baseline, dict) else {}
        delta: dict[str, Any] = {}
        for key, value in final_total.items():
            if not isinstance(value, (int, float)):
                continue
            prior = base.get(key, 0)
            if isinstance(prior, (int, float)):
                delta[key] = value - prior
            else:
                delta[key] = value
        return delta

    def _build_app_server_meta(
        self,
        *,
        thread_id: str,
        turn_id: str,
        thread_info: Optional[dict[str, Any]],
        model: Optional[str],
        reasoning_effort: Optional[str],
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {"thread_id": thread_id, "turn_id": turn_id}
        if model:
            meta["model"] = model
        if reasoning_effort:
            meta["reasoning_effort"] = reasoning_effort
        if not isinstance(thread_info, dict):
            return meta

        def _first_string(keys: tuple[str, ...]) -> Optional[str]:
            for key in keys:
                value = thread_info.get(key)
                if isinstance(value, str) and value:
                    return value
            return None

        if "model" not in meta:
            thread_model = _first_string(("model", "model_id", "modelId", "model_name"))
            if thread_model:
                meta["model"] = thread_model
        provider = _first_string(
            ("model_provider", "modelProvider", "provider", "model_provider_name")
        )
        if provider:
            meta["model_provider"] = provider
        if "reasoning_effort" not in meta:
            thread_effort = _first_string(
                ("reasoning_effort", "reasoningEffort", "effort")
            )
            if thread_effort:
                meta["reasoning_effort"] = thread_effort
        return meta

    def _write_run_artifact(self, run_id: int, name: str, content: str) -> Path:
        self._ensure_run_log_dir()
        path = self.log_path.parent / "runs" / f"run-{run_id}.{name}"
        atomic_write(path, content)
        return path

    def _read_log_range(self, entry: dict) -> Optional[str]:
        start = entry.get("start_offset")
        end = entry.get("end_offset")
        if start is None or end is None:
            return None
        try:
            start_offset = int(start)
            end_offset = int(end)
        except (TypeError, ValueError) as exc:
            self._app_server_logger.debug("Failed to parse log range offsets: %s", exc)
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
        except (FileNotFoundError, OSError) as exc:
            self._app_server_logger.debug("Failed to read log range: %s", exc)
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

    async def _run_agent_turn_async(
        self,
        agent_id: str,
        prompt: str,
        run_id: int,
        *,
        external_stop_flag: Optional[threading.Event] = None,
    ) -> int:
        orchestrator = self._get_orchestrator(agent_id)
        if orchestrator is None:
            self.log_line(
                run_id,
                f"error: agent '{agent_id}' backend is not configured",
            )
            return 1

        thread_key = f"autorunner.{agent_id}"
        with state_lock(self.state_path):
            state = load_state(self.state_path)
        effective_model = state.autorunner_model_override or self.config.codex_model
        effective_effort = (
            state.autorunner_effort_override or self.config.codex_reasoning
        )

        with self._app_server_threads_lock:
            conversation_id = self._app_server_threads.get_thread_id(thread_key)
            if not conversation_id:
                try:
                    conversation_info = (
                        await orchestrator.create_or_resume_conversation(
                            self.repo_root, agent_id
                        )
                    )
                    conversation_id = conversation_info.id
                    self._app_server_threads.set_thread_id(thread_key, conversation_id)
                except Exception as exc:
                    self.log_line(
                        run_id, f"error: failed to create conversation: {exc}"
                    )
                    return 1

        if conversation_id:
            self._update_run_telemetry(run_id, thread_id=conversation_id)

        approval_policy = state.autorunner_approval_policy or "never"
        sandbox_mode = state.autorunner_sandbox_mode or "dangerFullAccess"
        if sandbox_mode == "workspaceWrite":
            sandbox_policy: Union[Dict[str, Any], str] = {
                "type": "workspaceWrite",
                "writableRoots": [str(self.repo_root)],
                "networkAccess": bool(state.autorunner_workspace_write_network),
            }
        else:
            sandbox_policy = sandbox_mode

        stop_event = asyncio.Event()
        stop_task: Optional[asyncio.Task] = None

        if external_stop_flag:
            stop_task = asyncio.create_task(
                self._wait_for_stop(external_stop_flag, stop_event)
            )

        try:
            result = await orchestrator.run_turn(
                self.repo_root,
                conversation_id,
                prompt,
                model=effective_model,
                reasoning=effective_effort,
                approval_mode=approval_policy,
                sandbox_policy=sandbox_policy,
            )
            if result.get("status") != "completed":
                self.log_line(
                    run_id, f"error: turn failed with status {result.get('status')}"
                )
                return 1
            output = result.get("output", "")
            if output:
                self._log_app_server_output(run_id, output.splitlines())
                output_path = self._write_run_artifact(run_id, "output.txt", output)
                self._merge_run_index_entry(
                    run_id, {"artifacts": {"output_path": str(output_path)}}
                )
            return 0
        except Exception as exc:
            self.log_line(run_id, f"error: {exc}")
            return 1
        finally:
            if stop_task is not None:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            if stop_event.is_set():
                await orchestrator.interrupt_turn(
                    self.repo_root, conversation_id, grace_seconds=30.0
                )
                self._last_run_interrupted = True

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
        with state_lock(self.state_path):
            state = load_state(self.state_path)
        effective_model = state.autorunner_model_override or config.codex_model
        effective_effort = state.autorunner_effort_override or config.codex_reasoning
        approval_policy = state.autorunner_approval_policy or "never"
        sandbox_mode = state.autorunner_sandbox_mode or "dangerFullAccess"
        if sandbox_mode == "workspaceWrite":
            sandbox_policy: Any = {
                "type": "workspaceWrite",
                "writableRoots": [str(self.repo_root)],
                "networkAccess": bool(state.autorunner_workspace_write_network),
            }
        else:
            sandbox_policy = sandbox_mode
        try:
            client = await supervisor.get_client(self.repo_root)
            with self._app_server_threads_lock:
                thread_id = self._app_server_threads.get_thread_id("autorunner")
                thread_info: Optional[dict[str, Any]] = None
                if thread_id:
                    try:
                        resume_result = await client.thread_resume(thread_id)
                        resumed = resume_result.get("id")
                        if isinstance(resumed, str) and resumed:
                            thread_id = resumed
                            self._app_server_threads.set_thread_id(
                                "autorunner", thread_id
                            )
                        if isinstance(resume_result, dict):
                            thread_info = resume_result
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
                    if isinstance(thread, dict):
                        thread_info = thread
            if thread_id:
                self._update_run_telemetry(run_id, thread_id=thread_id)
            turn_kwargs: dict[str, Any] = {}
            if effective_model:
                turn_kwargs["model"] = str(effective_model)
            if effective_effort:
                turn_kwargs["effort"] = str(effective_effort)
            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                **turn_kwargs,
            )
            app_server_meta = self._build_app_server_meta(
                thread_id=thread_id,
                turn_id=handle.turn_id,
                thread_info=thread_info,
                model=turn_kwargs.get("model"),
                reasoning_effort=turn_kwargs.get("effort"),
            )
            self._merge_run_index_entry(run_id, {"app_server": app_server_meta})
            self._update_run_telemetry(
                run_id, thread_id=thread_id, turn_id=handle.turn_id
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
            output_text = "\n\n".join(turn_result.agent_messages).strip()
            if output_text:
                output_path = self._write_run_artifact(
                    run_id, "output.txt", output_text
                )
                self._merge_run_index_entry(
                    run_id, {"artifacts": {"output_path": str(output_path)}}
                )
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
            notification_handler=self._handle_app_server_notification,
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

    def _build_opencode_supervisor(self) -> Optional[OpenCodeSupervisor]:
        config = self.config.app_server
        opencode_command = self.config.agent_serve_command("opencode")
        opencode_binary = None
        try:
            opencode_binary = self.config.agent_binary("opencode")
        except ConfigError:
            opencode_binary = None

        supervisor = build_opencode_supervisor(
            opencode_command=opencode_command,
            opencode_binary=opencode_binary,
            workspace_root=self.repo_root,
            logger=self._app_server_logger,
            request_timeout=config.request_timeout,
            max_handles=config.max_handles,
            idle_ttl_seconds=config.idle_ttl_seconds,
            base_env=None,
        )

        if supervisor is None:
            self._app_server_logger.info(
                "OpenCode command unavailable; skipping opencode supervisor."
            )
            return None

        return supervisor

    def _ensure_opencode_supervisor(self) -> Optional[OpenCodeSupervisor]:
        if self._opencode_supervisor is None:
            self._opencode_supervisor = self._build_opencode_supervisor()
        return self._opencode_supervisor

    async def _close_opencode_supervisor(self) -> None:
        if self._opencode_supervisor is None:
            return
        supervisor = self._opencode_supervisor
        self._opencode_supervisor = None
        try:
            await supervisor.close_all()
        except Exception as exc:
            self._app_server_logger.warning("opencode supervisor close failed: %s", exc)

    def _get_orchestrator(self, agent_id: str):
        if agent_id == "opencode":
            opencode_sup = self._ensure_opencode_supervisor()
            if opencode_sup is None:
                return None
            return create_orchestrator(agent_id, opencode_supervisor=opencode_sup)
        else:
            app_server_sup = self._ensure_app_server_supervisor(
                lambda workspace_root, workspace_id, state_dir: {}
            )
            return create_orchestrator(
                agent_id,
                codex_supervisor=app_server_sup,
                codex_events=self._app_server_events,
            )

    async def _wait_for_stop(
        self,
        external_stop_flag: Optional[threading.Event],
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        while not self._should_stop(external_stop_flag):
            await asyncio.sleep(AUTORUNNER_STOP_POLL_SECONDS)
        if stop_event is not None:
            stop_event.set()

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

    async def _abort_opencode(self, client: Any, session_id: str, run_id: int) -> None:
        try:
            await client.abort(session_id)
        except Exception as exc:
            self.log_line(run_id, f"error: opencode abort failed: {exc}")

    async def _run_opencode_app_server_async(
        self,
        prompt: str,
        run_id: int,
        *,
        model: Optional[str],
        reasoning: Optional[str],
        external_stop_flag: Optional[threading.Event] = None,
    ) -> int:
        supervisor = self._ensure_opencode_supervisor()
        if supervisor is None:
            self.log_line(
                run_id, "error: opencode backend is not configured in this repo"
            )
            return 1
        try:
            client = await supervisor.get_client(self.repo_root)
        except OpenCodeSupervisorError as exc:
            self.log_line(run_id, f"error: opencode backend unavailable: {exc}")
            return 1

        with self._app_server_threads_lock:
            key = "autorunner.opencode"
            thread_id = self._app_server_threads.get_thread_id(key)
            if thread_id:
                try:
                    await client.get_session(thread_id)
                except Exception as exc:
                    self._app_server_logger.debug(
                        "Failed to get existing opencode session '%s': %s",
                        thread_id,
                        exc,
                    )
                    self._app_server_threads.reset_thread(key)
                    thread_id = None
            if not thread_id:
                session = await client.create_session(directory=str(self.repo_root))
                thread_id = extract_session_id(session, allow_fallback_id=True)
                if not isinstance(thread_id, str) or not thread_id:
                    self.log_line(run_id, "error: opencode did not return a session id")
                    return 1
                self._app_server_threads.set_thread_id(key, thread_id)

        model_payload = split_model_id(model)
        missing_env = await opencode_missing_env(
            client, str(self.repo_root), model_payload
        )
        if missing_env:
            provider_id = model_payload.get("providerID") if model_payload else None
            self.log_line(
                run_id,
                "error: opencode provider "
                f"{provider_id or 'selected'} requires env vars: "
                f"{', '.join(missing_env)}",
            )
            return 1
        opencode_turn_started = False
        await supervisor.mark_turn_started(self.repo_root)
        opencode_turn_started = True
        turn_id = build_turn_id(thread_id)
        self._update_run_telemetry(run_id, thread_id=thread_id, turn_id=turn_id)
        app_server_meta = self._build_app_server_meta(
            thread_id=thread_id,
            turn_id=turn_id,
            thread_info=None,
            model=model,
            reasoning_effort=reasoning,
        )
        app_server_meta["agent"] = "opencode"
        self._merge_run_index_entry(run_id, {"app_server": app_server_meta})

        active = ActiveOpencodeRun(
            session_id=thread_id,
            turn_id=turn_id,
            client=client,
            interrupted=False,
            interrupt_event=asyncio.Event(),
        )
        with state_lock(self.state_path):
            state = load_state(self.state_path)
        permission_policy = map_approval_policy_to_permission(
            state.autorunner_approval_policy, default="allow"
        )

        async def _opencode_part_handler(
            part_type: str, part: dict[str, Any], delta_text: Optional[str]
        ) -> None:
            if part_type == "usage" and isinstance(part, dict):
                for line in self._opencode_event_formatter.format_usage(part):
                    self.log_line(run_id, f"stdout: {line}" if line else "stdout: ")
            else:
                for line in self._opencode_event_formatter.format_part(
                    part_type, part, delta_text
                ):
                    self.log_line(run_id, f"stdout: {line}" if line else "stdout: ")

        ready_event = asyncio.Event()
        output_task = asyncio.create_task(
            collect_opencode_output(
                client,
                session_id=thread_id,
                workspace_path=str(self.repo_root),
                permission_policy=permission_policy,
                question_policy="auto_first_option",
                should_stop=active.interrupt_event.is_set,
                part_handler=_opencode_part_handler,
                ready_event=ready_event,
            )
        )
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ready_event.wait(), timeout=2.0)
        prompt_task = asyncio.create_task(
            client.prompt_async(
                thread_id,
                message=prompt,
                model=model_payload,
                variant=reasoning,
            )
        )
        stop_task = asyncio.create_task(self._wait_for_stop(external_stop_flag))
        timeout_task = None
        turn_timeout = self.config.app_server.turn_timeout_seconds
        if turn_timeout:
            timeout_task = asyncio.create_task(asyncio.sleep(turn_timeout))
        timed_out = False
        try:
            try:
                prompt_response = await prompt_task
                prompt_info = (
                    prompt_response.get("info")
                    if isinstance(prompt_response, dict)
                    else {}
                )
                tokens = (
                    prompt_info.get("tokens") if isinstance(prompt_info, dict) else {}
                )
                if isinstance(tokens, dict):
                    input_tokens = int(tokens.get("input", 0) or 0)
                    cached_read = (
                        int(tokens.get("cache", {}).get("read", 0) or 0)
                        if isinstance(tokens.get("cache"), dict)
                        else 0
                    )
                    output_tokens = int(tokens.get("output", 0) or 0)
                    reasoning_tokens = int(tokens.get("reasoning", 0) or 0)
                    total_tokens = (
                        input_tokens + cached_read + output_tokens + reasoning_tokens
                    )
                    token_total = {
                        "total": total_tokens,
                        "input_tokens": input_tokens,
                        "prompt_tokens": input_tokens,
                        "cached_input_tokens": cached_read,
                        "output_tokens": output_tokens,
                        "completion_tokens": output_tokens,
                        "reasoning_tokens": reasoning_tokens,
                        "reasoning_output_tokens": reasoning_tokens,
                    }
                    self._update_run_telemetry(run_id, token_total=token_total)
            except Exception as exc:
                active.interrupt_event.set()
                output_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await output_task
                self.log_line(run_id, f"error: opencode prompt failed: {exc}")
                return 1
            tasks = {output_task, stop_task}
            if timeout_task is not None:
                tasks.add(timeout_task)
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            timed_out = timeout_task is not None and timeout_task in done
            stopped = stop_task in done
            if timed_out:
                self.log_line(
                    run_id, "error: opencode turn timed out; aborting session"
                )
                active.interrupt_event.set()
            if stopped:
                active.interrupted = True
                active.interrupt_event.set()
                self.log_line(run_id, "info: stop requested; aborting opencode")
            if timed_out or stopped:
                await self._abort_opencode(client, thread_id, run_id)
                done, _pending = await asyncio.wait(
                    {output_task}, timeout=AUTORUNNER_INTERRUPT_GRACE_SECONDS
                )
                if not done:
                    output_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await output_task
                    if timed_out:
                        return 1
                    self._last_run_interrupted = active.interrupted
                    return 0
            output_result = await output_task
            if not output_result.text and not output_result.error:
                fallback = parse_message_response(prompt_response)
                if fallback.text:
                    output_result = OpenCodeTurnOutput(
                        text=fallback.text, error=fallback.error
                    )
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            if timeout_task is not None:
                timeout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timeout_task
            if opencode_turn_started:
                await supervisor.mark_turn_finished(self.repo_root)

        output = output_result.text
        if output:
            self._log_app_server_output(run_id, [output])
            output_text = output.strip()
            if output_text:
                output_path = self._write_run_artifact(
                    run_id, "output.txt", output_text
                )
                self._merge_run_index_entry(
                    run_id, {"artifacts": {"output_path": str(output_path)}}
                )
        if output_result.error:
            self.log_line(
                run_id, f"error: opencode session error: {output_result.error}"
            )
            return 1
        self._last_run_interrupted = active.interrupted
        if timed_out:
            return 1
        return 0

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
            else (
                state.runner_stop_after_runs
                if state.runner_stop_after_runs is not None
                else self.config.runner_stop_after_runs
            )
        )
        no_progress_count = 0
        last_outstanding_count = len(self.docs.todos()[0])
        last_done_count = len(self.docs.todos()[1])
        exit_reason: Optional[str] = None

        try:
            while True:
                if self._should_stop(external_stop_flag):
                    self.clear_stop_request()
                    self._update_state(
                        "idle", run_id - 1, last_exit_code, finished=True
                    )
                    exit_reason = "stop_requested"
                    break
                if self.config.runner_max_wallclock_seconds is not None:
                    if (
                        time.time() - start_wallclock
                        > self.config.runner_max_wallclock_seconds
                    ):
                        self._update_state(
                            "idle", run_id - 1, state.last_exit_code, finished=True
                        )
                        exit_reason = "max_wallclock_seconds"
                        break

                if self.todos_done():
                    if not self.summary_finalized():
                        exit_code = await self._run_final_summary_job(
                            run_id, external_stop_flag=external_stop_flag
                        )
                        last_exit_code = exit_code
                        exit_reason = (
                            "error_exit" if exit_code != 0 else "todos_complete"
                        )
                    else:
                        current = load_state(self.state_path)
                        last_exit_code = current.last_exit_code
                        self._update_state(
                            "idle", run_id - 1, last_exit_code, finished=True
                        )
                        exit_reason = "todos_complete"
                    break

                prev_output = self.extract_prev_output(run_id - 1)
                prompt = self._build_app_server_prompt(prev_output)

                exit_code = await self._execute_run_step(
                    prompt, run_id, external_stop_flag=external_stop_flag
                )
                last_exit_code = exit_code

                if exit_code != 0:
                    exit_reason = "error_exit"
                    break

                # Check for no progress across runs
                current_outstanding, current_done = self.docs.todos()
                current_outstanding_count = len(current_outstanding)
                current_done_count = len(current_done)

                # Check if there was any meaningful progress
                has_progress = (
                    current_outstanding_count != last_outstanding_count
                    or current_done_count != last_done_count
                )

                # Check if there was any meaningful output (diff, files changed, etc.)
                has_output = False
                try:
                    output_path = (
                        self.repo_root
                        / ".codex-autorunner"
                        / "runs"
                        / f"run-{run_id}"
                        / "output.txt"
                    )
                    if output_path.exists():
                        output_content = output_path.read_text(encoding="utf-8").strip()
                        # Consider it output if there's meaningful text (not just empty or whitespace)
                        has_output = len(output_content) > 100
                except (OSError, IOError):
                    pass

                if not has_progress and not has_output:
                    no_progress_count += 1
                    self.log_line(
                        run_id,
                        f"info: no progress detected ({no_progress_count}/{self.config.runner_no_progress_threshold} runs without progress)",
                    )
                    if no_progress_count >= self.config.runner_no_progress_threshold:
                        self.log_line(
                            run_id,
                            f"info: stopping after {no_progress_count} consecutive runs with no progress (threshold: {self.config.runner_no_progress_threshold})",
                        )
                        self._update_state(
                            "idle",
                            run_id,
                            exit_code,
                            finished=True,
                        )
                        exit_reason = "no_progress_threshold"
                        break
                else:
                    no_progress_count = 0

                last_outstanding_count = current_outstanding_count
                last_done_count = current_done_count

                # If TODO is now complete, run the final report job once and stop.
                if self.todos_done() and not self.summary_finalized():
                    exit_code = await self._run_final_summary_job(
                        run_id + 1, external_stop_flag=external_stop_flag
                    )
                    last_exit_code = exit_code
                    exit_reason = "error_exit" if exit_code != 0 else "todos_complete"
                    break

                if target_runs is not None and run_id >= target_runs:
                    exit_reason = "stop_after_runs"
                    break

                run_id += 1
                if self._should_stop(external_stop_flag):
                    self.clear_stop_request()
                    self._update_state("idle", run_id - 1, exit_code, finished=True)
                    exit_reason = "stop_requested"
                    break
                await asyncio.sleep(self.config.runner_sleep_seconds)
        except Exception as exc:
            # Never silently die: persist's reason to agent log and surface in state.
            exit_reason = exit_reason or "error_exit"
            try:
                self.log_line(run_id, f"FATAL: run_loop crashed: {exc!r}")
                tb = traceback.format_exc()
                for line in tb.splitlines():
                    self.log_line(run_id, f"traceback: {line}")
            except (OSError, IOError) as exc:
                self._app_server_logger.error("Failed to log run_loop crash: %s", exc)
            try:
                self._update_state("error", run_id, 1, finished=True)
            except (OSError, IOError) as exc:
                self._app_server_logger.error(
                    "Failed to update state after run_loop crash: %s", exc
                )
        finally:
            try:
                await self._maybe_run_end_review(
                    exit_reason=exit_reason or "unknown",
                    last_exit_code=last_exit_code,
                )
            except Exception as exc:
                self._app_server_logger.warning("End-of-run review failed: %s", exc)
            await self._close_app_server_supervisor()
            await self._close_opencode_supervisor()
        # IMPORTANT: lock ownership is managed by the caller (CLI/Hub/Server runner).
        # Engine.run_loop must never unconditionally mutate the lock file.

    async def _maybe_run_end_review(
        self, *, exit_reason: str, last_exit_code: Optional[int]
    ) -> None:
        runner_cfg = self.config.raw.get("runner") or {}
        review_cfg = runner_cfg.get("review")
        if not isinstance(review_cfg, dict) or not review_cfg.get("enabled"):
            return

        trigger_cfg = review_cfg.get("trigger") or {}
        reason_key_map = {
            "todos_complete": "on_todos_complete",
            "no_progress_threshold": "on_no_progress_stop",
            "stop_after_runs": "on_max_runs_stop",
            # Share the max-runs trigger for wallclock cutoffs to avoid extra config flags.
            "max_wallclock_seconds": "on_max_runs_stop",
            "stop_requested": "on_stop_requested",
            "error_exit": "on_error_exit",
        }
        trigger_key = reason_key_map.get(exit_reason)
        if not trigger_key or not trigger_cfg.get(trigger_key, False):
            return

        state = load_state(self.state_path)
        last_run_id = state.last_run_id
        if last_run_id is None:
            return

        top_review_cfg = self.config.raw.get("review") or {}
        agent = review_cfg.get("agent") or top_review_cfg.get("agent") or "opencode"
        model = review_cfg.get("model") or top_review_cfg.get("model")
        reasoning = review_cfg.get("reasoning") or top_review_cfg.get("reasoning")
        max_wallclock_seconds = review_cfg.get("max_wallclock_seconds")
        if max_wallclock_seconds is None:
            max_wallclock_seconds = top_review_cfg.get("max_wallclock_seconds")

        context_cfg = review_cfg.get("context") or {}
        primary_docs = context_cfg.get("primary_docs") or ["spec", "progress"]
        include_docs = context_cfg.get("include_docs") or []
        include_last_run_artifacts = bool(
            context_cfg.get("include_last_run_artifacts", True)
        )
        max_doc_chars = context_cfg.get("max_doc_chars", 20000)
        try:
            max_doc_chars = int(max_doc_chars)
        except (TypeError, ValueError):
            max_doc_chars = 20000

        context_md = build_spec_progress_review_context(
            self,
            exit_reason=exit_reason,
            last_run_id=last_run_id,
            last_exit_code=last_exit_code,
            max_doc_chars=max_doc_chars,
            primary_docs=primary_docs,
            include_docs=include_docs,
            include_last_run_artifacts=include_last_run_artifacts,
        )

        payload: dict[str, Any] = {
            "agent": agent,
            "model": model,
            "reasoning": reasoning,
            "max_wallclock_seconds": max_wallclock_seconds,
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        opencode_supervisor: Optional[OpenCodeSupervisor] = None
        app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None

        if agent == "codex":
            if not self.config.app_server.command:
                self._app_server_logger.info(
                    "Skipping end-of-run review: codex backend not configured"
                )
                return

            def _env_builder(
                workspace_root: Path, _workspace_id: str, state_dir: Path
            ) -> dict[str, str]:
                state_dir.mkdir(parents=True, exist_ok=True)
                return build_app_server_env(
                    self.config.app_server.command,
                    workspace_root,
                    state_dir,
                    logger=self._app_server_logger,
                    event_prefix="review",
                )

            app_server_supervisor = self._ensure_app_server_supervisor(_env_builder)
        else:
            opencode_supervisor = self._ensure_opencode_supervisor()
            if opencode_supervisor is None:
                self._app_server_logger.info(
                    "Skipping end-of-run review: opencode backend not configured"
                )
                return

        from .review import ReviewService

        review_service = ReviewService(
            self,
            opencode_supervisor=opencode_supervisor,
            app_server_supervisor=app_server_supervisor,
            logger=self._app_server_logger,
        )
        result_state = await review_service.run_blocking_async(
            payload=payload,
            prompt_kind="spec_progress",
            seed_context_files={"AUTORUNNER_CONTEXT.md": context_md},
            ignore_repo_busy=True,
        )

        review_id = result_state.get("id")
        artifacts_cfg = review_cfg.get("artifacts") or {}
        attach = bool(artifacts_cfg.get("attach_to_last_run_index", True))
        if attach:
            artifacts_update: dict[str, str] = {}
            final_report = result_state.get("final_output_path")
            scratch_bundle = result_state.get("scratchpad_bundle_path")
            if isinstance(final_report, str) and final_report:
                artifacts_update["final_review_report_path"] = final_report
            if isinstance(scratch_bundle, str) and scratch_bundle:
                artifacts_update["final_review_scratchpad_bundle_path"] = scratch_bundle
            if artifacts_update:
                self._merge_run_index_entry(
                    last_run_id,
                    {"artifacts": artifacts_update},
                )
        if review_id:
            self.log_line(
                last_run_id,
                f"info: end-of-run review completed (review_id={review_id})",
            )

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
                autorunner_agent_override=current.autorunner_agent_override,
                autorunner_model_override=current.autorunner_model_override,
                autorunner_effort_override=current.autorunner_effort_override,
                autorunner_approval_policy=current.autorunner_approval_policy,
                autorunner_sandbox_mode=current.autorunner_sandbox_mode,
                autorunner_workspace_write_network=current.autorunner_workspace_write_network,
                runner_pid=runner_pid,
                sessions=current.sessions,
                repo_to_session=current.repo_to_session,
            )
            save_state(self.state_path, new_state)


def clear_stale_lock(lock_path: Path) -> bool:
    assessment = assess_lock(
        lock_path,
        expected_cmd_substrings=DEFAULT_RUNNER_CMD_HINTS,
    )
    if assessment.freeable:
        lock_path.unlink(missing_ok=True)
        return True
    return False


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
    Read at most last `max_bytes` bytes from a UTF-8-ish text file.
    Returns decoded text with errors replaced.
    """
    logger = logging.getLogger("codex_autorunner.engine")
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.debug("Failed to stat log file for tail read: %s", exc)
        return ""
    if size <= 0:
        return ""
    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError, IOError) as exc:
        logger.debug("Failed to read tail of log file: %s", exc)
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
    logger = logging.getLogger("codex_autorunner.engine")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        logger.debug("Failed to parse manifest version: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    return int(version) if isinstance(version, int) else None


def _manifest_has_worktrees(manifest_path: Path) -> bool:
    logger = logging.getLogger("codex_autorunner.engine")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        logger.debug("Failed to parse manifest for worktrees: %s", exc)
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
    hub_config = load_hub_config(start_path)
    repo_config: Optional[RepoConfig] = None
    try:
        repo_root = find_repo_root(start_path)
        repo_config = derive_repo_config(hub_config, repo_root)
    except RepoNotFoundError:
        repo_config = None
    checks: list[DoctorCheck] = []
    config = repo_config or hub_config
    root = config.root

    if repo_config is not None:
        missing = []
        for key in ("todo", "progress", "opinions"):
            path = repo_config.doc_path(key)
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

        if ensure_executable(repo_config.codex_binary):
            _append_check(
                checks,
                "codex.binary",
                "ok",
                f"Codex binary resolved: {repo_config.codex_binary}",
            )
        else:
            _append_check(
                checks,
                "codex.binary",
                "error",
                f"Codex binary not found in PATH: {repo_config.codex_binary}",
                "Install Codex or set codex.binary to a full path.",
            )

        voice_enabled = bool(repo_config.voice.get("enabled", True))
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

    if hub_config.manifest_path.exists():
        version = _parse_manifest_version(hub_config.manifest_path)
        if version is None:
            _append_check(
                checks,
                "hub.manifest.version",
                "error",
                f"Failed to read manifest version from {hub_config.manifest_path}.",
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
            f"Hub manifest missing at {hub_config.manifest_path}.",
            "Run `car hub scan` or `car hub create` to generate it.",
        )

    if not hub_config.repos_root.exists():
        _append_check(
            checks,
            "hub.repos_root",
            "error",
            f"Hub repos_root does not exist: {hub_config.repos_root}",
            "Create the directory or update hub.repos_root in config.",
        )
    elif not hub_config.repos_root.is_dir():
        _append_check(
            checks,
            "hub.repos_root",
            "error",
            f"Hub repos_root is not a directory: {hub_config.repos_root}",
            "Point hub.repos_root at a directory.",
        )
    else:
        _append_check(
            checks,
            "hub.repos_root",
            "ok",
            f"Hub repos_root exists: {hub_config.repos_root}",
        )

    manifest_has_worktrees = (
        hub_config.manifest_path.exists()
        and _manifest_has_worktrees(hub_config.manifest_path)
    )
    worktrees_enabled = hub_config.worktrees_root.exists() or manifest_has_worktrees
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
