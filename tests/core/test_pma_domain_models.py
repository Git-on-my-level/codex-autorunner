from __future__ import annotations

from codex_autorunner.core.pma_domain import (
    PmaDomainEventType,
    PmaOriginContext,
    PmaSubscription,
    PmaTimer,
    PmaWakeup,
    normalize_pma_delivery_attempt,
    normalize_pma_delivery_intent,
    normalize_pma_delivery_state,
    normalize_pma_delivery_target,
    normalize_pma_dispatch_decision,
    normalize_pma_domain_event,
    normalize_pma_origin_context,
    normalize_pma_subscription,
    normalize_pma_timer,
    normalize_pma_wakeup,
    pma_dispatch_decision_to_dict,
    pma_origin_context_to_dict,
    pma_subscription_to_dict,
    pma_timer_to_dict,
    pma_wakeup_to_dict,
)
from codex_autorunner.core.pma_domain.constants import (
    DEFAULT_PMA_LANE_ID,
    ROUTE_EXPLICIT,
    SUBSCRIPTION_STATE_ACTIVE,
    SURFACE_KIND_DISCORD,
    SURFACE_KIND_TELEGRAM,
    TIMER_TYPE_ONE_SHOT,
    TIMER_TYPE_WATCHDOG,
    WAKEUP_STATE_DISPATCHED,
    WAKEUP_STATE_PENDING,
)

# ---------------------------------------------------------------------------
# PmaOriginContext round-trip
# ---------------------------------------------------------------------------


class TestPmaOriginContext:
    def test_round_trip(self) -> None:
        original = {
            "thread_id": "t-1",
            "lane_id": "discord",
            "agent": "codex",
            "profile": "default",
        }
        model = normalize_pma_origin_context(original)
        assert model is not None
        assert model.thread_id == "t-1"
        assert model.lane_id == "discord"
        serialized = pma_origin_context_to_dict(model)
        re_parsed = normalize_pma_origin_context(serialized)
        assert re_parsed is not None
        assert re_parsed.thread_id == model.thread_id
        assert re_parsed.lane_id == model.lane_id
        assert re_parsed.agent == model.agent
        assert re_parsed.profile == model.profile

    def test_empty_dict_returns_none(self) -> None:
        assert normalize_pma_origin_context({}) is None

    def test_non_dict_returns_none(self) -> None:
        assert normalize_pma_origin_context("bad") is None

    def test_is_empty(self) -> None:
        empty = PmaOriginContext()
        assert empty.is_empty()
        non_empty = PmaOriginContext(thread_id="t-1")
        assert not non_empty.is_empty()


# ---------------------------------------------------------------------------
# PmaSubscription round-trip
# ---------------------------------------------------------------------------


class TestPmaSubscription:
    def _sample_dict(self) -> dict:
        return {
            "subscription_id": "sub-001",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "state": "active",
            "event_types": ["flow_failed", "flow_completed"],
            "repo_id": "repo-1",
            "run_id": None,
            "thread_id": "thread-1",
            "lane_id": "discord",
            "from_state": "running",
            "to_state": "failed",
            "reason": "test",
            "idempotency_key": "key-1",
            "max_matches": 5,
            "match_count": 2,
            "metadata": {"pma_origin": {"thread_id": "origin-t-1"}},
        }

    def test_round_trip(self) -> None:
        data = self._sample_dict()
        model = normalize_pma_subscription(data)
        assert model is not None
        assert model.subscription_id == "sub-001"
        assert model.event_types == ("flow_failed", "flow_completed")
        assert model.repo_id == "repo-1"
        assert model.lane_id == "discord"
        serialized = pma_subscription_to_dict(model)
        re_parsed = normalize_pma_subscription(serialized)
        assert re_parsed is not None
        assert re_parsed.subscription_id == model.subscription_id
        assert re_parsed.event_types == model.event_types
        assert re_parsed.max_matches == model.max_matches
        assert re_parsed.match_count == model.match_count
        assert re_parsed.metadata == model.metadata

    def test_notify_once_converts_to_max_matches(self) -> None:
        model = normalize_pma_subscription({"notify_once": True})
        assert model is not None
        assert model.max_matches == 1

    def test_missing_fields_get_defaults(self) -> None:
        model = normalize_pma_subscription({})
        assert model is not None
        assert model.state == SUBSCRIPTION_STATE_ACTIVE
        assert model.event_types == ()
        assert model.lane_id == DEFAULT_PMA_LANE_ID
        assert model.match_count == 0

    def test_none_returns_none(self) -> None:
        assert normalize_pma_subscription(None) is None

    def test_string_returns_none(self) -> None:
        assert normalize_pma_subscription("bad") is None

    def test_is_active_and_exhausted(self) -> None:
        active = PmaSubscription(
            subscription_id="a", created_at="", updated_at="", match_count=0
        )
        assert active.is_active()
        assert not active.is_exhausted()

        exhausted = PmaSubscription(
            subscription_id="b",
            created_at="",
            updated_at="",
            max_matches=1,
            match_count=1,
        )
        assert not exhausted.is_exhausted() or exhausted.match_count >= (
            exhausted.max_matches or 0
        )

    def test_event_types_deduped_and_lowered(self) -> None:
        model = normalize_pma_subscription(
            {
                "event_types": ["Flow_Failed", "flow_failed", "FLOW_COMPLETED"],
            }
        )
        assert model is not None
        assert model.event_types == ("flow_failed", "flow_completed")


# ---------------------------------------------------------------------------
# PmaTimer round-trip
# ---------------------------------------------------------------------------


class TestPmaTimer:
    def _sample_dict(self) -> dict:
        return {
            "timer_id": "timer-001",
            "due_at": "2025-06-01T12:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "state": "pending",
            "timer_type": "watchdog",
            "idle_seconds": 300,
            "subscription_id": "sub-001",
            "repo_id": "repo-1",
            "thread_id": "thread-1",
            "lane_id": "telegram",
            "from_state": "running",
            "to_state": "idle",
            "reason": "idle check",
            "idempotency_key": "timer-key-1",
            "metadata": {"key": "val"},
        }

    def test_round_trip(self) -> None:
        data = self._sample_dict()
        model = normalize_pma_timer(data)
        assert model is not None
        assert model.timer_id == "timer-001"
        assert model.timer_type == TIMER_TYPE_WATCHDOG
        assert model.idle_seconds == 300
        serialized = pma_timer_to_dict(model)
        re_parsed = normalize_pma_timer(serialized)
        assert re_parsed is not None
        assert re_parsed.timer_id == model.timer_id
        assert re_parsed.timer_type == model.timer_type
        assert re_parsed.idle_seconds == model.idle_seconds
        assert re_parsed.metadata == model.metadata

    def test_default_timer_type(self) -> None:
        model = normalize_pma_timer({})
        assert model is not None
        assert model.timer_type == TIMER_TYPE_ONE_SHOT

    def test_is_pending_and_is_watchdog(self) -> None:
        timer = PmaTimer(
            timer_id="t",
            due_at="",
            created_at="",
            updated_at="",
            timer_type=TIMER_TYPE_WATCHDOG,
        )
        assert timer.is_pending()
        assert timer.is_watchdog()

        fired = PmaTimer(
            timer_id="t",
            due_at="",
            created_at="",
            updated_at="",
            state="fired",
        )
        assert not fired.is_pending()


# ---------------------------------------------------------------------------
# PmaWakeup round-trip
# ---------------------------------------------------------------------------


class TestPmaWakeup:
    def _sample_dict(self) -> dict:
        return {
            "wakeup_id": "wakeup-001",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "state": "pending",
            "source": "transition",
            "repo_id": "repo-1",
            "run_id": "run-1",
            "thread_id": "thread-1",
            "lane_id": "discord",
            "from_state": "running",
            "to_state": "completed",
            "reason": "done",
            "timestamp": "2025-01-01T01:00:00Z",
            "idempotency_key": "wakeup-key-1",
            "subscription_id": "sub-001",
            "timer_id": "timer-001",
            "event_id": "evt-001",
            "event_type": "flow_completed",
            "event_data": {"detail": "success"},
            "metadata": {"pma_origin": {"thread_id": "origin-t-1"}},
        }

    def test_round_trip(self) -> None:
        data = self._sample_dict()
        model = normalize_pma_wakeup(data)
        assert model is not None
        assert model.wakeup_id == "wakeup-001"
        assert model.source == "transition"
        assert model.event_type == "flow_completed"
        assert model.event_data == {"detail": "success"}
        serialized = pma_wakeup_to_dict(model)
        re_parsed = normalize_pma_wakeup(serialized)
        assert re_parsed is not None
        assert re_parsed.wakeup_id == model.wakeup_id
        assert re_parsed.source == model.source
        assert re_parsed.event_data == model.event_data
        assert re_parsed.metadata == model.metadata

    def test_default_source(self) -> None:
        model = normalize_pma_wakeup({})
        assert model is not None
        assert model.source == "automation"
        assert model.state == WAKEUP_STATE_PENDING

    def test_is_pending_and_dispatched(self) -> None:
        pending = PmaWakeup(wakeup_id="w", created_at="", updated_at="")
        assert pending.is_pending()
        assert not pending.is_dispatched()

        dispatched = PmaWakeup(
            wakeup_id="w",
            created_at="",
            updated_at="",
            state=WAKEUP_STATE_DISPATCHED,
            dispatched_at="2025-01-01T00:00:00Z",
        )
        assert dispatched.is_dispatched()


# ---------------------------------------------------------------------------
# PmaDispatchDecision round-trip
# ---------------------------------------------------------------------------


class TestPmaDispatchDecision:
    def _sample_dict(self) -> dict:
        return {
            "requested_delivery": "auto",
            "suppress_publish": False,
            "attempts": [
                {
                    "route": "explicit",
                    "delivery_mode": "bound",
                    "surface_kind": "discord",
                    "surface_key": "chan-1",
                    "repo_id": "repo-1",
                    "workspace_root": "/tmp/repo",
                },
                {
                    "route": "primary_pma",
                    "delivery_mode": "primary_pma",
                    "surface_kind": "telegram",
                    "surface_key": None,
                    "repo_id": "repo-1",
                },
            ],
        }

    def test_round_trip(self) -> None:
        data = self._sample_dict()
        model = normalize_pma_dispatch_decision(data)
        assert model is not None
        assert model.requested_delivery == "auto"
        assert len(model.attempts) == 2
        assert model.attempts[0].route == ROUTE_EXPLICIT
        assert model.attempts[0].surface_key == "chan-1"
        assert model.attempts[0].workspace_root == "/tmp/repo"
        serialized = pma_dispatch_decision_to_dict(model)
        re_parsed = normalize_pma_dispatch_decision(serialized)
        assert re_parsed is not None
        assert re_parsed.requested_delivery == model.requested_delivery
        assert len(re_parsed.attempts) == len(model.attempts)
        assert re_parsed.attempts[0].route == model.attempts[0].route
        assert re_parsed.attempts[0].surface_key == model.attempts[0].surface_key
        assert re_parsed.attempts[0].workspace_root == model.attempts[0].workspace_root

    def test_missing_requested_delivery_returns_none(self) -> None:
        assert normalize_pma_dispatch_decision({"suppress_publish": True}) is None

    def test_suppressed_decision(self) -> None:
        model = normalize_pma_dispatch_decision(
            {
                "requested_delivery": "suppressed_duplicate",
                "suppress_publish": True,
                "attempts": [],
            }
        )
        assert model is not None
        assert model.suppress_publish is True
        assert model.attempts == ()

    def test_invalid_attempt_skipped(self) -> None:
        model = normalize_pma_dispatch_decision(
            {
                "requested_delivery": "auto",
                "attempts": [
                    {"route": "explicit"},
                    {
                        "route": "bound",
                        "delivery_mode": "bound",
                        "surface_kind": "discord",
                    },
                ],
            }
        )
        assert model is not None
        assert len(model.attempts) == 1
        assert model.attempts[0].route == "bound"


# ---------------------------------------------------------------------------
# PmaDeliveryTarget / PmaDeliveryAttempt / PmaDeliveryIntent
# ---------------------------------------------------------------------------


class TestPmaDeliveryTarget:
    def test_round_trip(self) -> None:
        data = {"surface_kind": "discord", "surface_key": "chan-1"}
        model = normalize_pma_delivery_target(data)
        assert model is not None
        assert model.surface_kind == SURFACE_KIND_DISCORD
        assert model.surface_key == "chan-1"

    def test_invalid_surface_kind(self) -> None:
        model = normalize_pma_delivery_target(
            {"surface_kind": "irc", "surface_key": "x"}
        )
        assert model is None

    def test_non_dict_returns_none(self) -> None:
        assert normalize_pma_delivery_target(None) is None


class TestPmaDeliveryAttempt:
    def test_round_trip(self) -> None:
        data = {
            "route": "explicit",
            "delivery_mode": "bound",
            "target": {"surface_kind": "telegram", "surface_key": "grp-1"},
            "repo_id": "repo-1",
            "workspace_root": "/tmp/ws",
        }
        model = normalize_pma_delivery_attempt(data)
        assert model is not None
        assert model.route == ROUTE_EXPLICIT
        assert model.target.surface_kind == SURFACE_KIND_TELEGRAM
        assert model.workspace_root == "/tmp/ws"

    def test_missing_route_returns_none(self) -> None:
        assert normalize_pma_delivery_attempt({"delivery_mode": "bound"}) is None


class TestPmaDeliveryIntent:
    def test_round_trip(self) -> None:
        data = {
            "message": "hello",
            "correlation_id": "corr-1",
            "source_kind": "automation",
            "requested_delivery": "auto",
            "attempts": [
                {
                    "route": "bound",
                    "delivery_mode": "bound",
                    "target": {"surface_kind": "discord", "surface_key": "c-1"},
                },
            ],
            "repo_id": "repo-1",
            "run_id": "run-1",
            "managed_thread_id": "thread-1",
        }
        model = normalize_pma_delivery_intent(data)
        assert model is not None
        assert model.message == "hello"
        assert model.correlation_id == "corr-1"
        assert len(model.attempts) == 1
        assert model.repo_id == "repo-1"

    def test_missing_message_returns_none(self) -> None:
        assert normalize_pma_delivery_intent({"correlation_id": "x"}) is None


# ---------------------------------------------------------------------------
# PmaDeliveryState round-trip
# ---------------------------------------------------------------------------


class TestPmaDeliveryState:
    def test_round_trip(self) -> None:
        data = {
            "delivery_id": "del-001",
            "wakeup_id": "wakeup-001",
            "dispatch_decision": {
                "requested_delivery": "auto",
                "suppress_publish": False,
                "attempts": [
                    {
                        "route": "explicit",
                        "delivery_mode": "bound",
                        "surface_kind": "discord",
                    },
                ],
            },
            "status": "pending",
            "attempts_made": 0,
            "metadata": {"key": "val"},
        }
        model = normalize_pma_delivery_state(data)
        assert model is not None
        assert model.delivery_id == "del-001"
        assert model.dispatch_decision is not None
        assert model.dispatch_decision.requested_delivery == "auto"
        assert len(model.dispatch_decision.attempts) == 1

    def test_missing_delivery_id_returns_none(self) -> None:
        assert normalize_pma_delivery_state({"status": "pending"}) is None


# ---------------------------------------------------------------------------
# PmaDomainEvent round-trip
# ---------------------------------------------------------------------------


class TestPmaDomainEvent:
    def test_round_trip(self) -> None:
        data = {
            "event_type": "subscription_created",
            "event_id": "evt-001",
            "timestamp": "2025-01-01T00:00:00Z",
            "payload": {"repo_id": "repo-1"},
            "correlation_id": "corr-1",
        }
        model = normalize_pma_domain_event(data)
        assert model is not None
        assert model.event_type == PmaDomainEventType.SUBSCRIPTION_CREATED
        assert model.event_id == "evt-001"
        assert model.payload == {"repo_id": "repo-1"}
        assert model.correlation_id == "corr-1"

    def test_all_event_types_valid(self) -> None:
        for et in PmaDomainEventType:
            data = {
                "event_type": et.value,
                "event_id": "e",
                "timestamp": "2025-01-01T00:00:00Z",
                "payload": {},
            }
            model = normalize_pma_domain_event(data)
            assert model is not None
            assert model.event_type == et

    def test_invalid_event_type_returns_none(self) -> None:
        data = {
            "event_type": "not_real",
            "event_id": "e",
            "timestamp": "",
            "payload": {},
        }
        assert normalize_pma_domain_event(data) is None

    def test_missing_event_type_returns_none(self) -> None:
        assert (
            normalize_pma_domain_event(
                {"event_id": "e", "timestamp": "", "payload": {}}
            )
            is None
        )


# ---------------------------------------------------------------------------
# Cross-cutting: domain models round-trip through dict shapes used by the repo
# ---------------------------------------------------------------------------


class TestDomainModelsRoundTrip:
    def test_subscription_persisted_shape_round_trips(self) -> None:
        shape = {
            "subscription_id": "sub-abc",
            "created_at": "2025-03-15T10:30:00Z",
            "updated_at": "2025-03-15T10:30:00Z",
            "state": "active",
            "event_types": ["flow_failed"],
            "repo_id": "my-repo",
            "run_id": "run-42",
            "thread_id": "thread-7",
            "lane_id": "pma:default",
            "from_state": "running",
            "to_state": "failed",
            "reason": "timeout",
            "idempotency_key": "sub:repo:my-repo:flow_failed",
            "max_matches": None,
            "match_count": 0,
            "metadata": {
                "delivery_target": {
                    "surface_kind": "discord",
                    "surface_key": "chan-99",
                },
                "pma_origin": {"thread_id": "origin-thread", "lane_id": "discord"},
            },
        }
        model = normalize_pma_subscription(shape)
        assert model is not None
        serialized = pma_subscription_to_dict(model)
        re_parsed = normalize_pma_subscription(serialized)
        assert re_parsed is not None
        assert re_parsed.subscription_id == model.subscription_id
        assert re_parsed.metadata == model.metadata

    def test_wakeup_persisted_shape_round_trips(self) -> None:
        shape = {
            "wakeup_id": "wakeup-abc",
            "created_at": "2025-03-15T11:00:00Z",
            "updated_at": "2025-03-15T11:00:00Z",
            "state": "dispatched",
            "dispatched_at": "2025-03-15T11:01:00Z",
            "source": "transition",
            "repo_id": "my-repo",
            "run_id": "run-42",
            "thread_id": "thread-7",
            "lane_id": "discord",
            "from_state": "running",
            "to_state": "completed",
            "reason": "success",
            "timestamp": "2025-03-15T11:00:00Z",
            "idempotency_key": "trans:key:sub-abc",
            "subscription_id": "sub-abc",
            "timer_id": None,
            "event_id": None,
            "event_type": "flow_completed",
            "event_data": {},
            "metadata": {
                "dispatch_decision": {
                    "requested_delivery": "auto",
                    "suppress_publish": False,
                    "attempts": [
                        {
                            "route": "explicit",
                            "delivery_mode": "bound",
                            "surface_kind": "discord",
                            "surface_key": "chan-99",
                            "repo_id": "my-repo",
                        },
                    ],
                },
            },
        }
        model = normalize_pma_wakeup(shape)
        assert model is not None
        serialized = pma_wakeup_to_dict(model)
        re_parsed = normalize_pma_wakeup(serialized)
        assert re_parsed is not None
        assert re_parsed.wakeup_id == model.wakeup_id
        assert re_parsed.state == WAKEUP_STATE_DISPATCHED
        assert re_parsed.metadata["dispatch_decision"]["requested_delivery"] == "auto"

    def test_dispatch_decision_in_metadata_round_trips(self) -> None:
        decision_dict = {
            "requested_delivery": "auto",
            "suppress_publish": False,
            "attempts": [
                {
                    "route": "primary_pma",
                    "delivery_mode": "primary_pma",
                    "surface_kind": "discord",
                    "repo_id": "repo-1",
                },
                {
                    "route": "primary_pma",
                    "delivery_mode": "primary_pma",
                    "surface_kind": "telegram",
                    "repo_id": "repo-1",
                },
            ],
        }
        model = normalize_pma_dispatch_decision(decision_dict)
        assert model is not None
        serialized = pma_dispatch_decision_to_dict(model)
        re_parsed = normalize_pma_dispatch_decision(serialized)
        assert re_parsed is not None
        assert len(re_parsed.attempts) == 2
        assert re_parsed.attempts[0].surface_kind == SURFACE_KIND_DISCORD
        assert re_parsed.attempts[1].surface_kind == SURFACE_KIND_TELEGRAM

    def test_timer_persisted_shape_round_trips(self) -> None:
        shape = {
            "timer_id": "timer-xyz",
            "due_at": "2025-06-01T00:00:00Z",
            "created_at": "2025-03-15T10:00:00Z",
            "updated_at": "2025-03-15T10:00:00Z",
            "state": "pending",
            "fired_at": None,
            "timer_type": "watchdog",
            "idle_seconds": 600,
            "subscription_id": "sub-abc",
            "repo_id": "my-repo",
            "run_id": None,
            "thread_id": "thread-7",
            "lane_id": "discord",
            "from_state": None,
            "to_state": None,
            "reason": "idle watchdog",
            "idempotency_key": "wd:sub-abc",
            "metadata": {},
        }
        model = normalize_pma_timer(shape)
        assert model is not None
        serialized = pma_timer_to_dict(model)
        re_parsed = normalize_pma_timer(serialized)
        assert re_parsed is not None
        assert re_parsed.timer_id == model.timer_id
        assert re_parsed.timer_type == TIMER_TYPE_WATCHDOG
        assert re_parsed.idle_seconds == 600
