import re

from codex_autorunner.core.ports import AgentBackend as CoreAgentBackend
from codex_autorunner.core.ports import AgentEvent as CoreAgentEvent
from codex_autorunner.core.ports import AgentEventType as CoreAgentEventType
from codex_autorunner.core.ports import RunEvent as CoreRunEvent
from codex_autorunner.core.ports import now_iso as core_now_iso
from codex_autorunner.integrations.agents import AgentBackend as LegacyAgentBackend
from codex_autorunner.integrations.agents import AgentEvent as LegacyAgentEvent
from codex_autorunner.integrations.agents import AgentEventType as LegacyAgentEventType
from codex_autorunner.integrations.agents import RunEvent as LegacyRunEvent
from codex_autorunner.integrations.agents.run_event import now_iso as legacy_now_iso


def test_ports_import_paths_are_compatible():
    assert CoreAgentBackend is LegacyAgentBackend
    assert CoreAgentEvent is LegacyAgentEvent
    assert CoreAgentEventType is LegacyAgentEventType
    assert CoreRunEvent == LegacyRunEvent
    assert callable(core_now_iso)
    assert callable(legacy_now_iso)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", core_now_iso())
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", legacy_now_iso())


def test_agent_event_type_values_unchanged():
    assert CoreAgentEventType.STREAM_DELTA.value == "stream_delta"
    assert CoreAgentEventType.TOOL_CALL.value == "tool_call"
    assert CoreAgentEventType.TOOL_RESULT.value == "tool_result"
    assert CoreAgentEventType.MESSAGE_COMPLETE.value == "message_complete"
    assert CoreAgentEventType.ERROR.value == "error"
    assert CoreAgentEventType.APPROVAL_REQUESTED.value == "approval_requested"
    assert CoreAgentEventType.APPROVAL_GRANTED.value == "approval_granted"
    assert CoreAgentEventType.APPROVAL_DENIED.value == "approval_denied"
    assert CoreAgentEventType.SESSION_STARTED.value == "session_started"
    assert CoreAgentEventType.SESSION_ENDED.value == "session_ended"
    assert CoreAgentEventType.SESION_STARTED.value == "session_started"
