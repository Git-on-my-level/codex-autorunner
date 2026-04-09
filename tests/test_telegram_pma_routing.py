# ruff: noqa: F401

from tests.telegram_pma_routing_support import (
    test_message_routing_submits_thread_work_through_orchestration_ingress,
    test_pma_image_uses_hub_root,
    test_pma_managed_thread_turn_forwards_non_yolo_override,
    test_pma_managed_thread_turn_forwards_yolo_defaults,
    test_pma_media_uses_hub_root,
    test_pma_prompt_routing_preserves_native_input_items,
    test_pma_prompt_routing_uses_hub_root,
    test_pma_voice_uses_hub_root,
    test_sanitize_runtime_thread_result_error_maps_interrupted_to_surface_interrupted,
    test_sanitize_runtime_thread_result_error_maps_timeout_to_surface_timeout,
    test_sanitize_runtime_thread_result_error_preserves_sanitized_detail,
    test_telegram_media_messages_route_through_orchestration_ingress,
    test_telegram_opencode_turn_routes_through_managed_thread_without_root,
    test_telegram_text_messages_route_through_orchestration_ingress,
)
