"""Compatibility shim for telegram bot imports.

TODO: Remove after one release cycle.
"""

from .integrations.telegram.config import (  # noqa: F401
    DEFAULT_APP_SERVER_COMMAND,
)
from .integrations.telegram.service import *  # noqa: F401,F403
from .integrations.telegram.service import (  # noqa: F401
    _extract_thread_path,
    _paths_compatible,
    _telegram_lock_path,
)
