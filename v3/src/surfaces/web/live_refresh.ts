/** Progressive enhancement only. No authoritative state or draft content is stored in the browser. */
export const LIVE_REFRESH_JS = `(() => {
  const raw = Number(document.currentScript.dataset.refreshMs ?? "15000");
  const delay = Number.isFinite(raw) ? Math.max(5000, Math.min(60000, raw)) : 15000;
  let dirty = false;
  const notice = document.getElementById("refresh-paused");
  const pause = () => { if (notice) notice.hidden = false; };
  const edited = (event) => { if (event.target.closest("form")) { dirty = true; pause(); } };
  document.addEventListener("input", edited);
  document.addEventListener("change", edited);
  // A dirty form stays dirty after blur. Timer refresh must never discard it.
  const refresh = () => {
    const editing = document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    const reviewing = document.querySelector("details[open]");
    if (document.visibilityState === "visible" && !dirty && !editing && !reviewing) location.reload();
    else { if (dirty || editing || reviewing) pause(); window.setTimeout(refresh, delay); }
  };
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-manual-refresh]");
    if (link && dirty && !window.confirm("Refresh and discard the unsent draft on this page?")) event.preventDefault();
  });
  // Convert readable UTC fallback timestamps to the viewer's local timezone.
  const formatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  for (const time of document.querySelectorAll("time[datetime]")) {
    const value = new Date(time.dateTime);
    if (Number.isFinite(value.getTime())) time.textContent = formatter.format(value);
  }
  window.setTimeout(refresh, delay);
})();`;
