"""OpenCode harness support."""

from .client import OpenCodeClient
from .events import SSEEvent, parse_sse_lines
from .harness import OpenCodeHarness
from .run_prompt import OpenCodeRunConfig, OpenCodeRunResult, run_opencode_prompt
from .supervisor import OpenCodeSupervisor

__all__ = [
    "OpenCodeClient",
    "OpenCodeHarness",
    "OpenCodeRunConfig",
    "OpenCodeRunResult",
    "OpenCodeSupervisor",
    "SSEEvent",
    "parse_sse_lines",
    "run_opencode_prompt",
]
