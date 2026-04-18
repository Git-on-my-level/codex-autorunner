from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ...core.hub_control_plane import (
    HandshakeCompatibility,
    HttpHubControlPlaneClient,
    HubControlPlaneError,
    evaluate_handshake_compatibility,
)
from ...core.hub_control_plane.models import HandshakeRequest
from ...core.hub_control_plane.service import CONTROL_PLANE_API_VERSION
from ...core.logging_utils import log_event
from ...core.orchestration import ORCHESTRATION_SCHEMA_VERSION


@dataclass
class HubHandshakeResult:
    success: bool
    compatibility: Optional[HandshakeCompatibility]


async def perform_hub_handshake(
    hub_client: Optional[HttpHubControlPlaneClient],
    logger: logging.Logger,
    hub_root: str,
) -> HubHandshakeResult:
    expected_schema_generation = ORCHESTRATION_SCHEMA_VERSION
    if hub_client is None:
        log_event(
            logger,
            logging.ERROR,
            "discord.hub_control_plane.client_not_configured",
            hub_root=hub_root,
            expected_schema_generation=expected_schema_generation,
        )
        return HubHandshakeResult(success=False, compatibility=None)

    try:
        response = await hub_client.handshake(
            HandshakeRequest(
                client_name="discord",
                client_api_version=CONTROL_PLANE_API_VERSION,
                expected_schema_generation=expected_schema_generation,
            )
        )
        compatibility = evaluate_handshake_compatibility(
            response,
            client_api_version=CONTROL_PLANE_API_VERSION,
            expected_schema_generation=expected_schema_generation,
        )
        if compatibility.compatible:
            log_event(
                logger,
                logging.INFO,
                "discord.hub_control_plane.handshake_ok",
                hub_root=hub_root,
                api_version=response.api_version,
                schema_generation=response.schema_generation,
                expected_schema_generation=expected_schema_generation,
            )
            return HubHandshakeResult(success=True, compatibility=compatibility)
        else:
            log_event(
                logger,
                logging.ERROR,
                "discord.hub_control_plane.handshake_incompatible",
                hub_root=hub_root,
                reason=compatibility.reason,
                server_api_version=compatibility.server_api_version,
                client_api_version=compatibility.client_api_version,
                server_schema_generation=compatibility.server_schema_generation,
                expected_schema_generation=compatibility.expected_schema_generation,
            )
            return HubHandshakeResult(success=False, compatibility=compatibility)
    except HubControlPlaneError as exc:
        log_event(
            logger,
            logging.ERROR,
            "discord.hub_control_plane.handshake_failed",
            hub_root=hub_root,
            error_code=exc.code,
            retryable=exc.retryable,
            message=str(exc),
            expected_schema_generation=expected_schema_generation,
        )
        return HubHandshakeResult(success=False, compatibility=None)
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "discord.hub_control_plane.handshake_unexpected_error",
            hub_root=hub_root,
            exc=exc,
            expected_schema_generation=expected_schema_generation,
        )
        return HubHandshakeResult(success=False, compatibility=None)
