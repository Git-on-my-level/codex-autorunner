import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


class UsageError(Exception):
    pass


def _default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def _parse_timestamp(value: str) -> datetime:
    try:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception as exc:
        raise UsageError(f"Invalid timestamp in session log: {value}") from exc


@dataclasses.dataclass
class TokenTotals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "TokenTotals") -> None:
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        self.total_tokens += other.total_tokens

    def diff(self, other: "TokenTotals") -> "TokenTotals":
        return TokenTotals(
            input_tokens=self.input_tokens - other.input_tokens,
            cached_input_tokens=self.cached_input_tokens - other.cached_input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens
            - other.reasoning_output_tokens,
            total_tokens=self.total_tokens - other.total_tokens,
        )

    def to_dict(self) -> Dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclasses.dataclass
class TokenEvent:
    timestamp: datetime
    session_path: Path
    cwd: Optional[Path]
    model: Optional[str]
    totals: TokenTotals
    delta: TokenTotals
    rate_limits: Optional[dict]


@dataclasses.dataclass
class UsageSummary:
    totals: TokenTotals
    events: int
    latest_rate_limits: Optional[dict]

    def to_dict(self) -> Dict[str, object]:
        return {
            "events": self.events,
            "totals": self.totals.to_dict(),
            "latest_rate_limits": self.latest_rate_limits,
        }


def _coerce_totals(payload: Optional[dict]) -> TokenTotals:
    payload = payload or {}
    return TokenTotals(
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        cached_input_tokens=int(payload.get("cached_input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        reasoning_output_tokens=int(payload.get("reasoning_output_tokens", 0) or 0),
        total_tokens=int(payload.get("total_tokens", 0) or 0),
    )


def _iter_session_files(codex_home: Path) -> Iterable[Path]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.glob("**/*.jsonl"))


def iter_token_events(
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Iterable[TokenEvent]:
    """
    Yield token usage events from Codex CLI session JSONL logs.
    Events are ordered by file path; per-file ordering matches log order.
    """
    codex_home = (codex_home or _default_codex_home()).expanduser()
    for session_path in _iter_session_files(codex_home):
        session_cwd: Optional[Path] = None
        session_model: Optional[str] = None
        last_totals: Optional[TokenTotals] = None

        try:
            lines = session_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        for line in lines:
            try:
                record = json.loads(line)
            except Exception:
                continue

            rec_type = record.get("type")
            payload = record.get("payload", {}) or {}
            if rec_type == "session_meta":
                cwd_val = payload.get("cwd")
                session_cwd = Path(cwd_val).resolve() if cwd_val else None
                session_model = payload.get("model") or payload.get("model_provider")
                continue

            if rec_type != "event_msg" or payload.get("type") != "token_count":
                continue

            info = payload.get("info") or {}
            total_usage = info.get("total_token_usage")
            last_usage = info.get("last_token_usage")
            if not total_usage and not last_usage:
                # No usable token data; still track rate limits but skip usage.
                last_totals = last_totals
                rate_limits = payload.get("rate_limits")
                ts = record.get("timestamp")
                if ts and rate_limits:
                    timestamp = _parse_timestamp(ts)
                    if since and timestamp < since:
                        continue
                    if until and timestamp > until:
                        continue
                    yield TokenEvent(
                        timestamp=timestamp,
                        session_path=session_path,
                        cwd=session_cwd,
                        model=session_model,
                        totals=last_totals or TokenTotals(),
                        delta=TokenTotals(),
                        rate_limits=rate_limits,
                    )
                continue

            totals = _coerce_totals(total_usage or last_usage)
            delta = (
                _coerce_totals(last_usage)
                if last_usage
                else totals.diff(last_totals or TokenTotals())
            )
            last_totals = totals

            timestamp_raw = record.get("timestamp")
            if not timestamp_raw:
                continue
            timestamp = _parse_timestamp(timestamp_raw)
            if since and timestamp < since:
                continue
            if until and timestamp > until:
                continue

            yield TokenEvent(
                timestamp=timestamp,
                session_path=session_path,
                cwd=session_cwd,
                model=session_model,
                totals=totals,
                delta=delta,
                rate_limits=payload.get("rate_limits"),
            )


def summarize_repo_usage(
    repo_root: Path,
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> UsageSummary:
    repo_root = repo_root.resolve()
    totals = TokenTotals()
    events = 0
    latest_rate_limits: Optional[dict] = None

    for event in iter_token_events(codex_home, since=since, until=until):
        if event.cwd and (event.cwd == repo_root or repo_root in event.cwd.parents):
            totals.add(event.delta)
            events += 1
            if event.rate_limits:
                latest_rate_limits = event.rate_limits
    return UsageSummary(
        totals=totals, events=events, latest_rate_limits=latest_rate_limits
    )


def summarize_hub_usage(
    repo_map: List[Tuple[str, Path]],
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Tuple[Dict[str, UsageSummary], UsageSummary]:
    repo_map = [(repo_id, path.resolve()) for repo_id, path in repo_map]
    per_repo: Dict[str, UsageSummary] = {
        repo_id: UsageSummary(TokenTotals(), 0, None) for repo_id, _ in repo_map
    }
    unmatched = UsageSummary(TokenTotals(), 0, None)

    def _match_repo(cwd: Optional[Path]) -> Optional[str]:
        if not cwd:
            return None
        for repo_id, repo_path in repo_map:
            if cwd == repo_path or repo_path in cwd.parents:
                return repo_id
        return None

    for event in iter_token_events(codex_home, since=since, until=until):
        repo_id = _match_repo(event.cwd)
        if repo_id is None:
            unmatched.totals.add(event.delta)
            unmatched.events += 1
            if event.rate_limits:
                unmatched.latest_rate_limits = event.rate_limits
            continue
        summary = per_repo[repo_id]
        summary.totals.add(event.delta)
        summary.events += 1
        if event.rate_limits:
            summary.latest_rate_limits = event.rate_limits

    return per_repo, unmatched


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception as exc:
        raise UsageError(
            "Use ISO timestamps such as 2025-12-01 or 2025-12-01T12:00Z"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_codex_home() -> Path:
    return _default_codex_home()
