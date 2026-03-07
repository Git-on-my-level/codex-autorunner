from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..core.process_termination import terminate_record


class ServeModeError(RuntimeError):
    category = "serve_mode_error"


class BadReadyUrlError(ServeModeError):
    category = "bad_ready_url"


class ReadinessTimeoutError(ServeModeError):
    category = "readiness_timeout"


class ProcessExitedEarlyError(ServeModeError):
    category = "process_exited_early"


@dataclass(frozen=True)
class BrowserServeConfig:
    serve_cmd: str
    ready_url: Optional[str] = None
    ready_log_pattern: Optional[str] = None
    cwd: Optional[Path] = None
    env_overrides: Dict[str, str] = field(default_factory=dict)
    project_root: Optional[Path] = None
    project_context_enabled: bool = False
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.2
    grace_seconds: float = 0.4
    kill_seconds: float = 0.4


@dataclass(frozen=True)
class BrowserServeSession:
    pid: int
    pgid: Optional[int]
    ready_source: str
    target_url: Optional[str]
    ready_url: Optional[str]


def parse_env_overrides(entries: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries:
        key, sep, value = entry.partition("=")
        if sep != "=" or not key.strip():
            raise ValueError(
                f"Invalid --env value: {entry!r}. Expected format KEY=VALUE."
            )
        parsed[key.strip()] = value
    return parsed


def _project_bin_entries(project_root: Path) -> list[str]:
    venv_bin_dir = "Scripts" if os.name == "nt" else "bin"
    return [
        str(project_root / "node_modules" / ".bin"),
        str(project_root / ".venv" / venv_bin_dir),
        str(project_root / ".codex-autorunner" / "bin"),
        str(project_root / "bin"),
    ]


def _prepend_path_entries(entries: list[str], existing: Optional[str]) -> str:
    merged: list[str] = []
    for entry in [*entries, *((existing or "").split(os.pathsep))]:
        if entry and entry not in merged:
            merged.append(entry)
    return os.pathsep.join(merged)


def _build_launch_env_and_cwd(
    config: BrowserServeConfig,
) -> tuple[dict[str, str], Optional[Path]]:
    env = dict(os.environ)
    resolved_cwd = config.cwd
    if config.project_context_enabled and config.project_root is not None:
        resolved_project_root = config.project_root.expanduser().resolve()
        if resolved_cwd is None:
            resolved_cwd = resolved_project_root
        env["PATH"] = _prepend_path_entries(
            _project_bin_entries(resolved_project_root),
            env.get("PATH"),
        )
    env.update(config.env_overrides)
    return env, resolved_cwd


def _parse_ready_url(url: str) -> tuple[str, str]:
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BadReadyUrlError(
            f"Invalid --ready-url value: {url!r}. Expected absolute http(s) URL."
        )
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return parsed.geturl(), origin


class BrowserServerSupervisor:
    def __init__(self, config: BrowserServeConfig) -> None:
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._pgid: Optional[int] = None
        self._pattern = (
            re.compile(config.ready_log_pattern)
            if config.ready_log_pattern and config.ready_log_pattern.strip()
            else None
        )
        self._ready_event = threading.Event()
        self._detected_url: Optional[str] = None
        self._tail: deque[str] = deque(maxlen=100)
        self._watchers: list[threading.Thread] = []
        self._ready_url: Optional[str] = None
        self._ready_origin: Optional[str] = None
        if config.ready_url:
            self._ready_url, self._ready_origin = _parse_ready_url(config.ready_url)
        if self._ready_url is None and self._pattern is None:
            raise BadReadyUrlError(
                "Serve mode requires --ready-url or --ready-log-pattern."
            )

    @property
    def process_pid(self) -> Optional[int]:
        if self._process is None:
            return None
        return self._process.pid

    @property
    def process_pgid(self) -> Optional[int]:
        return self._pgid

    @property
    def tail(self) -> tuple[str, ...]:
        return tuple(self._tail)

    def start(self) -> None:
        cmd = shlex.split(self._config.serve_cmd or "")
        if not cmd:
            raise ValueError("Serve command is empty.")
        env, resolved_cwd = _build_launch_env_and_cwd(self._config)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(resolved_cwd) if resolved_cwd else None,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "bufsize": 1,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        self._process = subprocess.Popen(cmd, **popen_kwargs)
        if os.name != "nt":
            try:
                self._pgid = os.getpgid(self._process.pid)
            except Exception:
                self._pgid = None
        self._start_watchers()

    def wait_until_ready(self) -> BrowserServeSession:
        if self._process is None:
            raise RuntimeError("Serve process not started.")
        timeout = max(0.1, float(self._config.timeout_seconds))
        poll_interval = max(0.05, float(self._config.poll_interval_seconds))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadinessTimeoutError(
                    "Timed out waiting for serve-mode readiness. "
                    f"Recent output: {self._tail_preview()}"
                )
            if self._ready_url:
                probe_timeout = min(1.0, max(0.05, remaining))
                if self._probe_ready_url(
                    self._ready_url, timeout_seconds=probe_timeout
                ):
                    return BrowserServeSession(
                        pid=self._process.pid,
                        pgid=self._pgid,
                        ready_source="ready_url",
                        target_url=self._ready_origin,
                        ready_url=self._ready_url,
                    )
            elif self._ready_event.is_set():
                return BrowserServeSession(
                    pid=self._process.pid,
                    pgid=self._pgid,
                    ready_source="ready_log_pattern",
                    target_url=self._detected_url,
                    ready_url=None,
                )

            return_code = self._process.poll()
            if return_code is not None:
                raise ProcessExitedEarlyError(
                    f"Serve command exited early with code {return_code}. "
                    f"Recent output: {self._tail_preview()}"
                )
            time.sleep(min(poll_interval, max(0.0, remaining)))

    def stop(self) -> None:
        proc = self._process
        if proc is not None and proc.poll() is None:
            terminate_record(
                proc.pid,
                self._pgid,
                grace_seconds=self._config.grace_seconds,
                kill_seconds=self._config.kill_seconds,
                event_prefix="browser_server",
            )
        if proc is not None:
            try:
                wait_timeout = max(
                    0.1, self._config.grace_seconds + self._config.kill_seconds + 0.5
                )
                proc.wait(timeout=wait_timeout)
            except (subprocess.TimeoutExpired, OSError):
                pass
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except Exception:
                    pass
        for thread in self._watchers:
            thread.join(timeout=1.0)

    def _tail_preview(self) -> str:
        if not self._tail:
            return "<no output>"
        return " | ".join(self._tail)

    def _probe_ready_url(self, ready_url: str, *, timeout_seconds: float) -> bool:
        try:
            response = httpx.get(
                ready_url,
                timeout=timeout_seconds,
                follow_redirects=True,
            )
            return 200 <= response.status_code < 300
        except httpx.HTTPError:
            return False

    def _start_watchers(self) -> None:
        assert self._process is not None
        streams = (
            ("stdout", self._process.stdout),
            ("stderr", self._process.stderr),
        )
        for label, stream in streams:
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._watch_stream,
                args=(label, stream),
                daemon=True,
            )
            thread.start()
            self._watchers.append(thread)

    def _watch_stream(self, label: str, stream: Any) -> None:
        for raw_line in iter(stream.readline, ""):
            line = raw_line.rstrip("\r\n")
            if line:
                self._tail.append(f"{label}: {line}")
            if self._pattern is None:
                continue
            match = self._pattern.search(line)
            if match is None:
                continue
            url_group = match.groupdict().get("url")
            if isinstance(url_group, str) and url_group.strip():
                parsed = urlsplit(url_group.strip())
                if parsed.scheme and parsed.netloc:
                    self._detected_url = urlunsplit(
                        (parsed.scheme, parsed.netloc, "", "", "")
                    )
            self._ready_event.set()


@contextmanager
def supervised_server(config: BrowserServeConfig) -> Iterator[BrowserServeSession]:
    supervisor = BrowserServerSupervisor(config)
    supervisor.start()
    try:
        session = supervisor.wait_until_ready()
        yield session
    finally:
        supervisor.stop()
