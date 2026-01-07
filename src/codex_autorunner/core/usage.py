import copy
import dataclasses
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast


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
    except Exception:
        return None


def _empty_rollup_bucket() -> Dict[str, Any]:
    return {
        "total": 0,
        "models": {},
        "token_types": {},
        "model_token": {},
    }


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
                "version": 2,
                "files": {},
                "file_rollups": {},
                "rollups": {"by_cwd": {}},
            }
            return self._cache
        try:
            payload = cast(
                Dict[str, Any], json.loads(self.cache_path.read_text(encoding="utf-8"))
            )
            if payload.get("version") != 2:
                raise ValueError("Unsupported cache version")
            payload.setdefault("files", {})
            payload.setdefault("file_rollups", {})
            payload.setdefault("rollups", {}).setdefault("by_cwd", {})
            self._cache = payload
            return payload
        except Exception:
            self._cache = {
                "version": 2,
                "files": {},
                "file_rollups": {},
                "rollups": {"by_cwd": {}},
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
            except Exception:
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

    def _update_cache(self, payload: Dict[str, Any]) -> None:
        try:
            files = cast(Dict[str, Any], payload.setdefault("files", {}))
            file_rollups = cast(Dict[str, Any], payload.setdefault("file_rollups", {}))
            rollups = cast(
                Dict[str, Any],
                payload.setdefault("rollups", {}).setdefault("by_cwd", {}),
            )
            rebuild_rollups = False
            existing_paths = {
                str(path) for path in _iter_session_files(self.codex_home)
            }
            for path_key in list(files.keys()):
                if path_key not in existing_paths:
                    files.pop(path_key, None)
                    file_rollups.pop(path_key, None)
                    rebuild_rollups = True

            for session_path in _iter_session_files(self.codex_home):
                path_key = str(session_path)
                file_state = files.get(path_key, {})
                offset = int(file_state.get("offset", 0) or 0)
                try:
                    size = session_path.stat().st_size
                except Exception:
                    continue
                if size < offset:
                    offset = 0
                    file_state = {}
                    file_rollups.pop(path_key, None)
                    rebuild_rollups = True
                if size == offset:
                    continue
                updated_state = self._ingest_session_file(
                    session_path, offset, file_state, rollups, file_rollups
                )
                files[path_key] = updated_state
            if rebuild_rollups:
                payload["rollups"]["by_cwd"] = self._rebuild_rollups(file_rollups)
            payload["version"] = 2
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
    ) -> Dict[str, Any]:
        cwd = state.get("cwd")
        model = state.get("model")
        last_totals_raw = state.get("last_totals")
        last_totals = _coerce_totals(last_totals_raw) if last_totals_raw else None

        try:
            with session_path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
                new_offset = handle.tell()
        except Exception:
            return state

        if not data:
            state["offset"] = offset
            return state

        try:
            text = data.decode("utf-8")
        except Exception:
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

        for line in lines:
            try:
                record = json.loads(line)
            except Exception:
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
            if not total_usage and not last_usage:
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
            try:
                timestamp = _parse_timestamp(timestamp_raw)
            except UsageError:
                continue

            cwd_key = cwd or "__unknown__"
            model_key = model or "unknown"

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

        state["offset"] = new_offset
        state["cwd"] = cwd
        state["model"] = model
        state["last_totals"] = last_totals.to_dict() if last_totals else None
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
            except Exception:
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
            except Exception:
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
    return cache.get_repo_series(
        repo_root, since=since, until=until, bucket=bucket, segment=segment
    )


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
    return cache.get_hub_series(
        repo_map, since=since, until=until, bucket=bucket, segment=segment
    )
