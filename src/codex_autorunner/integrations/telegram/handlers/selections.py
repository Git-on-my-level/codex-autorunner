from __future__ import annotations

from typing import Any, Callable, Optional, cast

from ....core.update import _normalize_update_target
from ..adapter import (
    AgentCallback,
    CancelCallback,
    EffortCallback,
    FlowCallback,
    FlowRunCallback,
    ModelCallback,
    PageCallback,
    ReviewCommitCallback,
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramMessage,
    UpdateCallback,
    UpdateConfirmCallback,
    build_agent_keyboard,
    build_bind_keyboard,
    build_effort_keyboard,
    build_flow_runs_keyboard,
    build_model_keyboard,
    build_resume_keyboard,
    build_review_commit_keyboard,
    build_update_keyboard,
    encode_page_callback,
)
from ..constants import (
    AGENT_PICKER_PROMPT,
    BIND_PICKER_PROMPT,
    DEFAULT_PAGE_SIZE,
    EFFORT_PICKER_PROMPT,
    FLOW_RUNS_PICKER_PROMPT,
    MODEL_PICKER_PROMPT,
    RESUME_BUTTON_PREVIEW_LIMIT,
    RESUME_PICKER_PROMPT,
    REVIEW_COMMIT_PICKER_PROMPT,
    TELEGRAM_MAX_MESSAGE_LENGTH,
)
from ..helpers import (
    ModelOption,
    _compact_preview,
    _format_selection_prompt,
    _page_count,
    _page_slice,
    _selection_contains,
    _set_model_overrides,
    _split_topic_key,
)
from ..types import ModelPickerState, ReviewCommitSelectionState, SelectionState


class TelegramSelectionHandlers:
    async def _dismiss_review_custom_prompt(
        self,
        message: TelegramMessage,
        pending: Optional[dict[str, Any]],
    ) -> None:
        if not pending:
            return
        message_id = pending.get("message_id")
        prompt_text = pending.get("prompt_text")
        if isinstance(message_id, int) and isinstance(prompt_text, str):
            await self._edit_message_text(
                message.chat_id,
                message_id,
                prompt_text,
                reply_markup=None,
            )

    def _handle_pending_resume(self, key: str, text: str) -> bool:
        if not text.isdigit():
            return False
        state = self._resume_options.get(key)
        if not state:
            return False
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        thread_id = page_items[choice - 1][0]
        self._resume_options.pop(key, None)
        self._enqueue_topic_work(
            key,
            lambda: self._resume_thread_by_id(key, thread_id),
        )
        return True

    def _handle_pending_bind(self, key: str, text: str) -> bool:
        if not text.isdigit():
            return False
        state = self._bind_options.get(key)
        if not state:
            return False
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        repo_id = page_items[choice - 1][0]
        self._bind_options.pop(key, None)
        self._enqueue_topic_work(
            key,
            lambda: self._bind_topic_by_repo_id(key, repo_id),
        )
        return True

    async def _handle_agent_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: AgentCallback,
    ) -> None:
        state = self._agent_options.get(key)
        if not state or not _selection_contains(state.items, parsed.agent):
            await self._answer_callback(callback, "Selection expired")
            return
        self._agent_options.pop(key, None)
        record = await self._router.ensure_topic(callback.chat_id, callback.thread_id)
        current = self._effective_agent(record)
        desired = parsed.agent
        if desired == "opencode" and not self._opencode_available():
            await self._answer_callback(callback, "OpenCode missing")
            await self._finalize_selection(
                key,
                callback,
                "OpenCode binary not found. Install opencode or switch to /agent codex.",
            )
            return
        if desired == current:
            await self._answer_callback(callback, "Agent already set")
            await self._finalize_selection(
                key, callback, f"Agent already set to {current}."
            )
            return
        note = await self._apply_agent_change(
            callback.chat_id, callback.thread_id, desired
        )
        await self._answer_callback(callback, "Agent set")
        await self._finalize_selection(
            key,
            callback,
            f"Agent set to {desired}{note}.",
        )

    async def _handle_pending_review_commit(
        self,
        message: TelegramMessage,
        runtime: Any,
        key: str,
        text: str,
    ) -> bool:
        if not text.isdigit():
            return False
        state = self._review_commit_options.get(key)
        if not state:
            return False
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        sha = page_items[choice - 1][0]
        subjects = self._review_commit_subjects.get(key, {})
        subject = subjects.get(sha)
        self._review_commit_options.pop(key, None)
        self._review_commit_subjects.pop(key, None)
        record = await self._require_bound_record(message)
        if not record:
            return True
        thread_id = await self._ensure_thread_id(message, record)
        if not thread_id:
            return True
        target: dict[str, Any] = {"type": "commit", "sha": sha}
        if subject:
            target["title"] = subject
        await self._start_review(
            message,
            runtime,
            record=record,
            thread_id=thread_id,
            target=target,
            delivery=state.delivery,
        )
        return True

    async def _handle_pending_review_custom(
        self,
        key: str,
        message: TelegramMessage,
        runtime: Any,
        command: Optional[TelegramCommand],
        raw_text: str,
        raw_caption: str,
    ) -> bool:
        if command is not None:
            return False
        pending = self._pending_review_custom.get(key)
        if not pending:
            return False
        instructions = raw_text if raw_text.strip() else raw_caption
        if not instructions.strip():
            return False
        self._pending_review_custom.pop(key, None)
        await self._dismiss_review_custom_prompt(message, pending)
        record = await self._require_bound_record(message)
        if not record:
            return True
        thread_id = await self._ensure_thread_id(message, record)
        if not thread_id:
            return True
        target = {"type": "custom", "instructions": instructions}
        await self._start_review(
            message,
            runtime,
            record=record,
            thread_id=thread_id,
            target=target,
            delivery=pending.get("delivery", "inline"),
        )
        return True

    async def _handle_model_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: ModelCallback,
    ) -> None:
        state = self._model_options.get(key)
        if not state:
            await self._answer_callback(callback, "Selection expired")
            return
        option = state.options.get(parsed.model_id)
        if not option:
            await self._answer_callback(callback, "Selection expired")
            return
        self._model_options.pop(key, None)
        if not option.efforts:
            chat_id, thread_id = _split_topic_key(key)
            await self._router.update_topic(
                chat_id,
                thread_id,
                lambda record: _set_model_overrides(
                    record,
                    option.model_id,
                    clear_effort=True,
                ),
            )
            await self._answer_callback(callback, "Model set")
            await self._finalize_selection(
                key,
                callback,
                f"Model set to {option.model_id}. Will apply on the next turn.",
            )
            return
        self._model_pending[key] = option
        self._touch_cache_timestamp("model_pending", key)
        if option.default_effort:
            prompt = (
                f"Select a reasoning effort for {option.model_id} "
                f"(default {option.default_effort})."
            )
        else:
            prompt = EFFORT_PICKER_PROMPT.format(model=option.model_id)
        keyboard = self._build_effort_keyboard(option)
        await self._update_selection_message(key, callback, prompt, keyboard)
        await self._answer_callback(callback, "Select effort")

    async def _handle_effort_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: EffortCallback,
    ) -> None:
        option = self._model_pending.get(key)
        if not option:
            await self._answer_callback(callback, "Selection expired")
            return
        if parsed.effort not in option.efforts:
            await self._answer_callback(callback, "Selection expired")
            return
        self._model_pending.pop(key, None)
        chat_id, thread_id = _split_topic_key(key)
        await self._router.update_topic(
            chat_id,
            thread_id,
            lambda record: _set_model_overrides(
                record,
                option.model_id,
                effort=parsed.effort,
            ),
        )
        await self._answer_callback(callback, "Model set")
        await self._finalize_selection(
            key,
            callback,
            f"Model set to {option.model_id} (effort={parsed.effort}). Will apply on the next turn.",
        )

    async def _handle_update_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: UpdateCallback,
    ) -> None:
        state = self._update_options.get(key)
        if not state or not _selection_contains(state.items, parsed.target):
            await self._answer_callback(callback, "Selection expired")
            return
        self._update_options.pop(key, None)
        try:
            update_target = _normalize_update_target(parsed.target)
        except ValueError:
            await self._answer_callback(callback, "Selection expired")
            await self._finalize_selection(key, callback, "Update target invalid.")
            return
        chat_id, thread_id = _split_topic_key(key)
        await self._start_update(
            chat_id=chat_id,
            thread_id=thread_id,
            update_target=update_target,
            callback=callback,
            selection_key=key,
        )

    async def _handle_update_confirm_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: UpdateConfirmCallback,
    ) -> None:
        if not self._update_confirm_options.get(key):
            await self._answer_callback(callback, "Selection expired")
            return
        self._update_confirm_options.pop(key, None)
        if parsed.decision != "yes":
            await self._answer_callback(callback, "Cancelled")
            await self._finalize_selection(key, callback, "Update cancelled.")
            return
        await self._prompt_update_selection_from_callback(key, callback)
        await self._answer_callback(callback, "Select update target")

    async def _handle_review_commit_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: ReviewCommitCallback,
    ) -> None:
        state = self._review_commit_options.get(key)
        subjects = self._review_commit_subjects.get(key, {})
        if not state or not _selection_contains(state.items, parsed.sha):
            await self._answer_callback(callback, "Selection expired")
            return
        if callback.chat_id is None or callback.message_id is None:
            await self._answer_callback(callback, "Selection expired")
            return
        self._review_commit_options.pop(key, None)
        self._review_commit_subjects.pop(key, None)
        message = TelegramMessage(
            update_id=callback.update_id,
            message_id=callback.message_id,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            from_user_id=callback.from_user_id,
            text=None,
            date=None,
            is_topic_message=bool(callback.thread_id),
        )
        record = await self._require_bound_record(message)
        if not record:
            await self._finalize_selection(
                key, callback, "Topic not bound. Use /bind <repo_id> or /bind <path>."
            )
            return
        thread_id = await self._ensure_thread_id(message, record)
        if not thread_id:
            return
        target: dict[str, Any] = {"type": "commit", "sha": parsed.sha}
        subject = subjects.get(parsed.sha)
        if subject:
            target["title"] = subject
        await self._answer_callback(callback, "Review started")
        await self._finalize_selection(key, callback, "Starting review...")
        runtime = self._router.runtime_for(key)
        await self._start_review(
            message,
            runtime,
            record=record,
            thread_id=thread_id,
            target=target,
            delivery=state.delivery,
        )

    async def _handle_flow_run_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: FlowRunCallback,
    ) -> None:
        state = self._flow_run_options.get(key)
        if not state or not _selection_contains(state.items, parsed.run_id):
            await self._answer_callback(callback, "Selection expired")
            return
        self._flow_run_options.pop(key, None)
        await self._handle_flow_callback(
            callback, FlowCallback(action="status", run_id=parsed.run_id)
        )

    def _selection_prompt(self, base: str, state: SelectionState) -> str:
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        return _format_selection_prompt(base, state.page, total_pages)

    def _page_button(
        self, kind: str, state: SelectionState
    ) -> Optional[tuple[str, str]]:
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        if total_pages <= 1:
            return None
        next_page = (state.page + 1) % total_pages
        return ("More...", encode_page_callback(kind, next_page))

    def _build_resume_keyboard(self, state: SelectionState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = []
        for idx, (item_id, label) in enumerate(page_items, 1):
            button_label = self._resume_button_label(state, item_id, label)
            options.append(
                (
                    item_id,
                    f"{idx}) {_compact_preview(button_label, RESUME_BUTTON_PREVIEW_LIMIT)}",
                )
            )
        return build_resume_keyboard(
            options,
            page_button=self._page_button("resume", state),
            include_cancel=True,
        )

    def _resume_button_label(
        self, state: SelectionState, item_id: str, label: str
    ) -> str:
        if state.button_labels:
            button_label = state.button_labels.get(item_id)
            if isinstance(button_label, str) and button_label.strip():
                return button_label
        return label

    def _build_bind_keyboard(self, state: SelectionState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_bind_keyboard(
            options,
            page_button=self._page_button("bind", state),
            include_cancel=True,
        )

    def _build_update_keyboard(self, state: SelectionState) -> dict[str, Any]:
        options = list(state.items)
        return build_update_keyboard(options, include_cancel=True)

    def _build_agent_keyboard(self, state: SelectionState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_agent_keyboard(
            options,
            page_button=self._page_button("agent", state),
            include_cancel=True,
        )

    def _build_model_keyboard(self, state: ModelPickerState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_model_keyboard(
            options,
            page_button=self._page_button("model", state),
            include_cancel=True,
        )

    def _build_review_commit_keyboard(
        self, state: ReviewCommitSelectionState
    ) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = [
            (item_id, f"{idx}) {label}")
            for idx, (item_id, label) in enumerate(page_items, 1)
        ]
        return build_review_commit_keyboard(
            options,
            page_button=self._page_button("review-commit", state),
            include_cancel=True,
        )

    def _build_flow_runs_keyboard(self, state: SelectionState) -> dict[str, Any]:
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        options = []
        for idx, (item_id, label) in enumerate(page_items, 1):
            button_label = label
            if state.button_labels:
                button_label = state.button_labels.get(item_id, label)
            options.append(
                (
                    item_id,
                    f"{idx}) {_compact_preview(button_label, RESUME_BUTTON_PREVIEW_LIMIT)}",
                )
            )
        return build_flow_runs_keyboard(
            options,
            page_button=self._page_button("flow-runs", state),
            include_cancel=True,
        )

    def _flow_runs_prompt(self, state: SelectionState) -> str:
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
        lines = [FLOW_RUNS_PICKER_PROMPT]
        for run_id, label in page_items:
            if label:
                lines.append(f"- `{run_id}` — {label}")
            else:
                lines.append(f"- `{run_id}`")
        base = "\n".join(lines)
        return _format_selection_prompt(base, state.page, total_pages)

    def _build_effort_keyboard(self, option: ModelOption) -> dict[str, Any]:
        options = []
        for effort in option.efforts:
            label = effort
            if option.default_effort and effort == option.default_effort:
                label = f"{effort} (default)"
            options.append((effort, label))
        return build_effort_keyboard(options, include_cancel=True)

    async def _update_selection_message(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        text: str,
        reply_markup: dict[str, Any],
    ) -> None:
        if await self._edit_callback_message(callback, text, reply_markup=reply_markup):
            return
        chat_id, thread_id = _split_topic_key(key)
        await self._send_message(
            chat_id,
            text,
            thread_id=thread_id,
            reply_markup=reply_markup,
        )

    async def _finalize_selection(
        self,
        key: str,
        callback: Optional[TelegramCallbackQuery],
        text: str,
    ) -> None:
        if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            if callback and await self._edit_callback_message(
                callback,
                "Selection complete.",
                reply_markup={"inline_keyboard": []},
            ):
                chat_id, thread_id = _split_topic_key(key)
                await self._send_message(chat_id, text, thread_id=thread_id)
                return
        if callback and await self._edit_callback_message(
            callback, text, reply_markup={"inline_keyboard": []}
        ):
            return
        chat_id, thread_id = _split_topic_key(key)
        await self._send_message(chat_id, text, thread_id=thread_id)

    async def _handle_selection_cancel(
        self,
        key: str,
        parsed: CancelCallback,
        callback: TelegramCallbackQuery,
    ) -> None:
        if parsed.kind == "resume":
            self._resume_options.pop(key, None)
            text = "Resume selection cancelled."
        elif parsed.kind == "bind":
            self._bind_options.pop(key, None)
            text = "Bind selection cancelled."
        elif parsed.kind == "agent":
            self._agent_options.pop(key, None)
            text = "Agent selection cancelled."
        elif parsed.kind == "model":
            self._model_options.pop(key, None)
            self._model_pending.pop(key, None)
            text = "Model selection cancelled."
        elif parsed.kind == "update":
            self._update_options.pop(key, None)
            text = "Update cancelled."
        elif parsed.kind == "update-confirm":
            self._update_confirm_options.pop(key, None)
            text = "Update cancelled."
        elif parsed.kind == "review-commit":
            self._review_commit_options.pop(key, None)
            self._review_commit_subjects.pop(key, None)
            text = "Review commit selection cancelled."
        elif parsed.kind == "review-custom":
            self._pending_review_custom.pop(key, None)
            text = "Custom review cancelled."
        elif parsed.kind == "flow-runs":
            self._flow_run_options.pop(key, None)
            text = "Flow run selection cancelled."
        else:
            await self._answer_callback(callback, "Selection expired")
            return
        await self._answer_callback(callback, "Cancelled")
        await self._finalize_selection(key, callback, text)

    async def _handle_selection_page(
        self,
        key: str,
        parsed: PageCallback,
        callback: TelegramCallbackQuery,
    ) -> None:
        build_keyboard: Callable[[SelectionState], dict[str, Any]]
        if parsed.kind == "resume":
            state = self._resume_options.get(key)
            prompt_base = RESUME_PICKER_PROMPT
            build_keyboard = self._build_resume_keyboard
        elif parsed.kind == "bind":
            state = self._bind_options.get(key)
            prompt_base = BIND_PICKER_PROMPT
            build_keyboard = self._build_bind_keyboard
        elif parsed.kind == "agent":
            state = self._agent_options.get(key)
            prompt_base = AGENT_PICKER_PROMPT
            build_keyboard = self._build_agent_keyboard
        elif parsed.kind == "model":
            state = self._model_options.get(key)
            prompt_base = MODEL_PICKER_PROMPT
            build_keyboard = cast(
                Callable[[SelectionState], dict[str, Any]],
                self._build_model_keyboard,
            )
        elif parsed.kind == "review-commit":
            state = self._review_commit_options.get(key)
            prompt_base = REVIEW_COMMIT_PICKER_PROMPT
            build_keyboard = cast(
                Callable[[SelectionState], dict[str, Any]],
                self._build_review_commit_keyboard,
            )
        elif parsed.kind == "flow-runs":
            state = self._flow_run_options.get(key)
            prompt_base = ""
            build_keyboard = self._build_flow_runs_keyboard
        else:
            await self._answer_callback(callback, "Selection expired")
            return
        if not state:
            await self._answer_callback(callback, "Selection expired")
            return
        total_pages = _page_count(len(state.items), DEFAULT_PAGE_SIZE)
        if total_pages <= 1:
            await self._answer_callback(callback, "No more pages")
            return
        page = parsed.page % total_pages
        state.page = page
        if parsed.kind == "flow-runs":
            prompt = self._flow_runs_prompt(state)
        else:
            prompt = _format_selection_prompt(prompt_base, page, total_pages)
        keyboard = build_keyboard(state)
        await self._update_selection_message(key, callback, prompt, keyboard)
        await self._answer_callback(callback, f"Page {page + 1}/{total_pages}")
