// GENERATED FILE - do not edit directly. Source: static_src/
import { api, escapeHtml, flash, getUrlParams, resolvePath, updateUrlParams, } from "./utils.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
let bellInitialized = false;
let messagesInitialized = false;
let activeRunId = null;
let selectedRunId = null;
const threadsEl = document.getElementById("messages-thread-list");
const detailEl = document.getElementById("messages-thread-detail");
const refreshEl = document.getElementById("messages-refresh");
const replyBodyEl = document.getElementById("messages-reply-body");
const replyFilesEl = document.getElementById("messages-reply-files");
const replySendEl = document.getElementById("messages-reply-send");
function formatTimestamp(ts) {
    if (!ts)
        return "–";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime()))
        return ts;
    return date.toLocaleString();
}
function setBadge(count) {
    const badge = document.getElementById("tab-badge-inbox");
    if (!badge)
        return;
    if (count > 0) {
        badge.textContent = String(count);
        badge.classList.remove("hidden");
    }
    else {
        badge.textContent = "";
        badge.classList.add("hidden");
    }
}
export async function refreshBell() {
    if (!isRepoHealthy()) {
        activeRunId = null;
        setBadge(0);
        return;
    }
    try {
        const res = (await api("/api/messages/active"));
        if (res?.active && res.run_id) {
            activeRunId = res.run_id;
            setBadge(1);
        }
        else {
            activeRunId = null;
            setBadge(0);
        }
    }
    catch (_err) {
        // Best-effort.
        activeRunId = null;
        setBadge(0);
    }
}
export function initMessageBell() {
    if (bellInitialized)
        return;
    bellInitialized = true;
    // Cheap polling. (The repo shell already does other polling; keep this light.)
    refreshBell();
    window.setInterval(() => {
        if (document.hidden)
            return;
        if (!isRepoHealthy())
            return;
        refreshBell();
    }, 15000);
    subscribe("repo:health", (payload) => {
        const status = payload?.status || "";
        if (status === "ok" || status === "degraded") {
            void refreshBell();
        }
    });
}
function renderThreadItem(thread) {
    const latestDispatch = thread.latest?.dispatch;
    const isHandoff = latestDispatch?.is_handoff || latestDispatch?.mode === "pause";
    const title = latestDispatch?.title || (isHandoff ? "Handoff" : "Dispatch");
    const subtitle = latestDispatch?.body ? latestDispatch.body.slice(0, 120) : "";
    const isPaused = thread.status === "paused";
    // Only show action indicator if there's an unreplied handoff (pause)
    // Compare dispatch_seq vs reply_seq to check if user has responded
    const ticketState = thread.ticket_state;
    const dispatchSeq = ticketState?.dispatch_seq ?? 0;
    const replySeq = ticketState?.reply_seq ?? 0;
    const hasUnrepliedHandoff = isPaused && (dispatchSeq > replySeq || (isHandoff && replySeq === 0));
    const indicator = hasUnrepliedHandoff ? `<span class="messages-thread-indicator" title="Action required"></span>` : "";
    const dispatches = thread.dispatch_count ?? 0;
    const replies = thread.reply_count ?? 0;
    const metaLine = `${dispatches} dispatch${dispatches !== 1 ? "es" : ""} · ${replies} repl${replies !== 1 ? "ies" : "y"}`;
    return `
    <button class="messages-thread" data-run-id="${escapeHtml(thread.run_id)}">
      <div class="messages-thread-title">${indicator}${escapeHtml(title)}</div>
      <div class="messages-thread-subtitle muted">${escapeHtml(subtitle)}</div>
      <div class="messages-thread-meta-line">${escapeHtml(metaLine)}</div>
    </button>
  `;
}
async function loadThreads() {
    if (!threadsEl)
        return;
    threadsEl.innerHTML = "Loading…";
    if (!isRepoHealthy()) {
        threadsEl.innerHTML = "<div class=\"muted\">Repo offline or uninitialized</div>";
        return;
    }
    let res;
    try {
        res = (await api("/api/messages/threads"));
    }
    catch (err) {
        threadsEl.innerHTML = "";
        flash("Failed to load inbox", "error");
        return;
    }
    const conversations = res?.conversations || [];
    if (!conversations.length) {
        threadsEl.innerHTML = "<div class=\"muted\">No dispatches</div>";
        return;
    }
    threadsEl.innerHTML = conversations.map(renderThreadItem).join("");
    threadsEl.querySelectorAll(".messages-thread").forEach((btn) => {
        btn.addEventListener("click", () => {
            const runId = btn.dataset.runId || "";
            if (!runId)
                return;
            updateUrlParams({ tab: "inbox", run_id: runId });
            void loadThread(runId);
        });
    });
}
function formatBytes(size) {
    if (typeof size !== "number" || Number.isNaN(size))
        return "";
    if (size >= 1000000)
        return `${(size / 1000000).toFixed(1)} MB`;
    if (size >= 1000)
        return `${(size / 1000).toFixed(0)} KB`;
    return `${size} B`;
}
export function renderMarkdown(body) {
    if (!body)
        return "";
    let text = escapeHtml(body);
    // Extract fenced code blocks to avoid mutating their contents later.
    const codeBlocks = [];
    text = text.replace(/```([\s\S]*?)```/g, (_m, code) => {
        const placeholder = `@@CODEBLOCK_${codeBlocks.length}@@`;
        codeBlocks.push(`<pre class="md-code"><code>${code}</code></pre>`);
        return placeholder;
    });
    // Inline code
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Bold and italic (simple, non-nested)
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    // Links [text](url) only for http/https
    text = text.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, (_m, label, url) => {
        return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
    });
    // Lists (skip placeholders so code fences remain untouched)
    const lines = text.split(/\n/);
    const out = [];
    let inList = false;
    lines.forEach((line) => {
        if (/^@@CODEBLOCK_\d+@@$/.test(line)) {
            if (inList) {
                out.push("</ul>");
                inList = false;
            }
            out.push(line);
            return;
        }
        if (/^[-*]\s+/.test(line)) {
            if (!inList) {
                out.push("<ul>");
                inList = true;
            }
            out.push(`<li>${line.replace(/^[-*]\s+/, "")}</li>`);
        }
        else {
            if (inList) {
                out.push("</ul>");
                inList = false;
            }
            out.push(line);
        }
    });
    if (inList)
        out.push("</ul>");
    // Paragraphs and placeholder restoration
    const joined = out.join("\n");
    return joined
        .split(/\n\n+/)
        .map((block) => {
        const match = block.match(/^@@CODEBLOCK_(\d+)@@$/);
        if (match) {
            const idx = Number(match[1]);
            return codeBlocks[idx] ?? "";
        }
        return `<p>${block.replace(/\n/g, "<br>")}</p>`;
    })
        .join("");
}
function renderFiles(files) {
    if (!files || !files.length)
        return "";
    const items = files
        .map((f) => {
        const size = formatBytes(f.size);
        const href = resolvePath(f.url || "");
        return `<li class="messages-file">
        <span class="messages-file-icon">📎</span>
        <a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(f.name)}</a>
        ${size ? `<span class="messages-file-size muted small">${escapeHtml(size)}</span>` : ""}
      </li>`;
    })
        .join("");
    return `<ul class="messages-files">${items}</ul>`;
}
function renderDispatch(entry, isLatest, runStatus) {
    const dispatch = entry.dispatch;
    const isHandoff = dispatch?.is_handoff || dispatch?.mode === "pause";
    const title = dispatch?.title || (isHandoff ? "Handoff" : "Agent update");
    let modeClass = "pill-info";
    let modeLabel = "INFO";
    if (isHandoff) {
        // Only show "ACTION REQUIRED" if this is the latest dispatch AND the run is actually paused.
        // Otherwise, show "HANDOFF" to indicate a historical pause point.
        if (isLatest && runStatus === "paused") {
            modeClass = "pill-action";
            modeLabel = "ACTION REQUIRED";
        }
        else {
            modeClass = "pill-idle";
            modeLabel = "HANDOFF";
        }
    }
    const modePill = dispatch?.mode ? ` <span class="pill pill-small ${modeClass}">${escapeHtml(modeLabel)}</span>` : "";
    const body = dispatch?.body ? `<div class="messages-body messages-markdown">${renderMarkdown(dispatch.body)}</div>` : "";
    const ts = entry.created_at ? formatTimestamp(entry.created_at) : "";
    return `
    <div class="messages-entry" data-seq="${entry.seq}" data-type="dispatch" data-created="${escapeHtml(entry.created_at || "")}">
      <div class="messages-entry-header">
        <span class="messages-entry-seq">#${entry.seq.toString().padStart(4, "0")}</span>
        <span class="messages-entry-title">${escapeHtml(title)}</span>
        ${modePill}
        <span class="messages-entry-time">${escapeHtml(ts)}</span>
      </div>
      ${body}
      ${renderFiles(entry.files)}
    </div>
  `;
}
function renderReply(entry, parentSeq) {
    const rep = entry.reply;
    const title = rep?.title || "Your reply";
    const body = rep?.body ? `<div class="messages-body messages-markdown">${renderMarkdown(rep.body)}</div>` : "";
    const ts = entry.created_at ? formatTimestamp(entry.created_at) : "";
    const replyIndicator = parentSeq !== undefined
        ? `<div class="messages-reply-indicator">In response to #${parentSeq.toString().padStart(4, "0")}</div>`
        : "";
    return `
    <div class="messages-entry messages-entry-reply" data-seq="${entry.seq}" data-type="reply" data-created="${escapeHtml(entry.created_at || "")}">
      ${replyIndicator}
      <div class="messages-entry-header">
        <span class="messages-entry-seq">#${entry.seq.toString().padStart(4, "0")}</span>
        <span class="messages-entry-title">${escapeHtml(title)}</span>
        <span class="pill pill-small pill-idle">you</span>
        <span class="messages-entry-time">${escapeHtml(ts)}</span>
      </div>
      ${body}
      ${renderFiles(entry.files)}
    </div>
  `;
}
function buildThreadedTimeline(dispatches, replies, runStatus) {
    // Combine all entries into a single timeline
    const timeline = [];
    // Find the latest dispatch sequence number to identify the most recent agent message
    let maxDispatchSeq = -1;
    dispatches.forEach((d) => {
        if (d.seq > maxDispatchSeq)
            maxDispatchSeq = d.seq;
        timeline.push({
            type: "dispatch",
            seq: d.seq,
            created_at: d.created_at || null,
            dispatch: d,
        });
    });
    replies.forEach((r) => {
        timeline.push({
            type: "reply",
            seq: r.seq,
            created_at: r.created_at || null,
            reply: r,
        });
    });
    // Sort chronologically by created_at, fallback to seq
    timeline.sort((a, b) => {
        if (a.created_at && b.created_at) {
            return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
        }
        return a.seq - b.seq;
    });
    // Render timeline, associating replies with preceding dispatches
    let lastDispatchSeq;
    const rendered = [];
    timeline.forEach((entry) => {
        if (entry.type === "dispatch" && entry.dispatch) {
            lastDispatchSeq = entry.dispatch.seq;
            const isLatest = entry.dispatch.seq === maxDispatchSeq;
            rendered.push(renderDispatch(entry.dispatch, isLatest, runStatus));
        }
        else if (entry.type === "reply" && entry.reply) {
            rendered.push(renderReply(entry.reply, lastDispatchSeq));
        }
    });
    return rendered.join("");
}
async function loadThread(runId) {
    selectedRunId = runId;
    if (!detailEl)
        return;
    detailEl.innerHTML = "Loading…";
    if (!isRepoHealthy()) {
        detailEl.innerHTML = "<div class=\"muted\">Repo offline or uninitialized.</div>";
        return;
    }
    let detail;
    try {
        detail = (await api(`/api/messages/threads/${encodeURIComponent(runId)}`));
    }
    catch (_err) {
        detailEl.innerHTML = "";
        flash("Failed to load message thread", "error");
        return;
    }
    const runStatus = (detail.run?.status || "").toString();
    const isPaused = runStatus === "paused";
    const dispatchHistory = detail.dispatch_history || [];
    const replyHistory = detail.reply_history || [];
    const dispatchCount = detail.dispatch_count ?? dispatchHistory.length;
    const replyCount = detail.reply_count ?? replyHistory.length;
    const ticketState = detail.ticket_state;
    const turns = ticketState?.total_turns ?? null;
    // Truncate run ID for display
    const shortRunId = runId.length > 12 ? runId.slice(0, 8) + "…" : runId;
    // Build compact stats line
    const statsParts = [];
    statsParts.push(`${dispatchCount} dispatch${dispatchCount !== 1 ? "es" : ""}`);
    statsParts.push(`${replyCount} repl${replyCount !== 1 ? "ies" : "y"}`);
    if (turns != null)
        statsParts.push(`${turns} turn${turns !== 1 ? "s" : ""}`);
    const statsLine = statsParts.join(" · ");
    // Status pill
    const statusPillClass = isPaused ? "pill-action" : "pill-idle";
    const statusLabel = isPaused ? "paused" : runStatus || "idle";
    // Build threaded timeline
    const threadedContent = buildThreadedTimeline(dispatchHistory, replyHistory, runStatus);
    detailEl.innerHTML = `
    <div class="messages-thread-history">
      ${threadedContent || '<div class="muted">No dispatches yet</div>'}
    </div>
    <div class="messages-thread-footer">
      <code title="${escapeHtml(runId)}">${escapeHtml(shortRunId)}</code>
      <span class="pill pill-small ${statusPillClass}">${escapeHtml(statusLabel)}</span>
      <span class="messages-footer-stats">${escapeHtml(statsLine)}</span>
    </div>
  `;
    // Only show reply box for paused runs - replies to other states won't be seen
    const replyBoxEl = document.querySelector(".messages-reply-box");
    if (replyBoxEl) {
        replyBoxEl.classList.toggle("hidden", !isPaused);
    }
    // Always scroll to bottom of the thread detail (the scrollable container)
    requestAnimationFrame(() => {
        if (detailEl) {
            detailEl.scrollTop = detailEl.scrollHeight;
        }
    });
}
async function sendReply() {
    const runId = selectedRunId;
    if (!runId) {
        flash("Select a message thread first", "error");
        return;
    }
    if (!isRepoHealthy()) {
        flash("Repo offline; cannot send reply.", "error");
        return;
    }
    const body = replyBodyEl?.value || "";
    const fd = new FormData();
    fd.append("body", body);
    if (replyFilesEl?.files) {
        Array.from(replyFilesEl.files).forEach((f) => fd.append("files", f));
    }
    try {
        await api(`/api/messages/${encodeURIComponent(runId)}/reply`, {
            method: "POST",
            body: fd,
        });
        if (replyBodyEl)
            replyBodyEl.value = "";
        if (replyFilesEl)
            replyFilesEl.value = "";
        flash("Reply sent", "success");
        // Always resume after sending
        await api(`/api/flows/${encodeURIComponent(runId)}/resume`, { method: "POST" });
        flash("Run resumed", "success");
        void refreshBell();
        void loadThread(runId);
    }
    catch (_err) {
        flash("Failed to send reply", "error");
    }
}
export function initMessages() {
    if (messagesInitialized)
        return;
    if (!threadsEl || !detailEl)
        return;
    messagesInitialized = true;
    refreshEl?.addEventListener("click", () => {
        void loadThreads();
        const runId = selectedRunId;
        if (runId)
            void loadThread(runId);
    });
    replySendEl?.addEventListener("click", () => {
        void sendReply();
    });
    // Load threads immediately, and try to open run_id from URL if present.
    void loadThreads().then(() => {
        const params = getUrlParams();
        const runId = params.get("run_id");
        if (runId) {
            selectedRunId = runId;
            void loadThread(runId);
            return;
        }
        // Fall back to active message if any.
        if (activeRunId) {
            selectedRunId = activeRunId;
            updateUrlParams({ run_id: activeRunId });
            void loadThread(activeRunId);
        }
    });
    subscribe("tab:change", (tabId) => {
        if (tabId === "inbox") {
            void refreshBell();
            void loadThreads();
            const params = getUrlParams();
            const runId = params.get("run_id");
            if (runId) {
                selectedRunId = runId;
                void loadThread(runId);
            }
        }
    });
    subscribe("state:update", () => {
        void refreshBell();
    });
}
