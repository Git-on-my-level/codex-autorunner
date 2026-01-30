"""Backward-compatible system routes."""

import sys

from ..surfaces.web.routes import system as _system

sys.modules[__name__] = _system
