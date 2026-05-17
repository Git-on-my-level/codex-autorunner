import DOMPurify from 'isomorphic-dompurify';
import { marked } from 'marked';

// Tuned for ergonomics over strictness — LLMs (and humans drafting tickets in
// a hurry) often skip blank lines between block elements, mix unordered and
// ordered lists, leave URLs unbracketed, etc. We lean on marked's GFM mode
// plus `breaks: true` (single \n → <br>) to forgive most of those mistakes.
marked.setOptions({
  gfm: true,
  breaks: true,
  pedantic: false
});

// Keep DOMPurify config narrow: forbid scripts/event handlers, force any link
// to be safe (`http`, `https`, `mailto`, relative paths). We render output via
// Svelte's `{@html …}` so this layer is the trust boundary.
const PURIFY_CONFIG = {
  USE_PROFILES: { html: true },
  FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form'],
  FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick', 'onmouseover']
};

const MAX_RENDER_CACHE_ENTRIES = 250;
const MAX_RENDER_CACHE_CHARS = 1_000_000;
const renderCache = new Map<string, string>();
let renderCacheChars = 0;

export type RenderMarkdownOptions = {
  /** When true, every `<a>` gets `target="_blank"` and `rel="noopener noreferrer"`. */
  openLinksInNewTab?: boolean;
};

function stripAnchorNavigationAttrs(attrs: string): string {
  return attrs
    .replace(/\starget\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/\srel\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .trim();
}

function forceMarkdownLinksOpenInNewTab(html: string): string {
  return html.replace(/<a\b([^>]*)>/gi, (_full, attrs: string) => {
    const rest = stripAnchorNavigationAttrs(attrs);
    return rest
      ? `<a ${rest} target="_blank" rel="noopener noreferrer">`
      : `<a target="_blank" rel="noopener noreferrer">`;
  });
}

export function renderMarkdownToHtml(markdown: string, options?: RenderMarkdownOptions): string {
  if (!markdown) return '';
  const cacheKey = `${options?.openLinksInNewTab ? '1' : '0'}:${markdown}`;
  const cached = renderCache.get(cacheKey);
  if (cached !== undefined) {
    renderCache.delete(cacheKey);
    renderCache.set(cacheKey, cached);
    return cached;
  }
  const rawHtml = marked.parse(markdown, { async: false }) as string;
  const purified = DOMPurify.sanitize(rawHtml, PURIFY_CONFIG);
  // Strip any href that survived sanitize but resolves to a dangerous scheme.
  // DOMPurify already blocks `javascript:` by default, but we also reject
  // `data:` and `vbscript:` to match the legacy renderer's contract.
  let out = purified.replace(/href="(data|vbscript):[^"]*"/gi, 'href="#"');
  if (options?.openLinksInNewTab) out = forceMarkdownLinksOpenInNewTab(out);
  rememberRenderedMarkdown(cacheKey, out);
  return out;
}

function rememberRenderedMarkdown(cacheKey: string, html: string): void {
  renderCache.set(cacheKey, html);
  renderCacheChars += cacheKey.length + html.length;
  while (renderCache.size > MAX_RENDER_CACHE_ENTRIES || renderCacheChars > MAX_RENDER_CACHE_CHARS) {
    const oldestKey = renderCache.keys().next().value;
    if (oldestKey === undefined) break;
    const oldestValue = renderCache.get(oldestKey) ?? '';
    renderCache.delete(oldestKey);
    renderCacheChars -= oldestKey.length + oldestValue.length;
  }
}
