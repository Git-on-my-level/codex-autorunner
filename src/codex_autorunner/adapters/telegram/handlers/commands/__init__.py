"""Command handler modules for Telegram integration.

This package contains focused modules for handling different categories of Telegram commands.
"""

from ..commands_spec import CommandSpec, build_command_specs
from .approvals import ApprovalsCommands
from .document_browser import DocumentBrowserCommands
from .execution import ExecutionCommands
from .files import FilesCommands
from .flows import FlowCommands
from .formatting import FormattingHelpers
from .github import GitHubCommands
from .shared import TelegramCommandSupportMixin
from .voice import VoiceCommands
from .workspace import WorkspaceCommands
from .workspace_binding import WorkspaceBindingMixin
from .workspace_resume import (
    ResumeCommandArgs,
    ResumeThreadData,
    WorkspaceResumeMixin,
)
from .workspace_session_commands import WorkspaceSessionCommandsMixin
from .workspace_status import WorkspaceStatusMixin

WorkspaceCommandsMixin = WorkspaceCommands

__all__ = [
    "ApprovalsCommands",
    "CommandSpec",
    "DocumentBrowserCommands",
    "ExecutionCommands",
    "FilesCommands",
    "FlowCommands",
    "FormattingHelpers",
    "GitHubCommands",
    "ResumeCommandArgs",
    "ResumeThreadData",
    "TelegramCommandSupportMixin",
    "VoiceCommands",
    "WorkspaceBindingMixin",
    "WorkspaceCommands",
    "WorkspaceCommandsMixin",
    "WorkspaceResumeMixin",
    "WorkspaceSessionCommandsMixin",
    "WorkspaceStatusMixin",
    "build_command_specs",
]
