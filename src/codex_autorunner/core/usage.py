import copy
import dataclasses
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

logger = logging.getLogger("codex_autorunner.core.usage")


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
    rate_limits: Optional[Dict[str, Any]]
    agent: str


@dataclasses.dataclass
class UsageSummary:
    totals: TokenTotals
    events: int
    latest_rate_limits: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, object]:
        return {
            "events": self.events,
            "totals": self.totals.to_dict(),
            "latest_rate_limits": self.latest_rate_limits,
        }


def _coerce_totals(payload: Optional[Dict[str, Any]]) -> TokenTotals:
    payload = payload or {}
    return TokenTotals(
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        cached_input_tokens=int(payload.get("cached_input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        reasoning_output_tokens=int(payload.get("reasoning_output_tokens", 0) or 0),
        total_tokens=int(payload.get("total_tokens", 0) or 0),
    )


CODEX_AGENT_ID = "codex"
OPENCODE_AGENT_ID = "opencode"

_OPENCODE_USAGE_KEYS = {
    "input_tokens": [
        "prompt_tokens",
        "promptTokens",
        "input_tokens",
        "inputTokens",
    ],
    "cached_input_tokens": [
        "cached_input_tokens",
        "cachedInputTokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cachedTokens",
    ],
    "output_tokens": [
        "completion_tokens",
        "completionTokens",
        "output_tokens",
        "outputTokens",
    ],
    "reasoning_output_tokens": [
        "reasoning_tokens",
        "reasoningTokens",
        "reasoning_output_tokens",
        "reasoningOutputTokens",
    ],
    "total_tokens": [
        "total_tokens",
        "totalTokens",
        "total",
    ],
}


def _coerce_opencode_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        logger.debug("Failed to coerce int from %r: %s", value, exc)
        return 0


def _coerce_opencode_field(payload: Dict[str, Any], keys: List[str]) -> int:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return _coerce_opencode_int(payload.get(key))
    return 0


def _coerce_opencode_totals(payload: Optional[Dict[str, Any]]) -> TokenTotals:
    payload = payload or {}
    input_tokens = _coerce_opencode_field(payload, _OPENCODE_USAGE_KEYS["input_tokens"])
    cached_tokens = _coerce_opencode_field(
        payload, _OPENCODE_USAGE_KEYS["cached_input_tokens"]
    )
    output_tokens = _coerce_opencode_field(
        payload, _OPENCODE_USAGE_KEYS["output_tokens"]
    )
    reasoning_tokens = _coerce_opencode_field(
        payload, _OPENCODE_USAGE_KEYS["reasoning_output_tokens"]
    )
    total_tokens = _coerce_opencode_field(payload, _OPENCODE_USAGE_KEYS["total_tokens"])
    if not total_tokens:
        total_tokens = input_tokens + cached_tokens + output_tokens + reasoning_tokens
    return TokenTotals(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


def _looks_like_opencode_usage(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for keys in _OPENCODE_USAGE_KEYS.values():
        for key in keys:
            if key in payload:
                return True
    return False


def _extract_opencode_usage_payload(
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    for key in (
        "usage",
        "token_usage",
        "tokenUsage",
        "usage_stats",
        "usageStats",
        "stats",
    ):
        usage = payload.get(key)
        if _looks_like_opencode_usage(usage):
            return cast(Dict[str, Any], usage)
    response = payload.get("response")
    if isinstance(response, dict):
        usage = response.get("usage")
        if _looks_like_opencode_usage(usage):
            return cast(Dict[str, Any], usage)
    return None


def _extract_opencode_entries(
    payload: Dict[str, Any],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    entries: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    detail_found = False
    for list_key in ("messages", "events", "turns", "responses", "steps"):
        items = payload.get(list_key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            usage = _extract_opencode_usage_payload(item)
            if usage:
                entries.append((item, usage))
                detail_found = True
    if detail_found:
        return entries
    usage = _extract_opencode_usage_payload(payload)
    if usage:
        entries.append((payload, usage))
    return entries


def _parse_opencode_timestamp(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return _parse_timestamp(value)
        except UsageError:
            pass
        if value.isdigit():
            return _parse_opencode_timestamp(int(value), fallback)
    return fallback


def _extract_opencode_timestamp(
    container: Dict[str, Any], fallback: datetime
) -> datetime:
    for key in (
        "timestamp",
        "created_at",
        "createdAt",
        "time",
        "started_at",
        "completed_at",
        "ts",
    ):
        if key in container:
            return _parse_opencode_timestamp(container.get(key), fallback)
    return fallback


def _format_opencode_model(
    model: Optional[str], provider: Optional[str]
) -> Optional[str]:
    if model and provider and provider not in model:
        return f"{provider}:{model}"
    return model or provider


def _extract_opencode_model(
    container: Dict[str, Any],
    fallback_model: Optional[str],
    fallback_provider: Optional[str],
) -> Optional[str]:
    model = (
        container.get("model")
        or container.get("model_name")
        or container.get("modelName")
        or fallback_model
    )
    provider = (
        container.get("provider")
        or container.get("model_provider")
        or container.get("modelProvider")
        or fallback_provider
    )
    return _format_opencode_model(model, provider)


def _iter_opencode_session_files(repo_root: Path) -> Iterable[Path]:
    sessions_dir = repo_root / ".opencode" / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.glob("**/*.json"))


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
            with open(session_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        logger.debug(
                            "Failed to parse JSON line in %s: %s", session_path, exc
                        )
                        continue

                    rec_type = record.get("type")
                    payload = record.get("payload", {}) or {}
                    if rec_type == "session_meta":
                        cwd_val = payload.get("cwd")
                        session_cwd = Path(cwd_val).resolve() if cwd_val else None
                        session_model = payload.get("model") or payload.get(
                            "model_provider"
                        )
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
                                agent=CODEX_AGENT_ID,
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
                        agent=CODEX_AGENT_ID,
                    )
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("Failed to process session file %s: %s", session_path, exc)


def iter_opencode_events(
    repo_roots: Iterable[Path],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Iterable[TokenEvent]:
    """
    Yield token usage events from OpenCode session JSON files in repos.
    Events are ordered by repo root and file path; per-file ordering matches entry order.
    """
    for repo_root in sorted({path.resolve() for path in repo_roots}):
        for session_path in _iter_opencode_session_files(repo_root):
            try:
                with open(session_path, "r", encoding="utf-8") as f:
                    payload = json.loads(f.read())
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("Failed to read session file %s: %s", session_path, exc)
                continue

            try:
                mtime = datetime.fromtimestamp(
                    session_path.stat().st_mtime, tz=timezone.utc
                )
            except OSError as exc:
                logger.debug("Failed to get mtime for %s: %s", session_path, exc)
                mtime = datetime.now(timezone.utc)

            top_model = payload.get("model") if isinstance(payload, dict) else None
            top_provider = (
                payload.get("provider") if isinstance(payload, dict) else None
            )
            entries = (
                _extract_opencode_entries(payload) if isinstance(payload, dict) else []
            )
            if not entries:
                continue

            totals = TokenTotals()
            for container, usage in entries:
                delta = _coerce_opencode_totals(usage)
                if not any(
                    (
                        delta.input_tokens,
                        delta.cached_input_tokens,
                        delta.output_tokens,
                        delta.reasoning_output_tokens,
                        delta.total_tokens,
                    )
                ):
                    continue
                totals.add(delta)
                timestamp = _extract_opencode_timestamp(container, mtime)
                if since and timestamp < since:
                    continue
                if until and timestamp > until:
                    continue
                model = _extract_opencode_model(container, top_model, top_provider)
                yield TokenEvent(
                    timestamp=timestamp,
                    session_path=session_path,
                    cwd=repo_root,
                    model=model,
                    totals=copy.deepcopy(totals),
                    delta=delta,
                    rate_limits=None,
                    agent=OPENCODE_AGENT_ID,
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
    for event in iter_opencode_events([repo_root], since=since, until=until):
        totals.add(event.delta)
        events += 1
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

    for event in iter_opencode_events(
        [path for _, path in repo_map], since=since, until=until
    ):
        repo_id = _match_repo(event.cwd)
        if repo_id is None:
            continue
        summary = per_repo[repo_id]
        summary.totals.add(event.delta)
        summary.events += 1

    return per_repo, unmatched


def summarize_opencode_repo_usage(
    repo_root: Path,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> UsageSummary:
    totals = TokenTotals()
    events = 0
    for event in iter_opencode_events([repo_root], since=since, until=until):
        totals.add(event.delta)
        events += 1
    return UsageSummary(totals=totals, events=events, latest_rate_limits=None)


def summarize_opencode_hub_usage(
    repo_map: List[Tuple[str, Path]],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, UsageSummary]:
    repo_map = [(repo_id, path.resolve()) for repo_id, path in repo_map]
    per_repo: Dict[str, UsageSummary] = {
        repo_id: UsageSummary(TokenTotals(), 0, None) for repo_id, _ in repo_map
    }

    def _match_repo(cwd: Optional[Path]) -> Optional[str]:
        if not cwd:
            return None
        for repo_id, repo_path in repo_map:
            if cwd == repo_path or repo_path in cwd.parents:
                return repo_id
        return None

    for event in iter_opencode_events(
        [path for _, path in repo_map], since=since, until=until
    ):
        repo_id = _match_repo(event.cwd)
        if repo_id is None:
            continue
        summary = per_repo[repo_id]
        summary.totals.add(event.delta)
        summary.events += 1

    return per_repo


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


def _bucket_start(dt: datetime, bucket: str) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        start = dt - timedelta(days=dt.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    raise UsageError(f"Unsupported bucket: {bucket}")


def _bucket_label(dt: datetime, bucket: str) -> str:
    if bucket == "hour":
        return dt.strftime("%Y-%m-%dT%H:00Z")
    return dt.date().isoformat()


def _iter_buckets(start: datetime, end: datetime, bucket: str) -> List[datetime]:
    if end < start:
        return []
    step = timedelta(hours=1)
    if bucket == "day":
        step = timedelta(days=1)
    elif bucket == "week":
        step = timedelta(days=7)
    buckets: List[datetime] = []
    cursor = start
    while cursor <= end:
        buckets.append(cursor)
        cursor += step
    return buckets


def _default_usage_series_cache_path(codex_home: Path) -> Path:
    return codex_home / "usage_series_cache.json"


def _parse_bucket_label(value: str, bucket: str) -> Optional[datetime]:
    try:
        if bucket == "hour":
            dt = datetime.strptime(value, "%Y-%m-%dT%H:00Z")
            return dt.replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError) as exc:
        logger.debug("Failed to parse timestamp %r: %s", value, exc)
        return None


def _empty_rollup_bucket() -> Dict[str, Any]:
    return {
        "total": 0,
        "models": {},
        "token_types": {},
        "model_token": {},
    }


def _empty_summary_entry() -> Dict[str, Any]:
    return {
        "events": 0,
        "totals": TokenTotals().to_dict(),
        "latest_rate_limits": None,
        "latest_rate_limits_pos": None,
    }


def _rate_limits_pos_key(pos: Optional[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    if not pos:
        return None
    file_val = str(pos.get("file") or "")
    try:
        index_val = int(pos.get("index", 0) or 0)
    except (TypeError, ValueError) as exc:
        logger.debug("Failed to parse rate limits index: %s", exc)
        index_val = 0
    return (file_val, index_val)


def _is_rate_limits_newer(
    candidate: Optional[Dict[str, Any]],
    current: Optional[Dict[str, Any]],
) -> bool:
    cand_key = _rate_limits_pos_key(candidate)
    if cand_key is None:
        return False
    curr_key = _rate_limits_pos_key(current)
    if curr_key is None:
        return True
    if cand_key[0] == curr_key[0]:
        return cand_key[1] >= curr_key[1]
    return cand_key[0] > curr_key[0]


@dataclasses.dataclass
class _SummaryAccumulator:
    totals: TokenTotals = dataclasses.field(default_factory=TokenTotals)
    events: int = 0
    latest_rate_limits: Optional[Dict[str, Any]] = None
    latest_rate_limits_pos: Optional[Dict[str, Any]] = None

    def add_entry(self, entry: Dict[str, Any]) -> None:
        self.totals.add(_coerce_totals(entry.get("totals")))
        self.events += int(entry.get("events", 0) or 0)
        pos = entry.get("latest_rate_limits_pos")
        if pos and _is_rate_limits_newer(pos, self.latest_rate_limits_pos):
            self.latest_rate_limits = entry.get("latest_rate_limits")
            self.latest_rate_limits_pos = pos


class UsageSeriesCache:
    def __init__(self, codex_home: Path, cache_path: Path):
        self.codex_home = codex_home
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._updating = False
        self._cache: Optional[Dict[str, Any]] = None

    def _load_cache(self) -> Dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self.cache_path.exists():
            self._cache = {
                "version": 3,
                "files": {},
                "file_rollups": {},
                "file_summaries": {},
                "rollups": {"by_cwd": {}},
                "summary": {"by_cwd": {}},
            }
            return self._cache
        try:
            payload = cast(
                Dict[str, Any], json.loads(self.cache_path.read_text(encoding="utf-8"))
            )
            if payload.get("version") != 3:
                raise ValueError("Unsupported cache version")
            payload.setdefault("files", {})
            payload.setdefault("file_rollups", {})
            payload.setdefault("file_summaries", {})
            payload.setdefault("rollups", {}).setdefault("by_cwd", {})
            payload.setdefault("summary", {}).setdefault("by_cwd", {})
            self._cache = payload
            return payload
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.debug("Failed to load usage cache: %s", exc)
            self._cache = {
                "version": 3,
                "files": {},
                "file_rollups": {},
                "file_summaries": {},
                "rollups": {"by_cwd": {}},
                "summary": {"by_cwd": {}},
            }
            return self._cache

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cache_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(self.cache_path)

    def _needs_update(self, payload: Dict[str, Any]) -> bool:
        files = cast(Dict[str, Any], payload.get("files", {}))
        existing_paths = {str(path) for path in _iter_session_files(self.codex_home)}
        for path_key in list(files.keys()):
            if path_key not in existing_paths:
                return True
        for session_path in _iter_session_files(self.codex_home):
            path_key = str(session_path)
            file_state = files.get(path_key)
            try:
                size = session_path.stat().st_size
            except OSError as exc:
                logger.debug("Failed to stat session file %s: %s", session_path, exc)
                continue
            if not file_state:
                return True
            offset = int(file_state.get("offset", 0) or 0)
            if size != offset:
                return True
        return False

    def _start_update(self, payload: Dict[str, Any]) -> None:
        if self._updating:
            return
        cache_snapshot = copy.deepcopy(payload)
        self._updating = True
        thread = threading.Thread(
            target=self._update_cache, args=(cache_snapshot,), daemon=True
        )
        thread.start()

    def request_update(self) -> str:
        with self._lock:
            payload = self._load_cache()
            needs_update = self._needs_update(payload)
            if needs_update:
                self._start_update(payload)
                return "loading"
            return "loading" if self._updating else "ready"

    def get_repo_series(
        self,
        repo_root: Path,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        bucket: str = "day",
        segment: str = "none",
    ) -> Tuple[Dict[str, object], str]:
        status = self.request_update()
        with self._lock:
            payload = self._load_cache()
            series = self._build_repo_series(
                payload,
                repo_root,
                since=since,
                until=until,
                bucket=bucket,
                segment=segment,
            )
        return series, status

    def get_hub_series(
        self,
        repo_map: List[Tuple[str, Path]],
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        bucket: str = "day",
        segment: str = "none",
    ) -> Tuple[Dict[str, object], str]:
        status = self.request_update()
        with self._lock:
            payload = self._load_cache()
            series = self._build_hub_series(
                payload,
                repo_map,
                since=since,
                until=until,
                bucket=bucket,
                segment=segment,
            )
        return series, status

    def get_repo_summary(
        self,
        repo_root: Path,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> Tuple[UsageSummary, str]:
        status = self.request_update()
        with self._lock:
            payload = self._load_cache()
            summary = self._build_repo_summary(
                payload, repo_root, since=since, until=until
            )
        return summary, status

    def get_hub_summary(
        self,
        repo_map: List[Tuple[str, Path]],
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> Tuple[Dict[str, UsageSummary], UsageSummary, str]:
        status = self.request_update()
        with self._lock:
            payload = self._load_cache()
            per_repo, unmatched = self._build_hub_summary(
                payload, repo_map, since=since, until=until
            )
        return per_repo, unmatched, status

    def _update_cache(self, payload: Dict[str, Any]) -> None:
        try:
            files = cast(Dict[str, Any], payload.setdefault("files", {}))
            file_rollups = cast(Dict[str, Any], payload.setdefault("file_rollups", {}))
            file_summaries = cast(
                Dict[str, Any], payload.setdefault("file_summaries", {})
            )
            rollups = cast(
                Dict[str, Any],
                payload.setdefault("rollups", {}).setdefault("by_cwd", {}),
            )
            summary_rollups = cast(
                Dict[str, Any],
                payload.setdefault("summary", {}).setdefault("by_cwd", {}),
            )
            rebuild_rollups = False
            rebuild_summary = False
            existing_paths = {
                str(path) for path in _iter_session_files(self.codex_home)
            }
            for path_key in list(files.keys()):
                if path_key not in existing_paths:
                    files.pop(path_key, None)
                    file_rollups.pop(path_key, None)
                    file_summaries.pop(path_key, None)
                    rebuild_rollups = True
                    rebuild_summary = True

            for session_path in _iter_session_files(self.codex_home):
                path_key = str(session_path)
                file_state = files.get(path_key, {})
                offset = int(file_state.get("offset", 0) or 0)
                try:
                    size = session_path.stat().st_size
                except OSError as exc:
                    logger.debug(
                        "Failed to stat session file %s: %s", session_path, exc
                    )
                    continue
                if size < offset:
                    offset = 0
                    file_state = {}
                    file_rollups.pop(path_key, None)
                    file_summaries.pop(path_key, None)
                    rebuild_rollups = True
                    rebuild_summary = True
                if size == offset:
                    continue
                updated_state = self._ingest_session_file(
                    session_path,
                    offset,
                    file_state,
                    rollups,
                    file_rollups,
                    summary_rollups,
                    file_summaries,
                )
                files[path_key] = updated_state
            if rebuild_rollups:
                payload["rollups"]["by_cwd"] = self._rebuild_rollups(file_rollups)
            if rebuild_summary:
                payload["summary"]["by_cwd"] = self._rebuild_summary(file_summaries)
            payload["version"] = 3
            self._save_cache(payload)
            with self._lock:
                self._cache = payload
        finally:
            with self._lock:
                self._updating = False

    def _ingest_session_file(
        self,
        session_path: Path,
        offset: int,
        state: Dict[str, Any],
        rollups: Dict[str, Any],
        file_rollups: Dict[str, Any],
        summary_rollups: Dict[str, Any],
        file_summaries: Dict[str, Any],
    ) -> Dict[str, Any]:
        cwd = state.get("cwd")
        model = state.get("model")
        last_totals_raw = state.get("last_totals")
        last_totals = _coerce_totals(last_totals_raw) if last_totals_raw else None
        event_index = int(state.get("event_index", 0) or 0)

        try:
            with session_path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
                new_offset = handle.tell()
        except OSError as exc:
            logger.debug(
                "Failed to read session file %s at offset %d: %s",
                session_path,
                offset,
                exc,
            )
            return state

        if not data:
            state["offset"] = offset
            return state

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.debug(
                "Failed to decode session file %s as UTF-8: %s", session_path, exc
            )
            text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()

        token_fields = [
            ("input", "input_tokens"),
            ("cached", "cached_input_tokens"),
            ("output", "output_tokens"),
            ("reasoning", "reasoning_output_tokens"),
        ]

        path_key = str(session_path)
        file_entry = file_rollups.setdefault(path_key, {}).setdefault("by_cwd", {})
        file_summary_entry = file_summaries.setdefault(path_key, {}).setdefault(
            "by_cwd", {}
        )

        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug("Failed to parse JSON line in %s: %s", session_path, exc)
                continue

            rec_type = record.get("type")
            payload = record.get("payload", {}) or {}
            if rec_type == "session_meta":
                cwd_val = payload.get("cwd")
                cwd = str(Path(cwd_val).resolve()) if cwd_val else cwd
                model = payload.get("model") or payload.get("model_provider") or model
                continue

            if rec_type != "event_msg" or payload.get("type") != "token_count":
                continue

            info = payload.get("info") or {}
            total_usage = info.get("total_token_usage")
            last_usage = info.get("last_token_usage")
            rate_limits = payload.get("rate_limits")
            if not total_usage and not last_usage and not rate_limits:
                continue

            timestamp_raw = record.get("timestamp")
            if not timestamp_raw:
                continue
            try:
                timestamp = _parse_timestamp(timestamp_raw)
            except UsageError:
                continue

            cwd_key = cwd or "__unknown__"
            model_key = model or "unknown"

            if total_usage or last_usage:
                totals = _coerce_totals(total_usage or last_usage)
                delta = (
                    _coerce_totals(last_usage)
                    if last_usage
                    else totals.diff(last_totals or TokenTotals())
                )
                last_totals = totals
                for bucket_name in ("hour", "day", "week"):
                    bucket_start = _bucket_start(timestamp, bucket_name)
                    bucket_label = _bucket_label(bucket_start, bucket_name)
                    self._apply_rollup_delta(
                        rollups,
                        cwd_key,
                        bucket_name,
                        bucket_label,
                        model_key,
                        delta,
                        token_fields,
                    )
                    self._apply_rollup_delta(
                        file_entry,
                        cwd_key,
                        bucket_name,
                        bucket_label,
                        model_key,
                        delta,
                        token_fields,
                    )
            else:
                delta = TokenTotals()

            event_index += 1
            pos = {"file": path_key, "index": event_index}
            self._apply_summary_delta(
                summary_rollups,
                file_summary_entry,
                cwd_key,
                delta,
                rate_limits,
                pos,
            )

        state["offset"] = new_offset
        state["cwd"] = cwd
        state["model"] = model
        state["last_totals"] = last_totals.to_dict() if last_totals else None
        state["event_index"] = event_index
        return state

    def _apply_rollup_delta(
        self,
        rollups: Dict[str, Any],
        cwd_key: str,
        bucket_name: str,
        bucket_label: str,
        model_key: str,
        delta: TokenTotals,
        token_fields: List[Tuple[str, str]],
    ) -> None:
        cwd_rollups = rollups.setdefault(cwd_key, {})
        bucket_rollups = cwd_rollups.setdefault(bucket_name, {})
        entry = bucket_rollups.get(bucket_label)
        if entry is None:
            entry = _empty_rollup_bucket()
            bucket_rollups[bucket_label] = entry

        entry["total"] = int(entry.get("total", 0)) + delta.total_tokens

        models = entry.setdefault("models", {})
        models[model_key] = int(models.get(model_key, 0)) + delta.total_tokens

        token_types = entry.setdefault("token_types", {})
        model_token = entry.setdefault("model_token", {}).setdefault(model_key, {})
        for label, field in token_fields:
            value = getattr(delta, field)
            if not value:
                continue
            token_types[label] = int(token_types.get(label, 0)) + value
            model_token[label] = int(model_token.get(label, 0)) + value

    def _apply_summary_delta(
        self,
        summary_rollups: Dict[str, Any],
        file_summary_entry: Dict[str, Any],
        cwd_key: str,
        delta: TokenTotals,
        rate_limits: Optional[Dict[str, Any]],
        pos: Dict[str, Any],
    ) -> None:
        summary_entry = summary_rollups.setdefault(cwd_key, _empty_summary_entry())
        file_entry = file_summary_entry.setdefault(cwd_key, _empty_summary_entry())

        for entry in (summary_entry, file_entry):
            totals = _coerce_totals(entry.get("totals"))
            totals.add(delta)
            entry["totals"] = totals.to_dict()
            entry["events"] = int(entry.get("events", 0) or 0) + 1
            if rate_limits is not None and _is_rate_limits_newer(
                pos, entry.get("latest_rate_limits_pos")
            ):
                entry["latest_rate_limits"] = rate_limits
                entry["latest_rate_limits_pos"] = pos

    def _rebuild_rollups(self, file_rollups: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for file_entry in file_rollups.values():
            cwd_rollups = file_entry.get("by_cwd", {})
            for cwd_key, buckets in cwd_rollups.items():
                target = merged.setdefault(cwd_key, {})
                for bucket_name, bucket_map in buckets.items():
                    target_bucket = target.setdefault(bucket_name, {})
                    for bucket_label, entry in (bucket_map or {}).items():
                        merged_entry = target_bucket.get(bucket_label)
                        if merged_entry is None:
                            merged_entry = _empty_rollup_bucket()
                            target_bucket[bucket_label] = merged_entry
                        merged_entry["total"] = int(merged_entry.get("total", 0)) + int(
                            entry.get("total", 0)
                        )
                        for model_key, total in (entry.get("models") or {}).items():
                            models = merged_entry.setdefault("models", {})
                            models[model_key] = int(models.get(model_key, 0)) + int(
                                total
                            )
                        for token_key, total in (
                            entry.get("token_types") or {}
                        ).items():
                            token_types = merged_entry.setdefault("token_types", {})
                            token_types[token_key] = int(
                                token_types.get(token_key, 0)
                            ) + int(total)
                        for model_key, token_map in (
                            entry.get("model_token") or {}
                        ).items():
                            model_token = merged_entry.setdefault(
                                "model_token", {}
                            ).setdefault(model_key, {})
                            for token_key, total in (token_map or {}).items():
                                model_token[token_key] = int(
                                    model_token.get(token_key, 0)
                                ) + int(total)
        return merged

    def _rebuild_summary(self, file_summaries: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for path_key in sorted(file_summaries.keys()):
            file_entry = file_summaries.get(path_key, {})
            cwd_map = file_entry.get("by_cwd", {})
            for cwd_key, entry in (cwd_map or {}).items():
                target = merged.setdefault(cwd_key, _empty_summary_entry())
                target_totals = _coerce_totals(target.get("totals"))
                target_totals.add(_coerce_totals(entry.get("totals")))
                target["totals"] = target_totals.to_dict()
                target["events"] = int(target.get("events", 0) or 0) + int(
                    entry.get("events", 0) or 0
                )
                pos = entry.get("latest_rate_limits_pos")
                if pos and _is_rate_limits_newer(
                    pos, target.get("latest_rate_limits_pos")
                ):
                    target["latest_rate_limits"] = entry.get("latest_rate_limits")
                    target["latest_rate_limits_pos"] = pos
        return merged

    def _buckets_for_range(
        self,
        bucket_rollups: Dict[str, Any],
        *,
        since: Optional[datetime],
        until: Optional[datetime],
        bucket: str,
    ) -> List[str]:
        if since and until:
            start = _bucket_start(since, bucket)
            end = _bucket_start(until, bucket)
            return [
                _bucket_label(dt, bucket) for dt in _iter_buckets(start, end, bucket)
            ]

        times: List[datetime] = []
        for label in bucket_rollups.keys():
            dt = _parse_bucket_label(label, bucket)
            if dt:
                times.append(dt)
        if not times:
            return []
        start = min(times)
        end = max(times)
        return [_bucket_label(dt, bucket) for dt in _iter_buckets(start, end, bucket)]

    def _build_series_from_map(
        self,
        buckets: List[str],
        series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]],
    ) -> List[Dict[str, Any]]:
        series: List[Dict[str, Any]] = []
        for (key, model, token_type), values in series_map.items():
            series_values = [int(values.get(bucket, 0)) for bucket in buckets]
            series.append(
                {
                    "key": key,
                    "model": model,
                    "token_type": token_type,
                    "total": sum(series_values),
                    "values": series_values,
                }
            )
        series.sort(key=lambda item: int(item["total"]), reverse=True)
        return series

    def _build_repo_summary(
        self,
        payload: Dict[str, Any],
        repo_root: Path,
        *,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> UsageSummary:
        if since or until:
            return summarize_repo_usage(
                repo_root,
                codex_home=self.codex_home,
                since=since,
                until=until,
            )
        repo_root = repo_root.resolve()
        rollups = cast(Dict[str, Any], payload.get("summary", {}).get("by_cwd", {}))
        acc = _SummaryAccumulator()
        for cwd, entry in rollups.items():
            try:
                cwd_path = Path(cwd)
            except (TypeError, ValueError, OSError) as exc:
                logger.debug("Failed to create Path from cwd %r: %s", cwd, exc)
                continue
            if cwd_path != repo_root and repo_root not in cwd_path.parents:
                continue
            acc.add_entry(entry)
        return UsageSummary(
            totals=acc.totals,
            events=acc.events,
            latest_rate_limits=acc.latest_rate_limits,
        )

    def _build_hub_summary(
        self,
        payload: Dict[str, Any],
        repo_map: List[Tuple[str, Path]],
        *,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> Tuple[Dict[str, UsageSummary], UsageSummary]:
        if since or until:
            return summarize_hub_usage(
                repo_map,
                codex_home=self.codex_home,
                since=since,
                until=until,
            )
        repo_map = [(repo_id, path.resolve()) for repo_id, path in repo_map]

        def _match_repo(cwd: Optional[Path]) -> Optional[str]:
            if not cwd:
                return None
            for repo_id, repo_path in repo_map:
                if cwd == repo_path or repo_path in cwd.parents:
                    return repo_id
            return None

        rollups = cast(Dict[str, Any], payload.get("summary", {}).get("by_cwd", {}))
        per_repo: Dict[str, _SummaryAccumulator] = {
            repo_id: _SummaryAccumulator() for repo_id, _ in repo_map
        }
        unmatched = _SummaryAccumulator()

        for cwd, entry in rollups.items():
            try:
                cwd_path = Path(cwd)
            except (TypeError, ValueError, OSError) as exc:
                logger.debug("Failed to create Path from cwd %r: %s", cwd, exc)
                cwd_path = None
            repo_id = _match_repo(cwd_path)
            if repo_id is None:
                unmatched.add_entry(entry)
            else:
                per_repo[repo_id].add_entry(entry)

        per_repo_summary = {
            repo_id: UsageSummary(
                totals=acc.totals,
                events=acc.events,
                latest_rate_limits=acc.latest_rate_limits,
            )
            for repo_id, acc in per_repo.items()
        }
        unmatched_summary = UsageSummary(
            totals=unmatched.totals,
            events=unmatched.events,
            latest_rate_limits=unmatched.latest_rate_limits,
        )
        return per_repo_summary, unmatched_summary

    def _build_repo_series(
        self,
        payload: Dict[str, Any],
        repo_root: Path,
        *,
        since: Optional[datetime],
        until: Optional[datetime],
        bucket: str,
        segment: str,
    ) -> Dict[str, object]:
        allowed_buckets = {"hour", "day", "week"}
        allowed_segments = {"none", "model", "token_type", "model_token"}
        if bucket not in allowed_buckets:
            raise UsageError(f"Unsupported bucket: {bucket}")
        if segment not in allowed_segments:
            raise UsageError(f"Unsupported segment: {segment}")
        repo_root = repo_root.resolve()
        rollups = cast(Dict[str, Any], payload.get("rollups", {}).get("by_cwd", {}))

        series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]] = {}
        bucket_union: Dict[str, Any] = {}

        for cwd, cwd_data in rollups.items():
            try:
                cwd_path = Path(cwd)
            except (TypeError, ValueError, OSError) as exc:
                logger.debug("Failed to create Path from cwd %r: %s", cwd, exc)
                continue
            if cwd_path != repo_root and repo_root not in cwd_path.parents:
                continue
            bucket_rollups = cwd_data.get(bucket, {})
            if not bucket_rollups:
                continue
            bucket_union.update(bucket_rollups)
            for bucket_label, entry in bucket_rollups.items():
                if segment == "none":
                    key = ("total", None, None)
                    series_map.setdefault(key, {})
                    series_map[key][bucket_label] = series_map[key].get(
                        bucket_label, 0
                    ) + int(entry.get("total", 0))
                    continue

                if segment == "model":
                    for model_key, total in (entry.get("models") or {}).items():
                        key = (model_key, model_key, None)
                        series_map.setdefault(key, {})
                        series_map[key][bucket_label] = series_map[key].get(
                            bucket_label, 0
                        ) + int(total)
                    continue

                if segment == "token_type":
                    for token_key, total in (entry.get("token_types") or {}).items():
                        key = (token_key, None, token_key)
                        series_map.setdefault(key, {})
                        series_map[key][bucket_label] = series_map[key].get(
                            bucket_label, 0
                        ) + int(total)
                    continue

                for model_key, token_map in (entry.get("model_token") or {}).items():
                    for token_key, total in (token_map or {}).items():
                        key = (
                            f"{model_key}:{token_key}",
                            model_key,
                            token_key,
                        )
                        series_map.setdefault(key, {})
                        series_map[key][bucket_label] = series_map[key].get(
                            bucket_label, 0
                        ) + int(total)

        buckets = self._buckets_for_range(
            bucket_union, since=since, until=until, bucket=bucket
        )
        series = self._build_series_from_map(buckets, series_map)
        return {
            "bucket": bucket,
            "segment": segment,
            "buckets": buckets,
            "series": series,
        }

    def _build_hub_series(
        self,
        payload: Dict[str, Any],
        repo_map: List[Tuple[str, Path]],
        *,
        since: Optional[datetime],
        until: Optional[datetime],
        bucket: str,
        segment: str,
    ) -> Dict[str, object]:
        allowed_buckets = {"hour", "day", "week"}
        allowed_segments = {"none", "repo"}
        if bucket not in allowed_buckets:
            raise UsageError(f"Unsupported bucket: {bucket}")
        if segment not in allowed_segments:
            raise UsageError(f"Unsupported segment: {segment}")
        repo_map = [(repo_id, path.resolve()) for repo_id, path in repo_map]

        def _match_repo(cwd: Path) -> Optional[str]:
            for repo_id, repo_path in repo_map:
                if cwd == repo_path or repo_path in cwd.parents:
                    return repo_id
            return None

        rollups = cast(Dict[str, Any], payload.get("rollups", {}).get("by_cwd", {}))
        series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]] = {}
        bucket_union: Dict[str, Any] = {}

        for cwd, cwd_data in rollups.items():
            bucket_rollups = cwd_data.get(bucket, {})
            if not bucket_rollups:
                continue
            bucket_union.update(bucket_rollups)
            try:
                cwd_path = Path(cwd)
            except (TypeError, ValueError, OSError) as exc:
                logger.debug("Failed to create Path from cwd %r: %s", cwd, exc)
                cwd_path = None

            if segment == "none":
                for bucket_label, entry in bucket_rollups.items():
                    key = ("total", None, None)
                    series_map.setdefault(key, {})
                    series_map[key][bucket_label] = series_map[key].get(
                        bucket_label, 0
                    ) + int(entry.get("total", 0))
                continue

            repo_id = _match_repo(cwd_path) if cwd_path else None
            label = repo_id or "other"
            for bucket_label, entry in bucket_rollups.items():
                repo_key: Tuple[str, Optional[str], Optional[str]] = (
                    label,
                    repo_id,
                    None,
                )
                series_map.setdefault(repo_key, {})
                series_map[repo_key][bucket_label] = series_map[repo_key].get(
                    bucket_label, 0
                ) + int(entry.get("total", 0))

        buckets = self._buckets_for_range(
            bucket_union, since=since, until=until, bucket=bucket
        )
        series = self._build_series_from_map(buckets, series_map)
        return {
            "bucket": bucket,
            "segment": segment,
            "buckets": buckets,
            "series": series,
        }


_USAGE_SERIES_CACHES: Dict[str, UsageSeriesCache] = {}


def _build_series_entries(
    buckets: List[str],
    series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]],
) -> List[Dict[str, Any]]:
    series: List[Dict[str, Any]] = []
    for (key, model, token_type), values in series_map.items():
        series_values = [int(values.get(bucket, 0)) for bucket in buckets]
        series.append(
            {
                "key": key,
                "model": model,
                "token_type": token_type,
                "total": sum(series_values),
                "values": series_values,
            }
        )
    series.sort(key=lambda item: int(item["total"]), reverse=True)
    return series


def _bucket_labels_for_events(
    timestamps: List[datetime],
    *,
    bucket: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> List[str]:
    if since and until:
        start = _bucket_start(since, bucket)
        end = _bucket_start(until, bucket)
        return [_bucket_label(dt, bucket) for dt in _iter_buckets(start, end, bucket)]
    if not timestamps:
        return []
    start = _bucket_start(min(timestamps), bucket)
    end = _bucket_start(max(timestamps), bucket)
    return [_bucket_label(dt, bucket) for dt in _iter_buckets(start, end, bucket)]


def _sort_bucket_labels(labels: Iterable[str], bucket: str) -> List[str]:
    def _sort_key(label: str) -> datetime:
        parsed = _parse_bucket_label(label, bucket)
        return parsed or datetime.min.replace(tzinfo=timezone.utc)

    return sorted(set(labels), key=_sort_key)


def _merge_usage_series(
    base: Dict[str, object],
    extra: Dict[str, object],
    *,
    bucket: str,
) -> Dict[str, object]:
    base_buckets = cast(List[str], base.get("buckets", []))
    extra_buckets = cast(List[str], extra.get("buckets", []))
    buckets = _sort_bucket_labels(base_buckets + extra_buckets, bucket)

    def _series_to_map(
        series: List[Dict[str, Any]], buckets_ref: List[str]
    ) -> Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]]:
        series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]] = {}
        for entry in series:
            raw_key = entry.get("key")
            if raw_key is None:
                continue
            key = (
                str(raw_key),
                cast(Optional[str], entry.get("model")),
                cast(Optional[str], entry.get("token_type")),
            )
            values = cast(List[int], entry.get("values", []))
            bucket_map = {label: int(val) for label, val in zip(buckets_ref, values)}
            series_map[key] = bucket_map
        return series_map

    base_map = _series_to_map(
        cast(List[Dict[str, Any]], base.get("series", [])), base_buckets
    )
    extra_map = _series_to_map(
        cast(List[Dict[str, Any]], extra.get("series", [])), extra_buckets
    )

    merged_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]] = {}
    for series_map in (base_map, extra_map):
        for key, values in series_map.items():
            bucket_values = merged_map.setdefault(key, {})
            for label, value in values.items():
                bucket_values[label] = bucket_values.get(label, 0) + int(value)

    return {
        "bucket": bucket,
        "segment": base.get("segment", extra.get("segment")),
        "buckets": buckets,
        "series": _build_series_entries(buckets, merged_map),
    }


def _build_series_from_events(
    events: Iterable[TokenEvent],
    *,
    bucket: str,
    segment: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> Dict[str, object]:
    allowed_buckets = {"hour", "day", "week"}
    allowed_segments = {"none", "model", "token_type", "model_token", "agent"}
    if bucket not in allowed_buckets:
        raise UsageError(f"Unsupported bucket: {bucket}")
    if segment not in allowed_segments:
        raise UsageError(f"Unsupported segment: {segment}")

    token_fields = [
        ("input", "input_tokens"),
        ("cached", "cached_input_tokens"),
        ("output", "output_tokens"),
        ("reasoning", "reasoning_output_tokens"),
    ]
    series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]] = {}
    timestamps: List[datetime] = []

    for event in events:
        bucket_label = _bucket_label(_bucket_start(event.timestamp, bucket), bucket)
        timestamps.append(event.timestamp)
        if segment == "none":
            series_key: Tuple[str, Optional[str], Optional[str]] = ("total", None, None)
            series_map.setdefault(series_key, {})
            series_map[series_key][bucket_label] = series_map[series_key].get(
                bucket_label, 0
            ) + int(event.delta.total_tokens)
            continue

        if segment == "agent":
            series_key = (event.agent, None, None)
            series_map.setdefault(series_key, {})
            series_map[series_key][bucket_label] = series_map[series_key].get(
                bucket_label, 0
            ) + int(event.delta.total_tokens)
            continue

        if segment == "model":
            model = event.model or "unknown"
            series_key = (model, model, None)
            series_map.setdefault(series_key, {})
            series_map[series_key][bucket_label] = series_map[series_key].get(
                bucket_label, 0
            ) + int(event.delta.total_tokens)
            continue

        if segment == "token_type":
            for label, field in token_fields:
                value = getattr(event.delta, field)
                if not value:
                    continue
                series_key = (label, None, label)
                series_map.setdefault(series_key, {})
                series_map[series_key][bucket_label] = series_map[series_key].get(
                    bucket_label, 0
                ) + int(value)
            continue

        model = event.model or "unknown"
        for label, field in token_fields:
            value = getattr(event.delta, field)
            if not value:
                continue
            series_key = (f"{model}:{label}", model, label)
            series_map.setdefault(series_key, {})
            series_map[series_key][bucket_label] = series_map[series_key].get(
                bucket_label, 0
            ) + int(value)

    buckets = _bucket_labels_for_events(
        timestamps, bucket=bucket, since=since, until=until
    )
    return {
        "bucket": bucket,
        "segment": segment,
        "buckets": buckets,
        "series": _build_series_entries(buckets, series_map),
    }


def _build_repo_opencode_series(
    repo_root: Path,
    *,
    since: Optional[datetime],
    until: Optional[datetime],
    bucket: str,
    segment: str,
) -> Dict[str, object]:
    events = list(iter_opencode_events([repo_root], since=since, until=until))
    return _build_series_from_events(
        events, bucket=bucket, segment=segment, since=since, until=until
    )


def _build_hub_opencode_series(
    repo_map: List[Tuple[str, Path]],
    *,
    since: Optional[datetime],
    until: Optional[datetime],
    bucket: str,
    segment: str,
) -> Dict[str, object]:
    allowed_buckets = {"hour", "day", "week"}
    allowed_segments = {"none", "repo", "agent"}
    if bucket not in allowed_buckets:
        raise UsageError(f"Unsupported bucket: {bucket}")
    if segment not in allowed_segments:
        raise UsageError(f"Unsupported segment: {segment}")

    repo_map = [(repo_id, path.resolve()) for repo_id, path in repo_map]

    def _match_repo(cwd: Optional[Path]) -> Optional[str]:
        if not cwd:
            return None
        for repo_id, repo_path in repo_map:
            if cwd == repo_path or repo_path in cwd.parents:
                return repo_id
        return None

    series_map: Dict[Tuple[str, Optional[str], Optional[str]], Dict[str, int]] = {}
    timestamps: List[datetime] = []
    events = iter_opencode_events(
        [path for _, path in repo_map], since=since, until=until
    )
    for event in events:
        repo_id = _match_repo(event.cwd)
        if repo_id is None:
            continue
        bucket_label = _bucket_label(_bucket_start(event.timestamp, bucket), bucket)
        timestamps.append(event.timestamp)
        if segment == "none":
            key = ("total", None, None)
            series_map.setdefault(key, {})
            series_map[key][bucket_label] = series_map[key].get(bucket_label, 0) + int(
                event.delta.total_tokens
            )
            continue

        if segment == "agent":
            key = (event.agent, None, None)
            series_map.setdefault(key, {})
            series_map[key][bucket_label] = series_map[key].get(bucket_label, 0) + int(
                event.delta.total_tokens
            )
            continue

        repo_key: Tuple[str, Optional[str], Optional[str]] = (repo_id, repo_id, None)
        series_map.setdefault(repo_key, {})
        series_map[repo_key][bucket_label] = series_map[repo_key].get(
            bucket_label, 0
        ) + int(event.delta.total_tokens)

    buckets = _bucket_labels_for_events(
        timestamps, bucket=bucket, since=since, until=until
    )
    return {
        "bucket": bucket,
        "segment": segment,
        "buckets": buckets,
        "series": _build_series_entries(buckets, series_map),
    }


def get_usage_series_cache(codex_home: Path) -> UsageSeriesCache:
    cache_path = _default_usage_series_cache_path(codex_home)
    key = str(cache_path)
    cache = _USAGE_SERIES_CACHES.get(key)
    if cache is None:
        cache = UsageSeriesCache(codex_home, cache_path)
        _USAGE_SERIES_CACHES[key] = cache
    return cache


def get_repo_usage_series_cached(
    repo_root: Path,
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    bucket: str = "day",
    segment: str = "none",
) -> Tuple[Dict[str, object], str]:
    codex_root = (codex_home or default_codex_home()).expanduser()
    cache = get_usage_series_cache(codex_root)
    if segment == "agent":
        codex_series, status = cache.get_repo_series(
            repo_root, since=since, until=until, bucket=bucket, segment="none"
        )
        opencode_series = _build_repo_opencode_series(
            repo_root, since=since, until=until, bucket=bucket, segment="agent"
        )
        codex_series["segment"] = "agent"
        codex_series["series"] = [
            {**entry, "key": CODEX_AGENT_ID}
            for entry in cast(List[Dict[str, Any]], codex_series.get("series", []))
        ]
        return _merge_usage_series(codex_series, opencode_series, bucket=bucket), status

    codex_series, status = cache.get_repo_series(
        repo_root, since=since, until=until, bucket=bucket, segment=segment
    )
    opencode_series = _build_repo_opencode_series(
        repo_root, since=since, until=until, bucket=bucket, segment=segment
    )
    return _merge_usage_series(codex_series, opencode_series, bucket=bucket), status


def get_repo_usage_summary_cached(
    repo_root: Path,
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Tuple[UsageSummary, str]:
    codex_root = (codex_home or default_codex_home()).expanduser()
    cache = get_usage_series_cache(codex_root)
    summary, status = cache.get_repo_summary(repo_root, since=since, until=until)
    opencode_summary = summarize_opencode_repo_usage(
        repo_root, since=since, until=until
    )
    merged = UsageSummary(
        totals=TokenTotals(),
        events=summary.events + opencode_summary.events,
        latest_rate_limits=summary.latest_rate_limits,
    )
    merged.totals.add(summary.totals)
    merged.totals.add(opencode_summary.totals)
    return merged, status


def get_hub_usage_series_cached(
    repo_map: List[Tuple[str, Path]],
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    bucket: str = "day",
    segment: str = "none",
) -> Tuple[Dict[str, object], str]:
    codex_root = (codex_home or default_codex_home()).expanduser()
    cache = get_usage_series_cache(codex_root)
    if segment == "agent":
        codex_series, status = cache.get_hub_series(
            repo_map, since=since, until=until, bucket=bucket, segment="none"
        )
        opencode_series = _build_hub_opencode_series(
            repo_map, since=since, until=until, bucket=bucket, segment="agent"
        )
        codex_series["segment"] = "agent"
        codex_series["series"] = [
            {**entry, "key": CODEX_AGENT_ID}
            for entry in cast(List[Dict[str, Any]], codex_series.get("series", []))
        ]
        return _merge_usage_series(codex_series, opencode_series, bucket=bucket), status

    codex_series, status = cache.get_hub_series(
        repo_map, since=since, until=until, bucket=bucket, segment=segment
    )
    opencode_series = _build_hub_opencode_series(
        repo_map, since=since, until=until, bucket=bucket, segment=segment
    )
    return _merge_usage_series(codex_series, opencode_series, bucket=bucket), status


def get_hub_usage_summary_cached(
    repo_map: List[Tuple[str, Path]],
    codex_home: Optional[Path] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Tuple[Dict[str, UsageSummary], UsageSummary, str]:
    codex_root = (codex_home or default_codex_home()).expanduser()
    cache = get_usage_series_cache(codex_root)
    per_repo, unmatched, status = cache.get_hub_summary(
        repo_map, since=since, until=until
    )
    opencode_per_repo = summarize_opencode_hub_usage(repo_map, since=since, until=until)
    merged_per_repo: Dict[str, UsageSummary] = {}
    for repo_id, summary in per_repo.items():
        extra = opencode_per_repo.get(repo_id)
        if extra:
            merged = UsageSummary(
                totals=TokenTotals(),
                events=summary.events + extra.events,
                latest_rate_limits=summary.latest_rate_limits,
            )
            merged.totals.add(summary.totals)
            merged.totals.add(extra.totals)
            merged_per_repo[repo_id] = merged
        else:
            merged_per_repo[repo_id] = summary
    return merged_per_repo, unmatched, status
