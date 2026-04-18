from __future__ import annotations

import pytest

from codex_autorunner.integrations.chat.callbacks import LogicalCallback
from codex_autorunner.integrations.telegram.chat_callbacks import (
    TelegramCallbackCodec,
    TelegramCallbackContractScenario,
    cataloged_telegram_callback_contract_scenarios,
)


@pytest.fixture
def codec() -> TelegramCallbackCodec:
    return TelegramCallbackCodec()


_SCENARIOS = cataloged_telegram_callback_contract_scenarios()


@pytest.mark.parametrize(
    "scenario",
    _SCENARIOS,
    ids=[s.label for s in _SCENARIOS],
)
@pytest.mark.anyio
async def test_cataloged_scenario_round_trips(
    codec: TelegramCallbackCodec,
    scenario: TelegramCallbackContractScenario,
) -> None:
    original = LogicalCallback(
        callback_id=scenario.callback_id,
        payload=scenario.payload,
    )
    wire = codec.encode(original)
    assert isinstance(wire, str)
    assert len(wire) > 0

    decoded = codec.decode(wire)
    assert decoded is not None
    assert decoded.callback_id == original.callback_id
    for key, expected_value in original.payload.items():
        assert key in decoded.payload, f"missing key {key!r} in decoded payload"
        assert (
            decoded.payload[key] == expected_value
        ), f"payload mismatch for {key!r}: {decoded.payload[key]!r} != {expected_value!r}"


@pytest.mark.anyio
async def test_decode_returns_none_for_empty_string(
    codec: TelegramCallbackCodec,
) -> None:
    assert codec.decode("") is None


@pytest.mark.anyio
async def test_decode_returns_none_for_none(codec: TelegramCallbackCodec) -> None:
    assert codec.decode(None) is None


@pytest.mark.anyio
async def test_decode_returns_none_for_unknown_prefix(
    codec: TelegramCallbackCodec,
) -> None:
    assert codec.decode("unknown:payload") is None


@pytest.mark.anyio
async def test_decode_returns_none_for_no_colon(
    codec: TelegramCallbackCodec,
) -> None:
    assert codec.decode("nocolon") is None


@pytest.mark.anyio
async def test_encode_raises_for_unsupported_callback_id(
    codec: TelegramCallbackCodec,
) -> None:
    with pytest.raises(ValueError, match="unsupported callback id"):
        codec.encode(LogicalCallback(callback_id="nonexistent", payload={}))


@pytest.mark.parametrize(
    "wire, expected_kind",
    [
        ("appr:approve:req-1", "approval"),
        ("qopt:0:1:req-1", "question_option"),
        ("qdone:req-1", "question_done"),
        ("qcustom:req-1", "question_custom"),
        ("qcancel:req-1", "question_cancel"),
        ("resume:thread-1", "resume"),
        ("bind:repo-1", "bind"),
        ("agent:codex", "agent"),
        ("agent_profile:default", "agent_profile"),
        ("model:gpt-5.4", "model"),
        ("effort:high", "effort"),
        ("update:web", "update"),
        ("update_confirm:yes", "update_confirm"),
        ("review_commit:abc123", "review_commit"),
        ("cancel:interrupt", "cancel"),
        ("compact:start", "compact"),
        ("page:resume:1", "page"),
        ("flow:resume:run-1", "flow"),
        ("flow_run:run-1", "flow_run"),
    ],
)
@pytest.mark.anyio
async def test_wire_parse_returns_expected_kind(
    codec: TelegramCallbackCodec,
    wire: str,
    expected_kind: str,
) -> None:
    decoded = codec.decode(wire)
    assert decoded is not None
    assert decoded.callback_id is not None


@pytest.mark.anyio
async def test_flow_callback_with_repo_id_round_trips(
    codec: TelegramCallbackCodec,
) -> None:
    from codex_autorunner.integrations.chat.callbacks import CALLBACK_FLOW

    original = LogicalCallback(
        callback_id=CALLBACK_FLOW,
        payload={"action": "resume", "run_id": "run-42", "repo_id": "repo-7"},
    )
    wire = codec.encode(original)
    decoded = codec.decode(wire)
    assert decoded is not None
    assert decoded.payload["action"] == "resume"
    assert decoded.payload["run_id"] == "run-42"
    assert decoded.payload["repo_id"] == "repo-7"


@pytest.mark.anyio
async def test_flow_callback_action_only_round_trips(
    codec: TelegramCallbackCodec,
) -> None:
    from codex_autorunner.integrations.chat.callbacks import CALLBACK_FLOW

    original = LogicalCallback(
        callback_id=CALLBACK_FLOW,
        payload={"action": "status", "run_id": None, "repo_id": None},
    )
    wire = codec.encode(original)
    decoded = codec.decode(wire)
    assert decoded is not None
    assert decoded.payload["action"] == "status"
    assert decoded.payload.get("run_id") is None
    assert decoded.payload.get("repo_id") is None


@pytest.mark.anyio
async def test_all_cataloged_scenarios_produce_valid_wire_data() -> None:
    codec = TelegramCallbackCodec()
    for scenario in _SCENARIOS:
        original = LogicalCallback(
            callback_id=scenario.callback_id,
            payload=scenario.payload,
        )
        wire = codec.encode(original)
        assert isinstance(wire, str), f"{scenario.label}: wire is not str"
        assert ":" in wire, f"{scenario.label}: wire missing colon separator"
        assert (
            len(wire.encode("utf-8")) <= 64
        ), f"{scenario.label}: wire data exceeds 64 byte limit"


def test_catalog_includes_core_callback_types() -> None:
    labels = {s.label for s in _SCENARIOS}
    for expected in (
        "approval",
        "question_option",
        "resume",
        "bind",
        "agent",
        "model",
        "effort",
        "flow",
        "flow_run",
        "compact",
        "selection_cancel",
    ):
        assert expected in labels, f"missing core scenario: {expected}"


def test_catalog_includes_control_variants() -> None:
    labels = {s.label for s in _SCENARIOS}
    for expected in (
        "flow_refresh",
        "interrupt",
        "queue_cancel",
        "queue_interrupt_send",
        "pagination",
    ):
        assert expected in labels, f"missing control variant: {expected}"
