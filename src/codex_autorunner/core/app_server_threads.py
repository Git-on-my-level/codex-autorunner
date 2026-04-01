"""Compatibility shim for app-server thread registry helpers.

Uses importlib to avoid a static core -> integrations import edge (see
scripts/check_import_boundaries.py).
"""

from importlib import import_module

_threads = import_module("codex_autorunner.integrations.app_server.threads")

APP_SERVER_THREADS_FILENAME = _threads.APP_SERVER_THREADS_FILENAME
APP_SERVER_THREADS_VERSION = _threads.APP_SERVER_THREADS_VERSION
APP_SERVER_THREADS_CORRUPT_SUFFIX = _threads.APP_SERVER_THREADS_CORRUPT_SUFFIX
APP_SERVER_THREADS_NOTICE_SUFFIX = _threads.APP_SERVER_THREADS_NOTICE_SUFFIX
FILE_CHAT_KEY = _threads.FILE_CHAT_KEY
FILE_CHAT_OPENCODE_KEY = _threads.FILE_CHAT_OPENCODE_KEY
FILE_CHAT_HERMES_KEY = _threads.FILE_CHAT_HERMES_KEY
FILE_CHAT_PREFIX = _threads.FILE_CHAT_PREFIX
FILE_CHAT_OPENCODE_PREFIX = _threads.FILE_CHAT_OPENCODE_PREFIX
FILE_CHAT_HERMES_PREFIX = _threads.FILE_CHAT_HERMES_PREFIX
PMA_KEY = _threads.PMA_KEY
PMA_OPENCODE_KEY = _threads.PMA_OPENCODE_KEY
PMA_HERMES_KEY = _threads.PMA_HERMES_KEY
PMA_PREFIX = _threads.PMA_PREFIX
PMA_OPENCODE_PREFIX = _threads.PMA_OPENCODE_PREFIX
PMA_HERMES_PREFIX = _threads.PMA_HERMES_PREFIX
FEATURE_KEYS = _threads.FEATURE_KEYS
default_app_server_threads_path = _threads.default_app_server_threads_path
normalize_feature_key = _threads.normalize_feature_key
pma_base_key = _threads.pma_base_key
pma_legacy_alias_key = _threads.pma_legacy_alias_key
pma_legacy_alias_keys = _threads.pma_legacy_alias_keys
pma_legacy_migration_fallback_keys = _threads.pma_legacy_migration_fallback_keys
pma_prefix_for_agent = _threads.pma_prefix_for_agent
pma_prefixes_for_reset = _threads.pma_prefixes_for_reset
pma_topic_scoped_key = _threads.pma_topic_scoped_key
file_chat_discord_key = _threads.file_chat_discord_key
AppServerThreadRegistry = _threads.AppServerThreadRegistry

__all__ = [
    "APP_SERVER_THREADS_CORRUPT_SUFFIX",
    "APP_SERVER_THREADS_FILENAME",
    "APP_SERVER_THREADS_NOTICE_SUFFIX",
    "APP_SERVER_THREADS_VERSION",
    "AppServerThreadRegistry",
    "FEATURE_KEYS",
    "FILE_CHAT_HERMES_KEY",
    "FILE_CHAT_HERMES_PREFIX",
    "FILE_CHAT_KEY",
    "FILE_CHAT_OPENCODE_KEY",
    "FILE_CHAT_OPENCODE_PREFIX",
    "FILE_CHAT_PREFIX",
    "PMA_HERMES_KEY",
    "PMA_HERMES_PREFIX",
    "PMA_KEY",
    "PMA_OPENCODE_KEY",
    "PMA_OPENCODE_PREFIX",
    "PMA_PREFIX",
    "default_app_server_threads_path",
    "file_chat_discord_key",
    "normalize_feature_key",
    "pma_base_key",
    "pma_legacy_alias_key",
    "pma_legacy_alias_keys",
    "pma_legacy_migration_fallback_keys",
    "pma_prefix_for_agent",
    "pma_prefixes_for_reset",
    "pma_topic_scoped_key",
]
