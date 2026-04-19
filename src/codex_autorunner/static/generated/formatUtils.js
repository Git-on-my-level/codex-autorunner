export function formatTimestamp(ts) {
    if (!ts)
        return "\u2013";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime()))
        return ts;
    return date.toLocaleString();
}
export function formatBytes(bytes) {
    if (bytes === null || bytes === undefined)
        return "\u2013";
    if (bytes < 1024)
        return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024)
        return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    return `${mb.toFixed(1)} MB`;
}
