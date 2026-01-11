import collections
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping, Optional, OrderedDict

from .config import LogConfig
from .request_context import get_request_id

_MAX_CACHED_LOGGERS = 64
_LOGGER_CACHE: "OrderedDict[str, logging.Logger]" = collections.OrderedDict()
_REDACTED_VALUE = "<redacted>"
_SENSITIVE_FIELD_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bot_token",
    "openai_api_key",
    "password",
    "secret",
    "token",
)
_MAX_LOG_STRING = 200


def setup_rotating_logger(name: str, log_config: LogConfig) -> logging.Logger:
    """
    Configure (or retrieve) an isolated rotating logger for the given name.
    Each logger owns a single handler to avoid shared handlers across hub/repos.
    """
    existing = _LOGGER_CACHE.get(name)
    if existing is not None:
        # Keep cache bounded and prefer most-recently-used.
        _LOGGER_CACHE.move_to_end(name)
        return existing

    log_path: Path = log_config.path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=log_config.max_bytes,
        backupCount=log_config.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False

    _LOGGER_CACHE[name] = logger
    _LOGGER_CACHE.move_to_end(name)
    # Bounded cache to avoid unbounded growth in long-lived hub processes.
    while len(_LOGGER_CACHE) > _MAX_CACHED_LOGGERS:
        _, evicted = _LOGGER_CACHE.popitem(last=False)
        try:
            for h in list(evicted.handlers):
                try:
                    h.close()
                except Exception:
                    pass
            evicted.handlers.clear()
        except Exception:
            pass
    return logger


def safe_log(
    logger: logging.Logger,
    level: int,
    message: str,
    *args,
    exc: Optional[Exception] = None,
) -> None:
    try:
        formatted = message
        if args:
            try:
                formatted = message % args
            except Exception:
                formatted = f"{message} {' '.join(str(arg) for arg in args)}"
        if exc is not None:
            formatted = f"{formatted}: {exc}"
        logger.log(level, formatted)
    except Exception:
        pass


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc: Optional[Exception] = None,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {"event": event}
    if "request_id" not in fields:
        request_id = get_request_id()
        if request_id:
            fields["request_id"] = request_id
    if fields:
        payload.update(_sanitize_fields(fields))
    if exc is not None:
        payload["error"] = _sanitize_value(str(exc))
        payload["error_type"] = type(exc).__name__
    try:
        message = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        logger.log(level, message)
    except Exception:
        pass


def _sanitize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if _is_sensitive_key(str(key)):
            sanitized[key] = _REDACTED_VALUE
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        if len(value) > _MAX_LOG_STRING:
            return value[: _MAX_LOG_STRING - 3] + "..."
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _sanitize_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in mapping.items():
        if _is_sensitive_key(str(key)):
            sanitized[key] = _REDACTED_VALUE
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_FIELD_PARTS)
