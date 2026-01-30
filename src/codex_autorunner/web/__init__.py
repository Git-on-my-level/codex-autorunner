"""Backward-compatible web surface exports."""

from ..surfaces.web.app import create_app, create_hub_app, create_repo_app  # noqa: F401

__all__ = ["create_app", "create_hub_app", "create_repo_app"]
