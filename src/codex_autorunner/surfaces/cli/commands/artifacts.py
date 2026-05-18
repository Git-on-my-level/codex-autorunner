from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

import typer

from ....core.artifact_delivery import (
    ACTIVE_DELIVERY_STATES,
    ArtifactDeliveryService,
    DeliveryState,
    artifact_delivery_db_path,
    serialize_delivery,
)
from ....core.artifact_filebox_storage import ArtifactFileBoxStorage
from ....core.artifact_instructions import (
    ARTIFACT_TARGET_CONVERSATION_ENV,
    ARTIFACT_TARGET_SURFACE_ENV,
    ARTIFACT_WORKSPACE_SCOPE_ENV,
    current_artifact_target_failure_message,
)
from ....core.filebox import list_regular_files, outbox_dir, outbox_pending_dir
from ....core.utils import RepoNotFoundError, find_repo_root


def _root(root: Optional[Path]) -> Path:
    if root is not None:
        return root.expanduser().resolve()
    try:
        return find_repo_root(Path.cwd())
    except RepoNotFoundError:
        return Path.cwd().resolve()


def _states(value: Optional[str]) -> tuple[DeliveryState, ...] | None:
    if value is None or not value.strip():
        return None
    states: list[DeliveryState] = []
    allowed = {
        "pending",
        "claimed",
        "sending",
        "sent",
        "failed",
        "cancelled",
    }
    for item in value.split(","):
        state = item.strip().lower()
        if not state:
            continue
        if state not in allowed:
            raise typer.BadParameter(f"unknown delivery state: {state}")
        states.append(state)  # type: ignore[arg-type]
    return tuple(states)


def _current_target() -> tuple[str, str, str | None]:
    surface = os.environ.get(ARTIFACT_TARGET_SURFACE_ENV, "").strip()
    conversation = os.environ.get(ARTIFACT_TARGET_CONVERSATION_ENV, "").strip()
    workspace = os.environ.get(ARTIFACT_WORKSPACE_SCOPE_ENV, "").strip() or None
    if not surface or not conversation:
        message = current_artifact_target_failure_message(os.environ)
        raise typer.BadParameter(message or "current artifact target is unavailable")
    return surface, conversation, workspace


def _target(
    *,
    to: str,
    surface: Optional[str],
    conversation: Optional[str],
    workspace_scope: Optional[str],
) -> tuple[str, str, str | None]:
    if to == "current":
        return _current_target()
    if to != "explicit":
        raise typer.BadParameter("--to must be current or explicit")
    if not surface or not conversation:
        raise typer.BadParameter(
            "--surface and --conversation are required with --to explicit"
        )
    return surface, conversation, workspace_scope


def _echo_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def _delivery_payload(service: ArtifactDeliveryService, delivery_id: str) -> dict:
    result = service.inspect_with_artifact(delivery_id)
    if result is None:
        raise typer.BadParameter(f"unknown delivery id: {delivery_id}")
    intent, artifact = result
    return serialize_delivery(intent, artifact=artifact)


def _print_table(rows: Iterable[dict]) -> None:
    materialized = list(rows)
    if not materialized:
        typer.echo("(none)")
        return
    for row in materialized:
        artifact = row.get("artifact") or {}
        name = artifact.get("filename") or row["artifact_id"]
        error = f" error={row['last_error']}" if row.get("last_error") else ""
        typer.echo(
            f"{row['delivery_id']}  {row['state']:9}  {row['target_surface']}  "
            f"{row['target_conversation_key']}  {name}{error}"
        )


def _legacy_pending(root: Path) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for scope, folder in (
        ("outbox", outbox_dir(root)),
        ("outbox/pending", outbox_pending_dir(root)),
    ):
        for path in list_regular_files(folder):
            findings.append(
                {
                    "kind": "legacy_filebox_pending",
                    "root": str(root),
                    "scope": scope,
                    "path": str(path),
                    "filename": path.name,
                    "message": (
                        "File is in a legacy FileBox outbox without an explicit "
                        "delivery intent."
                    ),
                }
            )
    return findings


def register_artifacts_commands(app: typer.Typer) -> None:
    @app.command("list")
    def list_deliveries(
        root: Optional[Path] = typer.Option(None, "--root", help="Repo or hub root"),
        state: Optional[str] = typer.Option(
            None, "--state", help="Comma-separated states to include"
        ),
        surface: Optional[str] = typer.Option(
            None, "--surface", help="Filter by target surface"
        ),
        conversation: Optional[str] = typer.Option(
            None, "--conversation", help="Filter by target conversation key"
        ),
        workspace_scope: Optional[str] = typer.Option(
            None, "--workspace-scope", help="Filter by workspace scope"
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """List artifact delivery records."""
        service = ArtifactDeliveryService(_root(root))
        deliveries = service.list_deliveries(
            states=_states(state),
            target_surface=surface,
            target_conversation_key=conversation,
            workspace_scope=workspace_scope,
        )
        rows = [
            serialize_delivery(
                intent,
                artifact=service.store.get_artifact(intent.artifact_id),
            )
            for intent in deliveries
        ]
        if json_output:
            _echo_json(rows)
        else:
            _print_table(rows)

    @app.command("send")
    def send(
        path: Path = typer.Argument(..., help="File to enqueue"),
        root: Optional[Path] = typer.Option(None, "--root", help="Repo or hub root"),
        to: str = typer.Option("current", "--to", help="current or explicit"),
        surface: Optional[str] = typer.Option(
            None, "--surface", help="Explicit target surface"
        ),
        conversation: Optional[str] = typer.Option(
            None, "--conversation", help="Explicit target conversation key"
        ),
        workspace_scope: Optional[str] = typer.Option(
            None, "--workspace-scope", help="Workspace scope to record"
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Queue a file for artifact delivery."""
        target_surface, target_conversation, target_workspace = _target(
            to=to,
            surface=surface,
            conversation=conversation,
            workspace_scope=workspace_scope,
        )
        repo_root = _root(root)
        storage = ArtifactFileBoxStorage(repo_root)
        service = storage.delivery
        intent = storage.enqueue_delivery_file(
            path,
            target_surface=target_surface,
            target_conversation_key=target_conversation,
            workspace_scope=target_workspace,
        )
        payload = serialize_delivery(
            intent,
            artifact=service.store.get_artifact(intent.artifact_id),
        )
        if json_output:
            _echo_json(payload)
        else:
            typer.echo(
                f"queued {intent.delivery_id} for {target_surface}:{target_conversation}"
            )

    @app.command("inspect")
    def inspect(
        delivery_id: str,
        root: Optional[Path] = typer.Option(None, "--root", help="Repo or hub root"),
    ) -> None:
        """Inspect one artifact delivery by id."""
        _echo_json(_delivery_payload(ArtifactDeliveryService(_root(root)), delivery_id))

    @app.command("import-legacy")
    def import_legacy(
        root: Optional[Path] = typer.Option(None, "--root", help="Repo or hub root"),
        to: str = typer.Option("current", "--to", help="current or explicit"),
        surface: Optional[str] = typer.Option(
            None, "--surface", help="Explicit target surface"
        ),
        conversation: Optional[str] = typer.Option(
            None, "--conversation", help="Explicit target conversation key"
        ),
        workspace_scope: Optional[str] = typer.Option(
            None, "--workspace-scope", help="Workspace scope to record"
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Import legacy FileBox outbox files into delivery records."""
        target_surface, target_conversation, target_workspace = _target(
            to=to,
            surface=surface,
            conversation=conversation,
            workspace_scope=workspace_scope,
        )
        service = ArtifactDeliveryService(_root(root))
        intents = service.import_legacy_outbox(
            target_surface=target_surface,
            target_conversation_key=target_conversation,
            workspace_scope=target_workspace,
        )
        rows = [
            serialize_delivery(
                intent,
                artifact=service.store.get_artifact(intent.artifact_id),
            )
            for intent in intents
        ]
        if json_output:
            _echo_json(rows)
        else:
            typer.echo(f"imported {len(rows)} legacy FileBox file(s)")

    @app.command("retry")
    def retry(
        delivery_id: str,
        root: Optional[Path] = typer.Option(None, "--root", help="Repo or hub root"),
        next_attempt_at: Optional[str] = typer.Option(
            None, "--next-at", help="Optional ISO timestamp for the next attempt"
        ),
    ) -> None:
        """Retry a failed artifact delivery."""
        service = ArtifactDeliveryService(_root(root))
        intent = service.retry(delivery_id, next_attempt_at=next_attempt_at)
        _echo_json(
            serialize_delivery(
                intent,
                artifact=service.store.get_artifact(intent.artifact_id),
            )
        )

    @app.command("cancel")
    def cancel(
        delivery_id: str,
        root: Optional[Path] = typer.Option(None, "--root", help="Repo or hub root"),
    ) -> None:
        """Cancel an active artifact delivery."""
        service = ArtifactDeliveryService(_root(root))
        intent = service.cancel(delivery_id)
        _echo_json(
            serialize_delivery(
                intent,
                artifact=service.store.get_artifact(intent.artifact_id),
            )
        )

    @app.command("diagnose")
    def diagnose(
        root: Optional[Path] = typer.Option(None, "--root", help="Current repo root"),
        sibling_root: list[Path] = typer.Option(
            [], "--sibling-root", help="Additional repo/hub root to scan"
        ),
        surface: Optional[str] = typer.Option(
            None, "--surface", help="Current target surface for mismatch checks"
        ),
        conversation: Optional[str] = typer.Option(
            None, "--conversation", help="Current conversation key for mismatch checks"
        ),
    ) -> None:
        """Find stranded or mismatched artifact deliveries."""
        current_root = _root(root)
        roots = [current_root, *(p.expanduser().resolve() for p in sibling_root)]
        findings: list[dict[str, object]] = []
        for candidate in roots:
            findings.extend(_legacy_pending(candidate))
            if not artifact_delivery_db_path(candidate).exists():
                continue
            service = ArtifactDeliveryService(candidate)
            for intent in service.list_deliveries(states=ACTIVE_DELIVERY_STATES):
                if surface and intent.target_surface != surface:
                    findings.append(
                        {
                            "kind": "target_mismatch",
                            "root": str(candidate),
                            "delivery_id": intent.delivery_id,
                            "message": "Delivery is not targeted to the current surface.",
                        }
                    )
                    continue
                if conversation and intent.target_conversation_key != conversation:
                    findings.append(
                        {
                            "kind": "target_mismatch",
                            "root": str(candidate),
                            "delivery_id": intent.delivery_id,
                            "message": (
                                "Delivery is not targeted to the current conversation."
                            ),
                        }
                    )
        _echo_json({"findings": findings})
