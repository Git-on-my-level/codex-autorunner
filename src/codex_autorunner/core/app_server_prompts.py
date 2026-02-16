"""Compatibility shim for app-server prompt builders."""

from importlib import import_module

_prompts = import_module("codex_autorunner.integrations.app_server.prompts")

AUTORUNNER_APP_SERVER_TEMPLATE = _prompts.AUTORUNNER_APP_SERVER_TEMPLATE
APP_SERVER_PROMPT_BUILDERS = _prompts.APP_SERVER_PROMPT_BUILDERS
TRUNCATION_MARKER = _prompts.TRUNCATION_MARKER
build_autorunner_prompt = _prompts.build_autorunner_prompt
truncate_text = _prompts.truncate_text

__all__ = [
    "AUTORUNNER_APP_SERVER_TEMPLATE",
    "APP_SERVER_PROMPT_BUILDERS",
    "TRUNCATION_MARKER",
    "build_autorunner_prompt",
    "truncate_text",
]
