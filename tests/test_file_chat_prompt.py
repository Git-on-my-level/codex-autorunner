from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.utils import atomic_write
from codex_autorunner.surfaces.web.routes import file_chat as file_chat_routes


def test_file_chat_prompt_has_car_and_file_content(tmp_path: Path) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    content = "---\nagent: codex\ndone: false\n---\n" "Body\n"
    ticket_path.write_text(content, encoding="utf-8")

    target = file_chat_routes._parse_target(repo_root, "ticket:1")
    prompt = file_chat_routes._build_file_chat_prompt(
        target=target, message="update the ticket", before=content
    )

    assert "<injected context>" in prompt
    assert "</injected context>" in prompt
    assert "<FILE_CONTENT>" in prompt
    assert "</FILE_CONTENT>" in prompt
    assert "<file_role_context>" in prompt
    assert "</file_role_context>" in prompt


def test_ticket_target_chat_scope_changes_with_instance_token(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text("---\nagent: codex\ndone: false\n---\n", encoding="utf-8")

    monkeypatch.setattr(
        file_chat_routes, "ticket_state_key", lambda idx, path: f"ticket_{idx:03d}_v1"
    )
    monkeypatch.setattr(
        file_chat_routes, "ticket_chat_scope", lambda idx, path: f"ticket:{idx}:v1"
    )
    first = file_chat_routes._parse_target(repo_root, "ticket:1")

    monkeypatch.setattr(
        file_chat_routes, "ticket_state_key", lambda idx, path: f"ticket_{idx:03d}_v2"
    )
    monkeypatch.setattr(
        file_chat_routes, "ticket_chat_scope", lambda idx, path: f"ticket:{idx}:v2"
    )
    second = file_chat_routes._parse_target(repo_root, "ticket:1")

    assert first.state_key == "ticket_001_v1"
    assert second.state_key == "ticket_001_v2"
    assert first.chat_scope == "ticket:1:v1"
    assert second.chat_scope == "ticket:1:v2"


def test_ticket_target_identity_stable_across_atomic_write_updates(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_id = "tkt_abc123"
    ticket_path.write_text(
        f"---\nagent: codex\ndone: false\nticket_id: {ticket_id}\n---\n\none\n",
        encoding="utf-8",
    )

    first = file_chat_routes._parse_target(repo_root, "ticket:1")
    atomic_write(
        ticket_path,
        f"---\nagent: codex\ndone: true\nticket_id: {ticket_id}\n---\n\ntwo\n",
    )
    second = file_chat_routes._parse_target(repo_root, "ticket:1")

    assert first.state_key == second.state_key
    assert first.chat_scope == second.chat_scope
    assert first.chat_scope.endswith(ticket_id)
