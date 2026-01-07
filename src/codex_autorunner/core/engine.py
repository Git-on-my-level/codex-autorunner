import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
import threading
import traceback

from .about_car import ensure_about_car_file
from .codex_runner import run_codex_streaming
from .config import ConfigError, load_config
from .docs import DocsManager
from .locks import process_alive, read_lock_info, write_lock_info
from .notifications import NotificationManager
from .prompt import build_final_summary_prompt, build_prompt
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


class Engine:
    def __init__(self, repo_root: Path):
        self.config = load_config(repo_root)
        self.repo_root = self.config.root
        self.docs = DocsManager(self.config)
        self.notifier = NotificationManager(self.config)
        self.state_path = self.repo_root / ".codex-autorunner" / "state.json"
        self.log_path = self.config.log.path
        self.run_index_path = self.repo_root / ".codex-autorunner" / "run_index.json"
        self.lock_path = self.repo_root / ".codex-autorunner" / "lock"
        self.stop_path = self.repo_root / ".codex-autorunner" / "stop"
        self._active_global_handler: Optional[RotatingFileHandler] = None
        self._active_run_log = None
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
        if self.lock_path.exists():
            info = read_lock_info(self.lock_path)
            pid = info.pid
            if pid and process_alive(pid):
                if not force:
                    raise LockError(
                        f"Another autorunner is active (pid={pid}); use --force to override"
                    )
            else:
                self.lock_path.unlink(missing_ok=True)
        write_lock_info(self.lock_path, os.getpid(), started_at=now_iso())

    def release_lock(self) -> None:
        if self.lock_path.exists():
            self.lock_path.unlink()

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

    def _execute_run_step(self, prompt: str, run_id: int) -> int:
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
        with self._run_log_context(run_id):
            self._write_run_marker(run_id, "start")
            exit_code = self.run_codex_cli(prompt, run_id)
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
            self.maybe_git_commit(run_id)

        return exit_code

    def _run_final_summary_job(self, run_id: int) -> int:
        """
        Run a dedicated Codex invocation that produces/updates SUMMARY.md as the final user report.
        """
        prev_output = self.extract_prev_output(run_id - 1)
        prompt = build_final_summary_prompt(self.config, self.docs, prev_output)

        exit_code = self._execute_run_step(prompt, run_id)

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
        if index_entry:
            block = self._read_log_range(index_entry)
            if block is not None:
                return block
        run_log = self._run_log_path(run_id)
        if run_log.exists():
            try:
                return run_log.read_text(encoding="utf-8")
            except Exception:
                return None
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
    def _run_log_context(self, run_id: int) -> None:
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
        elif marker == "end":
            entry["end_offset"] = offset[1] if offset else None
            entry["finished_at"] = now_iso()
            entry["exit_code"] = exit_code
            entry.setdefault("log_path", str(self.log_path))
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

    def run_codex_cli(self, prompt: str, run_id: int) -> int:
        def _log_stdout(line: str) -> None:
            self.log_line(run_id, f"stdout: {line}" if line else "stdout: ")

        return run_codex_streaming(
            self.config,
            self.repo_root,
            prompt,
            on_stdout_line=_log_stdout,
        )

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
        add_cmd = ["git", "add"] + [
            str(p.relative_to(self.repo_root)) for p in paths if p.exists()
        ]
        subprocess.run(add_cmd, cwd=self.repo_root, check=False)
        subprocess.run(["git", "commit", "-m", msg], cwd=self.repo_root, check=False)

    def run_loop(
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
                        exit_code = self._run_final_summary_job(run_id)
                        last_exit_code = exit_code
                    else:
                        current = load_state(self.state_path)
                        last_exit_code = current.last_exit_code
                        self._update_state(
                            "idle", run_id - 1, last_exit_code, finished=True
                        )
                    break

                prev_output = self.extract_prev_output(run_id - 1)
                prompt = build_prompt(self.config, self.docs, prev_output)

                exit_code = self._execute_run_step(prompt, run_id)
                last_exit_code = exit_code

                if exit_code != 0:
                    break

                # If TODO is now complete, run the final report job once and stop.
                if self.todos_done() and not self.summary_finalized():
                    exit_code = self._run_final_summary_job(run_id + 1)
                    last_exit_code = exit_code
                    break

                if target_runs is not None and run_id >= target_runs:
                    break

                run_id += 1
                if self._should_stop(external_stop_flag):
                    self.clear_stop_request()
                    self._update_state("idle", run_id - 1, exit_code, finished=True)
                    break
                time.sleep(self.config.runner_sleep_seconds)
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
        # IMPORTANT: lock ownership is managed by the caller (CLI/Hub/Server runner).
        # Engine.run_loop must never unconditionally mutate the lock file.

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


def doctor(repo_root: Path) -> None:
    root = find_repo_root(repo_root)
    config = load_config(root)
    missing = []
    for key in ("todo", "progress", "opinions"):
        path = config.doc_path(key)
        if not path.exists():
            missing.append(path)
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise ConfigError(f"Missing doc files: {names}")
    if not ensure_executable(config.codex_binary):
        raise ConfigError(f"Codex binary not found in PATH: {config.codex_binary}")
