/** Progressive enhancement only. No authoritative state or draft content is stored in the browser. */
export const LIVE_REFRESH_JS = `(() => {
  document.documentElement?.classList?.add?.("js");
  const raw = Number(document.currentScript.dataset.refreshMs ?? "15000");
  const delay = Number.isFinite(raw) ? Math.max(5000, Math.min(60000, raw)) : 15000;
  let dirty = false;
  let reading = false;
  const notice = document.getElementById("refresh-paused");
  const draftGuard = document.getElementById("draft-navigation");
  const discardLink = document.getElementById("discard-draft");
  const keepButton = document.getElementById("keep-draft");
  let draftField = null;
  const pause = () => { if (notice) notice.hidden = false; };
  const edited = (event) => { if (event.target.closest("form")) { dirty = true; draftField = event.target; pause(); } };
  keepButton?.addEventListener("click", () => { if (draftGuard) draftGuard.hidden = true; draftField?.focus(); });
  document.addEventListener("input", edited);
  document.addEventListener("change", edited);
  const shortcutHelp = document.getElementById("keyboard-shortcuts");
  const shortcutToggle = document.getElementById("letter-shortcuts");
  const shortcutPreferenceKey = "car.v3.keyboard-shortcuts.enabled";
  // This is a convenience preference only. Never put reply text, tokens, or
  // authority state in browser storage. Private browsing and hardened browser
  // settings can make storage throw, so the default remains enabled and the
  // rest of the progressive enhancement must continue to work.
  const readShortcutPreference = () => {
    if (!shortcutToggle) return true;
    try {
      const stored = window.localStorage?.getItem(shortcutPreferenceKey);
      if (stored === "0") return false;
      if (stored === "1") return true;
    } catch (_) { /* unavailable storage is not a UI failure */ }
    return true;
  };
  if (shortcutToggle) {
    shortcutToggle.checked = readShortcutPreference();
    shortcutToggle.addEventListener("change", () => {
      try { window.localStorage?.setItem(shortcutPreferenceKey, shortcutToggle.checked ? "1" : "0"); }
      catch (_) { /* keep the in-page setting even when persistence is blocked */ }
    });
  }
  // Completion banners describe the POST that just happened. Remove only
  // their transient query markers from history so a later manual reload does
  // not suggest that the newly displayed item was the one completed.
  if (document.querySelector("[data-transient-notice]") && window.history?.replaceState) {
    try {
      const url = new URL(window.location.href);
      ["triage", "completed", "completed_id", "completed_kind", "recorded"].forEach((key) => url.searchParams.delete(key));
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (_) { /* history is optional progressive enhancement */ }
  }
  if (shortcutHelp) shortcutHelp.hidden = false;
  // Use the same links and native form controls as pointer navigation. The
  // existing draft guard therefore also protects keyboard navigation.
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.isComposing || event.repeat) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (event.key === "Escape") {
      if (draftGuard && !draftGuard.hidden) { event.preventDefault(); keepButton?.click(); return; }
      if (shortcutHelp?.open) { event.preventDefault(); shortcutHelp.open = false; shortcutHelp.querySelector("summary")?.focus(); return; }
    }
    const form = target.closest("form");
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key === "Enter") {
      if (form?.matches('form[action$="/answer"]') && form.querySelector('textarea') && !target.closest('[contenteditable="true"]')) {
        event.preventDefault(); form.requestSubmit();
      }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey || target.closest('input:not([type="radio"]),textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]')) return;
    if (event.key === "?" && shortcutHelp) { event.preventDefault(); shortcutHelp.open = !shortcutHelp.open; if (shortcutHelp.open) shortcutHelp.querySelector("summary")?.focus(); return; }
    if (shortcutHelp?.open) return;
    // Mobile list-only pages retain the desktop reader in the DOM. Hidden
    // controls must not become an invisible draft through global shortcuts.
    const visibleReader = document.querySelector('.mailbox-reader');
    if (visibleReader && visibleReader.getClientRects().length === 0) return;
    if (event.key === "Escape") {
      const back = document.querySelector('.reader-back');
      if (back) { event.preventDefault(); back.click(); }
      return;
    }
    if (shortcutToggle?.checked === false) return;
    const key = event.key.toLowerCase();
    let link = null;
    if (key === "j") link = document.querySelector('.reader-navigation a[aria-label="Next decision"]') ?? document.querySelector('[data-next-page]');
    if (key === "k") link = document.querySelector('.reader-navigation a[aria-label="Previous decision"]') ?? document.querySelector('[data-previous-page]');
    if (link) { event.preventDefault(); link.click(); return; }
    const reply = document.querySelector('.mailbox-reader .reply-form');
    if (key === "r") {
      const editor = reply?.querySelector('textarea[name="text"]') ?? document.querySelector('.mailbox-reader form[action$="/answer"] textarea');
      if (editor) { event.preventDefault(); reply?.querySelector('.custom-choice input')?.click(); editor.focus(); }
    } else if (/^[1-9]$/.test(key) && reply) {
      const choice = reply.querySelectorAll('.reply-choice:not(.custom-choice) input')[Number(key) - 1];
      if (choice) { event.preventDefault(); choice.click(); choice.focus(); }
    }
  });
  // A selected option is often the right starting point, but the human may
  // need to add a condition. Prefill only an untouched custom editor: never
  // overwrite text the human has already started writing.
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const control = target.closest("[data-customize-answer]");
    if (!control) return;
    const form = control.closest("form");
    const editor = form?.querySelector('textarea[name="text"]');
    if (!form || !editor) return;
    form.querySelector('.custom-choice input[name="option_id"]')?.click();
    if (!editor.value) {
      editor.value = control.getAttribute("data-customize-answer") ?? "";
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      const status = form.querySelector("[data-customize-status]");
      if (status) status.textContent = "Kept your existing draft; review it before sending.";
    }
    editor.focus();
  });
  // Native browser validation keeps an empty custom reply on the page.
  for (const form of document.querySelectorAll('.reply-form')) {
    const editor = form.querySelector('textarea[name="text"]');
    const syncRequired = () => { if (editor) editor.required = !form.querySelector('input[name="option_id"]:checked')?.value; };
    form.addEventListener("change", syncRequired);
    // With no selected choice the required radio group handles validation.
    if (editor) editor.required = !form.querySelector('input[name="option_id"]');
  }
  document.addEventListener("submit", () => { dirty = false; });
  window.addEventListener?.("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  document.querySelector('.mail-row-selected')?.scrollIntoView({ block: "nearest" });
  // A mailbox has independent scroll panes. Reloading while the reader is
  // partway through a message would jump back to its subject.
  document.addEventListener("scroll", (event) => {
    if (event.target instanceof Element && event.target.matches(".mailbox-reader, .mailbox-list")) { reading = true; pause(); }
  }, true);
  // A dirty form stays dirty after blur. Timer refresh must never discard it.
  const refresh = () => {
    const editing = document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    const reviewing = document.querySelector("details[open]");
    if (document.visibilityState === "visible" && !dirty && !reading && !editing && !reviewing) location.reload();
    else { if (dirty || reading || editing || reviewing) pause(); window.setTimeout(refresh, delay); }
  };
  document.addEventListener("click", (event) => {
    const destination = event.target.closest("a[href]");
    if (destination && destination === discardLink) { dirty = false; return; }
    if (dirty && destination && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey &&
        destination.target !== "_blank" && !destination.getAttribute("href").startsWith("#")) {
      event.preventDefault();
      if (draftGuard && discardLink) {
        discardLink.href = destination.href;
        draftGuard.hidden = false;
        keepButton?.focus();
      }
    }
  });
  // Convert readable UTC fallback timestamps to the viewer's local timezone.
  const formatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  const compactFormatter = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  for (const time of document.querySelectorAll("time[datetime]")) {
    const value = new Date(time.dateTime);
    if (Number.isFinite(value.getTime())) time.textContent = time.dataset.format === "compact" ? compactFormatter.format(value) : formatter.format(value);
  }
  window.setTimeout(refresh, delay);
})();`;
