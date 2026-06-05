from __future__ import annotations

from pathlib import Path

DEFAULT_LOG_MAX_BYTES = 1024 * 1024
DEFAULT_LOG_TAIL_LINES = 200

_STATE_DIR = ".codex-autorunner"
_SERVICES_DIR = "services"
_LOGS_DIR = "logs"


class PreviewServiceLogError(ValueError):
    pass


def service_log_relative_path(service_id: str) -> str:
    return f"{_STATE_DIR}/{_SERVICES_DIR}/{_LOGS_DIR}/{service_id}.log"


def service_log_tail_url(service_id: str) -> str:
    return f"/hub/services/{service_id}/logs"


def service_log_path(hub_root: Path, service_id: str) -> Path:
    return hub_root.resolve() / service_log_relative_path(service_id)


def prepare_log_file(
    hub_root: Path,
    service_id: str,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
) -> Path:
    path = service_log_path(hub_root, service_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and max_bytes > 0 and path.stat().st_size > max_bytes:
        _keep_tail_bytes(path, max_bytes)
    path.touch(exist_ok=True)
    return path


def tail_log_file(
    hub_root: Path,
    service_id: str,
    *,
    lines: int = DEFAULT_LOG_TAIL_LINES,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
) -> str:
    path = service_log_path(hub_root, service_id)
    if not path.exists():
        return ""
    line_count = max(0, int(lines))
    if line_count == 0:
        return ""
    byte_count = max(1, int(max_bytes))
    with path.open("rb") as handle:
        try:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - byte_count))
        except OSError as exc:
            raise PreviewServiceLogError(f"Unable to read service log: {path}") from exc
        content = handle.read()
    text = content.decode("utf-8", errors="replace")
    return "".join(text.splitlines(keepends=True)[-line_count:])


def _keep_tail_bytes(path: Path, max_bytes: int) -> None:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - max_bytes))
        content = handle.read()
    path.write_bytes(content)
