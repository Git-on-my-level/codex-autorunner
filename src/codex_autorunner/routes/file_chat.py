"""Backward-compatible file chat routes."""

import sys

from ..surfaces.web.routes import file_chat as _file_chat

sys.modules[__name__] = _file_chat
