from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextspaceDocCatalogEntry:
    kind: str
    path: str
    label: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.path,
            "label": self.label,
            "description": self.description,
        }


CONTEXTSPACE_DOC_CATALOG: tuple[ContextspaceDocCatalogEntry, ...] = (
    ContextspaceDocCatalogEntry(
        kind="active_context",
        path="active_context.md",
        label="Active Context",
        description="Short-lived working context for the current effort.",
    ),
    ContextspaceDocCatalogEntry(
        kind="decisions",
        path="decisions.md",
        label="Decisions",
        description="Durable architectural and product decisions.",
    ),
    ContextspaceDocCatalogEntry(
        kind="spec",
        path="spec.md",
        label="Spec",
        description="Source-of-truth requirements for ticket generation.",
    ),
)


__all__ = [
    "CONTEXTSPACE_DOC_CATALOG",
    "ContextspaceDocCatalogEntry",
]
