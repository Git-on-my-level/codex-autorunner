from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Coroutine, Optional, TypeVar

from .client import HubControlPlaneClient
from .errors import HubControlPlaneError
from .models import SurfaceBindingLookupRequest, SurfaceBindingUpsertRequest

ResultT = TypeVar("ResultT")


class RemoteSurfaceBindingStore:
    """Best-effort surface binding adapter backed by the hub control plane.

    The control plane currently exposes lookup and upsert operations for surface
    bindings. This adapter mirrors those authoritative operations and maintains a
    small local cache so read-only helpers like ``list_bindings`` continue to
    work for bindings observed in the current process.
    """

    def __init__(
        self,
        client: HubControlPlaneClient,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._bindings_by_key: dict[tuple[str, str], Any] = {}

    def _hub_unavailable(
        self,
        *,
        operation: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> HubControlPlaneError:
        payload = {"operation": operation}
        if isinstance(details, dict):
            payload.update(details)
        return HubControlPlaneError(
            "hub_unavailable",
            f"Hub control-plane unavailable during {operation}: {message}",
            retryable=True,
            details=payload,
        )

    def _run(
        self,
        *,
        operation: str,
        action: Callable[[], Coroutine[Any, Any, ResultT]],
    ) -> ResultT:
        def _invoke() -> ResultT:
            return asyncio.run(action())

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_invoke)
                return future.result(timeout=self._timeout_seconds)
        except FuturesTimeoutError as exc:
            raise self._hub_unavailable(
                operation=operation,
                message=f"request timed out after {self._timeout_seconds:g}s",
                details={"timeout_seconds": self._timeout_seconds},
            ) from exc
        except HubControlPlaneError as exc:
            if exc.code in {"hub_unavailable", "transport_failure"}:
                raise self._hub_unavailable(
                    operation=operation,
                    message=str(exc),
                    details={
                        "cause_code": exc.code,
                        **dict(exc.details),
                    },
                ) from exc
            raise
        except (ConnectionError, OSError) as exc:
            raise self._hub_unavailable(
                operation=operation,
                message=str(exc) or exc.__class__.__name__,
                details={"cause_type": exc.__class__.__name__},
            ) from exc

    @staticmethod
    def _normalize_key(surface_kind: str, surface_key: str) -> tuple[str, str]:
        return (str(surface_kind or "").strip(), str(surface_key or "").strip())

    def _remember(self, binding: Any) -> Any:
        if binding is None:
            return None
        key = self._normalize_key(
            getattr(binding, "surface_kind", ""),
            getattr(binding, "surface_key", ""),
        )
        if all(key):
            self._bindings_by_key[key] = binding
        return binding

    def upsert_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        thread_target_id: str,
        agent_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        mode: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Any:
        response = self._run(
            operation="upsert_surface_binding",
            action=lambda: self._client.upsert_surface_binding(
                SurfaceBindingUpsertRequest(
                    surface_kind=surface_kind,
                    surface_key=surface_key,
                    thread_target_id=thread_target_id,
                    agent_id=agent_id,
                    repo_id=repo_id,
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    mode=mode,
                    metadata=dict(metadata or {}),
                )
            ),
        )
        if response.binding is None:
            raise HubControlPlaneError(
                "hub_rejected",
                "Hub control-plane returned no surface binding",
                retryable=False,
                details={"operation": "upsert_surface_binding"},
            )
        return self._remember(response.binding)

    def get_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        include_disabled: bool = False,
    ) -> Any:
        response = self._run(
            operation="get_surface_binding",
            action=lambda: self._client.get_surface_binding(
                SurfaceBindingLookupRequest(
                    surface_kind=surface_kind,
                    surface_key=surface_key,
                    include_disabled=include_disabled,
                )
            ),
        )
        return self._remember(response.binding)

    def list_bindings(
        self,
        *,
        thread_target_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> list[Any]:
        _ = include_disabled
        bindings = list(self._bindings_by_key.values())
        filtered = []
        for binding in bindings:
            if (
                thread_target_id is not None
                and str(getattr(binding, "thread_target_id", "") or "").strip()
                != str(thread_target_id).strip()
            ):
                continue
            if (
                repo_id is not None
                and str(getattr(binding, "repo_id", "") or "").strip()
                != str(repo_id).strip()
            ):
                continue
            if (
                resource_kind is not None
                and str(getattr(binding, "resource_kind", "") or "").strip()
                != str(resource_kind).strip()
            ):
                continue
            if (
                resource_id is not None
                and str(getattr(binding, "resource_id", "") or "").strip()
                != str(resource_id).strip()
            ):
                continue
            if (
                agent_id is not None
                and str(getattr(binding, "agent_id", "") or "").strip()
                != str(agent_id).strip()
            ):
                continue
            if (
                surface_kind is not None
                and str(getattr(binding, "surface_kind", "") or "").strip()
                != str(surface_kind).strip()
            ):
                continue
            filtered.append(binding)
            if len(filtered) >= max(1, int(limit)):
                break
        return filtered

    def get_active_thread_for_binding(
        self, *, surface_kind: str, surface_key: str
    ) -> Optional[str]:
        binding = self.get_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        if binding is None:
            return None
        thread_target_id = str(getattr(binding, "thread_target_id", "") or "").strip()
        return thread_target_id or None

    def list_active_work_summaries(
        self,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[Any]:
        _ = repo_id, resource_kind, resource_id, agent_id, limit
        return []
