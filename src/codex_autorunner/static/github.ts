import { api, flash, statusPill } from "./utils.js";
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
      // Repo mode: PR sync always operates on current working tree/branch.
      (syncBtn as unknown as { mode?: string }).mode = "current";
    }
  } catch (err) {
    statusPill(pill, "error");
    setText(note, (err as Error).message || "Failed to load GitHub status");
    if (syncBtn) syncBtn.disabled = true;
  }
}

async function syncPr(): Promise<void> {
  const syncBtn = $("github-sync-pr") as HTMLButtonElement | null;
  const note = $("github-note") as HTMLElement | null;
  if (!syncBtn) return;

  syncBtn.disabled = true;
  syncBtn.classList.add("loading");
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
  }
}

export function initGitHub(): void {
  const card = $("github-card");
  if (!card) return;
  const syncBtn = $("github-sync-pr") as HTMLButtonElement | null;
  if (syncBtn) syncBtn.addEventListener("click", syncPr);

  // Initial load + auto-refresh while dashboard is active.
  loadGitHubStatus();
  registerAutoRefresh("github-status", {
    callback: loadGitHubStatus,
    tabId: null, // global: keep PR link available while browsing other tabs (mobile-friendly)
    interval: (CONSTANTS.UI?.AUTO_REFRESH_INTERVAL as number | undefined) || 15000,
    refreshOnActivation: true,
    immediate: false,
  });
}
