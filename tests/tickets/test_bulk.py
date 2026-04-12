from __future__ import annotations

from pathlib import Path

from codex_autorunner.agents.hermes_identity import CanonicalHermesIdentity
from codex_autorunner.tickets.bulk import bulk_canonicalize_hermes_agents
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter


def test_bulk_canonicalize_hermes_agents_passes_repo_root_context(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: hermes-m4-pma\ndone: false\nticket_id: tkt_bulkctx001\n---\n\nBody\n",
        encoding="utf-8",
    )

    observed: dict[str, object] = {}

    def _canonicalize(agent_id: str, profile: str | None = None, *, context=None):
        observed["agent_id"] = agent_id
        observed["profile"] = profile
        observed["context"] = context
        return CanonicalHermesIdentity(agent="hermes", profile="m4-pma")

    monkeypatch.setattr(
        "codex_autorunner.agents.hermes_identity.canonicalize_hermes_identity",
        _canonicalize,
    )

    result = bulk_canonicalize_hermes_agents(
        ticket_dir,
        None,
        repo_root=repo_root,
    )

    assert result.updated == 1
    assert observed["agent_id"] == "hermes-m4-pma"
    assert observed["context"] == repo_root

    frontmatter, _ = parse_markdown_frontmatter(ticket_path.read_text(encoding="utf-8"))
    assert frontmatter["agent"] == "hermes"
    assert frontmatter["profile"] == "m4-pma"
