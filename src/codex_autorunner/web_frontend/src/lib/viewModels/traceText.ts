const MIN_WHOLE_UNIT_CHARS = 16;
const MIN_WHOLE_REPETITIONS = 2;

/**
 * Collapse obvious exact-duplicate repetition in trace bodies. This is a
 * conservative, agent-agnostic safety net when backend merge/dedup misses.
 */
export function collapseRepeatedParagraphs(text: string): string {
  if (!text) return text;

  const whole = collapseExactWholeRepetition(text);
  if (whole !== text) return whole;

  if (/\n\n/.test(text)) {
    const collapsed = collapseConsecutiveDuplicateBlocks(text, /\n\n+/, '\n\n');
    if (collapsed !== text) return collapsed;
  }

  if (text.includes('\n')) {
    const collapsed = collapseConsecutiveDuplicateBlocks(text, /\n/, '\n');
    if (collapsed !== text) return collapsed;
  }

  return text;
}

function collapseExactWholeRepetition(text: string): string {
  const len = text.length;
  if (len < MIN_WHOLE_UNIT_CHARS * MIN_WHOLE_REPETITIONS) return text;

  const maxUnitLen = Math.floor(len / MIN_WHOLE_REPETITIONS);
  for (let unitLen = MIN_WHOLE_UNIT_CHARS; unitLen <= maxUnitLen; unitLen += 1) {
    if (len % unitLen !== 0) continue;
    const repeats = len / unitLen;
    if (repeats < MIN_WHOLE_REPETITIONS) continue;
    const unit = text.slice(0, unitLen);
    if (unit.repeat(repeats) === text) return unit;
  }

  return text;
}

function collapseConsecutiveDuplicateBlocks(text: string, separator: RegExp, joiner: string): string {
  const blocks = text.split(separator);
  if (blocks.length < MIN_WHOLE_REPETITIONS) return text;

  const collapsed: string[] = [];
  for (const block of blocks) {
    if (collapsed.length > 0 && block === collapsed[collapsed.length - 1]) continue;
    collapsed.push(block);
  }

  if (collapsed.length === blocks.length) return text;

  return collapsed.join(joiner);
}
