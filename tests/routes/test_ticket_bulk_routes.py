from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_flow_app

from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter


def test_bulk_set_agent_rejects_unknown_keys(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nticket_id: tkt_bulk001\nagent: codex\ndone: false\ntitle: One\n---\n\nBody 1\n",
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        "---\nticket_id: tkt_bulk002\nagent: codex\ndone: false\ntitle: Two\n---\n\nBody 2\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/api/flows/ticket_flow/tickets/bulk-set-agent",
            json={"agent": "opencode", "rangee": "2-2"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "rangee" for item in detail)


def test_bulk_set_agent_preserves_existing_profile_when_profile_omitted(
    tmp_path, monkeypatch
):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\n"
        "ticket_id: tkt_bulkprofile001\n"
        "agent: hermes\n"
        "profile: m4-pma\n"
        "done: false\n"
        "title: One\n"
        "---\n\n"
        "Body 1\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/api/flows/ticket_flow/tickets/bulk-set-agent",
            json={"agent": "codex"},
        )

    assert response.status_code == 200
    frontmatter, _body = parse_markdown_frontmatter(
        ticket_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "codex"
    assert frontmatter["profile"] == "m4-pma"


def test_bulk_set_agent_can_clear_profile_explicitly(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\n"
        "ticket_id: tkt_bulkprofileclear001\n"
        "agent: hermes\n"
        "profile: m4-pma\n"
        "done: false\n"
        "title: One\n"
        "---\n\n"
        "Body 1\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/api/flows/ticket_flow/tickets/bulk-set-agent",
            json={"agent": "hermes", "profile": None},
        )

    assert response.status_code == 200
    frontmatter, _body = parse_markdown_frontmatter(
        ticket_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "hermes"
    assert "profile" not in frontmatter


def test_bulk_set_agent_canonicalizes_hermes_alias_input(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\n"
        "ticket_id: tkt_bulkprofilealias001\n"
        "agent: codex\n"
        "done: false\n"
        "title: One\n"
        "---\n\n"
        "Body 1\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/api/flows/ticket_flow/tickets/bulk-set-agent",
            json={"agent": "hermes-m4-pma"},
        )

    assert response.status_code == 200
    frontmatter, _body = parse_markdown_frontmatter(
        ticket_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "hermes"
    assert frontmatter["profile"] == "m4-pma"


def test_bulk_clear_model_rejects_unknown_keys(tmp_path, monkeypatch):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nticket_id: tkt_bulkclear001\nagent: codex\nmodel: gpt-5.4\nreasoning: high\ndone: false\ntitle: One\n---\n\nBody 1\n",
        encoding="utf-8",
    )

    app = build_flow_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/api/flows/ticket_flow/tickets/bulk-clear-model",
            json={"rangee": "1-1"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"][-1] == "rangee" for item in detail)
