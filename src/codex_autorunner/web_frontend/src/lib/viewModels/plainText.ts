/**
 * Markdown → single-line plain text, for previews and summaries.
 *
 * List rows, collapsed disclosure summaries, and card teasers show an excerpt
 * of a body that is authored in markdown. Rendering the raw source there leaks
 * syntax into the UI — a ticket row reading `## Goal Fixture ticket used by
 * \`scripts/x.py\`. ## Evidence — …` spends most of its width on punctuation
 * the user cannot act on. Strip the syntax so the excerpt reads as a sentence;
 * the full body still renders as real markdown wherever it is displayed.
 */
export function markdownToPlainText(value: string | null | undefined): string {
  if (!value) return '';
  return value
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[*_~]{1,3}([^*_~]+)[*_~]{1,3}/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * `markdownToPlainText` truncated to `max` characters with a single-character
 * ellipsis. Returns an empty string when nothing survives stripping, so callers
 * can fall back to a label instead of rendering an empty row.
 */
export function markdownPreview(value: string | null | undefined, max = 120): string {
  const stripped = markdownToPlainText(value);
  if (!stripped) return '';
  return stripped.length > max ? `${stripped.slice(0, max).trimEnd()}…` : stripped;
}
