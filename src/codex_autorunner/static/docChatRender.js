// GENERATED FILE - do not edit directly. Source: static_src/
import { flash, statusPill, isMobileViewport } from "./utils.js";
import { chatUI } from "./docsElements.js";
import { CHAT_HISTORY_LIMIT, getActiveDoc, getChatState, getDraft, isDraftPreview, setHistoryNavIndex, } from "./docsState.js";
import { autoResizeTextarea, formatDraftTimestamp, renderDiffHtml, updateDocControls, updateDocVisibility, } from "./docsUi.js";
import { renderChatEvents } from "./docChatEvents.js";
export function updatePatchPreviewFromDraft(draft) {
    if (!chatUI.patchBody || !draft || !draft.patch)
        return;
    chatUI.patchBody.innerHTML = renderDiffHtml(draft.patch);
}
export function renderChat() {
    const state = getChatState();
    const latest = state.history[0];
    const isRunning = state.status === "running";
    const hasError = !!state.error;
    const pillState = isRunning
        ? "running"
        : state.status === "error"
            ? "error"
            : state.status === "interrupted"
                ? "interrupted"
                : "idle";
    if (chatUI.status) {
        statusPill(chatUI.status, pillState);
    }
    if (chatUI.send) {
        chatUI.send.disabled = isRunning;
    }
    if (chatUI.input) {
        chatUI.input.disabled = isRunning;
    }
    if (chatUI.cancel) {
        chatUI.cancel.classList.toggle("hidden", !isRunning);
    }
    if (chatUI.voiceBtn) {
        chatUI.voiceBtn.disabled =
            isRunning && !chatUI.voiceBtn.classList.contains("voice-retry");
        chatUI.voiceBtn.classList.toggle("disabled", chatUI.voiceBtn.disabled);
        if (typeof chatUI.voiceBtn.setAttribute === "function") {
            chatUI.voiceBtn.setAttribute("aria-disabled", chatUI.voiceBtn.disabled ? "true" : "false");
        }
    }
    if (chatUI.newThread) {
        chatUI.newThread.disabled = isRunning;
        chatUI.newThread.classList.toggle("disabled", isRunning);
    }
    if (chatUI.hint) {
        if (isRunning) {
            const statusText = state.statusText || "processing";
            chatUI.hint.textContent = statusText;
            chatUI.hint.classList.add("loading");
        }
        else {
            const sendHint = isMobileViewport()
                ? "Tap Send to send · Enter for newline"
                : "Cmd+Enter / Ctrl+Enter to send · Enter for newline";
            chatUI.hint.textContent = sendHint;
            chatUI.hint.classList.remove("loading");
        }
    }
    if (hasError) {
        chatUI.error.textContent = state.error;
        chatUI.error.classList.remove("hidden");
    }
    else {
        chatUI.error.textContent = "";
        chatUI.error.classList.add("hidden");
    }
    const activeDoc = getActiveDoc();
    const latestDrafts = latest?.drafts;
    const draft = getDraft(activeDoc) || (latestDrafts?.[activeDoc] || null);
    const hasPatch = !!(draft && (draft.patch || "").trim());
    const previewing = hasPatch && isDraftPreview(activeDoc);
    if (chatUI.patchMain) {
        chatUI.patchMain.classList.toggle("hidden", !hasPatch);
        chatUI.patchMain.classList.toggle("previewing", previewing);
        chatUI.patchBody.innerHTML = hasPatch
            ? renderDiffHtml(draft.patch)
            : "(no draft)";
        if (hasPatch) {
            chatUI.patchSummary.textContent =
                draft?.agentMessage ||
                    latest?.response ||
                    state.error ||
                    "Draft ready";
        }
        else {
            chatUI.patchSummary.textContent = "";
        }
        if (chatUI.patchMeta) {
            const metaParts = [];
            if (hasPatch && draft?.createdAt) {
                metaParts.push(`drafted ${formatDraftTimestamp(draft.createdAt)}`);
            }
            if (hasPatch && draft?.baseHash) {
                metaParts.push(`base ${draft.baseHash.slice(0, 7)}`);
            }
            chatUI.patchMeta.textContent = metaParts.join(" · ");
        }
        if (chatUI.patchApply)
            chatUI.patchApply.disabled = isRunning || !hasPatch;
        if (chatUI.patchDiscard)
            chatUI.patchDiscard.disabled = isRunning || !hasPatch;
        if (chatUI.patchReload)
            chatUI.patchReload.disabled = isRunning;
        if (chatUI.patchPreview) {
            chatUI.patchPreview.disabled = isRunning || !hasPatch;
            chatUI.patchPreview.textContent = previewing
                ? "Hide preview"
                : "Preview draft";
            chatUI.patchPreview.classList.toggle("active", previewing);
            chatUI.patchPreview.setAttribute("aria-pressed", previewing ? "true" : "false");
        }
    }
    updateDocVisibility();
    updateDocControls(activeDoc);
    renderChatEvents(state);
    renderChatHistory(state);
}
export function renderChatHistory(state) {
    if (!chatUI.history)
        return;
    const count = state.history.length;
    chatUI.historyCount.textContent = String(count);
    chatUI.history.innerHTML = "";
    if (count === 0) {
        const empty = document.createElement("div");
        empty.className = "doc-chat-empty";
        empty.textContent = "No messages yet.";
        chatUI.history.appendChild(empty);
        return;
    }
    state.history.slice(0, CHAT_HISTORY_LIMIT).forEach((entry) => {
        const wrapper = document.createElement("div");
        wrapper.className = `doc-chat-entry ${entry.status}`;
        const header = document.createElement("div");
        header.className = "doc-chat-entry-header";
        const promptRow = document.createElement("div");
        promptRow.className = "prompt-row";
        const prompt = document.createElement("div");
        prompt.className = "prompt";
        prompt.textContent = entry.prompt || "(no prompt)";
        prompt.title = entry.prompt;
        const copyBtn = document.createElement("button");
        copyBtn.className = "copy-prompt-btn";
        copyBtn.title = "Copy to input";
        copyBtn.innerHTML = "↑";
        copyBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            if (chatUI.input)
                chatUI.input.value = entry.prompt;
            autoResizeTextarea(chatUI.input);
            chatUI.input?.focus();
            setHistoryNavIndex(-1);
            flash("Prompt restored to input");
        });
        promptRow.appendChild(prompt);
        promptRow.appendChild(copyBtn);
        const meta = document.createElement("div");
        meta.className = "meta";
        const dot = document.createElement("span");
        dot.className = "status-dot";
        const stamp = document.createElement("span");
        stamp.textContent = entry.time
            ? new Date(entry.time).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
            })
            : entry.status;
        meta.appendChild(dot);
        meta.appendChild(stamp);
        header.appendChild(promptRow);
        header.appendChild(meta);
        const response = document.createElement("div");
        response.className = "doc-chat-entry-response";
        const isLatest = entry === state.history[0];
        const runningText = (isLatest && state.streamText) ||
            entry.response ||
            (isLatest && state.statusText) ||
            "queued";
        const responseText = entry.error ||
            (entry.status === "running" ? runningText : entry.response || "(no response)");
        response.textContent = responseText;
        response.classList.toggle("streaming", entry.status === "running" && !!(state.streamText || entry.response));
        wrapper.appendChild(header);
        wrapper.appendChild(response);
        const tags = [];
        if (entry.viewing) {
            tags.push(`Viewing: ${entry.viewing.toUpperCase()}`);
        }
        else if (entry.targets && entry.targets.length) {
            tags.push(`Targets: ${entry.targets.map((k) => k.toUpperCase()).join(", ")}`);
        }
        if (entry.updated && entry.updated.length) {
            tags.push(`Drafts: ${entry.updated.map((k) => k.toUpperCase()).join(", ")}`);
        }
        if (tags.length) {
            const tagLine = document.createElement("div");
            tagLine.className = "doc-chat-entry-tags";
            tagLine.textContent = tags.join(" · ");
            wrapper.appendChild(tagLine);
        }
        chatUI.history.appendChild(wrapper);
    });
}
