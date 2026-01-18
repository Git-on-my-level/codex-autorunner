import { CONSTANTS } from "./constants.js";
import { BASE_PATH } from "./env.js";

const toast = document.getElementById("toast");
const decoder = new TextDecoder();
const AUTH_TOKEN_KEY = "car_auth_token";

export interface ApiOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export interface StreamOptions {
  method?: string;
  body?: unknown;
  onMessage?: (data: string, event: string) => void;
  onError?: (err: Error) => void;
  onFinish?: () => void;
}

export function getAuthToken(): string | null {
  let token: string | null = null;
  try {
    token = sessionStorage.getItem(AUTH_TOKEN_KEY);
  } catch (_err) {
    token = null;
  }
  if (token) {
    return token;
  }
  if ((window as { __CAR_AUTH_TOKEN?: string }).__CAR_AUTH_TOKEN) {
    return (window as { __CAR_AUTH_TOKEN?: string }).__CAR_AUTH_TOKEN;
  }
  return null;
}

export function resolvePath(path: string): string {
  if (!path) return path;
  const absolutePrefixes = ["http://", "https://", "ws://", "wss://"];
  if (absolutePrefixes.some((prefix) => path.startsWith(prefix))) {
    return path;
  }
  if (!BASE_PATH) {
    return path;
  }
  if (path.startsWith(BASE_PATH)) {
    return path;
  }
  if (path.startsWith("/")) {
    return `${BASE_PATH}${path}`;
  }
  return `${BASE_PATH}/${path}`;
}

export function getUrlParams(): URLSearchParams {
  try {
    return new URLSearchParams(window.location.search || "");
  } catch (_err) {
    return new URLSearchParams();
  }
}

export function updateUrlParams(updates: Record<string, string | number | null | undefined> = {}): void {
  if (!window?.location?.href) return;
  if (typeof history === "undefined" || !history.replaceState) return;
  const url = new URL(window.location.href);
  const params = url.searchParams;
  Object.entries(updates).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      params.delete(key);
    } else {
      params.set(key, String(value));
    }
  });
  url.search = params.toString();
  history.replaceState(null, "", url.toString());
}

export function escapeHtml(value: unknown): string {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function buildWsUrl(path: string, query = ""): string {
  const resolved = resolvePath(path);
  const normalized = resolved.startsWith("/") ? resolved : `/${resolved}`;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
  const suffix = params.toString();
  return `${proto}://${window.location.host}${normalized}${suffix ? `?${suffix}` : ""}`;
}

export function flash(message: string, type: "info" | "error" | "success" = "info"): void {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.remove("error", "success");
  if (type === "error") {
    toast.classList.add("error");
  } else if (type === "success") {
    toast.classList.add("success");
  }
  toast.classList.add("show");
  setTimeout(() => {
    toast.classList.remove("show", "error", "success");
  }, CONSTANTS.UI.TOAST_DURATION);
}

export function statusPill(el: HTMLElement, status: string): void {
  const normalized = status || "idle";
  el.textContent = normalized;
  el.classList.remove("pill-idle", "pill-running", "pill-error", "pill-warn");
  const errorStates = ["error", "init_error"];
  const warnStates = [
    "locked",
    "missing",
    "uninitialized",
    "initializing",
    "interrupted",
  ];
  if (normalized === "running") {
    el.classList.add("pill-running");
  } else if (errorStates.includes(normalized)) {
    el.classList.add("pill-error");
  } else if (warnStates.includes(normalized)) {
    el.classList.add("pill-warn");
  } else {
    el.classList.add("pill-idle");
  }
}

interface ErrorDetailItem {
  msg?: string;
  message?: string;
  loc?: string | string[];
}

function extractErrorDetail(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const payloadObj = payload as Record<string, unknown>;
  const detail = payloadObj.detail ?? payloadObj.message ?? payloadObj.error;
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (!item) return "";
        if (typeof item === "string") return item;
        if (typeof item === "object") {
          const msg = (item as ErrorDetailItem).msg || (item as ErrorDetailItem).message || "";
          const locVal = (item as ErrorDetailItem).loc;
          const loc = Array.isArray(locVal) ? locVal.join(".") : String(locVal || "");
          if (msg && loc) return `${loc}: ${msg}`;
          if (msg) return msg;
        }
        try {
          return JSON.stringify(item);
        } catch (_err) {
          return String(item);
        }
      })
      .filter(Boolean);
    return parts.join(" | ");
  }
  try {
    return JSON.stringify(detail);
  } catch (_err) {
    return String(detail);
  }
}

async function buildErrorMessage(res: Response): Promise<string> {
  if (res.status === 401) {
    return "Unauthorized. Provide a valid token to access this server.";
  }
  let text = "";
  try {
    text = await res.text();
  } catch (_err) {
    text = "";
  }
  let payload: unknown = null;
  const contentType = res.headers.get("content-type") || "";
  const trimmed = text.trim();
  if (
    contentType.includes("application/json") ||
    trimmed.startsWith("{") ||
    trimmed.startsWith("[")
  ) {
    try {
      payload = JSON.parse(text);
    } catch (_err) {
      payload = null;
    }
  }
  const detail = extractErrorDetail(payload);
  if (detail) return detail;
  if (text) return text;
  return `Request failed (${res.status})`;
}

export async function api(path: string, options: ApiOptions = {}): Promise<unknown> {
  const headers: Record<string, string> = options.headers ? { ...options.headers } : {};
  const opts: RequestInit = {
    method: options.method,
    signal: options.signal,
    headers,
  };
  const target = resolvePath(path);
  const token = getAuthToken();
  if (token && !headers.Authorization) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    (opts as Record<string, unknown>).body = JSON.stringify(options.body);
  } else {
    (opts as Record<string, unknown>).body = options.body as BodyInit | null;
  }
  const res = await fetch(target, opts);
  if (!res.ok) {
    const message = await buildErrorMessage(res);
    throw new Error(message);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

export function streamEvents(path: string, options: StreamOptions = {}): () => void {
  const { method = "GET", body = null, onMessage, onError, onFinish } = options;
  const controller = new AbortController();
  let fetchBody: BodyInit | null = body as BodyInit | null;
  const target = resolvePath(path);
  const headers: Record<string, string> = {};
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (fetchBody && typeof fetchBody === "object" && !(fetchBody instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    fetchBody = JSON.stringify(fetchBody);
  }
  fetch(target, { method, body: fetchBody, headers, signal: controller.signal })
    .then(async (res) => {
      if (!res.ok) {
        const message = await buildErrorMessage(res);
        throw new Error(message);
      }
      if (!res.body) {
        throw new Error("Streaming not supported in this browser");
      }
      const reader = res.body.getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";
        for (const chunk of chunks) {
          if (!chunk.trim()) continue;
          const lines = chunk.split("\n");
          let event = "message";
          const dataLines: string[] = [];
          for (const line of lines) {
            if (line.startsWith("event:")) {
              event = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trimStart());
            }
          }
          if (!dataLines.length) continue;
          const data = dataLines.join("\n");
          if (onMessage) onMessage(data, event || "message");
        }
      }
      if (!controller.signal.aborted && onFinish) {
        onFinish();
      }
    })
    .catch((err) => {
      if (controller.signal.aborted) {
        if (onFinish) onFinish();
        return;
      }
      if (onError) onError(err);
      if (onFinish) onFinish();
    });

  return () => controller.abort();
}

export function createPoller(fn: () => Promise<void>, intervalMs: number, { immediate = true } = {}): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const tick = async () => {
    try {
      await fn();
    } finally {
      timer = setTimeout(tick, intervalMs);
    }
  };
  if (immediate) {
    tick();
  } else {
    timer = setTimeout(tick, intervalMs);
  }
  return () => {
    if (timer) clearTimeout(timer);
  };
}

export function isMobileViewport(): boolean {
  try {
    return Boolean(window.matchMedia && window.matchMedia("(max-width: 640px)").matches);
  } catch (_err) {
    return window.innerWidth <= 640;
  }
}

export function setMobileChromeHidden(hidden: boolean): void {
  document.documentElement.classList.toggle("mobile-chrome-hidden", Boolean(hidden));
}

export function setMobileComposeFixed(enabled: boolean): void {
  document.documentElement.classList.toggle("mobile-compose-fixed", Boolean(enabled));
}

const MODAL_BACKGROUND_IDS = ["hub-shell", "repo-shell"];
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type=\"hidden\"])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex=\"-1\"])",
].join(",");
let modalOpenCount = 0;

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  if (!container || !container.querySelectorAll) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
    (el): el is HTMLElement => el && (el as HTMLElement).tabIndex !== -1 && !(el as HTMLElement).hidden && !(el as HTMLButtonElement | HTMLInputElement).disabled
  );
}

function setModalBackgroundHidden(hidden: boolean): void {
  if (hidden) {
    modalOpenCount += 1;
  } else {
    modalOpenCount = Math.max(0, modalOpenCount - 1);
  }
  const shouldHide = modalOpenCount > 0;
  MODAL_BACKGROUND_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (shouldHide) {
      el.setAttribute("aria-hidden", "true");
      try {
        (el as HTMLElement).inert = true;
      } catch (_err) {
        el.setAttribute("inert", "");
      }
    } else {
      el.removeAttribute("aria-hidden");
      try {
        (el as HTMLElement).inert = false;
      } catch (_err) {
        el.removeAttribute("inert");
      }
    }
  });
}

function handleTabKey(event: KeyboardEvent, container: HTMLElement): void {
  const focusable = getFocusableElements(container);
  if (!focusable.length) {
    event.preventDefault();
    container?.focus?.();
    return;
  }
  const currentIndex = focusable.indexOf(document.activeElement as HTMLElement);
  const lastIndex = focusable.length - 1;
  if (event.shiftKey) {
    if (currentIndex <= 0) {
      event.preventDefault();
      focusable[lastIndex].focus();
    }
  } else if (currentIndex === -1 || currentIndex === lastIndex) {
    event.preventDefault();
    focusable[0].focus();
  }
}

export interface ModalOptions {
  closeOnEscape?: boolean;
  closeOnOverlay?: boolean;
  initialFocus?: HTMLElement | null;
  returnFocusTo?: HTMLElement | null;
  onKeydown?: (event: KeyboardEvent) => void;
  onRequestClose?: (reason: string) => void;
}

export function openModal(overlay: HTMLElement, options: ModalOptions = {}): () => void {
  if (!overlay) return () => {};
  const {
    closeOnEscape = true,
    closeOnOverlay = true,
    initialFocus,
    returnFocusTo,
    onKeydown,
    onRequestClose,
  } = options;
  const dialog = overlay.querySelector(".modal-dialog") || overlay;
  const previousActive = returnFocusTo || document.activeElement;
  let isClosed = false;

  const close = () => {
    if (isClosed) return;
    isClosed = true;
    overlay.hidden = true;
    overlay.removeEventListener("click", handleOverlayClick);
    document.removeEventListener("keydown", handleKeydown);
    setModalBackgroundHidden(false);
    if (previousActive && (previousActive as HTMLElement).focus) {
      (previousActive as HTMLElement).focus();
    }
  };

  const requestClose = onRequestClose || close;

  const handleOverlayClick = (event: Event) => {
    if (closeOnOverlay && event.target === overlay) {
      requestClose("overlay");
    }
  };

  const handleKeydown = (event: KeyboardEvent) => {
    if (event.key === "Escape" && closeOnEscape) {
      event.preventDefault();
      requestClose("escape");
      return;
    }
    if (event.key === "Tab") {
      handleTabKey(event, dialog as HTMLElement);
      return;
    }
    if (onKeydown) {
      onKeydown(event);
    }
  };

  overlay.hidden = false;
  setModalBackgroundHidden(true);

  overlay.addEventListener("click", handleOverlayClick);
  document.addEventListener("keydown", handleKeydown);

  const focusTarget = initialFocus || getFocusableElements(dialog as HTMLElement)[0] || dialog;
  if (focusTarget && (focusTarget as HTMLElement).focus) {
    (focusTarget as HTMLElement).focus();
  }

  return close;
}

export interface ConfirmModalOptions {
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

export function confirmModal(message: string, options: ConfirmModalOptions = {}): Promise<boolean> {
  const { confirmText = "Confirm", cancelText = "Cancel", danger = true } = options;
  return new Promise((resolve) => {
    const overlay = document.getElementById("confirm-modal");
    const messageEl = document.getElementById("confirm-modal-message");
    const okBtn = document.getElementById("confirm-modal-ok") as HTMLButtonElement | null;
    const cancelBtn = document.getElementById("confirm-modal-cancel") as HTMLButtonElement | null;

    if (!overlay || !messageEl || !okBtn || !cancelBtn) {
      resolve(false);
      return;
    }

    const triggerEl = document.activeElement;
    messageEl.textContent = message;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    okBtn.className = danger ? "danger" : "primary";
    let closeModal: (() => void) | null = null;
    let settled = false;

    const finalize = (result: boolean) => {
      if (settled) return;
      settled = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      resolve(result);
    };

    const onOk = () => {
      finalize(true);
    };

    const onCancel = () => {
      finalize(false);
    };

    closeModal = openModal(overlay, {
      initialFocus: cancelBtn,
      returnFocusTo: triggerEl as HTMLElement | null,
      onRequestClose: () => finalize(false),
      onKeydown: (event) => {
        if (event.key === "Enter" && document.activeElement === okBtn) {
          event.preventDefault();
          finalize(true);
        }
      },
    });

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
  });
}

export interface InputModalOptions {
  placeholder?: string;
  defaultValue?: string;
  confirmText?: string;
  cancelText?: string;
}

export function inputModal(message: string, options: InputModalOptions = {}): Promise<string | null> {
  const { placeholder = "", defaultValue = "", confirmText = "OK", cancelText = "Cancel" } = options;
  return new Promise((resolve) => {
    const overlay = document.getElementById("input-modal");
    const messageEl = document.getElementById("input-modal-message");
    const inputEl = document.getElementById("input-modal-input") as HTMLInputElement | null;
    const okBtn = document.getElementById("input-modal-ok") as HTMLButtonElement | null;
    const cancelBtn = document.getElementById("input-modal-cancel") as HTMLButtonElement | null;

    if (!overlay || !messageEl || !inputEl || !okBtn || !cancelBtn) {
      resolve(null);
      return;
    }

    const triggerEl = document.activeElement;
    messageEl.textContent = message;
    inputEl.placeholder = placeholder;
    inputEl.value = defaultValue;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    let closeModal: (() => void) | null = null;
    let settled = false;

    const finalize = (result: string | null) => {
      if (settled) return;
      settled = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      resolve(result);
    };

    const onOk = () => {
      const value = inputEl.value.trim();
      finalize(value || null);
    };

    const onCancel = () => {
      finalize(null);
    };

    closeModal = openModal(overlay, {
      initialFocus: inputEl,
      returnFocusTo: triggerEl as HTMLElement | null,
      onRequestClose: () => finalize(null),
      onKeydown: (event) => {
        if (event.key === "Enter") {
          const active = document.activeElement;
          if (active === inputEl || active === okBtn) {
            event.preventDefault();
            onOk();
          }
        }
      },
    });

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);

    inputEl.focus();
    inputEl.select();
  });
}

export interface IngestModalOptions {
  showRefinement?: boolean;
  placeholder?: string;
  defaultValue?: string;
  confirmText?: string;
  cancelText?: string;
}

export interface IngestModalResult {
  confirmed: boolean;
  message: string;
}

export function ingestModal(message: string, options: IngestModalOptions = {}): Promise<IngestModalResult> {
  const {
    showRefinement = false,
    placeholder = "Refine the ingest (optional)...",
    defaultValue = "",
    confirmText = "Ingest",
    cancelText = "Cancel",
  } = options;
  return new Promise((resolve) => {
    const overlay = document.getElementById("ingest-modal");
    const messageEl = document.getElementById("ingest-modal-message");
    const refinementEl = document.getElementById("ingest-modal-refinement");
    const inputEl = document.getElementById("ingest-modal-input") as HTMLTextAreaElement | null;
    const okBtn = document.getElementById("ingest-modal-ok") as HTMLButtonElement | null;
    const cancelBtn = document.getElementById("ingest-modal-cancel") as HTMLButtonElement | null;

    if (!overlay || !messageEl || !refinementEl || !inputEl || !okBtn || !cancelBtn) {
      resolve({ confirmed: false, message: "" });
      return;
    }

    const triggerEl = document.activeElement;
    messageEl.textContent = message;
    refinementEl.classList.toggle("hidden", !showRefinement);
    if (showRefinement) {
      inputEl.placeholder = placeholder;
      inputEl.value = defaultValue;
    }
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    let closeModal: (() => void) | null = null;
    let settled = false;

    const finalize = (result: IngestModalResult) => {
      if (settled) return;
      settled = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      resolve(result);
    };

    const onOk = () => {
      const value = showRefinement ? inputEl.value.trim() : "";
      finalize({ confirmed: true, message: value });
    };

    const onCancel = () => {
      finalize({ confirmed: false, message: "" });
    };

    closeModal = openModal(overlay, {
      initialFocus: showRefinement ? inputEl : okBtn,
      returnFocusTo: triggerEl as HTMLElement | null,
      onRequestClose: () => finalize({ confirmed: false, message: "" }),
      onKeydown: (event) => {
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
          if (showRefinement && document.activeElement === inputEl) {
            event.preventDefault();
            onOk();
          }
        } else if (event.key === "Enter" && !showRefinement) {
          if (document.activeElement === okBtn) {
            event.preventDefault();
            onOk();
          }
        }
      },
    });

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);

    if (showRefinement) {
      inputEl.focus();
    }
  });
}
