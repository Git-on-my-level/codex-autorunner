import { api, flash, statusPill } from "./utils.js";

let hubData = { repos: [], last_scan_at: null };

const repoListEl = document.getElementById("hub-repo-list");
const lastScanEl = document.getElementById("hub-last-scan");
const totalEl = document.getElementById("hub-count-total");
const runningEl = document.getElementById("hub-count-running");
const missingEl = document.getElementById("hub-count-missing");
const hubUsageList = document.getElementById("hub-usage-list");
const hubUsageMeta = document.getElementById("hub-usage-meta");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");

function formatTime(isoString) {
  if (!isoString) return "never";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  return date.toLocaleString();
}

function formatRunSummary(repo) {
  if (!repo.initialized) return "Not initialized";
  if (!repo.exists_on_disk) return "Missing on disk";
  if (!repo.last_run_id) return "No runs yet";
  const time = repo.last_run_finished_at || repo.last_run_started_at;
  const exit = repo.last_exit_code === null || repo.last_exit_code === undefined
    ? ""
    : ` exit:${repo.last_exit_code}`;
  return `#${repo.last_run_id}${exit}`;
}

function formatLastActivity(repo) {
  if (!repo.initialized) return "";
  const time = repo.last_run_finished_at || repo.last_run_started_at;
  if (!time) return "";
  return formatTimeCompact(time);
}

function setButtonLoading(scanning) {
  const buttons = [
    document.getElementById("hub-scan"),
    document.getElementById("hub-quick-scan"),
    document.getElementById("hub-refresh"),
  ];
  buttons.forEach((btn) => {
    if (!btn) return;
    btn.disabled = scanning;
    if (scanning) {
      btn.classList.add("loading");
    } else {
      btn.classList.remove("loading");
    }
  });
}

function formatTimeCompact(isoString) {
  if (!isoString) return "–";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return date.toLocaleDateString();
}

function renderSummary(repos) {
  const running = repos.filter((r) => r.status === "running").length;
  const missing = repos.filter((r) => !r.exists_on_disk).length;
  if (totalEl) totalEl.textContent = repos.length.toString();
  if (runningEl) runningEl.textContent = running.toString();
  if (missingEl) missingEl.textContent = missing.toString();
  if (lastScanEl) {
    lastScanEl.textContent = formatTimeCompact(hubData.last_scan_at);
  }
}

function formatTokensCompact(val) {
  if (val === null || val === undefined) return "0";
  const num = Number(val);
  if (Number.isNaN(num)) return val;
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(0)}k`;
  return num.toLocaleString();
}

function renderHubUsage(data) {
  if (!hubUsageList) return;
  if (hubUsageMeta) {
    hubUsageMeta.textContent = data?.codex_home || "–";
  }
  if (!data || !data.repos) {
    hubUsageList.innerHTML = '<span class="muted small">Usage unavailable</span>';
    return;
  }
  if (!data.repos.length && (!data.unmatched || !data.unmatched.events)) {
    hubUsageList.innerHTML = '<span class="muted small">No token events</span>';
    return;
  }
  hubUsageList.innerHTML = "";
  const entries = [...data.repos].sort(
    (a, b) => (b.totals?.total_tokens || 0) - (a.totals?.total_tokens || 0)
  );
  entries.forEach((repo) => {
    const div = document.createElement("div");
    div.className = "hub-usage-chip";
    const totals = repo.totals || {};
    const cached = totals.cached_input_tokens || 0;
    const cachePercent = totals.input_tokens ? Math.round((cached / totals.input_tokens) * 100) : 0;
    div.innerHTML = `
      <span class="hub-usage-chip-name">${repo.id}</span>
      <span class="hub-usage-chip-total">${formatTokensCompact(totals.total_tokens)}</span>
      <span class="hub-usage-chip-meta">${repo.events ?? 0}ev · ${cachePercent}%↻</span>
    `;
    hubUsageList.appendChild(div);
  });
  if (data.unmatched && data.unmatched.events) {
    const div = document.createElement("div");
    div.className = "hub-usage-chip hub-usage-chip-unmatched";
    const totals = data.unmatched.totals || {};
    div.innerHTML = `
      <span class="hub-usage-chip-name">other</span>
      <span class="hub-usage-chip-total">${formatTokensCompact(totals.total_tokens)}</span>
      <span class="hub-usage-chip-meta">${data.unmatched.events}ev</span>
    `;
    hubUsageList.appendChild(div);
  }
}

async function loadHubUsage() {
  if (hubUsageRefresh) hubUsageRefresh.disabled = true;
  try {
    const data = await api("/hub/usage");
    renderHubUsage(data);
  } catch (err) {
    flash(err.message || "Failed to load usage", "error");
    renderHubUsage(null);
  } finally {
    if (hubUsageRefresh) hubUsageRefresh.disabled = false;
  }
}

function buildActions(repo) {
  const actions = [];
  const missing = !repo.exists_on_disk;
  if (repo.init_error && !missing) {
    actions.push({ key: "init", label: "Re-init", kind: "primary" });
  } else if (!missing && !repo.initialized) {
    actions.push({ key: "init", label: "Init", kind: "primary" });
  }
  if (!missing && repo.initialized && repo.status !== "running") {
    actions.push({ key: "run", label: "Run", kind: "primary" });
    actions.push({ key: "once", label: "Once", kind: "ghost" });
  }
  if (repo.status === "running") {
    actions.push({ key: "stop", label: "Stop", kind: "ghost" });
  }
  if (repo.lock_status === "locked_stale") {
    actions.push({ key: "resume", label: "Resume", kind: "ghost" });
  }
  return actions;
}

function renderRepos(repos) {
  if (!repoListEl) return;
  repoListEl.innerHTML = "";
  if (!repos.length) {
    repoListEl.innerHTML =
      '<div class="hub-empty muted">No repos discovered yet. Run a scan or create a new repo.</div>';
    return;
  }

  repos.forEach((repo) => {
    const card = document.createElement("div");
    card.className = "hub-repo-card";
    card.dataset.repoId = repo.id;

    // Make card clickable only for repos that are actually mounted
    const canNavigate = repo.mounted === true;
    if (canNavigate) {
      card.classList.add("hub-repo-clickable");
      card.dataset.href = `/repos/${repo.id}/`;
      card.setAttribute("role", "link");
      card.setAttribute("tabindex", "0");
    }

    const actions = buildActions(repo)
      .map(
        (action) =>
          `<button class="${action.kind} sm" data-action="${action.key}" data-repo="${repo.id}">${action.label}</button>`
      )
      .join("");

    const lockBadge =
      repo.lock_status && repo.lock_status !== "unlocked"
        ? `<span class="pill pill-small pill-warn">${repo.lock_status.replace("_", " ")}</span>`
        : "";
    const initBadge = !repo.initialized
      ? '<span class="pill pill-small pill-warn">uninit</span>'
      : "";
    
    // Build note for errors
    let noteText = "";
    if (!repo.exists_on_disk) {
      noteText = "Missing on disk";
    } else if (repo.init_error) {
      noteText = repo.init_error;
    } else if (repo.mount_error) {
      noteText = `Cannot open: ${repo.mount_error}`;
    }
    const note = noteText ? `<div class="hub-repo-note">${noteText}</div>` : "";

    // Show open indicator only for navigable repos
    const openIndicator = canNavigate
      ? '<span class="hub-repo-open-indicator">→</span>'
      : '';
    
    // Build compact info line
    const runSummary = formatRunSummary(repo);
    const lastActivity = formatLastActivity(repo);
    const infoItems = [];
    if (runSummary && runSummary !== "No runs yet" && runSummary !== "Not initialized") {
      infoItems.push(runSummary);
    }
    if (lastActivity) {
      infoItems.push(lastActivity);
    }
    const infoLine = infoItems.length > 0 
      ? `<span class="hub-repo-info-line">${infoItems.join(' · ')}</span>`
      : '';

    card.innerHTML = `
      <div class="hub-repo-row">
        <div class="hub-repo-left">
            <span class="pill pill-small hub-status-pill">${repo.status}</span>
            ${lockBadge}
            ${initBadge}
          </div>
        <div class="hub-repo-center">
          <span class="hub-repo-title">${repo.display_name}</span>
          ${infoLine}
        </div>
        <div class="hub-repo-right">
          ${actions || ''}
          ${openIndicator}
        </div>
      </div>
      ${note}
    `;

    const statusEl = card.querySelector(".hub-status-pill");
    if (statusEl) {
      statusPill(statusEl, repo.status);
    }

    repoListEl.appendChild(card);
  });
}

async function refreshHub({ scan = false } = {}) {
  setButtonLoading(true);
  try {
    const path = scan ? "/hub/repos/scan" : "/hub/repos";
    const data = await api(path, { method: scan ? "POST" : "GET" });
    hubData = data;
    renderSummary(data.repos || []);
    renderRepos(data.repos || []);
    await loadHubUsage();
  } catch (err) {
    flash(err.message || "Hub request failed", "error");
  } finally {
    setButtonLoading(false);
  }
}

async function createRepo(repoId, repoPath, gitInit) {
  try {
    const payload = { id: repoId };
    if (repoPath) payload.path = repoPath;
    payload.git_init = gitInit;
    await api("/hub/repos", { method: "POST", body: payload });
    flash(`Created repo: ${repoId}`);
    await refreshHub();
    return true;
  } catch (err) {
    flash(err.message || "Failed to create repo", "error");
    return false;
  }
}

function showCreateRepoModal() {
  const modal = document.getElementById("create-repo-modal");
  if (modal) {
    modal.hidden = false;
    const input = document.getElementById("create-repo-id");
    if (input) {
      input.value = "";
      input.focus();
    }
    const pathInput = document.getElementById("create-repo-path");
    if (pathInput) pathInput.value = "";
    const gitCheck = document.getElementById("create-repo-git");
    if (gitCheck) gitCheck.checked = true;
  }
}

function hideCreateRepoModal() {
  const modal = document.getElementById("create-repo-modal");
  if (modal) modal.hidden = true;
}

async function handleCreateRepoSubmit() {
  const idInput = document.getElementById("create-repo-id");
  const pathInput = document.getElementById("create-repo-path");
  const gitCheck = document.getElementById("create-repo-git");
  
  const repoId = idInput?.value?.trim();
  const repoPath = pathInput?.value?.trim() || null;
  const gitInit = gitCheck?.checked ?? true;
  
  if (!repoId) {
    flash("Repo ID is required", "error");
    return;
  }
  
  const ok = await createRepo(repoId, repoPath, gitInit);
  if (ok) {
    hideCreateRepoModal();
  }
}

async function handleRepoAction(repoId, action) {
  const buttons = repoListEl?.querySelectorAll(
    `button[data-repo="${repoId}"][data-action="${action}"]`
  );
  buttons?.forEach((btn) => (btn.disabled = true));
  try {
    const pathMap = {
      run: `/hub/repos/${repoId}/run`,
      once: `/hub/repos/${repoId}/run`,
      stop: `/hub/repos/${repoId}/stop`,
      resume: `/hub/repos/${repoId}/resume`,
      init: `/hub/repos/${repoId}/init`,
    };
    const path = pathMap[action];
    if (!path) return;
    const payload = action === "once" ? { once: true } : null;
    await api(path, { method: "POST", body: payload });
    flash(`${action} sent to ${repoId}`);
    await refreshHub();
  } catch (err) {
    flash(err.message || "Hub action failed", "error");
  } finally {
    buttons?.forEach((btn) => (btn.disabled = false));
  }
}

function attachHubHandlers() {
  const scanBtn = document.getElementById("hub-scan");
  const refreshBtn = document.getElementById("hub-refresh");
  const quickScanBtn = document.getElementById("hub-quick-scan");
  const newRepoBtn = document.getElementById("hub-new-repo");
  const createCancelBtn = document.getElementById("create-repo-cancel");
  const createSubmitBtn = document.getElementById("create-repo-submit");
  const createRepoId = document.getElementById("create-repo-id");

  scanBtn?.addEventListener("click", () => refreshHub({ scan: true }));
  quickScanBtn?.addEventListener("click", () => refreshHub({ scan: true }));
  refreshBtn?.addEventListener("click", () => refreshHub({ scan: false }));
  hubUsageRefresh?.addEventListener("click", () => loadHubUsage());
  
  newRepoBtn?.addEventListener("click", showCreateRepoModal);
  createCancelBtn?.addEventListener("click", hideCreateRepoModal);
  createSubmitBtn?.addEventListener("click", handleCreateRepoSubmit);
  
  // Allow Enter key in the repo ID input to submit
  createRepoId?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleCreateRepoSubmit();
    }
  });
  
  // Close modal on Escape key
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideCreateRepoModal();
    }
  });
  
  // Close modal when clicking overlay background
  const createRepoModal = document.getElementById("create-repo-modal");
  createRepoModal?.addEventListener("click", (e) => {
    if (e.target === createRepoModal) {
      hideCreateRepoModal();
    }
  });

  repoListEl?.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;

    // Handle action buttons - stop propagation to prevent card navigation
    const btn = target.closest("button[data-action]");
    if (btn) {
      event.stopPropagation();
      const action = btn.dataset.action;
      const repoId = btn.dataset.repo;
      if (action && repoId) {
        handleRepoAction(repoId, action);
      }
      return;
    }

    // Handle card click for navigation
    const card = target.closest(".hub-repo-clickable");
    if (card && card.dataset.href) {
      window.location.href = card.dataset.href;
    }
  });

  // Support keyboard navigation for cards
  repoListEl?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      const target = event.target;
      if (target instanceof HTMLElement && target.classList.contains("hub-repo-clickable")) {
        event.preventDefault();
        if (target.dataset.href) {
          window.location.href = target.dataset.href;
        }
      }
    }
  });
}

export function initHub() {
  if (!repoListEl) return;
  attachHubHandlers();
  refreshHub();
}
