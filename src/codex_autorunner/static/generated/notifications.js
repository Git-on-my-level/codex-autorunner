// GENERATED FILE - do not edit directly. Source: static_src/
import { api, confirmModal, escapeHtml, flash, inputModal, openModal, resolvePath } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { registerAutoRefresh } from "./autoRefresh.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
const HUB_HINT_SCOPE_STORAGE_KEY = "car.hub.hint-scope";
const HUB_HINT_SCOPE_EVENT = "car:capability-hint-request";
let notificationsInitialized = false;
const notificationItemsByRoot = {};
let activeRoot = null;
let closeModalFn = null;
let documentListenerInstalled = false;
let modalElements = null;
let isRefreshing = false;
let volatileHubHintScopeKey = null;
const DROPDOWN_MARGIN = 8;
const DROPDOWN_OFFSET = 6;
const NOTIFICATIONS_REFRESH_ID = "notifications";
const NOTIFICATIONS_REFRESH_MS = 15000;
function newBrowserScopeToken() {
    try {
        if (typeof crypto !== "undefined" &&
            "randomUUID" in crypto &&
            typeof crypto.randomUUID === "function") {
            return crypto.randomUUID();
        }
    }
    catch {
        // ignore
    }
    return `scope-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
function getHubHintScopeKey() {
    try {
        const existing = localStorage.getItem(HUB_HINT_SCOPE_STORAGE_KEY);
        if (existing && existing.trim()) {
            return existing.trim();
        }
        const created = `web:browser:${newBrowserScopeToken()}`;
        localStorage.setItem(HUB_HINT_SCOPE_STORAGE_KEY, created);
        return created;
    }
    catch {
        if (!volatileHubHintScopeKey) {
            volatileHubHintScopeKey = `web:browser:${newBrowserScopeToken()}`;
        }
        return volatileHubHintScopeKey;
    }
}
function normalizeCapabilityHintPayload(item) {
    const extra = item.dispatch?.extra || {};
    const payload = item;
    const configKeysRaw = payload.config_keys ?? extra.config_keys;
    const configKeys = Array.isArray(configKeysRaw)
        ? configKeysRaw
            .map((entry) => (typeof entry === "string" ? entry.trim() : ""))
            .filter(Boolean)
        : [];
    return {
        hint_id: (typeof payload.hint_id === "string" && payload.hint_id.trim()) ||
            (typeof extra.hint_id === "string" && extra.hint_id.trim()) ||
            undefined,
        feature_label: (typeof payload.feature_label === "string" && payload.feature_label.trim()) ||
            (typeof extra.feature_label === "string" && extra.feature_label.trim()) ||
            null,
        prompt: (typeof payload.prompt === "string" && payload.prompt.trim()) ||
            (typeof extra.prompt === "string" && extra.prompt.trim()) ||
            null,
        config_keys: configKeys,
        reason_code: (typeof payload.reason_code === "string" && payload.reason_code.trim()) ||
            (typeof extra.reason_code === "string" && extra.reason_code.trim()) ||
            null,
        dismiss_label: (typeof payload.dismiss_label === "string" && payload.dismiss_label.trim()) ||
            (typeof extra.dismiss_label === "string" && extra.dismiss_label.trim()) ||
            null,
        open_label: (typeof payload.open_label === "string" && payload.open_label.trim()) ||
            (typeof extra.open_label === "string" && extra.open_label.trim()) ||
            null,
    };
}
function hubMessagesPath() {
    const params = new URLSearchParams({ scope_key: getHubHintScopeKey() });
    return `/hub/messages?${params.toString()}`;
}
function getModalElements() {
    if (modalElements)
        return modalElements;
    const overlay = document.getElementById("notifications-modal");
    const body = document.getElementById("notifications-modal-body");
    const closeBtn = document.getElementById("notifications-modal-close");
    if (!overlay || !body || !closeBtn)
        return null;
    modalElements = { overlay, body, closeBtn };
    return modalElements;
}
function getRootElements(root) {
    const trigger = root.querySelector("[data-notifications-trigger]");
    const badge = root.querySelector("[data-notifications-badge]");
    const dropdown = root.querySelector("[data-notifications-dropdown]");
    if (!trigger || !badge || !dropdown)
        return null;
    const key = root.getAttribute("data-notifications-root") || "hub";
    return { root, trigger, badge, dropdown, key };
}
function setBadgeCount(rootKey, count) {
    const roots = document.querySelectorAll(`[data-notifications-root="${rootKey}"]`);
    roots.forEach((root) => {
        const elements = getRootElements(root);
        if (!elements)
            return;
        elements.badge.textContent = count > 0 ? String(count) : "";
        elements.badge.classList.toggle("hidden", count <= 0);
        elements.trigger.setAttribute("aria-label", count > 0 ? `Notifications (${count})` : "Notifications");
    });
}
function normalizeHubItem(item) {
    const repoId = String(item.repo_id || "");
    const repoDisplay = item.repo_display_name || repoId;
    const itemType = item.item_type || "run_dispatch";
    const hint = normalizeCapabilityHintPayload(item);
    const mode = item.dispatch?.mode || "";
    const title = (item.title || "").trim() ||
        (item.dispatch?.title || "").trim() ||
        hint.feature_label ||
        "";
    const fallbackTitle = title ||
        (itemType === "capability_hint" ? "Feature hint" : mode || "Dispatch");
    const body = item.body || item.dispatch?.body || "";
    const isInformationalDispatch = itemType === "run_dispatch" && item.dispatch_actionable === false;
    const isHandoff = !isInformationalDispatch &&
        (Boolean(item.dispatch?.is_handoff) || mode === "pause");
    const runId = String(item.run_id || "");
    const openUrl = item.open_url ||
        (itemType === "capability_hint"
            ? `/repos/${repoId}/`
            : `/repos/${repoId}/?tab=inbox&run_id=${runId}`);
    const supersession = item.supersession;
    const isSuperseded = supersession?.superseded === true;
    const isPrimary = supersession?.is_primary === true;
    const supersededBy = supersession?.superseded_by || null;
    const supersededReason = supersession?.reason || null;
    return {
        kind: "hub",
        repoId,
        repoDisplay,
        runId,
        status: item.status || "paused",
        seq: item.seq,
        itemType,
        hintId: hint.hint_id,
        featureLabel: hint.feature_label || undefined,
        prompt: hint.prompt || undefined,
        configKeys: hint.config_keys || undefined,
        reasonCode: hint.reason_code || undefined,
        dismissLabel: hint.dismiss_label || undefined,
        openLabel: hint.open_label || undefined,
        title: fallbackTitle,
        mode,
        body,
        isHandoff,
        openUrl,
        pillLabel: itemType === "capability_hint"
            ? "hint"
            : isHandoff
                ? "handoff"
                : isInformationalDispatch
                    ? "info"
                    : "paused",
        isSuperseded,
        supersededBy,
        supersededReason,
        isPrimary,
    };
}
function normalizePmaItem(item) {
    const title = (item.title || "PMA dispatch").trim();
    const body = item.body || "";
    const priority = (item.priority || "info").toLowerCase();
    const isHandoff = priority === "action";
    return {
        kind: "pma",
        title,
        body,
        isHandoff,
        openUrl: "/?view=pma",
        pillLabel: priority,
        priority,
        links: item.links || [],
    };
}
function getItemsForRoot(rootKey) {
    return notificationItemsByRoot[rootKey] || [];
}
function renderDropdown(root) {
    if (!root)
        return;
    const items = getItemsForRoot(root.key);
    const actionableItems = items
        .map((item, index) => ({ item, index }))
        .filter(({ item }) => !item.isSuperseded);
    const supersededItems = items
        .map((item, index) => ({ item, index }))
        .filter(({ item }) => item.isSuperseded);
    if (!items.length) {
        root.dropdown.innerHTML = '<div class="notifications-empty muted small">No pending dispatches</div>';
        return;
    }
    const actionableHtml = actionableItems
        .map(({ item, index }) => {
        const pill = item.pillLabel || (item.isHandoff ? "handoff" : "paused");
        const primaryClass = item.isPrimary ? "notifications-item-primary" : "";
        return `
        <button class="notifications-item ${primaryClass}" type="button" data-index="${index}">
          <span class="notifications-item-repo">${escapeHtml(item.repoDisplay || "PMA")}</span>
          <span class="notifications-item-title">${escapeHtml(item.title)}</span>
          <span class="pill pill-small pill-warn notifications-item-pill">${escapeHtml(pill)}</span>
        </button>
      `;
    })
        .join("");
    const supersededHtml = supersededItems.length
        ? supersededItems
            .map(({ item, index }) => {
            return `
            <button class="notifications-item notifications-item-superseded muted" type="button" data-index="${index}" title="${escapeHtml(item.supersededReason || "Superseded by newer action")}">
              <span class="notifications-item-repo">${escapeHtml(item.repoDisplay || "PMA")}</span>
              <span class="notifications-item-title">${escapeHtml(item.title)}</span>
              <span class="pill pill-small pill-muted notifications-item-pill">superseded</span>
            </button>
          `;
        })
            .join("")
        : "";
    const supersededSection = supersededItems.length
        ? `<div class="notifications-superseded-section"><div class="notifications-section-label muted small">Superseded</div>${supersededHtml}</div>`
        : "";
    root.dropdown.innerHTML = actionableHtml + supersededSection;
}
function renderDropdownError(root) {
    if (!root)
        return;
    root.dropdown.innerHTML = '<div class="notifications-empty muted small">Failed to load dispatches</div>';
}
function closeDropdown() {
    if (!activeRoot)
        return;
    activeRoot.dropdown.classList.add("hidden");
    activeRoot.dropdown.style.position = "";
    activeRoot.dropdown.style.left = "";
    activeRoot.dropdown.style.right = "";
    activeRoot.dropdown.style.top = "";
    activeRoot.dropdown.style.visibility = "";
    activeRoot.trigger.setAttribute("aria-expanded", "false");
    activeRoot = null;
    removeDocumentListener();
}
function positionDropdown(root) {
    const { trigger, dropdown } = root;
    const triggerRect = trigger.getBoundingClientRect();
    dropdown.style.position = "fixed";
    dropdown.style.left = "0";
    dropdown.style.right = "auto";
    dropdown.style.top = "0";
    dropdown.style.visibility = "hidden";
    const dropdownRect = dropdown.getBoundingClientRect();
    const width = dropdownRect.width || 240;
    const height = dropdownRect.height || 0;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    let left = triggerRect.right - width;
    left = Math.min(Math.max(left, DROPDOWN_MARGIN), viewportWidth - width - DROPDOWN_MARGIN);
    const preferredTop = triggerRect.bottom + DROPDOWN_OFFSET;
    const fallbackTop = triggerRect.top - DROPDOWN_OFFSET - height;
    let top = preferredTop;
    if (preferredTop + height > viewportHeight - DROPDOWN_MARGIN) {
        top = Math.max(DROPDOWN_MARGIN, fallbackTop);
    }
    dropdown.style.left = `${Math.max(DROPDOWN_MARGIN, left)}px`;
    dropdown.style.top = `${Math.max(DROPDOWN_MARGIN, top)}px`;
    dropdown.style.visibility = "";
}
function openDropdown(root) {
    if (activeRoot && activeRoot !== root) {
        activeRoot.dropdown.classList.add("hidden");
        activeRoot.trigger.setAttribute("aria-expanded", "false");
    }
    activeRoot = root;
    renderDropdown(root);
    root.dropdown.classList.remove("hidden");
    positionDropdown(root);
    root.trigger.setAttribute("aria-expanded", "true");
    installDocumentListener();
}
function toggleDropdown(root) {
    if (activeRoot && activeRoot === root && !root.dropdown.classList.contains("hidden")) {
        closeDropdown();
        return;
    }
    openDropdown(root);
}
function installDocumentListener() {
    if (documentListenerInstalled)
        return;
    documentListenerInstalled = true;
    document.addEventListener("pointerdown", handleDocumentPointerDown);
}
function removeDocumentListener() {
    if (!documentListenerInstalled)
        return;
    documentListenerInstalled = false;
    document.removeEventListener("pointerdown", handleDocumentPointerDown);
}
function handleDocumentPointerDown(event) {
    if (!activeRoot)
        return;
    const target = event.target;
    if (!target || !activeRoot.root.contains(target)) {
        closeDropdown();
    }
}
function closeNotificationsModal() {
    if (!closeModalFn)
        return;
    closeModalFn();
    closeModalFn = null;
}
function isSameNotification(a, b) {
    return (a.kind === b.kind &&
        a.repoId === b.repoId &&
        a.runId === b.runId &&
        a.seq === b.seq &&
        a.itemType === b.itemType &&
        a.hintId === b.hintId);
}
function openNotificationsModal(item, returnFocusTo) {
    const modal = getModalElements();
    if (!modal)
        return;
    closeNotificationsModal();
    const body = item.body?.trim() ? escapeHtml(item.body) : '<span class="muted">No message body.</span>';
    if (item.kind === "pma") {
        const priority = item.priority || "info";
        const links = (item.links || [])
            .map((link) => `<a href="${escapeHtml(link.href)}" target="_blank" rel="noopener">${escapeHtml(link.label)}</a>`)
            .join("");
        const linkBlock = links ? `<div class="notifications-modal-links">${links}</div>` : "";
        modal.body.innerHTML = `
      <div class="notifications-modal-meta">
        <div class="notifications-modal-row">
          <span class="notifications-modal-label">Dispatch</span>
          <span class="notifications-modal-value">${escapeHtml(item.title)}</span>
        </div>
        <div class="notifications-modal-row">
          <span class="notifications-modal-label">Priority</span>
          <span class="notifications-modal-value">${escapeHtml(priority)}</span>
        </div>
      </div>
      <div class="notifications-modal-body">${body}</div>
      ${linkBlock}
      <div class="notifications-modal-actions">
        <a class="primary sm notifications-open-run" href="${escapeHtml(resolvePath(item.openUrl))}">Open PMA</a>
      </div>
    `;
    }
    else {
        const isCapabilityHint = item.itemType === "capability_hint";
        if (isCapabilityHint) {
            const configKeys = (item.configKeys || [])
                .map((key) => `<span class="pill pill-small notifications-key-pill">${escapeHtml(key)}</span>`)
                .join("");
            const configBlock = configKeys
                ? `
          <div class="notifications-modal-section">
            <div class="notifications-section-label muted small">Config keys</div>
            <div class="notifications-key-list">${configKeys}</div>
          </div>
        `
                : "";
            const promptBlock = item.prompt
                ? `
          <div class="notifications-modal-section">
            <div class="notifications-section-label muted small">PMA prompt</div>
            <div class="notifications-modal-body notifications-modal-prompt">${escapeHtml(item.prompt)}</div>
          </div>
        `
                : "";
            const reasonBlock = item.reasonCode
                ? `
          <div class="notifications-modal-row muted">
            <span class="notifications-modal-label">Reason</span>
            <span class="notifications-modal-value mono">${escapeHtml(item.reasonCode)}</span>
          </div>
        `
                : "";
            modal.body.innerHTML = `
        <div class="notifications-modal-meta">
          <div class="notifications-modal-row">
            <span class="notifications-modal-label">Repo</span>
            <span class="notifications-modal-value">${escapeHtml(item.repoDisplay || "")}</span>
          </div>
          <div class="notifications-modal-row">
            <span class="notifications-modal-label">Hint</span>
            <span class="notifications-modal-value">${escapeHtml(item.featureLabel || item.title)}</span>
          </div>
          ${reasonBlock}
        </div>
        <div class="notifications-modal-body">${body}</div>
        ${configBlock}
        ${promptBlock}
        <div class="notifications-modal-actions">
          <a class="primary sm notifications-open-run" href="${escapeHtml(resolvePath(item.openUrl))}">${escapeHtml(item.openLabel || "Open repo")}</a>
          <button class="ghost sm notifications-dismiss" type="button">${escapeHtml(item.dismissLabel || "Dismiss")}</button>
        </div>
      `;
            const dismissBtn = modal.body.querySelector(".notifications-dismiss");
            if (dismissBtn) {
                dismissBtn.addEventListener("click", async () => {
                    const confirmed = await confirmModal("Dismiss this feature hint?", {
                        confirmText: item.dismissLabel || "Dismiss",
                        danger: false,
                    });
                    if (!confirmed)
                        return;
                    await api("/hub/messages/resolve", {
                        method: "POST",
                        body: {
                            repo_id: item.repoId,
                            run_id: item.runId,
                            item_type: item.itemType,
                            hint_id: item.hintId,
                            scope_key: getHubHintScopeKey(),
                            action: "dismiss",
                        },
                    });
                    const hubItems = getItemsForRoot("hub").filter((entry) => !isSameNotification(entry, item));
                    notificationItemsByRoot.hub = hubItems;
                    setBadgeCount("hub", hubItems.filter((entry) => !entry.isSuperseded).length);
                    if (activeRoot && activeRoot.key === "hub") {
                        renderDropdown(activeRoot);
                    }
                    closeNotificationsModal();
                    flash("Feature hint dismissed");
                });
            }
        }
        else {
            const runId = item.runId || "";
            const runLabel = item.seq ? `${runId.slice(0, 8)} (#${item.seq})` : runId.slice(0, 8);
            const modeLabel = item.mode ? ` (${item.mode})` : "";
            const supersededBlock = item.isSuperseded
                ? `
          <div class="notifications-modal-row muted">
            <span class="notifications-modal-label">Status</span>
            <span class="notifications-modal-value">Superseded by ${escapeHtml(item.supersededBy || "newer action")}</span>
          </div>
          ${item.supersededReason ? `<div class="notifications-modal-row muted"><span class="notifications-modal-label"></span><span class="notifications-modal-value small">${escapeHtml(item.supersededReason)}</span></div>` : ""}
        `
                : "";
            const primaryLabel = item.isPrimary ? ' <span class="pill pill-small pill-info">primary</span>' : "";
            modal.body.innerHTML = `
        <div class="notifications-modal-meta">
          <div class="notifications-modal-row">
            <span class="notifications-modal-label">Repo</span>
            <span class="notifications-modal-value">${escapeHtml(item.repoDisplay || "")}${primaryLabel}</span>
          </div>
          <div class="notifications-modal-row">
            <span class="notifications-modal-label">Run</span>
            <span class="notifications-modal-value mono">${escapeHtml(runLabel)}</span>
          </div>
          <div class="notifications-modal-row">
            <span class="notifications-modal-label">Dispatch</span>
            <span class="notifications-modal-value">${escapeHtml(item.title)}${escapeHtml(modeLabel)}</span>
          </div>
          ${supersededBlock}
        </div>
        <div class="notifications-modal-body">${body}</div>
        <div class="notifications-modal-actions">
          <a class="primary sm notifications-open-run" href="${escapeHtml(resolvePath(item.openUrl))}">Open run</a>
          ${item.seq ? '<button class="ghost sm notifications-dismiss" type="button">Dismiss</button>' : ""}
        </div>
        <div class="notifications-modal-placeholder">Reply here (coming soon).</div>
      `;
            const dismissBtn = modal.body.querySelector(".notifications-dismiss");
            if (dismissBtn && item.seq) {
                dismissBtn.addEventListener("click", async () => {
                    const confirmed = await confirmModal("Dismiss this inbox item?", {
                        confirmText: "Dismiss",
                        danger: false,
                    });
                    if (!confirmed)
                        return;
                    const reason = await inputModal("Dismiss reason (optional)", {
                        placeholder: "obsolete, resolved elsewhere, ...",
                        confirmText: "Dismiss",
                        allowEmpty: true,
                    });
                    if (reason === null)
                        return;
                    await api("/hub/messages/dismiss", {
                        method: "POST",
                        body: {
                            repo_id: item.repoId,
                            run_id: item.runId,
                            seq: item.seq,
                            reason,
                        },
                    });
                    const hubItems = getItemsForRoot("hub").filter((entry) => !isSameNotification(entry, item));
                    notificationItemsByRoot.hub = hubItems;
                    setBadgeCount("hub", hubItems.filter((entry) => !entry.isSuperseded).length);
                    if (activeRoot && activeRoot.key === "hub") {
                        renderDropdown(activeRoot);
                    }
                    closeNotificationsModal();
                    flash("Dispatch dismissed");
                });
            }
        }
    }
    closeModalFn = openModal(modal.overlay, {
        closeOnEscape: true,
        closeOnOverlay: true,
        initialFocus: modal.closeBtn,
        returnFocusTo: returnFocusTo || null,
    });
}
async function refreshNotifications(_ctx) {
    if (isRefreshing)
        return;
    isRefreshing = true;
    try {
        let hubPayload = null;
        let pmaPayload = null;
        try {
            hubPayload = (await api(hubMessagesPath(), { method: "GET" }));
        }
        catch {
            hubPayload = null;
        }
        try {
            pmaPayload = (await api("/hub/pma/dispatches?include_resolved=false", {
                method: "GET",
            }));
        }
        catch {
            pmaPayload = { items: [] };
        }
        const hubItems = (hubPayload?.items || []).map(normalizeHubItem);
        const pmaItems = (pmaPayload?.items || []).map(normalizePmaItem);
        notificationItemsByRoot.hub = hubItems;
        notificationItemsByRoot.pma = pmaItems;
        setBadgeCount("hub", hubItems.filter((item) => !item.isSuperseded).length);
        setBadgeCount("pma", pmaItems.filter((item) => !item.isSuperseded).length);
        if (activeRoot) {
            renderDropdown(activeRoot);
        }
    }
    catch (_err) {
        if (activeRoot) {
            renderDropdownError(activeRoot);
        }
    }
    finally {
        isRefreshing = false;
    }
}
function attachRoot(root) {
    root.trigger.setAttribute("aria-haspopup", "menu");
    root.trigger.setAttribute("aria-expanded", "false");
    root.trigger.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleDropdown(root);
    });
    root.trigger.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
    });
    root.dropdown.addEventListener("click", (event) => {
        const target = event.target?.closest(".notifications-item");
        if (!target)
            return;
        event.preventDefault();
        event.stopPropagation();
        const index = Number(target.dataset.index || "-1");
        const items = getItemsForRoot(root.key);
        const item = items[index];
        if (!item)
            return;
        closeDropdown();
        const mouseEvent = event;
        if (item.itemType === "capability_hint" || mouseEvent.shiftKey) {
            openNotificationsModal(item, root.trigger);
            return;
        }
        window.location.href = resolvePath(item.openUrl);
    });
}
function findCapabilityHint(items, locator) {
    const normalizedHintId = locator.hintId.trim();
    const normalizedRepoId = typeof locator.repoId === "string" ? locator.repoId.trim() : "";
    if (normalizedRepoId) {
        const repoMatch = items.find((item) => item.itemType === "capability_hint" &&
            item.hintId === normalizedHintId &&
            item.repoId === normalizedRepoId);
        if (repoMatch)
            return repoMatch;
    }
    return items.find((item) => item.itemType === "capability_hint" && item.hintId === normalizedHintId);
}
async function openCapabilityHintByLocator(locator) {
    const hintId = locator.hintId.trim();
    if (!hintId)
        return false;
    let items = getItemsForRoot("hub");
    let match = findCapabilityHint(items, locator);
    if (!match) {
        await refreshNotifications();
        items = getItemsForRoot("hub");
        match = findCapabilityHint(items, locator);
    }
    if (!match) {
        return false;
    }
    openNotificationsModal(match, activeRoot?.trigger || null);
    return true;
}
function attachCapabilityHintListener() {
    window.addEventListener(HUB_HINT_SCOPE_EVENT, (event) => {
        const detail = event.detail;
        const hintId = typeof detail?.hintId === "string" ? detail.hintId.trim() : "";
        const repoId = typeof detail?.repoId === "string" ? detail.repoId.trim() : "";
        if (!hintId)
            return;
        void openCapabilityHintByLocator({ hintId, repoId }).then((opened) => {
            if (!opened) {
                flash("Feature hint unavailable right now", "error");
            }
        });
    });
}
function attachModalHandlers() {
    const modal = getModalElements();
    if (!modal)
        return;
    modal.closeBtn.addEventListener("click", () => {
        closeNotificationsModal();
    });
}
export function initNotifications() {
    if (notificationsInitialized)
        return;
    const roots = Array.from(document.querySelectorAll("[data-notifications-root]"));
    if (!roots.length)
        return;
    roots.forEach((root) => {
        const elements = getRootElements(root);
        if (!elements)
            return;
        attachRoot(elements);
    });
    attachModalHandlers();
    attachCapabilityHintListener();
    registerAutoRefresh(NOTIFICATIONS_REFRESH_ID, {
        callback: refreshNotifications,
        tabId: null,
        interval: NOTIFICATIONS_REFRESH_MS,
        refreshOnActivation: true,
        immediate: true,
    });
    notificationsInitialized = true;
}
export const __notificationsTest = {
    findCapabilityHint,
    getHubHintScopeKey,
};
