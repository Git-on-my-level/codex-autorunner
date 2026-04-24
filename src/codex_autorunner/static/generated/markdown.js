// GENERATED FILE - do not edit directly. Source: static_src/
import { escapeHtml } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
function isSafeHref(url) {
    const trimmed = (url || "").trim();
    if (!trimmed)
        return false;
    const lower = trimmed.toLowerCase();
    if (lower.startsWith("javascript:"))
        return false;
    if (lower.startsWith("data:"))
        return false;
    if (lower.startsWith("vbscript:"))
        return false;
    if (lower.startsWith("file:"))
        return false;
    return (lower.startsWith("http://") ||
        lower.startsWith("https://") ||
        trimmed.startsWith("/") ||
        trimmed.startsWith("./") ||
        trimmed.startsWith("../") ||
        trimmed.startsWith("#") ||
        lower.startsWith("mailto:"));
}
export function renderMarkdown(body) {
    if (!body)
        return "";
    let text = escapeHtml(body);
    const codeBlocks = [];
    text = text.replace(/```([\s\S]*?)```/g, (_m, code) => {
        const placeholder = `@@CODEBLOCK_${codeBlocks.length}@@`;
        codeBlocks.push(`<pre class="md-code"><code>${code}</code></pre>`);
        return placeholder;
    });
    const inlineCode = [];
    text = text.replace(/`([^`]+)`/g, (_m, code) => {
        const placeholder = `@@INLINECODE_${inlineCode.length}@@`;
        inlineCode.push(`<code>${code}</code>`);
        return placeholder;
    });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    const links = [];
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, rawUrl) => {
        const url = (rawUrl || "").trim();
        if (!isSafeHref(url)) {
            return match;
        }
        const placeholder = `@@LINK_${links.length}@@`;
        links.push(`<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
        return placeholder;
    });
    text = text.replace(/(https?:\/\/[^\s]+)/g, (url) => {
        let cleanUrl = url;
        let suffix = "";
        const trailing = /[.,;!?)]$/;
        while (trailing.test(cleanUrl)) {
            suffix = cleanUrl.slice(-1) + suffix;
            cleanUrl = cleanUrl.slice(0, -1);
        }
        return `<a href="${cleanUrl}" target="_blank" rel="noopener">${cleanUrl}</a>${suffix}`;
    });
    text = text.replace(/@@LINK_(\d+)@@/g, (_m, id) => {
        return links[Number(id)] ?? "";
    });
    text = text.replace(/@@INLINECODE_(\d+)@@/g, (_m, id) => {
        return inlineCode[Number(id)] ?? "";
    });
    const lines = text.split(/\n/);
    const out = [];
    let inList = false;
    lines.forEach((line) => {
        if (/^@@CODEBLOCK_\d+@@$/.test(line)) {
            if (inList) {
                out.push("</ul>");
                inList = false;
            }
            out.push(line);
            return;
        }
        if (/^[-*]\s+/.test(line)) {
            if (!inList) {
                out.push("", "<ul>");
                inList = true;
            }
            out.push(`<li>${line.replace(/^[-*]\s+/, "")}</li>`);
        }
        else {
            if (inList) {
                out.push("</ul>", "");
                inList = false;
            }
            out.push(line);
        }
    });
    if (inList)
        out.push("</ul>", "");
    const joined = out.join("\n");
    return joined
        .split(/\n\n+/)
        .map((block) => {
        if (block.trim().startsWith("<ul>")) {
            return block;
        }
        const match = block.match(/^@@CODEBLOCK_(\d+)@@$/);
        if (match) {
            const idx = Number(match[1]);
            return codeBlocks[idx] ?? "";
        }
        const content = block.replace(/\n/g, "<br>").replace(/@@CODEBLOCK_(\d+)@@/g, (_m, id) => codeBlocks[Number(id)] ?? "");
        return `<p>${content}</p>`;
    })
        .join("");
}
