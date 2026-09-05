/** Progressive enhancement only. No authoritative state or draft content is stored in the browser. */
export const LIVE_REFRESH_JS = `(() => {
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
    if (document.getElementById("letter-shortcuts")?.checked === false) return;
    const key = event.key.toLowerCase();
    let link = null;
    if (key === "j") link = document.querySelector('.reader-navigation a[aria-label="Next decision"]') ?? document.querySelector('[data-next-page]');
    if (key === "k") link = document.querySelector('.reader-navigation a[aria-label="Previous decision"]') ?? document.querySelector('[data-previous-page]');
    if (event.key === "Escape") link = document.querySelector('.reader-back');
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
