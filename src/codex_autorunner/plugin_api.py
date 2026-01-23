from __future__ import annotations

"""Codex Autorunner plugin API metadata.

This module is intentionally small and stable. External plugins SHOULD depend
only on the public API in `codex_autorunner.api` + this version constant.

Notes:
- Backwards-incompatible changes to the plugin API MUST bump
  `CAR_PLUGIN_API_VERSION`.
"""

CAR_PLUGIN_API_VERSION = 1

# Entry point groups (Python packaging entry points).
#
# Plugins can publish new agent backends by defining an entry point:
#
#   [project.entry-points."codex_autorunner.agent_backends"]
#   myagent = "my_package.my_module:AGENT_BACKEND"
#
CAR_AGENT_ENTRYPOINT_GROUP = "codex_autorunner.agent_backends"
