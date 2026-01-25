// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, confirmModal, openModal } from "./utils.js";
import { subscribe } from "./bus.js";
import { saveToCache, loadFromCache } from "./cache.js";
import { renderTodoPreview } from "./todoPreview.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";
let pendingSummaryOpen = false;
const usageChartState = {
    segment: "none",
    bucket: "day",
    windowDays: 30,
};
let usageSeriesRetryTimer = null;
let usageSummaryRetryTimer = null;
let latestMessageStats = null;
function updateTodoPreview(content) {
    renderTodoPreview(content || "");
    if (content !== undefined) {
        saveToCache("todo-doc", content || "");
    }
}
function handleDocsEvent(payload) {
    if (!payload)
        return;
    if (payload.kind === "todo") {
        updateTodoPreview(payload.content || "");
        return;
    }
    if (typeof payload.todo === "string") {
        updateTodoPreview(payload.todo);
    }
}
async function loadTodoPreview(options = {}) {
    const { silent = false } = options;
    try {
        const data = await api("/api/docs");
        updateTodoPreview(data?.todo || "");
    }
    catch (err) {
        if (!silent) {
            flash(err.message || "Failed to load TODO preview", "error");
        }
    }
}
function setUsageLoading(loading) {
    const btn = document.getElementById("usage-refresh");
    if (!btn)
        return;
    btn.disabled = loading;
    btn.classList.toggle("loading", loading);
}
function formatTokensCompact(val) {
    if (val === null || val === undefined)
        return "–";
    const num = Number(val);
    if (Number.isNaN(num))
        return String(val);
    if (num >= 1000000)
        return `${(num / 1000000).toFixed(1)}M`;
    if (num >= 1000)
        return `${(num / 1000).toFixed(0)}k`;
    return num.toLocaleString();
}
function formatTokensAxis(val) {
    if (val === null || val === undefined)
        return "0";
    const num = Number(val);
    if (Number.isNaN(num))
        return "0";
    if (num >= 1000000)
        return `${(num / 1000000).toFixed(1)}M`;
    if (num >= 1000)
        return `${(num / 1000).toFixed(1)}k`;
    return Math.round(num).toString();
}
function renderUsageProgressBar(container, percent, windowMinutes) {
    if (!container)
        return;
    const pct = typeof percent === "number" ? Math.min(100, Math.max(0, percent)) : 0;
    const hasData = typeof percent === "number";
    let barClass = "usage-bar-ok";
    if (pct >= 90)
        barClass = "usage-bar-critical";
    else if (pct >= 70)
        barClass = "usage-bar-warning";
    container.innerHTML = `
    <div class="usage-progress-bar ${hasData ? "" : "usage-progress-bar-empty"}">
      <div class="usage-progress-fill ${barClass}" style="width: ${pct}%"></div>
    </div>
    <span class="usage-progress-label">${hasData ? `${pct}%` : "–"}${windowMinutes ? `/${windowMinutes}m` : ""}</span>
  `;
}
function renderUsage(data) {
    if (data)
        saveToCache("usage", data);
    const totals = data?.totals || {};
    const events = data?.events ?? 0;
    const rate = data?.latest_rate_limits;
    const codexHome = data?.codex_home || "–";
    const eventsEl = document.getElementById("usage-events");
    if (eventsEl) {
        eventsEl.textContent = `${events} ev`;
    }
    const totalEl = document.getElementById("usage-total");
    const inputEl = document.getElementById("usage-input");
    const cachedEl = document.getElementById("usage-cached");
    const outputEl = document.getElementById("usage-output");
    const reasoningEl = document.getElementById("usage-reasoning");
    const ratesEl = document.getElementById("usage-rates");
    const metaEl = document.getElementById("usage-meta");
    const primaryBarEl = document.getElementById("usage-rate-primary");
    const secondaryBarEl = document.getElementById("usage-rate-secondary");
    if (totalEl)
        totalEl.textContent = formatTokensCompact(totals.total_tokens);
    if (inputEl)
        inputEl.textContent = formatTokensCompact(totals.input_tokens);
    if (cachedEl)
        cachedEl.textContent = formatTokensCompact(totals.cached_input_tokens);
    if (outputEl)
        outputEl.textContent = formatTokensCompact(totals.output_tokens);
    if (reasoningEl)
        reasoningEl.textContent = formatTokensCompact(totals.reasoning_output_tokens);
    if (rate) {
        const primary = rate.primary || {};
        const secondary = rate.secondary || {};
        renderUsageProgressBar(primaryBarEl, primary.used_percent ?? null, primary.window_minutes);
        renderUsageProgressBar(secondaryBarEl, secondary.used_percent ?? null, secondary.window_minutes);
        if (ratesEl) {
            ratesEl.textContent = `${primary.used_percent ?? "–"}%/${primary.window_minutes ?? ""}m · ${secondary.used_percent ?? "–"}%/${secondary.window_minutes ?? ""}m`;
        }
    }
    else {
        renderUsageProgressBar(primaryBarEl, null, null);
        renderUsageProgressBar(secondaryBarEl, null, null);
        if (ratesEl)
            ratesEl.textContent = "–";
    }
    if (metaEl)
        metaEl.textContent = codexHome;
}
async function loadMessageStats() {
    try {
        const data = (await api("/api/messages/threads"));
        const threads = Array.isArray(data?.threads) ? data.threads : [];
        if (!threads.length) {
            latestMessageStats = {
                handoffs: 0,
                replies: 0,
                currentTicket: null,
                totalTurns: null,
            };
        }
        else {
            const primary = threads.find((t) => t.status === "paused") ||
                threads[0];
            latestMessageStats = {
                handoffs: primary.handoff_count ?? 0,
                replies: primary.reply_count ?? 0,
                currentTicket: primary.ticket_state?.current_ticket || null,
                totalTurns: primary.ticket_state?.total_turns ?? null,
            };
        }
    }
    catch (_err) {
        // best effort
        latestMessageStats = latestMessageStats || null;
    }
    renderMessageStats();
}
function renderMessageStats() {
    const stats = latestMessageStats;
    const handoffsEl = document.getElementById("message-handoffs");
    const repliesEl = document.getElementById("message-replies");
    const ticketEl = document.getElementById("ticket-active");
    const turnsEl = document.getElementById("ticket-turns");
    const handoffs = stats?.handoffs ?? 0;
    const replies = stats?.replies ?? 0;
    const ticket = stats?.currentTicket || "–";
    const turns = stats?.totalTurns;
    if (handoffsEl)
        handoffsEl.textContent = String(handoffs);
    if (repliesEl)
        repliesEl.textContent = String(replies);
    if (ticketEl)
        ticketEl.textContent = ticket;
    if (turnsEl)
        turnsEl.textContent = turns != null ? String(turns) : "–";
}
function buildUsageSeriesQuery() {
    const params = new URLSearchParams();
    const now = new Date();
    const since = new Date(now.getTime() - usageChartState.windowDays * 86400000);
    const bucket = usageChartState.windowDays >= 180 ? "week" : usageChartState.bucket;
    params.set("since", since.toISOString());
    params.set("until", now.toISOString());
    params.set("bucket", bucket);
    params.set("segment", usageChartState.segment);
    return params.toString();
}
function renderUsageChart(data) {
    const container = document.getElementById("usage-chart-canvas");
    if (!container)
        return;
    const buckets = data?.buckets || [];
    const series = data?.series || [];
    const isLoading = data?.status === "loading";
    if (!buckets.length || !series.length) {
        container.__usageChartBound = false;
        container.innerHTML = isLoading
            ? '<div class="usage-chart-empty">Loading…</div>'
            : '<div class="usage-chart-empty">No data</div>';
        return;
    }
    const { width, height } = getChartSize(container, 320, 88);
    const padding = 8;
    const chartWidth = width - padding * 2;
    const chartHeight = height - padding * 2;
    const colors = [
        "#6cf5d8",
        "#6ca8ff",
        "#f5b86c",
        "#f56c8a",
        "#84d1ff",
        "#9be26f",
        "#f2a0c5",
    ];
    const { series: displaySeries } = normalizeSeries(limitSeries(series, 4, "rest").series, buckets.length);
    let scaleMax = 1;
    const totals = new Array(buckets.length).fill(0);
    displaySeries.forEach((entry) => {
        (entry.values || []).forEach((value, i) => {
            totals[i] += value;
        });
    });
    scaleMax = Math.max(...totals, 1);
    let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMinYMin meet" role="img" aria-label="Token usage trend">`;
    svg += `
    <defs></defs>
  `;
    const gridLines = 3;
    for (let i = 1; i <= gridLines; i += 1) {
        const y = padding + (chartHeight / (gridLines + 1)) * i;
        svg += `<line x1="${padding}" y1="${y}" x2="${padding + chartWidth}" y2="${y}" stroke="rgba(108, 245, 216, 0.12)" stroke-width="1" />`;
    }
    const maxLabel = formatTokensAxis(scaleMax);
    const midLabel = formatTokensAxis(scaleMax / 2);
    svg += `<text x="${padding}" y="${padding + 10}" fill="rgba(203, 213, 225, 0.7)" font-size="8">${maxLabel}</text>`;
    svg += `<text x="${padding}" y="${padding + chartHeight / 2 + 4}" fill="rgba(203, 213, 225, 0.6)" font-size="8">${midLabel}</text>`;
    svg += `<text x="${padding}" y="${padding + chartHeight + 2}" fill="rgba(203, 213, 225, 0.5)" font-size="8">0</text>`;
    const count = buckets.length;
    const barWidth = count ? chartWidth / count : chartWidth;
    const gap = Math.max(1, Math.round(barWidth * 0.2));
    const usableWidth = Math.max(1, barWidth - gap);
    if (usageChartState.segment === "none") {
        const values = displaySeries[0]?.values || [];
        values.forEach((value, i) => {
            const x = padding + i * barWidth + gap / 2;
            const h = (value / scaleMax) * chartHeight;
            const y = padding + chartHeight - h;
            svg += `<rect x="${x}" y="${y}" width="${usableWidth}" height="${h}" fill="#6cf5d8" opacity="0.75" rx="2" />`;
        });
    }
    else {
        const accum = new Array(count).fill(0);
        displaySeries.forEach((entry, idx) => {
            const color = colors[idx % colors.length];
            const values = entry.values || [];
            values.forEach((value, i) => {
                if (!value)
                    return;
                const base = accum[i];
                accum[i] += value;
                const h = (value / scaleMax) * chartHeight;
                const y = padding + chartHeight - (base / scaleMax) * chartHeight - h;
                const x = padding + i * barWidth + gap / 2;
                svg += `<rect x="${x}" y="${y}" width="${usableWidth}" height="${h}" fill="${color}" opacity="0.55" rx="2" />`;
            });
        });
    }
    svg += "</svg>";
    container.__usageChartBound = false;
    container.innerHTML = svg;
    attachUsageChartInteraction(container, {
        buckets,
        series: displaySeries,
        segment: usageChartState.segment,
        scaleMax,
        width,
        height,
        padding,
        chartWidth,
        chartHeight,
    });
}
function getChartSize(container, fallbackWidth, fallbackHeight) {
    const rect = container.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width || fallbackWidth));
    const height = Math.max(1, Math.round(rect.height || fallbackHeight));
    return { width, height };
}
function limitSeries(series, maxSeries, restKey) {
    if (series.length <= maxSeries)
        return { series };
    const sorted = [...series].sort((a, b) => (b.total || 0) - (a.total || 0));
    const top = sorted.slice(0, maxSeries).filter((entry) => (entry.total || 0) > 0);
    const rest = sorted.slice(maxSeries);
    if (!rest.length)
        return { series: top };
    const values = new Array((top[0]?.values || []).length).fill(0);
    rest.forEach((entry) => {
        (entry.values || []).forEach((value, i) => {
            values[i] += value;
        });
    });
    const total = values.reduce((sum, value) => sum + value, 0);
    if (total > 0) {
        top.push({ key: restKey, model: null, token_type: null, total, values });
    }
    return { series: top.length ? top : series };
}
function normalizeSeries(series, length) {
    const normalized = series.map((entry) => {
        const values = (entry.values || []).slice(0, length);
        while (values.length < length)
            values.push(0);
        return { ...entry, values, total: values.reduce((sum, v) => sum + v, 0) };
    });
    return { series: normalized };
}
function setChartLoading(container, loading) {
    if (!container)
        return;
    container.classList.toggle("loading", loading);
}
function attachUsageChartInteraction(container, state) {
    container.__usageChartState = state;
    if (container.__usageChartBound)
        return;
    container.__usageChartBound = true;
    const focus = document.createElement("div");
    focus.className = "usage-chart-focus";
    const dot = document.createElement("div");
    dot.className = "usage-chart-dot";
    const tooltip = document.createElement("div");
    tooltip.className = "usage-chart-tooltip";
    container.appendChild(focus);
    container.appendChild(dot);
    container.appendChild(tooltip);
    const updateTooltip = (event) => {
        const chartState = container.__usageChartState;
        if (!chartState)
            return;
        const rect = container.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const normalizedX = (x / rect.width) * chartState.width;
        const count = chartState.buckets.length;
        const usableWidth = chartState.chartWidth;
        const localX = Math.min(Math.max(normalizedX - chartState.padding, 0), usableWidth);
        const barWidth = count ? usableWidth / count : usableWidth;
        const index = Math.floor(localX / barWidth);
        const clampedIndex = Math.max(0, Math.min(chartState.buckets.length - 1, index));
        const xPos = chartState.padding + clampedIndex * barWidth + barWidth / 2;
        const totals = chartState.series.reduce((sum, entry) => {
            return sum + (entry.values?.[clampedIndex] || 0);
        }, 0);
        const yPos = chartState.padding +
            chartState.chartHeight -
            (totals / chartState.scaleMax) * chartState.chartHeight;
        focus.style.opacity = "1";
        dot.style.opacity = "1";
        focus.style.left = `${(xPos / chartState.width) * 100}%`;
        dot.style.left = `${(xPos / chartState.width) * 100}%`;
        dot.style.top = `${(yPos / chartState.height) * 100}%`;
        const bucketLabel = chartState.buckets[clampedIndex];
        const rows = [];
        rows.push(`<div class="usage-chart-tooltip-row"><span>Total</span><span>${formatTokensCompact(totals)}</span></div>`);
        if (chartState.segment !== "none") {
            const ranked = chartState.series
                .map((entry) => ({
                key: entry.key || "unknown",
                value: entry.values?.[clampedIndex] || 0,
            }))
                .filter((entry) => entry.value > 0)
                .sort((a, b) => b.value - a.value)
                .slice(0, 4);
            ranked.forEach((entry) => {
                rows.push(`<div class="usage-chart-tooltip-row"><span>${entry.key}</span><span>${formatTokensCompact(entry.value)}</span></div>`);
            });
        }
        tooltip.innerHTML = `<div class="usage-chart-tooltip-title">${bucketLabel}</div>${rows.join("")}`;
        const tooltipRect = tooltip.getBoundingClientRect();
        let tooltipLeft = x + 10;
        if (tooltipLeft + tooltipRect.width > rect.width) {
            tooltipLeft = x - tooltipRect.width - 10;
        }
        tooltipLeft = Math.max(6, tooltipLeft);
        const tooltipTop = 6;
        tooltip.style.opacity = "1";
        tooltip.style.transform = `translate(${tooltipLeft}px, ${tooltipTop}px)`;
    };
    container.addEventListener("pointermove", updateTooltip);
    container.addEventListener("pointerleave", () => {
        focus.style.opacity = "0";
        dot.style.opacity = "0";
        tooltip.style.opacity = "0";
    });
}
async function loadUsageSeries() {
    const container = document.getElementById("usage-chart-canvas");
    try {
        const data = await api(`/api/usage/series?${buildUsageSeriesQuery()}`);
        setChartLoading(container, data?.status === "loading");
        renderUsageChart(data);
        if (data?.status === "loading") {
            scheduleUsageSeriesRetry();
        }
        else {
            clearUsageSeriesRetry();
        }
    }
    catch (err) {
        setChartLoading(container, false);
        renderUsageChart(null);
        clearUsageSeriesRetry();
    }
}
function scheduleUsageSeriesRetry() {
    clearUsageSeriesRetry();
    usageSeriesRetryTimer = setTimeout(() => {
        loadUsageSeries();
    }, 1500);
}
function clearUsageSeriesRetry() {
    if (usageSeriesRetryTimer) {
        clearTimeout(usageSeriesRetryTimer);
        usageSeriesRetryTimer = null;
    }
}
function scheduleUsageSummaryRetry() {
    clearUsageSummaryRetry();
    usageSummaryRetryTimer = setTimeout(() => {
        loadUsage();
    }, 1500);
}
function clearUsageSummaryRetry() {
    if (usageSummaryRetryTimer) {
        clearTimeout(usageSummaryRetryTimer);
        usageSummaryRetryTimer = null;
    }
}
async function loadUsage() {
    setUsageLoading(true);
    try {
        const data = await api("/api/usage");
        const cachedUsage = loadFromCache("usage");
        const hasSummary = data && data.totals && typeof data.events === "number";
        if (data?.status === "loading") {
            if (hasSummary) {
                renderUsage(data);
            }
            else if (cachedUsage) {
                renderUsage(cachedUsage);
            }
            else {
                renderUsage(data);
            }
            scheduleUsageSummaryRetry();
        }
        else {
            renderUsage(data);
            clearUsageSummaryRetry();
        }
        loadUsageSeries();
    }
    catch (err) {
        const cachedUsage = loadFromCache("usage");
        if (cachedUsage) {
            renderUsage(cachedUsage);
        }
        else {
            renderUsage(null);
        }
        flash(err.message || "Failed to load usage", "error");
        clearUsageSummaryRetry();
    }
    finally {
        setUsageLoading(false);
    }
}
const UPDATE_TARGET_LABELS = {
    both: "web + Telegram",
    web: "web only",
    telegram: "Telegram only",
};
function normalizeUpdateTarget(value) {
    if (!value)
        return "both";
    if (value === "both" || value === "web" || value === "telegram")
        return value;
    return "both";
}
function getUpdateTarget(selectId) {
    const select = selectId ? document.getElementById(selectId) : null;
    return normalizeUpdateTarget(select ? select.value : "both");
}
function describeUpdateTarget(target) {
    return UPDATE_TARGET_LABELS[target] || UPDATE_TARGET_LABELS.both;
}
async function handleSystemUpdate(btnId, targetSelectId) {
    const btn = document.getElementById(btnId);
    if (!btn)
        return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Checking...";
    const updateTarget = getUpdateTarget(targetSelectId);
    const targetLabel = describeUpdateTarget(updateTarget);
    let check;
    try {
        check = await api("/system/update/check");
    }
    catch (err) {
        check = { update_available: true, message: err.message || "Unable to check for updates." };
    }
    if (!check?.update_available) {
        flash(check?.message || "No update available.", "info");
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    const restartNotice = updateTarget === "telegram"
        ? "The Telegram bot will restart."
        : "The service will restart.";
    const confirmed = await confirmModal(`${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`);
    if (!confirmed) {
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    btn.textContent = "Updating...";
    try {
        const res = await api("/system/update", {
            method: "POST",
            body: { target: updateTarget },
        });
        flash(res.message || `Update started (${targetLabel}).`, "success");
        if (updateTarget === "telegram") {
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }
        document.body.style.pointerEvents = "none";
        setTimeout(() => {
            const url = new URL(window.location.href);
            url.searchParams.set("v", String(Date.now()));
            window.location.replace(url.toString());
        }, 8000);
    }
    catch (err) {
        flash(err.message || "Update failed", "error");
        btn.disabled = false;
        btn.textContent = originalText;
    }
}
function initSettings() {
    const settingsBtn = document.getElementById("repo-settings");
    const modal = document.getElementById("repo-settings-modal");
    const closeBtn = document.getElementById("repo-settings-close");
    const updateBtn = document.getElementById("repo-update-btn");
    const updateTarget = document.getElementById("repo-update-target");
    let closeModal = null;
    const hideModal = () => {
        if (closeModal) {
            const close = closeModal;
            closeModal = null;
            close();
        }
    };
    if (settingsBtn && modal) {
        settingsBtn.addEventListener("click", () => {
            const triggerEl = document.activeElement;
            hideModal();
            closeModal = openModal(modal, {
                initialFocus: closeBtn || updateBtn || modal,
                returnFocusTo: triggerEl,
                onRequestClose: hideModal,
            });
        });
    }
    if (closeBtn && modal) {
        closeBtn.addEventListener("click", () => {
            hideModal();
        });
    }
    if (updateBtn) {
        updateBtn.addEventListener("click", () => handleSystemUpdate("repo-update-btn", updateTarget ? updateTarget.id : null));
    }
}
function initUsageChartControls() {
    const segmentSelect = document.getElementById("usage-chart-segment");
    const rangeSelect = document.getElementById("usage-chart-range");
    if (segmentSelect) {
        segmentSelect.value = usageChartState.segment;
        segmentSelect.addEventListener("change", () => {
            usageChartState.segment = segmentSelect.value;
            loadUsageSeries();
        });
    }
    if (rangeSelect) {
        rangeSelect.value = String(usageChartState.windowDays);
        rangeSelect.addEventListener("change", () => {
            const value = Number(rangeSelect.value);
            usageChartState.windowDays = Number.isNaN(value)
                ? usageChartState.windowDays
                : value;
            loadUsageSeries();
        });
    }
}
function bindAction(buttonId, action) {
    const btn = document.getElementById(buttonId);
    if (!btn)
        return;
    btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.classList.add("loading");
        try {
            await action();
        }
        catch (err) {
            flash(err.message || "Error", "error");
        }
        finally {
            btn.disabled = false;
            btn.classList.remove("loading");
        }
    });
}
function isDocsReady() {
    return document.body?.dataset?.docsReady === "true";
}
function openSummaryDoc() {
    const summaryChip = document.querySelector('.chip[data-doc="summary"]');
    if (summaryChip)
        summaryChip.click();
}
export function initDashboard() {
    initSettings();
    initUsageChartControls();
    // initReview(); // Removed - review.ts was deleted
    subscribe("todo:invalidate", () => {
        void loadTodoPreview({ silent: true });
    });
    subscribe("docs:updated", handleDocsEvent);
    subscribe("docs:loaded", handleDocsEvent);
    subscribe("docs:ready", () => {
        if (!isDocsReady()) {
            if (document.body) {
                document.body.dataset.docsReady = "true";
            }
        }
        if (pendingSummaryOpen) {
            pendingSummaryOpen = false;
            openSummaryDoc();
        }
    });
    bindAction("usage-refresh", loadUsage);
    bindAction("refresh-preview", loadTodoPreview);
    const cachedUsage = loadFromCache("usage");
    if (cachedUsage)
        renderUsage(cachedUsage);
    const cachedTodo = loadFromCache("todo-doc");
    if (typeof cachedTodo === "string") {
        updateTodoPreview(cachedTodo);
    }
    const summaryBtn = document.getElementById("open-summary");
    if (summaryBtn) {
        summaryBtn.addEventListener("click", () => {
            const docsTab = document.querySelector('.tab[data-target="docs"]');
            if (docsTab)
                docsTab.click();
            if (isDocsReady()) {
                requestAnimationFrame(openSummaryDoc);
            }
            else {
                pendingSummaryOpen = true;
            }
        });
    }
    loadUsage();
    loadTodoPreview();
    loadVersion();
    checkUpdateStatus();
    loadMessageStats();
    registerAutoRefresh("dashboard-usage", {
        callback: async () => { await loadUsage(); },
        tabId: "analytics",
        interval: CONSTANTS.UI.AUTO_REFRESH_USAGE_INTERVAL,
        refreshOnActivation: true,
        immediate: false,
    });
    registerAutoRefresh("message-stats", {
        callback: loadMessageStats,
        tabId: "analytics",
        interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
        refreshOnActivation: true,
        immediate: true,
    });
}
async function loadVersion() {
    const versionEl = document.getElementById("repo-version");
    if (!versionEl)
        return;
    try {
        const data = await api("/api/version", { method: "GET" });
        const version = data?.asset_version || "";
        versionEl.textContent = version ? `v${version}` : "v–";
    }
    catch (_err) {
        versionEl.textContent = "v–";
    }
}
async function checkUpdateStatus() {
    try {
        const data = await api("/system/update/status", { method: "GET" });
        if (!data || !data.status)
            return;
        const stamp = data.at ? String(data.at) : "";
        if (stamp && sessionStorage.getItem(UPDATE_STATUS_SEEN_KEY) === stamp)
            return;
        if (data.status === "rollback" || data.status === "error") {
            flash(data.message || "Update failed; rollback attempted.", "error");
        }
        if (stamp)
            sessionStorage.setItem(UPDATE_STATUS_SEEN_KEY, stamp);
    }
    catch (_err) {
        // ignore
    }
}
