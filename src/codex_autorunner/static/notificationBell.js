// GENERATED FILE - do not edit directly. Source: static_src/
import { api, escapeHtml, flash, openModal, resolvePath } from "./utils.js";
let bellInitialized = false;
let modalOpen = false;
let closeModal = null;
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
    return payload.title || payload.mode || "Message";
}
function itemBody(item) {
    const payload = item.dispatch || item.message || {};
    return payload.body || "";
}
function renderList(items) {
    const listEl = document.getElementById("notification-list");
    if (!listEl)
        return;
    if (!items.length) {
        listEl.innerHTML = '<div class="muted">No dispatches</div>';
        return;
    }
    const html = items
        .map((item) => {
        const title = itemTitle(item);
        const excerpt = itemBody(item).slice(0, 180);
        const repoLabel = item.repo_display_name || item.repo_id;
        const href = item.open_url || `/repos/${item.repo_id}/?tab=inbox&run_id=${item.run_id}`;
        const seq = item.seq ? `#${item.seq}` : "";
        const nextAction = item.next_action === "reply_and_resume" ? "Next: Reply + resume run" : "";
        return `
        <div class="notification-item">
          <div class="notification-item-header">
            <span class="notification-repo">${escapeHtml(repoLabel)} <span class="muted">(${item.run_id.slice(0, 8)}${seq})</span></span>
            <span class="pill pill-small pill-warn">paused</span>
          </div>
          <div class="notification-title">${escapeHtml(title)}</div>
          <div class="notification-excerpt">${escapeHtml(excerpt)}</div>
          ${nextAction ? `<div class="notification-next muted small">${escapeHtml(nextAction)}</div>` : ""}
          <div class="notification-actions">
            <a class="notification-action" href="${escapeHtml(resolvePath(href))}">Open run</a>
            <button class="notification-action" data-action="copy-run-id" data-run-id="${escapeHtml(item.run_id)}">Copy ID</button>
            ${item.repo_id ? `<button class="notification-action" data-action="copy-repo-id" data-repo-id="${escapeHtml(item.repo_id)}">Copy repo</button>` : ""}
          </div>
        </div>
      `;
    })
        .join("");
    listEl.innerHTML = html;
}
async function fetchNotifications() {
    const payload = (await api("/hub/messages", { method: "GET" }));
    return payload?.items || [];
}
async function refreshNotifications(options = {}) {
    const { silent = true, render = false } = options;
    try {
        const items = await fetchNotifications();
        setBadges(items.length);
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
    bellInitialized = true;
    const buttons = getBellButtons();
    if (!buttons.length)
        return;
    buttons.forEach((btn) => {
        btn.addEventListener("click", () => {
            openNotificationsModal();
        });
    });
    attachModalHandlers();
    void refreshNotifications({ render: false, silent: true });
    window.setInterval(() => {
        if (document.hidden)
            return;
        void refreshNotifications({ render: false, silent: true });
    }, 15000);
}
export const __notificationBellTest = {
    refreshNotifications,
};
