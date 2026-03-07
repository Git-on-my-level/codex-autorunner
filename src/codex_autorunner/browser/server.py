from __future__ import annotations

import json
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


class RenderSessionError(ServeModeError):
    category = "session_attach_error"


class MissingRenderSessionError(RenderSessionError):
    category = "session_missing"


class StaleRenderSessionError(RenderSessionError):
    category = "session_stale"


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


@dataclass(frozen=True)
class BrowserServeSessionMetadata:
    session_id: str
    pid: int
    pgid: Optional[int]
    target_url: str
    ready_source: str
    ready_url: Optional[str]
    serve_cmd: Optional[str]
    cwd: Optional[str]
    created_at_epoch_ms: int


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


def render_session_registry_dir(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "render_sessions"


def persist_render_session(
    *,
    repo_root: Path,
    session_id: str,
    config: BrowserServeConfig,
    session: BrowserServeSession,
) -> Path:
    _validate_session_id(session_id)
    if not session.target_url:
        raise RenderSessionError("Cannot persist render session without a target URL.")

    registry_dir = render_session_registry_dir(repo_root)
    registry_dir.mkdir(parents=True, exist_ok=True)
    record_path = _session_record_path(repo_root=repo_root, session_id=session_id)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "pid": session.pid,
        "pgid": session.pgid,
        "target_url": session.target_url,
        "ready_source": session.ready_source,
        "ready_url": session.ready_url,
        "serve_cmd": config.serve_cmd,
        "cwd": str(config.cwd) if config.cwd is not None else None,
        "created_at_epoch_ms": int(time.time() * 1000),
    }
    record_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record_path


def load_render_session(
    *,
    repo_root: Path,
    session_id: str,
    require_live: bool = True,
    probe_timeout_seconds: float = 1.0,
) -> BrowserServeSessionMetadata:
    _validate_session_id(session_id)
    record_path = _session_record_path(repo_root=repo_root, session_id=session_id)
    if not record_path.exists():
        raise MissingRenderSessionError(
            f"Render session '{session_id}' was not found at {record_path}."
        )

    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as exc:
        remove_render_session(repo_root=repo_root, session_id=session_id)
        raise StaleRenderSessionError(
            (
                f"Render session '{session_id}' metadata is unreadable and was removed: "
                f"{exc}"
            )
        ) from exc

    try:
        metadata = _decode_render_session_payload(
            payload=payload,
            session_id=session_id,
            record_path=record_path,
        )
    except RenderSessionError as exc:
        remove_render_session(repo_root=repo_root, session_id=session_id)
        raise StaleRenderSessionError(
            f"Render session '{session_id}' metadata is invalid and was removed: {exc}"
        ) from exc

    if not require_live:
        return metadata

    if not _is_pid_alive(metadata.pid):
        remove_render_session(repo_root=repo_root, session_id=session_id)
        raise StaleRenderSessionError(
            f"Render session '{session_id}' is stale: process {metadata.pid} is not running."
        )

    if metadata.ready_url and not _probe_url_health(
        metadata.ready_url, timeout_seconds=probe_timeout_seconds
    ):
        remove_render_session(repo_root=repo_root, session_id=session_id)
        raise StaleRenderSessionError(
            f"Render session '{session_id}' is stale: readiness URL "
            f"{metadata.ready_url} is unreachable."
        )
    return metadata


def remove_render_session(*, repo_root: Path, session_id: str) -> None:
    _validate_session_id(session_id)
    record_path = _session_record_path(repo_root=repo_root, session_id=session_id)
    record_path.unlink(missing_ok=True)


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


def _session_record_path(*, repo_root: Path, session_id: str) -> Path:
    return render_session_registry_dir(repo_root) / f"{session_id}.json"


def _validate_session_id(session_id: str) -> None:
    cleaned = (session_id or "").strip()
    if not cleaned:
        raise ValueError("Session id must be non-empty.")
    if re.search(r"[^A-Za-z0-9_.-]", cleaned):
        raise ValueError(
            "Session id contains unsupported characters. Use letters, numbers, dot, underscore, or hyphen."
        )


def _decode_render_session_payload(
    *,
    payload: Any,
    session_id: str,
    record_path: Path,
) -> BrowserServeSessionMetadata:
    if not isinstance(payload, dict):
        raise RenderSessionError(
            f"Render session '{session_id}' metadata is invalid at {record_path}."
        )
    target_url = payload.get("target_url")
    pid = payload.get("pid")
    ready_source = payload.get("ready_source")
    if not isinstance(target_url, str) or not target_url.strip():
        raise RenderSessionError(
            f"Render session '{session_id}' metadata is missing target_url."
        )
    if not isinstance(pid, int) or pid <= 0:
        raise RenderSessionError(
            f"Render session '{session_id}' metadata is missing a valid pid."
        )
    if not isinstance(ready_source, str) or not ready_source.strip():
        raise RenderSessionError(
            f"Render session '{session_id}' metadata is missing ready_source."
        )
    pgid_raw = payload.get("pgid")
    pgid = pgid_raw if isinstance(pgid_raw, int) else None
    ready_url_raw = payload.get("ready_url")
    ready_url = (
        ready_url_raw.strip()
        if isinstance(ready_url_raw, str) and ready_url_raw.strip()
        else None
    )
    serve_cmd_raw = payload.get("serve_cmd")
    serve_cmd = serve_cmd_raw if isinstance(serve_cmd_raw, str) else None
    cwd_raw = payload.get("cwd")
    cwd = cwd_raw if isinstance(cwd_raw, str) else None
    created_raw = payload.get("created_at_epoch_ms")
    created = created_raw if isinstance(created_raw, int) else 0
    return BrowserServeSessionMetadata(
        session_id=session_id,
        pid=pid,
        pgid=pgid,
        target_url=target_url.strip(),
        ready_source=ready_source.strip(),
        ready_url=ready_url,
        serve_cmd=serve_cmd,
        cwd=cwd,
        created_at_epoch_ms=created,
    )


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _probe_url_health(url: str, *, timeout_seconds: float) -> bool:
    try:
        response = httpx.get(
            url,
            timeout=max(0.1, float(timeout_seconds)),
            follow_redirects=True,
        )
        return 200 <= response.status_code < 300
    except httpx.HTTPError:
        return False


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
