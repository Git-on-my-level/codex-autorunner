from __future__ import annotations

from pathlib import Path

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
