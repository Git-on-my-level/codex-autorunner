import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import threading
import json
import signal

from .config import Config, ConfigError, load_config
from .docs import DocsManager
from .prompt import build_prompt
from .state import RunnerState, load_state, now_iso, save_state
from .utils import ensure_executable, find_repo_root


class LockError(Exception):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Engine:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.config = load_config(repo_root)
        self.docs = DocsManager(self.config)
        self.state_path = repo_root / ".codex-autorunner" / "state.json"
        self.log_path = repo_root / ".codex-autorunner" / "codex-autorunner.log"
        self.lock_path = repo_root / ".codex-autorunner" / "lock"

    @staticmethod
    def from_cwd(repo: Optional[Path] = None) -> "Engine":
        root = find_repo_root(repo or Path.cwd())
        return Engine(root)

    def acquire_lock(self, force: bool = False) -> None:
        if self.lock_path.exists():
            pid_text = self.lock_path.read_text(encoding="utf-8").strip()
            pid = int(pid_text) if pid_text.isdigit() else None
            if pid and _process_alive(pid):
                if not force:
                    raise LockError(
                        f"Another autorunner is active (pid={pid}); use --force to override"
                    )
            else:
                self.lock_path.unlink(missing_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_lock(self) -> None:
        if self.lock_path.exists():
            self.lock_path.unlink()

    def kill_running_process(self) -> Optional[int]:
        """Force-kill the process holding the lock, if any. Returns pid if killed."""
        if not self.lock_path.exists():
            return None
        pid_text = self.lock_path.read_text(encoding="utf-8").strip()
        pid = int(pid_text) if pid_text.isdigit() else None
        if pid and _process_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                return pid
            except OSError:
                return None
        # stale lock
        self.lock_path.unlink(missing_ok=True)
        return None

    def todos_done(self) -> bool:
        return self.docs.todos_done()

    def extract_prev_output(self, run_id: int) -> Optional[str]:
        if not self.log_path.exists() or run_id <= 0:
            return None
        start = f"=== run {run_id} start ==="
        end = f"=== run {run_id} end"
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
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
        if not self.log_path.exists():
            return None
        start = f"=== run {run_id} start"
        end = f"=== run {run_id} end"
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

    def tail_log(self, tail: int) -> str:
        if not self.log_path.exists():
            return ""
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-tail:])

    def log_line(self, run_id: int, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{timestamp()}] run={run_id} {message}\n"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def run_codex_cli(self, prompt: str, run_id: int) -> int:
        cmd = [self.config.codex_binary] + self.config.codex_args + [prompt]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise ConfigError(f"Codex binary not found: {self.config.codex_binary}")

        if proc.stdout:
            for line in proc.stdout:
                self.log_line(
                    run_id, f"stdout: {line.rstrip()}" if line else "stdout: "
                )
                print(line, end="")

        proc.wait()
        return proc.returncode

    def run_codex_chat(self, prompt: str, run_id: int) -> tuple[int, str]:
        """Run Codex once and return exit code and aggregated output."""
        cmd = [self.config.codex_binary] + self.config.codex_args + [prompt]
        output_lines = []
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise ConfigError(f"Codex binary not found: {self.config.codex_binary}")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"=== run {run_id} chat start ===\n")

        if proc.stdout:
            for line in proc.stdout:
                clean = line.rstrip("\n")
                output_lines.append(clean)
                self.log_line(run_id, f"chat: {clean}")

        proc.wait()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"=== run {run_id} chat end (code {proc.returncode}) ===\n")
        return proc.returncode, "\n".join(output_lines)

    def maybe_git_commit(self, run_id: int) -> None:
        msg = self.config.git_commit_message_template.replace(
            "{run_id}", str(run_id)
        ).replace("#{run_id}", str(run_id))
        paths = [
            self.config.doc_path("todo"),
            self.config.doc_path("progress"),
            self.config.doc_path("opinions"),
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
        start_wallclock = time.time()
        target_runs = (
            stop_after_runs
            if stop_after_runs is not None
            else self.config.runner_stop_after_runs
        )

        while True:
            if external_stop_flag and external_stop_flag.is_set():
                self._update_state(
                    "idle", run_id - 1, state.last_exit_code, finished=True
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
                self._update_state(
                    "idle", run_id - 1, state.last_exit_code, finished=True
                )
                break

            prev_output = self.extract_prev_output(run_id - 1)
            prompt = build_prompt(self.config, self.docs, prev_output)

            self._update_state("running", run_id, None, started=True)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"=== run {run_id} start ===\n")

            exit_code = self.run_codex_cli(prompt, run_id)

            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"=== run {run_id} end (code {exit_code}) ===\n")

            self._update_state(
                "error" if exit_code != 0 else "idle", run_id, exit_code, finished=True
            )

            if self.config.git_auto_commit and exit_code == 0:
                self.maybe_git_commit(run_id)

            if exit_code != 0:
                break

            if target_runs is not None and run_id >= target_runs:
                break

            run_id += 1
            if external_stop_flag and external_stop_flag.is_set():
                self._update_state("idle", run_id - 1, exit_code, finished=True)
                break
            time.sleep(self.config.runner_sleep_seconds)

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
        )
        save_state(self.state_path, new_state)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def clear_stale_lock(lock_path: Path) -> None:
    if lock_path.exists():
        pid_text = lock_path.read_text(encoding="utf-8").strip()
        pid = int(pid_text) if pid_text.isdigit() else None
        if not pid or not _process_alive(pid):
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
