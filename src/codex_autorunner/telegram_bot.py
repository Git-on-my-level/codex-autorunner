"""Compatibility shim for telegram bot imports.

TODO: Remove after one release cycle.
"""

from .integrations.telegram.config import (  # noqa: F401
    DEFAULT_APP_SERVER_COMMAND,
)
from .integrations.telegram.constants import (  # noqa: F401
    WHISPER_TRANSCRIPT_DISCLAIMER,
)
from .integrations.telegram.helpers import (  # noqa: F401
    _extract_thread_path,
    _paths_compatible,
    _telegram_lock_path,
)
from .integrations.telegram.service import *  # noqa: F401,F403
