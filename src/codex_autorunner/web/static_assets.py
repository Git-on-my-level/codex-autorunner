"""Backward-compatible static asset exports."""

import sys

from ..surfaces.web import static_assets as _static_assets

sys.modules[__name__] = _static_assets
