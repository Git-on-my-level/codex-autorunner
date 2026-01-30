"""Backward-compatible message routes."""

import sys

from ..surfaces.web.routes import messages as _messages

sys.modules[__name__] = _messages
