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
  replacements: new Map(), // Map<placeholder, originalElement>
};

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
    // Skip true hidden inputs (type="hidden") as they don't trigger the bar
    if (field instanceof HTMLInputElement && field.type === "hidden") return;
    
    // For iOS to hide the accessory bar, the focused element must be the ONLY
    // focusable element in the DOM. Even "display: none" or "disabled" inputs
    // can trigger the bar in some heuristics.
    // The safest way is to temporarily remove them from the DOM.
    
    // Create a placeholder. If the field is visible, we should try to preserve layout,
    // but usually in the terminal view other inputs are hidden or in other panels.
    // We'll use a hidden span as a marker.
    const placeholder = document.createElement("span");
    placeholder.style.display = "none";
    placeholder.dataset.codexPlaceholder = "1";
    
    if (field.parentNode) {
        terminalFieldSuppression.replacements.set(placeholder, field);
        field.replaceWith(placeholder);
    }
  });
}

function restoreFormFields() {
  if (!terminalFieldSuppression.active) return;
  
  // Restore elements from placeholders
  for (const [placeholder, field] of terminalFieldSuppression.replacements) {
      if (placeholder.parentNode) {
          placeholder.replaceWith(field);
      }
  }
  
  terminalFieldSuppression.replacements.clear();
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
