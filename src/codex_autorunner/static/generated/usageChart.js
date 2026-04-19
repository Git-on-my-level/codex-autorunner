export function formatTokensCompact(val) {
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
export function formatTokensAxis(val) {
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
export function usageSeriesSignature(data) {
    if (!data)
        return "none";
    const buckets = data.buckets || [];
    const series = data.series || [];
    const seriesSig = series
        .map((entry) => {
        const values = entry.values || [];
        return [
            entry.key ?? "",
            entry.model ?? "",
            entry.token_type ?? "",
            entry.total ?? "",
            values.join(","),
        ].join(":");
    })
        .join("|");
    return `${data.status || ""}::${buckets.join(",")}::${seriesSig}`;
}
export function getChartSize(container, fallbackWidth, fallbackHeight) {
    const rect = container.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width || fallbackWidth));
    const height = Math.max(1, Math.round(rect.height || fallbackHeight));
    return { width, height };
}
export function limitSeries(series, maxSeries, restKey) {
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
export function normalizeSeries(series, length) {
    const normalized = series.map((entry) => {
        const values = (entry.values || []).slice(0, length);
        while (values.length < length)
            values.push(0);
        return { ...entry, values, total: values.reduce((sum, v) => sum + v, 0) };
    });
    return { series: normalized };
}
export function setChartLoading(container, loading) {
    if (!container)
        return;
    container.classList.toggle("loading", loading);
}
export function renderUsageChart(data, segment) {
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
        "#373",
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
    if (segment === "none") {
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
        segment,
        scaleMax,
        width,
        height,
        padding,
        chartWidth,
        chartHeight,
    });
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
