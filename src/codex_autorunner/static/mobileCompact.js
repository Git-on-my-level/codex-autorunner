import {
  isMobileViewport,
  setMobileChromeHidden,
  setMobileComposeFixed,
} from "./utils.js";
import { subscribe } from "./bus.js";

const COMPOSE_INPUT_SELECTOR = "#doc-chat-input, #terminal-textarea";
const SEND_BUTTON_SELECTOR = "#doc-chat-send, #terminal-text-send";

function isVisible(el) {
  if (!el) return false;
  return Boolean(el.offsetParent || el.getClientRects().length);
}

function isComposeFocused() {
  const el = document.activeElement;
  if (!el || !(el instanceof HTMLElement)) return false;
  return el.matches(COMPOSE_INPUT_SELECTOR);
}

function hasComposeDraft() {
  const inputs = Array.from(document.querySelectorAll(COMPOSE_INPUT_SELECTOR));
  return inputs.some((input) => {
    if (!(input instanceof HTMLTextAreaElement)) return false;
    if (!isVisible(input)) return false;
    return Boolean(input.value && input.value.trim());
  });
}

function updateViewportInset() {
  if (!window.visualViewport) return;
  const vv = window.visualViewport;
  const bottom = Math.max(0, window.innerHeight - (vv.height + vv.offsetTop));
  document.documentElement.style.setProperty("--vv-bottom", `${bottom}px`);
}

function updateComposeFixed() {
  if (!isMobileViewport()) {
    setMobileComposeFixed(false);
    return;
  }
  const enabled = isComposeFocused() || hasComposeDraft();
  setMobileComposeFixed(enabled);
}

export function initMobileCompact() {
  const maybeHide = () => {
    if (!isMobileViewport()) return;
    if (!(isComposeFocused() || hasComposeDraft())) return;
    setMobileChromeHidden(true);
  };

  const show = () => {
    if (!isMobileViewport()) return;
    setMobileChromeHidden(false);
    updateComposeFixed();
  };

  window.addEventListener("scroll", maybeHide, { passive: true });
  document.addEventListener("scroll", maybeHide, { passive: true, capture: true });
  document.addEventListener("touchmove", maybeHide, { passive: true });
  document.addEventListener("wheel", maybeHide, { passive: true });

  document.addEventListener(
    "focusin",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      updateComposeFixed();
      setMobileChromeHidden(false);
    },
    true
  );

  document.addEventListener(
    "focusout",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      setTimeout(() => {
        if (isComposeFocused()) return;
        show();
      }, 0);
    },
    true
  );

  document.addEventListener(
    "click",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.closest(SEND_BUTTON_SELECTOR)) return;
      show();
    },
    true
  );

  document.addEventListener(
    "input",
    (e) => {
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      updateComposeFixed();
    },
    true
  );

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateViewportInset);
    window.visualViewport.addEventListener("scroll", updateViewportInset);
    updateViewportInset();
  }

  window.addEventListener("resize", updateComposeFixed, { passive: true });

  subscribe("tab:change", () => {
    show();
  });

  updateComposeFixed();
}
