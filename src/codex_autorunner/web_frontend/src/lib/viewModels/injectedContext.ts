/** Matches CAR `wrap_injected_context` / `strip_injected_context_blocks` delimiters (case-insensitive). */
const INJECTED_CONTEXT_BLOCK_RE = /<injected context>\s*[\s\S]*?\s*<\/injected context>/gi;

/**
 * Remove `<injected context>…</injected context>` blocks from text used for chat/thread titles
 * and other user-facing chrome. Aligns with backend `strip_injected_context_blocks`.
 */
export function stripInjectedContextBlocks(text: string | null | undefined): string {
  if (text == null || text === '') return '';
  const lowered = text.toLowerCase();
  if (!lowered.includes('<injected context>')) return text;
  return text.replace(INJECTED_CONTEXT_BLOCK_RE, '').replace(/\n{3,}/g, '\n\n').trim();
}
