import {
  api,
  escapeHtml,
  flash,
  getUrlParams,
  resolvePath,
  updateUrlParams,
} from "./utils.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
import { preserveScroll } from "./preserve.js";
import { createSmartRefresh, type SmartRefreshReason } from "./smartRefresh.js";

/**
 * Dispatch: Agent-to-human communication.
 * - mode: "notify" = informational, agent continues
 * - mode: "pause" = handoff, agent yields and awaits reply
 */
interface Dispatch {
  mode?: string;
  title?: string | null;
  body?: string | null;
  extra?: Record<string, unknown> | null;
  is_handoff?: boolean;  // True when mode === "pause"
}

interface ActiveMessageResponse {
  active?: boolean;
  run_id?: string;
  seq?: number;
  dispatch?: Dispatch | null;
  open_url?: string;
}

interface ConversationSummary {
  run_id: string;
  status?: string;
  latest?: {
    seq?: number;
    dispatch?: Dispatch | null;
    created_at?: string | null;
  };
  dispatch_count?: number;
  reply_count?: number;
  ticket_state?: TicketState | null;
}

interface ThreadsResponse {
  conversations?: ConversationSummary[];
}

interface FileAttachment {
  name: string;
  url: string;
  size?: number | null;
}

interface ReplyMessage {
  title?: string | null;
  body?: string | null;
}

interface DispatchHistoryEntry {
  seq: number;
  dispatch?: Dispatch | null;
  files?: Array<{ name: string; url: string; size?: number | null }>;
  created_at?: string | null;
}

interface ReplyHistoryEntry {
  seq: number;
  reply?: { title?: string | null; body?: string | null } | null;
  files?: Array<{ name: string; url: string; size?: number | null }>;
  created_at?: string | null;
}

interface ThreadDetail {
  run?: {
    id: string;
    status?: string;
    created_at?: string | null;
  };
  dispatch_history?: DispatchHistoryEntry[];
  reply_history?: ReplyHistoryEntry[];
  dispatch_count?: number;
  reply_count?: number;
  ticket_state?: TicketState | null;
}

interface TicketState {
  current_ticket?: string | null;
  total_turns?: number | null;
  ticket_turns?: number | null;
  dispatch_seq?: number | null;
  reply_seq?: number | null;
  status?: string | null;
  reason?: string | null;
}

let bellInitialized = false;
let messagesInitialized = false;
let activeRunId: string | null = null;
let selectedRunId: string | null = null;
const MESSAGE_REFRESH_REASONS: SmartRefreshReason[] = ["initial", "background", "manual"];

const threadsEl = document.getElementById("messages-thread-list");
const detailEl = document.getElementById("messages-thread-detail");
const layoutEl = document.querySelector(".messages-layout");
const backBtn = document.getElementById("messages-back-btn") as HTMLButtonElement | null;
const refreshEl = document.getElementById("messages-refresh") as HTMLButtonElement | null;
const replyBodyEl = document.getElementById("messages-reply-body") as HTMLTextAreaElement | null;
const replyFilesEl = document.getElementById("messages-reply-files") as HTMLInputElement | null;
const replySendEl = document.getElementById("messages-reply-send") as HTMLButtonElement | null;
let threadListRefreshCount = 0;
let threadDetailRefreshCount = 0;

function isMobileViewport(): boolean {
  return window.innerWidth <= 640;
}

function showThreadList(): void {
  layoutEl?.classList.remove("viewing-detail");
}

function showThreadDetail(): void {
  if (isMobileViewport()) {
    layoutEl?.classList.add("viewing-detail");
  }
}

function setThreadListRefreshing(active: boolean): void {
  if (!threadsEl) return;
  threadListRefreshCount = Math.max(0, threadListRefreshCount + (active ? 1 : -1));
  threadsEl.classList.toggle("refreshing", threadListRefreshCount > 0);
}

function setThreadDetailRefreshing(active: boolean): void {
  if (!detailEl) return;
  threadDetailRefreshCount = Math.max(0, threadDetailRefreshCount + (active ? 1 : -1));
  detailEl.classList.toggle("refreshing", threadDetailRefreshCount > 0);
}

function formatTimestamp(ts?: string | null): string {
  if (!ts) return "–";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function setBadge(count: number): void {
  const badge = document.getElementById("tab-badge-inbox");
  if (!badge) return;
  if (count > 0) {
    badge.textContent = String(count);
    badge.classList.remove("hidden");
  } else {
    badge.textContent = "";
    badge.classList.add("hidden");
  }
}

export async function refreshBell(): Promise<void> {
  if (!isRepoHealthy()) {
    activeRunId = null;
    setBadge(0);
    return;
  }
  try {
    const res = (await api("/api/messages/active")) as ActiveMessageResponse;
    if (res?.active && res.run_id) {
      activeRunId = res.run_id;
      setBadge(1);
    } else {
      activeRunId = null;
      setBadge(0);
    }
  } catch (_err) {
    // Best-effort.
    activeRunId = null;
    setBadge(0);
  }
}

export function initMessageBell(): void {
  if (bellInitialized) return;
  bellInitialized = true;

  // Cheap polling. (The repo shell already does other polling; keep this light.)
  refreshBell();
  window.setInterval(() => {
    if (document.hidden) return;
    if (!isRepoHealthy()) return;
    refreshBell();
  }, 15000);

  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status === "ok" || status === "degraded") {
      void refreshBell();
    }
  });
}

function formatRelativeTime(ts?: string | null): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  if (diffSecs < 60) return "just now";
  const diffMins = Math.floor(diffSecs / 60);
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function getStatusPillClass(status?: string): string {
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

function renderThreadItem(thread: ConversationSummary): string {
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

function syncSelectedThread(): void {
  if (!threadsEl) return;
  const buttons = threadsEl.querySelectorAll<HTMLButtonElement>(".messages-thread");
  buttons.forEach((btn) => {
    const runId = btn.dataset.runId || "";
    btn.classList.toggle("active", Boolean(runId) && runId === selectedRunId);
  });
}

type ThreadListPayload = {
  status: "ok" | "offline";
  conversations: ConversationSummary[];
};

type ThreadDetailPayload = {
  status: "ok" | "offline";
  runId: string;
  detail?: ThreadDetail;
};

function threadListSignature(conversations: ConversationSummary[]): string {
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

function threadDetailSignature(detail: ThreadDetail): string {
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

const threadListRefresh = createSmartRefresh<ThreadListPayload>({
  getSignature: (payload) => {
    if (payload.status !== "ok") return payload.status;
    return `ok::${threadListSignature(payload.conversations)}`;
  },
  render: (payload) => {
    if (!threadsEl) return;
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
      threadsEl.querySelectorAll<HTMLButtonElement>(".messages-thread").forEach((btn) => {
        btn.addEventListener("click", () => {
          const runId = btn.dataset.runId || "";
          if (!runId) return;
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

const threadDetailRefresh = createSmartRefresh<ThreadDetailPayload>({
  getSignature: (payload) => {
    if (payload.status !== "ok") return `${payload.status}::${payload.runId}`;
    if (!payload.detail) return `empty::${payload.runId}`;
    return `ok::${payload.runId}::${threadDetailSignature(payload.detail)}`;
  },
  render: (payload, ctx) => {
    if (!detailEl) return;
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

async function fetchThreadsPayload(): Promise<ThreadListPayload> {
  if (!isRepoHealthy()) {
    return { status: "offline", conversations: [] };
  }
  const res = (await api("/api/messages/threads")) as ThreadsResponse;
  return { status: "ok", conversations: res?.conversations || [] };
}

async function loadThreads(reason: SmartRefreshReason = "manual"): Promise<void> {
  if (!threadsEl) return;
  if (!MESSAGE_REFRESH_REASONS.includes(reason)) {
    reason = "manual";
  }
  const showFullLoading = reason === "initial";
  if (showFullLoading) {
    threadsEl.innerHTML = "Loading…";
  } else {
    setThreadListRefreshing(true);
  }
  try {
    await threadListRefresh.refresh(fetchThreadsPayload, { reason });
  } catch (_err) {
    if (showFullLoading) {
      threadsEl.innerHTML = "";
    }
    flash("Failed to load inbox", "error");
  } finally {
    if (!showFullLoading) {
      setThreadListRefreshing(false);
    }
  }
}

function formatBytes(size?: number | null): string {
  if (typeof size !== "number" || Number.isNaN(size)) return "";
  if (size >= 1_000_000) return `${(size / 1_000_000).toFixed(1)} MB`;
  if (size >= 1_000) return `${(size / 1_000).toFixed(0)} KB`;
  return `${size} B`;
}

export function renderMarkdown(body?: string | null): string {
  if (!body) return "";
  let text = escapeHtml(body);

  // Extract fenced code blocks to avoid mutating their contents later.
  const codeBlocks: string[] = [];
  text = text.replace(/```([\s\S]*?)```/g, (_m, code) => {
    const placeholder = `@@CODEBLOCK_${codeBlocks.length}@@`;
    codeBlocks.push(`<pre class="md-code"><code>${code}</code></pre>`);
    return placeholder;
  });

  // Extract inline code to avoid linking inside it
  const inlineCode: string[] = [];
  text = text.replace(/`([^`]+)`/g, (_m, code) => {
    const placeholder = `@@INLINECODE_${inlineCode.length}@@`;
    inlineCode.push(`<code>${code}</code>`);
    return placeholder;
  });

  // Bold and italic (simple, non-nested)
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\*([^*]+)\*/g, "<em>$1</em>");

  // Extract markdown links [text](url) to avoid double-linking
  const links: string[] = [];
  text = text.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, (_m, label, url) => {
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
  const out: string[] = [];
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
    } else {
      if (inList) {
        out.push("</ul>", "");
        inList = false;
      }
      out.push(line);
    }
  });
  if (inList) out.push("</ul>", "");

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
      return `<p>${block.replace(/\n/g, "<br>")}</p>`;
    })
    .join("");
}

function renderFiles(files: Array<{ name: string; url: string; size?: number | null }> | undefined): string {
  if (!files || !files.length) return "";
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

function renderDispatch(
  entry: DispatchHistoryEntry,
  isLatest: boolean,
  runStatus: string,
  isLastInTimeline: boolean = false
): string {
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
    } else {
      modeClass = "pill-idle";
      modeLabel = "HANDOFF";
    }
  }

  // Determine dispatch type for color coding
  let dispatchTypeClass = "";
  if (isHandoff) {
    dispatchTypeClass = "dispatch-pause";
  } else if (isNotify) {
    dispatchTypeClass = "dispatch-notify";
  } else if (isTurnSummary) {
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

function renderReply(entry: { seq: number; reply?: ReplyMessage | null; files?: FileAttachment[]; created_at?: string | null }, parentSeq?: number): string {
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

interface TimelineEntry {
  type: "dispatch" | "reply";
  seq: number;
  created_at: string | null;
  dispatch?: DispatchHistoryEntry;
  reply?: ReplyHistoryEntry;
}

function buildThreadedTimeline(
  dispatches: DispatchHistoryEntry[],
  replies: ReplyHistoryEntry[],
  runStatus: string
): string {
  // Combine all entries into a single timeline
  const timeline: TimelineEntry[] = [];

  // Find the latest dispatch sequence number to identify the most recent agent message
  let maxDispatchSeq = -1;
  dispatches.forEach((d) => {
    if (d.seq > maxDispatchSeq) maxDispatchSeq = d.seq;
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
  let lastDispatchSeq: number | undefined;
  let currentDispatchIndex = 0;
  const rendered: string[] = [];

  timeline.forEach((entry) => {
    if (entry.type === "dispatch" && entry.dispatch) {
      lastDispatchSeq = entry.dispatch.seq;
      const isLatest = entry.dispatch.seq === maxDispatchSeq;
      const isLastInTimeline = currentDispatchIndex === dispatchCount - 1;
      rendered.push(renderDispatch(entry.dispatch, isLatest, runStatus, isLastInTimeline));
      currentDispatchIndex++;
    } else if (entry.type === "reply" && entry.reply) {
      rendered.push(renderReply(entry.reply, lastDispatchSeq));
    }
  });

  return rendered.join("");
}

async function loadThread(runId: string, reason: SmartRefreshReason = "manual"): Promise<void> {
  selectedRunId = runId;
  syncSelectedThread();
  if (!detailEl) return;
  if (!MESSAGE_REFRESH_REASONS.includes(reason)) {
    reason = "manual";
  }
  const showFullLoading = reason === "initial";
  if (showFullLoading) {
    detailEl.innerHTML = "Loading…";
  } else {
    setThreadDetailRefreshing(true);
  }
  try {
    await threadDetailRefresh.refresh(async () => {
      if (!isRepoHealthy()) {
        return { status: "offline", runId };
      }
      const detail = (await api(`/api/messages/threads/${encodeURIComponent(runId)}`)) as ThreadDetail;
      return { status: "ok", runId, detail };
    }, { reason });
  } catch (_err) {
    if (showFullLoading) {
      detailEl.innerHTML = "";
    }
    flash("Failed to load message thread", "error");
  } finally {
    if (!showFullLoading) {
      setThreadDetailRefreshing(false);
    }
  }
}

function isAtBottom(el: HTMLElement): boolean {
  const threshold = 8;
  return el.scrollTop + el.clientHeight >= el.scrollHeight - threshold;
}

function updateMobileDetailHeader(status: string, dispatchCount: number, replyCount: number): void {
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

function attachCollapseHandlers(): void {
  if (!detailEl) return;
  
  // Helper to toggle collapse state
  const toggleEntry = (entry: HTMLElement, bar: HTMLElement) => {
    const isNowCollapsed = entry.classList.toggle("collapsed");
    bar.title = isNowCollapsed ? "Click to expand" : "Click to collapse";
    bar.setAttribute("aria-expanded", String(!isNowCollapsed));
    bar.setAttribute("aria-label", isNowCollapsed ? "Expand dispatch" : "Collapse dispatch");
  };
  
  // Attach handlers to collapse bars
  const collapseBars = detailEl.querySelectorAll<HTMLElement>(".messages-collapse-bar");
  collapseBars.forEach((bar) => {
    // Remove existing listeners by cloning
    const newBar = bar.cloneNode(true) as HTMLElement;
    bar.parentNode?.replaceChild(newBar, bar);
    
    newBar.addEventListener("click", (e) => {
      e.stopPropagation();
      const entry = newBar.closest(".messages-entry") as HTMLElement;
      if (entry) {
        toggleEntry(entry, newBar);
      }
    });
    
    // Keyboard support
    newBar.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        const entry = newBar.closest(".messages-entry") as HTMLElement;
        if (entry) {
          toggleEntry(entry, newBar);
        }
      }
    });
  });
  
  // Also make headers clickable for collapse
  const headers = detailEl.querySelectorAll<HTMLElement>(".messages-entry-header");
  headers.forEach((header) => {
    // Remove existing listeners by cloning
    const newHeader = header.cloneNode(true) as HTMLElement;
    header.parentNode?.replaceChild(newHeader, header);
    
    newHeader.addEventListener("click", (e) => {
      // Don't toggle if clicking on a link
      if ((e.target as HTMLElement).closest("a")) return;
      e.stopPropagation();
      const entry = newHeader.closest(".messages-entry") as HTMLElement;
      const bar = entry?.querySelector(".messages-collapse-bar") as HTMLElement;
      if (entry && bar) {
        toggleEntry(entry, bar);
      }
    });
  });
}

function renderThreadDetail(detail: ThreadDetail, runId: string, ctx: { reason: SmartRefreshReason }): void {
  if (!detailEl) return;
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
  const statsParts: string[] = [];
  statsParts.push(`${dispatchCount} dispatch${dispatchCount !== 1 ? "es" : ""}`);
  statsParts.push(`${replyCount} repl${replyCount !== 1 ? "ies" : "y"}`);
  if (turns != null) statsParts.push(`${turns} turn${turns !== 1 ? "s" : ""}`);
  const statsLine = statsParts.join(" · ");

  // Status pill
  const statusPillClass = isPaused ? "pill-action" : "pill-idle";
  const statusLabel = isPaused ? "paused" : runStatus || "idle";

  // Build threaded timeline
  const threadedContent = buildThreadedTimeline(
    dispatchHistory,
    replyHistory,
    runStatus
  );

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
  } else {
    renderDetail();
    attachCollapseHandlers();
  }

  // Only show reply box for paused runs - replies to other states won't be seen
  const replyBoxEl = document.querySelector(".messages-reply-box") as HTMLElement | null;
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

async function sendReply(): Promise<void> {
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
    if (replyBodyEl) replyBodyEl.value = "";
    if (replyFilesEl) replyFilesEl.value = "";
    flash("Reply sent", "success");
    // Always resume after sending
    await api(`/api/flows/${encodeURIComponent(runId)}/resume`, { method: "POST" });
    flash("Run resumed", "success");
    void refreshBell();
    void loadThread(runId);
  } catch (_err) {
    flash("Failed to send reply", "error");
  }
}

export function initMessages(): void {
  if (messagesInitialized) return;
  if (!threadsEl || !detailEl) return;
  messagesInitialized = true;

  backBtn?.addEventListener("click", showThreadList);

  window.addEventListener("resize", () => {
    if (!isMobileViewport()) {
      layoutEl?.classList.remove("viewing-detail");
    }
  });

  refreshEl?.addEventListener("click", () => {
    void loadThreads("manual");
    const runId = selectedRunId;
    if (runId) void loadThread(runId, "manual");
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

  subscribe("tab:change", (tabId: unknown) => {
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

  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status === "ok" || status === "degraded" || status === "offline") {
      void loadThreads("background");
      if (selectedRunId) {
        void loadThread(selectedRunId, "background");
      }
    }
  });
}
