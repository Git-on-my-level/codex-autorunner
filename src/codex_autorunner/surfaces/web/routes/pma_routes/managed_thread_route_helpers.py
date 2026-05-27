"""Temporary compatibility imports for managed-thread read-model helpers.

New callers and tests should import from ``services.pma.managed_thread_read_models``.
This module remains only to avoid breaking older route-helper import paths during
the managed-thread service migration.
"""

from __future__ import annotations

from ...services.pma.managed_thread_read_models import (
    ManagedThreadCreateResolution,
    ManagedThreadListQuery,
    ManagedThreadOwnerScopedQuery,
    ManagedThreadWorkspaceProvision,
    _apply_chat_binding_fields,
    _attach_latest_execution_fields,
    _build_operator_status_fields,
    _load_chat_binding_metadata_by_thread,
    _normalize_resource_owner,
    _normalize_workspace_root_input,
    _resolve_running_or_latest_execution,
    _serialize_managed_thread,
    _serialize_thread_target,
    managed_thread_metadata_for_provisioned_workspace,
    provision_managed_thread_workspace,
    resolve_managed_thread_create_resolution,
    resolve_managed_thread_list_query,
    resolve_owner_scoped_query,
    serialize_active_work_summary,
    serialize_binding_record,
    serialize_managed_thread_turn_summary,
)

__all__ = [
    "ManagedThreadCreateResolution",
    "ManagedThreadListQuery",
    "ManagedThreadOwnerScopedQuery",
    "ManagedThreadWorkspaceProvision",
    "_apply_chat_binding_fields",
    "_attach_latest_execution_fields",
    "_build_operator_status_fields",
    "_load_chat_binding_metadata_by_thread",
    "_normalize_resource_owner",
    "_normalize_workspace_root_input",
    "_resolve_running_or_latest_execution",
    "_serialize_managed_thread",
    "_serialize_thread_target",
    "managed_thread_metadata_for_provisioned_workspace",
    "provision_managed_thread_workspace",
    "resolve_managed_thread_create_resolution",
    "resolve_managed_thread_list_query",
    "resolve_owner_scoped_query",
    "serialize_active_work_summary",
    "serialize_binding_record",
    "serialize_managed_thread_turn_summary",
]
