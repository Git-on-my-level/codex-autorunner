from __future__ import annotations

from typing import Any, Callable


def handle_agent_output(
    log_app_server_output: Callable[[int, list[str]], None],
    write_run_artifact: Callable[[int, str, str], Any],
    merge_run_index_entry: Callable[[int, dict[str, Any]], None],
    run_id: int,
    output: str | list[str],
) -> None:
    if isinstance(output, str):
        messages = [output]
    else:
        messages = output
    log_app_server_output(run_id, messages)
    output_text = "\n\n".join(messages).strip() if messages else ""
    if output_text:
        output_path = write_run_artifact(run_id, "output.txt", output_text)
        merge_run_index_entry(run_id, {"artifacts": {"output_path": str(output_path)}})
