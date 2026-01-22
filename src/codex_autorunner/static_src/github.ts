import { api, flash, resolvePath, statusPill, streamEvents } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";

function $(id: string): HTMLElement | null {
  return document.getElementById(id);
}

function setText(el: HTMLElement | null, text: string | null | undefined): void {
  if (!el) return;
  el.textContent = text ?? "–";
}

function setLink(el: HTMLAnchorElement | null, { href, text, title }: { href?: string; text?: string; title?: string } = {}): void {
  if (!el) return;
  if (href) {
    el.href = href;
    el.target = "_blank";
    el.rel = "noopener noreferrer";
    el.classList.remove("muted");
    el.textContent = text || href;
    if (title) el.title = title;
  } else {
    el.removeAttribute("href");
    el.removeAttribute("target");
    el.removeAttribute("rel");
    el.classList.add("muted");
    el.textContent = text || "–";
    if (title) el.title = title;
  }
}

async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    // ignore
  }
  return false;
}

async function loadGitHubStatus(): Promise<void> {
  const pill = $("github-status-pill") as HTMLElement | null;
  const note = $("github-note") as HTMLElement | null;
  const syncBtn = $("github-sync-pr") as HTMLButtonElement | null;
  const openFilesBtn = $("github-open-pr-files") as HTMLButtonElement | null;
  const copyPrBtn = $("github-copy-pr") as HTMLButtonElement | null;
  const card = $("github-card");

  try {
    const data = await api("/api/github/status") as Record<string, unknown>;
    const gh = (data.gh || {}) as Record<string, unknown>;
    const repo = (data.repo || null) as Record<string, unknown> | null;
    const git = (data.git || {}) as Record<string, unknown>;
    const link = (data.link || {}) as Record<string, unknown>;
    const issue = (link.issue || null) as Record<string, unknown> | null;
    const pr = (link.pr || null) as Record<string, unknown> | null;
    const prLinks = (data.pr_links || null) as Record<string, unknown> | null;

    if (!gh.available) {
      statusPill(pill, "error");
      setText(note, "GitHub CLI (gh) not available.");
      if (syncBtn) syncBtn.disabled = true;
    } else if (!gh.authenticated) {
      statusPill(pill, "warn");
      setText(note, "GitHub CLI not authenticated.");
      if (syncBtn) syncBtn.disabled = true;
    } else {
      statusPill(pill, "idle");
      setText(note, git.clean ? "Clean working tree." : "Uncommitted changes.");
      if (syncBtn) syncBtn.disabled = false;
    }

    setLink($("github-repo-link") as HTMLAnchorElement | null, {
      href: repo?.url as string | undefined,
      text: (repo?.nameWithOwner as string | undefined) || "–",
      title: (repo?.url as string | undefined) || "",
    });
    if (card) {
      card.dataset.githubRepoUrl = (repo?.url as string | undefined) || "";
    }
    setText($("github-branch"), (git.branch as string | undefined) || "–");

    setLink($("github-issue-link") as HTMLAnchorElement | null, {
      href: issue?.url as string | undefined,
      text: issue?.number ? `#${issue.number as string}` : "–",
      title: (issue?.title as string | undefined) || (issue?.url as string | undefined) || "",
    });

    const prUrl = (prLinks?.url as string | undefined) || (pr?.url as string | undefined) || null;
    setLink($("github-pr-link") as HTMLAnchorElement | null, {
      href: prUrl || "",
      text: pr?.number ? `#${pr.number as string}` : prUrl ? "PR" : "–",
      title: (pr?.title as string | undefined) || prUrl || "",
    });

    const hasPr = !!prUrl;
    if (openFilesBtn) openFilesBtn.disabled = !hasPr;
    if (copyPrBtn) copyPrBtn.disabled = !hasPr;

    if (openFilesBtn) {
      openFilesBtn.onclick = () => {
        const files = (prLinks?.files as string | undefined) || (prUrl ? `${prUrl}/files` : null);
        if (!files) return;
        window.open(files, "_blank", "noopener,noreferrer");
      };
    }
    if (copyPrBtn) {
      copyPrBtn.onclick = async () => {
        if (!prUrl) return;
        const ok = await copyToClipboard(prUrl);
        flash(ok ? "Copied PR link" : "Copy failed", ok ? "info" : "error");
      };
    }

    if (syncBtn) {
      // Hub install: PR sync always operates on the current worktree/branch.
      (syncBtn as unknown as { mode?: string }).mode = "current";
    }
  } catch (err) {
    statusPill(pill, "error");
    setText(note, (err as Error).message || "Failed to load GitHub status");
    if (syncBtn) syncBtn.disabled = true;
  }
}

function prFlowEls(): {
  statusPill: HTMLElement | null;
  mode: HTMLSelectElement | null;
  ref: HTMLInputElement | null;
  base: HTMLInputElement | null;
  until: HTMLSelectElement | null;
  cycles: HTMLInputElement | null;
  runs: HTMLInputElement | null;
  timeout: HTMLInputElement | null;
  draft: HTMLInputElement | null;
  step: HTMLElement | null;
  cycle: HTMLElement | null;
  review: HTMLElement | null;
  specLink: HTMLAnchorElement | null;
  progressLink: HTMLAnchorElement | null;
  patchLink: HTMLAnchorElement | null;
  logsLink: HTMLAnchorElement | null;
  finalLink: HTMLAnchorElement | null;
  startBtn: HTMLButtonElement | null;
  stopBtn: HTMLButtonElement | null;
  resumeBtn: HTMLButtonElement | null;
} {
  return {
    statusPill: $("pr-flow-status"),
    mode: $("pr-flow-mode") as HTMLSelectElement | null,
    ref: $("pr-flow-ref") as HTMLInputElement | null,
    base: $("pr-flow-base") as HTMLInputElement | null,
    until: $("pr-flow-until") as HTMLSelectElement | null,
    cycles: $("pr-flow-cycles") as HTMLInputElement | null,
    runs: $("pr-flow-runs") as HTMLInputElement | null,
    timeout: $("pr-flow-timeout") as HTMLInputElement | null,
    draft: $("pr-flow-draft") as HTMLInputElement | null,
    step: $("pr-flow-step"),
    cycle: $("pr-flow-cycle"),
    review: $("pr-flow-review"),
    specLink: $("pr-flow-spec-link") as HTMLAnchorElement | null,
    progressLink: $("pr-flow-progress-link") as HTMLAnchorElement | null,
    patchLink: $("pr-flow-patch-link") as HTMLAnchorElement | null,
    logsLink: $("pr-flow-logs-link") as HTMLAnchorElement | null,
    finalLink: $("pr-flow-final-link") as HTMLAnchorElement | null,
    startBtn: $("pr-flow-start") as HTMLButtonElement | null,
    stopBtn: $("pr-flow-stop") as HTMLButtonElement | null,
    resumeBtn: $("pr-flow-resume") as HTMLButtonElement | null,
  };
}

function formatMissingPrFlowElements(missing: string[]): string {
  if (missing.length <= 4) {
    return missing.join(", ");
  }
  const extra = missing.length - 4;
  return `${missing.slice(0, 4).join(", ")} +${extra} more`;
}

function missingPrFlowElements(els: ReturnType<typeof prFlowEls>): string[] {
  const required: Array<[string, HTMLElement | null]> = [
    ["pr-flow-status", els.statusPill],
    ["pr-flow-mode", els.mode],
    ["pr-flow-ref", els.ref],
    ["pr-flow-base", els.base],
    ["pr-flow-until", els.until],
    ["pr-flow-cycles", els.cycles],
    ["pr-flow-runs", els.runs],
    ["pr-flow-timeout", els.timeout],
    ["pr-flow-draft", els.draft],
    ["pr-flow-step", els.step],
    ["pr-flow-cycle", els.cycle],
    ["pr-flow-review", els.review],
    ["pr-flow-spec-link", els.specLink],
    ["pr-flow-progress-link", els.progressLink],
    ["pr-flow-patch-link", els.patchLink],
    ["pr-flow-logs-link", els.logsLink],
    ["pr-flow-final-link", els.finalLink],
    ["pr-flow-start", els.startBtn],
    ["pr-flow-stop", els.stopBtn],
    ["pr-flow-resume", els.resumeBtn],
  ];
  return required.filter(([, el]) => !el).map(([id]) => id);
}

function formatCount(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (typeof value === "string" && value.trim()) return value;
  return "–";
}

function setFlowStatusPill(el: HTMLElement | null, status: string | null | undefined): void {
  if (!el) return;
  const normalized = status || "idle";
  el.textContent = normalized;
  el.classList.remove("pill-idle", "pill-running", "pill-error", "pill-warn");
  if (normalized === "running" || normalized === "pending") {
    el.classList.add("pill-running");
  } else if (normalized === "failed") {
    el.classList.add("pill-error");
  } else if (normalized === "stopping" || normalized === "stopped") {
    el.classList.add("pill-warn");
  } else {
    el.classList.add("pill-idle");
  }
}

function setArtifactLink(
  el: HTMLAnchorElement | null,
  runId: string | null,
  kind: string,
  hasValue: boolean,
): void {
  if (!el) return;
  if (!runId || !hasValue) {
    setLink(el, { href: undefined, text: el.textContent || "–" });
    return;
  }
  setLink(el, {
    href: resolvePath(`/api/flows/${runId}/artifact?kind=${kind}`),
    text: el.textContent || kind,
    title: `Open ${kind.replace("_", " ")}`,
  });
}

function setButtonBusy(btn: HTMLButtonElement | null, busy: boolean): void {
  if (!btn) return;
  btn.disabled = busy;
  btn.classList.toggle("loading", busy);
}

function setButtonsDisabled(buttons: Array<HTMLButtonElement | null>, disabled: boolean): boolean[] {
  const previous = buttons.map((btn) => (btn ? btn.disabled : true));
  buttons.forEach((btn) => {
    if (btn) btn.disabled = disabled;
  });
  return previous;
}

function restoreButtonsDisabled(buttons: Array<HTMLButtonElement | null>, previous: boolean[]): void {
  buttons.forEach((btn, idx) => {
    if (!btn) return;
    btn.disabled = previous[idx] ?? btn.disabled;
    btn.classList.remove("loading");
  });
}

function setTemporaryNote(note: HTMLElement | null, message: string): string {
  if (!note) return "";
  const previous = note.textContent || "";
  note.textContent = message;
  return previous;
}

function markPrFlowClick(action: string): void {
  const card = $("github-card");
  if (card) {
    card.dataset.prFlowLastAction = action;
    card.dataset.prFlowLastClick = new Date().toISOString();
  }
  console.debug(`[github] pr flow ${action} click`);
}

function restoreTemporaryNote(note: HTMLElement | null, previous: string, message: string): void {
  if (!note) return;
  if ((note.textContent || "") === message) {
    note.textContent = previous;
  }
}

const PR_FLOW_RUN_ID_KEY = "prFlowRunId";
let prFlowRunId: string | null = null;
let prFlowEventStopper: (() => void) | null = null;

function readStoredPrFlowRunId(): string | null {
  try {
    const stored = window.localStorage.getItem(PR_FLOW_RUN_ID_KEY);
    if (stored && stored.trim()) return stored.trim();
  } catch (_err) {
    return null;
  }
  return null;
}

function currentPrFlowRunId(): string | null {
  if (prFlowRunId !== null) return prFlowRunId;
  prFlowRunId = readStoredPrFlowRunId();
  return prFlowRunId;
}

function setPrFlowRunId(runId: string | null): void {
  prFlowRunId = runId;
  try {
    if (runId) {
      window.localStorage.setItem(PR_FLOW_RUN_ID_KEY, runId);
    } else {
      window.localStorage.removeItem(PR_FLOW_RUN_ID_KEY);
    }
  } catch (_err) {
    // ignore storage failures
  }
  const card = $("github-card");
  if (card) {
    card.dataset.prFlowRunId = runId || "";
  }
}

function stopPrFlowEventStream(): void {
  if (prFlowEventStopper) {
    prFlowEventStopper();
    prFlowEventStopper = null;
  }
}

function startPrFlowEventStream(runId: string | null): void {
  stopPrFlowEventStream();
  if (!runId) return;
  const note = $("github-note") as HTMLElement | null;
  const stop = streamEvents(`/api/flows/${runId}/events`, {
    onMessage: (_data, _event) => {
      void loadPrFlowStatus();
    },
    onError: (err) => {
      setTemporaryNote(note, err.message || "PR flow events unavailable");
      if (stop) stop();
    },
  });
  prFlowEventStopper = stop;
}

function normalizePrFlowRef(ref: string, mode: string, repoUrl: string | null): string | null {
  const trimmed = ref.trim();
  if (!trimmed) return null;
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  const normalizedRepo = (repoUrl || "").trim().replace(/\/$/, "");
  const number = trimmed.replace(/^#/, "");
  if (!normalizedRepo || !/^\d+$/.test(number)) return null;
  const kind = mode === "pr" ? "pull" : "issues";
  return `${normalizedRepo}/${kind}/${number}`;
}

async function loadPrFlowArtifacts(runId: string | null): Promise<void> {
  const els = prFlowEls();
  const fallback = () => {
    setArtifactLink(els.specLink, runId, "spec.md", false);
    setArtifactLink(els.progressLink, runId, "progress.md", false);
    setArtifactLink(els.patchLink, runId, "patch.diff", false);
    setArtifactLink(els.logsLink, runId, "logs.jsonl", false);
    setArtifactLink(els.finalLink, runId, "final_report.md", false);
  };
  if (!runId) {
    fallback();
    return;
  }
  try {
    const data = await api(`/api/flows/${runId}/artifacts`) as Array<{ kind?: string }>;
    const kinds = new Set<string>();
    data.forEach((entry) => {
      if (entry && typeof entry.kind === "string") {
        kinds.add(entry.kind);
      }
    });
    setArtifactLink(els.specLink, runId, "spec.md", kinds.has("spec.md"));
    setArtifactLink(els.progressLink, runId, "progress.md", kinds.has("progress.md"));
    setArtifactLink(els.patchLink, runId, "patch.diff", kinds.has("patch.diff"));
    setArtifactLink(els.logsLink, runId, "logs.jsonl", kinds.has("logs.jsonl"));
    setArtifactLink(els.finalLink, runId, "final_report.md", kinds.has("final_report.md"));
  } catch (_err) {
    fallback();
  }
}

async function loadPrFlowStatus(): Promise<void> {
  const els = prFlowEls();
  if (!els.statusPill) return;
  const runId = currentPrFlowRunId();
  if (!runId) {
    setFlowStatusPill(els.statusPill, "idle");
    setText(els.step, "–");
    setText(els.cycle, "–");
    setText(els.review, "–");
    if (els.startBtn) els.startBtn.disabled = false;
    if (els.stopBtn) els.stopBtn.disabled = true;
    if (els.resumeBtn) els.resumeBtn.disabled = true;
    await loadPrFlowArtifacts(runId);
    return;
  }
  try {
    const data = await api(`/api/flows/${runId}/status`) as Record<string, unknown>;
    const state = (data.state || {}) as Record<string, unknown>;
    const status = data.status as string | undefined;
    setFlowStatusPill(els.statusPill, status || "idle");
    setText(els.step, (data.current_step as string | undefined) || "–");
    setText(els.cycle, formatCount(state.cycle_count));
    setText(els.review, formatCount(state.feedback_count));
    const running = status === "running" || status === "pending" || status === "stopping";
    if (els.startBtn) els.startBtn.disabled = running;
    if (els.stopBtn) els.stopBtn.disabled = !running;
    if (els.resumeBtn) els.resumeBtn.disabled = running;
    await loadPrFlowArtifacts(runId);
  } catch (_err) {
    setFlowStatusPill(els.statusPill, "failed");
    setText(els.step, "Error");
    await loadPrFlowArtifacts(runId);
  }
}

function prFlowPayload(): { input_data: Record<string, unknown>; metadata?: Record<string, unknown> } | null {
  const els = prFlowEls();
  if (!els.mode || !els.ref) return null;
  const mode = els.mode.value || "issue";
  const ref = (els.ref.value || "").trim();
  if (!ref) return null;
  const card = $("github-card");
  const repoUrl = card?.dataset.githubRepoUrl || null;
  const targetUrl = normalizePrFlowRef(ref, mode, repoUrl);
  if (!targetUrl) return null;
  const input_data: Record<string, unknown> = {
    input_type: mode,
    issue_url: mode === "issue" ? targetUrl : null,
    pr_url: mode === "pr" ? targetUrl : null,
  };
  const metadata: Record<string, unknown> = {};
  if (ref !== targetUrl) metadata.raw_ref = ref;
  const baseBranch = (els.base?.value || "").trim();
  if (baseBranch) metadata.base_branch = baseBranch;
  const stopCondition = (els.until?.value || "").trim();
  if (stopCondition) metadata.stop_condition = stopCondition;
  const cycles = parseInt(els.cycles?.value || "", 10);
  if (!Number.isNaN(cycles) && cycles > 0) metadata.max_cycles = cycles;
  const runs = parseInt(els.runs?.value || "", 10);
  if (!Number.isNaN(runs) && runs > 0) metadata.max_implementation_runs = runs;
  const timeout = parseInt(els.timeout?.value || "", 10);
  if (!Number.isNaN(timeout) && timeout >= 0) metadata.max_wallclock_seconds = timeout;
  metadata.draft = !!els.draft?.checked;
  return Object.keys(metadata).length ? { input_data, metadata } : { input_data };
}

async function startPrFlow(): Promise<void> {
  const els = prFlowEls();
  const note = $("github-note") as HTMLElement | null;
  markPrFlowClick("start");
  setTemporaryNote(note, "PR flow: click received.");
  const payload = prFlowPayload();
  if (!payload) {
    setTemporaryNote(note, "Provide a valid issue or PR reference.");
    flash("Provide a valid issue or PR reference", "error");
    return;
  }
  const buttons = [els.startBtn, els.stopBtn, els.resumeBtn];
  const prevDisabled = setButtonsDisabled(buttons, true);
  setButtonBusy(els.startBtn, true);
  const message = "Starting PR flow...";
  const prevNote = setTemporaryNote(note, message);
  try {
    const data = await api("/api/flows/pr_flow/start", { method: "POST", body: payload }) as Record<string, unknown>;
    const runId = data.id as string | undefined;
    if (runId) {
      setPrFlowRunId(runId);
      startPrFlowEventStream(runId);
    }
    flash("PR flow started");
  } catch (err) {
    flash((err as Error).message || "PR flow start failed", "error");
  } finally {
    restoreButtonsDisabled(buttons, prevDisabled);
    restoreTemporaryNote(note, prevNote, message);
  }
  await loadPrFlowStatus();
}

async function stopPrFlow(): Promise<void> {
  const els = prFlowEls();
  const note = $("github-note") as HTMLElement | null;
  markPrFlowClick("stop");
  setTemporaryNote(note, "PR flow: click received.");
  const buttons = [els.startBtn, els.stopBtn, els.resumeBtn];
  const prevDisabled = setButtonsDisabled(buttons, true);
  setButtonBusy(els.stopBtn, true);
  const message = "Stopping PR flow...";
  const prevNote = setTemporaryNote(note, message);
  try {
    const runId = currentPrFlowRunId();
    if (!runId) throw new Error("No active PR flow run");
    await api(`/api/flows/${runId}/stop`, { method: "POST", body: {} });
    flash("PR flow stopping");
  } catch (err) {
    flash((err as Error).message || "PR flow stop failed", "error");
  } finally {
    restoreButtonsDisabled(buttons, prevDisabled);
    restoreTemporaryNote(note, prevNote, message);
  }
  await loadPrFlowStatus();
}

async function resumePrFlow(): Promise<void> {
  const els = prFlowEls();
  const note = $("github-note") as HTMLElement | null;
  markPrFlowClick("resume");
  setTemporaryNote(note, "PR flow: click received.");
  const buttons = [els.startBtn, els.stopBtn, els.resumeBtn];
  const prevDisabled = setButtonsDisabled(buttons, true);
  setButtonBusy(els.resumeBtn, true);
  const message = "Resuming PR flow...";
  const prevNote = setTemporaryNote(note, message);
  try {
    const runId = currentPrFlowRunId();
    if (!runId) throw new Error("No PR flow run to resume");
    await api(`/api/flows/${runId}/resume`, { method: "POST", body: {} });
    startPrFlowEventStream(runId);
    flash("PR flow resumed");
  } catch (err) {
    flash((err as Error).message || "PR flow resume failed", "error");
  } finally {
    restoreButtonsDisabled(buttons, prevDisabled);
    restoreTemporaryNote(note, prevNote, message);
  }
  await loadPrFlowStatus();
}

async function syncPr(): Promise<void> {
  const syncBtn = $("github-sync-pr") as HTMLButtonElement | null;
  const note = $("github-note") as HTMLElement | null;
  if (!syncBtn) return;

  syncBtn.disabled = true;
  syncBtn.classList.add("loading");
  const message = "Syncing PR...";
  const prevNote = setTemporaryNote(note, message);
  try {
    const res = await api("/api/github/pr/sync", {
      method: "POST",
      body: { draft: true },
    }) as { created?: boolean };
    const created = res.created;
    flash(created ? "PR created" : "PR synced");
    setText(note, "");
    await loadGitHubStatus();
  } catch (err) {
    flash((err as Error).message || "PR sync failed", "error");
  } finally {
    syncBtn.disabled = false;
    syncBtn.classList.remove("loading");
    restoreTemporaryNote(note, prevNote, message);
  }
}

export function initGitHub(): void {
  const card = $("github-card");
  if (!card) return;
  card.dataset.githubInitialized = "true";
  console.debug("[github] init");
  const syncBtn = $("github-sync-pr") as HTMLButtonElement | null;
  if (syncBtn) syncBtn.addEventListener("click", syncPr);
  const els = prFlowEls();
  const prFlowContainer = card.querySelector(".github-flow") as HTMLElement | null;
  const missingPrFlow = missingPrFlowElements(els);
  const prFlowReady = missingPrFlow.length === 0;
  if (!prFlowReady) {
    if (prFlowContainer) {
      prFlowContainer.dataset.prFlowInitialized = "0";
    }
    if (!card.dataset.prFlowInitError) {
      const summary = formatMissingPrFlowElements(missingPrFlow);
      const message = `PR Flow UI not initialized (missing ${summary}). Static assets may be out of date; rebuild frontend bundle.`;
      card.dataset.prFlowInitError = summary;
      flash(message, "error");
      console.warn(`[github] ${message}`);
    }
  } else {
    if (prFlowContainer) {
      prFlowContainer.dataset.prFlowInitialized = "1";
    }
    if (els.startBtn) els.startBtn.addEventListener("click", startPrFlow);
    if (els.stopBtn) els.stopBtn.addEventListener("click", stopPrFlow);
    if (els.resumeBtn) els.resumeBtn.addEventListener("click", resumePrFlow);
  }

  // Initial load + auto-refresh while dashboard is active.
  loadGitHubStatus();
  registerAutoRefresh("github-status", {
    callback: loadGitHubStatus,
    tabId: null, // global: keep PR link available while browsing other tabs (mobile-friendly)
    interval: (CONSTANTS.UI?.AUTO_REFRESH_INTERVAL as number | undefined) || 15000,
    refreshOnActivation: true,
    immediate: false,
  });
  if (prFlowReady) {
    loadPrFlowStatus();
    registerAutoRefresh("pr-flow-status", {
      callback: loadPrFlowStatus,
      tabId: null,
      interval: (CONSTANTS.UI?.AUTO_REFRESH_INTERVAL as number | undefined) || 15000,
      refreshOnActivation: true,
      immediate: false,
    });
    startPrFlowEventStream(currentPrFlowRunId());
  }
}
