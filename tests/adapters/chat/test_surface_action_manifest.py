from codex_autorunner.adapters.chat.surface_action_manifest import (
    SurfaceActionManifestContext,
    build_surface_action_manifest,
)


def _action(manifest, action_id: str):
    return next(action for action in manifest.actions if action.action_id == action_id)


def test_manifest_for_active_managed_thread_enables_interrupt() -> None:
    manifest = build_surface_action_manifest(
        SurfaceActionManifestContext(
            surface_kind="web",
            ui_kind="pma_web",
            target_kind="managed_thread",
            thread_id="thread-1",
            lifecycle_state="running",
            capabilities=frozenset({"interrupt"}),
        )
    )

    interrupt = _action(manifest, "managed_thread.interrupt")
    assert interrupt.enabled is True
    assert interrupt.command_id == "car.interrupt"
    assert interrupt.requires_confirmation is True


def test_manifest_for_idle_managed_thread_disables_interrupt() -> None:
    manifest = build_surface_action_manifest(
        SurfaceActionManifestContext(
            surface_kind="web",
            ui_kind="pma_web",
            target_kind="managed_thread",
            thread_id="thread-1",
            lifecycle_state="idle",
            capabilities=frozenset({"interrupt"}),
        )
    )

    interrupt = _action(manifest, "managed_thread.interrupt")
    assert interrupt.enabled is False
    assert interrupt.disabled_reason == "Managed thread has no active turn"


def test_manifest_for_running_ticket_flow_exposes_stop() -> None:
    manifest = build_surface_action_manifest(
        SurfaceActionManifestContext(
            surface_kind="web",
            ui_kind="pma_web",
            target_kind="ticket_flow",
            resource_kind="repo",
            resource_id="repo-1",
            run_id="run-1",
            lifecycle_state="running",
            has_run=True,
            has_open_tickets=True,
            capabilities=frozenset({"ticket_flow"}),
        )
    )

    stop = _action(manifest, "ticket_flow.stop")
    assert stop.enabled is True
    assert stop.command_id == "car.flow.stop"
    assert stop.method == "POST"
    assert stop.route == "/api/flows/run-1/stop"


def test_manifest_for_paused_ticket_flow_exposes_resume_and_restart() -> None:
    manifest = build_surface_action_manifest(
        SurfaceActionManifestContext(
            surface_kind="web",
            ui_kind="pma_web",
            target_kind="ticket_flow",
            resource_kind="repo",
            resource_id="repo-1",
            run_id="run-1",
            lifecycle_state="paused",
            archive_mode="confirm",
            has_run=True,
            capabilities=frozenset({"ticket_flow"}),
        )
    )

    assert _action(manifest, "ticket_flow.resume").enabled is True
    restart = _action(manifest, "ticket_flow.restart")
    assert restart.enabled is True
    assert restart.requires_confirmation is True


def test_manifest_disables_unsupported_capabilities() -> None:
    manifest = build_surface_action_manifest(
        SurfaceActionManifestContext(
            surface_kind="web",
            ui_kind="pma_web",
            target_kind="ticket_flow",
            resource_kind="repo",
            resource_id="repo-1",
            has_open_tickets=True,
            capabilities=frozenset(),
        )
    )

    start = _action(manifest, "ticket_flow.start")
    assert start.enabled is False
    assert start.missing_capabilities == ("ticket_flow",)
    assert (
        start.disabled_reason
        == "Cannot car.flow.start; missing capability: ticket_flow"
    )
