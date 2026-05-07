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
  const palette = [
    '#5b5fc7',
    '#117a4d',
    '#9a5b00',
    '#b42424',
    '#0f7285',
    '#7a3fb8',
    '#1f6fda',
    '#c0497d'
  ];
  let hash = 0;
  for (let i = 0; i < label.length; i++) {
    hash = (hash * 31 + label.charCodeAt(i)) >>> 0;
  }
  return palette[hash % palette.length];
}
