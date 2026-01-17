import dataclasses
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .locks import file_lock
from .utils import atomic_write, read_json


@dataclasses.dataclass
class RunnerState:
    last_run_id: Optional[int]
    status: str
    last_exit_code: Optional[int]
    last_run_started_at: Optional[str]
    last_run_finished_at: Optional[str]
    autorunner_model_override: Optional[str] = None
    autorunner_effort_override: Optional[str] = None
    autorunner_approval_policy: Optional[str] = None
    autorunner_sandbox_mode: Optional[str] = None
    autorunner_workspace_write_network: Optional[bool] = None
    runner_pid: Optional[int] = None
    sessions: dict[str, "SessionRecord"] = dataclasses.field(default_factory=dict)
    repo_to_session: dict[str, str] = dataclasses.field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "last_run_id": self.last_run_id,
            "status": self.status,
            "last_exit_code": self.last_exit_code,
            "last_run_started_at": self.last_run_started_at,
            "last_run_finished_at": self.last_run_finished_at,
            "autorunner_model_override": self.autorunner_model_override,
            "autorunner_effort_override": self.autorunner_effort_override,
            "autorunner_approval_policy": self.autorunner_approval_policy,
            "autorunner_sandbox_mode": self.autorunner_sandbox_mode,
            "autorunner_workspace_write_network": self.autorunner_workspace_write_network,
            "runner_pid": self.runner_pid,
            "sessions": {
                session_id: record.to_dict()
                for session_id, record in self.sessions.items()
            },
            "repo_to_session": dict(self.repo_to_session),
        }
        return json.dumps(payload, indent=2) + "\n"


@dataclasses.dataclass
class SessionRecord:
    repo_path: str
    created_at: str
    last_seen_at: Optional[str]
    status: str
    agent: str = "codex"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["SessionRecord"]:
        repo_path = payload.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path:
            return None
        created_at = payload.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            created_at = now_iso()
        last_seen_at = payload.get("last_seen_at")
        if not isinstance(last_seen_at, str):
            last_seen_at = None
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            status = "active"
        agent = payload.get("agent", "codex")
        if not isinstance(agent, str):
            agent = "codex"
        return cls(
            repo_path=repo_path,
            created_at=created_at,
            last_seen_at=last_seen_at,
            status=status,
            agent=agent,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "status": self.status,
            "agent": self.agent,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(state_path: Path) -> RunnerState:
    data = read_json(state_path)
    if not data:
        return RunnerState(None, "idle", None, None, None)
    sessions: dict[str, SessionRecord] = {}
    sessions_raw = data.get("sessions") if isinstance(data, dict) else None
    if isinstance(sessions_raw, dict):
        for session_id, record in sessions_raw.items():
            if not isinstance(session_id, str) or not isinstance(record, dict):
                continue
            parsed = SessionRecord.from_dict(record)
            if parsed:
                sessions[session_id] = parsed
    repo_to_session_raw = (
        data.get("repo_to_session") if isinstance(data, dict) else None
    )
    repo_to_session: dict[str, str] = {}
    if isinstance(repo_to_session_raw, dict):
        for repo_path, session_id in repo_to_session_raw.items():
            if isinstance(repo_path, str) and isinstance(session_id, str):
                repo_to_session[repo_path] = session_id
    return RunnerState(
        last_run_id=data.get("last_run_id"),
        status=data.get("status", "idle"),
        last_exit_code=data.get("last_exit_code"),
        last_run_started_at=data.get("last_run_started_at"),
        last_run_finished_at=data.get("last_run_finished_at"),
        autorunner_model_override=data.get("autorunner_model_override"),
        autorunner_effort_override=data.get("autorunner_effort_override"),
        autorunner_approval_policy=data.get("autorunner_approval_policy"),
        autorunner_sandbox_mode=data.get("autorunner_sandbox_mode"),
        autorunner_workspace_write_network=data.get(
            "autorunner_workspace_write_network"
        ),
        runner_pid=data.get("runner_pid"),
        sessions=sessions,
        repo_to_session=repo_to_session,
    )


def save_state(state_path: Path, state: RunnerState) -> None:
    atomic_write(state_path, state.to_json())


@contextmanager
def state_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with file_lock(lock_path):
        yield


def persist_session_registry(
    state_path: Path,
    sessions: dict[str, SessionRecord],
    repo_to_session: dict[str, str],
) -> None:
    with state_lock(state_path):
        state = load_state(state_path)
        state.sessions = dict(sessions)
        state.repo_to_session = dict(repo_to_session)
        save_state(state_path, state)
