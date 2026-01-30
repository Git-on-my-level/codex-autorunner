"""Backward-compatible flow routes."""

import sys

from ..surfaces.web.routes import flows as _flows

sys.modules[__name__] = _flows
