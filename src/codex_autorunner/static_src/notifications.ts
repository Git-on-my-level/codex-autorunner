import { api, escapeHtml, openModal, resolvePath } from "./utils.js";
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

interface NormalizedNotification {
  repoId: string;
  repoDisplay: string;
  runId: string;
  status: string;
  seq?: number;
  title: string;
  mode: string;
  body: string;
  isHandoff: boolean;
  openUrl: string;
}

interface NotificationRoot {
  root: HTMLElement;
  trigger: HTMLButtonElement;
  badge: HTMLElement;
  dropdown: HTMLElement;
}

interface ModalElements {
  overlay: HTMLElement;
  body: HTMLElement;
  closeBtn: HTMLButtonElement;
}

let notificationsInitialized = false;
let notificationItems: NormalizedNotification[] = [];
let activeRoot: NotificationRoot | null = null;
let closeModalFn: (() => void) | null = null;
let documentListenerInstalled = false;
let modalElements: ModalElements | null = null;
let isRefreshing = false;

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
  return { root, trigger, badge, dropdown };
}

function setBadgeCount(count: number): void {
  const roots = document.querySelectorAll<HTMLElement>("[data-notifications-root]");
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

function normalizeItem(item: HubMessageItem): NormalizedNotification {
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
  };
}

function renderDropdown(root: NotificationRoot): void {
  if (!root) return;
  if (!notificationItems.length) {
    root.dropdown.innerHTML = '<div class="notifications-empty muted small">No pending dispatches</div>';
    return;
  }
  const html = notificationItems
    .map((item, index) => {
      const pill = item.isHandoff ? "handoff" : "paused";
      return `
        <button class="notifications-item" type="button" data-index="${index}">
          <span class="notifications-item-repo">${escapeHtml(item.repoDisplay)}</span>
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
  activeRoot.trigger.setAttribute("aria-expanded", "false");
  activeRoot = null;
  removeDocumentListener();
}

function openDropdown(root: NotificationRoot): void {
  if (activeRoot && activeRoot !== root) {
    activeRoot.dropdown.classList.add("hidden");
    activeRoot.trigger.setAttribute("aria-expanded", "false");
  }
  activeRoot = root;
  renderDropdown(root);
  root.dropdown.classList.remove("hidden");
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

function openNotificationsModal(item: NormalizedNotification, returnFocusTo?: HTMLElement | null): void {
  const modal = getModalElements();
  if (!modal) return;
  closeNotificationsModal();
  const runLabel = item.seq ? `${item.runId.slice(0, 8)} (#${item.seq})` : item.runId.slice(0, 8);
  const modeLabel = item.mode ? ` (${item.mode})` : "";
  const body = item.body?.trim() ? escapeHtml(item.body) : '<span class="muted">No message body.</span>';
  modal.body.innerHTML = `
    <div class="notifications-modal-meta">
      <div class="notifications-modal-row">
        <span class="notifications-modal-label">Repo</span>
        <span class="notifications-modal-value">${escapeHtml(item.repoDisplay)}</span>
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
    </div>
    <div class="notifications-modal-placeholder">Reply here (coming soon).</div>
  `;
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
    const payload = (await api("/hub/messages", { method: "GET" })) as { items?: HubMessageItem[] };
    const items = payload?.items || [];
    notificationItems = items.map(normalizeItem);
    setBadgeCount(notificationItems.length);
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
    const index = Number(target.dataset.index || "-1");
    const item = notificationItems[index];
    if (!item) return;
    closeDropdown();
    openNotificationsModal(item, root.trigger);
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
