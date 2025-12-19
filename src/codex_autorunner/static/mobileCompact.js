import {
  isMobileViewport,
  setMobileChromeHidden,
  setMobileComposeFixed,
} from "./utils.js";
import { subscribe } from "./bus.js";
import { getTerminalManager } from "./terminal.js";

const COMPOSE_INPUT_SELECTOR = "#doc-chat-input, #terminal-textarea";
const SEND_BUTTON_SELECTOR = "#doc-chat-send, #terminal-text-send";
let baseViewportHeight = window.innerHeight;
const FORM_FIELD_SELECTOR = "input, textarea, select, [contenteditable=\"true\"]";
const terminalFieldSuppression = {
  active: false,
  touched: new Set(),
};

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
  const viewportHeight = window.innerHeight;
  if (viewportHeight > baseViewportHeight) {
    baseViewportHeight = viewportHeight;
  }
  let bottom = 0;
  if (window.visualViewport) {
    const vv = window.visualViewport;
    const referenceHeight = Math.max(baseViewportHeight, viewportHeight);
    bottom = Math.max(0, referenceHeight - (vv.height + vv.offsetTop));
  }
  const keyboardFallback = window.visualViewport
    ? 0
    : Math.max(0, baseViewportHeight - viewportHeight);
  const inset = bottom || keyboardFallback;
  document.documentElement.style.setProperty("--vv-bottom", `${inset}px`);
}

function isTerminalComposeOpen() {
  const panel = document.getElementById("terminal");
  const input = document.getElementById("terminal-text-input");
  if (!panel || !input) return false;
  if (!panel.classList.contains("active")) return false;
  if (input.classList.contains("hidden")) return false;
  return true;
}

function updateComposeFixed() {
  if (!isMobileViewport()) {
    setMobileComposeFixed(false);
    return;
  }
  const enabled = isComposeFocused() || hasComposeDraft() || isTerminalComposeOpen();
  setMobileComposeFixed(enabled);
  // Always update viewport inset when compose state changes so the composer
  // is positioned correctly above the keyboard even when not focused.
  if (enabled) {
    updateViewportInset();
    updateMobileControlsOffset();
  }
}

/**
 * Measure the actual height of the terminal text input panel and set a CSS
 * variable so the mobile controls can be positioned exactly above it.
 */
function updateMobileControlsOffset() {
  const textInput = document.getElementById("terminal-text-input");
  const mobileControls = document.getElementById("terminal-mobile-controls");
  if (!textInput || !mobileControls) return;
  
  // Get the actual rendered height of the text input panel
  const textInputHeight = textInput.offsetHeight || 0;
  // Add a small gap between controls and text input
  const offset = textInputHeight + 4;
  document.documentElement.style.setProperty("--compose-input-height", `${offset}px`);
  
  // Also set the total height for padding-bottom calculation
  const controlsHeight = mobileControls.offsetHeight || 0;
  const totalHeight = textInputHeight + controlsHeight + 8;
  document.documentElement.style.setProperty("--compose-total-height", `${totalHeight}px`);
}

function isTerminalTextarea(el) {
  return Boolean(el && el instanceof HTMLElement && el.id === "terminal-textarea");
}

function suppressOtherFormFields(activeEl) {
  if (terminalFieldSuppression.active) return;
  if (!activeEl || !(activeEl instanceof HTMLElement)) return;
  terminalFieldSuppression.active = true;
  const fields = Array.from(document.querySelectorAll(FORM_FIELD_SELECTOR));
  fields.forEach((field) => {
    if (!(field instanceof HTMLElement)) return;
    if (field === activeEl) return;
    // Skip already-suppressed fields
    if (field.dataset?.codexFieldSuppressed === "1") return;
    // Skip true hidden inputs (type="hidden")
    if (field instanceof HTMLInputElement && field.type === "hidden") return;
    // NOTE: We intentionally do NOT skip non-visible fields here.
    // iOS may still detect them for the keyboard accessory bar even if they
    // are in inactive panels or have display:none ancestors. Suppressing ALL
    // form fields (except the active one) ensures iOS sees only one input.
    if (field.hasAttribute("tabindex")) {
      field.dataset.codexPrevTabindex = field.getAttribute("tabindex") || "";
    }
    field.dataset.codexFieldSuppressed = "1";
    field.setAttribute("tabindex", "-1");
    if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement || field instanceof HTMLSelectElement) {
      if (field.disabled) {
        field.dataset.codexPrevDisabled = "1";
      }
      field.disabled = true;
    } else if (field.getAttribute("contenteditable") === "true") {
      field.dataset.codexPrevContenteditable = "true";
      field.setAttribute("contenteditable", "false");
    }
    terminalFieldSuppression.touched.add(field);
  });
}

function restoreFormFields() {
  if (!terminalFieldSuppression.active) return;
  terminalFieldSuppression.touched.forEach((field) => {
    if (!(field instanceof HTMLElement)) return;
    if (field.dataset.codexFieldSuppressed !== "1") return;
    const prev = field.dataset.codexPrevTabindex;
    if (prev === undefined) {
      field.removeAttribute("tabindex");
    } else {
      field.setAttribute("tabindex", prev);
    }
    delete field.dataset.codexPrevTabindex;
    delete field.dataset.codexFieldSuppressed;
    if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement || field instanceof HTMLSelectElement) {
      if (field.dataset.codexPrevDisabled === "1") {
        field.disabled = true;
      } else {
        field.disabled = false;
      }
      delete field.dataset.codexPrevDisabled;
    } else if (field.dataset.codexPrevContenteditable === "true") {
      field.setAttribute("contenteditable", "true");
      delete field.dataset.codexPrevContenteditable;
    }
  });
  terminalFieldSuppression.touched.clear();
  terminalFieldSuppression.active = false;
}

export function initMobileCompact() {
  setMobileChromeHidden(false);

  const maybeHide = () => {
    if (!isMobileViewport()) return;
    if (!(isComposeFocused() || hasComposeDraft())) return;
    setMobileChromeHidden(true);
  };

  const show = () => {
    if (!isMobileViewport()) return;
    setMobileChromeHidden(false);
    updateComposeFixed();
    // Force a visual update
    document.documentElement.style.display = 'none';
    document.documentElement.offsetHeight; // trigger reflow
    document.documentElement.style.display = '';
  };

  window.addEventListener("scroll", maybeHide, { passive: true });
  document.addEventListener("scroll", maybeHide, { passive: true, capture: true });
  document.addEventListener("touchmove", maybeHide, { passive: true });
  document.addEventListener("wheel", maybeHide, { passive: true });

  // Proactively suppress fields on touchstart to help iOS hide the accessory bar
  document.addEventListener(
    "touchstart",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!isTerminalTextarea(target)) return;

      // Suppress immediately so when focus happens (after touch/click),
      // iOS sees only one enabled input.
      suppressOtherFormFields(target);

      // Safety: if focus doesn't happen (e.g. scroll), restore after a delay.
      // If focus does happen, the focusout handler will eventually restore.
      // We check activeElement to avoid restoring if we successfully focused.
      setTimeout(() => {
        if (document.activeElement !== target) {
          restoreFormFields();
        }
      }, 1000);
    },
    { passive: true, capture: true }
  );

  document.addEventListener(
    "focusin",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      updateViewportInset();
      updateComposeFixed();
      setMobileChromeHidden(false);
      
      // If we are focusing the terminal input, switch to mobile view
      if (isTerminalTextarea(target)) {
         getTerminalManager()?.enterMobileInputMode();
         suppressOtherFormFields(target);
      }
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
        // Always update viewport inset - keyboard may still be visible or transitioning
        updateViewportInset();
        if (isComposeFocused()) return;
        show();
        getTerminalManager()?.exitMobileInputMode();
        restoreFormFields();
      }, 50);
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

  // Update viewport inset on any focus change when terminal compose is open.
  // This ensures the composer stays positioned correctly above the keyboard
  // even when focus moves to buttons (like mobile control keys).
  document.addEventListener(
    "focusin",
    () => {
      if (!isMobileViewport()) return;
      if (isTerminalComposeOpen()) {
        updateViewportInset();
      }
    },
    true
  );

  window.addEventListener(
    "resize",
    () => {
      if (!isMobileViewport()) {
        setMobileChromeHidden(false);
      }
      updateComposeFixed();
    },
    { passive: true }
  );

  subscribe("tab:change", () => {
    show();
  });

  subscribe("terminal:compose", (data) => {
    if (isMobileViewport() && data && data.open) {
      const el = document.getElementById("terminal-textarea");
      if (el) {
        suppressOtherFormFields(el);
        // Safety restore if focus doesn't follow (e.g. programmed not to focus)
        setTimeout(() => {
          if (document.activeElement !== el) {
            restoreFormFields();
          }
        }, 1000);
      }
    }
    updateViewportInset();
    updateComposeFixed();
    // Delay to ensure DOM has updated with new panel visibility
    requestAnimationFrame(() => updateMobileControlsOffset());
  });

  updateComposeFixed();
  // Initial measurement after layout
  requestAnimationFrame(() => updateMobileControlsOffset());
}
