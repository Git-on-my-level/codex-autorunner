"""Command handler modules for Telegram integration.

This package contains focused modules for handling different categories of Telegram commands.
"""

from ..commands_spec import CommandSpec, build_command_specs
from .approvals import ApprovalsCommands
from .execution import ExecutionCommands
from .files import FilesCommands
from .formatting import FormattingHelpers
from .github import GitHubCommands
from .shared import SharedHelpers
from .voice import VoiceCommands
from .workspace import WorkspaceCommands

__all__ = [
    "SharedHelpers",
    "WorkspaceCommands",
    "GitHubCommands",
    "FilesCommands",
    "VoiceCommands",
    "ExecutionCommands",
    "ApprovalsCommands",
    "FormattingHelpers",
    "CommandSpec",
    "build_command_specs",
]
