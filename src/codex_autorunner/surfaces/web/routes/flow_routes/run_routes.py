"""
Lifecycle route helpers for flow runs.

NOTE: This module is deprecated and will be removed. The canonical lifecycle
routes are implemented directly in flows.py. This file is kept temporarily
to avoid breaking any imports during the transition.

See TICKET-030 for the route convergence work.
"""

import logging

_logger = logging.getLogger(__name__)
