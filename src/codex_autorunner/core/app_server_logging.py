"""Compatibility shim for app-server event formatting."""

from importlib import import_module

AppServerEventFormatter = import_module(
    "codex_autorunner.integrations.app_server.logging"
).AppServerEventFormatter

__all__ = ["AppServerEventFormatter"]
