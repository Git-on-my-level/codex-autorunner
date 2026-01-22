import dataclasses
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .locks import file_lock
from .sqlite_utils import open_sqlite


@dataclasses.dataclass
class RunnerState:
    last_run_id: Optional[int]
    status: str
    last_exit_code: Optional[int]
    last_run_started_at: Optional[str]
    last_run_finished_at: Optional[str]
    autorunner_agent_override: Optional[str] = None
    autorunner_model_override: Optional[str] = None
    autorunner_effort_override: Optional[str] = None
    autorunner_approval_policy: Optional[str] = None
    autorunner_sandbox_mode: Optional[str] = None
    autorunner_workspace_write_network: Optional[bool] = None
    runner_stop_after_runs: Optional[int] = None
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
            "autorunner_agent_override": self.autorunner_agent_override,
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


def _ensure_state_schema(conn) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runner_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_run_id INTEGER,
                status TEXT NOT NULL,
                last_exit_code INTEGER,
                last_run_started_at TEXT,
                last_run_finished_at TEXT,
                runner_pid INTEGER,
                overrides_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                repo_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                status TEXT NOT NULL,
                agent TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repo_to_session (
                repo_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO runner_state (id, status, updated_at) VALUES (1, ?, ?)",
            ("idle", now_iso()),
        )


def _encode_overrides(state: RunnerState) -> Optional[str]:
    overrides: dict[str, Any] = {}
    if state.autorunner_agent_override is not None:
        overrides["autorunner_agent_override"] = state.autorunner_agent_override
    if state.autorunner_model_override is not None:
        overrides["autorunner_model_override"] = state.autorunner_model_override
    if state.autorunner_effort_override is not None:
        overrides["autorunner_effort_override"] = state.autorunner_effort_override
    if state.autorunner_approval_policy is not None:
        overrides["autorunner_approval_policy"] = state.autorunner_approval_policy
    if state.autorunner_sandbox_mode is not None:
        overrides["autorunner_sandbox_mode"] = state.autorunner_sandbox_mode
    if state.autorunner_workspace_write_network is not None:
        overrides["autorunner_workspace_write_network"] = (
            state.autorunner_workspace_write_network
        )
    if state.runner_stop_after_runs is not None:
        overrides["runner_stop_after_runs"] = state.runner_stop_after_runs
    if not overrides:
        return None
    return json.dumps(overrides, ensure_ascii=True)


def _apply_overrides(state: RunnerState, raw: Optional[str]) -> None:
    if not isinstance(raw, str) or not raw:
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    agent = data.get("autorunner_agent_override")
    if isinstance(agent, str):
        state.autorunner_agent_override = agent
    model = data.get("autorunner_model_override")
    if isinstance(model, str):
        state.autorunner_model_override = model
    effort = data.get("autorunner_effort_override")
    if isinstance(effort, str):
        state.autorunner_effort_override = effort
    approval_policy = data.get("autorunner_approval_policy")
    if isinstance(approval_policy, str):
        state.autorunner_approval_policy = approval_policy
    sandbox_mode = data.get("autorunner_sandbox_mode")
    if isinstance(sandbox_mode, str):
        state.autorunner_sandbox_mode = sandbox_mode
    workspace_write_network = data.get("autorunner_workspace_write_network")
    if isinstance(workspace_write_network, bool):
        state.autorunner_workspace_write_network = workspace_write_network
    runner_stop_after_runs = data.get("runner_stop_after_runs")
    if isinstance(runner_stop_after_runs, int) and not isinstance(
        runner_stop_after_runs, bool
    ):
        state.runner_stop_after_runs = runner_stop_after_runs


def load_state(state_path: Path) -> RunnerState:
    with open_sqlite(state_path) as conn:
        _ensure_state_schema(conn)
        row = conn.execute(
            """
            SELECT last_run_id,
                   status,
                   last_exit_code,
                   last_run_started_at,
                   last_run_finished_at,
                   runner_pid,
                   overrides_json
              FROM runner_state
             WHERE id = 1
            """
        ).fetchone()
        if row is None:
            state = RunnerState(None, "idle", None, None, None)
        else:
            state = RunnerState(
                last_run_id=row["last_run_id"],
                status=row["status"] or "idle",
                last_exit_code=row["last_exit_code"],
                last_run_started_at=row["last_run_started_at"],
                last_run_finished_at=row["last_run_finished_at"],
                runner_pid=row["runner_pid"],
            )
            _apply_overrides(state, row["overrides_json"])
        sessions: dict[str, SessionRecord] = {}
        for record in conn.execute(
            """
            SELECT session_id,
                   repo_path,
                   created_at,
                   last_seen_at,
                   status,
                   agent
              FROM sessions
            """
        ):
            parsed = SessionRecord(
                repo_path=record["repo_path"],
                created_at=record["created_at"],
                last_seen_at=record["last_seen_at"],
                status=record["status"],
                agent=record["agent"],
            )
            sessions[record["session_id"]] = parsed
        repo_to_session: dict[str, str] = {}
        for record in conn.execute("SELECT repo_key, session_id FROM repo_to_session"):
            repo_to_session[record["repo_key"]] = record["session_id"]
        state.sessions = sessions
        state.repo_to_session = repo_to_session
        return state


def save_state(state_path: Path, state: RunnerState) -> None:
    overrides_json = _encode_overrides(state)
    with open_sqlite(state_path) as conn:
        _ensure_state_schema(conn)
        updated_at = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO runner_state (
                    id,
                    last_run_id,
                    status,
                    last_exit_code,
                    last_run_started_at,
                    last_run_finished_at,
                    runner_pid,
                    overrides_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_run_id=excluded.last_run_id,
                    status=excluded.status,
                    last_exit_code=excluded.last_exit_code,
                    last_run_started_at=excluded.last_run_started_at,
                    last_run_finished_at=excluded.last_run_finished_at,
                    runner_pid=excluded.runner_pid,
                    overrides_json=excluded.overrides_json,
                    updated_at=excluded.updated_at
                """,
                (
                    1,
                    state.last_run_id,
                    state.status,
                    state.last_exit_code,
                    state.last_run_started_at,
                    state.last_run_finished_at,
                    state.runner_pid,
                    overrides_json,
                    updated_at,
                ),
            )
            conn.execute("DELETE FROM sessions")
            if state.sessions:
                conn.executemany(
                    """
                    INSERT INTO sessions (
                        session_id,
                        repo_path,
                        created_at,
                        last_seen_at,
                        status,
                        agent
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            session_id,
                            record.repo_path,
                            record.created_at,
                            record.last_seen_at,
                            record.status,
                            record.agent,
                        )
                        for session_id, record in state.sessions.items()
                    ],
                )
            conn.execute("DELETE FROM repo_to_session")
            if state.repo_to_session:
                conn.executemany(
                    """
                    INSERT INTO repo_to_session (repo_key, session_id)
                    VALUES (?, ?)
                    """,
                    list(state.repo_to_session.items()),
                )


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
        with open_sqlite(state_path) as conn:
            _ensure_state_schema(conn)
            with conn:
                conn.execute("DELETE FROM sessions")
                if sessions:
                    conn.executemany(
                        """
                        INSERT INTO sessions (
                            session_id,
                            repo_path,
                            created_at,
                            last_seen_at,
                            status,
                            agent
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                session_id,
                                record.repo_path,
                                record.created_at,
                                record.last_seen_at,
                                record.status,
                                record.agent,
                            )
                            for session_id, record in sessions.items()
                        ],
                    )
                conn.execute("DELETE FROM repo_to_session")
                if repo_to_session:
                    conn.executemany(
                        """
                        INSERT INTO repo_to_session (repo_key, session_id)
                        VALUES (?, ?)
                        """,
                        list(repo_to_session.items()),
                    )
                conn.execute(
                    "UPDATE runner_state SET updated_at=? WHERE id=1",
                    (now_iso(),),
                )
