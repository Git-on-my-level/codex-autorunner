import { api, escapeHtml, flash, getUrlParams, updateUrlParams } from "./utils.js";
import { activateTab } from "./tabs.js";
import { subscribe } from "./bus.js";
import { REPO_ID } from "./env.js";
import { isRepoHealthy } from "./health.js";

interface AgentMessage {
  mode?: string;
  title?: string | null;
  body?: string | null;
  extra?: Record<string, unknown> | null;
}

interface ActiveMessageResponse {
  active?: boolean;
  run_id?: string;
  seq?: number;
  message?: AgentMessage | null;
  open_url?: string;
}

interface ThreadSummary {
  run_id: string;
  status?: string;
  latest?: {
    seq?: number;
    message?: AgentMessage | null;
    created_at?: string | null;
  };
  handoff_count?: number;
  reply_count?: number;
  ticket_state?: TicketState | null;
}

interface ThreadsResponse {
  threads?: ThreadSummary[];
}

interface ThreadDetail {
  run?: {
    id: string;
    status?: string;
    created_at?: string | null;
  };
  handoff_history?: Array<{
    seq: number;
    message?: AgentMessage | null;
    files?: Array<{ name: string; url: string; size?: number | null }>;
    created_at?: string | null;
  }>;
  reply_history?: Array<{
    seq: number;
    reply?: { title?: string | null; body?: string | null } | null;
    files?: Array<{ name: string; url: string; size?: number | null }>;
    created_at?: string | null;
  }>;
  handoff_count?: number;
  reply_count?: number;
  ticket_state?: TicketState | null;
}

interface TicketState {
  current_ticket?: string | null;
  total_turns?: number | null;
  ticket_turns?: number | null;
  outbox_seq?: number | null;
  reply_seq?: number | null;
  status?: string | null;
  reason?: string | null;
}

let bellInitialized = false;
let messagesInitialized = false;
let activeRunId: string | null = null;
let selectedRunId: string | null = null;

const bellBtn = document.getElementById("repo-inbox-btn") as HTMLButtonElement | null;
const bellBadge = document.getElementById("repo-inbox-badge") as HTMLElement | null;

const threadsEl = document.getElementById("messages-thread-list");
const detailEl = document.getElementById("messages-thread-detail");
const refreshEl = document.getElementById("messages-refresh") as HTMLButtonElement | null;
const replyBodyEl = document.getElementById("messages-reply-body") as HTMLTextAreaElement | null;
const replyFilesEl = document.getElementById("messages-reply-files") as HTMLInputElement | null;
const replySendEl = document.getElementById("messages-reply-send") as HTMLButtonElement | null;
const replySendResumeEl = document.getElementById("messages-reply-send-resume") as HTMLButtonElement | null;
const resumeEl = document.getElementById("messages-resume") as HTMLButtonElement | null;

function formatTimestamp(ts?: string | null): string {
  if (!ts) return "–";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function setBadge(count: number): void {
  if (!bellBadge) return;
  if (count > 0) {
    bellBadge.textContent = String(count);
    bellBadge.classList.remove("hidden");
  } else {
    bellBadge.textContent = "";
    bellBadge.classList.add("hidden");
  }
}

async function refreshBell(): Promise<void> {
  if (!bellBtn) return;
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
  if (!bellBtn) return;
  bellInitialized = true;

  bellBtn.addEventListener("click", () => {
    const runId = activeRunId;
    activateTab("messages");
    if (runId) {
      updateUrlParams({ tab: "messages", run_id: runId });
      // messages tab init will pick this up.
    } else {
      updateUrlParams({ tab: "messages" });
    }
  });

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

function renderThreadItem(thread: ThreadSummary): string {
  const latest = thread.latest?.message;
  const title = latest?.title || latest?.mode || "Message";
  const subtitle = latest?.body ? latest.body.slice(0, 120) : "";
  return `
    <button class="messages-thread" data-run-id="${escapeHtml(thread.run_id)}">
      <div class="messages-thread-title">${escapeHtml(title)}</div>
      <div class="messages-thread-subtitle muted">${escapeHtml(subtitle)}</div>
      <div class="messages-thread-meta muted small">${escapeHtml(thread.status || "")}</div>
    </button>
  `;
}

async function loadThreads(): Promise<void> {
  if (!threadsEl) return;
  threadsEl.innerHTML = "Loading…";
  if (!isRepoHealthy()) {
    threadsEl.innerHTML = "<div class=\"muted\">Repo offline or uninitialized</div>";
    return;
  }
  let res: ThreadsResponse;
  try {
    res = (await api("/api/messages/threads")) as ThreadsResponse;
  } catch (err) {
    threadsEl.innerHTML = "";
    flash("Failed to load inbox", "error");
    return;
  }

  const threads = res?.threads || [];
  if (!threads.length) {
    threadsEl.innerHTML = "<div class=\"muted\">No messages</div>";
    return;
  }
  threadsEl.innerHTML = threads.map(renderThreadItem).join("");
  threadsEl.querySelectorAll<HTMLButtonElement>(".messages-thread").forEach((btn) => {
    btn.addEventListener("click", () => {
      const runId = btn.dataset.runId || "";
      if (!runId) return;
      updateUrlParams({ tab: "messages", run_id: runId });
      void loadThread(runId);
    });
  });
}

function formatBytes(size?: number | null): string {
  if (typeof size !== "number" || Number.isNaN(size)) return "";
  if (size >= 1_000_000) return `${(size / 1_000_000).toFixed(1)} MB`;
  if (size >= 1_000) return `${(size / 1_000).toFixed(0)} KB`;
  return `${size} B`;
}

function renderMarkdown(body?: string | null): string {
  if (!body) return "";
  let text = escapeHtml(body);

  // Extract fenced code blocks to avoid mutating their contents later.
  const codeBlocks: string[] = [];
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
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${line.replace(/^[-*]\s+/, "")}</li>`);
    } else {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      out.push(line);
    }
  });
  if (inList) out.push("</ul>");

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

function renderFiles(files: Array<{ name: string; url: string; size?: number | null }> | undefined): string {
  if (!files || !files.length) return "";
  const items = files
    .map((f) => {
      const size = formatBytes(f.size);
      return `<li class="messages-file">
        <span class="messages-file-icon">📎</span>
        <a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.name)}</a>
        ${size ? `<span class="messages-file-size muted small">${escapeHtml(size)}</span>` : ""}
      </li>`;
    })
    .join("");
  return `<ul class="messages-files">${items}</ul>`;
}

function renderHandoff(entry: { seq: number; message?: AgentMessage | null; files?: any[]; created_at?: string | null }): string {
  const msg = entry.message;
  const title = msg?.title || "Agent message";
  const mode = msg?.mode ? ` <span class="pill pill-small">${escapeHtml(msg.mode)}</span>` : "";
  const body = msg?.body ? `<div class="messages-body messages-markdown">${renderMarkdown(msg.body)}</div>` : "";
  const ts = entry.created_at ? `<span class="muted small">${escapeHtml(formatTimestamp(entry.created_at))}</span>` : "";
  return `
    <details class="messages-entry" open>
      <summary>#${entry.seq.toString().padStart(4, "0")} ${escapeHtml(title)}${mode} ${ts}</summary>
      ${body}
      ${renderFiles(entry.files)}
    </details>
  `;
}

function renderReply(entry: { seq: number; reply?: any; files?: any[]; created_at?: string | null }): string {
  const rep = entry.reply;
  const title = rep?.title || "Reply";
  const body = rep?.body ? `<div class="messages-body messages-markdown">${renderMarkdown(rep.body)}</div>` : "";
  const ts = entry.created_at ? `<span class="muted small">${escapeHtml(formatTimestamp(entry.created_at))}</span>` : "";
  return `
    <details class="messages-entry" open>
      <summary>#${entry.seq.toString().padStart(4, "0")} ${escapeHtml(title)} <span class="pill pill-small pill-idle">you</span> ${ts}</summary>
      ${body}
      ${renderFiles(entry.files)}
    </details>
  `;
}

async function loadThread(runId: string): Promise<void> {
  selectedRunId = runId;
  if (!detailEl) return;
  detailEl.innerHTML = "Loading…";
  if (!isRepoHealthy()) {
    detailEl.innerHTML = "<div class=\"muted\">Repo offline or uninitialized.</div>";
    return;
  }
  let detail: ThreadDetail;
  try {
    detail = (await api(`/api/messages/threads/${encodeURIComponent(runId)}`)) as ThreadDetail;
  } catch (_err) {
    detailEl.innerHTML = "";
    flash("Failed to load message thread", "error");
    return;
  }

  const runStatus = (detail.run?.status || "").toString();
  const mode =
    ((detail.handoff_history || [])[0]?.message?.mode as string | undefined) || "";
  const handoff = (detail.handoff_history || []).map(renderHandoff).join("");
  const replies = (detail.reply_history || []).map(renderReply).join("");
  const resumeHint = runStatus === "paused" ? "Paused" : runStatus;
  const isPaused = runStatus === "paused";
  const createdAt = (detail.run?.created_at as string | undefined) || null;
  const ticketState = detail.ticket_state;
  const turns = ticketState?.total_turns ?? null;
  const currentTicket = ticketState?.current_ticket;
  const handoffCount = detail.handoff_count ?? (detail.handoff_history || []).length;
  const replyCount = detail.reply_count ?? (detail.reply_history || []).length;

  detailEl.innerHTML = `
    <div class="messages-thread-header">
      <div>
        <div class="messages-thread-id">Run: <code>${escapeHtml(runId)}</code></div>
        <div class="muted small">Repo: ${escapeHtml(REPO_ID || "–")} · Status: ${escapeHtml(resumeHint)} · Started: ${escapeHtml(formatTimestamp(createdAt))}</div>
      </div>
      <div class="messages-thread-tags">
        ${mode ? `<span class="pill pill-small">${escapeHtml(mode)}</span>` : ""}
        ${runStatus ? `<span class="pill pill-small pill-${isPaused ? "warn" : "idle"}">${escapeHtml(runStatus)}</span>` : ""}
      </div>
    </div>
    <div class="messages-thread-meta">
      <div class="messages-meta-item"><span class="muted small">Handoffs</span><span class="metric">${escapeHtml(String(handoffCount || 0))}</span></div>
      <div class="messages-meta-item"><span class="muted small">Replies</span><span class="metric">${escapeHtml(String(replyCount || 0))}</span></div>
      <div class="messages-meta-item"><span class="muted small">Turns</span><span class="metric">${escapeHtml(turns != null ? String(turns) : "–")}</span></div>
      <div class="messages-meta-item"><span class="muted small">Active ticket</span><span class="metric">${escapeHtml(currentTicket || "–")}</span></div>
    </div>
    <div class="messages-thread-history">
      <h3 class="messages-section-title">Agent messages</h3>
      ${handoff || '<div class="muted">No agent messages archived</div>'}
      <h3 class="messages-section-title">Your replies</h3>
      ${replies || '<div class="muted">No replies yet</div>'}
    </div>
  `;

  if (resumeEl) {
    resumeEl.disabled = !isPaused;
  }
  if (replySendEl) replySendEl.disabled = !isPaused;
  if (replySendResumeEl) replySendResumeEl.disabled = !isPaused;
}

async function sendReply({ resume }: { resume: boolean }): Promise<void> {
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
    if (resume) {
      await api(`/api/flows/${encodeURIComponent(runId)}/resume`, { method: "POST" });
      flash("Run resumed", "success");
      void refreshBell();
    }
    void loadThread(runId);
  } catch (_err) {
    flash("Failed to send reply", "error");
  }
}

export function initMessages(): void {
  if (messagesInitialized) return;
  if (!threadsEl || !detailEl) return;
  messagesInitialized = true;

  refreshEl?.addEventListener("click", () => {
    void loadThreads();
    const runId = selectedRunId;
    if (runId) void loadThread(runId);
  });

  replySendEl?.addEventListener("click", () => {
    void sendReply({ resume: false });
  });
  replySendResumeEl?.addEventListener("click", () => {
    void sendReply({ resume: true });
  });
  resumeEl?.addEventListener("click", () => {
    const runId = selectedRunId;
    if (!runId) return;
    void api(`/api/flows/${encodeURIComponent(runId)}/resume`, { method: "POST" })
      .then(() => {
        flash("Run resumed", "success");
        void refreshBell();
        void loadThread(runId);
      })
      .catch(() => flash("Failed to resume", "error"));
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

  subscribe("tab:change", (tabId: unknown) => {
    if (tabId === "messages") {
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
