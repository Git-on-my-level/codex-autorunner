// GENERATED FILE - do not edit directly. Source: static_src/
import { api, escapeHtml, flash, getUrlParams, resolvePath, updateUrlParams, } from "./utils.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
import { preserveScroll } from "./preserve.js";
import { createSmartRefresh } from "./smartRefresh.js";
import { createFileBoxWidget } from "./fileboxUi.js";
let bellInitialized = false;
let messagesInitialized = false;
let activeRunId = null;
let selectedRunId = null;
const MESSAGE_REFRESH_REASONS = ["initial", "background", "manual"];
const threadsEl = document.getElementById("messages-thread-list");
const detailEl = document.getElementById("messages-thread-detail");
const layoutEl = document.querySelector(".messages-layout");
const backBtn = document.getElementById("messages-back-btn");
const refreshEl = document.getElementById("messages-refresh");
const replyBodyEl = document.getElementById("messages-reply-body");
const replyFilesEl = document.getElementById("messages-reply-files");
const replySendEl = document.getElementById("messages-reply-send");
const fileBoxInboxEl = document.getElementById("messages-filebox-inbox");
const fileBoxOutboxEl = document.getElementById("messages-filebox-outbox");
const fileBoxUploadEl = document.getElementById("messages-filebox-upload");
const fileBoxUploadBtn = document.getElementById("messages-filebox-upload-btn");
const fileBoxRefreshBtn = document.getElementById("messages-filebox-refresh");
let threadListRefreshCount = 0;
let threadDetailRefreshCount = 0;
let fileBoxCtrl = null;
function isMobileViewport() {
    return window.innerWidth <= 640;
}
function showThreadList() {
    layoutEl?.classList.remove("viewing-detail");
}
function showThreadDetail() {
    if (isMobileViewport()) {
        layoutEl?.classList.add("viewing-detail");
    }
}
function initFileBox() {
    if (fileBoxCtrl || (!fileBoxInboxEl && !fileBoxOutboxEl))
        return;
    fileBoxCtrl = createFileBoxWidget({
        scope: "repo",
        inboxEl: fileBoxInboxEl,
        outboxEl: fileBoxOutboxEl,
        uploadInput: fileBoxUploadEl,
        uploadBtn: fileBoxUploadBtn,
        refreshBtn: fileBoxRefreshBtn,
        uploadBox: "inbox",
        emptyMessage: "No files",
    });
    void fileBoxCtrl.refresh();
}
function setThreadListRefreshing(active) {
    if (!threadsEl)
        return;
    threadListRefreshCount = Math.max(0, threadListRefreshCount + (active ? 1 : -1));
    threadsEl.classList.toggle("refreshing", threadListRefreshCount > 0);
}
function setThreadDetailRefreshing(active) {
    if (!detailEl)
        return;
    threadDetailRefreshCount = Math.max(0, threadDetailRefreshCount + (active ? 1 : -1));
    detailEl.classList.toggle("refreshing", threadDetailRefreshCount > 0);
}
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
function formatRelativeTime(ts) {
    if (!ts)
        return "";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime()))
        return "";
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffSecs = Math.floor(diffMs / 1000);
    if (diffSecs < 60)
        return "just now";
    const diffMins = Math.floor(diffSecs / 60);
    if (diffMins < 60)
        return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24)
        return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 7)
        return `${diffDays}d ago`;
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
}
function getStatusPillClass(status) {
    switch (status) {
        case "paused":
            return "pill-action";
        case "running":
        case "pending":
            return "pill-success";
        case "completed":
            return "pill-idle";
        case "failed":
        case "stopped":
            return "pill-error";
        default:
            return "pill-idle";
    }
}
function renderThreadItem(thread) {
    const latestDispatch = thread.latest?.dispatch;
    const isHandoff = latestDispatch?.is_handoff || latestDispatch?.mode === "pause";
    const title = latestDispatch?.title || (isHandoff ? "Handoff" : "Dispatch");
    const subtitle = latestDispatch?.body ? latestDispatch.body.slice(0, 120) : "";
    const isPaused = thread.status === "paused";
    const isActive = selectedRunId && thread.run_id === selectedRunId;
    // Only show action indicator if there's an unreplied handoff (pause)
    // Compare dispatch_seq vs reply_seq to check if user has responded
    const ticketState = thread.ticket_state;
    const dispatchSeq = ticketState?.dispatch_seq ?? 0;
    const replySeq = ticketState?.reply_seq ?? 0;
    const hasUnrepliedHandoff = isPaused && (dispatchSeq > replySeq || (isHandoff && replySeq === 0));
    const indicator = hasUnrepliedHandoff ? `<span class="messages-thread-indicator" title="Action required"></span>` : "";
    const dispatches = thread.dispatch_count ?? 0;
    const replies = thread.reply_count ?? 0;
    // Format timestamp for last dispatch
    const lastTs = thread.latest?.created_at;
    const timeAgo = formatRelativeTime(lastTs);
    // Status badge
    const status = thread.status || "idle";
    const statusClass = getStatusPillClass(status);
    const statusLabel = status === "paused" && hasUnrepliedHandoff ? "action" : status;
    // Build meta line with timestamp
    const countPart = `${dispatches} dispatch${dispatches !== 1 ? "es" : ""} · ${replies} repl${replies !== 1 ? "ies" : "y"}`;
    return `
    <button class="messages-thread${isActive ? " active" : ""}" data-run-id="${escapeHtml(thread.run_id)}">
      <div class="messages-thread-header">
        <div class="messages-thread-title">${indicator}${escapeHtml(title)}</div>
        <span class="pill pill-small ${statusClass}">${escapeHtml(statusLabel)}</span>
      </div>
      <div class="messages-thread-subtitle muted">${escapeHtml(subtitle)}</div>
      <div class="messages-thread-meta-line">
        <span class="messages-thread-counts">${escapeHtml(countPart)}</span>
        ${timeAgo ? `<span class="messages-thread-time">${escapeHtml(timeAgo)}</span>` : ""}
      </div>
    </button>
  `;
}
function syncSelectedThread() {
    if (!threadsEl)
        return;
    const buttons = threadsEl.querySelectorAll(".messages-thread");
    buttons.forEach((btn) => {
        const runId = btn.dataset.runId || "";
        btn.classList.toggle("active", Boolean(runId) && runId === selectedRunId);
    });
}
function threadListSignature(conversations) {
    return conversations
        .map((thread) => {
        const latest = thread.latest;
        const dispatch = latest?.dispatch;
        const ticketState = thread.ticket_state;
        return [
            thread.run_id,
            thread.status ?? "",
            latest?.seq ?? "",
            latest?.created_at ?? "",
            dispatch?.mode ?? "",
            dispatch?.is_handoff ? "1" : "0",
            thread.dispatch_count ?? "",
            thread.reply_count ?? "",
            ticketState?.dispatch_seq ?? "",
            ticketState?.reply_seq ?? "",
            ticketState?.status ?? "",
        ].join("|");
    })
        .join("::");
}
function threadDetailSignature(detail) {
    const dispatches = detail.dispatch_history || [];
    const replies = detail.reply_history || [];
    const maxDispatchSeq = dispatches.reduce((max, entry) => Math.max(max, entry.seq || 0), 0);
    const maxReplySeq = replies.reduce((max, entry) => Math.max(max, entry.seq || 0), 0);
    const lastDispatchAt = dispatches.find((entry) => entry.seq === maxDispatchSeq)?.created_at ?? "";
    const lastReplyAt = replies.find((entry) => entry.seq === maxReplySeq)?.created_at ?? "";
    const ticketState = detail.ticket_state;
    return [
        detail.run?.status ?? "",
        detail.run?.created_at ?? "",
        detail.dispatch_count ?? dispatches.length,
        detail.reply_count ?? replies.length,
        maxDispatchSeq,
        maxReplySeq,
        lastDispatchAt ?? "",
        lastReplyAt ?? "",
        ticketState?.dispatch_seq ?? "",
        ticketState?.reply_seq ?? "",
        ticketState?.status ?? "",
        ticketState?.current_ticket ?? "",
        ticketState?.total_turns ?? "",
        ticketState?.ticket_turns ?? "",
    ].join("|");
}
const threadListRefresh = createSmartRefresh({
    getSignature: (payload) => {
        if (payload.status !== "ok")
            return payload.status;
        return `ok::${threadListSignature(payload.conversations)}`;
    },
    render: (payload) => {
        if (!threadsEl)
            return;
        const renderList = () => {
            if (payload.status !== "ok") {
                threadsEl.innerHTML = "<div class=\"muted\">Repo offline or uninitialized</div>";
                return;
            }
            const conversations = payload.conversations || [];
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
                    selectedRunId = runId;
                    syncSelectedThread();
                    updateUrlParams({ tab: "inbox", run_id: runId });
                    showThreadDetail();
                    void loadThread(runId, "manual");
                });
            });
        };
        preserveScroll(threadsEl, renderList, { restoreOnNextFrame: true });
    },
});
const threadDetailRefresh = createSmartRefresh({
    getSignature: (payload) => {
        if (payload.status !== "ok")
            return `${payload.status}::${payload.runId}`;
        if (!payload.detail)
            return `empty::${payload.runId}`;
        return `ok::${payload.runId}::${threadDetailSignature(payload.detail)}`;
    },
    render: (payload, ctx) => {
        if (!detailEl)
            return;
        if (payload.status !== "ok") {
            detailEl.innerHTML = "<div class=\"muted\">Repo offline or uninitialized.</div>";
            return;
        }
        const detail = payload.detail;
        if (!detail) {
            detailEl.innerHTML = "<div class=\"muted\">No thread selected.</div>";
            return;
        }
        renderThreadDetail(detail, payload.runId, ctx);
    },
});
async function fetchThreadsPayload() {
    if (!isRepoHealthy()) {
        return { status: "offline", conversations: [] };
    }
    const res = (await api("/api/messages/threads"));
    return { status: "ok", conversations: res?.conversations || [] };
}
async function loadThreads(reason = "manual") {
    if (!threadsEl)
        return;
    if (!MESSAGE_REFRESH_REASONS.includes(reason)) {
        reason = "manual";
    }
    const showFullLoading = reason === "initial";
    if (showFullLoading) {
        threadsEl.innerHTML = "Loading…";
    }
    else {
        setThreadListRefreshing(true);
    }
    try {
        await threadListRefresh.refresh(fetchThreadsPayload, { reason });
    }
    catch (_err) {
        if (showFullLoading) {
            threadsEl.innerHTML = "";
        }
        flash("Failed to load inbox", "error");
    }
    finally {
        if (!showFullLoading) {
            setThreadListRefreshing(false);
        }
    }
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
function isSafeHref(url) {
    const trimmed = (url || "").trim();
    if (!trimmed)
        return false;
    const lower = trimmed.toLowerCase();
    if (lower.startsWith("javascript:"))
        return false;
    if (lower.startsWith("data:"))
        return false;
    if (lower.startsWith("vbscript:"))
        return false;
    if (lower.startsWith("file:"))
        return false;
    return (lower.startsWith("http://") ||
        lower.startsWith("https://") ||
        trimmed.startsWith("/") ||
        trimmed.startsWith("./") ||
        trimmed.startsWith("../") ||
        trimmed.startsWith("#") ||
        lower.startsWith("mailto:"));
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
    // Extract inline code to avoid linking inside it
    const inlineCode = [];
    text = text.replace(/`([^`]+)`/g, (_m, code) => {
        const placeholder = `@@INLINECODE_${inlineCode.length}@@`;
        inlineCode.push(`<code>${code}</code>`);
        return placeholder;
    });
    // Bold and italic (simple, non-nested)
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    // Extract markdown links [text](url) to avoid double-linking
    const links = [];
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, rawUrl) => {
        const url = (rawUrl || "").trim();
        if (!isSafeHref(url)) {
            return match;
        }
        const placeholder = `@@LINK_${links.length}@@`;
        // Note: label and url are already escaped because text is escaped.
        links.push(`<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
        return placeholder;
    });
    // Auto-link raw URLs
    text = text.replace(/(https?:\/\/[^\s]+)/g, (url) => {
        let cleanUrl = url;
        let suffix = "";
        const trailing = /[.,;!?)]$/;
        while (trailing.test(cleanUrl)) {
            suffix = cleanUrl.slice(-1) + suffix;
            cleanUrl = cleanUrl.slice(0, -1);
        }
        return `<a href="${cleanUrl}" target="_blank" rel="noopener">${cleanUrl}</a>${suffix}`;
    });
    // Restore markdown links
    text = text.replace(/@@LINK_(\d+)@@/g, (_m, id) => {
        return links[Number(id)] ?? "";
    });
    // Restore inline code
    text = text.replace(/@@INLINECODE_(\d+)@@/g, (_m, id) => {
        return inlineCode[Number(id)] ?? "";
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
                out.push("", "<ul>");
                inList = true;
            }
            out.push(`<li>${line.replace(/^[-*]\s+/, "")}</li>`);
        }
        else {
            if (inList) {
                out.push("</ul>", "");
                inList = false;
            }
            out.push(line);
        }
    });
    if (inList)
        out.push("</ul>", "");
    // Paragraphs and placeholder restoration
    const joined = out.join("\n");
    return joined
        .split(/\n\n+/)
        .map((block) => {
        if (block.trim().startsWith("<ul>")) {
            return block;
        }
        const match = block.match(/^@@CODEBLOCK_(\d+)@@$/);
        if (match) {
            const idx = Number(match[1]);
            return codeBlocks[idx] ?? "";
        }
        const content = block.replace(/\n/g, "<br>").replace(/@@CODEBLOCK_(\d+)@@/g, (_m, id) => codeBlocks[Number(id)] ?? "");
        return `<p>${content}</p>`;
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
        const isHttp = /^https?:\/\//.test(href);
        const targetAttrs = isHttp ? ' target="_blank" rel="noopener"' : "";
        const downloadAttr = isHttp ? "" : " download";
        return `<li class="messages-file">
        <span class="messages-file-icon">📎</span>
        <a href="${escapeHtml(href)}"${downloadAttr}${targetAttrs}>${escapeHtml(f.name)}</a>
        ${size ? `<span class="messages-file-size muted small">${escapeHtml(size)}</span>` : ""}
      </li>`;
    })
        .join("");
    return `<ul class="messages-files">${items}</ul>`;
}
function renderDispatch(entry, isLatest, runStatus, isLastInTimeline = false) {
    const dispatch = entry.dispatch;
    const isHandoff = dispatch?.is_handoff || dispatch?.mode === "pause";
    const isNotify = dispatch?.mode === "notify";
    const isTurnSummary = dispatch?.mode === "turn_summary" || dispatch?.extra?.is_turn_summary;
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
    // Determine dispatch type for color coding
    let dispatchTypeClass = "";
    if (isHandoff) {
        dispatchTypeClass = "dispatch-pause";
    }
    else if (isNotify) {
        dispatchTypeClass = "dispatch-notify";
    }
    else if (isTurnSummary) {
        dispatchTypeClass = "dispatch-turn";
    }
    // Collapse all but the last dispatch in the timeline
    const isCollapsed = !isLastInTimeline;
    const modePill = dispatch?.mode ? ` <span class="pill pill-small ${modeClass}">${escapeHtml(modeLabel)}</span>` : "";
    const body = dispatch?.body ? `<div class="messages-body messages-markdown">${renderMarkdown(dispatch.body)}</div>` : "";
    const ts = entry.created_at ? formatTimestamp(entry.created_at) : "";
    const collapseTitle = isCollapsed ? "Click to expand" : "Click to collapse";
    return `
    <div class="messages-entry${dispatchTypeClass ? " " + dispatchTypeClass : ""}${isCollapsed ? " collapsed" : ""}" 
         data-seq="${entry.seq}" 
         data-type="dispatch" 
         data-created="${escapeHtml(entry.created_at || "")}">
      <div class="messages-collapse-bar" 
           role="button" 
           tabindex="0" 
           title="${collapseTitle}"
           aria-label="${isCollapsed ? "Expand dispatch" : "Collapse dispatch"}" 
           aria-expanded="${String(!isCollapsed)}"></div>
      <div class="messages-content-wrapper">
        <div class="messages-entry-header">
          <span class="messages-entry-seq">#${entry.seq.toString().padStart(4, "0")}</span>
          <span class="messages-entry-title">${escapeHtml(title)}</span>
          ${modePill}
          <span class="messages-entry-time">${escapeHtml(ts)}</span>
        </div>
        <div class="messages-entry-body">
          ${body}
          ${renderFiles(entry.files)}
        </div>
      </div>
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
      <div class="messages-collapse-bar" 
           role="button" 
           tabindex="0" 
           title="Click to collapse"
           aria-label="Collapse reply" 
           aria-expanded="true"></div>
      <div class="messages-content-wrapper">
        ${replyIndicator}
        <div class="messages-entry-header">
          <span class="messages-entry-seq">#${entry.seq.toString().padStart(4, "0")}</span>
          <span class="messages-entry-title">${escapeHtml(title)}</span>
          <span class="pill pill-small pill-idle">you</span>
          <span class="messages-entry-time">${escapeHtml(ts)}</span>
        </div>
        <div class="messages-entry-body">
          ${body}
          ${renderFiles(entry.files)}
        </div>
      </div>
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
    // Count total dispatches in the sorted timeline
    let dispatchCount = 0;
    timeline.forEach((entry) => {
        if (entry.type === "dispatch") {
            dispatchCount++;
        }
    });
    // Render timeline, associating replies with preceding dispatches
    let lastDispatchSeq;
    let currentDispatchIndex = 0;
    const rendered = [];
    timeline.forEach((entry) => {
        if (entry.type === "dispatch" && entry.dispatch) {
            lastDispatchSeq = entry.dispatch.seq;
            const isLatest = entry.dispatch.seq === maxDispatchSeq;
            const isLastInTimeline = currentDispatchIndex === dispatchCount - 1;
            rendered.push(renderDispatch(entry.dispatch, isLatest, runStatus, isLastInTimeline));
            currentDispatchIndex++;
        }
        else if (entry.type === "reply" && entry.reply) {
            rendered.push(renderReply(entry.reply, lastDispatchSeq));
        }
    });
    return rendered.join("");
}
async function loadThread(runId, reason = "manual") {
    selectedRunId = runId;
    syncSelectedThread();
    if (!detailEl)
        return;
    if (!MESSAGE_REFRESH_REASONS.includes(reason)) {
        reason = "manual";
    }
    const showFullLoading = reason === "initial";
    if (showFullLoading) {
        detailEl.innerHTML = "Loading…";
    }
    else {
        setThreadDetailRefreshing(true);
    }
    try {
        await threadDetailRefresh.refresh(async () => {
            if (!isRepoHealthy()) {
                return { status: "offline", runId };
            }
            const detail = (await api(`/api/messages/threads/${encodeURIComponent(runId)}`));
            return { status: "ok", runId, detail };
        }, { reason });
    }
    catch (_err) {
        if (showFullLoading) {
            detailEl.innerHTML = "";
        }
        flash("Failed to load message thread", "error");
    }
    finally {
        if (!showFullLoading) {
            setThreadDetailRefreshing(false);
        }
    }
}
function isAtBottom(el) {
    const threshold = 8;
    return el.scrollTop + el.clientHeight >= el.scrollHeight - threshold;
}
function updateMobileDetailHeader(status, dispatchCount, replyCount) {
    const statusEl = document.getElementById("messages-detail-status");
    const countsEl = document.getElementById("messages-detail-counts");
    if (statusEl) {
        statusEl.className = `messages-detail-status pill pill-small ${getStatusPillClass(status)}`;
        statusEl.textContent = status || "idle";
    }
    if (countsEl) {
        countsEl.textContent = `${dispatchCount}D · ${replyCount}R`;
    }
}
function attachCollapseHandlers() {
    if (!detailEl)
        return;
    // Helper to toggle collapse state
    const toggleEntry = (entry, bar) => {
        const isNowCollapsed = entry.classList.toggle("collapsed");
        bar.title = isNowCollapsed ? "Click to expand" : "Click to collapse";
        bar.setAttribute("aria-expanded", String(!isNowCollapsed));
        bar.setAttribute("aria-label", isNowCollapsed ? "Expand dispatch" : "Collapse dispatch");
    };
    // Attach handlers to collapse bars
    const collapseBars = detailEl.querySelectorAll(".messages-collapse-bar");
    collapseBars.forEach((bar) => {
        // Remove existing listeners by cloning
        const newBar = bar.cloneNode(true);
        bar.parentNode?.replaceChild(newBar, bar);
        newBar.addEventListener("click", (e) => {
            e.stopPropagation();
            const entry = newBar.closest(".messages-entry");
            if (entry) {
                toggleEntry(entry, newBar);
            }
        });
        // Keyboard support
        newBar.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                const entry = newBar.closest(".messages-entry");
                if (entry) {
                    toggleEntry(entry, newBar);
                }
            }
        });
    });
    // Also make headers clickable for collapse
    const headers = detailEl.querySelectorAll(".messages-entry-header");
    headers.forEach((header) => {
        // Remove existing listeners by cloning
        const newHeader = header.cloneNode(true);
        header.parentNode?.replaceChild(newHeader, header);
        newHeader.addEventListener("click", (e) => {
            // Don't toggle if clicking on a link
            if (e.target.closest("a"))
                return;
            e.stopPropagation();
            const entry = newHeader.closest(".messages-entry");
            const bar = entry?.querySelector(".messages-collapse-bar");
            if (entry && bar) {
                toggleEntry(entry, bar);
            }
        });
    });
}
function renderThreadDetail(detail, runId, ctx) {
    if (!detailEl)
        return;
    const runStatus = (detail.run?.status || "").toString();
    const isPaused = runStatus === "paused";
    const dispatchHistory = detail.dispatch_history || [];
    const replyHistory = detail.reply_history || [];
    const dispatchCount = detail.dispatch_count ?? dispatchHistory.length;
    const replyCount = detail.reply_count ?? replyHistory.length;
    const ticketState = detail.ticket_state;
    const turns = ticketState?.total_turns ?? null;
    // Update mobile header metadata
    updateMobileDetailHeader(runStatus, dispatchCount, replyCount);
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
    const renderDetail = () => {
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
    };
    const preserve = ctx.reason === "background" && detailEl.scrollHeight > 0 && !isAtBottom(detailEl);
    if (preserve) {
        preserveScroll(detailEl, () => {
            renderDetail();
            attachCollapseHandlers();
        }, { restoreOnNextFrame: true });
    }
    else {
        renderDetail();
        attachCollapseHandlers();
    }
    // Only show reply box for paused runs - replies to other states won't be seen
    const replyBoxEl = document.querySelector(".messages-reply-box");
    if (replyBoxEl) {
        replyBoxEl.classList.toggle("hidden", !isPaused);
    }
    if (!preserve) {
        requestAnimationFrame(() => {
            if (detailEl) {
                detailEl.scrollTop = detailEl.scrollHeight;
            }
        });
    }
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
    initFileBox();
    backBtn?.addEventListener("click", showThreadList);
    window.addEventListener("resize", () => {
        if (!isMobileViewport()) {
            layoutEl?.classList.remove("viewing-detail");
        }
    });
    refreshEl?.addEventListener("click", () => {
        void loadThreads("manual");
        const runId = selectedRunId;
        if (runId)
            void loadThread(runId, "manual");
    });
    replySendEl?.addEventListener("click", () => {
        void sendReply();
    });
    // Load threads immediately, and try to open run_id from URL if present.
    void loadThreads("initial").then(() => {
        const params = getUrlParams();
        const runId = params.get("run_id");
        if (runId) {
            selectedRunId = runId;
            showThreadDetail();
            void loadThread(runId, "initial");
            return;
        }
        // Fall back to active message if any.
        if (activeRunId) {
            selectedRunId = activeRunId;
            updateUrlParams({ run_id: activeRunId });
            showThreadDetail();
            void loadThread(activeRunId, "initial");
        }
    });
    subscribe("tab:change", (tabId) => {
        if (tabId === "inbox") {
            void refreshBell();
            void loadThreads("manual");
            const params = getUrlParams();
            const runId = params.get("run_id");
            if (runId) {
                selectedRunId = runId;
                showThreadDetail();
                void loadThread(runId, "manual");
            }
        }
    });
    subscribe("state:update", () => {
        void refreshBell();
    });
    subscribe("repo:health", (payload) => {
        const status = payload?.status || "";
        if (status === "ok" || status === "degraded" || status === "offline") {
            void loadThreads("background");
            if (selectedRunId) {
                void loadThread(selectedRunId, "background");
            }
            if (status === "ok" || status === "degraded") {
                void fileBoxCtrl?.refresh();
            }
        }
    });
}
