import { api, escapeHtml, flash, getUrlParams, updateUrlParams } from "./utils.js";
import { activateTab } from "./tabs.js";
import { subscribe } from "./bus.js";

interface AgentMessage {
  mode?: string;
  title?: string | null;
  body?: string | null;
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
  };
}

interface ThreadsResponse {
  threads?: ThreadSummary[];
}

interface ThreadDetail {
  run?: {
    id: string;
    status?: string;
  };
  handoff_history?: Array<{
    seq: number;
    message?: AgentMessage | null;
    files?: Array<{ name: string; url: string }>;
  }>;
  reply_history?: Array<{
    seq: number;
    reply?: { title?: string | null; body?: string | null } | null;
    files?: Array<{ name: string; url: string }>;
  }>;
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
    refreshBell();
  }, 15000);
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

function renderFiles(files: Array<{ name: string; url: string }> | undefined): string {
  if (!files || !files.length) return "";
  const items = files
    .map((f) => `<li><a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.name)}</a></li>`)
    .join("");
  return `<ul class="messages-files">${items}</ul>`;
}

function renderHandoff(entry: { seq: number; message?: AgentMessage | null; files?: any[] }): string {
  const msg = entry.message;
  const title = msg?.title || "Agent message";
  const mode = msg?.mode ? ` <span class="pill pill-small">${escapeHtml(msg.mode)}</span>` : "";
  const body = msg?.body ? `<pre class="messages-body">${escapeHtml(msg.body)}</pre>` : "";
  return `
    <details class="messages-entry" open>
      <summary>#${entry.seq.toString().padStart(4, "0")} ${escapeHtml(title)}${mode}</summary>
      ${body}
      ${renderFiles(entry.files)}
    </details>
  `;
}

function renderReply(entry: { seq: number; reply?: any; files?: any[] }): string {
  const rep = entry.reply;
  const title = rep?.title || "Reply";
  const body = rep?.body ? `<pre class="messages-body">${escapeHtml(rep.body)}</pre>` : "";
  return `
    <details class="messages-entry" open>
      <summary>#${entry.seq.toString().padStart(4, "0")} ${escapeHtml(title)} <span class="pill pill-small pill-idle">you</span></summary>
      ${body}
      ${renderFiles(entry.files)}
    </details>
  `;
}

async function loadThread(runId: string): Promise<void> {
  selectedRunId = runId;
  if (!detailEl) return;
  detailEl.innerHTML = "Loading…";
  let detail: ThreadDetail;
  try {
    detail = (await api(`/api/messages/threads/${encodeURIComponent(runId)}`)) as ThreadDetail;
  } catch (_err) {
    detailEl.innerHTML = "";
    flash("Failed to load message thread", "error");
    return;
  }

  const runStatus = detail.run?.status || "";
  const handoff = (detail.handoff_history || []).map(renderHandoff).join("");
  const replies = (detail.reply_history || []).map(renderReply).join("");
  const resumeHint = runStatus === "paused" ? "Paused" : runStatus;

  detailEl.innerHTML = `
    <div class="messages-thread-header">
      <div>
        <div class="messages-thread-id">Run: <code>${escapeHtml(runId)}</code></div>
        <div class="muted small">Status: ${escapeHtml(resumeHint)}</div>
      </div>
    </div>
    <div class="messages-thread-history">
      <h3 class="messages-section-title">Agent messages</h3>
      ${handoff || '<div class="muted">No agent messages archived</div>'}
      <h3 class="messages-section-title">Your replies</h3>
      ${replies || '<div class="muted">No replies yet</div>'}
    </div>
  `;

  if (resumeEl) {
    resumeEl.disabled = runStatus !== "paused";
  }
}

async function sendReply({ resume }: { resume: boolean }): Promise<void> {
  const runId = selectedRunId;
  if (!runId) {
    flash("Select a message thread first", "error");
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
}
