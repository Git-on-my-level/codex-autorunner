// GENERATED FILE - do not edit directly. Source: static_src/
import { api, escapeHtml, flash, openModal, resolvePath } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
let bellInitialized = false;
let modalOpen = false;
let closeModal = null;
let bellPollIntervalId = null;
let unloadCleanupInstalled = false;
function getBellButtons() {
    return Array.from(document.querySelectorAll(".notification-bell"));
}
function setBadges(count) {
    getBellButtons().forEach((btn) => {
        const badge = btn.querySelector(".notification-badge");
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
    });
}
function itemTitle(item) {
    const payload = item.dispatch || item.message || {};
    return payload.title || payload.mode || item.reason || "Run requires attention";
}
function itemBody(item) {
    const payload = item.dispatch || item.message || {};
    return payload.body || item.run_state?.blocking_reason || "";
}
function isSuperseded(item) {
    return item.supersession?.superseded === true;
}
function isPrimary(item) {
    return item.supersession?.is_primary === true;
}
function formatFreshnessAge(ageSeconds) {
    if (typeof ageSeconds !== "number" || !Number.isFinite(ageSeconds) || ageSeconds < 0) {
        return "";
    }
    if (ageSeconds < 60)
        return `${Math.floor(ageSeconds)}s`;
    if (ageSeconds < 3600)
        return `${Math.floor(ageSeconds / 60)}m`;
    if (ageSeconds < 86400)
        return `${Math.floor(ageSeconds / 3600)}h`;
    return `${Math.floor(ageSeconds / 86400)}d`;
}
function freshnessDetail(freshness) {
    if (!freshness)
        return "";
    const basis = String(freshness.recency_basis || "").replace(/_/g, " ").trim();
    const age = formatFreshnessAge(freshness.age_seconds);
    if (basis && age)
        return `${basis} ${age} ago`;
    if (age)
        return `${age} old`;
    if (basis)
        return basis;
    return "";
}
function renderList(items) {
    const listEl = document.getElementById("notification-list");
    if (!listEl)
        return;
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
        const isInformationalDispatch = item.item_type === "run_dispatch" && item.dispatch_actionable === false;
        const canonicalState = item.canonical_state_v1;
        const recommendedAction = (canonicalState?.recommended_action || "").trim();
        const canonicalRecommendationConfidence = canonicalState?.recommendation_confidence;
        const canonicalRecommendationIsStale = Boolean(canonicalState?.recommendation_stale_reason) || canonicalRecommendationConfidence === "low";
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
        const stateLabel = canonicalState?.state || item.run_state?.state || item.status || "attention";
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
async function fetchNotifications() {
    const payload = (await api("/hub/messages", { method: "GET" }));
    return payload?.items || [];
}
async function refreshNotifications(options = {}) {
    const { silent = true, render = false } = options;
    try {
        const items = await fetchNotifications();
        const actionableItems = items.filter((item) => !isSuperseded(item));
        setBadges(actionableItems.length);
        if (modalOpen || render) {
            renderList(items);
        }
    }
    catch (err) {
        if (!silent) {
            flash(err.message || "Failed to load dispatches", "error");
        }
        setBadges(0);
        if (modalOpen || render) {
            renderList([]);
        }
    }
}
function openNotificationsModal() {
    const modal = document.getElementById("notification-modal");
    const closeBtn = document.getElementById("notification-close");
    if (!modal)
        return;
    if (closeModal)
        closeModal();
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
function attachModalHandlers() {
    const modal = document.getElementById("notification-modal");
    if (!modal)
        return;
    const closeBtn = document.getElementById("notification-close");
    const refreshBtn = document.getElementById("notification-refresh");
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
        const target = event.target;
        if (!target)
            return;
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
export function initNotificationBell() {
    if (bellInitialized)
        return;
    const buttons = getBellButtons();
    if (!buttons.length)
        return;
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
            if (document.hidden)
                return;
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
