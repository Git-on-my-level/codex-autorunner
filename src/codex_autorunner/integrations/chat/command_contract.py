"""Lightweight command contract manifest for cross-surface parity checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommandStatus = Literal["stable", "partial", "unsupported"]


@dataclass(frozen=True)
class CommandContractEntry:
    id: str
    path: tuple[str, ...]
    requires_bound_workspace: bool
    status: CommandStatus


COMMAND_CONTRACT: tuple[CommandContractEntry, ...] = (
    CommandContractEntry(
        id="car.agent",
        path=("car", "agent"),
        requires_bound_workspace=True,
        status="stable",
    ),
    CommandContractEntry(
        id="car.model",
        path=("car", "model"),
        requires_bound_workspace=True,
        status="stable",
    ),
    CommandContractEntry(
        id="car.status",
        path=("car", "status"),
        requires_bound_workspace=False,
        status="stable",
    ),
    CommandContractEntry(
        id="car.new",
        path=("car", "new"),
        requires_bound_workspace=True,
        status="stable",
    ),
    CommandContractEntry(
        id="car.update",
        path=("car", "update"),
        requires_bound_workspace=False,
        status="stable",
    ),
    CommandContractEntry(
        id="pma.on",
        path=("pma", "on"),
        requires_bound_workspace=False,
        status="stable",
    ),
    CommandContractEntry(
        id="pma.off",
        path=("pma", "off"),
        requires_bound_workspace=False,
        status="stable",
    ),
    CommandContractEntry(
        id="pma.status",
        path=("pma", "status"),
        requires_bound_workspace=False,
        status="stable",
    ),
)
