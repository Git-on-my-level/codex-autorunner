import { api, flash, resolvePath, statusPill, streamEvents } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
function $(id) {
    return document.getElementById(id);
}
function setText(el, text) {
    if (!el)
        return;
    el.textContent = text ?? "–";
}
function setLink(el, { href, text, title } = {}) {
    if (!el)
        return;
    if (href) {
        el.href = href;
        el.target = "_blank";
        el.rel = "noopener noreferrer";
        el.classList.remove("muted");
        el.textContent = text || href;
        if (title)
            el.title = title;
    }
    else {
        el.removeAttribute("href");
        el.removeAttribute("target");
        el.removeAttribute("rel");
        el.classList.add("muted");
        el.textContent = text || "–";
        if (title)
            el.title = title;
    }
}
async function copyToClipboard(text) {
    if (!text)
        return false;
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    }
    catch (err) {
        // ignore
    }
    return false;
}
async function loadGitHubStatus() {
    const pill = $("github-status-pill");
    const note = $("github-note");
    const syncBtn = $("github-sync-pr");
    const openFilesBtn = $("github-open-pr-files");
    const copyPrBtn = $("github-copy-pr");
    try {
        const data = await api("/api/github/status");
        const gh = (data.gh || {});
        const repo = (data.repo || null);
        const git = (data.git || {});
        const link = (data.link || {});
        const issue = (link.issue || null);
        const pr = (link.pr || null);
        const prLinks = (data.pr_links || null);
        if (!gh.available) {
            statusPill(pill, "error");
            setText(note, "GitHub CLI (gh) not available.");
            if (syncBtn)
                syncBtn.disabled = true;
        }
        else if (!gh.authenticated) {
            statusPill(pill, "warn");
            setText(note, "GitHub CLI not authenticated.");
            if (syncBtn)
                syncBtn.disabled = true;
        }
        else {
            statusPill(pill, "idle");
            setText(note, git.clean ? "Clean working tree." : "Uncommitted changes.");
            if (syncBtn)
                syncBtn.disabled = false;
        }
        setLink($("github-repo-link"), {
            href: repo?.url,
            text: repo?.nameWithOwner || "–",
            title: repo?.url || "",
        });
        setText($("github-branch"), git.branch || "–");
        setLink($("github-issue-link"), {
            href: issue?.url,
            text: issue?.number ? `#${issue.number}` : "–",
            title: issue?.title || issue?.url || "",
        });
        const prUrl = prLinks?.url || pr?.url || null;
        setLink($("github-pr-link"), {
            href: prUrl || "",
            text: pr?.number ? `#${pr.number}` : prUrl ? "PR" : "–",
            title: pr?.title || prUrl || "",
        });
        const hasPr = !!prUrl;
        if (openFilesBtn)
            openFilesBtn.disabled = !hasPr;
        if (copyPrBtn)
            copyPrBtn.disabled = !hasPr;
        if (openFilesBtn) {
            openFilesBtn.onclick = () => {
                const files = prLinks?.files || (prUrl ? `${prUrl}/files` : null);
                if (!files)
                    return;
                window.open(files, "_blank", "noopener,noreferrer");
            };
        }
        if (copyPrBtn) {
            copyPrBtn.onclick = async () => {
                if (!prUrl)
                    return;
                const ok = await copyToClipboard(prUrl);
                flash(ok ? "Copied PR link" : "Copy failed", ok ? "info" : "error");
            };
        }
        if (syncBtn) {
            // Hub install: PR sync always operates on the current worktree/branch.
            syncBtn.mode = "current";
        }
    }
    catch (err) {
        statusPill(pill, "error");
        setText(note, err.message || "Failed to load GitHub status");
        if (syncBtn)
            syncBtn.disabled = true;
    }
}
function prFlowEls() {
    return {
        statusPill: $("pr-flow-status"),
        mode: $("pr-flow-mode"),
        ref: $("pr-flow-ref"),
        base: $("pr-flow-base"),
        until: $("pr-flow-until"),
        cycles: $("pr-flow-cycles"),
        runs: $("pr-flow-runs"),
        timeout: $("pr-flow-timeout"),
        draft: $("pr-flow-draft"),
        step: $("pr-flow-step"),
        cycle: $("pr-flow-cycle"),
        review: $("pr-flow-review"),
        reviewLink: $("pr-flow-review-link"),
        logLink: $("pr-flow-log-link"),
        finalLink: $("pr-flow-final-link"),
        startBtn: $("pr-flow-start"),
        stopBtn: $("pr-flow-stop"),
        resumeBtn: $("pr-flow-resume"),
        collectBtn: $("pr-flow-collect"),
    };
}
function formatReviewSummary(summary) {
    if (!summary)
        return "–";
    const total = summary.total ?? 0;
    const major = summary.major ?? 0;
    const minor = summary.minor ?? 0;
    if (total === 0)
        return "No issues";
    return `${total} issues (${major} major, ${minor} minor)`;
}
function setArtifactLink(el, kind, hasValue) {
    if (!el)
        return;
    if (!hasValue) {
        setLink(el, { href: undefined, text: el.textContent || "–" });
        return;
    }
    setLink(el, {
        href: resolvePath(`/api/github/pr_flow/artifact?kind=${kind}`),
        text: el.textContent || kind,
        title: `Open ${kind.replace("_", " ")}`,
    });
}
function setButtonBusy(btn, busy) {
    if (!btn)
        return;
    btn.disabled = busy;
    btn.classList.toggle("loading", busy);
}
function setButtonsDisabled(buttons, disabled) {
    const previous = buttons.map((btn) => (btn ? btn.disabled : true));
    buttons.forEach((btn) => {
        if (btn)
            btn.disabled = disabled;
    });
    return previous;
}
function restoreButtonsDisabled(buttons, previous) {
    buttons.forEach((btn, idx) => {
        if (!btn)
            return;
        btn.disabled = previous[idx] ?? btn.disabled;
        btn.classList.remove("loading");
    });
}
function setTemporaryNote(note, message) {
    if (!note)
        return "";
    const previous = note.textContent || "";
    note.textContent = message;
    return previous;
}
function restoreTemporaryNote(note, previous, message) {
    if (!note)
        return;
    if ((note.textContent || "") === message) {
        note.textContent = previous;
    }
}
async function loadPrFlowStatus() {
    const els = prFlowEls();
    if (!els.statusPill)
        return;
    try {
        const data = await api("/api/github/pr_flow/status");
        const flow = (data.flow || {});
        statusPill(els.statusPill, flow.status || "idle");
        setText(els.step, flow.step || "–");
        setText(els.cycle, flow.cycle ? String(flow.cycle) : "–");
        setText(els.review, formatReviewSummary(flow.review_summary));
        setArtifactLink(els.reviewLink, "review_bundle", !!flow.review_bundle_path);
        setArtifactLink(els.logLink, "workflow_log", !!flow.workflow_log_path);
        setArtifactLink(els.finalLink, "final_report", !!flow.final_report_path);
        const running = flow.status === "running" || flow.status === "stopping";
        if (els.startBtn)
            els.startBtn.disabled = running;
        if (els.stopBtn)
            els.stopBtn.disabled = !running;
        if (els.resumeBtn)
            els.resumeBtn.disabled = running;
    }
    catch (_err) {
        statusPill(els.statusPill, "error");
        setText(els.step, "Error");
    }
}
function prFlowPayload() {
    const els = prFlowEls();
    if (!els.mode || !els.ref)
        return null;
    const mode = els.mode.value || "issue";
    const ref = (els.ref.value || "").trim();
    if (!ref)
        return null;
    const payload = {
        mode,
        draft: !!els.draft?.checked,
        base_branch: (els.base?.value || "").trim() || null,
        stop_condition: (els.until?.value || "").trim() || null,
    };
    const cycles = parseInt(els.cycles?.value || "", 10);
    if (!Number.isNaN(cycles) && cycles > 0)
        payload.max_cycles = cycles;
    const runs = parseInt(els.runs?.value || "", 10);
    if (!Number.isNaN(runs) && runs > 0)
        payload.max_implementation_runs = runs;
    const timeout = parseInt(els.timeout?.value || "", 10);
    if (!Number.isNaN(timeout) && timeout >= 0)
        payload.max_wallclock_seconds = timeout;
    if (mode === "issue") {
        payload.issue = ref;
    }
    else {
        payload.pr = ref;
    }
    return payload;
}
async function startPrFlow() {
    const els = prFlowEls();
    const note = $("github-note");
    setTemporaryNote(note, "PR flow: click received.");
    const payload = prFlowPayload();
    if (!payload) {
        setTemporaryNote(note, "Provide an issue or PR reference.");
        flash("Provide an issue or PR reference", "error");
        return;
    }
    const buttons = [els.startBtn, els.stopBtn, els.resumeBtn, els.collectBtn];
    const prevDisabled = setButtonsDisabled(buttons, true);
    setButtonBusy(els.startBtn, true);
    const message = "Starting PR flow...";
    const prevNote = setTemporaryNote(note, message);
    try {
        await api("/api/github/pr_flow/start", { method: "POST", body: payload });
        flash("PR flow started");
    }
    catch (err) {
        flash(err.message || "PR flow start failed", "error");
    }
    finally {
        restoreButtonsDisabled(buttons, prevDisabled);
        restoreTemporaryNote(note, prevNote, message);
    }
    await loadPrFlowStatus();
}
async function stopPrFlow() {
    const els = prFlowEls();
    const note = $("github-note");
    setTemporaryNote(note, "PR flow: click received.");
    const buttons = [els.startBtn, els.stopBtn, els.resumeBtn, els.collectBtn];
    const prevDisabled = setButtonsDisabled(buttons, true);
    setButtonBusy(els.stopBtn, true);
    const message = "Stopping PR flow...";
    const prevNote = setTemporaryNote(note, message);
    try {
        await api("/api/github/pr_flow/stop", { method: "POST", body: {} });
        flash("PR flow stopping");
    }
    catch (err) {
        flash(err.message || "PR flow stop failed", "error");
    }
    finally {
        restoreButtonsDisabled(buttons, prevDisabled);
        restoreTemporaryNote(note, prevNote, message);
    }
    await loadPrFlowStatus();
}
async function resumePrFlow() {
    const els = prFlowEls();
    const note = $("github-note");
    setTemporaryNote(note, "PR flow: click received.");
    const buttons = [els.startBtn, els.stopBtn, els.resumeBtn, els.collectBtn];
    const prevDisabled = setButtonsDisabled(buttons, true);
    setButtonBusy(els.resumeBtn, true);
    const message = "Resuming PR flow...";
    const prevNote = setTemporaryNote(note, message);
    try {
        await api("/api/github/pr_flow/resume", { method: "POST", body: {} });
        flash("PR flow resumed");
    }
    catch (err) {
        flash(err.message || "PR flow resume failed", "error");
    }
    finally {
        restoreButtonsDisabled(buttons, prevDisabled);
        restoreTemporaryNote(note, prevNote, message);
    }
    await loadPrFlowStatus();
}
async function collectPrFlow() {
    const els = prFlowEls();
    const note = $("github-note");
    setTemporaryNote(note, "PR flow: click received.");
    const buttons = [els.startBtn, els.stopBtn, els.resumeBtn, els.collectBtn];
    const prevDisabled = setButtonsDisabled(buttons, true);
    setButtonBusy(els.collectBtn, true);
    const message = "Collecting PR reviews...";
    const prevNote = setTemporaryNote(note, message);
    try {
        await api("/api/github/pr_flow/collect", { method: "POST", body: {} });
        flash("Review bundle updated");
    }
    catch (err) {
        flash(err.message || "Review collection failed", "error");
    }
    finally {
        restoreButtonsDisabled(buttons, prevDisabled);
        restoreTemporaryNote(note, prevNote, message);
    }
    await loadPrFlowStatus();
}
async function syncPr() {
    const syncBtn = $("github-sync-pr");
    const note = $("github-note");
    if (!syncBtn)
        return;
    syncBtn.disabled = true;
    syncBtn.classList.add("loading");
    const message = "Syncing PR...";
    const prevNote = setTemporaryNote(note, message);
    try {
        const res = await api("/api/github/pr/sync", {
            method: "POST",
            body: { draft: true },
        });
        const created = res.created;
        flash(created ? "PR created" : "PR synced");
        setText(note, "");
        await loadGitHubStatus();
    }
    catch (err) {
        flash(err.message || "PR sync failed", "error");
    }
    finally {
        syncBtn.disabled = false;
        syncBtn.classList.remove("loading");
        restoreTemporaryNote(note, prevNote, message);
    }
}
function startPrFlowEventStream() {
    const note = $("github-note");
    const stop = streamEvents("/api/github/pr_flow/events", {
        onMessage: (_data, _event) => {
            void loadPrFlowStatus();
        },
        onError: (err) => {
            setTemporaryNote(note, err.message || "PR flow events unavailable");
            if (stop)
                stop();
        },
    });
}
export function initGitHub() {
    const card = $("github-card");
    if (!card)
        return;
    card.dataset.githubInitialized = "true";
    console.debug("[github] init");
    const syncBtn = $("github-sync-pr");
    if (syncBtn)
        syncBtn.addEventListener("click", syncPr);
    const els = prFlowEls();
    if (els.startBtn)
        els.startBtn.addEventListener("click", startPrFlow);
    if (els.stopBtn)
        els.stopBtn.addEventListener("click", stopPrFlow);
    if (els.resumeBtn)
        els.resumeBtn.addEventListener("click", resumePrFlow);
    if (els.collectBtn)
        els.collectBtn.addEventListener("click", collectPrFlow);
    // Initial load + auto-refresh while dashboard is active.
    loadGitHubStatus();
    loadPrFlowStatus();
    registerAutoRefresh("github-status", {
        callback: loadGitHubStatus,
        tabId: null, // global: keep PR link available while browsing other tabs (mobile-friendly)
        interval: CONSTANTS.UI?.AUTO_REFRESH_INTERVAL || 15000,
        refreshOnActivation: true,
        immediate: false,
    });
    registerAutoRefresh("pr-flow-status", {
        callback: loadPrFlowStatus,
        tabId: null,
        interval: CONSTANTS.UI?.AUTO_REFRESH_INTERVAL || 15000,
        refreshOnActivation: true,
        immediate: false,
    });
    startPrFlowEventStream();
}
