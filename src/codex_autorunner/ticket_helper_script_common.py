from __future__ import annotations

from textwrap import dedent

from .agents.registry import get_registered_agents

PORTABLE_TICKET_ID_PATTERN = r"^[A-Za-z0-9._-]{6,128}$"
PORTABLE_TICKET_ID_ERROR = (
    "frontmatter.ticket_id must match [A-Za-z0-9._-]{6,128} when provided."
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


        def _parse_scalar(raw: str) -> object:
            value = raw.strip()
            if not value:
                return ""
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                return (
                    value[1:-1]
                    .replace("\\\\n", "\\n")
                    .replace('\\\\\\"', '"')
                    .replace("\\\\\\\\", "\\\\")
                )
            lowered = value.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if value.isdigit():
                return int(value)
            if ": " in value or value.endswith(":"):
                raise ValueError("unsupported unquoted ':' in scalar")
            return value


        def _parse_simple_yaml_mapping(text: str) -> dict[str, object]:
            data: dict[str, object] = {{}}
            lines = text.splitlines()
            idx = 0
            while idx < len(lines):
                line = lines[idx]
                if not line.strip():
                    idx += 1
                    continue
                if line[:1].isspace():
                    raise ValueError("unexpected indentation")
                if ":" not in line:
                    raise ValueError("expected 'key: value'")
                key, raw_value = line.split(":", 1)
                key = key.strip()
                if not key:
                    raise ValueError("missing mapping key")

                value = raw_value.strip()
                if value:
                    data[key] = _parse_scalar(value)
                    idx += 1
                    continue

                idx += 1
                block: list[str] = []
                while idx < len(lines):
                    child = lines[idx]
                    if not child.strip():
                        idx += 1
                        continue
                    if not child.startswith("  "):
                        break
                    block.append(child[2:])
                    idx += 1

                if not block:
                    data[key] = None
                    continue

                if all(item.lstrip().startswith("- ") for item in block):
                    values: list[object] = []
                    for item in block:
                        stripped = item.lstrip()
                        if not stripped.startswith("- "):
                            raise ValueError("mixed list indentation")
                        values.append(_parse_scalar(stripped[2:].strip()))
                    data[key] = values
                    continue

                data[key] = _parse_simple_yaml_mapping("\\n".join(block))

            return data


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

            raw_ticket_id = data.get("ticket_id")
            ticket_id = _sanitize_ticket_id(raw_ticket_id)
            if raw_ticket_id is not None and not ticket_id:
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
