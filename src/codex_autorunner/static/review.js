import { api, flash, statusPill } from "./utils.js";
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
let reviewInterval = null;
async function loadReviewStatus() {
    try {
        const data = (await api("/api/review/status"));
        const review = data.review || {};
        const statusPillEl = $("review-status-pill");
        const runIdEl = $("review-run-id");
        const startedEl = $("review-started");
        const finishedEl = $("review-finished");
        const startBtn = $("review-start");
        const stopBtn = $("review-stop");
        const resetBtn = $("review-reset");
        const status = review.status || "idle";
        const running = review.running || false;
        statusPill(statusPillEl, status);
        setText(runIdEl, review.id);
        setText(startedEl, review.started_at);
        setText(finishedEl, review.finished_at);
        if (startBtn) {
            startBtn.disabled = running;
        }
        if (stopBtn) {
            stopBtn.disabled = !running || status === "stopped";
        }
        if (resetBtn) {
            resetBtn.disabled = running;
        }
        const finalLink = $("review-final-link");
        const logLink = $("review-log-link");
        const bundleLink = $("review-scratchpad-link");
        setLink(finalLink, {
            href: review.final_output_path ? "/api/review/artifact?kind=final_report" : null,
            text: "Final report",
            title: "Open the final review report",
        });
        setLink(logLink, {
            href: review.run_dir ? "/api/review/artifact?kind=workflow_log" : null,
            text: "Log",
            title: "Open the review workflow log",
        });
        setLink(bundleLink, {
            href: review.scratchpad_bundle_path
                ? "/api/review/artifact?kind=scratchpad_bundle"
                : null,
            text: "Scratchpad",
            title: "Download scratchpad files as zip",
        });
    }
    catch (err) {
        console.error("Failed to load review status:", err);
    }
}
async function startReview() {
    try {
        const agentEl = $("review-agent");
        const modelEl = $("review-model");
        const reasoningEl = $("review-reasoning");
        const timeoutEl = $("review-timeout");
        const payload = {
            agent: agentEl?.value || "opencode",
            model: modelEl?.value || "zai-coding-plan/glm-4.7",
            reasoning: reasoningEl?.value || null,
            max_wallclock_seconds: timeoutEl?.value
                ? parseInt(timeoutEl.value, 10) || null
                : null,
        };
        const data = (await api("/api/review/start", {
            method: "POST",
            body: payload,
        }));
        if (data.status === "ok" || data.review) {
            flash("Review started");
            await loadReviewStatus();
        }
    }
    catch (err) {
        console.error("Failed to start review:", err);
        const message = err instanceof Error ? err.message : "Failed to start review";
        flash(message);
    }
}
async function stopReview() {
    try {
        const data = (await api("/api/review/stop", {
            method: "POST",
        }));
        if (data.status === "ok" || data.review) {
            flash("Review stopped");
            await loadReviewStatus();
        }
    }
    catch (err) {
        console.error("Failed to stop review:", err);
        const message = err instanceof Error ? err.message : "Failed to stop review";
        flash(message);
    }
}
async function resetReview() {
    try {
        const data = (await api("/api/review/reset", {
            method: "POST",
        }));
        if (data.status === "ok" || data.review) {
            flash("Review state reset");
            await loadReviewStatus();
        }
    }
    catch (err) {
        console.error("Failed to reset review:", err);
        const message = err instanceof Error ? err.message : "Failed to reset review";
        flash(message);
    }
}
export function initReview() {
    const startBtn = $("review-start");
    const stopBtn = $("review-stop");
    const resetBtn = $("review-reset");
    startBtn?.addEventListener("click", startReview);
    stopBtn?.addEventListener("click", stopReview);
    resetBtn?.addEventListener("click", resetReview);
    loadReviewStatus();
    if (reviewInterval) {
        clearInterval(reviewInterval);
    }
    reviewInterval = setInterval(loadReviewStatus, 5000);
}
