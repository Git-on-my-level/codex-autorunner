import { api, flash, statusPill } from "./utils.js";

let hubData = { repos: [], last_scan_at: null };

const repoListEl = document.getElementById("hub-repo-list");
const lastScanEl = document.getElementById("hub-last-scan");
const totalEl = document.getElementById("hub-count-total");
const runningEl = document.getElementById("hub-count-running");
const missingEl = document.getElementById("hub-count-missing");

function formatTime(isoString) {
  if (!isoString) return "never";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  return date.toLocaleString();
}

function formatRunSummary(repo) {
  if (!repo.initialized) return "Not initialized yet";
  if (!repo.exists_on_disk) return "Missing on disk";
  if (!repo.last_run_id) return "No runs yet";
  const time = repo.last_run_finished_at || repo.last_run_started_at;
  const exit = repo.last_exit_code === null || repo.last_exit_code === undefined
    ? ""
    : ` (exit ${repo.last_exit_code})`;
  return `Run ${repo.last_run_id}${exit} • ${formatTime(time)}`;
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
      '<div class="hub-empty muted">No repos discovered yet. Run a scan to seed new repos.</div>';
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
      ? '<span class="pill pill-small pill-warn">uninitialized</span>'
      : "";
    
    // Build note for errors
    let noteText = "";
    if (!repo.exists_on_disk) {
      noteText = "Repo missing on disk";
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

    card.innerHTML = `
      <div class="hub-repo-main">
        <div class="hub-repo-info">
          <div class="hub-repo-name">
            <span class="hub-repo-title">${repo.display_name}</span>
            <span class="hub-repo-path">${repo.path}</span>
          </div>
          <div class="hub-repo-meta">
            <span class="pill pill-small hub-status-pill">${repo.status}</span>
            ${lockBadge}
            ${initBadge}
            <span class="muted small">${formatRunSummary(repo)}</span>
          </div>
        </div>
        <div class="hub-repo-actions">
          ${actions || '<span class="muted small">–</span>'}
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
  } catch (err) {
    flash(err.message || "Hub request failed", "error");
  } finally {
    setButtonLoading(false);
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

  scanBtn?.addEventListener("click", () => refreshHub({ scan: true }));
  quickScanBtn?.addEventListener("click", () => refreshHub({ scan: true }));
  refreshBtn?.addEventListener("click", () => refreshHub({ scan: false }));

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
