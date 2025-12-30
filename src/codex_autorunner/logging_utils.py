import collections
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, OrderedDict

from .config import LogConfig

_MAX_CACHED_LOGGERS = 64
_LOGGER_CACHE: "OrderedDict[str, logging.Logger]" = collections.OrderedDict()


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
