// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash } from "./utils.js";
const UI = {
    status: document.getElementById("snapshot-status"),
    content: document.getElementById("snapshot-content"),
    generate: document.getElementById("snapshot-generate"),
    update: document.getElementById("snapshot-update"),
    regenerate: document.getElementById("snapshot-regenerate"),
    copy: document.getElementById("snapshot-copy"),
    refresh: document.getElementById("snapshot-refresh"),
};
let latest = { exists: false, content: "", state: {} };
let busy = false;
function setBusy(on) {
    busy = on;
    const disabled = !!on;
    for (const btn of [UI.generate, UI.update, UI.regenerate, UI.copy, UI.refresh]) {
        if (!btn)
            continue;
        btn.disabled = disabled;
    }
    if (UI.status)
        UI.status.textContent = on ? "Working…" : "";
}
function render() {
    if (!UI.content)
        return;
    UI.content.value = latest.content || "";
    if (UI.generate)
        UI.generate.classList.toggle("hidden", false);
    if (UI.update)
        UI.update.classList.toggle("hidden", true);
    if (UI.regenerate)
        UI.regenerate.classList.toggle("hidden", true);
    if (UI.copy)
        UI.copy.disabled = busy || !(latest.content || "").trim();
}
async function loadSnapshot({ notify = false } = {}) {
    if (busy)
        return;
    try {
        setBusy(true);
        const data = await api("/api/snapshot");
        latest = {
            exists: !!data?.exists,
            content: data?.content || "",
            state: data?.state || {},
        };
        render();
        if (notify)
            flash(latest.exists ? "Snapshot loaded" : "No snapshot yet");
    }
    catch (err) {
        flash(err?.message || "Failed to load snapshot");
    }
    finally {
        setBusy(false);
    }
}
async function runSnapshot() {
    if (busy)
        return;
    try {
        setBusy(true);
        const data = await api("/api/snapshot", {
            method: "POST",
            body: {},
        });
        latest = {
            exists: true,
            content: data?.content || "",
            state: data?.state || {},
        };
        render();
        flash("Snapshot generated");
    }
    catch (err) {
        flash(err?.message || "Snapshot generation failed");
    }
    finally {
        setBusy(false);
    }
}
async function copyToClipboard() {
    const text = UI.content?.value || "";
    if (!text.trim())
        return;
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            flash("Copied to clipboard");
            return;
        }
    }
    catch {
        // fall through
    }
    try {
        UI.content?.focus();
        UI.content?.select();
        const ok = document.execCommand("copy");
        flash(ok ? "Copied to clipboard" : "Copy failed");
    }
    catch {
        flash("Copy failed");
    }
    finally {
        try {
            UI.content?.setSelectionRange(0, 0);
        }
        catch {
            // ignore
        }
    }
}
export function initSnapshot() {
    if (!UI.content)
        return;
    UI.generate?.addEventListener("click", () => runSnapshot());
    UI.update?.addEventListener("click", () => runSnapshot());
    UI.regenerate?.addEventListener("click", () => runSnapshot());
    UI.copy?.addEventListener("click", copyToClipboard);
    UI.refresh?.addEventListener("click", () => loadSnapshot({ notify: true }));
    loadSnapshot().catch(() => { });
}
