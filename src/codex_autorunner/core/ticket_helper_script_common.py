from __future__ import annotations

from textwrap import dedent

from ..agents.registry import get_registered_agents

PORTABLE_TICKET_ID_PATTERN = r"^[A-Za-z0-9._-]{6,128}$"
PORTABLE_TICKET_ID_ERROR = (
    "frontmatter.ticket_id is required and must match [A-Za-z0-9._-]{6,128}."
)
PORTABLE_TICKET_AGENT_REQUIRED_ERROR = (
    "frontmatter.agent is required (e.g. 'codex' or 'opencode')."
)
PORTABLE_TICKET_DONE_ERROR = "frontmatter.done is required and must be a boolean."


def portable_known_agent_ids() -> tuple[str, ...]:
    return tuple(sorted({"user", *get_registered_agents().keys()}))


def portable_ticket_validation_source() -> str:
    known_agents = portable_known_agent_ids()
    return dedent(
        f"""\
        _TICKET_ID_RE = re.compile(r"{PORTABLE_TICKET_ID_PATTERN}")
        _KNOWN_AGENT_IDS = {known_agents!r}
        _IGNORED_NON_TICKET_FILENAMES = {{"AGENTS.md", "ingest_state.json"}}


        def _sanitize_ticket_id(raw: object) -> Optional[str]:
            if not isinstance(raw, str):
                return None
            cleaned = raw.strip()
            if not cleaned or not _TICKET_ID_RE.match(cleaned):
                return None
            return cleaned


        def _normalize_agent(raw: object) -> Tuple[Optional[str], Optional[str]]:
            if not isinstance(raw, str):
                return None, "{PORTABLE_TICKET_AGENT_REQUIRED_ERROR}"

            cleaned = raw.strip()
            if not cleaned:
                return None, "{PORTABLE_TICKET_AGENT_REQUIRED_ERROR}"

            normalized = cleaned.lower()
            if normalized not in _KNOWN_AGENT_IDS:
                return None, f"frontmatter.agent is invalid: Unknown agent: {{cleaned!r}}"

            return normalized, None


        def _lint_frontmatter(data: dict[str, Any]) -> List[str]:
            errors: List[str] = []

            ticket_id = _sanitize_ticket_id(data.get("ticket_id"))
            if not ticket_id:
                errors.append("{PORTABLE_TICKET_ID_ERROR}")

            _agent, agent_error = _normalize_agent(data.get("agent"))
            if agent_error:
                errors.append(agent_error)

            done = data.get("done")
            if not isinstance(done, bool):
                errors.append("{PORTABLE_TICKET_DONE_ERROR}")

            return errors
        """
    )
