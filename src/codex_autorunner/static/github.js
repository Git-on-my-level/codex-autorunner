import { api, flash, statusPill } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";

function $(id) {
  return document.getElementById(id);
}

function setText(el, text) {
  if (!el) return;
  el.textContent = text ?? "–";
}

function setLink(el, { href, text, title } = {}) {
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

async function copyToClipboard(text) {
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

async function loadGitHubStatus() {
  const pill = $("github-status-pill");
  const note = $("github-note");
  const syncBtn = $("github-sync-pr");
  const openFilesBtn = $("github-open-pr-files");
  const copyPrBtn = $("github-copy-pr");

  try {
    const data = await api("/api/github/status");
    const gh = data.gh || {};
    const repo = data.repo || null;
    const git = data.git || {};
    const link = data.link || {};
    const issue = link.issue || null;
    const pr = data.pr || link.pr || null;
    const prLinks = data.pr_links || null;

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

    setLink($("github-repo-link"), {
      href: repo?.url,
      text: repo?.nameWithOwner || "–",
      title: repo?.url || "",
    });
    setText($("github-branch"), git.branch || "–");

    setLink($("github-issue-link"), {
      href: issue?.url,
      text: issue?.number ? `#${issue.number}` : "–",
      title: issue?.title || issue?.url || "",
    });

    const prUrl = prLinks?.url || pr?.url || null;
    setLink($("github-pr-link"), {
      href: prUrl,
      text: pr?.number ? `#${pr.number}` : prUrl ? "PR" : "–",
      title: pr?.title || prUrl || "",
    });

    const hasPr = !!prUrl;
    if (openFilesBtn) openFilesBtn.disabled = !hasPr;
    if (copyPrBtn) copyPrBtn.disabled = !hasPr;

    if (openFilesBtn) {
      openFilesBtn.onclick = () => {
        const files = prLinks?.files || (prUrl ? `${prUrl}/files` : null);
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
      // Repo mode: PR sync always operates on the current working tree/branch.
      syncBtn.dataset.mode = "current";
    }
  } catch (err) {
    statusPill(pill, "error");
    setText(note, err.message || "Failed to load GitHub status");
    if (syncBtn) syncBtn.disabled = true;
  }
}

async function syncPr() {
  const syncBtn = $("github-sync-pr");
  const note = $("github-note");
  if (!syncBtn) return;

  syncBtn.disabled = true;
  syncBtn.classList.add("loading");
  try {
    const res = await api("/api/github/pr/sync", {
      method: "POST",
      body: { draft: true },
    });
    const created = res.created;
    flash(created ? "PR created" : "PR synced");
    setText(note, "");
    await loadGitHubStatus();
  } catch (err) {
    flash(err.message || "PR sync failed", "error");
  } finally {
    syncBtn.disabled = false;
    syncBtn.classList.remove("loading");
  }
}

export function initGitHub() {
  const card = $("github-card");
  if (!card) return;
  const syncBtn = $("github-sync-pr");
  if (syncBtn) syncBtn.addEventListener("click", syncPr);

  // Initial load + auto-refresh while dashboard is active.
  loadGitHubStatus();
  registerAutoRefresh("github-status", {
    callback: loadGitHubStatus,
    tabId: null, // global: keep PR link available while browsing other tabs (mobile-friendly)
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: false,
  });
}


