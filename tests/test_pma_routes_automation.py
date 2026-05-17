# ruff: noqa: F401

import pytest

from tests.pma_support.automation import (
    test_pma_automation_cancel_subscription_can_disable_unified_only_rule,
    test_pma_automation_cancel_timer_can_cancel_unified_only_schedule,
    test_pma_automation_list_endpoints_include_unified_read_model,
    test_pma_automation_real_create_routes_write_unified_records_only,
    test_pma_automation_subscription_alias_endpoint_supports_kwargs_only_store,
    test_pma_automation_subscription_create_normalizes_event_type_aliases,
    test_pma_automation_subscription_endpoints,
    test_pma_automation_timer_alias_endpoint_supports_fallback_method_signatures,
    test_pma_automation_timer_endpoints,
    test_pma_automation_timer_rejects_invalid_due_at,
    test_pma_automation_timer_rejects_unknown_subscription_id,
    test_pma_automation_watchdog_timer_create,
    test_pma_orchestration_service_integration_for_thread_operations,
)

pytestmark = pytest.mark.slow
