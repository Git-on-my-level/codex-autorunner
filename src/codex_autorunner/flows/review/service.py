from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from ...agents.opencode.run_prompt import OpenCodeRunConfig, run_opencode_prompt
from ...agents.opencode.runtime import PERMISSION_ALLOW
from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...agents.registry import has_capability, validate_agent_id
from ...core.config import RepoConfig
from ...core.locks import (
    FileLock,
    FileLockBusy,
    FileLockError,
    process_alive,
    read_lock_info,
)
from ...core.runtime import RuntimeContext
from ...core.state import now_iso
from ...core.utils import atomic_write, read_json
from ...integrations.chat.agents import DEFAULT_CHAT_AGENT_MODELS
from .models import (
    ACTIVE_REVIEW_STATUSES,
    ReviewPromptKind,
    ReviewState,
    ReviewStateSnapshot,
    ReviewStatus,
)

REVIEW_STATE_VERSION = 1
REVIEW_TIMEOUT_SECONDS = 3600
REVIEW_INTERRUPT_GRACE_SECONDS = 10
REVIEW_PROMPT = """# Multi-Agent Code Review Prompt (Coordinator + Subagents)

You are coordinating a multi-agent, high-signal code review.

## Only Required Variables

* Scratchpad (temporary coordination workspace): {{scratchpad_dir}}
* Final output path (single source of truth for final findings): {{final_output_path}}

Everything else should be inferred from the repository and discovery phase.

---

## North Star

Produce a review that is:

* **Impact-focused:** real production failures, credible security risks, data loss/corruption, reliability hazards.
* **Project-aware:** adapt to the project's architecture, maturity, and runtime assumptions you can infer.
* **Trackable:** findings must be verifiable and easy to convert into work items.

Avoid noise: style-only comments, theoretical issues without a plausible failure/attack path, micro-optimizations.

---

## Shared Output Contract (All Agents)

Every finding must be:

* **Verifiable:** include `path:line` if possible; otherwise function/class anchors.
* **Concrete:** include 1–2 evidence cues from the code.
* **Actionable:** include a fix direction (high-level).

Each finding includes:

* Severity: **Critical / High / Medium**
* Likelihood: **High / Medium / Low**
* Confidence: **High / Medium / Low**
* Evidence: concrete cues
* Impact: what breaks / what could be exploited
* Fix direction: high-level approach

---

## Phase 0: Setup (Coordinator)

Create directories/files under `{{scratchpad_dir}}` for coordination artifacts.

Required scratchpad files:

* `{{scratchpad_dir}}/REVIEW_CONTEXT.md`
* `{{scratchpad_dir}}/BUCKETS.md`
* `{{scratchpad_dir}}/FINDINGS_RAW.md`
* `{{scratchpad_dir}}/PRUNE_A.md`
* `{{scratchpad_dir}}/PRUNE_B.md`
* (Optional) `{{scratchpad_dir}}/PRUNE_C.md`

Final output file:

* `{{final_output_path}}`

---

## Phase 1: Discovery (Coordinator)

### 1) Rapid Recon

Goal: understand what the project is, how it's structured, and where risk concentrates.

Use lightweight exploration appropriate to the environment. (Any commands shown are examples; adapt.)

Write `{{scratchpad_dir}}/REVIEW_CONTEXT.md` with:

* Project summary (what it is, how it runs, as inferred)
* Key components / directories and their roles
* Trust boundaries (inputs/outputs, external calls, persistence, rendering)
* 3–7 "must-hold" invariants (e.g., authz enforced server-side; timeouts on external calls; idempotent handlers)
* Review plan: how many buckets, depth expectations, any notable constraints

### 2) Define Buckets

Create 4–8 buckets that match the project's actual shape. Prefer buckets that reduce overlap.

Record in `{{scratchpad_dir}}/BUCKETS.md`:
For each bucket:

* Name
* Included paths
* Key entrypoints (if any)
* "Read deeply" file suggestions (if obvious)

---

## Phase 2: Parallel Bucket Reviews (Subagents)

### 3) Launch One Subagent Per Bucket

For each bucket, launch a subagent using the template below.

**IMPORTANT**: Use the "subagent" agent type. Do NOT specify model in the prompt.
- The subagent agent is pre-configured with GLM-4.7-FlashX
- This avoids concurrency throttling while maintaining full GLM-4.7 for the coordinator

Subagent prompt template:

Task: Thorough code review of **[BUCKET NAME]**

CONTEXT:

* Read `{{scratchpad_dir}}/REVIEW_CONTEXT.md` and `{{scratchpad_dir}}/BUCKETS.md` first.
* Focus on real failure modes and credible risks. Prefer systemic patterns.

SCOPE:

* Review paths: [paths from BUCKETS.md]
* Read deeply: identify 3–8 central files in-scope (or use those suggested)

RULES:

* Be explicit when something is an inference; state what would confirm/refute it.
* Avoid style-only feedback unless it causes defects or blocks maintenance.
* Provide anchors: `path:line` if possible, else function/class anchors.

RETURN FORMAT (exact):

## 🔴 High Priority

* [path:line or anchor] Finding

  * Severity:
  * Likelihood:
  * Confidence:
  * Evidence:
  * Impact:
  * Fix direction:

## 🟡 Medium Priority

* (same fields)

## 🧩 Cross-file Patterns / Notes

* Systemic patterns observed (good or risky)

## ❓ Unknowns / Assumptions

* What you couldn't verify quickly, and what to check next

---

## Phase 3: Aggregation (Coordinator)

### 4) Aggregate Without Pruning

Collect all subagent outputs into `{{scratchpad_dir}}/FINDINGS_RAW.md`.

Then:

* Deduplicate obvious repeats (same root cause)
* Normalize formatting to match the shared output contract
* Tag each finding with a category prefix if helpful (Security, Reliability, Data Integrity, Concurrency, Config, Performance, Architecture)

---

## Phase 4: Pruning for 80/20 Value (Coordinator + Pruning Agents)

### 5) Launch 2–3 Independent Pruning Agents

Each pruning agent must read `{{scratchpad_dir}}/FINDINGS_RAW.md` first, then produce a pruned list.

Pruning criteria:

* Remove: speculative/theoretical items without plausible impact, style-only items, micro-optimizations, doc-only notes.
* Keep: credible security issues, production-breaking bugs, data loss/corruption risks, likely reliability failures, lifecycle leaks, dangerous defaults, systemic patterns that repeatedly cause defects.

Pruning output format:

## 🔥 Critical

* [path:line] Finding — why critical, fix direction, rough effort

## ⚠️ High

* ...

## 📌 Medium

* ...

## Summary

* Themes kept vs discarded and why

Write pruning outputs to:

* `{{scratchpad_dir}}/PRUNE_A.md`
* `{{scratchpad_dir}}/PRUNE_B.md`
* (Optional) `{{scratchpad_dir}}/PRUNE_C.md`

---

## Phase 5: Final Synthesis (Coordinator)

### 6) Compare Pruning Outputs and Consolidate

Create `{{final_output_path}}` as the single source of truth:

* Critical → High → Medium
* Group by category
* Keep consistent fields (severity/likelihood/confidence/evidence/impact/fix direction)
* Add a short "Systemic Patterns" section (3–10 bullets max)

---

## Stop Conditions (avoid over-review)

Stop digging deeper when:

* New findings are mostly repeats or low impact
* You've covered key trust boundaries and invariants
* You can clearly name the top systemic risks and fix directions
"""
REVIEW_PROMPT_SPEC_PROGRESS = """# Autorunner Final Review (Spec + Progress Focus)

You are coordinating a multi-agent review immediately after an autorunner loop completes. The goal is to validate work against spec.md, not to suggest generic code style fixes.

## Required Scratchpad + Output

* Scratchpad dir: {{scratchpad_dir}}
* Final report (single source of truth): {{final_output_path}}
* Required scratchpad files:
  * {{scratchpad_dir}}/AUTORUNNER_CONTEXT.md (read this first)
  * {{scratchpad_dir}}/REVIEW_CONTEXT.md
  * {{scratchpad_dir}}/BUCKETS.md
  * {{scratchpad_dir}}/FINDINGS_RAW.md
  * {{scratchpad_dir}}/PRUNE_A.md
  * {{scratchpad_dir}}/PRUNE_B.md
  * (Optional) {{scratchpad_dir}}/PRUNE_C.md

## Source of Truth + Focus

* Read AUTORUNNER_CONTEXT.md first. It contains the exit reason, spec.md, and last-run artifacts (output/diff/plan excerpts).
* Treat spec.md as the contract: extract explicit requirements, promised validation steps, and open questions.
* Derive must-hold invariants directly from SPEC (not generic guesses).
* Buckets should be anchored to spec sections mapped to implementation areas, plus any recently changed/critical modules from AUTORUNNER_CONTEXT.md.

## North Star

* Spec compliance over style.
* High-signal risks and regressions that would violate spec commitments.
* Trackable output that can be turned into tickets.

## Phase 0: Setup (Coordinator)

Prepare scratchpad files under {{scratchpad_dir}} (see list above).

## Phase 1: Discovery (Coordinator)

1) Read AUTORUNNER_CONTEXT.md and summarize:
   * Project shape and runtime assumptions
   * Spec requirements + invariants
   * Open questions/gaps
2) Define buckets in BUCKETS.md:
   * Buckets by review dimension + code areas (spec sections → code paths)
   * Include any critical/changed modules surfaced in the last run artifacts

## Phase 2: Parallel Bucket Reviews (Subagents)

Launch subagents by review dimension:
* Spec compliance agent: requirement → evidence mapping.
* Risk & regression agent: likely failure modes introduced by recent changes.
* Optional: Test adequacy agent if tests are in-scope.

Each subagent must read AUTORUNNER_CONTEXT.md and BUCKETS.md first and write findings back to the scratchpad.

## Phase 3: Aggregate (Coordinator)

Collect subagent outputs into FINDINGS_RAW.md. Deduplicate and normalize to the shared output contract.

## Phase 4: Prune (Independent Agents)

Run 2–3 pruning passes (PRUNE_A.md, PRUNE_B.md, optional PRUNE_C.md) to drop speculative or low-impact items. Keep credible security issues, regressions, data loss/corruption risks, and systemic failures.

## Phase 5: Final Synthesis (Coordinator)

Create the final report at {{final_output_path}} with this structure:

## Spec-to-Implementation Matrix
| Spec item | Status (met/partial/missing) | Evidence | Notes |

## Spec Verification
- Spec requirement: ... (cite spec section)
  - Verified by: ...
  - Not verified because: ...

## Findings (prioritized)
### Critical
...
### High
...
### Medium
...

## Follow-ups (copy/paste into TODO)
- [ ] ...
- [ ] ...

Add a short "Systemic Patterns" section if helpful. Keep severity/likelihood/confidence/evidence/impact/fix direction for each finding.

## Stop Conditions

Stop when spec coverage is complete, progress claims are validated or refuted, and additional digging yields only low-impact or duplicate issues.
"""


class ReviewError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ReviewBusyError(ReviewError):
    pass


class ReviewConflictError(ReviewError):
    pass


def _workflow_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "review"


def _default_state() -> ReviewState:
    return ReviewState(version=REVIEW_STATE_VERSION)


class ReviewService:
    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        opencode_supervisor: Optional[OpenCodeSupervisor] = None,
        app_server_supervisor: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.ctx = ctx
        self._opencode_supervisor = opencode_supervisor
        self._app_server_supervisor = app_server_supervisor
        self._logger = logger or logging.getLogger("codex_autorunner.review")
        self._state_path = _workflow_root(self.ctx.repo_root) / "state.json"
        self._lock_path = (
            self.ctx.repo_root / ".codex-autorunner" / "locks" / "review.lock"
        )
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._lock_handle: Optional[FileLock] = None

    def _repo_config(self) -> RepoConfig:
        if not isinstance(self.ctx.config, RepoConfig):
            raise ReviewError("Review requires a repo workspace config")
        return self.ctx.config

    def status(self) -> ReviewStateSnapshot:
        state = self._load_state()
        lock_info = read_lock_info(self._lock_path)
        lock_alive = bool(lock_info.pid and process_alive(lock_info.pid))
        is_running = bool(self._thread and self._thread.is_alive()) or lock_alive
        if state.status in ACTIVE_REVIEW_STATUSES and not is_running:
            state = state.recover_after_restart()
            self._save_state(state)
        return state.with_runtime(running=is_running)

    def start(self, *, payload: dict[str, Any]) -> ReviewState:
        with self._thread_lock:
            snapshot = self.status()
            if snapshot.status in ACTIVE_REVIEW_STATUSES:
                raise ReviewBusyError("Review already running", status_code=409)
            if self._thread and self._thread.is_alive():
                raise ReviewBusyError("Review already running", status_code=409)
            busy_reason = self.ctx.repo_busy_reason()
            if busy_reason:
                raise ReviewConflictError(
                    f"Cannot start review: {busy_reason}", status_code=409
                )
            self._acquire_lock()
            thread_started = False
            try:
                state = self._initialize_state(payload=payload)
                if state.id is None:
                    raise ReviewError("Review state is incomplete")
                self._stop_event.clear()
                state = state.with_worker(
                    worker_id=uuid.uuid4().hex,
                    worker_pid=os.getpid(),
                    worker_started_at=now_iso(),
                )
                self._save_state(state)
                self._thread = threading.Thread(
                    target=self._run_review, args=(state.id,), daemon=True
                )
                self._thread.start()
                thread_started = True
                self._log(f"Started review run {state.id}")
                return state
            finally:
                if not thread_started:
                    self._release_lock()

    async def run_blocking_async(
        self,
        *,
        payload: dict[str, Any],
        prompt_kind: ReviewPromptKind | str,
        seed_context_files: Optional[dict[str, str]] = None,
        ignore_repo_busy: bool = False,
    ) -> ReviewState:
        with self._thread_lock:
            snapshot = self.status()
            if snapshot.status in ACTIVE_REVIEW_STATUSES:
                raise ReviewBusyError("Review already running", status_code=409)
            busy_reason = self.ctx.repo_busy_reason()
            if busy_reason and not ignore_repo_busy:
                raise ReviewConflictError(
                    f"Cannot start review: {busy_reason}", status_code=409
                )
            self._acquire_lock()
            try:
                state = self._initialize_state(payload=payload, prompt_kind=prompt_kind)
                if state.id is None or state.scratchpad_dir is None:
                    raise ReviewError("Review state is incomplete")
                self._stop_event.clear()
                state = state.with_worker(
                    worker_id=uuid.uuid4().hex,
                    worker_pid=os.getpid(),
                    worker_started_at=now_iso(),
                )
                self._save_state(state)
                self._log(f"Started review run {state.id} (blocking)")
            except (
                Exception
            ):  # intentional: re-raise safety net to guarantee lock release
                self._release_lock()
                raise

        run_id = state.id
        scratchpad_dir_str = state.scratchpad_dir
        if run_id is None or scratchpad_dir_str is None:
            raise ReviewError("Review state is incomplete")

        scratchpad_dir = Path(scratchpad_dir_str)
        if seed_context_files:
            self._write_seed_context_files(scratchpad_dir, seed_context_files)

        try:
            try:
                await self._run_review_async(run_id)
            except (
                ReviewError,
                ValueError,
                TypeError,
                RuntimeError,
                OSError,
                AttributeError,
                ConnectionResetError,
            ) as exc:  # intentional: top-level review error handler, must capture any agent/infra failure
                self._log(f"Review run failed: {exc}")
                failure_state = self._load_state().mark_failed(str(exc))
                self._save_state(failure_state)
                raise
            return self._load_state()
        finally:
            self._release_lock()

    def run_blocking(
        self,
        *,
        payload: dict[str, Any],
        prompt_kind: ReviewPromptKind | str,
        seed_context_files: Optional[dict[str, str]] = None,
        ignore_repo_busy: bool = False,
    ) -> ReviewState:
        return asyncio.run(
            self.run_blocking_async(
                payload=payload,
                prompt_kind=prompt_kind,
                seed_context_files=seed_context_files,
                ignore_repo_busy=ignore_repo_busy,
            )
        )

    def stop(self) -> ReviewState:
        self._stop_event.set()
        state = self._load_state()
        next_state = state.request_stop()
        if state.status in ACTIVE_REVIEW_STATUSES:
            self._save_state(next_state)
            self._log("Stop requested")
        return next_state

    def reset(self) -> ReviewState:
        with self._thread_lock:
            snapshot = self.status()
            if snapshot.status in ACTIVE_REVIEW_STATUSES:
                raise ReviewBusyError(
                    "Cannot reset while review is running", status_code=409
                )
            state = _default_state()
            self._save_state(state)
            self._log("Review state reset")
            return state

    def _acquire_lock(self) -> None:
        if self._lock_handle is not None:
            return
        try:
            self._lock_handle = FileLock(self._lock_path)
            self._lock_handle.acquire(blocking=False)
        except FileLockBusy as exc:
            raise ReviewBusyError("Review is locked by another process") from exc
        except FileLockError as exc:
            raise ReviewError(f"Failed to acquire lock: {exc}") from exc

    def _release_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            self._lock_handle.release()
        except (
            Exception
        ) as e:  # intentional: best-effort lock release, must not mask original error
            self._logger.debug("Lock release failed: %s", e)
        self._lock_handle = None

    def _load_state(self) -> ReviewState:
        state = read_json(self._state_path) or {}
        if not isinstance(state, dict):
            return _default_state()
        return ReviewState.model_validate(state)

    def _save_state(self, state: ReviewState) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self._state_path,
            json.dumps(state.persist_payload(), indent=2) + "\n",
        )

    def _workflow_log_path(self, state: Optional[ReviewState] = None) -> Optional[Path]:
        if state is None:
            state = self._load_state()
        run_dir = state.run_dir
        if not run_dir:
            return None
        return Path(run_dir) / "review.log"

    def _ensure_workflow_log(self, state: ReviewState) -> None:
        log_path = self._workflow_log_path(state)
        if log_path is None or log_path.exists():
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            started_at = state.started_at or now_iso()
            run_id = state.id or "unknown"
            log_path.write_text(
                f"[{started_at}] Review run {run_id} started\n",
                encoding="utf-8",
            )
        except OSError:
            self._log("Failed to create workflow log")

    def _append_workflow_log(self, message: str) -> None:
        log_path = self._workflow_log_path()
        if log_path is None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{now_iso()}] {message}\n")
        except OSError:
            self._log("Failed to append workflow log")

    def _render_prompt(
        self, *, state: ReviewState, scratchpad_dir: Path, final_output_path: Path
    ) -> str:
        prompt_kind = state.prompt_kind
        template = (
            REVIEW_PROMPT_SPEC_PROGRESS
            if prompt_kind == ReviewPromptKind.SPEC_PROGRESS
            else REVIEW_PROMPT
        )
        return template.replace("{{scratchpad_dir}}", str(scratchpad_dir)).replace(
            "{{final_output_path}}", str(final_output_path)
        )

    def _write_seed_context_files(
        self, scratchpad_dir: Path, seed_context_files: dict[str, str]
    ) -> None:
        for name, content in (seed_context_files or {}).items():
            if not isinstance(name, str) or not name:
                continue
            target = scratchpad_dir / name
            try:
                atomic_write(target, str(content) if content is not None else "")
            except OSError as exc:
                self._log(f"Failed to write seed context '{name}': {exc}")

    def _initialize_state(
        self,
        *,
        payload: dict[str, Any],
        prompt_kind: ReviewPromptKind | str = ReviewPromptKind.CODE,
    ) -> ReviewState:
        config = self._repo_config()
        review_cfg = config.raw.get("review") or {}
        run_id = uuid.uuid4().hex[:12]
        agent_input = payload.get("agent") or review_cfg.get("agent") or "opencode"
        try:
            agent_id = validate_agent_id(agent_input, config)
        except ValueError as exc:
            raise ReviewError(
                f"Invalid agent '{agent_input}': {exc}",
                status_code=400,
            ) from exc

        model = (
            payload.get("model")
            or review_cfg.get("model")
            or DEFAULT_CHAT_AGENT_MODELS.get("opencode")
        )
        reasoning = payload.get("reasoning") or review_cfg.get("reasoning")
        max_wallclock_seconds = payload.get("max_wallclock_seconds") or review_cfg.get(
            "max_wallclock_seconds"
        )

        if not has_capability(agent_id, "review"):
            raise ReviewError(
                f"Agent '{agent_id}' does not support review.",
                status_code=400,
            )

        runs_dir = _workflow_root(self.ctx.repo_root) / "runs"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        scratchpad_dir = run_dir / "scratchpad"
        scratchpad_dir.mkdir(parents=True, exist_ok=True)

        final_output_path = run_dir / "final_report.md"
        prompt_kind_value = (
            ReviewPromptKind(prompt_kind) if prompt_kind else ReviewPromptKind.CODE
        )

        state = ReviewState(
            version=REVIEW_STATE_VERSION,
            id=run_id,
            status=ReviewStatus.RUNNING,
            agent=agent_id,
            model=model,
            reasoning=reasoning,
            max_wallclock_seconds=max_wallclock_seconds,
            run_dir=run_dir.as_posix(),
            scratchpad_dir=scratchpad_dir.as_posix(),
            final_output_path=final_output_path.as_posix(),
            started_at=now_iso(),
            updated_at=now_iso(),
            prompt_kind=prompt_kind_value,
        )
        self._ensure_workflow_log(state)
        return state

    def _run_review(self, run_id: str) -> None:
        try:
            asyncio.run(self._run_review_async(run_id))
        except (
            Exception
        ) as exc:  # intentional: top-level thread error handler, must capture any async failure
            self._log(f"Review run failed: {exc}")
            state = self._load_state()
            if state.id == run_id:
                self._save_state(state.mark_failed(str(exc)))
        finally:
            self._release_lock()

    async def _run_review_async(self, run_id: str) -> None:
        state = self._load_state()
        if state.id != run_id:
            return
        if not state.scratchpad_dir or not state.final_output_path:
            raise ReviewError("Review state is incomplete")

        scratchpad_dir = Path(state.scratchpad_dir)
        final_output_path = Path(state.final_output_path)

        prompt = self._render_prompt(
            state=state,
            scratchpad_dir=scratchpad_dir,
            final_output_path=final_output_path,
        )

        max_seconds = state.max_wallclock_seconds
        timeout_seconds = (
            max_seconds if max_seconds is not None else REVIEW_TIMEOUT_SECONDS
        )

        agent_id = state.agent or "opencode"
        if agent_id == "codex":
            if self._app_server_supervisor is None:
                raise ReviewError("Codex backend is not configured")
            client = await self._app_server_supervisor.get_client(self.ctx.repo_root)
            thread_id = uuid.uuid4().hex
            review_kwargs: dict[str, Any] = {}
            if state.model:
                review_kwargs["model"] = state.model
            if state.reasoning:
                review_kwargs["effort"] = state.reasoning
            handle = await client.review_start(
                thread_id=thread_id,
                target={"type": "custom", "instructions": prompt},
                delivery="inline",
                cwd=str(self.ctx.repo_root),
                **review_kwargs,
            )

            state = state.with_session(session_id=thread_id, turn_id=handle.turn_id)
            self._save_state(state)

            stop_task = asyncio.create_task(asyncio.to_thread(self._stop_event.wait))
            review_task = asyncio.create_task(handle.wait(timeout=timeout_seconds))
            done, _ = await asyncio.wait(
                {review_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if stop_task in done:
                try:
                    await client.turn_interrupt(
                        handle.turn_id, thread_id=handle.thread_id
                    )
                except (
                    RuntimeError,
                    OSError,
                    ConnectionError,
                    asyncio.TimeoutError,
                    ValueError,
                    TypeError,
                ) as exc:  # best-effort interrupt during shutdown
                    self._log(f"Review stop interrupt failed: {exc}")
                review_task.cancel()
                try:
                    await review_task
                except (
                    Exception
                ) as exc:  # intentional: cancelled task cleanup, must not block stop
                    self._log(f"Cancelled review task raised: {exc}")
                self._save_state(self._load_state().mark_stopped())
                return

            stop_task.cancel()
            try:
                await stop_task
            except (
                RuntimeError,
                OSError,
                asyncio.CancelledError,
            ) as exc:  # cancelled stop listener cleanup
                self._log(f"Cancelled stop listener raised: {exc}")

            try:
                codex_result = await review_task
            except asyncio.TimeoutError as exc:
                raise ReviewError("Review timed out") from exc
            if codex_result.errors:
                raise ReviewError(f"Codex review failed: {codex_result.errors[0]}")
        else:
            if self._opencode_supervisor is None:
                raise ReviewError("OpenCode backend is not configured")

            repo_config = self._repo_config()
            review_cfg = repo_config.raw.get("review") or {}

            subagent_agent_id = review_cfg.get("subagent_agent")
            subagent_model = review_cfg.get("subagent_model")
            if subagent_agent_id:
                await self._opencode_supervisor.ensure_subagent_config(
                    workspace_root=self.ctx.repo_root,
                    agent_id=subagent_agent_id,
                    model=subagent_model,
                )

            config = OpenCodeRunConfig(
                agent=agent_id,
                model=state.model,
                reasoning=state.reasoning,
                prompt=prompt,
                workspace_root=str(self.ctx.repo_root),
                timeout_seconds=timeout_seconds,
                interrupt_grace_seconds=REVIEW_INTERRUPT_GRACE_SECONDS,
                permission_policy=PERMISSION_ALLOW,
            )

            opencode_result = await run_opencode_prompt(
                self._opencode_supervisor,
                config,
                should_stop=self._stop_event.is_set,
                logger=self._logger,
            )

            state = state.with_session(
                session_id=opencode_result.session_id,
                turn_id=opencode_result.turn_id,
            )
            self._save_state(state)

            if opencode_result.stopped:
                self._save_state(self._load_state().mark_stopped())
                return

            if opencode_result.timed_out:
                raise ReviewError("Review timed out")

            if opencode_result.output_error:
                raise ReviewError(
                    f"OpenCode output collection failed: {opencode_result.output_error}"
                )

        if not final_output_path.exists():
            raise ReviewError("Final report not found after review completed")

        final_report = final_output_path.read_text(encoding="utf-8").strip()
        if not final_report:
            raise ReviewError("Final report is empty")

        self._log(
            f"Review completed successfully. Report length: {len(final_report)} chars"
        )

        if state.run_dir is None or state.id is None:
            raise ReviewError("Review state is incomplete")
        scratchpad_bundle_path = self._create_scratchpad_bundle(
            Path(state.run_dir),
            state.id,
        )
        self._save_state(
            self._load_state().mark_completed(
                scratchpad_bundle_path=(
                    scratchpad_bundle_path.as_posix()
                    if scratchpad_bundle_path is not None
                    else None
                )
            )
        )

    def _create_scratchpad_bundle(self, run_dir: Path, run_id: str) -> Optional[Path]:
        scratchpad_dir = run_dir / "scratchpad"
        if not scratchpad_dir.exists():
            return None

        try:
            bundle_path = run_dir / f"scratchpad_{run_id}.zip"
            with zipfile.ZipFile(
                bundle_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6
            ) as zipf:
                for file_path in scratchpad_dir.rglob("*"):
                    if file_path.is_file():
                        arcname = file_path.relative_to(scratchpad_dir)
                        zipf.write(file_path, arcname)
            self._log(f"Created scratchpad bundle: {bundle_path}")
            return bundle_path
        except (OSError, zipfile.BadZipFile) as exc:
            self._log(f"Failed to create scratchpad bundle: {exc}")
            return None

    def _log(self, message: str) -> None:
        self._logger.info(f"Review: {message}")
        self._append_workflow_log(message)
