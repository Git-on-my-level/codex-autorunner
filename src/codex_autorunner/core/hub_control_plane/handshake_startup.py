from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Tuple

from ..logging_utils import log_event
from ..orchestration import ORCHESTRATION_SCHEMA_VERSION
from .client import HubControlPlaneClient
from .errors import HubControlPlaneError
from .models import (
    HandshakeCompatibility,
    HandshakeRequest,
    evaluate_handshake_compatibility,
)


async def perform_startup_hub_handshake(
    *,
    hub_client: HubControlPlaneClient,
    log_event_name_prefix: str,
    handshake_client_name: str,
    hub_root_str: str,
    startup_monotonic: Optional[float],
    retry_window_seconds: float,
    retry_delay_seconds: float,
    retry_max_delay_seconds: float,
    client_api_version: str,
    logger: logging.Logger,
) -> Tuple[bool, Optional[HandshakeCompatibility]]:
    """Run hub handshake with startup retry/backoff for transport failures.

    Returns ``(ok, compatibility)`` where ``compatibility`` is set when a
    handshake response was received (compatible or not); ``None`` on errors
    before a successful transport.
    """
    expected_schema_generation = ORCHESTRATION_SCHEMA_VERSION
    startup_retry_deadline: Optional[float] = None
    if startup_monotonic is not None:
        startup_retry_deadline = startup_monotonic + retry_window_seconds
    delay_seconds = retry_delay_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await hub_client.handshake(
                HandshakeRequest(
                    client_name=handshake_client_name,
                    client_api_version=client_api_version,
                    expected_schema_generation=expected_schema_generation,
                )
            )
            compatibility = evaluate_handshake_compatibility(
                response,
                client_api_version=client_api_version,
                expected_schema_generation=expected_schema_generation,
            )
            if compatibility.compatible:
                log_event(
                    logger,
                    logging.INFO,
                    f"{log_event_name_prefix}.hub_control_plane.handshake_ok",
                    hub_root=hub_root_str,
                    api_version=response.api_version,
                    schema_generation=response.schema_generation,
                    expected_schema_generation=expected_schema_generation,
                )
                return True, compatibility
            log_event(
                logger,
                logging.ERROR,
                f"{log_event_name_prefix}.hub_control_plane.handshake_incompatible",
                hub_root=hub_root_str,
                reason=compatibility.reason,
                server_api_version=compatibility.server_api_version,
                client_api_version=compatibility.client_api_version,
                server_schema_generation=compatibility.server_schema_generation,
                expected_schema_generation=compatibility.expected_schema_generation,
            )
            return False, compatibility
        except HubControlPlaneError as exc:
            should_retry = (
                startup_retry_deadline is not None
                and exc.retryable
                and exc.code in {"transport_failure", "hub_unavailable"}
                and time.monotonic() < startup_retry_deadline
            )
            if should_retry:
                log_event(
                    logger,
                    logging.WARNING,
                    f"{log_event_name_prefix}.hub_control_plane.handshake_retrying",
                    hub_root=hub_root_str,
                    attempt=attempt,
                    delay_seconds=round(delay_seconds, 2),
                    error_code=exc.code,
                    message=str(exc),
                    expected_schema_generation=expected_schema_generation,
                )
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(
                    max(delay_seconds, 0.1) * 2.0,
                    retry_max_delay_seconds,
                )
                continue
            log_event(
                logger,
                logging.ERROR,
                f"{log_event_name_prefix}.hub_control_plane.handshake_failed",
                hub_root=hub_root_str,
                error_code=exc.code,
                retryable=exc.retryable,
                message=str(exc),
                expected_schema_generation=expected_schema_generation,
            )
            return False, None
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                f"{log_event_name_prefix}.hub_control_plane.handshake_unexpected_error",
                hub_root=hub_root_str,
                exc=exc,
                expected_schema_generation=expected_schema_generation,
            )
            return False, None
