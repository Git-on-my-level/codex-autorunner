import { api, escapeHtml, flash, openModal, resolvePath } from "./utils.js";

interface FreshnessPayload {
  generated_at?: string | null;
  recency_basis?: string | null;
  basis_at?: string | null;
  age_seconds?: number | null;
  stale_threshold_seconds?: number | null;
  is_stale?: boolean | null;
  status?: string | null;
}

interface SupersessionPayload {
  status?: "primary" | "non_primary" | "superseded" | null;
  is_primary?: boolean | null;
  superseded?: boolean | null;
  superseded_by?: string | null;
  reason?: string | null;
}

interface HubMessageItem {
  repo_id: string;
  repo_display_name?: string;
  run_id: string;
  status?: string;
  seq?: number;
  item_type?: string;
  next_action?: string;
  dispatch_actionable?: boolean;
  reason?: string;
  run_state?: {
    state?: string | null;
    blocking_reason?: string | null;
    current_ticket?: string | null;
    last_progress_at?: string | null;
    recommended_action?: string | null;
  };
  canonical_state_v1?: {
    state?: string | null;
    recommended_action?: string | null;
    recommendation_confidence?: "high" | "medium" | "low" | null;
    recommendation_stale_reason?: string | null;
    freshness?: FreshnessPayload | null;
  } | null;
  supersession?: SupersessionPayload | null;
  message?: {
    mode?: string;
    title?: string | null;
    body?: string | null;
  };
  dispatch?: {
    mode?: string;
    title?: string | null;
    body?: string | null;
    is_handoff?: boolean;
  };
  open_url?: string;
}

let bellInitialized = false;
let modalOpen = false;
let closeModal: (() => void) | null = null;
let bellPollIntervalId: ReturnType<typeof setInterval> | null = null;
let unloadCleanupInstalled = false;

function getBellButtons(): HTMLButtonElement[] {
  return Array.from(document.querySelectorAll<HTMLButtonElement>(".notification-bell"));
}

function setBadges(count: number): void {
  getBellButtons().forEach((btn) => {
    const badge = btn.querySelector<HTMLElement>(".notification-badge");
    if (!badge) return;
    if (count > 0) {
      badge.textContent = String(count);
      badge.classList.remove("hidden");
    } else {
      badge.textContent = "";
      badge.classList.add("hidden");
    }
  });
}

function itemTitle(item: HubMessageItem): string {
  const payload = item.dispatch || item.message || {};
  return payload.title || payload.mode || item.reason || "Run requires attention";
}

function itemBody(item: HubMessageItem): string {
  const payload = item.dispatch || item.message || {};
  return payload.body || item.run_state?.blocking_reason || "";
}

function isSuperseded(item: HubMessageItem): boolean {
  return item.supersession?.superseded === true;
}

function isPrimary(item: HubMessageItem): boolean {
  return item.supersession?.is_primary === true;
}

function formatFreshnessAge(ageSeconds: number | null | undefined): string {
  if (typeof ageSeconds !== "number" || !Number.isFinite(ageSeconds) || ageSeconds < 0) {
    return "";
  }
  if (ageSeconds < 60) return `${Math.floor(ageSeconds)}s`;
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m`;
  if (ageSeconds < 86400) return `${Math.floor(ageSeconds / 3600)}h`;
  return `${Math.floor(ageSeconds / 86400)}d`;
}

function freshnessDetail(freshness: FreshnessPayload | null | undefined): string {
  if (!freshness) return "";
  const basis = String(freshness.recency_basis || "").replace(/_/g, " ").trim();
  const age = formatFreshnessAge(freshness.age_seconds);
  if (basis && age) return `${basis} ${age} ago`;
  if (age) return `${age} old`;
  if (basis) return basis;
  return "";
}

function renderList(items: HubMessageItem[]): void {
  const listEl = document.getElementById("notification-list");
  if (!listEl) return;
  if (!items.length) {
    listEl.innerHTML = '<div class="muted">No dispatches</div>';
    return;
  }
  const actionableItems = items.filter((item) => !isSuperseded(item));
  const supersededItems = items.filter(isSuperseded);
  const html = actionableItems
    .map((item) => {
      const title = itemTitle(item);
      const excerpt = itemBody(item).slice(0, 180);
      const repoLabel = item.repo_display_name || item.repo_id;
      const href = item.open_url || `/repos/${item.repo_id}/?tab=inbox&run_id=${item.run_id}`;
      const seq = item.seq ? `#${item.seq}` : "";
      const isInformationalDispatch =
        item.item_type === "run_dispatch" && item.dispatch_actionable === false;
      const canonicalState = item.canonical_state_v1;
      const canonicalRecommendedAction = (canonicalState?.recommended_action || "").trim();
      const legacyRecommendedAction = (item.run_state?.recommended_action || "").trim();
      const recommendedAction = canonicalRecommendedAction || legacyRecommendedAction;
      const canonicalRecommendationConfidence = canonicalState?.recommendation_confidence;
      const canonicalRecommendationIsStale = Boolean(
        canonicalState?.recommendation_stale_reason,
      ) || canonicalRecommendationConfidence === "low";
      const snapshotFreshness = canonicalState?.freshness;
      const snapshotIsStale = snapshotFreshness?.is_stale === true;
      const nextAction = isInformationalDispatch
        ? "Info only"
        : canonicalRecommendationIsStale
          ? recommendedAction
            ? `Suggestion: ${recommendedAction}`
            : "Suggestion only"
          : recommendedAction
          ? `Next: ${recommendedAction}`
          : item.next_action === "reply_and_resume"
            ? "Next: Reply + resume run"
            : "";
      const freshnessLine = snapshotIsStale
        ? `Snapshot stale${freshnessDetail(snapshotFreshness) ? `: ${freshnessDetail(snapshotFreshness)}` : ""}`
        : "";
      const stateLabel =
        canonicalState?.state || item.run_state?.state || item.status || "attention";
      const stateClass = isInformationalDispatch
        ? "pill-info"
        : stateLabel === "paused"
          ? "pill-warn"
          : "pill-caution";
      const primaryIndicator = isPrimary(item) ? "primary-item" : "";
      return `
        <div class="notification-item ${primaryIndicator}">
          <div class="notification-item-header">
            <span class="notification-repo">${escapeHtml(repoLabel)} <span class="muted">(${item.run_id.slice(0, 8)}${seq})</span></span>
            <span class="pill pill-small ${stateClass}">${escapeHtml(stateLabel)}</span>
          </div>
          <div class="notification-title">${escapeHtml(title)}</div>
          <div class="notification-excerpt">${escapeHtml(excerpt)}</div>
          ${nextAction ? `<div class="notification-next muted small">${escapeHtml(nextAction)}</div>` : ""}
          ${freshnessLine ? `<div class="notification-next muted small">${escapeHtml(freshnessLine)}</div>` : ""}
          <div class="notification-actions">
            <a class="notification-action" href="${escapeHtml(resolvePath(href))}">Open run</a>
            <button class="notification-action" data-action="copy-run-id" data-run-id="${escapeHtml(item.run_id)}">Copy ID</button>
            ${item.repo_id ? `<button class="notification-action" data-action="copy-repo-id" data-repo-id="${escapeHtml(item.repo_id)}">Copy repo</button>` : ""}
          </div>
        </div>
      `;
    })
    .join("");
  const supersededHtml = supersededItems.length
    ? supersededItems
        .map((item) => {
          const title = itemTitle(item);
          const repoLabel = item.repo_display_name || item.repo_id;
          const seq = item.seq ? `#${item.seq}` : "";
          const supersededBy = item.supersession?.superseded_by || "newer item";
          const supersededReason = item.supersession?.reason || "Superseded by newer action";
          return `
            <div class="notification-item superseded-item muted">
              <div class="notification-item-header">
                <span class="notification-repo">${escapeHtml(repoLabel)} <span class="muted">(${item.run_id.slice(0, 8)}${seq})</span></span>
                <span class="pill pill-small pill-muted">superseded</span>
              </div>
              <div class="notification-title">${escapeHtml(title)}</div>
              <div class="notification-next small">Replaced by: ${escapeHtml(supersededBy)}</div>
              <div class="notification-next small">${escapeHtml(supersededReason)}</div>
            </div>
          `;
        })
        .join("")
    : "";
  const supersededSection = supersededItems.length
    ? `<div class="notification-superseded-section"><div class="notification-section-label muted small">Superseded items</div>${supersededHtml}</div>`
    : "";
  listEl.innerHTML = html + supersededSection;
}

async function fetchNotifications(): Promise<HubMessageItem[]> {
  const payload = (await api("/hub/messages", { method: "GET" })) as { items?: HubMessageItem[] };
  return payload?.items || [];
}

async function refreshNotifications(options: { silent?: boolean; render?: boolean } = {}): Promise<void> {
  const { silent = true, render = false } = options;
  try {
    const items = await fetchNotifications();
    const actionableItems = items.filter((item) => !isSuperseded(item));
    setBadges(actionableItems.length);
    if (modalOpen || render) {
      renderList(items);
    }
  } catch (err) {
    if (!silent) {
      flash((err as Error).message || "Failed to load dispatches", "error");
    }
    setBadges(0);
    if (modalOpen || render) {
      renderList([]);
    }
  }
}

function openNotificationsModal(): void {
  const modal = document.getElementById("notification-modal") as HTMLElement | null;
  const closeBtn = document.getElementById("notification-close") as HTMLButtonElement | null;
  if (!modal) return;
  if (closeModal) closeModal();
  closeModal = openModal(modal, {
    initialFocus: closeBtn || modal,
    onRequestClose: () => {
      modalOpen = false;
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
    },
  });
  modalOpen = true;
  void refreshNotifications({ render: true, silent: true });
}

function attachModalHandlers(): void {
  const modal = document.getElementById("notification-modal");
  if (!modal) return;
  const closeBtn = document.getElementById("notification-close") as HTMLButtonElement | null;
  const refreshBtn = document.getElementById("notification-refresh") as HTMLButtonElement | null;
  closeBtn?.addEventListener("click", () => {
    if (closeModal) {
      const close = closeModal;
      closeModal = null;
      modalOpen = false;
      close();
    }
  });
  refreshBtn?.addEventListener("click", () => {
    void refreshNotifications({ render: true, silent: false });
  });
  const listEl = document.getElementById("notification-list");
  listEl?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (!target) return;
    const action = target.dataset.action || "";
    if (action === "copy-run-id") {
      const runId = target.dataset.runId || "";
      if (runId) {
        void navigator.clipboard.writeText(runId).then(() => {
          flash("Copied run ID", "info");
        });
      }
    }
    if (action === "copy-repo-id") {
      const repoId = target.dataset.repoId || "";
      if (repoId) {
        void navigator.clipboard.writeText(repoId).then(() => {
          flash("Copied repo ID", "info");
        });
      }
    }
  });
}

export function initNotificationBell(): void {
  if (bellInitialized) return;
  const buttons = getBellButtons();
  if (!buttons.length) return;
  bellInitialized = true;
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      openNotificationsModal();
    });
  });
  attachModalHandlers();
  void refreshNotifications({ render: false, silent: true });
  if (bellPollIntervalId === null) {
    bellPollIntervalId = window.setInterval(() => {
      if (document.hidden) return;
      void refreshNotifications({ render: false, silent: true });
    }, 15000);
  }
  if (!unloadCleanupInstalled) {
    unloadCleanupInstalled = true;
    window.addEventListener("beforeunload", () => {
      if (bellPollIntervalId !== null) {
        clearInterval(bellPollIntervalId);
        bellPollIntervalId = null;
      }
    });
  }
}

export const __notificationBellTest = {
  refreshNotifications,
};
