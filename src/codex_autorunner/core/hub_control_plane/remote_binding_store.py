from __future__ import annotations

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Coroutine, Optional, TypeVar

from .client import HubControlPlaneClient
from .errors import HubControlPlaneError
from .models import (
    SurfaceBindingListRequest,
    SurfaceBindingLookupRequest,
    SurfaceBindingUpsertRequest,
)

ResultT = TypeVar("ResultT")


class RemoteSurfaceBindingStore:
    """Best-effort surface binding adapter backed by the hub control plane.

    The control plane is authoritative for surface binding reads and writes. This
    adapter maintains a small local cache for degraded hub-unavailable fallback
    behavior so routing helpers can keep operating on recently observed bindings.
    """

    def __init__(
        self,
        client: HubControlPlaneClient,
        *,
        timeout_seconds: float = 30.0,
        cache_fallback_ttl_seconds: float = 300.0,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._cache_fallback_ttl_seconds = max(0.0, float(cache_fallback_ttl_seconds))
        self._bindings_by_key: dict[tuple[str, str], Any] = {}
        self._binding_cached_at_by_key: dict[tuple[str, str], float] = {}

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
        action: Callable[[HubControlPlaneClient], Coroutine[Any, Any, ResultT]],
    ) -> ResultT:
        def _invoke() -> ResultT:
            background_client = self._client
            clone = getattr(type(self._client), "clone_for_background_loop", None)
            if callable(clone) and not inspect.iscoroutinefunction(clone):
                cloned_client = clone(self._client)
                if cloned_client is not None and not inspect.isawaitable(cloned_client):
                    background_client = cloned_client

            async def _run_action() -> ResultT:
                try:
                    return await action(background_client)
                finally:
                    close = getattr(background_client, "aclose", None)
                    if callable(close) and background_client is not self._client:
                        result = close()
                        if inspect.isawaitable(result):
                            await result

            return asyncio.run(_run_action())

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

    def _forget_binding(self, *, surface_kind: str, surface_key: str) -> None:
        key = self._normalize_key(surface_kind, surface_key)
        self._bindings_by_key.pop(key, None)
        self._binding_cached_at_by_key.pop(key, None)

    def _remember(self, binding: Any) -> Any:
        if binding is None:
            return None
        key = self._normalize_key(
            getattr(binding, "surface_kind", ""),
            getattr(binding, "surface_key", ""),
        )
        if all(key):
            self._bindings_by_key[key] = binding
            self._binding_cached_at_by_key[key] = time.monotonic()
        return binding

    def _get_cached_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        include_disabled: bool = False,
    ) -> Any:
        key = self._normalize_key(surface_kind, surface_key)
        cached = self._bindings_by_key.get(key)
        if cached is None:
            return None
        cached_at = self._binding_cached_at_by_key.get(key)
        if cached_at is None:
            return None
        if (
            self._cache_fallback_ttl_seconds > 0.0
            and time.monotonic() - cached_at > self._cache_fallback_ttl_seconds
        ):
            return None
        if (
            not include_disabled
            and str(getattr(cached, "disabled_at", "") or "").strip()
        ):
            return None
        return cached

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
            action=lambda client: client.upsert_surface_binding(
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
        try:
            response = self._run(
                operation="get_surface_binding",
                action=lambda client: client.get_surface_binding(
                    SurfaceBindingLookupRequest(
                        surface_kind=surface_kind,
                        surface_key=surface_key,
                        include_disabled=include_disabled,
                    )
                ),
            )
        except HubControlPlaneError as exc:
            if exc.code != "hub_unavailable":
                raise
            cached = self._get_cached_binding(
                surface_kind=surface_kind,
                surface_key=surface_key,
                include_disabled=include_disabled,
            )
            if cached is None:
                raise
            return cached
        if response.binding is None:
            self._forget_binding(surface_kind=surface_kind, surface_key=surface_key)
            return None
        return self._remember(response.binding)

    @staticmethod
    def _matches_filter(
        binding: Any,
        *,
        thread_target_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
    ) -> bool:
        if (
            not include_disabled
            and str(getattr(binding, "disabled_at", "") or "").strip()
        ):
            return False
        if (
            thread_target_id is not None
            and str(getattr(binding, "thread_target_id", "") or "").strip()
            != str(thread_target_id).strip()
        ):
            return False
        if (
            repo_id is not None
            and str(getattr(binding, "repo_id", "") or "").strip()
            != str(repo_id).strip()
        ):
            return False
        if (
            resource_kind is not None
            and str(getattr(binding, "resource_kind", "") or "").strip()
            != str(resource_kind).strip()
        ):
            return False
        if (
            resource_id is not None
            and str(getattr(binding, "resource_id", "") or "").strip()
            != str(resource_id).strip()
        ):
            return False
        if (
            agent_id is not None
            and str(getattr(binding, "agent_id", "") or "").strip()
            != str(agent_id).strip()
        ):
            return False
        if (
            surface_kind is not None
            and str(getattr(binding, "surface_kind", "") or "").strip()
            != str(surface_kind).strip()
        ):
            return False
        return True

    def _filter_cached_bindings(
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
        filtered = []
        for binding in self._bindings_by_key.values():
            if not self._matches_filter(
                binding,
                thread_target_id=thread_target_id,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                agent_id=agent_id,
                surface_kind=surface_kind,
                include_disabled=include_disabled,
            ):
                continue
            filtered.append(binding)
            if len(filtered) >= max(1, int(limit)):
                break
        return filtered

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
        try:
            response = self._run(
                operation="list_surface_bindings",
                action=lambda client: client.list_surface_bindings(
                    SurfaceBindingListRequest(
                        thread_target_id=thread_target_id,
                        repo_id=repo_id,
                        resource_kind=resource_kind,
                        resource_id=resource_id,
                        agent_id=agent_id,
                        surface_kind=surface_kind,
                        include_disabled=include_disabled,
                        limit=max(1, int(limit)),
                    )
                ),
            )
        except HubControlPlaneError as exc:
            if exc.code != "hub_unavailable":
                raise
            return self._filter_cached_bindings(
                thread_target_id=thread_target_id,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                agent_id=agent_id,
                surface_kind=surface_kind,
                include_disabled=include_disabled,
                limit=limit,
            )
        return [self._remember(binding) for binding in response.bindings]

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
