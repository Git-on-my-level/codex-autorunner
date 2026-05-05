import { escapeHtml } from "./utils.js";

function escapeAttr(value: string): string {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

function isSafeHref(url: string): boolean {
  const trimmed = (url || "").trim();
  if (!trimmed) return false;
  const lower = trimmed.toLowerCase();
  if (lower.startsWith("javascript:")) return false;
  if (lower.startsWith("data:")) return false;
  if (lower.startsWith("vbscript:")) return false;
  if (lower.startsWith("file:")) return false;

  return (
    lower.startsWith("http://") ||
    lower.startsWith("https://") ||
    trimmed.startsWith("/") ||
    trimmed.startsWith("./") ||
    trimmed.startsWith("../") ||
    trimmed.startsWith("#") ||
    lower.startsWith("mailto:")
  );
}

function stashCodeBlocks(text: string): { text: string; codeBlocks: string[] } {
  const codeBlocks: string[] = [];
  const lines = text.split(/\n/);
  const out: string[] = [];
  let inFence = false;
  let fence: string[] = [];

  const flushFence = (): void => {
    const placeholder = `@@CODEBLOCK_${codeBlocks.length}@@`;
    codeBlocks.push(`<pre class="md-code"><code>${fence.join("\n")}</code></pre>`);
    out.push(placeholder);
    fence = [];
  };

  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      if (inFence) {
        flushFence();
        inFence = false;
      } else {
        inFence = true;
        fence = [];
      }
      continue;
    }
    if (inFence) {
      fence.push(line);
    } else {
      out.push(line);
    }
  }
  if (inFence) {
    flushFence();
  }
  return { text: out.join("\n"), codeBlocks };
}

function renderInlineMarkdown(text: string): string {
  const inlineCode: string[] = [];
  text = text.replace(/`([^`\n]+)`/g, (_m, code) => {
    const placeholder = `@@INLINECODE_${inlineCode.length}@@`;
    inlineCode.push(`<code>${code}</code>`);
    return placeholder;
  });
  // Be forgiving with a dangling inline-code marker at end-of-line.
  text = text.replace(/`([^`\n]+)(?=$|\n)/g, (_m, code) => {
    const placeholder = `@@INLINECODE_${inlineCode.length}@@`;
    inlineCode.push(`<code>${code}</code>`);
    return placeholder;
  });

  const links: string[] = [];
  text = text.replace(/\[([^\]]+)\]\(([^)\s]+(?:\s+[^)]*)?)\)/g, (match, label, rawUrl) => {
    const url = (rawUrl || "").trim();
    if (!isSafeHref(url)) {
      return match;
    }
    const placeholder = `@@LINK_${links.length}@@`;
    links.push(`<a href="${escapeAttr(url)}" target="_blank" rel="noopener">${label}</a>`);
    return placeholder;
  });

  text = text.replace(/(https?:\/\/[^\s<]+)/g, (url) => {
    let cleanUrl = url;
    let suffix = "";
    const trailing = /[.,;!?)]$/;
    while (trailing.test(cleanUrl)) {
      suffix = cleanUrl.slice(-1) + suffix;
      cleanUrl = cleanUrl.slice(0, -1);
    }
    return `<a href="${escapeAttr(cleanUrl)}" target="_blank" rel="noopener">${cleanUrl}</a>${suffix}`;
  });

  text = text.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");

  text = text.replace(/@@LINK_(\d+)@@/g, (_m, id) => {
    return links[Number(id)] ?? "";
  });

  return text.replace(/@@INLINECODE_(\d+)@@/g, (_m, id) => {
    return inlineCode[Number(id)] ?? "";
  });
}

type ListKind = "ul" | "ol";

export function renderMarkdown(body?: string | null): string {
  if (!body) return "";
  const stashed = stashCodeBlocks(escapeHtml(body));
  const text = renderInlineMarkdown(stashed.text);
  const { codeBlocks } = stashed;

  const lines = text.split(/\n/);
  const out: string[] = [];
  let paragraph: string[] = [];
  let listKind: ListKind | null = null;

  const closeParagraph = (): void => {
    if (!paragraph.length) return;
    out.push(`<p>${paragraph.join("<br>")}</p>`);
    paragraph = [];
  };

  const closeList = (): void => {
    if (!listKind) return;
    out.push(`</${listKind}>`);
    listKind = null;
  };

  const openList = (kind: ListKind): void => {
    if (listKind === kind) return;
    closeList();
    closeParagraph();
    out.push(`<${kind}>`);
    listKind = kind;
  };

  lines.forEach((line) => {
    const trimmed = line.trim();

    if (!trimmed) {
      closeParagraph();
      closeList();
      return;
    }

    const codeMatch = trimmed.match(/^@@CODEBLOCK_(\d+)@@$/);
    if (codeMatch) {
      closeParagraph();
      closeList();
      out.push(codeBlocks[Number(codeMatch[1])] ?? "");
      return;
    }

    const heading = trimmed.match(/^(#{1,6})\s*(.+)$/);
    if (heading) {
      closeParagraph();
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${heading[2]}</h${level}>`);
      return;
    }

    if (/^[-*_]{3,}$/.test(trimmed)) {
      closeParagraph();
      closeList();
      out.push("<hr>");
      return;
    }

    const quote = trimmed.match(/^>\s?(.*)$/);
    if (quote) {
      closeParagraph();
      closeList();
      out.push(`<blockquote>${quote[1]}</blockquote>`);
      return;
    }

    const bullet = trimmed.match(/^[-*+]\s+(.+)$/);
    if (bullet) {
      openList("ul");
      out.push(`<li>${bullet[1]}</li>`);
      return;
    }

    const numbered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (numbered) {
      openList("ol");
      out.push(`<li>${numbered[1]}</li>`);
      return;
    }

    closeList();
    paragraph.push(line);
  });
  closeParagraph();
  closeList();

  return out.join("");
}
