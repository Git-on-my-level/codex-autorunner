from __future__ import annotations

from codex_autorunner.core.car_context import (
    build_car_context_bundle,
    build_car_context_capsule,
)
from codex_autorunner.core.context_capsules import (
    ContextCapsule,
    ContextCapsuleExpiry,
    ContextCapsuleLedgerKey,
    ContextCapsuleRenderDecision,
    ContextCapsuleScope,
    ContextCapsuleVisibility,
    capsule_payload_digest,
    plan_capsule_render,
)


def _capsule(
    *,
    source_digest: str = "source-1",
    expiry: ContextCapsuleExpiry = ContextCapsuleExpiry.WHEN_SOURCE_CHANGES,
    payload: dict[str, object] | None = None,
) -> ContextCapsule:
    return ContextCapsule(
        capsule_id="car.repo_basics",
        version=1,
        scope=ContextCapsuleScope.THREAD,
        visibility=ContextCapsuleVisibility.MODEL_ONLY,
        source_digest=source_digest,
        expiry=expiry,
        reason="test",
        payload=payload or {"b": 2, "a": 1},
    )


def _key(capsule: ContextCapsule) -> ContextCapsuleLedgerKey:
    return ContextCapsuleLedgerKey.from_capsule(
        capsule,
        surface_kind="discord",
        surface_key="channel-1/thread-1",
        managed_thread_id="managed-1",
        backend_thread_id="backend-1",
        scope_id="managed-1",
    )


def test_capsule_identity_uses_canonical_scope_and_boundaries() -> None:
    capsule = _capsule()
    first = _key(capsule)
    second = ContextCapsuleLedgerKey.from_capsule(
        capsule,
        surface_kind="discord",
        surface_key="channel-1/thread-1",
        managed_thread_id="managed-1",
        backend_thread_id="backend-2",
        scope_id="managed-1",
    )

    assert first.identity_tuple() == (
        "discord",
        "channel-1/thread-1",
        "managed-1",
        "backend-1",
        "thread",
        "managed-1",
        "car.repo_basics",
        "1",
    )
    assert first.stable_id() != second.stable_id()


def test_capsule_payload_digest_is_stable_before_transport_rendering() -> None:
    first = _capsule(payload={"b": 2, "a": 1})
    second = _capsule(payload={"a": 1, "b": 2})

    assert capsule_payload_digest(first) == capsule_payload_digest(second)


def test_capsule_render_plan_handles_new_changed_expired_and_force_refresh() -> None:
    capsule = _capsule()
    key = _key(capsule)
    first = plan_capsule_render(capsule, key, previous=None)
    assert first.decision is ContextCapsuleRenderDecision.NEW
    assert first.should_render is True

    duplicate = plan_capsule_render(
        capsule,
        key,
        previous=first_observation(first),
    )
    assert duplicate.decision is ContextCapsuleRenderDecision.SKIP_DUPLICATE
    assert duplicate.should_render is False

    changed = plan_capsule_render(
        _capsule(source_digest="source-2"),
        key,
        previous=first_observation(first),
    )
    assert changed.decision is ContextCapsuleRenderDecision.CHANGED

    expired = plan_capsule_render(
        _capsule(expiry=ContextCapsuleExpiry.EVERY_TURN),
        key,
        previous=first_observation(first),
    )
    assert expired.decision is ContextCapsuleRenderDecision.EXPIRED

    forced = plan_capsule_render(
        capsule, key, previous=first_observation(first), force_refresh=True
    )
    assert forced.decision is ContextCapsuleRenderDecision.FORCE_REFRESHED


def test_turn_scoped_capsules_cannot_seed_durable_user_fields() -> None:
    capsule = ContextCapsule(
        capsule_id="artifact_delivery.current_turn",
        version=1,
        scope=ContextCapsuleScope.TURN,
        visibility=ContextCapsuleVisibility.MODEL_ONLY,
        source_digest="source",
        expiry=ContextCapsuleExpiry.TURN_SCOPED,
        reason="turn_attachment",
        payload={"text": "temporary"},
    )
    plan = plan_capsule_render(capsule, _key(capsule), previous=None)

    assert capsule.can_seed_durable_user_fields is False
    assert plan.can_seed_durable_user_fields is False


def test_car_awareness_has_capsule_migration_path() -> None:
    bundle = build_car_context_bundle(
        "car_ambient",
        prompt_text="Please handle this ticket",
        initiated_by_ticket_flow=True,
    )
    capsule = build_car_context_capsule(bundle)

    assert capsule is not None
    assert capsule.capsule_id == "car.repo_awareness"
    assert capsule.visibility is ContextCapsuleVisibility.MODEL_ONLY
    assert capsule.payload["initiated_by_ticket_flow"] is True
    assert "Codex Autorunner" in str(capsule.payload["text"])


def first_observation(plan):
    from codex_autorunner.core.context_capsules import ContextCapsuleLedgerObservation

    return ContextCapsuleLedgerObservation(
        key=plan.key,
        payload_digest=plan.payload_digest,
        source_digest=plan.capsule.source_digest,
        expiry=plan.capsule.expiry,
        visibility=plan.capsule.visibility,
        reason=plan.capsule.reason,
    )
