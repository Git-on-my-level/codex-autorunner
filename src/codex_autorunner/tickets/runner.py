from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.git_utils import run_git
from .agent_pool import AgentPool, AgentTurnRequest
from .files import list_ticket_paths, read_ticket, safe_relpath, ticket_is_done
from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_frontmatter
from .models import TicketFrontmatter, TicketResult, TicketRunConfig, normalize_requires
from .outbox import dispatch_outbox, ensure_outbox_dirs, resolve_outbox_paths
from .replies import ensure_reply_dirs, parse_user_reply, resolve_reply_paths

_logger = logging.getLogger(__name__)


class TicketRunner:
    """Execute a ticket directory one agent turn at a time.

    This runner is intentionally small and file-backed:
    - Tickets are markdown files under `config.ticket_dir`.
    - User messages + optional attachments are written to an outbox under `config.runs_dir`.
    - The orchestrator is stateless aside from the `state` dict passed into step().
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        run_id: str,
        config: TicketRunConfig,
        agent_pool: AgentPool,
    ):
        self._workspace_root = workspace_root
        self._run_id = run_id
        self._config = config
        self._agent_pool = agent_pool

    async def step(self, state: dict[str, Any]) -> TicketResult:
        """Execute exactly one orchestration step.

        A step is either:
        - run one agent turn for the current ticket, or
        - pause because prerequisites are missing, or
        - mark the whole run completed (no remaining tickets).
        """

        state = dict(state or {})
        # Clear transient reason from previous pause/resume cycles.
        state.pop("reason", None)
        # Global counters.
        total_turns = int(state.get("total_turns") or 0)
        if total_turns >= self._config.max_total_turns:
            return self._pause(
                state,
                reason=f"Max turns reached ({self._config.max_total_turns}). Review tickets and resume.",
            )

        ticket_dir = self._workspace_root / self._config.ticket_dir
        runs_dir = self._config.runs_dir

        # Ensure outbox dirs exist.
        outbox_paths = resolve_outbox_paths(
            workspace_root=self._workspace_root,
            runs_dir=runs_dir,
            run_id=self._run_id,
        )
        ensure_outbox_dirs(outbox_paths)

        # Ensure reply inbox dirs exist (human -> agent messages).
        reply_paths = resolve_reply_paths(
            workspace_root=self._workspace_root,
            runs_dir=runs_dir,
            run_id=self._run_id,
        )
        ensure_reply_dirs(reply_paths)

        ticket_paths = list_ticket_paths(ticket_dir)
        if not ticket_paths:
            return self._pause(
                state,
                reason=(
                    "No tickets found. Create tickets under "
                    f"{safe_relpath(ticket_dir, self._workspace_root)} and resume."
                ),
            )

        current_ticket = state.get("current_ticket")
        current_path: Optional[Path] = (
            (self._workspace_root / current_ticket)
            if isinstance(current_ticket, str) and current_ticket
            else None
        )

        # If current ticket is done, clear it.
        if current_path and ticket_is_done(current_path):
            current_path = None
            state.pop("current_ticket", None)
            state.pop("ticket_turns", None)
            state.pop("last_agent_output", None)
            state.pop("lint", None)

        if current_path is None:
            next_path = self._find_next_ticket(ticket_paths)
            if next_path is None:
                state["status"] = "completed"
                return TicketResult(
                    status="completed", state=state, reason="All tickets done."
                )
            current_path = next_path
            state["current_ticket"] = safe_relpath(current_path, self._workspace_root)
            # New ticket resets per-ticket state.
            state["ticket_turns"] = 0
            state.pop("last_agent_output", None)
            state.pop("lint", None)

        # Determine lint-retry mode early. When lint state is present, we allow the
        # agent to fix the ticket frontmatter even if the ticket is currently
        # unparsable by the strict lint rules.
        lint_state = state.get("lint") if isinstance(state.get("lint"), dict) else {}
        lint_errors = (
            lint_state.get("errors")
            if isinstance(lint_state.get("errors"), list)
            else []
        )
        lint_retries = int(lint_state.get("retries") or 0)
        reuse_conversation_id = (
            lint_state.get("conversation_id")
            if isinstance(lint_state.get("conversation_id"), str)
            else None
        )

        # Read ticket (may lint-fail). In lint-retry mode, fall back to a relaxed
        # frontmatter parse so we can still execute an agent turn to repair the file.
        ticket_doc = None
        ticket_errors: list[str] = []
        if lint_errors:
            try:
                raw = current_path.read_text(encoding="utf-8")
            except OSError as exc:
                return self._pause(
                    state,
                    reason=(
                        "Ticket unreadable during lint retry for "
                        f"{safe_relpath(current_path, self._workspace_root)}: {exc}"
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                )

            data, _ = parse_markdown_frontmatter(raw)
            agent = data.get("agent")
            agent_id = agent.strip() if isinstance(agent, str) else None
            if not agent_id:
                return self._pause(
                    state,
                    reason=(
                        "Cannot determine ticket agent during lint retry (missing frontmatter.agent). "
                        "Fix the ticket frontmatter manually and resume."
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                )

            # Validate agent id unless it is the special pause sentinel.
            if agent_id != "pause":
                try:
                    from ..agents.registry import validate_agent_id

                    agent_id = validate_agent_id(agent_id)
                except Exception as exc:
                    return self._pause(
                        state,
                        reason=(
                            "Cannot determine valid agent during lint retry for "
                            f"{safe_relpath(current_path, self._workspace_root)}: {exc}"
                        ),
                        current_ticket=safe_relpath(current_path, self._workspace_root),
                    )

            requires = normalize_requires(data.get("requires"))
            ticket_doc = type(
                "_TicketDocForLintRetry",
                (),
                {
                    "frontmatter": TicketFrontmatter(
                        agent=agent_id,
                        done=False,
                        requires=requires,
                    )
                },
            )()
        else:
            ticket_doc, ticket_errors = read_ticket(current_path)
            if ticket_errors or ticket_doc is None:
                return self._pause(
                    state,
                    reason=(
                        "Ticket frontmatter invalid for "
                        f"{safe_relpath(current_path, self._workspace_root)}:\n- "
                        + "\n- ".join(ticket_errors)
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                )

        # Built-in manual pause ticket.
        if ticket_doc.frontmatter.agent == "pause":
            if ticket_doc.frontmatter.done:
                # Nothing to do, will advance next step.
                return TicketResult(status="continue", state=state)
            return self._pause(
                state,
                reason=(
                    "Paused for user input. Mark ticket as done when ready: "
                    f"{safe_relpath(current_path, self._workspace_root)}"
                ),
                current_ticket=safe_relpath(current_path, self._workspace_root),
            )

        # Validate required input files (skip during lint retry; the only goal is to
        # repair the ticket metadata so normal orchestration can resume).
        if not lint_errors:
            missing = self._missing_required_inputs(ticket_doc.frontmatter.requires)
            if missing:
                rel_missing = [
                    safe_relpath(self._workspace_root / m, self._workspace_root)
                    for m in missing
                ]
                return self._pause(
                    state,
                    reason=(
                        "Missing required input files for this ticket:\n- "
                        + "\n- ".join(rel_missing)
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                )

        ticket_turns = int(state.get("ticket_turns") or 0)
        reply_seq = int(state.get("reply_seq") or 0)
        reply_context, reply_max_seq = self._build_reply_context(
            reply_paths=reply_paths, last_seq=reply_seq
        )

        prompt = self._build_prompt(
            ticket_path=current_path,
            ticket_doc=ticket_doc,
            last_agent_output=(
                state.get("last_agent_output")
                if isinstance(state.get("last_agent_output"), str)
                else None
            ),
            outbox_paths=outbox_paths,
            lint_errors=lint_errors if lint_errors else None,
            reply_context=reply_context,
        )

        # Execute turn.
        req = AgentTurnRequest(
            agent_id=ticket_doc.frontmatter.agent,
            prompt=prompt,
            workspace_root=self._workspace_root,
            conversation_id=reuse_conversation_id,
        )

        total_turns += 1
        ticket_turns += 1
        state["total_turns"] = total_turns
        state["ticket_turns"] = ticket_turns

        result = await self._agent_pool.run_turn(req)
        if result.error:
            state["last_agent_output"] = result.text
            state["last_agent_id"] = result.agent_id
            state["last_agent_conversation_id"] = result.conversation_id
            state["last_agent_turn_id"] = result.turn_id
            return self._pause(
                state,
                reason=(
                    "Agent turn failed; fix the underlying issue and resume.\n"
                    f"Error: {result.error}"
                ),
                current_ticket=safe_relpath(current_path, self._workspace_root),
            )

        # Mark replies as consumed only after a successful agent turn.
        if reply_max_seq > reply_seq:
            state["reply_seq"] = reply_max_seq
        state["last_agent_output"] = result.text
        state["last_agent_id"] = result.agent_id
        state["last_agent_conversation_id"] = result.conversation_id
        state["last_agent_turn_id"] = result.turn_id

        # Post-turn: archive outbox if USER_MESSAGE exists.
        outbox_seq = int(state.get("outbox_seq") or 0)
        dispatch, dispatch_errors = dispatch_outbox(
            outbox_paths, next_seq=outbox_seq + 1
        )
        if dispatch_errors:
            # Treat as pause: user should fix USER_MESSAGE frontmatter. Keep outbox
            # lint separate from ticket frontmatter lint to avoid mixing behaviors.
            state["outbox_lint"] = dispatch_errors
            return self._pause(
                state,
                reason=(
                    "Invalid USER_MESSAGE.md frontmatter:\n- "
                    + "\n- ".join(dispatch_errors)
                ),
                current_ticket=safe_relpath(current_path, self._workspace_root),
            )

        if dispatch is not None:
            state["outbox_seq"] = dispatch.seq
            state.pop("outbox_lint", None)

        # Post-turn: ticket frontmatter must remain valid.
        updated_fm, fm_errors = self._recheck_ticket_frontmatter(current_path)
        if fm_errors:
            lint_retries += 1
            if lint_retries > self._config.max_lint_retries:
                return self._pause(
                    state,
                    reason=(
                        "Ticket frontmatter is invalid after agent turn and exceeded lint retry limit.\n"
                        "Fix the ticket frontmatter manually and resume.\n\nErrors:\n- "
                        + "\n- ".join(fm_errors)
                    ),
                    current_ticket=safe_relpath(current_path, self._workspace_root),
                )

            state["lint"] = {
                "errors": fm_errors,
                "retries": lint_retries,
                "conversation_id": result.conversation_id,
            }
            return TicketResult(
                status="continue",
                state=state,
                reason="Ticket frontmatter invalid; requesting agent fix.",
                current_ticket=safe_relpath(current_path, self._workspace_root),
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )

        # Clear lint state if previously set.
        if state.get("lint"):
            state.pop("lint", None)

        # Optional: auto-commit checkpoint (best-effort).
        checkpoint_error = None
        if self._config.auto_commit:
            checkpoint_error = self._checkpoint_git(
                turn=total_turns, agent=result.agent_id
            )

        # If we dispatched a pause message, pause regardless of ticket completion.
        if dispatch is not None and dispatch.message.mode == "pause":
            reason = dispatch.message.title or "Paused for user input."
            if checkpoint_error:
                reason += f"\n\nNote: checkpoint commit failed: {checkpoint_error}"
            state["reason"] = reason
            return TicketResult(
                status="paused",
                state=state,
                reason=reason,
                dispatch=dispatch,
                current_ticket=safe_relpath(current_path, self._workspace_root),
                agent_output=result.text,
                agent_id=result.agent_id,
                agent_conversation_id=result.conversation_id,
                agent_turn_id=result.turn_id,
            )

        # Advance if ticket done.
        if updated_fm and updated_fm.done:
            state.pop("current_ticket", None)
            state.pop("ticket_turns", None)
            state.pop("last_agent_output", None)
            state.pop("lint", None)

        if checkpoint_error:
            # Non-fatal, but surface in state for UI.
            state["last_checkpoint_error"] = checkpoint_error
        else:
            state.pop("last_checkpoint_error", None)

        return TicketResult(
            status="continue",
            state=state,
            reason="Turn complete.",
            dispatch=dispatch,
            current_ticket=safe_relpath(current_path, self._workspace_root),
            agent_output=result.text,
            agent_id=result.agent_id,
            agent_conversation_id=result.conversation_id,
            agent_turn_id=result.turn_id,
        )

    def _find_next_ticket(self, ticket_paths: list[Path]) -> Optional[Path]:
        for path in ticket_paths:
            if ticket_is_done(path):
                continue
            return path
        return None

    def _missing_required_inputs(self, requires: tuple[str, ...]) -> list[str]:
        missing: list[str] = []
        for rel in requires:
            abs_path = self._workspace_root / rel
            if not abs_path.exists():
                missing.append(rel)
        return missing

    def _recheck_ticket_frontmatter(self, ticket_path: Path):
        try:
            raw = ticket_path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, [f"Failed to read ticket after turn: {exc}"]
        from .frontmatter import parse_markdown_frontmatter

        data, _ = parse_markdown_frontmatter(raw)
        fm, errors = lint_ticket_frontmatter(data)
        return fm, errors

    def _checkpoint_git(self, *, turn: int, agent: str) -> Optional[str]:
        """Create a best-effort git commit checkpoint.

        Returns an error string if the checkpoint failed, else None.
        """

        try:
            status_proc = run_git(
                ["status", "--porcelain"], cwd=self._workspace_root, check=True
            )
            if not (status_proc.stdout or "").strip():
                return None
            run_git(["add", "-A"], cwd=self._workspace_root, check=True)
            msg = self._config.checkpoint_message_template.format(
                run_id=self._run_id,
                turn=turn,
                agent=agent,
            )
            run_git(["commit", "-m", msg], cwd=self._workspace_root, check=True)
            return None
        except Exception as exc:
            _logger.exception("Checkpoint commit failed")
            return str(exc)

    def _pause(
        self,
        state: dict[str, Any],
        *,
        reason: str,
        current_ticket: Optional[str] = None,
    ) -> TicketResult:
        state = dict(state)
        state["status"] = "paused"
        state["reason"] = reason
        return TicketResult(
            status="paused",
            state=state,
            reason=reason,
            current_ticket=current_ticket
            or (
                state.get("current_ticket")
                if isinstance(state.get("current_ticket"), str)
                else None
            ),
        )

    def _build_reply_context(self, *, reply_paths, last_seq: int) -> tuple[str, int]:
        """Render new human replies (reply_history) into a prompt block.

        Returns (rendered_text, max_seq_seen).
        """

        history_dir = getattr(reply_paths, "reply_history_dir", None)
        if history_dir is None:
            return "", last_seq
        if not history_dir.exists() or not history_dir.is_dir():
            return "", last_seq

        entries: list[tuple[int, Path]] = []
        try:
            for child in history_dir.iterdir():
                try:
                    if not child.is_dir():
                        continue
                    name = child.name
                    if not (len(name) == 4 and name.isdigit()):
                        continue
                    seq = int(name)
                    if seq <= last_seq:
                        continue
                    entries.append((seq, child))
                except OSError:
                    continue
        except OSError:
            return "", last_seq

        if not entries:
            return "", last_seq

        entries.sort(key=lambda x: x[0])
        max_seq = max(seq for seq, _ in entries)

        blocks: list[str] = []
        for seq, entry_dir in entries:
            reply_path = entry_dir / "USER_REPLY.md"
            reply, errors = (
                parse_user_reply(reply_path)
                if reply_path.exists()
                else (None, ["USER_REPLY.md missing"])
            )

            block_lines: list[str] = [f"[USER_REPLY {seq:04d}]"]
            if errors:
                block_lines.append("Errors:\n- " + "\n- ".join(errors))
            if reply is not None:
                if reply.title:
                    block_lines.append(f"Title: {reply.title}")
                if reply.body:
                    block_lines.append(reply.body)

            attachments: list[str] = []
            try:
                for child in sorted(entry_dir.iterdir(), key=lambda p: p.name):
                    try:
                        if child.name.startswith("."):
                            continue
                        if child.name == "USER_REPLY.md":
                            continue
                        if child.is_dir():
                            continue
                        attachments.append(safe_relpath(child, self._workspace_root))
                    except OSError:
                        continue
            except OSError:
                attachments = []

            if attachments:
                block_lines.append("Attachments:\n- " + "\n- ".join(attachments))

            blocks.append("\n".join(block_lines).strip())

        rendered = "\n\n".join(blocks).strip()
        return rendered, max_seq

    def _build_prompt(
        self,
        *,
        ticket_path: Path,
        ticket_doc,
        last_agent_output: Optional[str],
        outbox_paths,
        lint_errors: Optional[list[str]],
        reply_context: Optional[str] = None,
    ) -> str:
        rel_ticket = safe_relpath(ticket_path, self._workspace_root)
        rel_handoff = safe_relpath(outbox_paths.handoff_dir, self._workspace_root)
        rel_user_msg = safe_relpath(
            outbox_paths.user_message_path, self._workspace_root
        )

        header = (
            "You are running inside Codex AutoRunner (CAR) in a ticket-based workflow.\n"
            "Complete the current ticket by making changes in the repo and updating the ticket file.\n\n"
            "Key rules:\n"
            f"- Current ticket file: {rel_ticket}\n"
            "- Ticket completion is controlled by YAML frontmatter: set 'done: true' when finished.\n"
            "- To message the user, optionally write attachments first to the handoff directory, then write USER_MESSAGE.md last.\n"
            f"  - Handoff directory: {rel_handoff}\n"
            f"  - USER_MESSAGE.md path: {rel_user_msg}\n"
            "  USER_MESSAGE.md frontmatter supports: mode: notify|pause (pause will halt the run).\n"
            "- Keep tickets minimal and avoid scope creep. You may create new tickets only if blocking the current SPEC.\n"
        )

        if lint_errors:
            lint_block = (
                "\n\nTicket frontmatter lint failed. Fix ONLY the ticket frontmatter to satisfy:\n- "
                + "\n- ".join(lint_errors)
                + "\n"
            )
        else:
            lint_block = ""

        requires_block = ""
        if ticket_doc.frontmatter.requires:
            requires_block = (
                "\n\nRequired input files for this ticket:\n- "
                + "\n- ".join(ticket_doc.frontmatter.requires)
                + "\n"
            )

        reply_block = ""
        if reply_context:
            reply_block = (
                "\n\n---\n\nHUMAN REPLIES (from reply_history; newest since last turn):\n"
                + reply_context
                + "\n"
            )

        ticket_block = (
            "\n\n---\n\n"
            "TICKET CONTENT (edit this file to track progress; update frontmatter.done when complete):\n"
            f"PATH: {rel_ticket}\n"
            "\n" + ticket_path.read_text(encoding="utf-8")
        )

        prev_block = ""
        if last_agent_output:
            prev_block = (
                "\n\n---\n\nPREVIOUS AGENT OUTPUT (same ticket):\n" + last_agent_output
            )

        return (
            header
            + lint_block
            + requires_block
            + reply_block
            + ticket_block
            + prev_block
        )
