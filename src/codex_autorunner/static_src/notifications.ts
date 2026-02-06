import { api, confirmModal, escapeHtml, flash, inputModal, openModal, resolvePath } from "./utils.js";
import { registerAutoRefresh, type RefreshContext } from "./autoRefresh.js";

interface HubDispatch {
  mode?: string;
  title?: string | null;
  body?: string | null;
  extra?: Record<string, unknown> | null;
  is_handoff?: boolean;
}

interface HubMessageItem {
  repo_id: string;
  repo_display_name?: string;
  run_id: string;
  status?: string;
  seq?: number;
  dispatch?: HubDispatch | null;
  open_url?: string;
}

interface PmaDispatchItem {
  id: string;
  title?: string;
  body?: string;
  priority?: string;
  links?: Array<{ label: string; href: string }>;
  created_at?: string;
  resolved_at?: string | null;
}

interface NormalizedNotification {
  kind: "hub" | "pma";
  repoId?: string;
  repoDisplay?: string;
  runId?: string;
  status?: string;
  seq?: number;
  title: string;
  mode?: string;
  body: string;
  isHandoff: boolean;
  openUrl: string;
  pillLabel: string;
  priority?: string;
  links?: Array<{ label: string; href: string }>;
}

interface NotificationRoot {
  root: HTMLElement;
  trigger: HTMLButtonElement;
  badge: HTMLElement;
  dropdown: HTMLElement;
  key: string;
}

interface ModalElements {
  overlay: HTMLElement;
  body: HTMLElement;
  closeBtn: HTMLButtonElement;
}

let notificationsInitialized = false;
const notificationItemsByRoot: Record<string, NormalizedNotification[]> = {};
let activeRoot: NotificationRoot | null = null;
let closeModalFn: (() => void) | null = null;
let documentListenerInstalled = false;
let modalElements: ModalElements | null = null;
let isRefreshing = false;
const DROPDOWN_MARGIN = 8;
const DROPDOWN_OFFSET = 6;

const NOTIFICATIONS_REFRESH_ID = "notifications";
const NOTIFICATIONS_REFRESH_MS = 15000;

function getModalElements(): ModalElements | null {
  if (modalElements) return modalElements;
  const overlay = document.getElementById("notifications-modal");
  const body = document.getElementById("notifications-modal-body");
  const closeBtn = document.getElementById("notifications-modal-close") as HTMLButtonElement | null;
  if (!overlay || !body || !closeBtn) return null;
  modalElements = { overlay, body, closeBtn };
  return modalElements;
}

function getRootElements(root: HTMLElement): NotificationRoot | null {
  const trigger = root.querySelector("[data-notifications-trigger]") as HTMLButtonElement | null;
  const badge = root.querySelector("[data-notifications-badge]") as HTMLElement | null;
  const dropdown = root.querySelector("[data-notifications-dropdown]") as HTMLElement | null;
  if (!trigger || !badge || !dropdown) return null;
  const key = root.getAttribute("data-notifications-root") || "hub";
  return { root, trigger, badge, dropdown, key };
}

function setBadgeCount(rootKey: string, count: number): void {
  const roots = document.querySelectorAll<HTMLElement>(`[data-notifications-root="${rootKey}"]`);
  roots.forEach((root) => {
    const elements = getRootElements(root);
    if (!elements) return;
    elements.badge.textContent = count > 0 ? String(count) : "";
    elements.badge.classList.toggle("hidden", count <= 0);
    elements.trigger.setAttribute(
      "aria-label",
      count > 0 ? `Notifications (${count})` : "Notifications"
    );
  });
}

function normalizeHubItem(item: HubMessageItem): NormalizedNotification {
  const repoId = String(item.repo_id || "");
  const repoDisplay = item.repo_display_name || repoId;
  const mode = item.dispatch?.mode || "";
  const title = (item.dispatch?.title || "").trim();
  const fallbackTitle = title || mode || "Dispatch";
  const body = item.dispatch?.body || "";
  const isHandoff = Boolean(item.dispatch?.is_handoff) || mode === "pause";
  const runId = String(item.run_id || "");
  const openUrl = item.open_url || `/repos/${repoId}/?tab=inbox&run_id=${runId}`;
  return {
    kind: "hub",
    repoId,
    repoDisplay,
    runId,
    status: item.status || "paused",
    seq: item.seq,
    title: fallbackTitle,
    mode,
    body,
    isHandoff,
    openUrl,
    pillLabel: isHandoff ? "handoff" : "paused",
  };
}

function normalizePmaItem(item: PmaDispatchItem): NormalizedNotification {
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

function getItemsForRoot(rootKey: string): NormalizedNotification[] {
  return notificationItemsByRoot[rootKey] || [];
}

function renderDropdown(root: NotificationRoot): void {
  if (!root) return;
  const items = getItemsForRoot(root.key);
  if (!items.length) {
    root.dropdown.innerHTML = '<div class="notifications-empty muted small">No pending dispatches</div>';
    return;
  }
  const html = items
    .map((item, index) => {
      const pill = item.pillLabel || (item.isHandoff ? "handoff" : "paused");
      return `
        <button class="notifications-item" type="button" data-index="${index}">
          <span class="notifications-item-repo">${escapeHtml(item.repoDisplay || "PMA")}</span>
          <span class="notifications-item-title">${escapeHtml(item.title)}</span>
          <span class="pill pill-small pill-warn notifications-item-pill">${escapeHtml(pill)}</span>
        </button>
      `;
    })
    .join("");
  root.dropdown.innerHTML = html;
}

function renderDropdownError(root: NotificationRoot): void {
  if (!root) return;
  root.dropdown.innerHTML = '<div class="notifications-empty muted small">Failed to load dispatches</div>';
}

function closeDropdown(): void {
  if (!activeRoot) return;
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

function positionDropdown(root: NotificationRoot): void {
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

function openDropdown(root: NotificationRoot): void {
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

function toggleDropdown(root: NotificationRoot): void {
  if (activeRoot && activeRoot === root && !root.dropdown.classList.contains("hidden")) {
    closeDropdown();
    return;
  }
  openDropdown(root);
}

function installDocumentListener(): void {
  if (documentListenerInstalled) return;
  documentListenerInstalled = true;
  document.addEventListener("pointerdown", handleDocumentPointerDown);
}

function removeDocumentListener(): void {
  if (!documentListenerInstalled) return;
  documentListenerInstalled = false;
  document.removeEventListener("pointerdown", handleDocumentPointerDown);
}

function handleDocumentPointerDown(event: PointerEvent): void {
  if (!activeRoot) return;
  const target = event.target as Node | null;
  if (!target || !activeRoot.root.contains(target)) {
    closeDropdown();
  }
}

function closeNotificationsModal(): void {
  if (!closeModalFn) return;
  closeModalFn();
  closeModalFn = null;
}

function isSameNotification(a: NormalizedNotification, b: NormalizedNotification): boolean {
  return (
    a.kind === b.kind &&
    a.repoId === b.repoId &&
    a.runId === b.runId &&
    a.seq === b.seq
  );
}

function openNotificationsModal(item: NormalizedNotification, returnFocusTo?: HTMLElement | null): void {
  const modal = getModalElements();
  if (!modal) return;
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
  } else {
    const runId = item.runId || "";
    const runLabel = item.seq ? `${runId.slice(0, 8)} (#${item.seq})` : runId.slice(0, 8);
    const modeLabel = item.mode ? ` (${item.mode})` : "";
    modal.body.innerHTML = `
      <div class="notifications-modal-meta">
        <div class="notifications-modal-row">
          <span class="notifications-modal-label">Repo</span>
          <span class="notifications-modal-value">${escapeHtml(item.repoDisplay || "")}</span>
        </div>
        <div class="notifications-modal-row">
          <span class="notifications-modal-label">Run</span>
          <span class="notifications-modal-value mono">${escapeHtml(runLabel)}</span>
        </div>
        <div class="notifications-modal-row">
          <span class="notifications-modal-label">Dispatch</span>
          <span class="notifications-modal-value">${escapeHtml(item.title)}${escapeHtml(modeLabel)}</span>
        </div>
      </div>
      <div class="notifications-modal-body">${body}</div>
      <div class="notifications-modal-actions">
        <a class="primary sm notifications-open-run" href="${escapeHtml(resolvePath(item.openUrl))}">Open run</a>
        ${item.seq ? '<button class="ghost sm notifications-dismiss" type="button">Dismiss</button>' : ""}
      </div>
      <div class="notifications-modal-placeholder">Reply here (coming soon).</div>
    `;
    const dismissBtn = modal.body.querySelector(
      ".notifications-dismiss"
    ) as HTMLButtonElement | null;
    if (dismissBtn && item.seq) {
      dismissBtn.addEventListener("click", async () => {
        const confirmed = await confirmModal("Dismiss this inbox item?", {
          confirmText: "Dismiss",
          danger: false,
        });
        if (!confirmed) return;
        const reason = await inputModal("Dismiss reason (optional)", {
          placeholder: "obsolete, resolved elsewhere, ...",
          confirmText: "Continue",
        });
        await api("/hub/messages/dismiss", {
          method: "POST",
          body: {
            repo_id: item.repoId,
            run_id: item.runId,
            seq: item.seq,
            reason: reason || "",
          },
        });
        const hubItems = getItemsForRoot("hub").filter(
          (entry) => !isSameNotification(entry, item)
        );
        notificationItemsByRoot.hub = hubItems;
        setBadgeCount("hub", hubItems.length);
        if (activeRoot && activeRoot.key === "hub") {
          renderDropdown(activeRoot);
        }
        closeNotificationsModal();
        flash("Dispatch dismissed");
      });
    }
  }
  closeModalFn = openModal(modal.overlay, {
    closeOnEscape: true,
    closeOnOverlay: true,
    initialFocus: modal.closeBtn,
    returnFocusTo: returnFocusTo || null,
  });
}

async function refreshNotifications(_ctx?: RefreshContext): Promise<void> {
  if (isRefreshing) return;
  isRefreshing = true;
  try {
    let hubPayload: { items?: HubMessageItem[] } | null = null;
    let pmaPayload: { items?: PmaDispatchItem[] } | null = null;
    try {
      hubPayload = (await api("/hub/messages", { method: "GET" })) as {
        items?: HubMessageItem[];
      };
    } catch {
      hubPayload = null;
    }
    try {
      pmaPayload = (await api("/hub/pma/dispatches?include_resolved=false", {
        method: "GET",
      })) as { items?: PmaDispatchItem[] };
    } catch {
      pmaPayload = { items: [] };
    }
    const hubItems = (hubPayload?.items || []).map(normalizeHubItem);
    const pmaItems = (pmaPayload?.items || []).map(normalizePmaItem);
    notificationItemsByRoot.hub = hubItems;
    notificationItemsByRoot.pma = pmaItems;
    setBadgeCount("hub", hubItems.length);
    setBadgeCount("pma", pmaItems.length);
    if (activeRoot) {
      renderDropdown(activeRoot);
    }
  } catch (_err) {
    if (activeRoot) {
      renderDropdownError(activeRoot);
    }
  } finally {
    isRefreshing = false;
  }
}

function attachRoot(root: NotificationRoot): void {
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
    const target = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>(
      ".notifications-item"
    );
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    const index = Number(target.dataset.index || "-1");
    const items = getItemsForRoot(root.key);
    const item = items[index];
    if (!item) return;
    closeDropdown();
    const mouseEvent = event as MouseEvent;
    if (mouseEvent.shiftKey) {
      openNotificationsModal(item, root.trigger);
      return;
    }
    window.location.href = resolvePath(item.openUrl);
  });
}

function attachModalHandlers(): void {
  const modal = getModalElements();
  if (!modal) return;
  modal.closeBtn.addEventListener("click", () => {
    closeNotificationsModal();
  });
}

export function initNotifications(): void {
  if (notificationsInitialized) return;
  const roots = Array.from(document.querySelectorAll<HTMLElement>("[data-notifications-root]"));
  if (!roots.length) return;

  roots.forEach((root) => {
    const elements = getRootElements(root);
    if (!elements) return;
    attachRoot(elements);
  });

  attachModalHandlers();

  registerAutoRefresh(NOTIFICATIONS_REFRESH_ID, {
    callback: refreshNotifications,
    tabId: null,
    interval: NOTIFICATIONS_REFRESH_MS,
    refreshOnActivation: true,
    immediate: true,
  });

  notificationsInitialized = true;
}
