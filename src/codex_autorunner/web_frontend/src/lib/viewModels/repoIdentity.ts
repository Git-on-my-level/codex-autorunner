/** Deterministic repo avatar initials from a display label (path segments, etc.). */
export function repoInitials(label: string): string {
  if (!label) return '·';
  const cleaned = label.replace(/[_\-./]+/g, ' ').trim();
  const words = cleaned.split(/\s+/).filter(Boolean);
  if (words.length === 0) return label.slice(0, 1).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/** Deterministic accent color per repo based on label hash. */
export function repoAccent(label: string): string {
  // Identity hues only: cool blues, teals, greens, violets — avoid amber/red/rose
  // that read as warning/error states (those mirror semantic tokens elsewhere).
  const palette = [
    '#5b5fc7',
    '#117a4d',
    '#2563eb',
    '#6366f1',
    '#0f7285',
    '#7a3fb8',
    '#0891b2',
    '#8b5cf6'
  ];
  let hash = 0;
  for (let i = 0; i < label.length; i++) {
    hash = (hash * 31 + label.charCodeAt(i)) >>> 0;
  }
  return palette[hash % palette.length];
}
