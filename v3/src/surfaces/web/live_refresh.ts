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
