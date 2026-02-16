"""Compatibility shim for app-server thread registry helpers."""

from importlib import import_module

_threads = import_module("codex_autorunner.integrations.app_server.threads")

APP_SERVER_THREADS_FILENAME = _threads.APP_SERVER_THREADS_FILENAME
APP_SERVER_THREADS_VERSION = _threads.APP_SERVER_THREADS_VERSION
APP_SERVER_THREADS_CORRUPT_SUFFIX = _threads.APP_SERVER_THREADS_CORRUPT_SUFFIX
APP_SERVER_THREADS_NOTICE_SUFFIX = _threads.APP_SERVER_THREADS_NOTICE_SUFFIX
FILE_CHAT_KEY = _threads.FILE_CHAT_KEY
FILE_CHAT_OPENCODE_KEY = _threads.FILE_CHAT_OPENCODE_KEY
FILE_CHAT_PREFIX = _threads.FILE_CHAT_PREFIX
FILE_CHAT_OPENCODE_PREFIX = _threads.FILE_CHAT_OPENCODE_PREFIX
PMA_KEY = _threads.PMA_KEY
PMA_OPENCODE_KEY = _threads.PMA_OPENCODE_KEY
FEATURE_KEYS = _threads.FEATURE_KEYS
default_app_server_threads_path = _threads.default_app_server_threads_path
normalize_feature_key = _threads.normalize_feature_key
AppServerThreadRegistry = _threads.AppServerThreadRegistry

__all__ = [
    "APP_SERVER_THREADS_FILENAME",
    "APP_SERVER_THREADS_VERSION",
    "APP_SERVER_THREADS_CORRUPT_SUFFIX",
    "APP_SERVER_THREADS_NOTICE_SUFFIX",
    "FILE_CHAT_KEY",
    "FILE_CHAT_OPENCODE_KEY",
    "FILE_CHAT_PREFIX",
    "FILE_CHAT_OPENCODE_PREFIX",
    "PMA_KEY",
    "PMA_OPENCODE_KEY",
    "FEATURE_KEYS",
    "default_app_server_threads_path",
    "normalize_feature_key",
    "AppServerThreadRegistry",
]
